"""Download OHLCV from Binance USD-M (perpetual) and write a data snapshot pickle.

Takes the top-N USDT perps BY 24H VOLUME among those with history >= MIN_YEARS years (by the
onboardDate listing date), downloads bars at --interval (1d default; 15m|1h|4h intraday),
and atomically overwrites the snapshot in the (tickers, ohlcvs) format — exactly what
evolution/ and AlphaNode read. 1d writes data.pickle; an intraday interval writes its own
data_<tf>.pickle, so timeframes never clobber each other. Public endpoints, no keys needed.
Each pair also gets its perp funding-rate history (a `funding` column: the rate paid within
each bar); `python fetch_data.py --add-funding [--interval 1h]` upgrades an EXISTING snapshot
with that column without refetching the klines.

  python fetch_data.py --top 150
  python fetch_data.py --top 100 --min-years 3 --start 2019-09-05
  python fetch_data.py --top 60 --interval 1h --start 2023-01-01

Why the years filter: young coins (listed recently) would give almost solid NaN in the search
window and would break the data fetcher (the wrapper walks the pre-listing period one day at a time
-> thousands of requests). We cut them off by onboardDate and start each pair's download from its listing date.

Caution: overwrites data.pickle (the old snapshot is replaced). Run it when the node is stopped.
Note: these are SURVIVING coins (Binance doesn't serve delisted ones) — survivorship remains.
"""
import os
import sys
import json
import pickle
import asyncio
import argparse
import urllib.request
from datetime import datetime, timezone

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.join(HERE, 'evolution'), os.path.join(HERE, 'alphanode')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import vision_klines as vk                                       # noqa: E402  (fapi→archive fallback)

from quantpylib.wrappers.binance import Binance                 # noqa: E402
from quantpylib.throttler.rate_semaphore import AsyncRateSemaphore  # noqa: E402
from quantpylib.throttler.exceptions import HTTPException           # noqa: E402
try:
    from timeframe import resolve as _resolve_tf   # per-timeframe recommended history start
except Exception:                                  # pragma: no cover
    _resolve_tf = None

MIN_BARS = 30                                      # final backstop: completely empty ones — skip
YEAR_SECS = 365.25 * 24 * 3600
# bar size -> the qt wrapper's (granularity, multiplier); keep in sync with evolution/timeframe.py
INTERVALS = {'15m': ('m', 15), '1h': ('h', 1), '4h': ('h', 4), '1d': ('d', 1)}
# default per-PAIR timeout by interval: intraday majors need MANY paginated requests (BTC 1h from
# 2019 ≈ 40+ x 1490-bar pages through a shared rate limiter). A flat 120s silently DROPS them.
TIMEOUTS = {'1d': 120, '4h': 360, '1h': 900, '15m': 2400}
# default concurrency by interval: intraday floods Binance with paginated kline requests; at 6
# parallel the weight limit kicks in after ~15 min and ALL downloads stall in a rate-limit pause.
# Fewer workers finish sooner in wall-clock because they never trip the cooldown.
CONCURRENCY = {'1d': 6, '4h': 4, '1h': 3, '15m': 2}
# Starter universe for a first run with no data snapshot: an explicit list of liquid majors,
# NOT top-10 by 24h turnover — today's turnover leaders can be freshly-listed memecoins with
# months of history, while these ten give years of bars and stable coverage.
DEFAULT_SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT',
                   'DOGEUSDT', 'ADAUSDT', 'LINKUSDT', 'AVAXUSDT', 'LTCUSDT')
FUNDING_TIMEOUT = 180        # per pair: funding history is ~8 LIGHT pages even for 2019 majors


def _bar_freq(interval):
    """pandas floor/reindex frequency of the bar grid for a Binance interval."""
    if _resolve_tf is not None:
        return _resolve_tf(interval).pandas_freq
    return {'1d': 'D', '4h': '4h', '1h': 'h', '15m': '15min'}[interval]


def save_pickle(path, obj):
    with open(path, 'wb') as f:
        pickle.dump(obj, f)


async def _aclose(bn):
    """Gently close the aiosonic session before the loop closes — otherwise a cosmetic SSL-transport
    traceback spills out on exit (the data is already written by that point)."""
    try:
        sess = getattr(bn.http_client, 'session', None)
        if sess is not None:
            await sess.__aexit__(None, None, None)
    except Exception:
        pass
    await asyncio.sleep(0.2)


def _onboard_ts(sym_info):
    od = sym_info.get('onboardDate')
    try:
        return int(od) / 1000.0 if od else None    # listing unix-seconds
    except (TypeError, ValueError):
        return None


def _make_binance():
    """Binance client with sane pacing. The fapi budget is 2400 weight/min and a 1500-bar kline
    page costs weight 10 → ~240 pages a minute for the whole IP. quantpylib's stock throttle
    (6000 credits / 20 per page / 60s) admits 300 pages/min — 25% OVER budget, so long intraday
    paginations trip 429 regardless of how few pairs run in parallel. Re-budget to 180 pages/min
    and, when a 429/418 still slips through, honor Retry-After and repeat the SAME page instead
    of dropping the whole pair."""
    bn = Binance()
    bn.rate_semaphore = AsyncRateSemaphore(3600)
    raw_request = bn.http_client.request

    async def paced_request(**kw):
        attempts = 8
        for i in range(attempts):
            try:
                return await raw_request(**kw)
            except HTTPException as e:
                if e.status_code not in (429, 418) or i == attempts - 1:
                    raise
                try:
                    ra = float(e.headers.get('retry-after'))
                except (TypeError, ValueError, AttributeError):
                    ra = 30.0
                wait = ra + 5 + 15 * i                     # the weight window is a rolling minute:
                print(f'  · rate-limit {e.status_code}: pausing {wait:.0f}s, '  # right after Retry-After
                      f'then retrying the same page (attempt {i + 1}/{attempts})…', flush=True)
                await asyncio.sleep(wait)

    bn.http_client.request = paced_request
    return bn


async def _funding_series(bn, sym, start, end):
    """Full funding-payment history of a perp as a rate Series indexed by payment time (UTC).
    /fapi/v1/fundingRate: public, full depth since listing, 1000 records/page ≈ 333 days."""
    rows = []
    cur = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    while cur < end_ms:
        res = await bn.http_client.request(
            endpoint='/fapi/v1/fundingRate', method='GET',
            params={'symbol': sym, 'startTime': cur, 'endTime': end_ms, 'limit': 1000})
        if not res:
            break
        rows.extend(res)
        nxt = int(res[-1]['fundingTime']) + 1
        if nxt <= cur:
            break
        cur = nxt
        if len(res) < 1000:
            break
    if not rows:
        return None
    idx = pd.to_datetime([int(r['fundingTime']) for r in rows], unit='ms', utc=True)
    ser = pd.Series([float(r['fundingRate']) for r in rows], index=idx, dtype=float).sort_index()
    return ser[~ser.index.duplicated(keep='last')]


def _resolve_source(source):
    """'auto' probes fapi once: reachable → the live API exactly as before; blocked (US geo-fence,
    DNS, firewall) → the data.binance.vision archive. Same Binance bars either way. An explicit
    api/vision wins; None or 'auto' defers to ALPHANODE_DATA_SOURCE, then to the probe."""
    mode = (source or '').strip().lower()
    if mode in ('', 'auto'):
        mode = (os.environ.get('ALPHANODE_DATA_SOURCE') or 'auto').strip().lower()
    if mode not in ('auto', 'api', 'vision'):
        print(f'  unknown source "{mode}" → auto', flush=True)
        mode = 'auto'
    if mode == 'auto':
        mode = 'api' if vk.fapi_reachable() else 'vision'
        if mode == 'vision':
            print('· fapi.binance.com unreachable from here (geo-block?) → falling back to the '
                  'data.binance.vision public archive: same bars, no keys, ~10-30h behind live',
                  flush=True)
    return mode


def _vision_df(sym, start, end, interval):
    """Vision-archive klines as the same DataFrame the API wrapper returns: UTC open-time index,
    float64 OHLCV, deduped, sliced to [start, end]."""
    rows = vk.vision_rows(sym, int(start.timestamp() * 1000), int(end.timestamp() * 1000), interval)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=['t', 'open', 'high', 'low', 'close', 'volume',
                                     'T', '-1', '-2', '-3', '-4', '-5'])
    df['datetime'] = pd.to_datetime(df['t'], unit='ms', utc=True)
    df = df.set_index('datetime')[['open', 'high', 'low', 'close', 'volume']].astype('float64')
    df = df[~df.index.duplicated(keep='last')]
    return df[start:end]


def _vision_funding_series(sym, start, end):
    """Funding events from monthly archive files as a Series (same shape as _funding_series).
    The current month is not in the archive yet — those bars stay 0 until the file lands."""
    ev = vk.vision_funding(sym, int(start.timestamp() * 1000), int(end.timestamp() * 1000))
    if not ev:
        return None
    idx = pd.to_datetime([t for t, _ in ev], unit='ms', utc=True)
    ser = pd.Series([r for _, r in ev], index=idx, dtype=float).sort_index()
    return ser[~ser.index.duplicated(keep='last')]


def _gate_universe(top_n, min_years, end, quote='USDT'):
    """Universe listing without fapi: rank Gate.io USDT-perp tickers by 24h turnover (their public
    data API serves US IPs), map BTC_USDT→BTCUSDT, and keep only symbols whose Binance Vision
    archive actually has klines min_years back (HEAD probe of the cutoff-month file) — that one
    check replaces both the exchangeInfo existence test and the onboardDate age filter."""
    try:
        raw = json.load(urllib.request.urlopen(
            'https://api.gateio.ws/api/v4/futures/usdt/tickers', timeout=30))
        if not isinstance(raw, list):
            raise ValueError(f'unexpected Gate response: {type(raw).__name__}')
    except Exception as e:                             # noqa: BLE001 — caller prints the --symbols hint
        print(f'  Gate.io ranking failed: {type(e).__name__} {e}', flush=True)
        return []
    vols = []
    for t in raw:
        c = t.get('contract', '')
        if not c.endswith('_' + quote):
            continue
        v = 0.0
        for k in ('volume_24h_quote', 'volume_24h_settle', 'volume_24h_base'):
            try:
                v = float(t.get(k) or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v:
                break
        vols.append((c.replace('_', ''), v))
    vols.sort(key=lambda x: -x[1])
    cutoff = end - pd.Timedelta(days=min_years * 365.25)
    chosen, probed = [], 0
    for sym, _v in vols:
        if len(chosen) >= top_n:
            break
        probed += 1
        if vk.vision_has_month(sym, '1d', cutoff.year, cutoff.month):
            chosen.append(sym)
    print(f'  Gate ranking: {len(vols)} perps; archive age-check passed {len(chosen)}/{probed} '
          f'probed (cutoff {cutoff.date()})', flush=True)
    return chosen


def _attach_funding(df, fser, freq):
    """`funding` column = sum of the 8h payments that land inside each bar (floor() also absorbs
    the occasional ms jitter in fundingTime). Bars without a payment -> 0.0 (it's a flow)."""
    if fser is None or len(fser) == 0:
        df['funding'] = 0.0
        return df
    per_bar = fser.groupby(fser.index.floor(freq)).sum()
    df['funding'] = per_bar.reindex(df.index).fillna(0.0)
    return df


async def fetch(top_n, start, end, out_path, quote, min_years, concurrency, timeout, interval='1d',
                symbols=None, source='auto'):
    gran, mult = INTERVALS[interval]
    bar_freq = _bar_freq(interval)
    mode = _resolve_source(source)
    bn = _make_binance() if mode == 'api' else None
    onboard = {}

    if mode == 'api':
        print('· pulling instrument list (exchangeInfo)…', flush=True)
        info = await bn.exchange_info()
        perps = []
        for s in info.get('symbols', []):
            if (s.get('status') == 'TRADING' and s.get('contractType') == 'PERPETUAL'
                    and s.get('quoteAsset') == quote):
                perps.append(s['symbol'])
                onboard[s['symbol']] = _onboard_ts(s)

        if symbols:                                           # explicit universe: no ranking, no age filter
            want = [s.strip().upper() for s in symbols if s.strip()]
            missing = [s for s in want if s not in onboard]
            if missing:
                print(f'  not a TRADING {quote} perp on Binance (skipped): {", ".join(missing)}', flush=True)
            chosen = [s for s in want if s in onboard]
            print(f'  explicit universe ({len(chosen)}): {", ".join(chosen)}', flush=True)
            if not chosen:
                print('✗ none of the requested symbols trade on Binance perps', flush=True)
                await _aclose(bn)
                return 1
        else:
            cutoff = end.timestamp() - min_years * YEAR_SECS  # listed no later than min_years before the end
            aged = [sym for sym in perps if onboard.get(sym) is not None and onboard[sym] <= cutoff]
            print(f'  {quote} perps TRADING: {len(perps)}; with history >= {min_years:g} years: {len(aged)} '
                  f'(young filtered out: {len(perps) - len(aged)})', flush=True)

            print('· ranking by 24h turnover (ticker/24hr)…', flush=True)
            tick = await bn.http_client.request(endpoint='/fapi/v1/ticker/24hr', method='GET')
            vol = {t['symbol']: float(t.get('quoteVolume', 0) or 0) for t in tick}
            aged.sort(key=lambda s: vol.get(s, 0.0), reverse=True)
            chosen = aged[:top_n]
            print(f'  taking top-{len(chosen)}: {", ".join(chosen[:12])}{" …" if len(chosen) > 12 else ""}', flush=True)
    else:                                                     # vision: archive zips, no fapi at all
        if symbols:
            chosen = [s.strip().upper() for s in symbols if s.strip()]
            print(f'  explicit universe ({len(chosen)}): {", ".join(chosen)} '
                  f'(existence checked by download — empty pairs are skipped)', flush=True)
        else:
            print('· ranking by Gate.io 24h turnover + Vision archive age probe '
                  '(exchangeInfo is unreachable here)…', flush=True)
            loop = asyncio.get_event_loop()
            chosen = await loop.run_in_executor(None, _gate_universe, top_n, min_years, end, quote)
            if not chosen:
                print('✗ universe listing failed (Gate.io unreachable too?) — pass --symbols explicitly',
                      flush=True)
                return 1
            print(f'  taking top-{len(chosen)}: {", ".join(chosen[:12])}{" …" if len(chosen) > 12 else ""}', flush=True)
        print('  note: funding for the CURRENT month is not in the archive yet (monthly files, '
              '~1-2 day lag after month end) — those newest bars keep funding=0 until the next fetch',
              flush=True)

    print(f'· downloading {interval} bars up to {end.date()} '
          f'(each pair starts from its listing; {concurrency} in parallel)…', flush=True)
    sem = asyncio.Semaphore(concurrency)
    active = {}                                                # sym -> download start (monotonic)

    async def one(sym):
        ob = onboard.get(sym)
        s_start = start
        if ob is not None:
            ob_dt = datetime.fromtimestamp(ob, tz=timezone.utc)
            if ob_dt > start:
                s_start = ob_dt                                # without a day-by-day walk of the pre-listing period
        loop = asyncio.get_event_loop()
        async with sem:
            active[sym] = loop.time()
            try:
                if mode == 'api':
                    df = await asyncio.wait_for(bn.get_trade_bars(sym, s_start, end, gran, mult),
                                                timeout=timeout)
                else:                                          # archive: pre-listing months 404 -> skipped
                    df = await asyncio.wait_for(
                        loop.run_in_executor(None, _vision_df, sym, s_start, end, interval),
                        timeout=timeout)
                if df is not None and not df.empty:            # perp funding history (light pages/files)
                    try:
                        if mode == 'api':
                            fser = await asyncio.wait_for(_funding_series(bn, sym, s_start, end),
                                                          timeout=FUNDING_TIMEOUT)
                        else:
                            fser = await asyncio.wait_for(
                                loop.run_in_executor(None, _vision_funding_series, sym, s_start, end),
                                timeout=FUNDING_TIMEOUT)
                    except Exception:                          # noqa: BLE001 — funding is optional
                        fser = None
                        print(f'  {sym}: funding history unavailable — column zeroed', flush=True)
                    _attach_funding(df, fser, bar_freq)
                return sym, df
            except asyncio.TimeoutError:
                return sym, TimeoutError(f'timeout {timeout}s')
            except Exception as e:                             # noqa: BLE001
                return sym, e
            finally:
                active.pop(sym, None)

    got = {}
    done = 0
    pending = set(chosen)

    async def heartbeat():
        """Intraday pairs take minutes each with no per-request output — show what is ACTUALLY
        downloading right now (only `concurrency` pairs at a time; the rest wait in a queue) so
        the console doesn't look frozen while the majors paginate through years of bars."""
        loop = asyncio.get_event_loop()
        last_done = -1
        stall_since = loop.time()
        while pending:
            await asyncio.sleep(20)
            if not pending:
                break
            if done != last_done:                              # progress since the last beat
                last_done, stall_since = done, loop.time()
            now = loop.time()
            act = ', '.join(f'{s} {now - t:.0f}s' for s, t in sorted(active.items(), key=lambda kv: kv[1]))
            line = (f'  … downloading now: {act or "—"} · queued {max(0, len(pending) - len(active))} '
                    f'· done {done}/{len(chosen)}')
            if now - stall_since > 120:                        # nothing finished for 2+ min
                line += ('  [no completions for a while — likely a Binance rate-limit pause; '
                         f'the client retries, each pair caps at {timeout:.0f}s]')
            print(line, flush=True)

    hb = asyncio.ensure_future(heartbeat())
    for coro in asyncio.as_completed([one(s) for s in chosen]):
        sym, res = await coro
        done += 1
        pending.discard(sym)
        if isinstance(res, Exception):
            hint = (' — re-run, or raise --timeout / lower --top' if isinstance(res, TimeoutError)
                    else '')
            print(f'  [{done}/{len(chosen)}] {sym}: ! {type(res).__name__} {res}{hint}', flush=True)
            continue
        n = 0 if res is None else len(res)
        ok = res is not None and not res.empty and n >= MIN_BARS
        print(f'  [{done}/{len(chosen)}] {sym}: {n} bars{"" if ok else "  (skip)"}', flush=True)
        if ok:
            got[sym] = res
    hb.cancel()

    tickers = [s for s in chosen if s in got]                 # order by volume
    ohlcvs = [got[s] for s in tickers]
    if not tickers:
        print('✗ nothing downloaded — not overwriting data.pickle', flush=True)
        if bn is not None:
            await _aclose(bn)
        return 1

    tmp = out_path + '.tmp'                                    # atomic: the node won't catch a half-written file
    save_pickle(tmp, (tickers, ohlcvs))
    os.replace(tmp, out_path)
    span = f'{min(df.index.min() for df in ohlcvs).date()}..{max(df.index.max() for df in ohlcvs).date()}'
    print(f'✓ wrote {out_path}: {len(tickers)} pairs, range {span} (source: '
          f'{"fapi live API" if mode == "api" else "data.binance.vision archive"})', flush=True)
    if bn is not None:
        await _aclose(bn)
    return 0


async def add_funding(out_path, interval, concurrency=6, source='auto'):
    """Upgrade an EXISTING snapshot in place: download each pair's funding history and add the
    `funding` column, without refetching the klines. Atomic rewrite; klines stay untouched.
    Source-aware like the main fetch: under a fapi geo-block the funding comes from the Vision
    archive instead of silently zeroing the column of every pair."""
    with open(out_path, 'rb') as f:
        tickers, ohlcvs = pickle.load(f)
    mode = _resolve_source(source)
    print(f'· adding funding to {out_path}: {len(tickers)} pairs, klines untouched…', flush=True)
    bn = _make_binance() if mode == 'api' else None
    freq = _bar_freq(interval)
    end = datetime.now(timezone.utc)
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def one(i):
        nonlocal done
        sym, df = tickers[i], ohlcvs[i]
        loop = asyncio.get_event_loop()
        async with sem:
            try:
                if mode == 'api':
                    fser = await asyncio.wait_for(_funding_series(bn, sym, df.index.min(), end),
                                                  timeout=FUNDING_TIMEOUT)
                else:
                    fser = await asyncio.wait_for(
                        loop.run_in_executor(None, _vision_funding_series, sym,
                                             df.index.min(), end),
                        timeout=FUNDING_TIMEOUT)
            except Exception as e:                             # noqa: BLE001
                print(f'  {sym}: ! {type(e).__name__} {e} — funding column zeroed', flush=True)
                fser = None
            _attach_funding(df, fser, freq)
            done += 1
            npay = int((df['funding'] != 0).sum())
            print(f'  [{done}/{len(tickers)}] {sym}: funding on {npay} bars', flush=True)

    await asyncio.gather(*[one(i) for i in range(len(tickers))])
    tmp = out_path + '.tmp'                                    # atomic, like the main fetch
    save_pickle(tmp, (tickers, ohlcvs))
    os.replace(tmp, out_path)
    print(f'✓ upgraded {out_path}: funding column on all {len(tickers)} pairs', flush=True)
    if bn is not None:
        await _aclose(bn)
    return 0


def run(out_path, interval='1d', symbols=None, top=150, min_years=3.0, start=None, end=None,
        source='auto'):
    """Programmatic entry for the node/GUI bootstrap — unlike main(), returns an exit code
    instead of os._exit(), so the caller keeps running after the download."""
    if start is None:
        start = (_resolve_tf(interval).history if _resolve_tf is not None else '2019-09-05')
    start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    end_dt = (datetime.fromisoformat(end) if end else datetime.now(timezone.utc)).replace(tzinfo=timezone.utc)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(lambda _l, _c: None)
    try:
        return loop.run_until_complete(fetch(top, start_dt, end_dt, out_path, 'USDT', min_years,
                                             CONCURRENCY[interval], TIMEOUTS[interval],
                                             interval=interval, symbols=symbols, source=source))
    except KeyboardInterrupt:
        return 130


def main():
    # Progress lines below use '→'/'✓'; on a cp1251/… Windows console or pipe the default stream
    # encoding can't represent them and print() raises UnicodeEncodeError — never crash over
    # cosmetics. (The frozen app and the GUI already force UTF-8; this covers bare CLI runs.)
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors='replace')
        except (AttributeError, OSError, ValueError):
            pass
    ap = argparse.ArgumentParser(description='Download top-N USDT perps (with history >= N years) from Binance')
    ap.add_argument('--top', type=int, default=150, help='how many pairs (top by 24h turnover)')
    ap.add_argument('--min-years', type=float, default=3.0, help='minimum years of history (by listing date)')
    ap.add_argument('--start', default=None,
                    help='history start YYYY-MM-DD (default: the timeframe\'s recommended start — '
                         '2019-09-05 for 1d, later for intraday since its history is denser)')
    ap.add_argument('--end', default=None, help='history end YYYY-MM-DD (default today)')
    ap.add_argument('--out', default=None,
                    help='where to write (default: data.pickle for 1d, data_<interval>.pickle otherwise)')
    ap.add_argument('--quote', default='USDT', help='quote currency (USDT)')
    ap.add_argument('--concurrency', type=int, default=None,
                    help='how many pairs to download in parallel (default scales with --interval: '
                         '6 for 1d, 3 for 1h, 2 for 15m — more trips the Binance rate limit)')
    ap.add_argument('--timeout', type=float, default=None,
                    help='timeout per pair, sec (default scales with --interval: 120 for 1d, '
                         '900 for 1h, … — intraday needs many paginated requests per pair)')
    ap.add_argument('--symbols', default=None,
                    help='comma-separated explicit pair list (e.g. BTCUSDT,ETHUSDT) — skips the '
                         'top-N turnover ranking and the min-years filter; "default" = the built-in '
                         'starter universe of 10 majors')
    ap.add_argument('--interval', default='1d', choices=sorted(INTERVALS),
                    help='bar size (15m|1h|4h|1d); intraday is written to its own data_<tf>.pickle')
    ap.add_argument('--source', default=None, choices=('auto', 'api', 'vision'),
                    help='where to download from: api = live fapi endpoints (as always), '
                         'vision = the data.binance.vision public archive (same bars, works where '
                         'fapi is geo-blocked, ~10-30h behind live), auto = probe fapi and pick '
                         '(default; also via ALPHANODE_DATA_SOURCE)')
    ap.add_argument('--add-funding', action='store_true',
                    help='do NOT refetch klines: add the funding column to the existing snapshot '
                         'of --interval (or --out) and exit')
    args = ap.parse_args()
    if args.timeout is None:
        args.timeout = TIMEOUTS[args.interval]
    if args.concurrency is None:
        args.concurrency = CONCURRENCY[args.interval]
    if args.out is None:
        suffix = '' if args.interval == '1d' else f'_{args.interval}'
        args.out = os.path.join(HERE, f'data{suffix}.pickle')
    if args.add_funding:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.set_exception_handler(lambda _l, _c: None)
        try:
            rc = loop.run_until_complete(add_funding(args.out, args.interval, source=args.source))
        except KeyboardInterrupt:
            rc = 130
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(rc)
    if args.start is None:                                # per-timeframe recommended history start
        args.start = (_resolve_tf(args.interval).history if _resolve_tf is not None
                      else '2019-09-05')
        print(f'· --start not given → {args.start} (recommended for {args.interval}); '
              'override with --start', flush=True)
    symbols = None
    if args.symbols:
        symbols = (list(DEFAULT_SYMBOLS) if args.symbols.strip().lower() == 'default'
                   else [x.strip().upper() for x in args.symbols.replace(',', ' ').split() if x.strip()])
    if args.interval == '15m' and symbols is None and args.top > 60:
        print('note: 15m bars are heavy (~96/day/pair) — consider --top <= 60', flush=True)

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = (datetime.fromisoformat(args.end) if args.end else datetime.now(timezone.utc)).replace(tzinfo=timezone.utc)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(lambda _l, _c: None)      # silence the cosmetic aiosonic teardown
    try:
        rc = loop.run_until_complete(fetch(args.top, start, end, args.out, args.quote,
                                           args.min_years, args.concurrency, args.timeout,
                                           interval=args.interval, symbols=symbols,
                                           source=args.source))
    except KeyboardInterrupt:
        rc = 130
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)                                         # instant: without GC junk from SSL transports


if __name__ == '__main__':
    main()

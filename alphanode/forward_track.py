"""Forward track — append-only paper stepping of enrolled strategies INSIDE the node.

Enrolling FREEZES a strategy (formulas + universe + vol/fee) as of that day; from then on the
node steps it once per closed daily bar on LIVE Binance data with the same semantics as a
live signal service: run the real engine up to the last closed bar -> target weights ->
mark-to-market -> rebalance -> fees -> log. The account lives in the entry, one JSON per node
(state/forward.json). History is APPEND-ONLY: forward numbers are written by live steps and
never recomputed backwards — a "recomputed" track would just be another backtest.

CLI (the GUI spawns this as a worker; cron users can call it directly):
    python alphanode/forward_track.py step [--force]   # step every due entry
    python alphanode/forward_track.py list             # one line per entry

Daily timeframe only — the stepping engine is the real quantpylib Portfolio (same as paper).
"""
import os
import sys
import json
import hashlib
import argparse
import warnings
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
for _p in (os.path.join(PROJ, 'evolution'), PROJ, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np                                        # noqa: E402
import pandas as pd                                       # noqa: E402
import vision_klines as vk                                # noqa: E402  fapi→archive fallback

warnings.filterwarnings('ignore')
np.seterr(all='ignore')

DUST = 1.0                                                # ignore rebalances under $1 notional
START_CAPITAL = 10000.0
BAR_SECS = {'15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}
BAR_FMT = {'1d': '%Y-%m-%d'}                              # intraday bars carry the time too


def _fmt_bar(ts, tf):
    return ts.strftime(BAR_FMT.get(tf, '%Y-%m-%d %H:%M'))


def _state_dir():
    d = os.environ.get('ALPHANODE_STATE_DIR')
    if not d:
        import apppaths                            # NOT "state next to the module": frozen, HERE is
        d = apppaths.state_dir()                   # the read-only bundle (AppImage squashfs / deb's
    os.makedirs(d, exist_ok=True)                  # /opt) — and the GUI imports us IN-PROCESS, where
    return d                                       # ALPHANODE_STATE_DIR is not set (only children get it)


def track_file():
    return os.path.join(_state_dir(), 'forward.json')


def load_track():
    try:
        with open(track_file(), encoding='utf-8') as f:
            t = json.load(f)
        t.setdefault('entries', [])
        return t
    except (OSError, json.JSONDecodeError):
        return {'entries': []}


def save_track(track):
    path = track_file()
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(track, f, indent=1)
    os.replace(tmp, path)


def _current_session():
    """The current session id, '' when unavailable (see the stamp's comment in new_entry)."""
    try:
        from sessions import current_session_id
        return current_session_id(_state_dir())
    except Exception:                                    # noqa: BLE001
        return ''


def new_entry(name, kind, formulas, tickers, vol, exec_rate, engine_start,
              capital=START_CAPITAL, tf='1d', entry_id=None, session=None):
    """A frozen strategy: everything a step needs, snapshotted at enrollment.
    entry_id: explicit id (the GUI passes the leaderboard's md5(formula)[:6], so the
    forward track and the leaderboard read as ONE list); legacy callers get name_sig."""
    sig = hashlib.md5(('|'.join(formulas) + '#' + ','.join(sorted(tickers)) + '#' + tf)
                      .encode()).hexdigest()[:6]
    return {
        'id': entry_id or f'{name}_{sig}',
        'name': name, 'kind': kind,                      # 'alpha' | 'portfolio'
        'tf': tf,                                        # bar size FROZEN with the strategy
        'formulas': list(formulas), 'tickers': list(tickers),
        'vol': float(vol), 'exec': float(exec_rate),
        'engine_start': str(engine_start)[:10],          # warm-up start for the simulation
        'start_capital': float(capital),
        'enrolled': datetime.now(timezone.utc).date().isoformat(),
        # which working session enrolled it. The track OUTLIVES sessions — Clear all history
        # spares it and a session load merges rather than replaces it — so entries from many
        # sessions pile up in one list, and this stamp is what still says where each came
        # from. Guarded: a missing/unimportable sessions module must never block an enroll.
        'session': _current_session() if session is None else str(session),
        'archived': False,
        'state': {'equity': float(capital), 'positions': {}, 'prices': {}, 'last_run': None},
        'history': [],
    }


def migrate_ids(track):
    """One-time rename of legacy 'alpha_<md5>_<sig>' entry ids to the bare leaderboard
    id '<md5>' — the two panels must read as one list. Returns the number renamed.
    Safe mid-flight: a step child syncing under the OLD id simply finds no match and
    drops that one step (append-only semantics); the next step lands normally."""
    import re
    n = 0
    ids = {e.get('id') for e in track.get('entries', [])}
    for e in track.get('entries', []):
        m = re.fullmatch(r'alpha_([0-9a-f]{6})_[0-9a-f]{6}', str(e.get('id', '')))
        if m and m.group(1) not in ids:
            e['id'] = m.group(1)
            e['name'] = m.group(1)
            ids.add(m.group(1))
            n += 1
    return n


def find_entry(track, entry_id):
    """The entry carrying `entry_id` — the ACTIVE copy when the id is doubled up. A track can
    hold an archived copy AND a live re-enrollment under one id (portfolio ids are the
    deterministic name_sig; the old Archive button left the ghost behind), and 'first match
    wins' pinned the stepper to the ghost: every tick computed the step, sync dropped it as
    'archived mid-step', the live entry never advanced — a 2-minute loop every 5 minutes."""
    hit = None
    for e in track.get('entries', []):
        if e.get('id') == entry_id:
            if not e.get('archived'):
                return e
            hit = hit or e
    return hit


def unique_id(track, base):
    """`base` if no entry — archived or not — carries it, else base-2, base-3, …  An id
    must be unique across the WHOLE file: the stepper syncs its results by id."""
    ids = {e.get('id') for e in track.get('entries', [])}
    if base not in ids:
        return base
    n = 2
    while f'{base}-{n}' in ids:
        n += 1
    return f'{base}-{n}'


def drop_ghosts(track):
    """One-time cure for a file already poisoned by the doubled-id bug (see find_entry):
    an ARCHIVED copy whose id is also carried by an active entry is a ghost — it holds a
    stale prefix of the same stream and nothing can ever address it. Returns how many
    were dropped; the caller saves."""
    live = {e.get('id') for e in track.get('entries', []) if not e.get('archived')}
    keep = [e for e in track.get('entries', [])
            if not (e.get('archived') and e.get('id') in live)]
    n = len(track.get('entries', [])) - len(keep)
    if n:
        track['entries'] = keep
    return n


def find_duplicate(track, formulas, tickers, tf='1d'):
    """An ACTIVE entry with the same frozen strategy (formulas + universe + bar size)."""
    key = ('|'.join(formulas), ','.join(sorted(tickers)), tf)
    for e in track['entries']:
        if not e.get('archived') and (('|'.join(e['formulas']), ','.join(sorted(e['tickers'])),
                                       e.get('tf', '1d')) == key):
            return e
    return None


def is_due(entry, now=None):
    """Has a NEW bar of this entry's timeframe closed since the last processed one?"""
    now = now or datetime.now(timezone.utc)
    lr = entry['state'].get('last_run')
    if not lr:
        return True
    tf = entry.get('tf', '1d')
    if tf == '1d':
        return lr < (now.date() - timedelta(days=1)).isoformat()
    last_open = datetime.fromisoformat(lr).replace(tzinfo=timezone.utc)
    # last_run stores the OPEN time of the last processed bar; the NEXT bar has closed
    # once two full bar lengths have elapsed since then
    return (now - last_open).total_seconds() >= 2 * BAR_SECS[tf]


def metrics(entry):
    """days / total return / annualized Sharpe / max drawdown from the append-only history."""
    hist = entry.get('history') or []
    eq = [entry['start_capital']] + [h['equity'] for h in hist]
    out = {'days': len(hist), 'equity': eq[-1], 'ret': eq[-1] / eq[0] - 1.0,
           'sharpe': None, 'dd': None, 'last': (hist[-1]['date'] if hist else None)}
    if len(eq) >= 3:
        e = np.asarray(eq, dtype=float)
        r = e[1:] / e[:-1] - 1.0
        peak = np.maximum.accumulate(e)
        out['dd'] = float((e / peak - 1.0).min())
        if len(r) >= 10 and r.std() > 0:
            ann = 365.0 * 86400.0 / BAR_SECS.get(entry.get('tf', '1d'), 86400)
            out['sharpe'] = float(r.mean() / r.std() * np.sqrt(ann))
    return out


# ---- live data (public USD-M klines; no keys) ----
def _now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def fetch_klines(symbol, start_ms, end_ms, interval='1d'):
    # fapi when reachable, the data.binance.vision archive when geo-blocked (same Binance
    # bars, just ~10-30h behind live — a blocked region steps later, never differently)
    out = vk.fetch_rows(symbol, start_ms, end_ms, interval)
    if not out:
        return pd.DataFrame()
    df = pd.DataFrame(out, columns=['openTime', 'open', 'high', 'low', 'close', 'volume',
                                    'closeTime', 'qav', 'trades', 'tbb', 'tbq', 'ig'])
    df = df[df['closeTime'] <= _now_ms()]                 # CLOSED candles only — no live bar
    for c in ('open', 'high', 'low', 'close', 'volume'):
        df[c] = df[c].astype(float)
    df['datetime'] = pd.to_datetime(df['openTime'], unit='ms', utc=True)
    df = df.set_index('datetime')[['open', 'high', 'low', 'close', 'volume']]
    return df[~df.index.duplicated()]


def _compute_targets(entry, tickers, dfs, end):
    """Same maths as the signal service: each formula via the real engine, combined by Portfolio;
    the last row gives target weights + leverage (one alpha = a one-strategy portfolio)."""
    from evolved_strategy import make_evolved
    from quantpylib.simulator.alpha import Portfolio
    start = datetime.fromisoformat(entry['engine_start'])
    stratdfs = []
    for i, formula in enumerate(entry['formulas']):
        Alpha = make_evolved(formula, f'F{i}')
        a = Alpha(insts=tickers, dfs={t: dfs[t].copy() for t in tickers}, start=start, end=end,
                  portfolio_vol=entry['vol'], execrates=entry['exec'])
        stratdfs.append(a.run_simulation())
    pf = Portfolio(insts=tickers, dfs={t: dfs[t].copy() for t in tickers}, start=start, end=end,
                   stratdfs=stratdfs, portfolio_vol=entry['vol'], execrates=entry['exec'])
    last = pf.run_simulation().iloc[-1]
    lev = float(last.get('leverage', 0.0))
    weights = {t: float(last.get(f'{t} w', 0.0)) for t in tickers}
    return weights, lev


def _compute_targets_fast(entry, tickers, dfs, tf):
    """Intraday targets via fastsim — the SEARCH engine, parameterized by the bar size (the real
    quantpylib engine is daily-only). Same combination trick as the intraday portfolio builder:
    Σ(weight×leverage) paths fed back through the kernel reproduces Portfolio's semantics. The
    last row of the combined path is already weight×leverage — so the returned lev is 1.0."""
    from genome import parse
    from evaluator import panel_from_raw, make_market, eval_alpha_panel
    from fastsim import fast_sim_paths
    from timeframe import resolve
    t = resolve(tf)
    start = pd.Timestamp(entry['engine_start'], tz='UTC')
    end = max(df.index[-1] for df in dfs.values())
    panel = panel_from_raw(tickers, dfs, start, end, t.pandas_freq)
    market = make_market(panel, tickers, dfs, vol_window=t.vol_window)
    paths = []
    for f in entry['formulas']:
        ap = eval_alpha_panel(parse(f), panel)
        _r, wl = fast_sim_paths(ap[tickers].to_numpy(dtype=np.float64), market,
                                entry['vol'], entry['exec'],
                                ann=t.periods_per_year, ewma_lambda=t.ewma_lambda)
        paths.append(wl)
    comb = np.sum(paths, axis=0)
    _r, comb_wl = fast_sim_paths(comb, market, entry['vol'], entry['exec'],
                                 ann=t.periods_per_year, ewma_lambda=t.ewma_lambda)
    last = np.nan_to_num(comb_wl[-1])
    return {tk: float(last[i]) for i, tk in enumerate(tickers)}, 1.0


def _funding_events(t, from_ms, to_ms, cache):
    """Funding events of `t` in [from_ms, to_ms), via the shared per-run cache (one fetch per
    ticker per run; re-fetched when an entry needs an earlier start OR a later end — entries
    on different timeframes share the ticker but not the window). None = rates unavailable
    right now (e.g. current month under a geo-block) — the caller must treat that as
    "unknown", never as zero. Half-open [from, to): an event stamped exactly on a bar close
    belongs to the NEXT step and is charged on the post-rebalance book, matching how fastsim
    floors events into the bar that OPENS at that instant — so which book pays no longer
    depends on millisecond jitter in fundingTime."""
    key = (t, '_funding')
    got = cache.get(key)
    if got is None or got[0] > from_ms or got[1] < to_ms:
        fetched_to = _now_ms()
        try:
            cache[key] = (from_ms, fetched_to, vk.fetch_funding(t, from_ms, fetched_to))
        except Exception:                                 # noqa: BLE001 — no rates this cycle
            cache[key] = (from_ms, fetched_to, None)
    _f, _t, ev = cache[key]
    if ev is None:
        return None
    return [(ts, r) for ts, r in ev if from_ms <= ts < to_ms]


def step_entry(entry, kline_cache, force=False, log=print):
    """One trading step for one entry. Returns True if the entry advanced (needs saving)."""
    tf = entry.get('tf', '1d')
    ok, dfs = [], {}
    start_ms = int(pd.Timestamp(entry['engine_start'], tz='UTC').timestamp() * 1000)
    for t in entry['tickers']:
        key = (t, tf)
        if key not in kline_cache:
            try:
                kline_cache[key] = fetch_klines(t, start_ms, _now_ms(), interval=tf)
            except Exception as e:                        # noqa: BLE001
                kline_cache[key] = pd.DataFrame()
                log(f'  {t}: download failed ({type(e).__name__})')
        df = kline_cache[key]
        if len(df) > 60:
            dfs[t] = df
            ok.append(t)
    if not ok:
        log(f'[{entry["id"]}] no data for any ticker — step skipped')
        return False
    tickers = ok
    last_bar = max(df.index[-1] for df in dfs.values())
    # Complete-universe guard: a ticker whose feed is missing or ends BEFORE the common bar
    # would silently leave the book (its position teleports to zero with no trade booked) and
    # get re-bought next bar — phantom churn and double fees. A transient gap just delays the
    # step; the loop retries in minutes. (A permanently dead symbol freezes the entry — that
    # is the honest outcome; archive the entry if the perp was delisted.)
    stale = [t for t in entry['tickers'] if t not in dfs or dfs[t].index[-1] < last_bar]
    if stale:
        log(f'[{entry["id"]}] incomplete universe at {_fmt_bar(last_bar, tf)} — '
            f'{len(stale)}/{len(entry["tickers"])} without fresh data ({", ".join(stale[:4])}'
            f'{"…" if len(stale) > 4 else ""}) — waiting')
        return False
    end_str = _fmt_bar(last_bar, tf)
    st = entry['state']
    hist = entry['history']
    # The data source may sit BEHIND the recorded track: a node that stepped on live fapi and
    # then lost it (US geo-block → Vision archive, ~10-30h lag) would otherwise append an older
    # bar after a newer one and re-count its P&L backwards. Append-only means forward-only —
    # wait until the source catches up. (Same-format strings compare chronologically.)
    prev = hist[-1]['date'] if hist else None
    if prev is not None and end_str < prev:
        log(f'[{entry["id"]}] data source is behind the track (last step {prev}, source has '
            f'{end_str}) — waiting for fresher bars')
        return False
    if not force and st.get('last_run') == end_str:
        log(f'[{entry["id"]}] up to date ({end_str})')
        return False

    log(f'[{entry["id"]}] stepping to {end_str} ({len(tickers)} assets, {tf})…')
    if tf == '1d':
        end = datetime(last_bar.year, last_bar.month, last_bar.day)
        weights, lev = _compute_targets(entry, tickers, dfs, end)
    else:
        weights, lev = _compute_targets_fast(entry, tickers, dfs, tf)
    prices = {t: float(dfs[t]['close'].iloc[-1]) for t in tickers}

    equity = float(st['equity'])
    positions = {t: float(v) for t, v in (st.get('positions') or {}).items()}
    prev_prices = st.get('prices') or {}
    pnl = sum(positions.get(t, 0.0) * (prices[t] - float(prev_prices.get(t, prices[t])))
              for t in tickers)
    equity += pnl
    # Perp funding accrual — the same economics fastsim charges during mining: every funding
    # event inside the gap costs units × price × rate (longs pay a positive rate, shorts
    # receive it), priced at the last close BEFORE the event (fastsim's units×C[i-1]×F[i]).
    # ALL-OR-NOTHING: if any held ticker's rates are unavailable (archive lag under a
    # geo-block, a transient fetch error), the step accrues NOTHING and records funding as
    # null — unknown is not zero, a partial sum posing as a total is worse, and append-only
    # means a wrong number could never be corrected later.
    funding = 0.0                                         # known-zero: flat book / no gap
    fund_note = ''
    prev_str = hist[-1]['date'] if hist else None
    if prev_str and positions:
        bar_ms = BAR_SECS[tf] * 1000
        w_from = int(pd.Timestamp(prev_str, tz='UTC').timestamp() * 1000) + bar_ms
        w_to = int(last_bar.timestamp() * 1000) + bar_ms
        if w_to > w_from:
            acc, complete = 0.0, True
            for t, units in positions.items():
                if not units:
                    continue
                ev = _funding_events(t, w_from, w_to, kline_cache) if t in dfs else None
                if ev is None:
                    complete = False
                    break
                closes = dfs[t]['close']
                for ts, rate in ev:
                    px = float(closes.asof(pd.Timestamp(ts - 1, unit='ms', tz='UTC')))
                    if px == px:                          # NaN-safe: skip pre-history events
                        acc -= units * px * rate
            if complete:
                funding = acc
            else:
                funding = None                            # recorded as unknown, shown as —
                fund_note = (' · funding rates unavailable — NOT accrued, recorded as '
                             'unknown (never as zero)')
    equity += funding or 0.0
    target = {t: (weights.get(t, 0.0) * lev * equity / prices[t]) if prices[t] > 0 else 0.0
              for t in tickers}
    trades = {}
    for t in tickers:
        d = target[t] - positions.get(t, 0.0)
        if abs(d * prices[t]) > DUST:
            trades[t] = d
    turnover = sum(abs(d * prices[t]) for t, d in trades.items())
    fees = turnover * entry['exec']
    equity -= fees

    st['positions'] = {t: target[t] for t in tickers if abs(target[t] * prices[t]) > DUST}
    st['prices'] = prices
    st['equity'] = equity
    st['last_run'] = end_str
    # intraday weights are already weight×leverage (lev returned as 1.0) — log the real gross
    lev_disp = lev if tf == '1d' else sum(abs(w) for w in weights.values())
    row = {'date': end_str, 'equity': round(equity, 2), 'pnl': round(pnl, 2),
           'funding': (None if funding is None else round(funding, 2)),
           'fees': round(fees, 2), 'turnover': round(turnover, 2), 'leverage': round(lev_disp, 3),
           # the SIGNALS of this step: the held book (signed fraction of equity per asset)
           # and the executed rebalance (signed $ notional per asset)
           'pos': {t: round(target[t] * prices[t] / equity, 4) for t in tickers
                   if equity > 0 and abs(target[t] * prices[t]) > DUST},
           'trades': {t: round(d * prices[t], 2) for t, d in trades.items()}}
    if hist and hist[-1]['date'] == end_str:              # a force re-step overwrites the same bar
        # the bar's funding was accrued by the ORIGINAL step (this re-step's window is empty
        # and would show 0.00 while st.equity correctly keeps the amount) — carry it over
        row['funding'] = hist[-1].get('funding')
        hist[-1] = row
    else:
        hist.append(row)
    fund_disp = 'n/a' if funding is None else f'${funding:+,.2f}'
    log(f'  equity ${equity:,.2f} · P&L ${pnl:+,.2f} · funding {fund_disp} · '
        f'fees ${fees:,.2f} · lev {lev_disp:.2f}{fund_note}')
    return True


def sync_entry_to_disk(entry):
    """Persist ONE stepped entry into the CURRENT on-disk track (read-merge-write).

    step_all() used to hold its whole-file snapshot for minutes (kline fetches) and then
    save_track(snapshot) after every entry — overwriting anything the user did meanwhile.
    The shipped symptom: press Archive during a stepping pass and the entry RESURRECTS on
    the stepper's next save (a fresh enrollment could vanish the same way). Merge rules:
    the stepper owns only its own outputs (state, history); every structural fact — the
    one-way `archived` flag above all, or the entry's very existence — belongs to the
    freshest disk copy. Returns True if the step result was persisted."""
    disk = load_track()
    e = find_entry(disk, entry.get('id'))                # the ACTIVE copy if the id is doubled
    if e is None:
        return False                                     # removed on disk mid-step: stays gone
    if e.get('archived'):
        return False                                     # archived mid-step: the click wins
    merged = dict(e)                                     # disk copy keeps structural edits
    merged['state'] = entry['state']
    merged['history'] = entry['history']
    i = next(i for i, x in enumerate(disk['entries']) if x is e)
    disk['entries'][i] = merged
    save_track(disk)
    return True


def step_all(force=False, log=print):
    track = load_track()
    active = [e for e in track['entries'] if not e.get('archived')]
    if not active:
        log('forward track is empty — enroll a champion or a portfolio in the GUI')
        return 0
    cache = {}
    stepped = 0
    for e in active:
        try:
            # an Archive click may land while EARLIER entries were stepping — re-check the
            # disk before spending minutes fetching klines for an entry nobody wants stepped
            fresh = find_entry(load_track(), e['id'])
            if fresh is None or fresh.get('archived'):
                log(f'[{e["id"]}] archived/removed while the pass ran — skipped')
                continue
            if step_entry(e, cache, force=force, log=log):
                if sync_entry_to_disk(e):                # crash-safe AND edit-safe: only this
                    stepped += 1                         # entry's step lands, nothing else
                else:
                    log(f'[{e["id"]}] archived/removed mid-step — step result dropped')
        except Exception as ex:                          # noqa: BLE001
            log(f'[{e["id"]}] step failed: {type(ex).__name__}: {ex}')
    log(f'done: {stepped}/{len(active)} entries advanced')
    return 0


def main():
    ap = argparse.ArgumentParser(description='Forward track: append-only paper steps of enrolled strategies')
    ap.add_argument('cmd', choices=('step', 'list'))
    ap.add_argument('--force', action='store_true', help='re-step even if the bar was processed')
    args = ap.parse_args()
    if args.cmd == 'list':
        track = load_track()
        for e in track['entries']:
            m = metrics(e)
            sh = f'{m["sharpe"]:+.2f}' if m['sharpe'] is not None else '—'
            print(f'{"[arch] " if e.get("archived") else ""}{e["id"]}: {m["days"]} steps · '
                  f'${m["equity"]:,.0f} ({m["ret"]*100:+.1f}%) · Sharpe {sh} · since {e["enrolled"]}')
        return 0
    return step_all(force=args.force)


if __name__ == '__main__':
    sys.exit(main())

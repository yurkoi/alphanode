"""Leaderboard trade stats (long/short/win/activity on TEST), computed OUT OF PROCESS.

The GUI used to run this on a background thread, but the work is numpy/pandas-heavy PYTHON —
parsing a genome, evaluating it over the panel, running fast_sim — and it holds the GIL far more
than it releases it. The Tk main loop starves behind it: a status poll that costs 14ms turns into
~800ms and the window visibly stalls while the table fills in. A subprocess has its own GIL, so the
GUI only ever waits on a pipe (which releases it).

    python alphanode/metrics_worker.py      # config from ALPHANODE_* env + config.ini
    <exe> --role metrics                    # frozen build

stdin  -> {"formulas": [...], "instruments": [...]|null, "vol": .., "exec": ..,
           "train_start": "YYYY-MM-DD", "test_start": ..., "test_end": ...}
stdout -> {"ok": true, "trend_bars": {"up":n,"down":n,"flat":n},
           "metrics": {formula: {"long":n,"short":n,"win":f,"act":f,
           "dd":f,"cagr":f|null,"sortino":f|null,
           "tup":f|null,"tdown":f|null,"tflat":f|null} | "err"}}
           tup/tdown/tflat = the alpha's TEST Sharpe on trending-up / trending-down /
           flat market bars (drift t-stat of the EW basket over ~30 calendar days of
           bars, labels lagged one bar; see evaluator.trend_regime and trend_split
           below) — under 30 bars in a bucket comes back null
           {"ok": false, "error": "..."}   on a failure that kills the whole batch

A formula that cannot be parsed or never trades comes back as "err" — same contract the GUI's
cache already speaks, so callers need no new branches.
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
for _p in (os.path.join(PROJ, 'evolution'), PROJ, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import warnings                                          # noqa: E402
import numpy as np                                       # noqa: E402
import pandas as pd                                      # noqa: E402
warnings.filterwarnings('ignore'); np.seterr(all='ignore')


def build_ctx(opt):
    """Panel/market + the TEST mask, exactly as the GUI's _metrics_ctx built them.
    Timeframe-aware: the grid freq / vol window / bars-per-year come from load_config
    (which honors ALPHANODE_TF), so intraday libraries get intraday-correct stats."""
    from config import load_config
    from evaluator import build_panel, make_market, trend_regime
    cfg = load_config()
    if opt.get('instruments'):
        cfg['instruments'] = list(opt['instruments'])
    vol = float(opt.get('vol', cfg['vol']))
    ex = float(opt.get('exec', cfg['exec']))
    ann = float(cfg.get('ann', 365.0))
    tr = pd.Timestamp(opt['train_start'], tz='UTC')
    te = pd.Timestamp(opt['test_start'], tz='UTC')
    en = pd.Timestamp(opt['test_end'], tz='UTC')
    tk, raw, panel = build_panel(cfg['data'], tr.tz_localize(None).to_pydatetime(),
                                 en.tz_localize(None).to_pydatetime(), cfg.get('instruments'),
                                 freq=cfg.get('freq', 'D'))
    market = make_market(panel, tk, raw, vol_window=cfg.get('vol_window', 30))
    tmask = (market['index'] >= te) & (market['index'] < en)
    elig = market['base_elig']
    n_assets = int(elig[tmask].any(axis=0).sum()) or int(elig.shape[1])   # assets live on TEST
    years = max(float(np.count_nonzero(tmask)) / ann, 1e-9)
    # market DIRECTION regime: +1 up / -1 down / 0 flat / NaN warmup — same causal contract.
    # The window is 30 CALENDAR days of bars whatever the grid (30 on 1d, 180 on 4h, 720 on
    # 1h) — 'ann<=400 ? 30 : 120' would have handed 15m a 1.25-day "trend". The labels are
    # then LAGGED one bar: bar t carries the regime known at t-1's close, so a label's window
    # never contains the very return that gets sliced by it (see trend_split).
    trd = trend_regime(panel, window=max(30, int(round(30 * ann / 365.0))))
    trd = trd.reindex(pd.DatetimeIndex(market['index'])).to_numpy()
    trd = np.concatenate([[np.nan], trd[:-1]])
    return {'panel': panel, 'market': market, 'V': market['V'], 'elig': elig, 'tmask': tmask,
            'n_assets': max(1, n_assets), 'years': years, 'vol': vol, 'exec': ex,
            'ann': ann, 'ewma': float(cfg.get('ewma_lambda', 0.06)), 'trend': trd}


def regime_sharpe(x, ann):
    """Sharpe of one regime slice — under 30 bars (or zero variance) an honest None,
    never a number invented from a handful of bars."""
    if x.size < 30 or float(x.std()) < 1e-12:
        return None
    return float(x.mean()) * ann / (float(x.std()) * np.sqrt(ann))


def trend_split(rt, trd, ann):
    """{'tup','tdown','tflat'}: TEST Sharpe by direction bucket. `trd` must already be
    LAGGED one bar (build_ctx does it): slicing on the unlagged label conditions each
    return on its own bar's market move — a beta-1 clone of the market on a DRIFTLESS
    walk showed ~+1.3 of pure T↑−T↓ Sharpe mirage before the lag."""
    return {'tup': regime_sharpe(rt[trd == 1.0], ann),
            'tdown': regime_sharpe(rt[trd == -1.0], ann),
            'tflat': regime_sharpe(rt[trd == 0.0], ann)}


def trend_bar_counts(ctx):
    """How many TEST bars each direction bucket holds — one number per bucket for the
    whole batch: the split is the MARKET's calendar, identical for every formula."""
    lab = ctx['trend'][ctx['tmask']]
    return {'up': int((lab == 1.0).sum()), 'down': int((lab == -1.0).sum()),
            'flat': int((lab == 0.0).sum())}


def call_accuracy(real, up):
    """Accuracy of one side's calls. `real` holds the next-bar asset returns of every
    (bar, asset) cell where the formula called that side — long cells for up=True,
    short cells for up=False. A call is right when the price MOVED the called way;
    a cell where it didn't move (|r| <= 1e-9) judges nothing. None under 30 calls
    (the trend columns' evidence floor) or under 5 that moved."""
    if real.size < 30:
        return None
    a = np.abs(real) > 1e-9
    if int(a.sum()) < 5:
        return None
    r = real[a]
    return float((r > 0).mean()) if up else float((r < 0).mean())


def trade_stats(formula, ctx):
    """{long, short, long_yr, short_yr, win, wup, wdown, act, dd, cagr, sortino, tup,
    tdown, tflat} for one formula on TEST — act = trades per asset per year (relative
    activity, universe/period independent), long_yr/short_yr the same rate split by side;
    dd/cagr/sortino from the same simulated TEST equity; tup/tdown/tflat = Sharpe by
    market DIRECTION regime (trend_split); wup/wdown = accuracy of the formula's own
    long / short calls (call_accuracy); net = the book's directional tilt on TEST, the
    time-average of net/gross in [-1, +1]. 'err' if it doesn't parse or never trades."""
    from genome import parse
    from evaluator import eval_alpha_panel
    from fastsim import fast_sim
    market, V, elig, tmask = ctx['market'], ctx['V'], ctx['elig'], ctx['tmask']
    try:
        raw = eval_alpha_panel(parse(formula), ctx['panel'])[market['tk']].to_numpy(dtype=np.float64)
        A = pd.DataFrame(raw).ffill().to_numpy()
        E = elig & np.isfinite(A)
        fc = np.where(E, np.where(E, A, 0.0) / V, 0.0)
        chips = np.nansum(np.abs(fc), axis=1, keepdims=True)
        W = fc / np.where(chips == 0.0, 1.0, chips)                      # + long / − short
        side = np.where(W > 0.0005, 1, np.where(W < -0.0005, -1, 0))     # daily side [T,N]
        # directional tilt on TEST: time-average of net/gross, -1 (all short) .. +1 (all long).
        # W already carries the inverse-vol weighting and sums to 1 in absolute value per bar,
        # so the per-bar net IS the row sum; bars where nothing is held are skipped.
        _g = np.abs(W[tmask]).sum(axis=1)
        _live = _g > 0
        net = float((W[tmask][_live].sum(axis=1) / _g[_live]).mean()) if _live.any() else 0.0
        if not np.abs(side[tmask]).any():                               # no positions on TEST — invalid
            return 'err'
        # a "trade" = opening a position: cross into long/short from flat/opposite
        prev = np.vstack([np.zeros((1, side.shape[1])), side[:-1]])      # previous calendar day
        long_tr = int(((side == 1) & (prev != 1))[tmask].sum())         # long entries in TEST
        short_tr = int(((side == -1) & (prev != -1))[tmask].sum())      # short entries in TEST
        rt = fast_sim(raw, market, ctx['vol'], ctx['exec'],
                      ann=ctx['ann'], ewma_lambda=ctx['ewma']).to_numpy()[tmask]
        active = np.abs(rt) > 1e-9                                       # days when something happened
        win = float((rt[active] > 0).mean()) if active.any() else 0.0
        act = (long_tr + short_tr) / ctx['n_assets'] / ctx['years']     # trades / asset / year
        eq = np.cumprod(1.0 + rt)
        dd = float((eq / np.maximum.accumulate(eq) - 1.0).min())
        last = float(eq[-1])
        cagr = (last ** (1.0 / ctx['years']) - 1.0) if last > 0 else None   # wiped out -> null
        dstd = float(np.sqrt(np.mean(np.minimum(rt, 0.0) ** 2)))            # downside deviation
        sortino = (float(rt.mean()) * ctx['ann'] / (dstd * np.sqrt(ctx['ann']))
                   if dstd > 1e-12 else None)                               # no losing bars -> null

        lab = ctx['trend'][tmask]                                           # labels pre-lagged
        ts = trend_split(rt, lab, ctx['ann'])
        # WIN ↑ / WIN ↓ — accuracy of the formula's OWN calls, not the market's regime.
        # The side held at the prior close is a per-asset call on THIS bar's move (the
        # simulator books PnL the same way: units_prev × today's price change), so a
        # long cell is right when the asset rose and a short cell when it fell.
        pred = prev[tmask]
        real = market['R'][tmask]
        wup = call_accuracy(real[pred == 1], up=True)
        wdown = call_accuracy(real[pred == -1], up=False)

        def _fin(v):                                                        # JSON-safe: NaN/inf -> null
            return float(v) if (v is not None and np.isfinite(v)) else None
        return {'long': long_tr, 'short': short_tr, 'win': win, 'act': act, 'net': _fin(net),
                'long_yr': _fin(long_tr / ctx['n_assets'] / ctx['years']),  # entries / asset / year,
                'short_yr': _fin(short_tr / ctx['n_assets'] / ctx['years']),  # split by side
                'wup': _fin(wup), 'wdown': _fin(wdown),
                'dd': _fin(dd), 'cagr': _fin(cagr), 'sortino': _fin(sortino),
                'tup': _fin(ts['tup']), 'tdown': _fin(ts['tdown']), 'tflat': _fin(ts['tflat'])}
    except Exception:                                                   # noqa: BLE001
        return 'err'


def main():
    try:
        opt = json.load(sys.stdin)
    except Exception as e:                               # noqa: BLE001
        print(json.dumps({'ok': False, 'error': f'bad input: {e}'}))
        return
    formulas = [f for f in (opt.get('formulas') or []) if f]
    if not formulas:
        print(json.dumps({'ok': True, 'metrics': {}}))
        return
    try:
        ctx = build_ctx(opt)
    except Exception as e:                               # noqa: BLE001 — no data/config: the GUI
        print(json.dumps({'ok': False,                   # marks the whole batch 'err' and moves on
                          'error': f'{type(e).__name__}: {e}'}))
        return
    out = {f: trade_stats(f, ctx) for f in formulas}
    print(json.dumps({'ok': True, 'metrics': out, 'trend_bars': trend_bar_counts(ctx)}))


if __name__ == '__main__':
    main()

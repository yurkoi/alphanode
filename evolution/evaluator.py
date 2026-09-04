"""Compile a genome -> run the REAL quantpylib engine -> metrics by segment.

Key idea: build the feature panel ONCE (wide tables on a common calendar, exactly as the
engine builds them). For a genome we compute alpha_panel (the wide signal table), then run
the engine with a lightweight PrecomputedAlpha class that simply writes the ready signal into
post_compute. This way all the position, vol-targeting and fee math stays in the engine and
identical to hand-written strategies.
"""
import os
import sys
import pickle
import warnings

import numpy as np
import pandas as pd

# Dev-only bootstrap so `import quantpylib` works when run straight from evolution/. In the
# frozen build quantpylib ships inside _internal and this would instead put the USER-WRITABLE
# app root at sys.path[0] — an invitation to shadow bundled modules with planted .py files.
if not getattr(sys, 'frozen', False):
    PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if PROJ not in sys.path:
        sys.path.insert(0, PROJ)

from quantpylib.simulator.alpha import Alpha           # noqa: E402
import primitives as P                                  # noqa: E402
from fastsim import precompute_market, fast_sim         # noqa: E402

warnings.filterwarnings('ignore')
np.seterr(divide='ignore', invalid='ignore')

ANN = 365
MIN_ACTIVE_FRAC = 0.10      # the signal must actually trade on at least 10% of the segment's days

BASE_FEATURES = ['close', 'open', 'high', 'low', 'volume']


def add_derived_features(panel):
    """Add derived terminal features to `panel` IN PLACE (same-day transforms of OHLCV, so
    look-ahead safe). Needs the 5 base OHLCV tables; adds 'ret' if it isn't there yet. Shared by
    the search panel (build_panel) and the live export (evolved_strategy) so the terminal set
    never diverges between them."""
    def _fin(df):
        return df.replace([np.inf, -np.inf], np.nan)
    if 'ret' not in panel:
        panel['ret'] = panel['close'].pct_change()
    panel['vwap'] = (panel['high'] + panel['low'] + panel['close']) / 3.0    # typical price
    panel['range'] = _fin((panel['high'] - panel['low']) / panel['close'])   # intrabar range
    panel['body'] = _fin((panel['close'] - panel['open']) / panel['open'])   # candle body
    panel['dvol'] = panel['close'] * panel['volume']                         # dollar volume
    panel['logret'] = _fin(np.log1p(panel['ret']))                           # log return
    return panel


# ---------------- panel ----------------
def load_raw(data_path, instruments=None):
    """(tickers, {ticker: OHLCV df}) from data.pickle — the raw per-ticker frames only, WITHOUT
    building the wide feature panel. For callers that need just the raw dfs (e.g. the real-engine
    portfolio workers), avoiding the panel's memory/CPU cost.

    instruments=None -> all pairs; a list -> keep only those, in that order.

    A requested pair the snapshot lacks is DROPPED with a warning, not an error: top-N-by-turnover
    universes shift between fetches (a pair falls out of the ranking), and raising here used to
    kill every pool worker's initializer — mp.Pool respawns such workers forever, so one stale
    ticker turned into an endless crash-dialog loop and a hung round. Only an entirely unknown
    universe still raises."""
    with open(data_path, 'rb') as f:
        tk, oh = pickle.load(f)
    if instruments:
        pos = {t: i for i, t in enumerate(tk)}
        missing = [t for t in instruments if t not in pos]
        if missing:
            keep = [t for t in instruments if t in pos]
            if not keep:
                raise ValueError(f'no data for {missing} in {os.path.basename(data_path)}. '
                                 f'Available: {tk}. New pairs need to be downloaded.')
            print(f'WARNING: no data for {", ".join(missing)} in {os.path.basename(data_path)} '
                  f'(the snapshot was refreshed and the ranking shifted?) — continuing on the '
                  f'{len(keep)} remaining pairs; re-download data to include them', flush=True)
            instruments = keep
        oh = [oh[pos[t]] for t in instruments]
        tk = list(instruments)
    return tk, {t: oh[i] for i, t in enumerate(tk)}


def build_panel(data_path, start, end, instruments=None, freq='D'):
    """(tickers, raw_dfs, feature_panel) on a common START..END grid at `freq` (default 'D' = daily;
    intraday timeframes pass their bar frequency — see timeframe.py).

    instruments=None -> all pairs from data.pickle; a list -> keep only those (in that order)."""
    tk, raw = load_raw(data_path, instruments)
    return tk, raw, panel_from_raw(tk, raw, start, end, freq)


def panel_from_raw(tk, raw, start, end, freq='D'):
    """The feature panel from an in-memory {ticker: OHLCV(+funding) df} dict — same maths as
    build_panel, no pickle needed (live signal computation feeds freshly fetched candles here)."""
    idx = pd.date_range(start=start, end=end, freq=freq, tz='UTC')

    # IMPORTANT (look-ahead guard): SIGNAL features are ffill only, NO bfill.
    # bfill would drag a not-yet-listed ticker's first price into the past, and
    # cross-sectional operators (cs_rank/zscore/...) would see the future in the market slice.
    # Pre-listing cells stay NaN -> correctly ignored by rank/mean(axis=1) and masked by
    # eligible. The simulation matrices (C/R/eligible) are engine-correctly bfilled inside
    # precompute_market — on live dates ffill and bfill coincide.
    def wide(col):
        return pd.DataFrame({t: raw[t][col] for t in tk}).reindex(idx).ffill()

    panel = {c: wide(c) for c in BASE_FEATURES}
    add_derived_features(panel)                 # 'ret' + vwap/range/body/dvol/logret

    # funding: the rate PAID during the bar (a flow, not a level — so missing bars become 0.0,
    # not ffill of the last payment). Snapshots fetched before funding support have no such
    # column -> all-zero feature (degenerate funding-only genomes die in the activity filter).
    # Pre-listing cells stay NaN (masked by close) — the cs-leak guard, as for every feature.
    fcols = {t: (raw[t]['funding'].reindex(idx) if 'funding' in raw[t].columns
                 else pd.Series(np.nan, index=idx)) for t in tk}
    panel['funding'] = pd.DataFrame(fcols).fillna(0.0).where(panel['close'].notna())
    return panel


def eval_alpha_panel(node, panel):
    """Compute the wide signal table from the tree (with a cache of shared subtrees)."""
    cache = {}
    return _eval(node, panel, cache)


def _eval(node, panel, cache):
    key = node.canon()
    if key in cache:
        return cache[key]
    if node.is_terminal:
        res = panel[node.op]
    else:
        args = [_eval(c, panel, cache) for c in node.children]
        res = P.apply_primitive(node.op, args, node.window)
    cache[key] = res
    return res


# ---------------- lightweight wrapper class ----------------
class PrecomputedAlpha(Alpha):
    """Engine that reads a pre-computed signal from a wide table."""

    def __init__(self, alpha_panel, **kwargs):
        super().__init__(**kwargs)
        self.alpha_panel = alpha_panel

    def pre_compute(self, date_range):
        pass

    def compute_forecasts(self, date, eligibles):
        return {inst: self.dfs[inst].at[date, 'alpha'] for inst in eligibles}

    def post_compute(self, date_range):
        for inst in self.insts:
            a = self.alpha_panel[inst].reindex(self.dfs[inst].index)
            self.dfs[inst]['alpha'] = a.ffill()
            self.dfs[inst]['eligible'] = self.dfs[inst]['eligible'] & (~pd.isna(self.dfs[inst]['alpha']))


STD_FLOOR = 1e-9        # variance floor: exact ==0 misses a near-constant series (std~1e-18)


# ---------------- metrics ----------------
# CALENDAR convention: flat (zero-return) bars STAY in the series. Dropping them (the old
# behavior) inflated Sharpe by ~1/sqrt(active_fraction) and CAGR far more, so the GA fitness
# systematically preferred sparse long-warm-up genomes (a strategy flat for 2/3 of TRAIN got a
# ~1.7x Sharpe bonus for it). Every genome is measured over the same segment calendar:
# "what would $1 in this strategy have done over the whole segment" — comparable across genomes.
# `n` still reports ACTIVE bars (a coverage/activity stat, not the metric window).
# `ann` = bars per year (365 daily; see timeframe.py for intraday).
def _sharpe(r, ann=ANN):
    act = (r != 0).sum()
    s = r.std()
    if act < 5 or not np.isfinite(s) or s < STD_FLOOR:
        return np.nan
    return (r.mean() * ann) / (s * np.sqrt(ann))


def _winrate(r):
    """Raw per-bar win rate: the share of ACTIVE bars (|r| > 1e-9) that gained — the SAME
    definition the leaderboard's win% column shows (metrics_worker). Display/diagnostic
    only: the SELECTION score is _wr_score below."""
    a = np.abs(r) > 1e-9
    if int(a.sum()) < 5:
        return np.nan
    return float((r[a] > 0).mean())


WR_MIN_ACT = 30         # under this many active bars a segment/block is not evidence at all


def _wr_score(r, se_penalty=1.0):
    """SELECTION score for the win-rate objective on one segment (or block).

    The raw win rate strips flat bars from its denominator — exactly the sparse-Sharpe
    inflation the calendar convention above was introduced to kill: a coin-flip genome
    active on 10% of bars clears min(TRAIN,VAL) wr >= 0.62 at p~2e-3, so a 200k-eval
    search fills the HoF with sparse luck while a dense genuine 55%-edge (~0.54 observed)
    can never enter (confirmed by review Monte Carlo). Three honesty layers fix that:
      1) calendar damping — the edge over a coin flip shrinks by sqrt(active share),
         the same convention that fixed sparse Sharpe: 62% of 40 bars < 55% of 700;
      2) binomial SE shrinkage with an add-one prior p~=(k+1)/(n+2) — keeps the SE
         alive at w=0/1, so a 5/5 streak reads as luck, not certainty;
      3) under WR_MIN_ACT active bars: NaN — no evidence, the genome is invalid.
    Result stays in win-rate units (~0..1), formattable as a percentage."""
    x = np.asarray(r, dtype=np.float64)
    a = np.abs(x) > 1e-9
    n_act = int(a.sum())
    if n_act < WR_MIN_ACT:
        return np.nan
    k = int((x[a] > 0).sum())
    wr = k / n_act
    pt = (k + 1.0) / (n_act + 2.0)
    se = np.sqrt(pt * (1.0 - pt) / n_act)
    damp = np.sqrt(n_act / max(len(x), 1))
    return float(0.5 + (wr - 0.5) * damp - se_penalty * se)


def _metrics(r, ann=ANN):
    act = int((r != 0).sum())
    s = r.std()
    if act < 5 or not np.isfinite(s) or s < STD_FLOOR:
        return None
    eq = (1 + r).cumprod()
    yrs = len(r) / ann
    last = float(eq.iloc[-1])
    return {
        'sharpe': (r.mean() * ann) / (s * np.sqrt(ann)),
        'dd': float((eq / eq.cummax() - 1).min()),
        'cagr': (last ** (1 / yrs) - 1) if (yrs > 0 and last > 0) else np.nan,  # wiped-out capital -> NaN, not complex
        'n': act,
        # stored with every champion so a SEALED row (vault: formula hidden, numbers visible)
        # can still be read: the raw win rate over ACTIVE bars — the leaderboard's own win%
        # definition — and the segment's total return, the 'PnL' a row shows without a key
        'wr': float((r[r != 0] > 0).mean()),
        'ret': last - 1.0,
    }


# ---------------- robust multi-block fitness ----------------
# The selection span (TRAIN start .. TEST start) is cut into `blocks` contiguous slices;
# each slice's Sharpe is shrunk by `se_penalty` standard errors (Lo 2002 — short or merely
# lucky slices lose the most), and the fitness is the `quantile` of the shrunk values
# (0 = strict worst block). One golden regime can no longer carry a formula: it has to
# work, at least modestly, almost everywhere. blocks = 0 restores min(TRAIN, VAL).
def _block_fitness(sel, ann, blocks, quantile, se_penalty, metric='sharpe'):
    n = len(sel)
    if n < blocks * 30:                                # too short to measure per block
        return None
    edges = np.linspace(0, n, blocks + 1).astype(int)
    adj = []
    for a, b in zip(edges[:-1], edges[1:]):
        r = sel.iloc[a:b]
        if metric == 'winrate':
            s = _wr_score(r, se_penalty)
            if not np.isfinite(s):                     # under WR_MIN_ACT active bars: a noisy
                n_act = int((np.abs(np.asarray(r)) > 1e-9).sum())   # coin flip — neutral minus
                s = 0.5 - se_penalty * np.sqrt(0.25 / max(n_act, 5))  # noise, never a prize
            adj.append(s)
            continue
        sh = _sharpe(r, ann)
        if not np.isfinite(sh):
            sh = 0.0                                   # no evidence in this slice ≠ disaster
        se = np.sqrt((1.0 + 0.5 * sh * sh) * ann / max(b - a, 1))
        adj.append(sh - se_penalty * se)
    return float(np.quantile(adj, quantile)), [round(float(x), 3) for x in adj]


def _mean_eff_n(A, elig, rows):
    """Average effective number of positions — 1/HHI of the |alpha| shares per bar. This is
    the book concentration the simulator actually trades to first order: vol targeting
    scales every weight by the same factor (HHI-invariant), inertia only smooths it."""
    X = np.where(elig[rows], np.abs(A[rows]), 0.0)
    X = np.where(np.isfinite(X), X, 0.0)
    g = X.sum(axis=1)
    live = g > 0
    if not live.any():
        return 0.0
    F = X[live] / g[live, None]
    return float((1.0 / np.maximum((F * F).sum(axis=1), 1e-12)).mean())


def _mean_net(A, V, elig, rows):
    """Time-average of net/gross — the book's persistent DIRECTIONAL tilt, in [-1, +1]:
    +1 = every bar fully long, 0 = long and short balance, -1 = fully short.

    Unlike _mean_eff_n this divides by V, because the simulator does: a long in a quiet coin
    carries more dollars than a long in a wild one, so inverse-vol weighting moves the tilt.
    A cross-sectional alpha should land near zero; a book that sits at +0.9 is not an alpha,
    it is the market with extra steps, and in crypto it will inherit the market's Sharpe."""
    X = np.where(elig[rows], A[rows], 0.0) / np.where(V[rows] > 0, V[rows], np.nan)
    X = np.where(np.isfinite(X), X, 0.0)
    g = np.abs(X).sum(axis=1)
    live = g > 0
    if not live.any():
        return 0.0
    return float((X[live].sum(axis=1) / g[live]).mean())


def make_market(panel, tk, raw=None, vol_window=30):
    return precompute_market(panel, tk, raw, vol_window=vol_window)


def simulate_returns(node, tk, panel, market, vol, exec_rate, ann=ANN, ewma_lambda=0.06):
    """Full series of the genome's NET returns over the whole period (for champion charts)."""
    try:
        ap = eval_alpha_panel(node, panel)
        return fast_sim(ap[tk].to_numpy(dtype=np.float64), market, vol, exec_rate,
                        ann=ann, ewma_lambda=ewma_lambda)
    except Exception:
        return None


def basket_returns(panel):
    """Equal-weight basket: mean ret over "active" tickers (like eligible in the engine)."""
    close = panel['close']
    sampled = (close != close.shift(1)).fillna(False)
    eligible = sampled.rolling(5, min_periods=1).max().fillna(0).astype(bool)
    return panel['ret'].where(eligible).mean(axis=1).fillna(0.0)


def vol_regime(panel, vol_window=30, ann=365.0):
    """Market volatility regime per bar: 1.0 = "storm" (the EW basket's realized vol is above
    its own trailing 1-year median), 0.0 = "calm", NaN = warmup (either window not filled yet).
    Causal — both the vol estimate and the median only look back — so regime-sliced stats have
    no look-ahead. The median split makes the two buckets roughly equal by construction, which
    keeps per-regime Sharpe comparable without showing bar counts."""
    b = basket_returns(panel)
    v = b.rolling(vol_window, min_periods=max(2, vol_window // 2)).std()
    med = v.rolling(int(ann), min_periods=max(vol_window, int(ann) // 4)).median()
    return (v > med).astype(float).where(v.notna() & med.notna())


def trend_regime(panel, window=30, t_hi=1.28):
    """Market DIRECTION regime per bar: +1 trending up, -1 trending down, 0 flat,
    NaN warmup. The gate is a drift t-statistic on the trailing window's basket
    log-returns (mean / (std/sqrt(n))): |t| >= t_hi labels the bar with the drift's
    sign, below it the bar is flat. NOT the R² of price-on-time — for an integrated
    price that R² is spuriously large (median ~0.45 on a DRIFTLESS random walk;
    ~62% of pure chop would read as 'trend'), while the t-stat is calibrated against
    exactly that null (t_hi=1.28 ≈ 90% one-sided confidence; ~21% of driftless noise
    still passes — honest, and the price of usable buckets). Returns are cleaned
    first (a zero close used to poison the cumsum into NaN forever). Causal: the
    window ends at the bar it labels — a consumer attributing a bar's RETURN to a
    regime must lag these labels one bar (metrics_worker.build_ctx does), or the
    label leaks the very return it conditions. The companion axis to vol_regime:
    that one splits TEST by how wild the market was, this one by where it went."""
    r = basket_returns(panel)
    r = np.log1p(r.clip(lower=-0.9999)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    mu = r.rolling(window, min_periods=window).mean()
    sd = r.rolling(window, min_periods=window).std()
    t = (mu * np.sqrt(float(window))) / sd                    # sd==0, mu==0 -> NaN -> flat;
    lab = np.sign(mu).where(t.abs() >= t_hi, 0.0)             # sd==0, mu!=0 -> inf -> a trend
    return lab.where(mu.notna())


def open_pnl_series(weights, rets):
    """Unrealized ("open") PnL of the currently held positions, as a share of the book.

    weights: DataFrame T×N of position weights (sign = side; an episode lasts while the sign
    holds). rets: DataFrame of per-asset simple returns on the same or a wider grid. Each bar
    accrues yesterday's weight × today's return into that asset's running episode; a sign flip
    (or going flat) "realizes" the episode, so its PnL leaves the line. The cross-asset sum is
    what the open book is currently up or down — the number a trader calls open PnL."""
    W = np.nan_to_num(weights.to_numpy(dtype=np.float64))
    R = np.nan_to_num(rets.reindex(index=weights.index, columns=weights.columns)
                      .to_numpy(dtype=np.float64))
    c = np.zeros_like(W)
    c[1:] = W[:-1] * R[1:]                     # what the position held INTO the bar earned on it
    s = np.zeros_like(W)
    s[1:] = np.sign(W[:-1])                    # the episode each accrual belongs to
    chg = np.ones_like(W, dtype=bool)
    chg[1:] = s[1:] != s[:-1]                  # episode starts (incl. re-entry after flat)
    cum = np.cumsum(c, axis=0)
    base = np.where(chg, cum - c, np.nan)      # cum level just before the episode's first accrual
    base = pd.DataFrame(base).ffill().to_numpy()
    per_asset = np.where(s != 0, cum - np.nan_to_num(base), 0.0)
    return pd.Series(per_asset.sum(axis=1), index=weights.index)


def evaluate(node, tk, panel, market, splits, vol, exec_rate, ann=ANN, ewma_lambda=0.06,
             fit=None):
    """Run the genome through the fast engine. Return a dict with per-segment metrics, the
    selection fitness `base_fit` and the train+val returns vector (for correlation/novelty).
    None -> an invalid genome. `fit` (optional dict: blocks / quantile / se_penalty /
    conc_penalty / min_eff_n) switches base_fit from the legacy min(TRAIN, VAL) Sharpe to
    the robust multi-block fitness — see _block_fitness."""
    try:
        alpha_panel = eval_alpha_panel(node, panel)
    except Exception:
        return None
    if alpha_panel is None or not np.isfinite(alpha_panel.to_numpy()).any():
        return None

    A = alpha_panel[tk].to_numpy(dtype=np.float64)
    try:
        ret = fast_sim(A, market, vol, exec_rate, ann=ann, ewma_lambda=ewma_lambda)
    except Exception:
        return None

    tr = ret[(ret.index >= splits['train'][0]) & (ret.index < splits['train'][1])]
    va = ret[(ret.index >= splits['val'][0]) & (ret.index < splits['val'][1])]
    te = ret[(ret.index >= splits['test'][0]) & (ret.index < splits['test'][1])]

    # degeneracy filter: the signal must actually trade in train and val
    if (tr != 0).mean() < MIN_ACTIVE_FRAC or (va != 0).mean() < MIN_ACTIVE_FRAC:
        return None

    m_tr, m_va, m_te = _metrics(tr, ann), _metrics(va, ann), _metrics(te, ann)
    if m_tr is None or m_va is None:
        return None

    metric = (fit or {}).get('metric', 'sharpe')
    if metric == 'winrate':
        sp = float((fit or {}).get('se_penalty', 1.0))
        wr_tr, wr_va = _wr_score(tr, sp), _wr_score(va, sp)   # evidence-shrunk win rate
        if not (np.isfinite(wr_tr) and np.isfinite(wr_va)):
            return None
        base_fit = min(wr_tr, wr_va)                   # the score must hold on BOTH segments
    else:
        base_fit = min(m_tr['sharpe'], m_va['sharpe'])  # legacy fitness (blocks = 0)
    # penalties below are calibrated in Sharpe units (a ~2.5-wide scale); win rate lives
    # on ~0.25 (0.40..0.65), so every subtraction shrinks 10x or it swamps the signal
    scale = 0.1 if metric == 'winrate' else 1.0
    blocks_adj = eff_n = None
    if fit and int(fit.get('blocks', 0)) >= 2:
        sel = ret[(ret.index >= splits['train'][0]) & (ret.index < splits['test'][0])]
        bf = _block_fitness(sel, ann, int(fit['blocks']),
                            float(fit.get('quantile', 0.25)),
                            float(fit.get('se_penalty', 1.0)), metric=metric)
        if bf is None:
            return None
        base_fit, blocks_adj = bf
    sel_rows = None
    if fit and float(fit.get('conc_penalty', 0.0)) > 0:   # independent of the blocks switch
        sel_rows = ((alpha_panel.index >= splits['train'][0])
                    & (alpha_panel.index < splits['test'][0]))
        eff_n = round(_mean_eff_n(A, market['base_elig'], sel_rows), 2)
        need = float(fit.get('min_eff_n', 3.0))
        if eff_n < need:                               # one-coin books bleed fitness
            base_fit -= scale * float(fit['conc_penalty']) * (need - eff_n) / need
    net = None
    if fit and float(fit.get('net_penalty', 0.0)) > 0:
        if sel_rows is None:
            sel_rows = ((alpha_panel.index >= splits['train'][0])
                        & (alpha_panel.index < splits['test'][0]))
        net = round(_mean_net(A, market['V'], market['base_elig'], sel_rows), 3)
        cap = float(fit.get('max_net', 0.5))            # tilt allowed before it costs anything
        if abs(net) > cap:                              # one-sided books bleed fitness
            base_fit -= (scale * float(fit['net_penalty'])
                         * (abs(net) - cap) / max(1.0 - cap, 1e-9))

    rv = pd.concat([tr, va])                    # vector for correlation/novelty
    return {
        'canon': node.canon(),
        'size': node.size(),
        'train': m_tr, 'val': m_va, 'test': m_te,
        'train_sharpe': m_tr['sharpe'], 'val_sharpe': m_va['sharpe'],
        'base_fit': float(base_fit), 'blocks': blocks_adj, 'eff_n': eff_n, 'net': net,
        'rv': rv.to_numpy(dtype=np.float32),
    }

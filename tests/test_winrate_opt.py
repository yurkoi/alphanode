"""Optimize-by-win-rate: the checkbox switches the GP objective from Sharpe to the
per-bar win rate — the SAME number the leaderboard's win% column shows — as
min(TRAIN, VAL), with every Sharpe-calibrated penalty rescaled to the win-rate
scale (0.25-wide vs 2.5-wide: unscaled parsimony alone would collapse the GP into
stumps). TEST stays held out. The leaderboard grows longs/shorts per asset per
year and call accuracy by predicted side (wup/wdown: long cells judged by the
asset's next-bar rise, short cells by its fall — the formula's own calls, not
the market regime)."""
import configparser
import io
import os

import numpy as np
import pandas as pd
import pytest

import config as evcfg
import evolution as evo
from evaluator import _block_fitness, _winrate, _wr_score, build_panel, evaluate, make_market
from genome import parse


# ---------- the metric itself ----------

def test_winrate_counts_only_active_bars():
    r = pd.Series([0.01, -0.01, 0.02, 0.0, 0.0, 0.01, -0.02, 0.03])
    assert _winrate(r) == pytest.approx(4 / 6)           # zeros are not wins, not losses
    assert np.isnan(_winrate(pd.Series([0.01, 0.02, 0.0])))   # under 5 active bars: no evidence


def test_wr_score_is_damped_and_shrunk():
    """The selection score = 0.5 + (wr-0.5)*sqrt(active share) - SE(add-one prior): the
    same calendar convention that killed sparse-Sharpe inflation, applied to win rate."""
    r = pd.Series(np.where(np.arange(100) % 3 == 0, -0.01, 0.01))   # dense: wr=2/3, n=100
    k, n_act = 66, 100                                   # 34 bars hit %3==0 -> 66 wins
    pt = (k + 1) / (n_act + 2)
    want = 0.5 + (k / n_act - 0.5) * 1.0 - np.sqrt(pt * (1 - pt) / n_act)
    assert _wr_score(r) == pytest.approx(want, rel=1e-12)
    assert np.isnan(_wr_score(pd.Series([0.01] * 29 + [0.0])))   # under 30 active bars: no evidence


def test_sparse_luck_scores_below_dense_honesty():
    """The confirmed critical: a coin-flip genome active on 10% of bars posting a lucky
    62% must NOT outrank a dense genuine 55% edge (raw win rate inverted this ranking —
    review Monte Carlo put the ENTIRE HoF at the activity floor)."""
    n = 700
    dense = np.where(np.arange(n) % 20 < 11, 0.01, -0.01)          # 55% of 700, always active
    sparse = np.zeros(n)
    sparse[:70] = np.where(np.arange(70) % 10 < 7, 0.01, -0.01)    # lucky 70% on 10% of bars
    assert _winrate(pd.Series(sparse)) > _winrate(pd.Series(dense))   # raw wr says sparse wins…
    assert _wr_score(pd.Series(dense)) > _wr_score(pd.Series(sparse))  # …the score says honesty


def test_block_winrate_thin_perfect_block_cannot_win():
    """Confirmed major: under the old 1e-4 variance floor a 5-active-bar all-win block
    scored 0.9955 and beat every dense honest block. Now: thin evidence = neutral minus
    noise, and each dense block carries the full damp+shrink score."""
    idx = pd.date_range('2024-01-01', periods=120, freq='D', tz='UTC')
    r = np.where(np.arange(120) % 3 == 0, -0.01, 0.01)   # block 1: dense wr=2/3
    r[60:] = 0.0
    r[60:65] = 0.01                                      # block 2: 5 active bars, all wins
    s = pd.Series(r, index=idx)
    got, adj = _block_fitness(s, 365.0, 2, 0.0, 1.0, metric='winrate')
    assert adj[1] < 0.5 < adj[0]                         # perfect-but-thin sits BELOW neutral
    assert got == pytest.approx(adj[1], abs=5e-4)        # strict-worst quantile takes the thin one
    assert _block_fitness(s, 365.0, 2, 0.0, 1.0, metric='winrate')[0] < adj[0]


# ---------- evaluate(): the objective switch on real data ----------

@pytest.fixture(scope='module')
def engine_world():
    cfg = evcfg.load_config()
    tk, raw, panel = build_panel(cfg['data'], cfg['start'], cfg['end'], cfg.get('instruments'))
    market = make_market(panel, tk, raw)
    return cfg, tk, panel, market


def test_evaluate_winrate_is_min_trainval_winrate_of_the_same_sim(engine_world):
    """base_fit in winrate mode must equal min over TRAIN/VAL of the evidence-shrunk
    win-rate score of the exact simulated return series — recomputed here through the
    same pipeline. Equality proves the objective derives from TRAIN/VAL only."""
    from evaluator import eval_alpha_panel
    from fastsim import fast_sim
    cfg, tk, panel, market = engine_world
    ann = float(cfg.get('ann', 365.0))
    node = parse('tanh(ret)')
    r_sh = evaluate(node, tk, panel, market, cfg['splits'], cfg['vol'], cfg['exec'],
                    ann=ann, fit={'metric': 'sharpe'})
    r_wr = evaluate(node, tk, panel, market, cfg['splits'], cfg['vol'], cfg['exec'],
                    ann=ann, fit={'metric': 'winrate'})
    assert r_sh is not None and r_wr is not None
    A = eval_alpha_panel(node, panel)[tk].to_numpy(dtype=np.float64)
    ret = fast_sim(A, market, cfg['vol'], cfg['exec'], ann=ann)
    sp = cfg['splits']
    tr = ret[(ret.index >= sp['train'][0]) & (ret.index < sp['train'][1])]
    va = ret[(ret.index >= sp['val'][0]) & (ret.index < sp['val'][1])]
    assert r_wr['base_fit'] == pytest.approx(min(_wr_score(tr), _wr_score(va)), rel=1e-12)
    assert 0.0 < r_wr['base_fit'] < 1.0
    assert r_wr['base_fit'] != r_sh['base_fit']          # a genuinely different objective
    assert r_sh['base_fit'] == pytest.approx(            # and sharpe mode is untouched
        min(r_sh['train']['sharpe'], r_sh['val']['sharpe']), rel=1e-12)


def test_evaluate_winrate_blocks_path(engine_world):
    cfg, tk, panel, market = engine_world
    ann = float(cfg.get('ann', 365.0))
    node = parse('tanh(ret)')
    fit = {'metric': 'winrate', 'blocks': 4, 'quantile': 0.25, 'se_penalty': 1.0}
    r = evaluate(node, tk, panel, market, cfg['splits'], cfg['vol'], cfg['exec'],
                 ann=ann, fit=fit)
    assert r is not None and 0.0 < r['base_fit'] < 1.0
    from evaluator import eval_alpha_panel
    from fastsim import fast_sim
    A = eval_alpha_panel(node, panel)[tk].to_numpy(dtype=np.float64)
    ret = fast_sim(A, market, cfg['vol'], cfg['exec'], ann=ann)
    sp = cfg['splits']
    sel = ret[(ret.index >= sp['train'][0]) & (ret.index < sp['test'][0])]
    want, _adj = _block_fitness(sel, ann, 4, 0.25, 1.0, metric='winrate')
    assert r['base_fit'] == pytest.approx(want, rel=1e-12)


# ---------- selection-layer scaling + plumbing ----------

def test_fitness_penalties_shrink_to_the_winrate_scale():
    res = {'base_fit': 0.6, 'size': 20, 'rv': np.zeros(10, dtype=np.float32)}
    cfg = {'parsimony': 0.01, 'corr_thresh': 0.7, 'corr_penalty': 0.3}
    assert evo.fitness(res, [], cfg) == pytest.approx(0.6 - 0.01 * 20)
    assert evo.fitness(res, [], {**cfg, 'fit_metric': 'winrate'}) \
        == pytest.approx(0.6 - 0.1 * 0.01 * 20)          # 10x shrink or parsimony eats the signal


def test_fit_cfg_forwards_the_metric_to_every_worker():
    assert evo.fit_cfg({'fit_metric': 'winrate'})['metric'] == 'winrate'
    assert evo.fit_cfg({})['metric'] == 'sharpe'


def test_config_ini_default_and_env_override(monkeypatch):
    assert evcfg.load_config().get('fit_metric') == 'sharpe'
    import node as nd
    for k in ('ALPHANODE_TRAIN_START', 'ALPHANODE_VAL_START',
              'ALPHANODE_TEST_START', 'ALPHANODE_TEST_END'):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv('ALPHANODE_FIT_METRIC', ' WinRate ')
    cfg = {}
    nd._apply_overrides(cfg)
    assert cfg['fit_metric'] == 'winrate'                # normalized, ready for fit_cfg


def test_champions_carry_the_objective_tag():
    import node as nd
    h = [{'canon': 'tanh(ret)', 'size': 3, 'base': 0.55}]
    assert nd.champions_from_hof(h, metric='winrate')[0]['fit_metric'] == 'winrate'
    assert nd.champions_from_hof(h)[0]['fit_metric'] == 'sharpe'


def test_node_leaderboard_prefers_the_active_metric(monkeypatch):
    """Confirmed major: winrate bases (<=1.0) drowned under old Sharpe rows (~1.5-2.5)
    in one raw ladder — refine kept seeding Sharpe formulas during a winrate run."""
    import node as nd
    rows = [{'base': 2.0}, {'base': 0.60, 'fit_metric': 'winrate'}, {'base': 1.2}]
    monkeypatch.setattr(nd, 'FIT_METRIC', 'winrate')
    rows.sort(key=nd._rank_key)
    assert rows[0]['fit_metric'] == 'winrate'            # the run's own objective leads
    assert [r['base'] for r in rows[1:]] == [2.0, 1.2]   # legacy rows keep their order below
    monkeypatch.setattr(nd, 'FIT_METRIC', 'sharpe')
    rows.sort(key=nd._rank_key)
    assert [r['base'] for r in rows] == [2.0, 1.2, 0.60]


def test_node_formats_fitness_in_its_own_units():
    import node as nd
    assert nd._fmt_fit({'fit_metric': 'winrate'}, 0.57) == '57%'
    assert nd._fmt_fit({}, 1.853) == '+1.85'
    assert nd._fmt_fit({}, None) == '—'


def test_rescore_keeps_the_winrate_objective(monkeypatch):
    """Confirmed: rescore_library used to overwrite a winrate row's base with a Sharpe
    while keeping the 'winrate' tag — the GUI then showed '-109%' style nonsense."""
    import evaluator as ev
    import genome as gn
    import rescore_library as rl
    seen = {}

    def fake_eval(node, tk, panel, market, splits, vol, exec_rate,
                  ann=365.0, ewma_lambda=0.06, fit=None):
        seen['fit'] = fit
        return {'train': {'sharpe': 1.0}, 'val': {'sharpe': 0.9}, 'test': {'sharpe': 0.1},
                'train_sharpe': 1.0, 'val_sharpe': 0.9, 'base_fit': 0.57}

    monkeypatch.setattr(ev, 'evaluate', fake_eval)
    monkeypatch.setattr(gn, 'parse', lambda s: None)
    monkeypatch.setitem(rl._G, 'cfg', {'splits': {}, 'vol': 0.3, 'exec': 0.0})
    for k in ('tk', 'panel', 'market'):
        monkeypatch.setitem(rl._G, k, None)
    out = rl._rescore_one({'formula': 'x', 'base': 0.44, 'fit_metric': 'winrate'})
    assert seen['fit'] == {'metric': 'winrate'}          # re-scored under the row's objective
    assert out['base'] == pytest.approx(0.57)            # base_fit, not min(train,val) Sharpe
    assert out['fit_metric'] == 'winrate'


# ---------- worker: the new leaderboard numbers ----------

def test_call_accuracy_guards_and_orientation():
    import metrics_worker as mw
    rng = np.random.default_rng(3)
    x = np.where(np.arange(90) % 3 == 0, -0.01, 0.01) + rng.normal(0, 1e-6, 90)
    assert mw.call_accuracy(x, up=True) == pytest.approx(2 / 3, abs=1e-9)     # long calls: rises are right
    assert mw.call_accuracy(x, up=False) == pytest.approx(1 / 3, abs=1e-9)    # short calls: falls are right
    assert mw.call_accuracy(x[:20], up=True) is None     # under 30 calls: no evidence
    assert mw.call_accuracy(np.zeros(50), up=True) is None   # nothing ever moved: nothing judged


def test_call_accuracy_judges_the_formulas_own_calls():
    """abs(...) never goes short, so win↓ has no calls to judge; neg(abs(...)) holds the
    EXACT mirror book on the same cells (weights normalize to −W), so its down-accuracy
    must complement the long twin's up-accuracy to 1 — flat cells drop out of both."""
    import metrics_worker as mw
    cp = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    cp.read(os.environ['ALPHANODE_CONFIG_INI'])
    seg = cp['segments']
    ctx = mw.build_ctx({'formulas': ['abs(tanh(ret))'],
                        'train_start': seg['train_start'].strip(),
                        'test_start': seg['test_start'].strip(),
                        'test_end': seg['test_end'].strip()})
    lo = mw.trade_stats('abs(tanh(ret))', ctx)
    sh = mw.trade_stats('neg(abs(tanh(ret)))', ctx)
    assert isinstance(lo, dict) and isinstance(sh, dict)
    assert lo['short'] == 0 and sh['long'] == 0          # no calls on the silent side…
    assert lo['wdown'] is None and sh['wup'] is None     # …means nothing to be accurate about
    assert lo['wup'] is not None and sh['wdown'] is not None
    assert lo['wup'] + sh['wdown'] == pytest.approx(1.0)


def test_call_accuracy_judges_the_NEXT_bar_not_the_current_one():
    """The lag is the whole column. 'ret' as a signal goes long exactly the assets that
    rose on the bar it is computed from, so scoring a call against its OWN bar reads a
    perfect 100% — a mirage of pure hindsight. Scored against the bar the position is
    actually held into (the one the simulator books PnL on) the same signal lands near a
    coin flip, which is the truth about yesterday's return as a predictor. Measured:
    0.48 shipped vs 1.00 for the off-by-one."""
    import metrics_worker as mw
    cp = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    cp.read(os.environ['ALPHANODE_CONFIG_INI'])
    seg = cp['segments']
    ctx = mw.build_ctx({'formulas': ['ret'],
                        'train_start': seg['train_start'].strip(),
                        'test_start': seg['test_start'].strip(),
                        'test_end': seg['test_end'].strip()})
    m = mw.trade_stats('ret', ctx)
    assert isinstance(m, dict)
    for k in ('wup', 'wdown'):
        assert 0.35 < m[k] < 0.65, f'{k}={m[k]} — a hindsight signal cannot be this good'


def test_trade_stats_emits_annualized_sides_and_call_accuracy():
    import metrics_worker as mw
    cp = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    cp.read(os.environ['ALPHANODE_CONFIG_INI'])
    seg = cp['segments']
    ctx = mw.build_ctx({'formulas': ['tanh(ret)'],
                        'train_start': seg['train_start'].strip(),
                        'test_start': seg['test_start'].strip(),
                        'test_end': seg['test_end'].strip()})
    m = mw.trade_stats('tanh(ret)', ctx)
    assert isinstance(m, dict)
    assert m['long_yr'] == pytest.approx(m['long'] / ctx['n_assets'] / ctx['years'])
    assert m['short_yr'] == pytest.approx(m['short'] / ctx['n_assets'] / ctx['years'])
    assert m['act'] == pytest.approx(m['long_yr'] + m['short_yr'])   # the same rate, split
    for k in ('wup', 'wdown'):
        assert m[k] is None or 0.0 <= m[k] <= 1.0


# ---------- GUI: checkbox -> env, columns render ----------

def test_gui_checkbox_flows_to_the_node_env(gui_app, monkeypatch):
    import alphanode_gui as G
    app, _rec, _state = gui_app
    captured = {}

    class FakeProc:
        stdout = io.StringIO('')
        pid = 424242

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(G.subprocess, 'Popen',
                        lambda *a, **kw: captured.update(env=kw.get('env')) or FakeProc())
    app.v_optwr.set(True)
    app.cfg.update(app._collect())
    assert app.cfg['opt_winrate'] is True
    app._start_node('', False)
    assert captured['env']['ALPHANODE_FIT_METRIC'] == 'winrate'
    app.proc = None                                      # don't let teardown poke the fake
    app.v_optwr.set(False)
    app.cfg.update(app._collect())
    app._start_node('', False)
    assert captured['env']['ALPHANODE_FIT_METRIC'] == 'sharpe'
    app.proc = None


def test_gui_new_columns_render_from_the_cache(gui_app):
    app, _rec, _state = gui_app
    champ = {'formula': 'tanh(low)', 'base': 0.57, 'fit_metric': 'winrate',
             'test': {'sharpe': 0.5}}
    app._metrics_cache[champ['formula']] = {
        'long': 30, 'short': 10, 'long_yr': 12.3, 'short_yr': 4.1,
        'win': 0.55, 'wup': 0.62, 'wdown': None, 'act': 16.4, 'dd': -0.1,
        'cagr': 0.2, 'sortino': 1.0, 'tup': 1.0, 'tdown': -0.5, 'tflat': None}
    app._treesig = None
    app._fill_tree([champ])
    item = app.tree.get_children()[0]
    assert app.tree.set(item, 'fit') == '57%'            # winrate-mined row: base is a share
    assert app.tree.set(item, 'ls') == '12.3/4.1'        # entries / asset / year, by side
    assert app.tree.set(item, 'wup') == '62%'
    assert app.tree.set(item, 'wdown') == '—'            # idle or thin bucket: honest dash
    for c in ('wup', 'wdown'):
        assert c in app.tree['displaycolumns']
        assert c in app._SORTABLE
    sharpe_row = {'formula': 'tanh(low)', 'base': 1.23, 'test': {'sharpe': 0.5}}
    app._treesig = None
    app._fill_tree([sharpe_row])
    item = app.tree.get_children()[0]
    assert app.tree.set(item, 'fit') == '+1.23'          # untagged rows keep the Sharpe format
    assert not app._test_tk_errors


def test_gui_defaults_and_reset_know_the_checkbox(gui_app):
    import alphanode_gui as G
    app, _rec, _state = gui_app
    assert G.DEFAULTS['opt_winrate'] is False
    app.v_optwr.set(True)
    app._apply_cfg_to_widgets()                          # reset pushes cfg back into widgets
    assert app.v_optwr.get() is False

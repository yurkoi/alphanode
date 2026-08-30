"""Directional tilt: show how the book sits, and dock the fitness of one-sided books.

A long-only "alpha" in crypto is the market with extra steps — it inherits the market's
Sharpe, and during a bull leg it scores well on TRAIN and VAL for reasons that have nothing
to do with the formula. net/gross names the tilt in one number, the leaderboard shows it as a
long/short split, and the search charges for it past max_net.
"""
import numpy as np
import pytest

import config as evcfg
from evaluator import _mean_net, build_panel, evaluate, make_market
from genome import parse


def _mk(A, V=None, elig=None):
    A = np.asarray(A, dtype=np.float64)
    V = np.ones_like(A) if V is None else np.asarray(V, dtype=np.float64)
    elig = np.ones_like(A, dtype=bool) if elig is None else np.asarray(elig)
    return A, V, elig, np.ones(A.shape[0], dtype=bool)


# ---------- the statistic ----------

def test_all_long_is_plus_one_all_short_is_minus_one():
    A, V, e, rows = _mk([[1.0, 2.0, 3.0]] * 10)
    assert _mean_net(A, V, e, rows) == pytest.approx(1.0)
    A, V, e, rows = _mk([[-1.0, -2.0, -3.0]] * 10)
    assert _mean_net(A, V, e, rows) == pytest.approx(-1.0)


def test_balanced_book_is_zero():
    A, V, e, rows = _mk([[2.0, -2.0, 1.0, -1.0]] * 10)
    assert _mean_net(A, V, e, rows) == pytest.approx(0.0, abs=1e-12)


def test_tilt_is_inverse_vol_weighted_like_the_simulator():
    """A long in a quiet coin carries more dollars than a short in a wild one, so the tilt
    must be computed on A/V — the weights the engine actually trades — not on A."""
    A = [[1.0, -1.0]] * 10                     # equal raw signal: naive net would be 0
    V = [[0.01, 0.10]] * 10                    # the long sits in the 10x quieter coin
    a, v, e, rows = _mk(A, V)
    assert _mean_net(a, v, e, rows) == pytest.approx((100 - 10) / (100 + 10))


def test_ineligible_and_dead_bars_do_not_count():
    A = [[1.0, 1.0], [0.0, 0.0], [1.0, -1.0]]
    a, v, e, rows = _mk(A)
    e = np.array([[True, True], [True, True], [True, True]])
    # bar 1 holds nothing -> skipped entirely; bars 0 and 2 average +1 and 0
    assert _mean_net(a, v, e, rows) == pytest.approx(0.5)
    e2 = np.array([[True, False], [True, True], [True, True]])   # asset 1 ineligible on bar 0
    assert _mean_net(a, v, e2, rows) == pytest.approx((1.0 + 0.0) / 2)


def test_no_live_bars_is_zero_not_a_crash():
    a, v, e, rows = _mk([[0.0, 0.0]] * 5)
    assert _mean_net(a, v, e, rows) == 0.0


def test_nan_signal_does_not_poison_the_average():
    a, v, e, rows = _mk([[np.nan, 1.0], [1.0, -1.0]])
    assert np.isfinite(_mean_net(a, v, e, rows))


# ---------- the penalty ----------

@pytest.fixture(scope='module')
def engine_world():
    cfg = evcfg.load_config()
    tk, raw, panel = build_panel(cfg['data'], cfg['start'], cfg['end'], cfg.get('instruments'))
    return cfg, tk, panel, make_market(panel, tk, raw)


def _ev(world, formula, **fit):
    cfg, tk, panel, market = world
    return evaluate(parse(formula), tk, panel, market, cfg['splits'], cfg['vol'], cfg['exec'],
                    ann=float(cfg.get('ann', 365.0)), fit=dict({'metric': 'sharpe'}, **fit))


def test_one_sided_book_is_docked_and_a_neutral_one_is_not(engine_world):
    """abs() can never go short, so its book sits at net/gross = +1 and must bleed; the same
    formula through cs_demean is forced balanced and must keep every point of its fitness."""
    one_sided = 'abs(tanh(ret))'
    neutral = 'cs_demean(tanh(ret))'
    for f, docked in ((one_sided, True), (neutral, False)):
        free = _ev(engine_world, f, net_penalty=0.0)
        guarded = _ev(engine_world, f, net_penalty=0.5, max_net=0.5)
        if free is None or guarded is None:
            pytest.skip(f'{f} does not trade on this snapshot')
        assert abs(guarded['net']) <= 1.0
        if docked:
            assert abs(guarded['net']) > 0.5
            assert guarded['base_fit'] < free['base_fit']
        else:
            assert abs(guarded['net']) < 0.5
            assert guarded['base_fit'] == pytest.approx(free['base_fit'], rel=1e-12)


def test_the_dock_scales_with_how_far_past_the_cap_it_sits(engine_world):
    a = _ev(engine_world, 'abs(tanh(ret))', net_penalty=0.5, max_net=0.5)
    b = _ev(engine_world, 'abs(tanh(ret))', net_penalty=1.0, max_net=0.5)
    if a is None or b is None:
        pytest.skip('does not trade on this snapshot')
    free = _ev(engine_world, 'abs(tanh(ret))', net_penalty=0.0)
    assert (free['base_fit'] - b['base_fit']) == pytest.approx(
        2 * (free['base_fit'] - a['base_fit']), rel=1e-9)


def test_a_generous_cap_lets_everything_through(engine_world):
    free = _ev(engine_world, 'abs(tanh(ret))', net_penalty=0.0)
    wide = _ev(engine_world, 'abs(tanh(ret))', net_penalty=0.5, max_net=1.0)
    if free is None or wide is None:
        pytest.skip('does not trade on this snapshot')
    assert wide['base_fit'] == pytest.approx(free['base_fit'], rel=1e-12)


def test_net_is_reported_only_when_the_guard_runs(engine_world):
    off = _ev(engine_world, 'tanh(ret)', net_penalty=0.0)
    on = _ev(engine_world, 'tanh(ret)', net_penalty=0.5)
    assert off['net'] is None                     # not computed, not guessed
    assert isinstance(on['net'], float)


def test_guard_never_touches_test(engine_world):
    """The tilt is measured on TRAIN+VAL only — the same discipline as every other penalty."""
    a = _ev(engine_world, 'tanh(ret)', net_penalty=0.0)
    b = _ev(engine_world, 'tanh(ret)', net_penalty=0.9, max_net=0.0)
    assert b['test']['sharpe'] == pytest.approx(a['test']['sharpe'], rel=1e-12)


# ---------- plumbing ----------

def test_fit_cfg_and_config_carry_the_knobs():
    import evolution as ev
    d = ev.fit_cfg({})
    assert d['net_penalty'] == 0.0 and d['max_net'] == 0.5
    d = ev.fit_cfg({'fit_net_penalty': 0.7, 'fit_max_net': 0.3})
    assert d['net_penalty'] == 0.7 and d['max_net'] == 0.3
    live = evcfg.load_config()
    assert live['fit_net_penalty'] == 0.5 and live['fit_max_net'] == 0.5   # on by default


def test_gui_renders_the_split(gui_app):
    app, _rec, _state = gui_app
    champ = {'formula': 'tanh(low)', 'base': 1.2, 'test': {'sharpe': 0.5}}
    app._metrics_cache[champ['formula']] = {
        'long': 30, 'short': 10, 'long_yr': 12.3, 'short_yr': 4.1, 'win': 0.55,
        'wup': 0.6, 'wdown': 0.5, 'act': 16.4, 'dd': -0.1, 'cagr': 0.2, 'sortino': 1.0,
        'tup': 1.0, 'tdown': -0.5, 'tflat': None, 'net': 0.4}
    app._treesig = None
    app._fill_tree([champ])
    item = app.tree.get_children()[0]
    assert app.tree.set(item, 'bal') == '70/30'           # net +0.4 -> 70% long, 30% short
    assert 'bal' in app.tree['displaycolumns']
    assert 'bal' in app._SORTABLE
    app._metrics_cache[champ['formula']]['net'] = -1.0
    app._treesig = None
    app._fill_tree([champ])
    assert app.tree.set(app.tree.get_children()[0], 'bal') == '0/100'
    app._metrics_cache[champ['formula']].pop('net')       # a doc from an older worker
    app._treesig = None
    app._fill_tree([champ])
    assert app.tree.set(app.tree.get_children()[0], 'bal') == '—'

"""The vault's return-series cache and the sealed portfolio preview built from it.

Without an activation key the node seals every formula, so the portfolio builder (another
process) has nothing to simulate. The node therefore caches each champion's NET return series
(series_cache) — no forward signal in it, nothing to trade — and the builder curates a preview
from those: the same combination search, an equal-weight mix, no formulas, no weights.
"""
import hashlib
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'alphanode'))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'evolution'))
import series_cache as SC                                # noqa: E402
import portfolio_build as PB                             # noqa: E402
import evaluator as EV                                   # noqa: E402


def _series(n, seed, start='2024-01-01'):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq='D', tz='UTC')
    return pd.Series(rng.normal(0.001, 0.01, n), index=idx)


def test_save_load_round_trip_keeps_the_utc_grid(tmp_path):
    r = _series(500, 1)
    p = SC.path(str(tmp_path), '1d', 'abc123def456')
    SC.save(p, r)
    back = SC.load(p)
    assert (back.index == r.index).all() and str(back.index.tz) == 'UTC'
    assert np.allclose(back.to_numpy(), r.to_numpy(), atol=1e-6), 'float32 on disk'
    assert SC.has(str(tmp_path), '1d', 'abc123def456')
    assert not SC.has(str(tmp_path), '1h', 'abc123def456'), 'one cache per timeframe'
    assert not os.path.exists(p + '.tmp'), 'atomic write leaves no tmp behind'


def test_prune_keeps_only_the_named_ids(tmp_path):
    for fid in ('a' * 12, 'b' * 12, 'c' * 12):
        SC.save(SC.path(str(tmp_path), '15m', fid), _series(50, 2))
    assert SC.prune(str(tmp_path), '15m', ['a' * 12]) == 2
    assert os.listdir(SC.cache_dir(str(tmp_path), '15m')) == ['a' * 12 + '.npz']
    assert SC.prune(str(tmp_path), '4h', []) == 0, 'no cache dir yet: nothing to do, no error'


def _write_lib(tmp_path, docs):
    lib = tmp_path / 'library.jsonl'
    lib.write_text('\n'.join(json.dumps(d) for d in docs) + '\n', encoding='utf-8')
    return str(lib)


def test_pick_top_admits_a_sealed_row_only_with_a_cached_series(tmp_path, monkeypatch):
    monkeypatch.setenv('ALPHANODE_STATE_DIR', str(tmp_path))
    lib = _write_lib(tmp_path, [
        {'formula': 'open_a(x)', 'base': 2.0, 'test': {'sharpe': 1.0}},
        {'locked': True, 'id': 'c' * 12, 'base': 2.5, 'test': {'sharpe': 1.2}},   # cached
        {'locked': True, 'id': 'd' * 12, 'base': 2.4, 'test': {'sharpe': 1.1}}])  # not cached
    SC.save(SC.path(str(tmp_path), '1d', 'c' * 12), _series(100, 3))
    top = PB._pick_top(10, 'base', lib, '1d', sealed_ok=True)
    assert [PB._row_id(c) for c in top] == ['c' * 12, hashlib.md5(b'open_a(x)').hexdigest()[:12]]
    assert all(c.get('formula') for c in PB._pick_top(10, 'base', lib, '1d')), \
        'the default stays plaintext-only (the real-engine paths need the text)'


def test_too_few_names_the_seal_as_the_reason(tmp_path):
    sealed = _write_lib(tmp_path, [{'locked': True, 'id': 'e' * 12, 'base': 1.0,
                                    'test': {'sharpe': 0.5}}])
    assert 'sealed' in str(PB._too_few(sealed)) and 'activate' in str(PB._too_few(sealed))
    plain = _write_lib(tmp_path, [{'formula': 'f(x)', 'base': 1.0}])
    assert 'sealed' not in str(PB._too_few(plain))


def test_sealed_pick_drops_clones_by_correlation_and_searches_the_span_only():
    """No text to compare in a sealed pool: a near-identical return series IS the clone. And
    the selection span ends at TEST start — an alpha that shines only inside TEST is not
    preferred over one that earns on TRAIN+VAL."""
    rng = np.random.default_rng(7)
    n = 2000
    idx = pd.date_range('2019-01-01', periods=n, freq='D', tz='UTC')
    base = rng.normal(0.004, 0.01, n)
    solo = rng.normal(0.002, 0.01, n)
    late = np.where(np.arange(n) >= 1500, 0.02, 0.0) + rng.normal(0, 0.01, n)   # TEST-only hero
    rets = [pd.Series(base + rng.normal(0, 0.0003, n), index=idx),   # 0: clone A
            pd.Series(base + rng.normal(0, 0.0003, n), index=idx),   # 1: clone B
            pd.Series(solo, index=idx),                              # 2: independent
            pd.Series(late, index=idx)]                              # 3: earns only in TEST
    kept, chosen, obj, evals = PB.sealed_pick(rets, idx[0], idx[1500], 2, 365, 'combo')
    assert 1 not in kept, 'the twin is dropped by return correlation'
    assert sorted(chosen) == [0, 2] and evals > 0 and obj > 0
    kept2, chosen2, _obj, evals2 = PB.sealed_pick(rets, idx[0], idx[1500], 2, 365, 'test')
    assert chosen2 == kept2 and evals2 == 0, 'a non-combo selection keeps every survivor'


def test_equal_mix_aligns_by_timestamp_and_a_stale_member_counts_flat():
    """Series cached on different days end on different bars; the mix is joined on the
    calendar, and a bar a member has not got is a flat 0 — never a shifted series."""
    idx = pd.date_range('2024-01-01', periods=10, freq='D', tz='UTC')
    fresh = pd.Series(0.01, index=idx)
    stale = pd.Series(0.03, index=idx[:5])               # cached five days earlier
    mix = PB.equal_mix([fresh, stale], idx[0], idx[-1] + pd.Timedelta(days=1))
    assert len(mix) == 10
    assert np.isclose(mix.iloc[0], 0.02) and np.isclose(mix.iloc[-1], 0.005)


def test_champion_metrics_carry_win_rate_and_total_return():
    """What a sealed row can still show: wr (win rate over active bars — the leaderboard's
    own definition) and ret (the segment's total return, the 'PnL' cell)."""
    r = pd.Series([0.01, -0.005, 0.02, 0.0, 0.01, -0.01, 0.015, 0.0])
    m = EV._metrics(r, ann=365)
    assert m['n'] == 6
    assert np.isclose(m['wr'], 4 / 6)
    assert np.isclose(m['ret'], float((1 + r).prod() - 1))

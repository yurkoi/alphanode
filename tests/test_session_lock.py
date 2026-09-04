"""The session contract: once a library is non-empty its search parameters are frozen.

Two rules the user asked for:
  1. no settings changes while the node runs (a GUI lock — exercised in the app, not here);
  2. no restart after changing the parameters a non-empty library was mined under — the
     scores in it were computed under the old rule, and appending new ones corrupts the
     ranking. Changing them needs a fresh node (Clear).

These check rule 2's engine: the contract snapshot, the conflict diff, and the seal.
"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'alphanode'))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'evolution'))
import alphanode_gui as G                                # noqa: E402


class _App:
    """Just enough App for the contract methods — no Tk."""
    CONTRACT_KEYS = G.App.CONTRACT_KEYS
    _contract = G.App._contract
    _contract_load = G.App._contract_load
    _contract_seal = G.App._contract_seal
    _session_conflict = G.App._session_conflict
    _count_lines = G.App._count_lines

    def __init__(self, lib_path, cfg):
        self._lib_path = lib_path
        self.cfg = cfg

    def _tf(self):
        return self.cfg.get('timeframe', '1d')

    def _lib_file(self):
        return self._lib_path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(G, 'SESSION_PARAMS_JSON', str(tmp_path / 'session_params.json'))
    lib = tmp_path / 'library.jsonl'

    def make(cfg=None, alphas=0):
        lib.write_text('\n'.join('{"formula": "x"}' for _ in range(alphas)), encoding='utf-8')
        base = dict(timeframe='1d', universe_list='BTCUSDT,ETHUSDT',
                    train_start='auto', val_start='auto', test_start='auto', test_end='today',
                    target_vol=0.25, exec_cost=0.001, opt_winrate=False, fit_blocks=0)
        base.update(cfg or {})
        return _App(str(lib), base)
    return make


def test_an_empty_library_never_conflicts(env):
    app = env(alphas=0)
    assert app._session_conflict() == ([], None)


def test_a_matching_restart_is_allowed(env):
    app = env(alphas=5)
    app._contract_seal()                                 # session begins, contract recorded
    diffs, snap = app._session_conflict()
    assert diffs == [] and snap is not None


def test_changing_a_frozen_parameter_conflicts(env):
    app = env(alphas=5)
    app._contract_seal()
    app.cfg['universe_list'] = 'BTCUSDT,SOLUSDT'         # user edits after a stop
    app.cfg['target_vol'] = 0.4
    diffs, snap = app._session_conflict()
    labels = {lbl for lbl, _was, _now in diffs}
    assert labels == {'Pairs universe', 'Target volatility'}
    assert snap['universe_list'] == 'BTCUSDT,ETHUSDT', 'snapshot keeps the original for revert'


def test_a_sliding_auto_window_does_not_conflict(env):
    """The date sentinels stay raw in the contract, so 'auto' == 'auto' tomorrow too — the
    window sliding with the calendar is never a conflict."""
    app = env(alphas=5)
    app._contract_seal()
    assert app._contract()['train_start'] == 'auto'
    assert app._session_conflict()[0] == []


def test_an_existing_library_without_a_snapshot_is_grandfathered(env):
    app = env(alphas=5)                                  # library from before this feature
    assert not os.path.exists(G.SESSION_PARAMS_JSON)
    assert app._session_conflict() == ([], None)         # no snapshot -> no block
    app._contract_seal()                                 # seal records it without moving anything
    assert json.load(open(G.SESSION_PARAMS_JSON))['1d']['universe_list'] == 'BTCUSDT,ETHUSDT'


def test_seal_does_not_move_an_existing_contract(env):
    app = env(alphas=5)
    app._contract_seal()
    app.cfg['exec_cost'] = 0.002                         # a change that WOULD conflict
    app._contract_seal()                                 # seal again must not adopt it
    assert json.load(open(G.SESSION_PARAMS_JSON))['1d']['exec_cost'] == '0.001'


def test_each_timeframe_keeps_its_own_contract(env):
    app = env(alphas=5)
    app._contract_seal()                                 # 1d sealed
    app.cfg['timeframe'] = '1h'                          # a different library file in reality —
    # here the same file, but tf keys the snapshot: an unseen tf is fresh, never a conflict
    app.cfg['universe_list'] = 'ETHUSDT'
    diffs, _ = app._session_conflict()
    assert diffs == [], 'a timeframe with no snapshot of its own does not inherit 1d’s'

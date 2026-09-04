"""The combo portfolio mode: search the best COMBINATION of N, honestly.

What these tests guard:
  * the objective prefers diversification: a clone pair loses to an uncorrelated mix
    even when the clones' individual Sharpes are higher;
  * greedy + swap finds the brute-force optimum on small pools (deterministic seed);
  * the search sees ONLY the selection span: an alpha that shines exclusively inside
    TEST must not be chosen (selection_matrix stops at TEST start);
  * plumbing: --select combo is a real CLI mode; the GUI maps the dropdown to it.
"""
import itertools
import json
import os
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'alphanode'))
import portfolio_build as PB


def _sharpe(x, ann=365):
    return float(x.mean() / x.std() * np.sqrt(ann))


def test_combo_prefers_diversification_over_clones():
    rng = np.random.default_rng(7)
    base = rng.normal(0.004, 0.01, 2000)                 # a strong common signal
    solo = rng.normal(0.002, 0.01, 2000)                 # weaker but independent
    R = np.column_stack([
        base + rng.normal(0, 0.001, 2000),               # 0: clone A (highest solo Sharpe)
        base + rng.normal(0, 0.001, 2000),               # 1: clone B (2nd highest)
        solo,                                            # 2: independent, weaker alone
    ])
    idx, obj, _ = PB.choose_combo(R, 2)
    assert 2 in idx                                      # the mix wants the uncorrelated one
    assert len(set(idx) & {0, 1}) == 1                   # and exactly one clone
    assert obj > _sharpe(R[:, [0, 1]].mean(axis=1))      # clones-only mix is strictly worse


def test_combo_matches_brute_force_on_a_small_pool():
    rng = np.random.default_rng(21)
    R = rng.normal(0.001, 0.01, (1500, 8)) + rng.normal(0.002, 0.008, (1500, 1))
    idx, obj, evals = PB.choose_combo(R, 3)
    best = max((PB._mix_sharpe(R, list(c), 365), sorted(c))
               for c in itertools.combinations(range(8), 3))
    assert obj == pytest.approx(best[0], abs=1e-9)
    assert idx == best[1]
    assert evals < 200                                   # hundreds, not C(8,3)*sims


def test_selection_matrix_stops_at_test_start():
    days = pd.date_range('2020-01-01', periods=300, freq='D', tz='UTC')
    lo, ts = days[0], days[200]                          # TEST = the last 100 days
    flat = pd.Series(0.0001, index=days)
    test_hero = pd.Series(np.r_[np.full(200, -0.002),    # awful before TEST…
                                np.full(100, 0.02)], index=days)   # …a rocket inside it
    honest = pd.Series(np.r_[np.full(200, 0.002),
                             np.full(100, -0.001)], index=days)
    R = PB.selection_matrix([flat, test_hero, honest], lo, ts)
    assert R.shape == (200, 3)                           # TEST rows never entered
    idx, _, _ = PB.choose_combo(R, 2)
    assert 1 not in idx                                  # the TEST-only rocket is invisible
    assert 2 in idx


def test_choose_combo_edges():
    R = np.random.default_rng(3).normal(0.001, 0.01, (500, 4))
    idx, _, _ = PB.choose_combo(R, 99)                   # k beyond pool -> whole pool
    assert idx == [0, 1, 2, 3]
    idx, _, _ = PB.choose_combo(R, 1)                    # k=1 -> the best single
    solos = [PB._mix_sharpe(R, [j], 365) for j in range(4)]
    assert idx == [int(np.argmax(solos))]
    dead = np.zeros((500, 3))                            # zero-variance members don't crash
    idx, obj, _ = PB.choose_combo(dead, 2)
    assert len(idx) == 2 and obj == -1e18


def test_cli_knows_the_combo_mode():
    out = subprocess.run([sys.executable,
                          os.path.join(os.path.dirname(PB.__file__), 'portfolio_build.py'),
                          '--help'], capture_output=True, text=True, timeout=60)
    assert 'combo' in out.stdout and '--pool' in out.stdout


@pytest.mark.gui
def test_gui_dropdown_maps_combo_to_the_cli(gui_app, monkeypatch):
    app, rec, state = gui_app
    import alphanode_gui as G
    calls = {}

    import io as _io

    class FakeProc:
        stdout = _io.StringIO('')
        returncode = 0
        def poll(self):
            return 0
        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kw):
        calls['cmd'] = cmd
        return FakeProc()

    monkeypatch.setattr(G.subprocess, 'Popen', fake_popen)
    app.v_pfsel.set('combo')
    app._build_portfolio()
    cmd = calls['cmd']
    i = cmd.index('--select')
    assert cmd[i + 1] == 'combo'


def test_auto_size_picks_the_best_size_not_a_fixed_quota():
    """--top 0: the size itself is searched (user request — 'not always top 6'). Two
    complementary alphas mix into a near-riskless book; padding it with noise members to
    any quota only dilutes it, so auto must stop at the pair."""
    rng = np.random.default_rng(7)
    base = rng.normal(0.0, 0.01, 400)
    a = 0.004 + base + rng.normal(0.0, 0.001, 400)
    b = 0.004 - base + rng.normal(0.0, 0.001, 400)
    noise = rng.normal(0.0, 0.01, (400, 6))
    R = np.column_stack([a, b, noise])
    idx, obj, _ = PB.choose_combo(R, 0)
    assert idx == [0, 1]
    assert obj > PB._mix_sharpe(R, list(range(8)), 365), 'beats the take-everything mix'


def test_auto_size_is_capped_at_ten_and_floored_at_two():
    """The owner's ceiling: never more than 10 members, however good the mix looks.
    25 iid positive columns love dilution — without the cap the search would take ~all."""
    rng = np.random.default_rng(3)
    R = rng.normal(0.0005, 0.01, (300, 25))
    idx, _, _ = PB.choose_combo(R, 0)
    assert 2 <= len(idx) <= PB.AUTO_MAX == 10

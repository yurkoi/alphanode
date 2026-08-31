"""round_eta.RoundEta — the per-round remaining-time model (see the module docstring)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'alphanode'))
from round_eta import RoundEta, POLISH_EVALS  # noqa: E402


def _synthetic(pop=200, gens=25, spe=0.2, delta=0.3, init=12.0, polish_evals=POLISH_EVALS):
    """A round where each generation scores pop*(1 - delta*g/gens) NEW formulas at `spe` s each,
    gen 0 additionally pays `init` seconds of worker start-up; polish costs polish_evals."""
    t, ev, lines = 0.0, 0, []
    for g in range(gens):
        new = int(round(pop * (1 - delta * g / gens)))
        t += new * spe + (init if g == 0 else 0)
        ev += new
        lines.append((t, f'gen {g:2d} | evaluated {ev:5d} (uniq {ev:5d}) | valid 190/{pop} | best fit +0.5'))
    total = t + polish_evals * spe
    return lines, total


def test_no_estimate_before_second_generation():
    r = RoundEta('exploring new', 200, 25, 0.0)
    assert r.eta(5.0) is None
    r.feed(50.0, 'gen  0 | evaluated   200 (uniq   200) | valid 190/200')
    assert r.eta(50.0) is None                       # gen 0 carries the start-up: not a rate yet


def test_tracks_a_decaying_round_within_a_few_percent():
    lines, total = _synthetic()
    r = RoundEta('exploring new', 200, 25, 0.0)
    worst = 0.0
    for t, m in lines[:-1]:
        r.feed(t, m)
        eta = r.eta(t)
        if eta is None:
            continue
        worst = max(worst, abs(eta - (total - t)))
    assert worst < 0.12 * total                      # prior-vs-truth mismatch early in the round
    t, m = lines[-3]
    assert abs(r.eta(t) - (total - t)) < 0.03 * total   # late in the round the learned slope rules


def test_learned_slope_matches_the_synthetic_decay():
    lines, _ = _synthetic(delta=0.4)
    r = RoundEta('explore', 200, 25, 0.0)
    for t, m in lines:
        r.feed(t, m)
    assert abs(r.learned_delta() - 0.4) < 0.03
    assert abs(r.eta(lines[-1][0]) - POLISH_EVALS * 0.2) < 1e-6   # last gen done -> only polish left
    assert r.eta(lines[-1][0] + 1e9) == 0.0


def test_bloat_trend_raises_the_estimate():
    """Same round, but each generation costs 2% more per evaluation than the previous one."""
    t, ev, lines = 0.0, 0, []
    for g in range(25):
        t += 200 * 0.2 * (1.02 ** g)
        ev += 200
        lines.append((t, f'gen {g:2d} | evaluated {ev:5d} (uniq {ev:5d}) | valid 190/200'))
    flat = RoundEta('refine', 200, 25, 0.0, spe_trend=False)
    trend = RoundEta('refine', 200, 25, 0.0, spe_trend=True)
    for tt, m in lines[:10]:
        flat.feed(tt, m)
        trend.feed(tt, m)
    tt = lines[9][0]
    truth = lines[-1][0] - tt
    assert trend.eta(tt) > flat.eta(tt)
    assert abs(trend.eta(tt) - truth) < abs(flat.eta(tt) - truth)


def test_mode_prior_and_persisted_prior():
    assert RoundEta('refining best', 200, 25, 0.0).d0 < RoundEta('exploring new', 200, 25, 0.0).d0
    assert RoundEta('refining best', 200, 25, 0.0, {'refine': 0.05}).d0 == 0.05


def test_progress_is_monotone_and_ends_at_one():
    lines, total = _synthetic()
    r = RoundEta('explore', 200, 25, 0.0)
    prev = 0.0
    for t, m in lines:
        r.feed(t, m)
        p = r.progress(t)
        assert 0.0 <= p <= 1.0 and p >= prev - 1e-9
        prev = p
    r.done = True
    assert r.progress(total) == 1.0


def test_mid_generation_countdown_does_not_jump():
    lines, total = _synthetic()
    r = RoundEta('explore', 200, 25, 0.0)
    for t, m in lines[:6]:
        r.feed(t, m)
    t0 = lines[5][0]
    a = r.eta(t0)
    b = r.eta(t0 + 10.0)
    assert 9.0 <= a - b <= 11.0                      # ten seconds later the ETA is ten seconds shorter

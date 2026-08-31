"""Remaining-time estimate for ONE search round (evolve() call).

Why not `elapsed / progress` with progress = evaluated / (pop * gens)? Measured on real rounds
(pop 200, gens 25, 2 workers) that formula overestimates the whole way and never converges,
because `pop * gens` is a ceiling nobody reaches: the engine skips formulas it has already
scored, and in an explore round the share of NEW formulas per generation may fall ~linearly
(200 -> ~130 by the last generation) — or not at all; refine rounds barely decay. The one
thing that IS stable is the cost of a single evaluation (sec/eval; +-5% across a round, in
some rounds a smooth climb of 25..65% as the formulas grow).

So the model is:  ETA = rate * (predicted remaining evaluations) + polish_evals * sec_per_eval

* sec_per_eval  — EMA over generations of  gen_time / new_formulas_in_gen  (gen 0 is skipped:
                  it carries the worker start-up).
* rate          — sec_per_eval drifted along a linear trend fitted on the last generations
                  (bloat makes late generations dearer); only upwards, capped.
* remaining     — sum over the remaining generations of  pop * (a - delta * g / gens): a straight
                  line for the "new share", anchored at the last observation, with slope `delta`
                  learned from this round once >= 3 generations are in, blended with a per-mode
                  prior taken from the node's previous rounds (defaults: explore 0.30, refine 0.10).
* polish_evals  — the window fine-tune after the last generation: 40..230 evaluations measured
                  (the engine logs nothing for unimproved champions), 120 is the compromise.

Replayed against 6 recorded rounds (243..1195 s), sampled every 5 s: mean abs error 2..9% of
the round's length (mean 5.7%; the naive formula: 30..50%, and it never converges). Round-to-
round history is NOT used: two explore rounds with an identical config took 776 s and 1195 s.
"""
import re

GEN_RE = re.compile(r'gen\s+(\d+)\s+\|\s+evaluated\s+(\d+)')
DELTA_PRIOR = {'explore': 0.30, 'refine': 0.10}
POLISH_EVALS = 120
EMA_ALPHA = 0.5          # sec/eval smoothing; 0.5 = a half-life of one generation
WARM_GENS = 8            # after this many observed gens the learned delta fully replaces the prior
SPE_TREND = True         # extrapolate the sec/eval drift (formula bloat) over the remaining gens
SPE_TREND_K = 8          # ... fitted on the last K generations
SPE_TREND_CAP = 0.5      # ... never more than +50% over the current rate


def _linfit(pts):
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    b = sum((x - mx) * (y - my) for x, y in pts) / max(1e-9, sum((x - mx) ** 2 for x, _ in pts))
    return my - b * mx, b


class RoundEta:
    """Feed every engine log line through `feed(now, line)`; read `eta()` / `progress()`.

    `priors` is a dict {mode: delta} the caller may persist across rounds (see `learned_delta`)."""

    def __init__(self, mode, pop, gens, t0, priors=None, *, delta_prior=None, warm=WARM_GENS,
                 polish_evals=POLISH_EVALS, spe_trend=SPE_TREND, alpha=EMA_ALPHA):
        self.mode = 'refine' if str(mode).startswith('refin') else 'explore'
        self.pop, self.gens, self.t0 = int(pop), int(gens), float(t0)
        self.d0 = (priors or {}).get(self.mode, (delta_prior or DELTA_PRIOR)[self.mode])
        self.warm, self.K, self.trend, self.alpha = warm, polish_evals, spe_trend, alpha
        self.g = -1               # last finished generation
        self.ev = 0               # engine's cumulative `evaluated`
        self.t_last = t0          # time of the last generation line
        self.spe = None           # sec per evaluation (EMA)
        self.obs = []             # (g/gens, new/pop) per generation >= 1
        self.rates = []           # (gen, sec/eval) per generation >= 1
        self.evo_end = None       # time the last generation finished (polish phase from here)
        self.done = False

    # ---- input ----
    def feed(self, now, line):
        m = GEN_RE.search(line)
        if not m:
            return
        gi, ev = int(m.group(1)), int(m.group(2))
        new, dt = ev - self.ev, now - self.t_last
        if gi >= 1 and new > 0:
            r = dt / new
            self.spe = r if self.spe is None else self.alpha * r + (1 - self.alpha) * self.spe
            self.obs.append((gi / self.gens, new / self.pop))
            self.rates.append((gi, r))
        self.g, self.ev, self.t_last = gi, ev, now
        if gi >= self.gens - 1:
            self.evo_end = now

    # ---- model ----
    def delta(self):
        if len(self.obs) < 3:
            return self.d0
        _a, b = _linfit(self.obs)
        d = min(0.95, max(0.0, -b))
        w = min(1.0, len(self.obs) / self.warm)
        return w * d + (1 - w) * self.d0

    def rate_for_remaining(self):
        """sec/eval to charge the remaining generations: the EMA, drifted along the fitted
        per-generation trend to the midpoint of what is left (bloat makes late gens dearer)."""
        if not self.trend or len(self.rates) < 4 or self.g >= self.gens - 1:
            return self.spe
        _a, b = _linfit(self.rates[-SPE_TREND_K:])
        mid = (self.gens - 1 - self.g) / 2.0
        return self.spe * min(1.0 + SPE_TREND_CAP, max(1.0, 1.0 + max(0.0, b) * mid / self.spe))

    def learned_delta(self):
        """The slope this round actually showed (None until enough generations) — persist it
        as the prior for the next round of the same mode."""
        if len(self.obs) < self.warm:
            return None
        _a, b = _linfit(self.obs)
        return min(0.95, max(0.0, -b))

    def remaining_evals(self):
        if self.evo_end is not None:
            return 0.0
        d = self.delta()
        a0 = (self.obs[-1][1] + d * self.obs[-1][0]) if self.obs else 1.0
        return sum(self.pop * max(0.05, a0 - d * gg / self.gens) for gg in range(self.g + 1, self.gens))

    # ---- output ----
    def eta(self, now):
        """Seconds left, or None while there is nothing to go on (before the 2nd generation)."""
        if self.done:
            return 0.0
        if self.spe is None:
            return None
        polish = self.K * self.spe
        if self.evo_end is not None:
            return max(0.0, polish - (now - self.evo_end))
        # inside a generation: the time already spent on it is work done, not extra wait
        in_gen = min(now - self.t_last, self.spe * self.pop)
        return max(0.0, self.rate_for_remaining() * self.remaining_evals() + polish - in_gen)

    def progress(self, now):
        """0..1 share of the round's work, by time: elapsed / (elapsed + eta)."""
        e = self.eta(now)
        if e is None:
            return 0.0
        el = max(0.0, now - self.t0)
        return 1.0 if self.done else el / max(1e-9, el + e)

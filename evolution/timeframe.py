"""Timeframe abstraction: everything that depends on the bar size (15m … 1d), in one place.

The engine (build_panel / precompute_market / fast_sim / metrics) is otherwise timeframe-agnostic,
so daily behaviour is reproduced EXACTLY by DAILY, and any intraday bar size is just a different
Timeframe resolved from config. Nothing else in the engine hard-codes "a day".

Derived quantities:
  * periods_per_year = (86400 / seconds) * 365   -> Sharpe = mean/std * sqrt(periods_per_year)
    (crypto trades 24/7, hence 365, not 252 trading days)
  * pandas_freq       -> the calendar grid build_panel reindexes onto
  * binance_interval  -> what fetch_data / the live paper loop request
  * vol_window        -> bars for the rolling realized-vol estimate (a knob; ~30 days of bars)
  * ewma_lambda       -> vol-target EWMA decay PER BAR (retune per tf so the wall-clock half-life
                         stays comparable; kept at the daily value for now)
"""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

DAY_SECONDS = 86400
ANN_DAYS = 365                     # 24/7 market -> 365 calendar days, not 252 trading days


@dataclass(frozen=True)
class Timeframe:
    name: str
    binance_interval: str          # fetch_data + live paper klines
    pandas_freq: str               # build_panel reindex grid
    seconds: int
    vol_window: int                # bars for rolling vol estimation
    ewma_lambda: float = 0.06      # vol-target EWMA decay per bar
    # --- recommended limits per bar size (finer bars: shorter history, fewer pairs) ---
    history: str = '2019-09-05'    # default fetch/search start (also TRAIN start)
    val_start: str = '2021-11-01'  # TRAIN | VAL boundary
    test_start: str = '2023-01-01'  # VAL | TEST boundary
    test_end: str = 'today'        # end of TEST (held-out); 'today' = the current UTC date
    max_pairs: int = 150           # sane default universe size for downloads at this bar size
    max_bars: int = 3000           # ceiling on TRAIN start -> TEST end, in bars (see check_segments)

    @property
    def periods_per_day(self):
        return DAY_SECONDS / self.seconds

    @property
    def periods_per_year(self):
        return self.periods_per_day * ANN_DAYS

    @property
    def max_span_days(self):
        """The max_bars ceiling expressed in calendar days — what the user actually types."""
        return self.max_bars / self.periods_per_day

    def bars(self, a, b):
        """How many bars of this size fit in [a, b) — the height of the panel that span builds.
        `a`/`b` are ISO strings or datetimes; a backwards span counts as zero, not negative."""
        a = a if isinstance(a, datetime) else datetime.fromisoformat(end_date(a))
        b = b if isinstance(b, datetime) else datetime.fromisoformat(end_date(b))
        #                                    end_date: literals pass through, 'today' resolves
        return max(0, int((b - a).total_seconds() // self.seconds))

    @property
    def segments(self):
        """The TRAIN/VAL/TEST window measured BACK from today (user spec): every bar size
        takes the same SHAPE — TRAIN 50% / VAL 20% / TEST 30% of a budget of 80% of its
        max_bars — so a freshly picked timeframe studies the most recent history it can
        afford at any calendar date. The pinned val_start/test_start/test_end fields are
        legacy anchors kept for reference; this property no longer reads them."""
        total = int(self.max_bars * 0.8 * self.seconds / 86400)
        today = datetime.now(timezone.utc)

        def back(days):
            return (today - timedelta(days=days)).strftime('%Y-%m-%d')

        return {'train_start': back(total), 'val_start': back(int(total * 0.5)),
                'test_start': back(int(total * 0.3)), 'test_end': 'today'}


# Registry. vol_window ≈ 30 days of bars (keeps the "monthly vol" meaning of the daily rolling(30)).
# The history/segment windows shrink as bars get finer: intraday history on Binance perps is shorter,
# far denser, and heavier to download, so we trade calendar span for a comparable bar count and a
# reasonable download. Each window still spans several market regimes. Tune to taste in config.ini /
# the GUI date fields — these are only the defaults the timeframe selector fills in.
_TF = {
    '1d':  Timeframe('1d',  '1d',  'D',     86400,   30,
                     history='2019-09-05', val_start='2021-11-01', test_start='2023-01-01',
                     test_end='today', max_pairs=150, max_bars=3_000),   # 1d fits max_bars
    #                                 open-ended; intraday keeps PINNED ends — 'today' would
    #                                 blow their max_bars window as the calendar advances
    '4h':  Timeframe('4h',  '4h',  '4h',    14400,  180,          # 30d * 6/day
                     history='2020-06-01', val_start='2023-01-01', test_start='2024-09-01',
                     test_end='2026-07-01', max_pairs=100, max_bars=16_000),
    '1h':  Timeframe('1h',  '1h',  'h',      3600,  720,          # 30d * 24/day
                     history='2022-06-01', val_start='2024-06-01', test_start='2025-06-01',
                     test_end='2026-07-01', max_pairs=60, max_bars=40_000),
    '15m': Timeframe('15m', '15m', '15min',   900, 2880,
                     history='2024-01-01', val_start='2025-04-01', test_start='2025-12-01',
                     test_end='2026-07-01', max_pairs=40, max_bars=96_000),
}
# 5m was removed from the product (2026-07-26): bars are too heavy to download and simulate,
# and at that scale unmodeled microstructure (slippage, queue position) dominates the signal.
# The engine itself stays bar-size-agnostic — re-adding an entry here is all it would take.

DAILY = _TF['1d']


def resolve(name):
    """Timeframe by short name ('15m','1h','4h','1d'); default/blank -> daily."""
    key = (name or '1d').strip().lower()
    if key not in _TF:
        raise ValueError(f'unknown timeframe {name!r}; known: {list(_TF)}')
    return _TF[key]


def known():
    return list(_TF)


# --- segment validation --------------------------------------------------------------
# Two rules, both of which used to be nobody's job:
#
#   ORDER   train_start < val_start < test_start < test_end, strictly. A reversed pair does
#           not error anywhere downstream — build_panel just returns an empty or backwards
#           slice, the search reports a Sharpe computed on nothing, and the leaderboard fills
#           with numbers that describe no data at all.
#
#   SIZE    the whole span, TRAIN start -> TEST end, must fit in max_bars for the bar size.
#           The panel is [bars x pairs] float64 held in RAM and re-read by every candidate, so
#           the cost of a span is linear in bars and the finer the bar the faster it runs away:
#           the same 4 calendar years is 1,460 daily bars and 140,000 15-minute ones. The caps
#           sit ~15% above each timeframe's own recommended window (the dates the selector
#           fills in), which is the span this build is tuned and tested for.
#
# MIN_BARS is the same evidence floor metrics_worker.regime_sharpe uses: under 30 bars a
# Sharpe is noise with a decimal point, so a segment that short is not a segment.
MIN_BARS = 30


def end_date(s):
    """A TEST-end value: a literal date, or the sentinel '' / 'today' / 'auto' meaning the
    CURRENT UTC date — so the freshest downloaded bar always lands inside TEST (user request:
    the end must follow today, not the stamp from whenever the segments were typed)."""
    raw = str(s or '').strip()
    if raw.lower() in ('', 'today', 'auto'):
        return datetime.now(timezone.utc).strftime('%Y-%m-%d')
    return raw


def seg_value(tf, field, v):
    """One date box resolved: '' / 'auto' -> this timeframe's computed-from-today default
    for that field (see Timeframe.segments); 'today' in the END box -> the current UTC
    date; a literal date passes through untouched."""
    t = tf if isinstance(tf, Timeframe) else resolve(tf)
    raw = str(v or '').strip().lower()
    if raw in ('', 'auto'):
        v = t.segments[field]
    if field == 'test_end':
        return end_date(v)
    return str(v).strip()


def check_segments(tf, train_start, val_start, test_start, test_end):
    """[] if the four dates are a usable TRAIN/VAL/TEST window for this bar size, otherwise a
    list of (field, problem) pairs, worst first. `field` is the label of the box the user should
    edit ('TRAIN start' …) so a caller can point at it; `problem` is plain language.
    `tf` is a Timeframe or its short name.

    Pure and stdlib-only on purpose: the GUI calls it on every keystroke to paint the note under
    the date fields, and Start calls it again as the hard gate."""
    t = tf if isinstance(tf, Timeframe) else resolve(tf)
    train_start = seg_value(t, 'train_start', train_start)   # sentinels ('auto'/'today') are
    val_start = seg_value(t, 'val_start', val_start)         # real dates from here on
    test_start = seg_value(t, 'test_start', test_start)
    test_end = seg_value(t, 'test_end', test_end)
    fields = (('TRAIN start', train_start), ('VAL start', val_start),
              ('TEST start', test_start), ('TEST end', test_end))
    bad, when = [], {}
    for label, raw in fields:
        try:
            when[label] = datetime.fromisoformat(str(raw).strip())
        except (ValueError, TypeError):
            bad.append((label, f'{label} is not a date — write it as YYYY-MM-DD '
                               f'(e.g. {t.history}).'))
    if bad:
        return bad                                   # nothing else can be said until they parse

    out = []
    for (a_lbl, _), (b_lbl, _) in zip(fields, fields[1:]):
        if when[b_lbl] <= when[a_lbl]:
            out.append((b_lbl, f'{b_lbl} must be later than {a_lbl} — '
                               f'the order is TRAIN < VAL < TEST < end.'))
    if out:
        return out                                   # sizes are meaningless on a broken order

    for a_lbl, b_lbl, seg in (('TRAIN start', 'VAL start', 'TRAIN'),
                              ('VAL start', 'TEST start', 'VAL'),
                              ('TEST start', 'TEST end', 'TEST')):
        n = t.bars(when[a_lbl], when[b_lbl])
        if n < MIN_BARS:
            out.append((b_lbl, f'{seg} is only {n} {t.name} bars — under {MIN_BARS} there is '
                               f'nothing to measure. Widen {a_lbl} → {b_lbl}.'))
    total = t.bars(when['TRAIN start'], when['TEST end'])
    if total > t.max_bars:
        over = (total - t.max_bars) / t.periods_per_day
        out.append(('TRAIN start',
                    f'{total:,} {t.name} bars from TRAIN start to TEST end — the limit for '
                    f'{t.name} is {t.max_bars:,} (about {t.max_span_days / 365.0:.1f} years). '
                    f'Move TRAIN start {over:.0f} days later, or pick a coarser bar size.'))
    return out

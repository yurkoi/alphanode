"""The four date boxes are checked, not trusted.

Two rules the app never enforced:
  ORDER  train_start < val_start < test_start < test_end. A reversed pair does not raise
         anywhere — build_panel hands back an empty slice and the search reports a Sharpe
         computed on no data at all.
  SIZE   the whole span must fit the bar size's max_bars. Four calendar years is 1,460 daily
         bars and ~140,000 15-minute ones; the panel is [bars x pairs] float64 re-read by every
         candidate, so the same dates that are cheap on 1d are unrunnable on 15m.
"""
import sys, os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'evolution'))
from timeframe import MIN_BARS, check_segments, known, resolve   # noqa: E402

OK = ('2022-06-01', '2024-06-01', '2025-06-01', '2026-07-01')    # the 1h defaults


def _msgs(probs):
    return ' '.join(m for _f, m in probs)


# ---------- the shipped defaults must pass, on every bar size ----------

def test_every_timeframes_own_defaults_are_valid():
    """The selector fills these in — a cap that rejects them would be a cap set wrong."""
    for name in known():
        t = resolve(name)
        s = t.segments
        probs = check_segments(name, s['train_start'], s['val_start'],
                               s['test_start'], s['test_end'])
        assert probs == [], f'{name}: {probs}'
        used = t.bars(s['train_start'], s['test_end'])
        assert used <= t.max_bars
        assert used > t.max_bars * 0.7, f'{name}: cap {t.max_bars} is loose to the point of' \
                                        f' meaningless against a {used}-bar default'


# ---------- ordering ----------

@pytest.mark.parametrize('idx,field', [(1, 'VAL start'), (2, 'TEST start'), (3, 'TEST end')])
def test_each_date_must_be_later_than_the_one_before(idx, field):
    d = list(OK)
    d[idx] = '2021-01-01'                                # earlier than everything before it
    probs = check_segments('1h', *d)
    assert probs and probs[0][0] == field                # points at the box to edit
    assert 'later than' in probs[0][1]


def test_equal_dates_are_rejected_too():
    """A zero-length segment is not 'in order' — TEST from X to X holds no bars."""
    d = list(OK)
    d[3] = d[2]
    probs = check_segments('1h', *d)
    assert probs and probs[0][0] == 'TEST end'


def test_a_broken_order_reports_order_only():
    """Bar counts on a backwards span are noise — don't bury the real problem under them."""
    probs = check_segments('1h', '2026-01-01', '2020-01-01', '2020-02-01', '2020-03-01')
    assert all('later than' in m for _f, m in probs)


# ---------- the per-timeframe ceiling ----------

def test_the_same_dates_pass_on_1d_and_fail_on_15m():
    """The whole point of a per-timeframe cap: calendar span is not the cost, bars are."""
    span = ('2020-01-01', '2022-01-01', '2023-01-01', '2024-01-01')
    assert check_segments('1d', *span) == []
    probs = check_segments('15m', *span)
    assert probs and probs[0][0] == 'TRAIN start'        # the field that shortens the window
    assert '15m' in probs[0][1] and 'limit' in probs[0][1]


def test_the_ceiling_names_the_number_of_days_to_move():
    t = resolve('1h')
    probs = check_segments('1h', '2020-01-01', '2024-06-01', '2025-06-01', '2026-07-01')
    assert len(probs) == 1
    over = t.bars('2020-01-01', '2026-07-01') - t.max_bars
    assert f'{over / t.periods_per_day:.0f} days later' in probs[0][1]


def test_exactly_at_the_ceiling_passes():
    """A cap is <=, not <: the boundary itself must be usable."""
    t = resolve('1h')
    from datetime import datetime, timedelta
    end = datetime.fromisoformat('2026-07-01')
    start = end - timedelta(seconds=t.max_bars * t.seconds)
    d = (start.isoformat(), '2025-01-01', '2026-01-01', '2026-07-01')
    assert t.bars(d[0], d[3]) == t.max_bars              # exactly on the line
    assert check_segments('1h', *d) == []


# ---------- unparseable input ----------

def test_a_typo_is_caught_here_instead_of_killing_the_node():
    """load_config does datetime.fromisoformat with no guard — the node dies before it can
    open a window to say why. This is the only place that can still tell the user."""
    probs = check_segments('1h', '01/06/2022', *OK[1:])
    assert probs == [('TRAIN start', probs[0][1])]
    assert 'YYYY-MM-DD' in probs[0][1]


def test_an_unparseable_date_suppresses_the_rest():
    # NB: '' is no longer garbage — an empty box means 'auto' (the sliding window)
    probs = check_segments('1h', 'yesterday', 'x', 'soon', '2026-07-01')
    assert len(probs) == 3                               # one per bad box, nothing else
    assert all('YYYY-MM-DD' in m for _f, m in probs)


# ---------- the evidence floor ----------

def test_a_segment_under_the_evidence_floor_is_rejected():
    probs = check_segments('1d', '2019-09-05', '2019-09-10', '2023-01-01', '2026-07-05')
    assert probs and probs[0][0] == 'VAL start'
    assert f'under {MIN_BARS}' in probs[0][1]


def test_thirty_bars_is_enough():
    t = resolve('1d')
    assert t.bars('2019-09-05', '2019-10-05') == MIN_BARS
    assert check_segments('1d', '2019-09-05', '2019-10-05', '2023-01-01', '2026-07-05') == []


# ---------- Timeframe.bars ----------

def test_bars_counts_the_grid_not_the_calendar():
    assert resolve('1d').bars('2024-01-01', '2024-01-31') == 30
    assert resolve('1h').bars('2024-01-01', '2024-01-02') == 24
    assert resolve('15m').bars('2024-01-01', '2024-01-02') == 96
    assert resolve('4h').bars('2024-01-02', '2024-01-01') == 0     # backwards, not negative


# ---------- the GUI side: a live note, a reddened box, and a Start that refuses ----------

@pytest.mark.gui
def test_the_note_is_hidden_while_the_dates_are_fine(gui_app):
    app, _rec, _state = gui_app
    assert app._seg_check() == []
    # auto mode: the boxes show REAL dates and the note names the mode…
    assert app.lbl_seg.cget('text').startswith('auto window')
    from timeframe import seg_value
    assert app.v_train.get() == seg_value('1h', 'train_start', 'auto'), \
        'the box shows the resolved date, not the word auto'
    for v, d in ((app.v_train, '2023-03-01'), (app.v_val, '2024-10-01'),
                 (app.v_test, '2025-06-01'), (app.v_end, '2026-08-01')):
        v.set(d)                                         # …four pinned literal dates
    app.root.update_idletasks()
    assert app._seg_check() == []
    assert app.lbl_seg.grid_info() == {}                 # need no note: grid_remove


@pytest.mark.gui
def test_typing_a_backwards_date_shows_the_note_and_reddens_the_box(gui_app):
    app, _rec, _state = gui_app
    import alphanode_gui as G
    app.v_val.set('2000-01-01')                          # the trace fires _seg_check
    app.root.update_idletasks()
    assert 'later than' in app.lbl_seg.cget('text')
    assert app.lbl_seg.grid_info() != {}                 # back on the grid, under the fields
    assert app.v_val.widget.cget('border_color') == G.NEG
    assert app.v_test.widget.cget('border_color') == G.BORDER    # only the culprit
    app.v_val.set(app.cfg['val_start'])                  # …and it clears again: sentinel boxes
    app.root.update_idletasks()                          # show the resolved sliding window now,
    assert app.lbl_seg.cget('text').startswith('auto window')     # not an empty gap
    assert app.v_val.widget.cget('border_color') == G.BORDER


@pytest.mark.gui
def test_start_refuses_a_span_the_bar_size_cannot_carry(gui_app):
    app, _rec, _state = gui_app
    app.v_tf.set('15m')
    app._on_tf_change()                                  # fills the 15m defaults
    app.root.update_idletasks()
    assert app._seg_check() == []
    app.v_train.set('2019-01-01')                        # ~250k 15m bars
    app.root.update_idletasks()
    app.start()
    assert app.proc is None                              # no node was launched
    kind, title, msg = _rec.calls[-1]
    assert kind == 'showerror' and title == 'Date segments'
    assert 'limit for 15m' in msg


@pytest.mark.gui
def test_the_timeframe_note_states_the_ceiling(gui_app):
    app, _rec, _state = gui_app
    for tf in ('1d', '1h', '15m'):
        app.v_tf.set(tf)
        app._tf_note()
        assert f'window ≤ {resolve(tf).max_bars:,} bars' in app.lbl_tf_note.cget('text')


@pytest.mark.gui
def test_untouched_daily_dates_follow_the_saved_timeframe(gui_app):
    """The DEFAULTS ship the 1d window. A settings file that says '1h' but never passed
    through the timeframe selector used to boot with 59,880 bars of 1h — past every limit."""
    app, _rec, _state = gui_app
    assert app.cfg['timeframe'] == '1h'                  # what conftest wrote
    assert app.cfg['train_start'] == 'auto'              # sentinels since the sliding window:
    from timeframe import seg_value                      # they resolve against the SAVED tf
    assert seg_value('1h', 'train_start', 'auto') == resolve('1h').segments['train_start']
    assert app._seg_check() == []


@pytest.mark.gui
def test_a_hand_typed_window_is_never_rewritten(gui_app):
    """Only verbatim defaults get snapped — anything the user chose survives the reload,
    and the note argues with it instead of silently replacing it."""
    import json
    import alphanode_gui as G
    app, _rec, _state = gui_app
    mine = {'timeframe': '1h', 'eula_accepted': '1.0.0', 'train_start': '2023-03-15',
            'val_start': '2024-06-01', 'test_start': '2025-06-01', 'test_end': '2026-07-01'}
    open(G.SETTINGS, 'w').write(json.dumps(mine))
    app.cfg = dict(G.DEFAULTS)
    app._load()
    assert app.cfg['train_start'] == '2023-03-15'


# ---------- the 'today' sentinel: the end follows the calendar ----------

def test_today_sentinel_resolves_to_the_current_utc_date():
    from datetime import datetime, timezone
    from timeframe import end_date
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    assert end_date('today') == end_date('') == end_date(' TODAY ') == now
    assert end_date('2026-08-30') == '2026-08-30', 'a literal date stays pinned'


def test_check_segments_accepts_today_as_the_end():
    """The user request: TEST end must follow the current date — the validator treats the
    sentinel as a real date, so the shipped 1d defaults (test_end='today') stay valid."""
    assert check_segments('1d', '2020-06-01', '2023-01-01', '2024-09-01', 'today') == []
    probs = check_segments('1d', '2020-06-01', '2023-01-01', 'today', '2024-09-01')
    assert probs, "the sentinel is only for the END box — 'today' elsewhere is not a date"


def test_auto_boxes_pull_the_same_shaped_window_back_from_today():
    """User spec: every bar size (15m/1h/4h/1d) fills its segments as the SAME-shaped
    window measured back from the current date — TRAIN 50% / VAL 20% / TEST 30% of 80%
    of its max_bars — instead of pinned dates that go stale."""
    from datetime import datetime, timezone
    from timeframe import seg_value
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    for name in known():
        t = resolve(name)
        seg = t.segments
        assert seg_value(name, 'train_start', 'auto') == seg['train_start']
        assert seg_value(name, 'test_end', 'auto') == today
        assert check_segments(name, 'auto', 'auto', 'auto', 'today') == [], name
        used = t.bars(seg['train_start'], today)
        assert used <= t.max_bars * 0.85, f'{name}: auto window must leave cap headroom'
        assert seg['train_start'] < seg['val_start'] < seg['test_start'] < today

"""The simple screen's state machine, exercised headless: fake root, fake labels, a status
file on disk — no Tk, no window, no subprocess."""
import json

import pytest

import alphanode_gui as G


class _Lbl:
    def __init__(self):
        self.text, self.fg = '', None

    def winfo_exists(self):
        return True

    def configure(self, text=None, fg=None, state=None):
        if text is not None:
            self.text = text
        if fg is not None:
            self.fg = fg
        if state is not None:
            self.state = state


class _Root:
    def __init__(self):
        self.scheduled = []

    def after(self, ms, fn=None):
        self.scheduled.append((ms, fn))


class _Proc:
    def __init__(self, alive=True):
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


class _App:
    SCALE = 1.0
    SM_SIG_MAX = G.App.SM_SIG_MAX
    _simple_tick = G.App._simple_tick
    _sm_set = G.App._sm_set

    _sm_reset_after_wipe = G.App._sm_reset_after_wipe

    def _sm_auto_serve(self):
        self.auto_serves.append(1)

    def _sm_paint_go(self, kind, progress, line1, line2=''):
        self.go_calls.append((kind, progress, line1, line2))

    def _sm_render_champs(self, best):
        self.champ_calls.append(list(best))

    def _spark_paint(self, cv, base, test):
        self.spark_calls.append((list(base), list(test)))
    _round_eta_left = staticmethod(G.App._round_eta_left)
    _fmt_left = staticmethod(G.App._fmt_left)
    _sig_alive = G.App._sig_alive
    _sig_status_color = staticmethod(G.App._sig_status_color)
    _sm_best_test_series = G.App._sm_best_test_series
    _sm_lib_kick = G.App._sm_lib_kick

    def _lib_file(self):
        return '/nonexistent/library.jsonl'              # no library in tick tests -> fallback line

    def __init__(self):
        import queue
        self._shell_mode = 'simple'
        self.cfg = {}
        self.logq = queue.Queue()
        self.root = _Root()
        self.proc = None
        self._pf_proc = None
        self._fetching = False
        self._sm_fetch_line = ''
        self._pf_doc = None
        self._sigs = []
        class _Spark:
            _h = 64

            def winfo_exists(self):
                return True

            def delete(self, *_a):
                pass

            def winfo_width(self):
                return 600

            def __getitem__(self, _k):
                return self._h

            def configure(self, height=None):
                if height is not None:
                    self._h = height
        self._sm = {k: _Lbl() for k in
                    ('state', 'go', 'sr_tri', 't_found', 't_fit', 't_gen', 'event')}
        self._sm['spark'] = _Spark()
        self._sm_spark_sig = None
        self.builds, self.go_calls, self.auto_serves = [], [], []
        self.champ_calls, self.spark_calls = [], []
        self._sig_canvas, self._sig_drawn, self._sig_health = {}, {}, {}

    def _simple_build_portfolio(self, n=0):     # the real default: combo auto-size
        self.builds.append(n)

    def _pid_alive(self, _pid):
        return False


@pytest.fixture()
def status(tmp_path, monkeypatch):
    p = tmp_path / 'status.json'
    monkeypatch.setattr(G, 'STATUS_FILE', str(p))

    def write(**kw):
        p.write_text(json.dumps(kw), encoding='utf-8')
    return write


def test_no_node_and_no_report_reads_as_setup(status):
    app = _App()
    app._simple_tick()
    assert 'press Start' in app._sm['state'].text
    assert app.root.scheduled, 'the loop must re-arm'


def test_a_stale_running_status_never_masquerades_as_live(status):
    """SIGKILL leaves state='running' in the file for ever. The process table decides."""
    status(state='running', rounds=3, trials_total=100, found=5)
    app = _App()                                          # proc is None — nothing is running
    app._simple_tick()
    assert 'round' not in app._sm['state'].text.lower()
    assert app.builds == [], 'a dead node must not trigger builds'


def test_downloading_shows_the_fetch_stream(status):
    app = _App()
    app._fetching = True
    app._sm_fetch_line = 'BTCUSDT 2023-01-01…'
    app._simple_tick()
    assert app._sm['state'].text == 'Search · downloading market data'
    assert app._sm['event'].text == 'BTCUSDT 2023-01-01…', 'the stream lands on the event line'
    assert app.go_calls[-1][0] == 'fetch'
    assert 'Cancel' in app.go_calls[-1][2]


def test_a_live_round_shows_real_counters(status):
    status(state='running', rounds=2, trials_total=8400, found=7, mode='exploring new',
           gen='gen 19 · evaluated 2610', history=[{'best_base': 2.2, 'best_test': 1.0},
                                                   {'best_base': 2.31, 'best_test': 1.1},
                                                   {'best_base': 2.4, 'best_test': 1.2}])
    app = _App()
    app.proc = _Proc()
    app._simple_tick()
    assert app._sm['state'].text.startswith('Search · round 3')
    assert app._sm['sr_tri'].text == '8,400 formulas'
    assert app._sm['t_found'].text == '7'
    assert app._sm['t_fit'].text == '+2.40'
    assert app._sm['t_gen'].text == '19'
    assert app.spark_calls and app.spark_calls[-1][0] == [2.2, 2.31, 2.4]
    kind, _prog, l1, l2 = app.go_calls[-1]
    assert kind == 'run' and l1 == 'round 3' and 'best +2.40' in l2


def test_a_winrate_run_formats_fitness_as_a_share(status):
    status(state='running', rounds=1, found=2, fit_metric='winrate',
           history=[{'best_base': 0.57}])
    app = _App()
    app.proc = _Proc()
    app._simple_tick()
    assert app._sm['t_fit'].text == '57%'


def test_a_round_budget_shows_as_round_x_of_y(status):
    status(state='running', rounds=7, found=3)
    app = _App()
    app.cfg = {'max_rounds': 8}
    app.proc = _Proc()
    app._simple_tick()
    assert app._sm['state'].text.startswith('Search · round 8 of 8')


def test_a_stopped_run_says_so_next_to_the_formula_count(status):
    status(state='stopped', rounds=8, trials_total=27463, found=120)
    app = _App()
    app._simple_tick()
    assert app._sm['sr_tri'].text == 'stopped · 27,463 formulas'


def test_the_champions_card_is_fed_from_status(status):
    status(state='running', rounds=1, found=3,
           best=[{'formula': 'ts_zscore:200(x)', 'base': 2.4, 'test': {'sharpe': 1.2}}])
    app = _App()
    app.proc = _Proc()
    app._simple_tick()
    assert app.champ_calls[-1][0]['formula'] == 'ts_zscore:200(x)'


def test_each_finished_round_rebuilds_the_portfolio_once(status):
    app = _App()
    app.proc = _Proc()
    status(state='running', rounds=1, found=4)
    app._simple_tick()
    app._simple_tick()                                    # same round -> no second build
    assert app.builds == [0], 'n=0 = combo auto-size (2..20), not a fixed six'
    status(state='running', rounds=2, found=5)
    app._simple_tick()
    assert app.builds == [0, 0]


def test_no_build_before_two_champions(status):
    """The builder needs >=2 usable alphas; spinning it earlier just burns a child."""
    status(state='running', rounds=1, found=1)
    app = _App()
    app.proc = _Proc()
    app._simple_tick()
    assert app.builds == []


def test_no_build_while_one_is_in_flight(status):
    status(state='running', rounds=1, found=4)
    app = _App()
    app.proc = _Proc()
    app._pf_proc = _Proc()                                # a build is already running
    app._simple_tick()
    assert app.builds == []


def test_the_stop_transition_runs_one_final_build(status):
    status(state='running', rounds=3, found=6)
    app = _App()
    app.proc = _Proc()
    app._simple_tick()                                    # running: builds round 3
    app.proc = _Proc(alive=False)                         # the node stopped
    status(state='stopped', rounds=3, found=6)
    app._simple_tick()
    assert len(app.builds) == 2, 'the last word is a fresh build over the full library'
    app._simple_tick()                                    # and only one
    assert len(app.builds) == 2


def test_leaving_simple_mode_stops_the_loop(status):
    app = _App()
    app._shell_mode = 'advanced'
    app._sm_tick_on = True
    app._simple_tick()
    assert app.root.scheduled == [], 'the tick must not survive into the dashboard'
    assert app._sm_tick_on is False


# ---------- the report's equity panel ----------

class _EqCanvas:
    def __init__(self, w=800, h=120):
        self._w, self._h, self.ops = w, h, []

    def winfo_exists(self):
        return True

    def winfo_width(self):
        return self._w

    def __getitem__(self, k):
        return self._h

    def delete(self, *_):
        self.ops.clear()

    def create_line(self, *a, **kw):
        self.ops.append(('line', a, kw.get('fill'), kw.get('width')))

    def create_rectangle(self, *a, **kw):
        self.ops.append(('rect', a, kw.get('fill')))

    def create_text(self, *a, **kw):
        self.ops.append(('text', a, kw.get('text')))


class _EqApp:
    SCALE = 1.0
    UI = 'ui'
    _sm_draw_equity = G.App._sm_draw_equity

    def __init__(self):
        self._sm = {'equity': _EqCanvas()}

    def _font(self, *_a):
        return ('f',)


def _eq_doc(n=40):
    return {'ok': True,
            'equity': {'dates': [f'2024-01-{i + 1:02d}' if i < 30 else f'2024-02-{i - 29:02d}'
                                 for i in range(n)],
                       'combined': [1.0 + i / 50 for i in range(n)],
                       'basket': [1.0 + i / 30 for i in range(n)]},
            'bounds': {'val_start': '2024-01-11', 'test_start': '2024-01-21',
                       'test_end': '2024-02-10'},
            'segments': {'train': {'sharpe': 0.21}, 'val': {'sharpe': 2.70},
                         'test': {'sharpe': 2.71}}}


def test_the_equity_panel_draws_both_curves_and_shades_test():
    G._apply_palette('light')
    app = _EqApp()
    app._sm_draw_equity(_eq_doc())
    kinds = [o[0] for o in app._sm['equity'].ops]
    assert kinds.count('line') >= 4, 'val/test ticks + basket + combined'
    assert 'rect' in kinds, 'the TEST wash'
    labels = [o[2] for o in app._sm['equity'].ops if o[0] == 'text']
    assert any(str(t).startswith('train ·') for t in labels), 'segment Sharpe labels'
    assert any(str(t).startswith('test ·') for t in labels)
    fills = [o[2] for o in app._sm['equity'].ops if o[0] == 'line']
    assert G.ACC in fills, 'the combined curve wears the accent'


def test_the_equity_panel_survives_zeroes_and_refuses_junk():
    app = _EqApp()
    doc = _eq_doc()
    doc['equity']['combined'][3] = 0.0                   # log of 0 must not explode
    app._sm_draw_equity(doc)
    assert app._sm['equity'].ops
    app._sm_draw_equity({'ok': True, 'equity': {'dates': ['a'], 'combined': [1.0]}})
    assert app._sm['equity'].ops == [], 'a malformed doc paints nothing, not garbage'
    app._sm_draw_equity({'ok': True})
    assert app._sm['equity'].ops == []


# ---------- the run pill ----------

class _GoCanvas:
    def __init__(self):
        self.ops = []

    def winfo_exists(self):
        return True

    def delete(self, *_):
        self.ops.clear()

    def create_rectangle(self, *a, **kw):
        self.ops.append(('rect', kw.get('fill')))

    def create_oval(self, *a, **kw):
        self.ops.append(('oval', kw.get('fill') or kw.get('outline')))

    def create_polygon(self, *a, **kw):
        self.ops.append(('poly', kw.get('fill')))

    def create_arc(self, *a, **kw):
        self.ops.append(('arc', kw.get('outline'), kw.get('extent')))

    def create_text(self, *a, **kw):
        self.ops.append(('text', kw.get('text')))


class _GoApp:
    SCALE = 1.0
    UI, MONO = 'ui', 'mono'
    _sm_paint_go = G.App._sm_paint_go
    _pill_paint = G.App._pill_paint
    _capsule = staticmethod(G.App._capsule)

    def __init__(self):
        self._sm = {'go': _GoCanvas()}

    def _font(self, *_a):
        return ('f',)


def test_the_ring_shows_real_progress_and_never_fakes_one():
    G._apply_palette('light')
    app = _GoApp()
    app._sm_paint_go('run', 0.62, 'round 3', 'best +2.41')
    arcs = [o for o in app._sm['go'].ops if o[0] == 'arc']
    assert len(arcs) == 1 and arcs[0][2] == pytest.approx(-0.62 * 359.9)
    app._sm_paint_go('run', None, 'round 1', 'estimating')   # no estimate yet -> track only
    assert [o for o in app._sm['go'].ops if o[0] == 'arc'] == []


def test_idle_wears_a_play_glyph_and_no_ring():
    app = _GoApp()
    app._sm_paint_go('idle', None, 'Start node')
    kinds = {o[0] for o in app._sm['go'].ops}
    assert 'poly' in kinds and 'arc' not in kinds and 'oval' in kinds  # capsule caps are ovals


def test_the_spark_dashed_line_is_the_librarys_best_test_so_far():
    """The dashed line must END on the same number the by-test champions top shows: a running
    max of TEST over the library by discovery round — not the fitness champion's own TEST
    (which sat at +0.02 while the table read +1.57)."""
    import os, tempfile, pathlib
    with tempfile.TemporaryDirectory() as td:
        docs = [{'formula': 'r1', 'base': 2.0, 'test': {'sharpe': 0.5}, 'round': 1},
                {'formula': 'r2_top', 'base': 1.1, 'test': {'sharpe': 1.57}, 'round': 2},
                {'formula': 'r3', 'base': 2.9, 'test': {'sharpe': 0.02}, 'round': 3}]
        app = _LibApp(_write_lib(pathlib.Path(td), docs))
        app._sm_lib = {'mtime': None, 'rows': [], 'computing': False}
        app._sm_lib_compute(app._lib_file(), os.path.getmtime(app._lib_file()))
        hist = [{'round': 1, 'best_test': 0.5}, {'round': 2, 'best_test': -0.1},
                {'round': 3, 'best_test': 0.02}]
        assert app._sm_best_test_series(hist) == [0.5, 1.57, 1.57]
        assert app._sm_champ_test_rows(1)[0][2] == 1.57, 'table top == line endpoint'


def test_the_spark_dashed_line_falls_back_to_history_without_a_library():
    app = _LibApp('/nonexistent/library.jsonl')
    hist = [{'round': 1, 'best_test': 0.5}, {'round': 2, 'best_test': 0.7}]
    assert app._sm_best_test_series(hist) == [0.5, 0.7]


# ---------- the advanced header pill + the fitness sparkline ----------

class _SparkCanvas:
    def __init__(self, w=600, h=46):
        self._w, self._h, self.ops = w, h, []

    def winfo_exists(self):
        return True

    def winfo_width(self):
        return self._w

    def __getitem__(self, k):
        return self._h

    def delete(self, *_):
        self.ops.clear()

    def create_line(self, *a, **kw):
        self.ops.append(('line', kw.get('fill'), kw.get('dash')))

    def create_oval(self, *a, **kw):
        self.ops.append(('oval', kw.get('fill')))

    def create_polygon(self, *a, **kw):
        self.ops.append(('poly', kw.get('fill'), None))

    def create_text(self, *a, **kw):
        self.ops.append(('text', kw.get('fill'), kw.get('text')))


class _SparkApp:
    """Drives the shared painter directly — the advanced wrapper around it is gone
    (that whole strip was removed from the advanced screen on user request)."""
    SCALE = 1.0
    MONO = 'mono'
    _spark_paint = G.App._spark_paint

    def _font(self, *_a, **_k):
        return None

    def __init__(self):
        self.fit_spark = _SparkCanvas()

    def paint(self, hist):
        base = [x.get('best_base', x.get('best_test')) for x in hist]
        base = [v for v in base if isinstance(v, (int, float))]
        self._spark_paint(self.fit_spark, base, [x.get('best_test') for x in hist])


def test_the_sparkline_draws_base_solid_and_test_dashed():
    G._apply_palette('light')
    app = _SparkApp()
    hist = [{'best_base': 1.0, 'best_test': 0.6}, {'best_base': 1.4, 'best_test': 0.9},
            {'best_base': 1.9, 'best_test': 1.0}]
    app.paint(hist)
    lines = [o for o in app.fit_spark.ops if o[0] == 'line']
    assert any(o[1] == G.ACC and not o[2] for o in lines), 'the fitness curve, solid accent'
    assert any(o[1] == G.SHORTC and o[2] for o in lines), \
        'its TEST shadow, dashed in the visible warm colour (FAINT was unreadable on dark)'


def test_the_sparkline_carries_its_values_as_text():
    """The user could not tell WHAT the curve was worth: the grid puts numbers on the scale
    and both lines label their newest point."""
    G._apply_palette('dark')
    app = _SparkApp()
    hist = [{'best_base': 1.0, 'best_test': 0.6}, {'best_base': 1.4, 'best_test': 0.9},
            {'best_base': 3.03, 'best_test': 0.42}]
    app.paint(hist)
    texts = [o for o in app.fit_spark.ops if o[0] == 'text']
    assert ('text', G.ACC, '+3.03') in texts, 'the fitness endpoint states its value'
    assert ('text', G.SHORTC, '+0.42') in texts, 'the TEST endpoint states its value'
    assert any(o[1] == G.MUT for o in texts), 'grid ticks carry the scale'


def test_the_sparkline_needs_two_rounds():
    app = _SparkApp()
    app.paint([{'best_base': 1.0}])
    assert app.fit_spark.ops == []


# ---------- the serving card at the bottom ----------

class _Packable:
    def __init__(self):
        self.packed = False

    def winfo_exists(self):
        return True

    def winfo_manager(self):
        return 'pack' if self.packed else ''

    def pack(self, **_kw):
        self.packed = True

    def pack_forget(self):
        self.packed = False


def _with_service(app, port=8799):
    app._sm['sig_card'] = _Packable()
    app._sm['sig_status'] = _Lbl()
    app._sm['sig_rows'] = [{'fr': _Packable(), 'hd': _Lbl(), 'st': _Lbl(),
                            'cv': object(), 'port': None}
                           for _ in range(G.App.SM_SIG_MAX)]
    app._sigs = [{'port': port, 'label': 'portfolio_top6', 'proc': None, 'pid': None}]
    return app


def test_a_live_service_packs_the_card_and_adopts_the_canvas(status):
    app = _with_service(_App())
    app._sig_canvas, app._sig_drawn = {}, {8799: 'T1'}
    app._simple_tick()
    assert app._sm['sig_card'].packed, 'the card must appear'
    row = app._sm['sig_rows'][0]
    assert row['fr'].packed and app._sig_canvas[8799] is row['cv']
    assert 8799 not in app._sig_drawn, 'the stale stamp must go, or the first paint never comes'
    assert ':8799' in row['hd'].text and 'portfolio_top6' in row['hd'].text
    assert not app._sm['sig_rows'][1]['fr'].packed, 'empty slots stay hidden'


def test_at_most_three_services_are_shown(status):
    """The user's cap: no more than 3 APIs below the portfolio — a fourth exists only on
    the Advanced screen."""
    app = _with_service(_App())
    app._sigs = [{'port': 8800 + i, 'label': f's{i}', 'proc': None, 'pid': None}
                 for i in range(5)]
    app._sig_canvas, app._sig_drawn = {}, {}
    app._simple_tick()
    packed = [r for r in app._sm['sig_rows'] if r['fr'].packed]
    assert [r['port'] for r in packed] == [8800, 8801, 8802]
    assert 8803 not in app._sig_canvas
    assert '3 of 3' in app._sm['sig_status'].text


def test_no_service_means_no_card(status):
    app = _with_service(_App())
    app._sig_canvas, app._sig_drawn = {}, {}
    app._sigs = []
    app._sm['sig_card'].packed = True                    # was showing; the service died
    app._simple_tick()
    assert not app._sm['sig_card'].packed


def test_a_computing_service_keeps_the_card_and_says_what_it_is_doing(status):
    """A first signal takes minutes; the card hiding until then read as 'serve did nothing'."""
    app = _with_service(_App())
    app._sig_health[8799] = '○ computing the first signal…'
    app._simple_tick()
    assert app._sm['sig_card'].packed
    assert 'computing the first signal' in app._sm['sig_rows'][0]['st'].text


def test_even_a_dead_service_stays_on_screen_with_its_status(status):
    app = _with_service(_App())
    app._sigs[0]['pid'] = 4242                           # dead pid -> _sig_alive is False
    app._sig_health[8799] = '○ stopped (the process exited) — port is free'
    app._simple_tick()
    assert app._sm['sig_card'].packed, 'permanent while the service exists'
    assert 'stopped' in app._sm['sig_rows'][0]['st'].text


def test_the_simple_wipe_reset_blanks_every_cache_the_tick_trusts():
    """Clear node in the simple shell: caches, autopilot round markers and labels must all
    reset — stale state would rebuild the very history the user just deleted."""
    app = _App()
    for k in ('rep_head', 'rep_line', 'rep_note'):
        app._sm[k] = _Lbl()
    app._pf_doc = {'ok': True}
    app._sm_best_cache = [{'formula': 'x'}]
    app._sm_built_round = 7
    app._sm_reset_after_wipe()
    assert app._pf_doc is None and app._sm_best_cache == []
    assert app._sm_built_round is None
    assert app.champ_calls[-1] == [], 'the champions card empties'
    assert 'no portfolio yet' in app._sm['rep_note'].text
    assert app._sm['t_found'].text == '0'


# ---------- the serving autopilot: portfolio + two TEST tops, only after the node stops ----------

class _ServeApp:
    SM_SIG_MAX = G.App.SM_SIG_MAX
    SM_CHAMPS = G.App.SM_CHAMPS
    _sm_auto_serve = G.App._sm_auto_serve
    _sig_alive = G.App._sig_alive

    def __init__(self):
        self._sigs = []
        self._pf_doc = None
        self.lib_rows = []                               # what _sm_champ_test_rows would return
        self.served, self.stopped, self.notes = [], [], []

    def _sm_champ_test_rows(self, cap):
        return self.lib_rows[:cap]

    def _sm_set(self, key, text, color=None):
        self.notes.append((key, text))

    def _pid_alive(self, _pid):
        return False

    def _stop_signal(self, entry):
        self.stopped.append(entry['label'])
        self._sigs.remove(entry)

    def _serve_signal(self, formulas, label, quiet=False):
        assert quiet, 'the autopilot must never pop a dialog'
        self.served.append((label, list(formulas)))
        self._sigs.append({'port': 8800 + len(self._sigs), 'label': label,
                           'proc': None, 'pid': None})

    def _sig_save(self):
        pass


def test_auto_serve_fills_all_three_slots():
    """User spec rev. 3: the portfolio plus the two best TEST formulas — near-clones of the
    top don't count as a second champion (the user wants variety, not the twin)."""
    app = _ServeApp()
    app._pf_doc = {'formulas_full': ['f1', 'f2', 'f3']}
    app.lib_rows = [('locked · Sharpe +2.00 · win 55% · PnL +40%', 3.0, 2.0, True),   # sealed:
                    ('ts_zscore:200(cs_rank(close))', 1.0, 1.6, False),   # unservable, skipped
                    ('ts_zscore:201(cs_rank(close))', 1.0, 1.5, False),   # a near-clone of the top
                    ('slog(sub(ts_sum:26(vwap),ema:3(low)))', 1.2, 1.4, False)]
    app._sm_auto_serve()
    assert app.served == [('auto_portfolio', ['f1', 'f2', 'f3']),
                          ('auto_top1', ['ts_zscore:200(cs_rank(close))']),
                          ('auto_top2', ['slog(sub(ts_sum:26(vwap),ema:3(low)))'])]


def test_auto_serve_retires_a_top_slot_whose_champion_left():
    app = _ServeApp()
    app._sigs = [{'port': 8801, 'label': 'auto_top1', 'proc': None, 'pid': None,
                  'fsig': 'stale'},
                 {'port': 8802, 'label': 'auto_top2', 'proc': None, 'pid': None,
                  'fsig': 'stale'}]
    app.lib_rows = [('only_one_left(x)', 1.0, 1.1)]      # the library thinned to one champion
    app._sm_auto_serve()
    assert 'auto_top2' in app.stopped, 'the orphaned slot goes down with its champion'
    assert app.served == [('auto_top1', ['only_one_left(x)'])]


def test_auto_serve_replaces_its_own_service_only_when_membership_changed():
    app = _ServeApp()
    app._pf_doc = {'formulas_full': ['f1', 'f2']}
    app._sm_auto_serve()                                 # first raise
    app.served.clear()
    app._sm_auto_serve()                                 # same membership -> no churn
    assert app.served == [] and app.stopped == []
    app._pf_doc = {'formulas_full': ['f1', 'f9']}
    app._sm_auto_serve()                                 # changed -> stop ours, raise anew
    assert app.stopped == ['auto_portfolio']
    assert app.served == [('auto_portfolio', ['f1', 'f9'])]


def test_auto_serve_never_touches_foreign_services_even_at_the_cap():
    """The user's own serves (any other label — 8799 included) are sacred: at the cap the
    autopilot backs off with a note instead of freeing a port itself."""
    app = _ServeApp()
    app._sigs = [{'port': 8799 + i, 'label': f'mine_{i}', 'proc': None, 'pid': None}
                 for i in range(3)]
    app._pf_doc = {'formulas_full': ['f1', 'f2']}
    app.lib_rows = [('hero(x)', 1.0, 1.2)]
    app._sm_auto_serve()
    assert app.served == [] and app.stopped == []
    assert any('free a port' in t for _k, t in app.notes)


def test_auto_serve_skips_quietly_without_a_portfolio():
    app = _ServeApp()
    app._sm_auto_serve()
    assert app.served == [] and app.stopped == [] and app.notes == []


def test_the_tick_never_serves_mid_run(status):
    """Serving happens in _simple_pf_done after the node stops — a running search must
    not raise or replace services, whatever the round number."""
    app = _App()
    app.proc = _Proc()
    for r in (3, 6, 9):
        status(state='running', rounds=r, found=4)
        app._simple_tick()
    assert app.auto_serves == []


def test_champion_rows_keep_the_node_fitness_order():
    best = [{'formula': 'A', 'base': 2.5, 'test': {'sharpe': -0.5}},
            {'formula': 'B', 'base': 2.0, 'test': {'sharpe': 1.4}},
            {'formula': 'C', 'base': 1.9}]
    assert [r[0] for r in G.App._sm_champ_rows(best, 8)] == ['A', 'B', 'C']


class _LibApp:
    """Just enough App for the by-test path: a real library file, no threads."""
    SM_CHAMPS = G.App.SM_CHAMPS
    _LB_TESTKEY = staticmethod(G.App._LB_TESTKEY)
    _sm_champ_test_rows = G.App._sm_champ_test_rows
    _sm_lib_compute = G.App._sm_lib_compute
    _sm_lib_kick = G.App._sm_lib_kick
    _sm_best_test_series = G.App._sm_best_test_series

    def __init__(self, path):
        self._path = path

    def _lib_file(self):
        return self._path


def _write_lib(tmp_path, docs):
    lib = tmp_path / 'library.jsonl'
    lib.write_text('\n'.join(json.dumps(d) for d in docs) + '\n', encoding='utf-8')
    return str(lib)


def test_by_test_ranks_the_whole_library_not_the_fitness_top():
    """The bug the user caught on screen: re-sorting status.json's best (top-KEEP by FITNESS)
    hid every high-TEST formula below the fitness cut. The card must rank the library —
    the same population the advanced TEST OOS view shows."""
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as td:
        docs = [{'formula': 'strong_fit', 'base': 2.9, 'test': {'sharpe': -0.2}},
                {'formula': 'no_test_yet', 'base': 2.5},
                {'formula': 'weak_fit_great_test', 'base': 1.1, 'test': {'sharpe': 0.9}},
                {'formula': 'mid', 'base': 2.0, 'test': {'sharpe': 0.1}}]
        app = _LibApp(_write_lib(pathlib.Path(td), docs))
        import os
        app._sm_lib = {'mtime': None, 'rows': [], 'computing': False}
        app._sm_lib_compute(app._lib_file(), os.path.getmtime(app._lib_file()))
        rows = app._sm_champ_test_rows(8)
        assert [r[0] for r in rows] == ['weak_fit_great_test', 'mid', 'strong_fit'], \
            'ranked by held-out TEST across the library; rows without TEST are out'
        assert rows[0][2] == 0.9


def test_by_test_survives_a_torn_last_line_and_caps_the_rows():
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as td:
        docs = [{'formula': f'f{i}', 'base': 1.0, 'test': {'sharpe': i / 100}}
                for i in range(G.App.SM_CHAMPS * 5)]
        path = _write_lib(pathlib.Path(td), docs)
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write('{"formula": "torn')                # the node mid-append
        app = _LibApp(path)
        import os
        app._sm_lib = {'mtime': None, 'rows': [], 'computing': False}
        app._sm_lib_compute(path, os.path.getmtime(path))
        assert len(app._sm_lib['rows']) == G.App.SM_CHAMPS * 3, 'kept a bounded slice'
        assert app._sm_champ_test_rows(8)[0][0] == f'f{G.App.SM_CHAMPS * 5 - 1}'


# ---------- no activation key: the cards show numbers, never a bare id ----------

def test_sealed_champion_rows_show_the_numbers_not_an_id():
    """User spec: without an activation key a champion row still says what the alpha did —
    Sharpe, win rate, PnL — in place of the formula; 'id:xxxxxx' told the customer nothing."""
    best = [{'locked': True, 'id': 'abcdef123456', 'base': 2.1,
             'test': {'sharpe': 1.04, 'wr': 0.54, 'ret': 0.29, 'cagr': 0.2}},
            {'locked': True, 'id': 'ffffff000000', 'base': 2.0,
             'test': {'sharpe': 0.5, 'cagr': 0.12}},        # mined before wr/ret were stored
            {'formula': 'open(x)', 'base': 1.9, 'test': {'sharpe': 0.1}}]
    rows = G.App._sm_champ_rows(best, 8)
    assert rows[0] == ('locked · Sharpe +1.04 · win 54% · PnL +29%', 2.1, 1.04, True)
    assert rows[1][0] == 'locked · Sharpe +0.50 · win — · PnL +12%/yr'
    assert rows[2] == ('open(x)', 1.9, 0.1, False)
    assert not any('id:' in r[0] for r in rows)


def test_by_test_rows_flag_sealed_entries_for_the_autopilot(tmp_path):
    """The serving autopilot reads the same rows: a sealed one must be recognisable as
    unservable by its flag, not by guessing at the text."""
    import os
    docs = [{'locked': True, 'id': 'abcdef123456', 'base': 2.0,
             'test': {'sharpe': 1.5, 'wr': 0.6, 'ret': 0.5}},
            {'formula': 'plain(x)', 'base': 1.0, 'test': {'sharpe': 0.4}}]
    app = _LibApp(_write_lib(tmp_path, docs))
    app._sm_lib = {'mtime': None, 'rows': [], 'computing': False}
    app._sm_lib_compute(app._lib_file(), os.path.getmtime(app._lib_file()))
    rows = app._sm_champ_test_rows(8)
    assert rows[0][3] is True and rows[0][0].startswith('locked · Sharpe +1.50 · win 60%')
    assert rows[1] == ('plain(x)', 1.0, 0.4, False)
    serve = _ServeApp()
    serve.lib_rows = rows
    serve._sm_auto_serve()
    assert serve.served == [('auto_top1', ['plain(x)'])], 'only the open row is served'

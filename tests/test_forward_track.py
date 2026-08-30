"""Forward-track enrollment: the on-disk track and the GUI click chain.

Guards the invariants behind a real shipped bug: the "Forward track ➕" button silently did
NOTHING in the frozen build — forward_track resolved its state dir next to the module (the
read-only AppImage/deb bundle), save_track's open() raised OSError inside the Tk callback,
and Tk swallowed the traceback, so no dialog, no forward.json, no error. These tests would
have caught it three ways: (A) load/save must live under ALPHANODE_STATE_DIR and survive a
missing/corrupt file, and (B) a real App's _fwd_enroll must actually produce forward.json in
the state dir, freeze the ACTIVE timeframe's universe (another past bug: 1h alphas frozen on
the 1d basket), confirm with a dialog, and leave the Tk error hook empty.
"""
import hashlib
import json
import os
import pickle

import pytest

import forward_track as ft

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORMULA = 'ema:10(ema:16(logret))'


# ---------------------------------------------------------------- Part A: the library itself

def test_new_entry_field_shape():
    e = ft.new_entry('alpha_t01', 'alpha', [FORMULA], ['BTCUSDT', 'ETHUSDT'],
                     0.25, 0.001, '2019-09-05', tf='1h')
    assert set(e) == {'id', 'name', 'kind', 'tf', 'formulas', 'tickers', 'vol', 'exec',
                      'engine_start', 'start_capital', 'enrolled', 'archived', 'state',
                      'history', 'session'}
    assert e['name'] == 'alpha_t01' and e['kind'] == 'alpha' and e['tf'] == '1h'
    assert e['formulas'] == [FORMULA]
    assert e['tickers'] == ['BTCUSDT', 'ETHUSDT']
    assert isinstance(e['vol'], float) and e['vol'] == 0.25
    assert isinstance(e['exec'], float) and e['exec'] == 0.001
    assert e['engine_start'] == '2019-09-05'          # trimmed to the date part
    assert e['start_capital'] == ft.START_CAPITAL
    assert e['archived'] is False
    assert e['history'] == []
    assert e['state'] == {'equity': ft.START_CAPITAL, 'positions': {}, 'prices': {},
                          'last_run': None}
    # enrolled is today's UTC date in ISO form
    assert len(e['enrolled']) == 10 and e['enrolled'][4] == '-' and e['enrolled'][7] == '-'
    # the enrolling session's id — '' only when the sessions module is unavailable
    assert e['session'] == '' or (len(e['session']) >= 6
                                  and all(c in '0123456789abcdef' for c in e['session']))


def test_new_entry_signature_matches_frozen_strategy():
    tickers = ['ETHUSDT', 'BTCUSDT']
    e = ft.new_entry('alpha_t01', 'alpha', [FORMULA], tickers, 0.25, 0.001,
                     '2019-09-05', tf='1h')
    sig = hashlib.md5((FORMULA + '#' + ','.join(sorted(tickers)) + '#1h')
                      .encode()).hexdigest()[:6]
    assert e['id'] == f'alpha_t01_{sig}'
    # ticker ORDER must not change the identity of the frozen strategy…
    e2 = ft.new_entry('alpha_t01', 'alpha', [FORMULA], list(reversed(tickers)),
                      0.25, 0.001, '2019-09-05', tf='1h')
    assert e2['id'] == e['id']
    # …but the bar size must: the same formula on 1d bars is a different strategy
    e3 = ft.new_entry('alpha_t01', 'alpha', [FORMULA], tickers, 0.25, 0.001,
                      '2019-09-05', tf='1d')
    assert e3['id'] != e['id']


def test_find_duplicate_ignores_ticker_order():
    e = ft.new_entry('a', 'alpha', [FORMULA], ['BTCUSDT', 'ETHUSDT'], 0.25, 0.001,
                     '2019-09-05', tf='1h')
    track = {'entries': [e]}
    dup = ft.find_duplicate(track, [FORMULA], ['ETHUSDT', 'BTCUSDT'], tf='1h')
    assert dup is e


def test_find_duplicate_distinguishes_timeframes():
    e = ft.new_entry('a', 'alpha', [FORMULA], ['BTCUSDT', 'ETHUSDT'], 0.25, 0.001,
                     '2019-09-05', tf='1h')
    track = {'entries': [e]}
    assert ft.find_duplicate(track, [FORMULA], ['BTCUSDT', 'ETHUSDT'], tf='1d') is None


def test_find_duplicate_skips_archived_entries():
    e = ft.new_entry('a', 'alpha', [FORMULA], ['BTCUSDT'], 0.25, 0.001,
                     '2019-09-05', tf='1h')
    e['archived'] = True
    assert ft.find_duplicate({'entries': [e]}, [FORMULA], ['BTCUSDT'], tf='1h') is None


def test_load_track_missing_file_yields_empty_track(sandbox):
    assert ft.track_file() == str(sandbox / 'forward.json')
    assert not os.path.exists(ft.track_file())
    assert ft.load_track() == {'entries': []}


def test_load_track_corrupt_file_yields_empty_track(sandbox):
    (sandbox / 'forward.json').write_text('{"entries": [oops — not json')
    assert ft.load_track() == {'entries': []}


def test_save_track_load_track_roundtrip_in_state_dir(sandbox):
    e = ft.new_entry('alpha_t01', 'alpha', [FORMULA], ['BTCUSDT', 'ETHUSDT'],
                     0.25, 0.001, '2019-09-05', tf='1h')
    ft.save_track({'entries': [e]})
    # the write MUST land in ALPHANODE_STATE_DIR (the shipped bug wrote into the bundle)
    assert (sandbox / 'forward.json').exists()
    assert not (sandbox / 'forward.json.tmp').exists()    # atomic replace left no temp file
    got = ft.load_track()
    assert got == {'entries': [e]}


# ------------------------------------------------- Part B: the real GUI click chain (needs X)

def _expected_1h_tickers():
    with open(os.path.join(ROOT, 'data_1h.pickle'), 'rb') as f:
        return list(pickle.load(f)[0])


def _enroll_once(app):
    app._fwd_enroll([FORMULA], 'alpha_t01', 'alpha')


@pytest.mark.gui
def test_gui_enroll_writes_forward_json_and_confirms(gui_app):
    app, rec, state = gui_app
    _enroll_once(app)
    fj = state / 'forward.json'
    assert fj.exists(), 'the ➕ click chain never wrote forward.json — the shipped-build bug'
    doc = json.loads(fj.read_text())
    assert len(doc['entries']) == 1
    e = doc['entries'][0]
    assert e['tf'] == '1h'                                # frozen with the ACTIVE timeframe
    assert e['tickers'] == _expected_1h_tickers()         # the 1h basket, not the 1d fallback
    assert e['formulas'] == [FORMULA]
    assert not e.get('archived')
    kinds = [c[0] for c in rec.calls]
    assert 'showerror' not in kinds and 'showwarning' not in kinds
    ask = [c for c in rec.calls if c[0] == 'askyesno']
    assert len(ask) == 1 and 'enrolled' in ask[0][2]


@pytest.mark.gui
def test_gui_second_identical_enroll_is_rejected_as_duplicate(gui_app):
    app, rec, state = gui_app
    _enroll_once(app)
    rec.calls.clear()
    _enroll_once(app)
    doc = json.loads((state / 'forward.json').read_text())
    assert len(doc['entries']) == 1, 'duplicate enroll appended a second entry'
    infos = [c for c in rec.calls if c[0] == 'showinfo']
    assert len(infos) == 1 and 'Already enrolled' in infos[0][2]
    assert not any(c[0] == 'askyesno' for c in rec.calls)  # no second "enrolled" confirmation


@pytest.mark.gui
def test_gui_fwd_refresh_is_clean_and_tk_swallows_no_errors(gui_app):
    app, rec, state = gui_app
    _enroll_once(app)
    app._fwd_refresh()                                    # must not raise with a live entry
    for _ in range(10):                                   # flush any deferred after() jobs
        app.root.update()
    assert app._test_tk_errors == [], (
        'a Tk callback raised and the exception was swallowed — exactly how the shipped '
        f'bug hid: {app._test_tk_errors}')


@pytest.mark.gui
def test_gui_universe_tickers_honors_explicit_list(gui_app):
    app, rec, state = gui_app
    assert app._universe_tickers() == _expected_1h_tickers()   # universe_all=true baseline
    app.cfg['universe_all'] = False
    app.cfg['universe_list'] = ' btcusdt, ETHUSDT ,solusdt '
    assert app._universe_tickers() == ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
    app.cfg['universe_list'] = '  ,  '
    assert app._universe_tickers() is None                     # empty list is "no universe"


# ------------------------------------------------- Part C: concurrent edits must not be lost

def _enroll_two(tmp_names=('alpha_a', 'alpha_b')):
    track = ft.load_track()
    e1 = ft.new_entry(tmp_names[0], 'alpha', [FORMULA], ['BTCUSDT'], 0.25, 0.001, '2019-09-05')
    e2 = ft.new_entry(tmp_names[1], 'alpha', [FORMULA + '#2'], ['ETHUSDT'], 0.25, 0.001, '2019-09-05')
    track['entries'] += [e1, e2]
    ft.save_track(track)
    return e1, e2


def test_archive_during_stepping_pass_stays_archived(sandbox):
    """The shipped resurrection bug: the stepper holds a pre-Archive snapshot of the whole
    file and its save used to write that snapshot back, erasing the user's Archive click.
    sync_entry_to_disk must refuse to persist a step for an entry archived on disk."""
    e1, _ = _enroll_two()
    stepper_copy = json.loads(json.dumps(e1))            # the stepper's stale in-memory entry

    track = ft.load_track()                              # ... user clicks Archive meanwhile
    for x in track['entries']:
        if x['id'] == e1['id']:
            x['archived'] = True
    ft.save_track(track)

    stepper_copy['state']['equity'] = 9999.0             # the step "finishes"
    stepper_copy['history'].append({'date': '2026-08-19', 'equity': 9999.0})
    assert ft.sync_entry_to_disk(stepper_copy) is False  # step result dropped

    on_disk = {x['id']: x for x in ft.load_track()['entries']}[e1['id']]
    assert on_disk['archived'] is True                   # the click survived the pass
    assert on_disk['history'] == []                      # no zombie step row
    assert on_disk['state']['equity'] == ft.START_CAPITAL


def test_enrollment_during_stepping_pass_survives(sandbox):
    """The same lost-update in the other direction: an entry enrolled while the stepper
    runs must not vanish when the stepper saves its step."""
    e1, _ = _enroll_two()
    stepper_copy = json.loads(json.dumps(e1))

    track = ft.load_track()                              # ... user enrolls a THIRD strategy
    e3 = ft.new_entry('alpha_c', 'alpha', [FORMULA + '#3'], ['SOLUSDT'], 0.25, 0.001, '2019-09-05')
    track['entries'].append(e3)
    ft.save_track(track)

    stepper_copy['state']['equity'] = 10123.0
    stepper_copy['history'].append({'date': '2026-08-19', 'equity': 10123.0})
    assert ft.sync_entry_to_disk(stepper_copy) is True   # the step itself lands...

    ids = [x['id'] for x in ft.load_track()['entries']]
    assert e3['id'] in ids                               # ...and the new enrollment survives
    on_disk = {x['id']: x for x in ft.load_track()['entries']}[e1['id']]
    assert on_disk['state']['equity'] == 10123.0
    assert len(on_disk['history']) == 1


def test_entry_deleted_on_disk_is_not_resurrected_by_step(sandbox):
    e1, _ = _enroll_two()
    stepper_copy = json.loads(json.dumps(e1))

    track = ft.load_track()
    track['entries'] = [x for x in track['entries'] if x['id'] != e1['id']]
    ft.save_track(track)

    stepper_copy['history'].append({'date': '2026-08-19', 'equity': 10001.0})
    assert ft.sync_entry_to_disk(stepper_copy) is False
    assert e1['id'] not in [x['id'] for x in ft.load_track()['entries']]


@pytest.mark.gui
def test_gui_delete_removes_entry_and_its_history_for_good(gui_app, monkeypatch):
    """The Delete button (ex-Archive): the entry leaves forward.json entirely — no hidden
    archived rows accumulating in the file — and the confirmation says it is irreversible."""
    app, rec, state = gui_app
    _enroll_once(app)
    fj = state / 'forward.json'
    e_id = json.loads(fj.read_text())['entries'][0]['id']

    app._fwd_refresh()
    app.fwd_tree.selection_set(e_id)                      # rows are keyed by entry id

    # the recorder's default askyesno=False must mean "delete cancelled, nothing changed"
    app._fwd_delete()
    assert len(json.loads(fj.read_text())['entries']) == 1

    monkeypatch.setattr(app.__class__, '_fwd_selected', lambda self: next(
        (x for x in ft.load_track()['entries'] if x['id'] == e_id), None))
    import alphanode_gui as G
    monkeypatch.setattr(G.messagebox, 'askyesno', lambda *a, **k: True, raising=False)
    app._fwd_delete()

    doc = json.loads(fj.read_text())
    assert doc['entries'] == []                           # gone from the file, not flagged


def test_new_entry_explicit_id_wins_over_name_sig():
    e = ft.new_entry('whatever', 'alpha', ['f'], ['BTCUSDT'], 0.25, 0.001,
                     '2020-01-01', entry_id='abc123')
    assert e['id'] == 'abc123'
    legacy = ft.new_entry('name', 'alpha', ['f'], ['BTCUSDT'], 0.25, 0.001, '2020-01-01')
    assert legacy['id'].startswith('name_')              # old callers unchanged


def test_migrate_ids_renames_legacy_alphas_only():
    track = {'entries': [
        {'id': 'alpha_c560b8_e75526', 'name': 'alpha_c560b8'},
        {'id': 'portfolio_top6_27d869', 'name': 'portfolio_top6'},   # portfolios keep style
        {'id': 'c0ffee', 'name': 'c0ffee'},                          # already new-style
        {'id': 'alpha_c0ffee_aaaaaa', 'name': 'x'},                  # rename would collide
    ]}
    assert ft.migrate_ids(track) == 1
    assert [e['id'] for e in track['entries']] == \
        ['c560b8', 'portfolio_top6_27d869', 'c0ffee', 'alpha_c0ffee_aaaaaa']
    assert track['entries'][0]['name'] == 'c560b8'


@pytest.mark.gui
def test_gui_enroll_id_matches_the_leaderboard(gui_app):
    """One list, two panels: the forward entry id must be EXACTLY the leaderboard's
    md5(formula)[:6] — no 'alpha_' prefix, no enrollment sig. The id doubles as the
    uniqueness key: the same alpha cannot enroll twice, even on another universe."""
    app, rec, state = gui_app
    _enroll_once(app)
    e = json.loads((state / 'forward.json').read_text())['entries'][0]
    lb_id = hashlib.md5(FORMULA.encode()).hexdigest()[:6]
    assert e['id'] == lb_id and e['name'] == lb_id

    app.cfg['universe_all'] = False
    app.cfg['universe_list'] = 'BTCUSDT,ETHUSDT'         # different basket, same alpha
    n0 = len(rec.calls)
    _enroll_once(app)
    doc = json.loads((state / 'forward.json').read_text())
    assert len(doc['entries']) == 1                      # refused: one alpha — one entry
    assert any(c[0] == 'showinfo' and 'Already enrolled' in c[2] for c in rec.calls[n0:])


@pytest.mark.gui
def test_gui_migrates_legacy_ids_on_first_touch(gui_app):
    app, rec, state = gui_app
    (state / 'forward.json').write_text(json.dumps({'entries': [
        ft.new_entry('alpha_aaaaaa', 'alpha', ['x'], ['BTCUSDT'], 0.25, 0.001,
                     '2020-01-01')]}))
    app._fwd_migrated = False                            # a fresh session touches the lib
    app._fwd_lib()
    ids = [e['id'] for e in json.loads((state / 'forward.json').read_text())['entries']]
    assert ids == ['aaaaaa']


@pytest.mark.gui
def test_gui_leaderboard_search_filters_by_id_and_formula(gui_app):
    """The find box: substring of the 6-char id or of the formula text, live, Esc clears.
    The same identifiers the table displays — so what you see is what you can find."""
    app, rec, state = gui_app
    rows = [{'formula': 'ts_sum:40(ts_roc:120(tanh(slog(high))))', 'base': 1.1,
             'test': {'sharpe': 1.8}},
            {'formula': 'ema:103(ts_mean:120(tanh(body)))', 'base': 0.8,
             'test': {'sharpe': 1.7}}]
    ids = [hashlib.md5(r['formula'].encode()).hexdigest()[:6] for r in rows]

    app._lib_cache.update(all=rows, families=rows, computed=True)
    app._fill_tree(rows)
    assert len(app.tree.get_children()) == 2

    app.v_lb_q.set(ids[0][:4])                           # prefix of the first id
    assert len(app.tree.get_children()) == 1
    assert ids[0] in str(app.tree.item(app.tree.get_children()[0])['values'])

    app.v_lb_q.set('TANH(BODY')                          # formula text, case-insensitive
    assert len(app.tree.get_children()) == 1
    app.v_lb_q.set('нет-такого')
    assert len(app.tree.get_children()) == 0
    app.v_lb_q.set('')                                   # cleared -> everything is back
    assert len(app.tree.get_children()) == 2


def test_new_entry_stamps_the_current_session(tmp_path, monkeypatch):
    """The track outlives sessions (Clear all history spares it; a load merges into it), so
    entries from many sessions pile up in one list — the stamp says where each came from."""
    monkeypatch.setenv('ALPHANODE_STATE_DIR', str(tmp_path))
    import sessions as S
    sid = S.current_session_id(str(tmp_path))
    e = ft.new_entry('a', 'alpha', [FORMULA], ['BTCUSDT'], 0.25, 0.001, '2024-01-01')
    assert e['session'] == sid
    # …and a fresh epoch changes the stamp for the NEXT enroll, not the existing entry
    S.begin_new_session(str(tmp_path))
    e2 = ft.new_entry('b', 'alpha', [FORMULA], ['ETHUSDT'], 0.25, 0.001, '2024-01-01')
    assert e2['session'] != sid and e['session'] == sid


# ------------------------------------------------- Part D: one id, one entry (BUG_FIXES 2026-08-25)

def test_sync_prefers_active_copy_when_id_is_doubled(sandbox):
    """A shipped loop: the track carried an ARCHIVED copy and a live re-enrollment of the
    same portfolio under one id (name_sig is deterministic; an old Archive button left the
    ghost). sync_entry_to_disk matched the ghost first -> 'archived mid-step — dropped' on
    every tick, and the live entry never advanced. The active copy must win; the ghost stays
    untouched."""
    e1, _ = _enroll_two()
    ghost = json.loads(json.dumps(e1))
    ghost['archived'] = True
    track = ft.load_track()
    track['entries'].insert(0, ghost)                    # the ghost sits BEFORE the live copy
    ft.save_track(track)

    stepped = json.loads(json.dumps(e1))                 # the stepper's copy, one bar later
    stepped['state']['equity'] = 12345.0
    stepped['history'].append({'date': '2026-08-25', 'equity': 12345.0})
    assert ft.sync_entry_to_disk(stepped) is True

    copies = [x for x in ft.load_track()['entries'] if x['id'] == e1['id']]
    live = [x for x in copies if not x['archived']]
    assert len(copies) == 2 and len(live) == 1
    assert live[0]['state']['equity'] == 12345.0 and len(live[0]['history']) == 1
    arch = next(x for x in copies if x['archived'])
    assert arch['history'] == [] and arch['state']['equity'] == ft.START_CAPITAL
    assert ft.find_entry(ft.load_track(), e1['id'])['archived'] is False


def test_unique_id_steps_around_ghosts():
    track = {'entries': [{'id': 'portfolio_top6_da7dbf', 'archived': True},
                         {'id': 'portfolio_top6_da7dbf-2', 'archived': True}]}
    assert ft.unique_id(track, 'portfolio_top6_da7dbf') == 'portfolio_top6_da7dbf-3'
    assert ft.unique_id(track, 'fresh') == 'fresh'
    assert ft.unique_id({'entries': []}, 'x') == 'x'


def test_drop_ghosts_cures_a_poisoned_file_and_spares_honest_archives():
    """The cure for a track already carrying the doubled id: the archived copy shadowed by a
    live entry goes; an archived entry nobody re-enrolled is history and stays."""
    live = {'id': 'portfolio_top6_da7dbf', 'archived': False, 'history': [1, 2, 3]}
    ghost = {'id': 'portfolio_top6_da7dbf', 'archived': True, 'history': [1]}
    honest = {'id': 'f2adc1', 'archived': True, 'history': [1, 2]}
    track = {'entries': [ghost, live, honest]}
    assert ft.drop_ghosts(track) == 1
    assert track['entries'] == [live, honest]
    assert ft.drop_ghosts(track) == 0                    # idempotent
    assert ft.drop_ghosts({'entries': []}) == 0


@pytest.mark.gui
def test_gui_reenroll_over_archived_ghost_gets_a_fresh_id(gui_app):
    """The shipped stepper loop: a portfolio archived by an old build stays on disk under its
    deterministic name_sig id; enrolling the same portfolio again must NOT reuse that id —
    the stepper syncs by id and landed every step on the ghost (dropped as 'archived')."""
    app, rec, state = gui_app
    forms = [FORMULA, FORMULA + '#2']
    app._fwd_enroll(forms, 'portfolio_top2', 'portfolio')
    track = ft.load_track()
    assert len(track['entries']) == 1
    ghost_id = track['entries'][0]['id']
    track['entries'][0]['archived'] = True               # what the old Archive button left
    ft.save_track(track)

    app._fwd_enroll(forms, 'portfolio_top2', 'portfolio')   # find_duplicate ignores the ghost
    entries = ft.load_track()['entries']
    assert len(entries) == 2 and len({e['id'] for e in entries}) == 2
    live = next(e for e in entries if not e['archived'])
    assert live['id'] == ghost_id + '-2'
    stepped = json.loads(json.dumps(live))
    stepped['state']['equity'] = 1.0
    assert ft.sync_entry_to_disk(stepped) is True        # lands on the live entry, not the ghost
    assert next(e for e in ft.load_track()['entries'] if e['archived'])['state']['equity'] \
        == ft.START_CAPITAL
    assert app._test_tk_errors == []


@pytest.mark.gui
def test_gui_opening_the_track_drops_ghosts_once(gui_app):
    """A file poisoned before the fix is cured the first time the GUI touches the track."""
    app, _rec, state = gui_app
    live = ft.new_entry('portfolio_top6', 'portfolio', [FORMULA, FORMULA + '#2'], ['BTCUSDT'],
                        0.25, 0.001, '2024-01-01')
    ghost = json.loads(json.dumps(live))
    ghost['archived'] = True
    (state / 'forward.json').write_text(json.dumps({'entries': [ghost, live]}))
    app._fwd_migrated = False
    app._fwd_lib()
    got = ft.load_track()['entries']
    assert [e['archived'] for e in got] == [False]
    assert got[0]['id'] == live['id']

"""Sessions: the workspace-as-a-file layer. What these tests guard:

  * a snapshot must NEVER carry the subscription key (a session can travel to another
    machine or person), and restore must never overwrite THIS machine's key;
  * EVERY timeframe's library travels — including the suffixless daily files
    (library.jsonl / history.jsonl): the field bug where a 1d user's alphas were
    silently absent from every checkpoint;
  * restore is a full swap: files the archive does not carry must not survive and mix
    two workspaces — but files sessions do not own (device_id, data) are untouched;
  * ★ favorites are session-owned: a star points at a formula in a particular library, so
    it must not be inherited by a workspace mined on another basket, cut or timeframe;
  * the WHOLE board travels: status.json carries the four counters, the live log and the
    round ticker, and a restore must not leave a library and a portfolio sitting above four
    zeros — but the restored status must never claim a node is running;
  * signals.json is local: it holds PIDs of services on THIS machine and must never travel;
  * the forward track OUTLIVES sessions: a load MERGES the archive's entries into the live
    ledger instead of replacing it (paper steps happen in real time and can never be
    recomputed — a load must not kill what is stepping); same id on both sides — the longer
    history wins;
  * the current session (epoch) id lives in state/session_id: adopted from a loaded archive,
    reset by Clear all history, minted on first need — and stamped into forward entries so
    a track full of many sessions' strategies still says where each came from;
  * a failed restore rolls the workspace back byte-for-byte; a failed snapshot leaves
    no file at all (no half-written archives under session names);
  * rotation reads the MANIFEST: named sessions are forever even if the user names one
    '..._auto'; identical back-to-back auto checkpoints are skipped;
  * an archive's id IS the session's — the one in the header at save time. Two saves of one
    session share it (two photographs of the same work, told apart by date); ids differ
    where SESSIONS differ, and the epoch mint re-rolls against everything already on disk.
    The id rides in the manifest and the filename, so an archive is identifiable unopened;
  * loading the oldest auto snapshot works even when the before-load checkpoint
    rotates the pool (the target is extracted before the backup happens);
  * a malicious archive (absolute paths, ../, symlinks, FIFOs, oversized members) is
    rejected before any write.
"""
import io
import json
import os
import tarfile

import pytest

import sessions as S


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    """An isolated workspace: state dir + settings file, sessions module pointed at them.
    Holds BOTH intraday (suffixed) and daily (suffixless) state files."""
    state = tmp_path / 'state'
    state.mkdir()
    settings = tmp_path / 'gui_settings.json'
    (state / 'library_1h.jsonl').write_text('{"f":1}\n{"f":2}\n{"f":3}\n')
    (state / 'history_1h.jsonl').write_text('{"r":1}\n')
    (state / 'library.jsonl').write_text('{"d":1}\n{"d":2}\n')   # the 1d files: no suffix
    (state / 'history.jsonl').write_text('{"r":1}\n')
    (state / 'forward.json').write_text(json.dumps({'entries': [
        {'id': 'a', 'archived': False, 'state': {'equity': 9950.0}},
        {'id': 'b', 'archived': True, 'state': {'equity': 111.0}}]}))
    (state / 'portfolio.json').write_text('{"top": 6}')
    (state / 'status.json').write_text(json.dumps({
        'state': 'running', 'rounds': 7, 'trials_total': 31337, 'found': 12, 'tf': '1h',
        'current': 'round 8: exploring new…', 'events': [{'ts': '10:00', 'k': 'round', 't': '▶'}],
        'history': [{'round': 7, 'best_base': 2.1}]}))
    (state / 'signals.json').write_text('[{"port": 8799, "pid": 4242}]')   # PIDs: stay local
    (state / 'favorites.json').write_text(json.dumps({'favorites': [
        {'formula': 'tanh(low)', 'added': '2026-08-01'},
        {'formula': 'ema:12(close)', 'added': '2026-08-02'}]}))
    (state / 'device_id').write_text('MACHINE')          # identity: never in a session
    (state / 'library.jsonl.bak').write_text('old\n')    # stray backup: not ours
    settings.write_text(json.dumps({'tf': '1h', 'vault_license': 'SECRET-KEY-123',
                                    'target_vol': 0.25}))
    return str(state), str(settings)


def _grow(state, line='{"f":9}\n'):
    """Change the workspace so the next auto snapshot is not skipped as a duplicate."""
    open(os.path.join(state, 'library_1h.jsonl'), 'a').write(line)


def test_snapshot_manifest_and_secret_stripping(ws):
    state, settings = ws
    p = S.snapshot(name='my exp', note='n1', state_dir=state, settings_path=settings)
    assert os.path.exists(p)
    with tarfile.open(p) as tar:
        names = {m.name for m in tar.getmembers()}
        man = json.load(tar.extractfile('manifest.json'))
        cfg = json.load(tar.extractfile('settings.json'))
    assert 'state/device_id' not in names                # identity stays home
    assert 'state/library.jsonl.bak' not in names        # stray files stay home
    assert 'state/library_1h.jsonl' in names
    assert 'state/library.jsonl' in names                # THE field bug: 1d must travel
    assert 'state/history.jsonl' in names
    assert man['alphas'] == {'1h': 3, '1d': 2}
    assert 'state/forward.json' not in names             # the track is global: never inside
    assert 'forward' not in man                          # …so the manifest has nothing to say
    assert 'state/favorites.json' in names               # stars travel with their session
    assert man['favorites'] == 2
    assert 'state/status.json' in names                  # …and so does the top of the board
    assert man['run'] == {'rounds': 7, 'trials_total': 31337, 'found': 12, 'tf': '1h'}
    assert 'state/signals.json' not in names             # PIDs of local services: never
    assert man['name'] == 'my exp' and man['auto'] is False
    assert man['fp']                                     # content fingerprint present
    assert 'vault_license' not in cfg                    # THE invariant
    assert cfg['target_vol'] == 0.25


def test_restore_round_trip_swaps_the_whole_workspace(ws):
    state, settings = ws
    p = S.snapshot(name='base', state_dir=state, settings_path=settings)
    # workspace moves on: libraries grow, an extra timeframe appears, settings change
    open(os.path.join(state, 'library_1h.jsonl'), 'a').write('{"f":4}\n')
    open(os.path.join(state, 'library.jsonl'), 'a').write('{"d":3}\n')
    open(os.path.join(state, 'library_4h.jsonl'), 'w').write('{"x":1}\n')
    open(os.path.join(state, 'status.json'), 'w').write('{"rounds": 99}')  # a later run
    json.dump({'tf': '4h', 'vault_license': 'SECRET-KEY-123'}, open(settings, 'w'))

    man = S.restore(p, state_dir=state, settings_path=settings)
    assert man['name'] == 'base'
    lines = open(os.path.join(state, 'library_1h.jsonl')).read().strip().splitlines()
    assert len(lines) == 3                               # back to the snapshot
    assert open(os.path.join(state, 'library.jsonl')).read().count('\n') == 2
    assert not os.path.exists(os.path.join(state, 'library_4h.jsonl'))  # no workspace mixing
    assert json.load(open(os.path.join(state, 'status.json')))['rounds'] == 7   # the archive's
    cfg = json.load(open(settings))
    assert cfg['tf'] == '1h'                             # session settings won...
    assert cfg['vault_license'] == 'SECRET-KEY-123'      # ...but the machine keeps its key
    assert open(os.path.join(state, 'device_id')).read() == 'MACHINE'
    # manual-save-only: restore() must NOT create any session on its own
    assert [m.get('name') for m in S.list_sessions(state)] == ['base']


def test_restore_never_installs_a_foreign_licence(ws):
    state, settings = ws
    p = S.snapshot(state_dir=state, settings_path=settings)
    # simulate a session file crafted WITH a licence inside (not one of ours)
    evil = p + '.evil.tar.gz'
    with tarfile.open(p) as src, tarfile.open(evil, 'w:gz') as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read()
            if m.name == 'settings.json':
                d = json.loads(data)
                d['vault_license'] = 'STOLEN-KEY'
                data = json.dumps(d).encode()
                m.size = len(data)
            dst.addfile(m, io.BytesIO(data))
    json.dump({'tf': '1d'}, open(settings, 'w'))         # this machine has NO key
    S.restore(evil, state_dir=state, settings_path=settings)
    assert 'vault_license' not in json.load(open(settings))


def test_rotation_reads_the_manifest_not_the_filename(ws):
    state, settings = ws
    named = S.snapshot(name='precious', state_dir=state, settings_path=settings)
    trap = S.snapshot(name='exp_auto', state_dir=state, settings_path=settings)
    autos = []
    for i in range(8):
        _grow(state, f'{{"f":{i}}}\n')                   # distinct content each time
        autos.append(S.snapshot(auto=True, state_dir=state, settings_path=settings, keep=5))
    left = os.listdir(S.sessions_dir(state))
    assert os.path.basename(named) in left
    assert os.path.basename(trap) in left                # named '..._auto' is still named
    kinds = [m.get('auto') for m in S.list_sessions(state)]
    assert kinds.count(True) == 5                        # rotation trimmed real autos only
    assert os.path.basename(autos[-1]) in left           # the newest auto survived


def test_auto_checkpoints_skip_unchanged_workspace(ws):
    state, settings = ws
    p1 = S.snapshot(auto=True, skip_unchanged=True, state_dir=state, settings_path=settings)
    p2 = S.snapshot(auto=True, skip_unchanged=True, state_dir=state, settings_path=settings)
    assert p1 and p2 is None                             # a no-op stop makes no new file
    _grow(state)
    p3 = S.snapshot(auto=True, skip_unchanged=True, state_dir=state, settings_path=settings)
    assert p3
    assert len([n for n in os.listdir(S.sessions_dir(state)) if n.endswith('.tar.gz')]) == 2


def test_loading_the_oldest_auto_survives_the_before_load_rotation(ws):
    """The field bug: the before-load checkpoint used to rotate the pool BEFORE the
    target was read — loading the oldest of 10 autos deleted that very file."""
    state, settings = ws
    autos = []
    for i in range(10):
        _grow(state, f'{{"f":{i}}}\n')
        autos.append(S.snapshot(auto=True, state_dir=state, settings_path=settings))
    # backup=True is the opt-in path: it must checkpoint AFTER extracting the target,
    # so rotation can never eat the very file being loaded
    man = S.restore(autos[0], state_dir=state, settings_path=settings, backup=True)
    assert man['alphas']['1h'] == 4                      # base 3 + one _grow line
    lines = open(os.path.join(state, 'library_1h.jsonl')).read().strip().splitlines()
    assert len(lines) == 4


def test_failed_restore_rolls_the_workspace_back(ws, monkeypatch):
    state, settings = ws
    p = S.snapshot(name='base', state_dir=state, settings_path=settings)
    _grow(state)                                         # current differs from the archive
    before = {n: open(os.path.join(state, n), 'rb').read()
              for n in sorted(os.listdir(state)) if os.path.isfile(os.path.join(state, n))}

    real_replace = os.replace
    tripped = []
    def boom(src, dst):
        # fail ONCE while PLACING archive files into state/ (a locked/blocked target);
        # the rollback's own moves must then succeed
        if (not tripped and os.path.dirname(dst) == state
                and os.path.basename(dst) == 'portfolio.json'):
            tripped.append(1)
            raise OSError('disk went away')
        return real_replace(src, dst)
    monkeypatch.setattr(S.os, 'replace', boom)
    with pytest.raises(OSError, match='disk went away'):
        S.restore(p, state_dir=state, settings_path=settings, backup=False)
    monkeypatch.setattr(S.os, 'replace', real_replace)
    after = {n: open(os.path.join(state, n), 'rb').read()
             for n in sorted(os.listdir(state)) if os.path.isfile(os.path.join(state, n))}
    assert after == before                               # byte-for-byte rollback
    assert not [d for d in os.listdir(S.sessions_dir(state)) if d.startswith('.undo-')]


def test_failed_snapshot_leaves_no_file(ws, monkeypatch):
    state, settings = ws
    real = S._owned_state_files
    monkeypatch.setattr(S, '_owned_state_files',
                        lambda d: real(d) + [os.path.join(d, 'vanished.jsonl')])
    with pytest.raises(FileNotFoundError):
        S.snapshot(name='x', state_dir=state, settings_path=settings)
    left = os.listdir(S.sessions_dir(state))
    assert not [n for n in left if n.endswith('.tar.gz') or n.endswith('.partial')]


def test_malicious_archive_is_rejected(ws, tmp_path):
    state, settings = ws
    def evil_tar(*members):
        path = str(tmp_path / f'evil{len(os.listdir(tmp_path))}.tar.gz')
        with tarfile.open(path, 'w:gz') as tar:
            data = b'{}'
            info = tarfile.TarInfo('manifest.json'); info.size = 2
            tar.addfile(info, io.BytesIO(data))
            for m in members:
                tar.addfile(m, io.BytesIO(data) if m.isreg() else None)
        return path

    before = sorted(os.listdir(state))

    esc = tarfile.TarInfo('state/../../../evil.jsonl'); esc.size = 2
    with pytest.raises(ValueError, match='unsafe member'):
        S.restore(evil_tar(esc), state_dir=state, settings_path=settings)

    fifo = tarfile.TarInfo('state/library_1h.jsonl')     # right name, wrong beast
    fifo.type = tarfile.FIFOTYPE
    with pytest.raises(ValueError, match='unsafe member'):
        S.restore(evil_tar(fifo), state_dir=state, settings_path=settings)

    link = tarfile.TarInfo('settings.json')              # symlink to the outside world
    link.type = tarfile.SYMTYPE
    link.linkname = '/etc/passwd'
    with pytest.raises(ValueError, match='unsafe member'):
        S.restore(evil_tar(link), state_dir=state, settings_path=settings)

    assert sorted(os.listdir(state)) == before           # nothing was ever touched


def test_oversized_archive_is_rejected(ws, tmp_path, monkeypatch):
    state, settings = ws
    monkeypatch.setattr(S, 'MAX_TOTAL_BYTES', 1000)
    big = str(tmp_path / 'big.tar.gz')
    with tarfile.open(big, 'w:gz') as tar:
        info = tarfile.TarInfo('manifest.json'); info.size = 2
        tar.addfile(info, io.BytesIO(b'{}'))
        blob = b'0' * 4000                               # inflates past the (test) cap
        info = tarfile.TarInfo('state/library_1h.jsonl'); info.size = len(blob)
        tar.addfile(info, io.BytesIO(blob))
    before = sorted(os.listdir(state))
    with pytest.raises(ValueError, match='unreasonably large'):
        S.restore(big, state_dir=state, settings_path=settings)
    assert sorted(os.listdir(state)) == before


def test_rotation_ignores_unreadable_archives(ws):
    """A corrupt/foreign .tar.gz must neither occupy a keep-slot nor be deleted."""
    state, settings = ws
    junk = os.path.join(S.sessions_dir(state), '20990101-000000_junk_auto.tar.gz')
    open(junk, 'wb').write(b'not a tar at all')
    autos = []
    for i in range(4):
        _grow(state, f'{{"f":{i}}}\n')
        autos.append(S.snapshot(auto=True, state_dir=state, settings_path=settings, keep=3))
    left = os.listdir(S.sessions_dir(state))
    assert os.path.basename(junk) in left                # never deleted blindly
    assert sum(1 for m in S.list_sessions(state) if m.get('auto')) == 3   # real autos kept


def test_list_sessions_newest_first_with_sizes(ws):
    state, settings = ws
    S.snapshot(name='one', state_dir=state, settings_path=settings)
    import time
    time.sleep(1.1)                                      # filename stamp has 1s resolution
    S.snapshot(name='two', state_dir=state, settings_path=settings)
    ls = S.list_sessions(state)
    assert [m['name'] for m in ls] == ['two', 'one']
    assert all(m['size'] > 0 and m['path'].endswith('.tar.gz') for m in ls)


@pytest.mark.gui
def test_gui_manual_save_restore_and_rebuild(gui_app):
    """Manual-save-only world: a hand-saved session restores, nothing auto-saves along
    the way, and _sessions_rebuild repaints the leaderboard without the node."""
    app, rec, state = gui_app
    import alphanode_gui as G
    assert not hasattr(app, '_sessions_auto')            # the auto hook is gone for good
    lib = state / 'library_1h.jsonl'
    lib.write_text('{"f":"x","base":1.0}\n')

    saved = S.snapshot(name='by-hand', state_dir=str(state), settings_path=G.SETTINGS)
    lib.write_text('{"f":"x","base":1.0}\n{"f":"y","base":0.5}\n')   # workspace moves on...
    S.restore(saved, state_dir=str(state), settings_path=G.SETTINGS)
    assert lib.read_text().count('\n') == 1              # ...and comes back
    sdir = state / 'sessions'
    assert [n for n in os.listdir(sdir) if n.endswith('.tar.gz')] \
        == [os.path.basename(saved)]                     # no auto files appeared

    app._sessions_rebuild()                              # the window survives the swap
    assert not [c for c in rec.calls if c[0] == 'showerror']

    # the restored library must reach the LEADERBOARD with no node and no status.json:
    # the field bug where everything stayed blank until the next node run
    import time as _t
    deadline = _t.time() + 10
    while _t.time() < deadline and not app._lib_cache.get('computed'):
        app.root.update()
        _t.sleep(0.05)
    assert app._lib_cache.get('computed')
    app._refresh_leaderboard([])                         # what any poll tick now does
    app.root.update()
    assert len(app.tree.get_children()) == 1


def test_peek_reads_archive_without_touching_the_workspace(ws):
    state, settings = ws
    p = S.snapshot(name='look', state_dir=state, settings_path=settings)
    before = sorted(os.listdir(state))
    pk = S.peek(p)
    assert pk['manifest']['name'] == 'look'
    assert pk['manifest']['alphas'] == {'1h': 3, '1d': 2}
    assert 'vault_license' not in pk['settings']         # the key never even shows
    assert pk['settings']['target_vol'] == 0.25
    assert pk['portfolio'] == {'top': 6}
    assert sorted(os.listdir(state)) == before           # read-only, nothing extracted

    junk = os.path.join(S.sessions_dir(state), 'junk.tar.gz')
    open(junk, 'wb').write(b'not a tar')
    assert S.peek(junk) == {'manifest': None, 'settings': None, 'portfolio': None}


# ---- ★ favorites belong to the session that mined them -------------------------------

def test_stars_travel_with_the_session_and_do_not_leak_between_them(ws):
    """The reported problem: a star outlived the library it pointed into. Save session A
    with two stars, star something else, load A back — you get A's stars, not today's."""
    state, settings = ws
    a = S.snapshot(name='A', state_dir=state, settings_path=settings)
    json.dump({'favorites': [{'formula': 'rank(volume)'}]},
              open(os.path.join(state, 'favorites.json'), 'w'))
    b = S.snapshot(name='B', state_dir=state, settings_path=settings)

    S.restore(a, state_dir=state, settings_path=settings)
    got = json.load(open(os.path.join(state, 'favorites.json')))['favorites']
    assert [f['formula'] for f in got] == ['tanh(low)', 'ema:12(close)']

    S.restore(b, state_dir=state, settings_path=settings)
    got = json.load(open(os.path.join(state, 'favorites.json')))['favorites']
    assert [f['formula'] for f in got] == ['rank(volume)']    # B's star, not A's two


def test_a_session_saved_without_stars_restores_without_stars(ws):
    """A full swap, not a merge — the same rule every other owned file follows. Loading a
    starless workspace must not leave the previous one's ★ behind."""
    state, settings = ws
    os.remove(os.path.join(state, 'favorites.json'))
    p = S.snapshot(name='no-stars', state_dir=state, settings_path=settings)
    json.dump({'favorites': [{'formula': 'rank(volume)'}]},
              open(os.path.join(state, 'favorites.json'), 'w'))
    man = S.restore(p, state_dir=state, settings_path=settings)
    assert man['favorites'] == 0
    assert not os.path.exists(os.path.join(state, 'favorites.json'))


def test_starring_makes_the_workspace_look_changed(ws):
    """The fingerprint drives 'skip this auto checkpoint, nothing moved'. A star IS a
    change now, so a checkpoint taken after one must not be skipped as a duplicate."""
    state, settings = ws
    before = S.workspace_fingerprint(state, settings)
    doc = json.load(open(os.path.join(state, 'favorites.json')))
    doc['favorites'].append({'formula': 'rank(volume)'})
    json.dump(doc, open(os.path.join(state, 'favorites.json'), 'w'))
    assert S.workspace_fingerprint(state, settings) != before


def test_a_corrupt_favorites_file_is_counted_as_none(ws):
    """The manifest is written on every save — a hand-mangled star file must not stop one."""
    state, settings = ws
    open(os.path.join(state, 'favorites.json'), 'w').write('{ not json')
    p = S.snapshot(name='c', state_dir=state, settings_path=settings)
    with tarfile.open(p) as tar:
        man = json.load(tar.extractfile('manifest.json'))
    assert man['favorites'] == 0
    assert 'state/favorites.json' in {m.name for m in tarfile.open(p).getmembers()}


@pytest.mark.gui
def test_the_leaderboard_repaints_stars_after_a_session_load(gui_app):
    """_fav_ids is cached until something sets it to None. A restore swaps favorites.json
    under the GUI, so without the invalidation the table keeps painting the PREVIOUS
    workspace's ★ onto rows that belong to a different library."""
    app, _rec, state = gui_app
    import alphanode_gui as G
    import favorites as favdb
    lib = state / 'library_1h.jsonl'
    lib.write_text('{"formula":"tanh(low)","base":1.0}\n')
    favdb.toggle(str(state), {'formula': 'tanh(low)'}, '1h')
    assert favdb.ids(str(state)) == {favdb.alpha_id('tanh(low)')}
    starless = S.snapshot(name='starless', state_dir=str(state), settings_path=G.SETTINGS)

    app._fav_ids = {'deadbe'}                            # a star from the workspace we are
    S.restore(starless, state_dir=str(state), settings_path=G.SETTINGS)   # about to leave
    app._sessions_rebuild()
    live = app._fav_ids if app._fav_ids is not None else favdb.ids(str(state))
    assert 'deadbe' not in live                          # the stale star did not survive
    assert live == favdb.ids(str(state)) == {favdb.alpha_id('tanh(low)')}


# ---- the whole board travels: counters, live log, round ticker ------------------------

def test_the_board_comes_back_with_the_session(ws):
    """A loaded session used to show its library, portfolio and forward track above four
    zeros and an empty log: status.json — the only home of ROUNDS / FORMULAS TRIED /
    ALPHAS FOUND / BEST FITNESS and the event feed — was deliberately never archived."""
    state, settings = ws
    p = S.snapshot(name='deep-run', state_dir=state, settings_path=settings)
    json.dump({'state': 'stopped', 'rounds': 0, 'trials_total': 0, 'events': []},
              open(os.path.join(state, 'status.json'), 'w'))          # a fresh, empty run
    S.restore(p, state_dir=state, settings_path=settings)
    st = json.load(open(os.path.join(state, 'status.json')))
    assert st['rounds'] == 7 and st['trials_total'] == 31337 and st['found'] == 12
    assert st['events'] and st['history']                             # log and fitness history


def test_a_restored_status_never_claims_the_node_is_running(ws):
    """The reason status.json was transient in the first place. It is saved mid-round, so the
    archive says 'running' — restoring that verbatim would have the window reporting a search
    that is not happening. Only that field is rewritten; the history it carries is the point."""
    state, settings = ws
    p = S.snapshot(name='mid-round', state_dir=state, settings_path=settings)
    with tarfile.open(p) as tar:
        archived = json.load(tar.extractfile('state/status.json'))
    assert archived['state'] == 'running'                             # saved as it really was
    S.restore(p, state_dir=state, settings_path=settings)
    st = json.load(open(os.path.join(state, 'status.json')))
    assert st['state'] == 'stopped'
    assert st['rounds'] == 7 and st['current'] == 'round 8: exploring new…'   # kept verbatim


def test_a_stopped_status_is_restored_untouched(ws):
    state, settings = ws
    doc = json.load(open(os.path.join(state, 'status.json')))
    doc['state'] = 'stopped'
    json.dump(doc, open(os.path.join(state, 'status.json'), 'w'))
    p = S.snapshot(name='done', state_dir=state, settings_path=settings)
    S.restore(p, state_dir=state, settings_path=settings)
    assert json.load(open(os.path.join(state, 'status.json')))['state'] == 'stopped'


def test_signals_json_stays_on_this_machine(ws):
    """A PID from another machine — or another boot of this one — is either meaningless or
    somebody else's process. The registry must survive a restore untouched, not travel."""
    state, settings = ws
    p = S.snapshot(name='s', state_dir=state, settings_path=settings)
    open(os.path.join(state, 'signals.json'), 'w').write('[{"port": 8800, "pid": 99}]')
    S.restore(p, state_dir=state, settings_path=settings)
    assert json.load(open(os.path.join(state, 'signals.json')))[0]['pid'] == 99


def test_a_corrupt_status_does_not_break_a_restore(ws):
    state, settings = ws
    open(os.path.join(state, 'status.json'), 'w').write('{ not json')
    p = S.snapshot(name='bad', state_dir=state, settings_path=settings)
    man = S.restore(p, state_dir=state, settings_path=settings)
    assert man['name'] == 'bad'
    assert open(os.path.join(state, 'status.json')).read() == '{ not json'   # left as found


@pytest.mark.gui
def test_the_gui_tiles_and_log_refill_after_a_load(gui_app):
    """End to end, in the widgets: the four counters and the event feed must read the
    restored run, and the state pill must not say 'running'."""
    app, _rec, state = gui_app
    import alphanode_gui as G
    (state / 'library_1h.jsonl').write_text('{"formula":"tanh(low)","base":1.0}\n')
    (state / 'status.json').write_text(json.dumps({
        'state': 'running', 'rounds': 7, 'trials_total': 31337, 'found': 12,
        'cpu_percent': 50, 'n_jobs': 6, 'cores': 12, 'universe': 'BTCUSDT',
        'current': 'round 8: exploring new…', 'best': [],
        'events': [{'ts': '10:00', 'k': 'round', 't': '▶ round 7: explore'}],
        'history': [{'round': 7, 'best_base': 2.14}]}))
    saved = S.snapshot(name='deep', state_dir=str(state), settings_path=G.SETTINGS)

    (state / 'status.json').write_text('{"state": "stopped", "rounds": 0, "trials_total": 0}')
    S.restore(saved, state_dir=str(state), settings_path=G.SETTINGS)
    app._sessions_rebuild()
    app._poll()
    app.root.update()

    assert app.s_rounds.cget('text') == '7'
    assert app.s_trials.cget('text') == '31,337'
    assert app.s_found.cget('text') == '12'
    assert app.s_fit.cget('text') == '+2.14'             # from the restored history
    assert 'round 7: explore' in app.logbox.get('1.0', 'end')
    assert 'running' not in app.lbl_state.cget('text')   # …but nothing is actually running
    assert 'last run' in app.lbl_res.cget('text')        # and the config line says as much
    #                       (lbl_cur went with the advanced spark strip, removed 2026-09-02)


# ---- every save gets its own id --------------------------------------------------------

def test_the_archive_wears_the_id_from_the_header(ws):
    """The field report, verbatim: 'я сохранил сессию be8434, а сохранило как d47541'. The
    id the user watches is the SESSION id — the archive must show that one, not a second
    identifier minted behind their back."""
    state, settings = ws
    sid = S.current_session_id(state)
    S.snapshot(name='session', state_dir=state, settings_path=settings)
    assert S.list_sessions(state)[0]['id'] == sid


def test_two_saves_of_one_session_share_the_id_and_two_sessions_never_do(ws):
    """Two saves of the same workspace are two photographs of ONE session — same id, told
    apart by their timestamps. A different session (after Clear) gets a different id, which
    is what keeps two same-named archives distinguishable."""
    state, settings = ws
    a = S.snapshot(name='test2', state_dir=state, settings_path=settings)
    b = S.snapshot(name='test2', state_dir=state, settings_path=settings)
    ids = [m['id'] for m in S.list_sessions(state)]
    assert len(set(ids)) == 1 and a != b                 # one session, two files
    S.begin_new_session(state)
    S.snapshot(name='test2', state_dir=state, settings_path=settings)
    ids = {m['id'] for m in S.list_sessions(state)}
    assert len(ids) == 2                                 # a new SESSION is a new id


def test_the_id_is_in_the_manifest_and_in_the_filename(ws):
    """On disk, mailed, or copied into a backup folder: the file says which session it is
    without anyone opening it."""
    state, settings = ws
    p = S.snapshot(name='exp', state_dir=state, settings_path=settings)
    with tarfile.open(p) as tar:
        man = json.load(tar.extractfile('manifest.json'))
    assert man['id'] and man['id'] in os.path.basename(p)


def test_a_new_epoch_never_reuses_an_archived_id(ws, monkeypatch):
    """'Unique' must not rest on probability alone: the epoch mint re-rolls against every id
    already on disk, so a fresh session can never collide with an archived one."""
    state, settings = ws
    seq = iter(['aaaaaa', 'aaaaaa', 'aaaaaa', 'bbbbbb'])
    monkeypatch.setattr(S.secrets, 'token_hex', lambda n: next(seq))
    S.begin_new_session(state)                           # epoch 'aaaaaa'
    S.snapshot(name='one', state_dir=state, settings_path=settings)
    got = S.begin_new_session(state)                     # 'aaaaaa' twice more -> re-rolled
    assert got == 'bbbbbb'
    assert {m['id'] for m in S.list_sessions(state)} == {'aaaaaa'}


def test_the_id_survives_a_restore_round_trip(ws):
    """Restoring does not renumber anything: the archive keeps the id it was written with."""
    state, settings = ws
    p = S.snapshot(name='keep', state_dir=state, settings_path=settings)
    sid = S.list_sessions(state)[0]['id']
    man = S.restore(p, state_dir=state, settings_path=settings)
    assert man['id'] == sid
    assert S.peek(p)['manifest']['id'] == sid


def test_an_archive_from_before_ids_still_gets_a_stable_handle(ws):
    """Old archives carry no id. Rather than a blank column, one is derived from the
    filename — stable across listings, and flagged so the details panel can say so."""
    state, settings = ws
    p = S.snapshot(name='old', state_dir=state, settings_path=settings)
    # rewrite the archive without an id, exactly as an older build would have written it
    with tarfile.open(p) as tar:
        members = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
    man = json.loads(members['manifest.json'])
    man.pop('id')
    members['manifest.json'] = json.dumps(man).encode()
    with tarfile.open(p, 'w:gz') as tar:
        for n, data in members.items():
            info = tarfile.TarInfo(n)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    listed = S.list_sessions(state)[0]
    assert len(listed['id']) == 6 and listed['id_derived'] is True
    assert S.list_sessions(state)[0]['id'] == listed['id']        # stable across listings


@pytest.mark.gui
def test_the_sessions_window_shows_the_id_column(gui_app):
    app, _rec, state = gui_app
    import alphanode_gui as G
    (state / 'library_1h.jsonl').write_text('{"formula":"x","base":1.0}\n')
    S.snapshot(name='test2', state_dir=str(state), settings_path=G.SETTINGS)
    S.snapshot(name='test2', state_dir=str(state), settings_path=G.SETTINGS)
    app._sessions_open()
    app.root.update()
    wins = [w for w in app.root.winfo_children()
            if w.winfo_class() in ('Toplevel', 'CTkToplevel') and w.title() == 'Sessions']
    assert wins, 'the Sessions window did not open'
    win = wins[-1]

    def _trees(w):
        out = [w] if w.winfo_class() == 'Treeview' else []
        for kid in w.winfo_children():
            out += _trees(kid)
        return out

    trees = _trees(win)
    assert trees, 'no sessions table'
    tree = trees[0]
    assert tree['columns'][0] == 'id'                    # first: it is the row's name
    ids = [tree.set(i, 'id') for i in tree.get_children()]
    names = [tree.set(i, 'name') for i in tree.get_children()]
    assert names == ['test2', 'test2']
    assert ids == [app._session_id()] * 2                # both photographs of THIS session
    win.destroy()


# ---- the forward track is GLOBAL — sessions never touch it; the epoch id says who enrolled what

def _fwd(entries):
    return {'entries': entries}


def _e(eid, steps, **over):
    d = {'id': eid, 'archived': False,
         'history': [{'date': f'2026-08-{i+1:02d}', 'equity': 10000 + i} for i in range(steps)],
         'state': {'equity': 10000.0}}
    d.update(over)
    return d


def _repack_with(p, name, data):
    """Rewrite archive `p` with one extra member — the way an archive from before the
    global-track rule looks (it carried state/forward.json)."""
    with tarfile.open(p) as tar:
        members = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
    members[name] = data
    with tarfile.open(p, 'w:gz') as tar:
        for n, d in members.items():
            info = tarfile.TarInfo(n)
            info.size = len(d)
            tar.addfile(info, io.BytesIO(d))


def test_snapshot_never_archives_the_forward_track(ws):
    """The paper bots belong to the node, not to a session: a save carries no forward.json,
    and stepping (which rewrites the file every bar) must not make a checkpoint look 'changed'."""
    state, settings = ws
    p = S.snapshot(name='no-track', state_dir=state, settings_path=settings)
    fp = S.workspace_fingerprint(state, settings)      # after the save minted session_id
    with tarfile.open(p) as tar:
        assert 'state/forward.json' not in {m.name for m in tar.getmembers()}
    json.dump(_fwd([_e('stepped', 40)]), open(os.path.join(state, 'forward.json'), 'w'))
    assert S.workspace_fingerprint(state, settings) == fp


def test_restore_leaves_the_live_track_byte_identical(ws):
    """Load swaps the library — and leaves the ledger alone, byte for byte."""
    state, settings = ws
    p = S.snapshot(name='old', state_dir=state, settings_path=settings)
    open(os.path.join(state, 'library_1h.jsonl'), 'w').write('{"f":99}\n')
    fwd = os.path.join(state, 'forward.json')
    json.dump(_fwd([_e('both01', 5), _e('live01', 1)]), open(fwd, 'w'))
    before = open(fwd, 'rb').read()
    S.restore(p, state_dir=state, settings_path=settings)
    assert open(fwd, 'rb').read() == before
    assert open(os.path.join(state, 'library_1h.jsonl')).read().count('\n') == 3   # swapped


def test_a_legacy_archive_with_a_forward_member_is_ignored_not_rejected(ws):
    """Archives saved before the global-track rule carry state/forward.json: they must still
    load (everything else restores), and that member must never land over the live file."""
    state, settings = ws
    p = S.snapshot(name='legacy', state_dir=state, settings_path=settings)
    foreign = json.dumps(_fwd([_e('deleted-long-ago', 50)])).encode()
    _repack_with(p, 'state/forward.json', foreign)
    fwd = os.path.join(state, 'forward.json')
    json.dump(_fwd([_e('live01', 3)]), open(fwd, 'w'))
    before = open(fwd, 'rb').read()
    open(os.path.join(state, 'library_1h.jsonl'), 'w').write('{"f":99}\n')
    S.restore(p, state_dir=state, settings_path=settings)          # must not raise
    assert open(fwd, 'rb').read() == before                         # the ghost stays buried
    assert open(os.path.join(state, 'library_1h.jsonl')).read().count('\n') == 3
    assert not [n for n in os.listdir(S.sessions_dir(state)) if n.startswith('.undo-')]


def test_restore_with_no_live_track_creates_none(ws):
    """A legacy archive on a machine that has no track yet does not seed one either —
    the rule is 'never written', not 'written only when absent'."""
    state, settings = ws
    p = S.snapshot(name='legacy', state_dir=state, settings_path=settings)
    _repack_with(p, 'state/forward.json', json.dumps(_fwd([_e('x', 2)])).encode())
    os.remove(os.path.join(state, 'forward.json'))
    S.restore(p, state_dir=state, settings_path=settings)
    assert not os.path.exists(os.path.join(state, 'forward.json'))


def test_manifest_no_longer_reports_forward_stats(ws):
    state, settings = ws
    S.snapshot(name='s1', state_dir=state, settings_path=settings)
    assert 'forward' not in S.list_sessions(state)[0]


def test_a_failed_restore_still_leaves_the_track_alone(ws, monkeypatch):
    state, settings = ws
    fwd = os.path.join(state, 'forward.json')
    before = open(fwd, 'rb').read()
    p = S.snapshot(name='boom', state_dir=state, settings_path=settings)
    real = S.os.replace

    def boom(src, dst):
        if dst.endswith('library.jsonl'):
            raise RuntimeError('disk boom')
        return real(src, dst)
    monkeypatch.setattr(S.os, 'replace', boom)
    with pytest.raises(RuntimeError):
        S.restore(p, state_dir=state, settings_path=settings)
    assert open(fwd, 'rb').read() == before


@pytest.mark.gui
def test_sessions_rebuild_reruns_the_track_cleanup(gui_app):
    app, _rec, _state = gui_app
    app._fwd_migrated = True
    app._sessions_rebuild()
    assert app._fwd_migrated is False
    app._fwd_lib()
    assert app._fwd_migrated is True


def test_current_session_id_is_minted_once_and_reset_deliberately(ws):
    state, _settings = ws
    a = S.current_session_id(state)
    assert a == S.current_session_id(state)              # stable across calls
    assert len(a) == 6 and all(c in '0123456789abcdef' for c in a)
    b = S.begin_new_session(state)
    assert b != a and b == S.current_session_id(state)
    open(os.path.join(state, 'session_id'), 'w').write('NOT-AN-ID')
    c = S.current_session_id(state)                      # corrupt file: re-minted, not trusted
    assert c not in (a, 'NOT-AN-ID') and len(c) == 6


def test_restore_adopts_the_archives_epoch(ws):
    """Loading a session means CONTINUING it: the workspace's current session id becomes the
    archive's, so forward entries enrolled after the load carry the loaded session's id."""
    state, settings = ws
    a = S.current_session_id(state)
    p = S.snapshot(name='epoch-a', state_dir=state, settings_path=settings)
    S.begin_new_session(state)
    assert S.current_session_id(state) != a
    S.restore(p, state_dir=state, settings_path=settings)
    assert S.current_session_id(state) == a


def test_the_manifest_id_and_session_agree(ws):
    """One id space: the manifest's 'id' and 'session' are the same value now — 'session'
    stays only so archives from the two-id interlude still render in the details panel."""
    state, settings = ws
    S.snapshot(name='s1', state_dir=state, settings_path=settings)
    row = S.list_sessions(state)[0]
    assert row['id'] == row['session'] == S.current_session_id(state)


def test_an_archive_from_before_epochs_starts_a_fresh_one(ws):
    """No session_id member inside → the swap leaves none behind → the next call mints a
    new epoch rather than inheriting the abandoned workspace's."""
    state, settings = ws
    p = S.snapshot(name='pre-epoch', state_dir=state, settings_path=settings)
    with tarfile.open(p) as tar:
        members = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
    members.pop('state/session_id')
    with tarfile.open(p, 'w:gz') as tar:
        for n, data in members.items():
            info = tarfile.TarInfo(n)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    before = S.current_session_id(state)
    S.restore(p, state_dir=state, settings_path=settings)
    assert not os.path.exists(os.path.join(state, 'session_id'))
    assert S.current_session_id(state) != before


@pytest.mark.gui
def test_clear_all_history_spares_the_track_and_mints_a_new_session(gui_app, monkeypatch):
    """Clear kills the library; the forward track keeps stepping — and what follows the
    clear is a NEW session, visible in the header, so entries enrolled before and after
    carry different stamps."""
    import types
    app, _rec, state = gui_app
    import alphanode_gui as G
    (state / 'library_1h.jsonl').write_text('{"formula":"x","base":1.0}\n')
    (state / 'forward.json').write_text(json.dumps(_fwd([_e('aaaa01', 2)])))
    sid_before = app._session_id()
    assert f'session {sid_before}' == app.sid_lbl.cget('text')
    yes = types.SimpleNamespace(askyesno=lambda *a, **k: True,
                                askokcancel=lambda *a, **k: True,
                                showinfo=lambda *a, **k: None,
                                showwarning=lambda *a, **k: None,
                                showerror=lambda *a, **k: None)
    monkeypatch.setattr(G, 'messagebox', yes)
    app._wipe_history()
    assert (state / 'forward.json').exists()             # the track survived the clear
    assert not (state / 'library_1h.jsonl').exists()     # the library did not
    sid_after = app._session_id()
    assert sid_after != sid_before                       # a fresh epoch
    assert app.sid_lbl.cget('text') == f'session {sid_after}'


@pytest.mark.gui
def test_the_forward_table_shows_each_entrys_session(gui_app):
    app, _rec, state = gui_app
    import forward_track as ft
    e = ft.new_entry('aaaa01', 'alpha', ['tanh(low)'], ['BTCUSDT'], 0.25, 0.001, '2024-01-01',
                     entry_id='aaaa01')
    legacy = ft.new_entry('bbbb02', 'alpha', ['rank(v)'], ['ETHUSDT'], 0.25, 0.001, '2024-01-01',
                          entry_id='bbbb02')
    legacy.pop('session')                                # enrolled before the stamp existed
    (state / 'forward.json').write_text(json.dumps(_fwd([e, legacy])))
    app._fwd_refresh()
    app.root.update()
    assert 'session' in app.fwd_tree['columns']
    assert app.fwd_tree.set('aaaa01', 'session') == app._session_id()
    assert app.fwd_tree.set('bbbb02', 'session') == '—'  # honest dash, not a blank


# ---- review findings, pinned -----------------------------------------------------------

def test_an_explicit_session_stamp_wins_over_the_module_default(tmp_path, monkeypatch):
    """The GUI passes its own session id: with ALPHANODE_STATE_DIR exported, the module-level
    default could read a DIFFERENT directory's epoch than the header shows."""
    monkeypatch.setenv('ALPHANODE_STATE_DIR', str(tmp_path))
    import forward_track as ft
    e = ft.new_entry('a', 'alpha', ['tanh(low)'], ['BTCUSDT'], 0.25, 0.001, '2024-01-01',
                     session='cafe01')
    assert e['session'] == 'cafe01'


@pytest.mark.gui
def test_a_running_forward_step_does_not_block_a_session_load(gui_app, monkeypatch):
    """Load never touches the global track, so the stepper (which writes nothing else) is no
    reason to refuse — the old 'Not now — a forward-track step is running' dialog is gone."""
    import types
    app, rec, state = gui_app
    import alphanode_gui as G
    S.snapshot(name='s', state_dir=str(state), settings_path=G.SETTINGS)
    app._fwd_proc = types.SimpleNamespace(poll=lambda: None)   # a step in flight
    seen = {}
    monkeypatch.setattr(S, 'restore', lambda path, **k: (seen.setdefault('path', path), {})[1])
    monkeypatch.setattr(app, '_sessions_rebuild', lambda: seen.setdefault('rebuilt', True))
    yes = types.SimpleNamespace(askyesno=lambda *a, **k: True, showinfo=lambda *a, **k: None,
                                showwarning=lambda *a, **k: rec.calls.append(('showwarning',) + a),
                                showerror=lambda *a, **k: rec.calls.append(('showerror',) + a),
                                askokcancel=lambda *a, **k: True)
    monkeypatch.setattr(G, 'messagebox', yes)
    app._sessions_open()
    app.root.update()
    win = [w for w in app.root.winfo_children()
           if w.winfo_class() in ('Toplevel', 'CTkToplevel') and w.title() == 'Sessions'][-1]

    def walk(w):
        yield w
        for kid in w.winfo_children():
            yield from walk(kid)
    tree = next(w for w in walk(win) if w.winfo_class() == 'Treeview')
    tree.selection_set(tree.get_children()[0])
    import customtkinter as ctk
    btn = next(w for w in walk(win) if isinstance(w, ctk.CTkButton)
               and w.cget('text') == 'Load selected')
    btn.invoke()
    assert seen.get('path', '').endswith('.tar.gz') and seen.get('rebuilt')
    assert not [c for c in rec.calls if c[0] == 'showerror' or 'Not now' in str(c)]
    assert app._test_tk_errors == []

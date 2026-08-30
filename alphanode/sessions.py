"""Sessions: the whole mining workspace as one restorable file.

A session is a tar.gz snapshot of everything a user would call "my setup": the sealed
formula libraries (every timeframe, including the suffixless daily files), round history,
the built portfolio, the ★ favorites and the app settings — plus a manifest that lets a
list of sessions read like a story (when, what timeframes, how many alphas). Market data
is NOT included (re-fetchable, heavy), neither are machine identity files
(device_id/node_id), nor the subscription key: a session may travel to another machine or
another person, and the licence must never ride along (SECRET_KEYS is stripped on save and
preserved on restore). The FORWARD TRACK is not part of a session either: it is one
global, append-only ledger per node that every session shares (see the note below).

Safety model, in the order things can go wrong:
  * snapshots are written to a .partial file and os.replace()d into place — a crash or
    a killed thread never leaves a half-archive under a session name;
  * the app creates sessions only when the user asks (Save current…) — there is no
    background auto-saving; the auto/rotate/skip_unchanged machinery below stays for
    callers that opt in;
  * rotation classifies auto vs named by the MANIFEST, never by the filename — a user
    naming a session 'my_auto' keeps it forever; unreadable archives are neither counted
    against the keep-window nor deleted;
  * restore() extracts and validates the target FIRST, then (backup=True) checkpoints
    the current workspace, then swaps files transactionally: originals are parked in an
    undo dir and put back if anything fails mid-swap;
  * archive members must be regular files with the exact names a snapshot writes;
    absolute paths, ../, links, FIFOs/devices and oversized archives are rejected
    before a single byte lands in the workspace.

Honesty note: sessions never touch the forward track's clock — loading one changes the
library, the portfolio and the counters, while the paper bots keep stepping on the same
global ledger as if nothing happened. Nothing is ever re-computed backwards.
"""
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import tarfile
import tempfile
import threading
from datetime import datetime, timezone

import apppaths
from version import __version__

# state files a session owns (basename patterns); everything else in state/ stays local.
# The timeframe suffix is OPTIONAL: daily files are plain library.jsonl / history.jsonl.
_STATE_PATTERNS = (re.compile(r'^library(_[A-Za-z0-9]+)?\.jsonl$'),
                   re.compile(r'^history(_[A-Za-z0-9]+)?\.jsonl$'),
                   re.compile(r'^portfolio\.json$'),
                   re.compile(r'^favorites\.json$'),
                   re.compile(r'^status\.json$'),
                   re.compile(r'^session_id$'))
# members an OLDER archive may carry that the workspace no longer owns: skipped on restore —
# never extracted, never written over the live file (forward.json — see the note below)
_LEGACY_SKIP = (re.compile(r'^forward\.json$'),)
# favorites.json is session-owned. A star points at a formula in the library,
# and it used to outlive the library it pointed into: load a different workspace and the
# ★ list still held rows mined on another basket, another cut, sometimes another timeframe.
# Now a session carries its own stars — saving keeps them, loading swaps them. Restore is a
# wholesale swap of owned files, so loading an archive written before this change (it has
# no favorites.json in it) leaves the workspace with none; that is the same rule every
# other owned file follows, and the load dialog says the workspace is replaced.
# status.json is session-owned too. It was 'transient' — never archived, wiped on restore —
# because it describes a RUNNING node, and a restored 'running' would have the window claiming
# a search that is not happening. But it is also the only home of what the top of the board
# shows: ROUNDS, FORMULAS TRIED, ALPHAS FOUND, BEST FITNESS, the live log and the round ticker.
# Dropping it meant a loaded session came back with its library, portfolio and forward track
# intact above four zeros and an empty log. So it travels, and restore neutralises the one
# field that could lie (see _settle_status); the GUI already dims a non-live status and
# prefixes it 'last run —'.
#
# signals.json stays local on purpose and must not be added here: it is a registry of PIDs of
# signal services running on THIS machine, and a pid from another machine or another boot is
# either meaningless or somebody else's process.
#
# session_id names the CURRENT working session — the epoch of work between beginnings.
# An epoch begins at first run, at 'Clear all history' (begin_new_session), or by loading an
# archive (this file is owned, so the swap adopts the archive's epoch; an archive from before
# epochs carries none, and the next current_session_id() call honestly mints a fresh one).
# Reopening the app or pressing Start again is NOT a new session — it continues the same work.
# Forward-track entries stamp this id at enrollment, which is what says where each came
# from once many sessions have enrolled into the one shared track.
#
# forward.json is GLOBAL — deliberately NOT in _STATE_PATTERNS. The track is a live,
# append-only ledger whose steps happen in real time and can never be recomputed; it belongs
# to the node, not to a session. So it is neither archived on save nor parked, swapped or
# merged on load, and it does not enter the workspace fingerprint. Archives written before
# this rule still carry a state/forward.json member: _safe_members skips it (never extracted).
# Paper bots therefore survive every Save / Load / Clear-all-history untouched, and a deleted
# bot stays deleted — no older archive can bring it back.
SECRET_KEYS = ('vault_license',)                         # never inside an archive

MAX_MEMBERS = 64                                         # a snapshot writes ~a dozen
MAX_TOTAL_BYTES = 512 * 1024 * 1024                      # declared unpacked size cap
MAX_META_BYTES = 1024 * 1024                             # manifest/settings read cap


def sessions_dir(state_dir=None):
    d = os.path.join(state_dir or apppaths.state_dir(), 'sessions')
    os.makedirs(d, exist_ok=True)
    return d


def _owned_state_files(state_dir):
    out = []
    try:
        names = sorted(os.listdir(state_dir))
    except OSError:
        return out
    for n in names:
        p = os.path.join(state_dir, n)
        if os.path.isfile(p) and any(pt.match(n) for pt in _STATE_PATTERNS):
            out.append(p)
    return out


def _clean_settings(settings_path):
    """The settings dict as it may travel: secrets stripped."""
    try:
        cfg = json.load(open(settings_path, encoding='utf-8'))
    except Exception:                                    # noqa: BLE001
        return {}
    for k in SECRET_KEYS:
        cfg.pop(k, None)
    return cfg


def workspace_fingerprint(state_dir, settings_path):
    """Content hash of everything a snapshot would carry — for skipping no-op checkpoints."""
    h = hashlib.sha1()
    for f in _owned_state_files(state_dir):
        h.update(os.path.basename(f).encode())
        try:
            with open(f, 'rb') as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b''):
                    h.update(chunk)
        except OSError:
            h.update(b'?')
    h.update(json.dumps(_clean_settings(settings_path), sort_keys=True).encode())
    return h.hexdigest()


def _slug(name):
    s = re.sub(r'[^A-Za-z0-9а-яА-ЯіїєґІЇЄҐ_-]+', '-', (name or '').strip())[:40].strip('-')
    return s


def _mint_sid(state_dir):
    """A fresh 6-hex epoch id, re-rolled against every id already on disk — the archives'
    and their recorded sessions' — so no two SESSIONS ever share one. With a handful of
    archives the re-roll never fires, but 'unique' should not be a promise made by
    probability alone. Six characters: the same shape an alpha, a forward entry and a
    portfolio member carry, so ids read alike everywhere."""
    taken = set()
    for m in list_sessions(state_dir):
        taken.add(m.get('id'))
        taken.add(m.get('session'))
    for _ in range(64):
        sid = secrets.token_hex(3)
        if sid not in taken:
            return sid
    return secrets.token_hex(6)                          # 64 collisions in a row: widen, not loop


_SID_RE = re.compile(r'^[0-9a-f]{6,12}$')


def current_session_id(state_dir=None):
    """The id of the CURRENT working session, minted on first need and kept in
    state/session_id. Six hex characters — the same shape an alpha, an archive and a
    portfolio member carry, so ids read alike everywhere. Atomic write + re-read: if two
    processes mint at once (the GUI and a CLI enroll), the write that lands last wins for
    both readers."""
    state_dir = state_dir or apppaths.state_dir()
    path = os.path.join(state_dir, 'session_id')
    try:
        sid = open(path, encoding='utf-8').read().strip()
    except OSError:
        sid = ''
    if _SID_RE.match(sid):
        return sid
    fresh = _mint_sid(state_dir)
    try:
        fd, tmp = tempfile.mkstemp(dir=state_dir, prefix='.sid-')
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(fresh)
        os.replace(tmp, path)
    except OSError:
        return fresh                                     # unwritable dir: an id for this call
    try:
        got = open(path, encoding='utf-8').read().strip()
        if _SID_RE.match(got):
            return got
    except OSError:
        pass
    return fresh


def begin_new_session(state_dir=None):
    """Start a NEW epoch — 'Clear all history' calls this. The search that follows is a
    different session, and forward entries enrolled from it must say so."""
    state_dir = state_dir or apppaths.state_dir()
    try:
        os.remove(os.path.join(state_dir, 'session_id'))
    except OSError:
        pass
    return current_session_id(state_dir)



def build_manifest(name, note, auto, state_dir, settings_path, sid=''):
    alphas = {}
    for f in _owned_state_files(state_dir):
        b = os.path.basename(f)
        if b.startswith('library'):
            tf = b[len('library_'):-len('.jsonl')] if b.startswith('library_') else '1d'
            try:
                alphas[tf] = sum(1 for line in open(f, encoding='utf-8') if line.strip())
            except OSError:
                alphas[tf] = 0
    try:
        doc = json.load(open(os.path.join(state_dir, 'favorites.json'), encoding='utf-8'))
        favs = sum(1 for f in doc.get('favorites', []) if isinstance(f, dict) and f.get('formula'))
    except Exception:                                    # noqa: BLE001 — absent/corrupt = none
        favs = 0
    run = {}
    try:                                                 # how far the search got, for the list
        st = json.load(open(os.path.join(state_dir, 'status.json'), encoding='utf-8'))
        run = {k: st.get(k) for k in ('rounds', 'trials_total', 'found')}
        run['tf'] = st.get('tf')
    except Exception:                                    # noqa: BLE001 — no run yet
        pass
    return {'id': sid, 'session': current_session_id(state_dir),
            'name': name or '', 'note': note or '', 'auto': bool(auto),
            'created': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'version': __version__, 'alphas': alphas, 'favorites': favs,
            'run': run, 'fp': workspace_fingerprint(state_dir, settings_path)}


def _read_manifest(path):
    """The archive's manifest, or None if the file is not a readable session archive.
    Reads are capped so a hostile archive cannot balloon memory from a mere listing."""
    try:
        with tarfile.open(path, 'r:gz') as tar:
            f = tar.extractfile('manifest.json')
            if f is None:
                return None
            raw = f.read(MAX_META_BYTES + 1)
            if len(raw) > MAX_META_BYTES:
                return None
            man = json.loads(raw)
            if not isinstance(man, dict):
                return None
            if not man.get('id'):                        # archived before ids existed: derive a
                man['id'] = hashlib.md5(                 # stable one from the filename, so the
                    os.path.basename(path).encode()).hexdigest()[:6]   # column is never blank
                man['id_derived'] = True                 # …and the details panel can say so
            return man
    except Exception:                                    # noqa: BLE001
        return None


def _newest_fingerprint(state_dir):
    d = sessions_dir(state_dir)
    newest, newest_mt = None, -1.0
    try:
        names = os.listdir(d)
    except OSError:
        return None
    for n in names:
        if not n.endswith('.tar.gz'):
            continue
        p = os.path.join(d, n)
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if mt > newest_mt:
            newest, newest_mt = p, mt
    if newest is None:
        return None
    man = _read_manifest(newest)
    return man.get('fp') if man else None


def snapshot(name='', note='', auto=False, state_dir=None, settings_path=None, keep=10,
             skip_unchanged=False):
    """Write a session archive; auto snapshots also rotate (oldest beyond `keep` go).
    With skip_unchanged=True, returns None instead of duplicating the newest session
    (same content fingerprint) or archiving a workspace that owns no files at all."""
    state_dir = state_dir or apppaths.state_dir()
    settings_path = settings_path or apppaths.settings_file()
    sid = current_session_id(state_dir)                  # ensure the epoch file exists: the
    if skip_unchanged:                                   # fingerprint and the tar both carry it
        if not _owned_state_files(state_dir):
            return None
        if workspace_fingerprint(state_dir, settings_path) == _newest_fingerprint(state_dir):
            return None
    # The archive's id IS the session's — the one in the window header when it was saved.
    # There is deliberately no separate per-save id: the user saves "session be8434" and must
    # see be8434 in the list, not a second identifier minted behind their back (which is
    # exactly how it read in the field). Two saves of one session share the id — they are two
    # photographs of the same work, told apart by their timestamps; ids differ where SESSIONS
    # differ, and the epoch mint re-rolls against everything already on disk. The id rides in
    # the FILENAME too, so an archive is identifiable without being opened. The
    # second-resolution stamp still guards two snapshots inside one second.
    man = build_manifest(name, note, auto, state_dir, settings_path, sid=sid)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    slug = _slug(name)
    for i in range(1000):
        mid = f'-{i}' if i else ''
        base = (f'{stamp}{"_" + slug if slug else ""}{mid}_{sid}'
                f'{"_auto" if auto else ""}.tar.gz')
        path = os.path.join(sessions_dir(state_dir), base)
        if not os.path.exists(path):
            break
    # write-then-rename: a crash or killed checkpoint thread never leaves a half-archive
    # under a session name (rotation and the list only ever see complete files)
    part = f'{path}.{os.getpid()}-{threading.get_ident()}.partial'
    try:
        with tarfile.open(part, 'w:gz') as tar:
            def _add_bytes(arcname, data):
                info = tarfile.TarInfo(arcname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            _add_bytes('manifest.json', json.dumps(man, ensure_ascii=False, indent=1).encode())
            _add_bytes('settings.json', json.dumps(_clean_settings(settings_path),
                                                   ensure_ascii=False, indent=1).encode())
            for f in _owned_state_files(state_dir):
                tar.add(f, arcname='state/' + os.path.basename(f))
        os.replace(part, path)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    if auto:
        rotate(state_dir, keep=keep)
    return path


def list_sessions(state_dir=None):
    """Newest first: [{path, size, **manifest}]. Unreadable archives are skipped."""
    state_dir = state_dir or apppaths.state_dir()
    out = []
    d = sessions_dir(state_dir)
    for n in sorted(os.listdir(d), reverse=True):
        if not n.endswith('.tar.gz'):
            continue
        p = os.path.join(d, n)
        man = _read_manifest(p)
        if man is None:
            continue
        man.update(path=p, size=os.path.getsize(p))
        out.append(man)
    return out


def _safe_members(tar):
    """Only the names a snapshot writes, only regular files, only sane sizes; absolute
    paths, .., links, FIFOs/devices and oversized archives are rejected outright."""
    members = tar.getmembers()
    if len(members) > MAX_MEMBERS:
        raise ValueError(f'session archive has too many members ({len(members)})')
    total = 0
    ok = []
    for m in members:
        n = m.name
        if not m.isreg():
            raise ValueError(f'unsafe member in session archive (not a regular file): {n!r}')
        total += m.size
        if total > MAX_TOTAL_BYTES:
            raise ValueError('session archive is unreasonably large unpacked')
        if n in ('manifest.json', 'settings.json'):
            if m.size > MAX_META_BYTES:
                raise ValueError(f'unsafe member in session archive (oversized): {n!r}')
            ok.append(m)
            continue
        b = os.path.basename(n)
        if n == 'state/' + b and any(p.match(b) for p in _LEGACY_SKIP):
            continue                                     # pre-global-track archive: ignored
        if n == 'state/' + b and any(p.match(b) for p in _STATE_PATTERNS):
            ok.append(m)
            continue
        raise ValueError(f'unsafe member in session archive: {n!r}')
    return ok


def _settle_status(state_dir):
    """A restored status.json describes the node as it was when the session was SAVED — very
    possibly mid-round. Nothing is running now, so the one field that would lie is rewritten:
    'running'/'starting' becomes 'stopped'. Everything else — the counters, the event log, the
    champion list, the round ticker — is history and is kept, which is the whole point of
    carrying the file. Unreadable or absent: leave it alone, the GUI copes with either."""
    p = os.path.join(state_dir, 'status.json')
    try:
        with open(p, encoding='utf-8') as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict) or doc.get('state') not in ('running', 'starting'):
            return
        doc['state'] = 'stopped'
        with open(p, 'w', encoding='utf-8') as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2, default=str)
    except Exception:                                    # noqa: BLE001 — absent/corrupt/read-only
        pass


def restore(path, state_dir=None, settings_path=None, backup=False):
    """Swap the workspace for the archived one. The caller must have stopped the node
    (it appends to the library mid-round). Order of operations is the safety story:
    the target is extracted and validated FIRST; only then, and only with backup=True
    (an opt-in — the app itself never snapshots without being asked), is the current
    workspace checkpointed; then files swap transactionally (originals parked in an
    undo dir and put back if anything fails mid-swap). Settings are replaced EXCEPT
    the secrets, which stay from this machine. Returns the manifest."""
    state_dir = state_dir or apppaths.state_dir()
    settings_path = settings_path or apppaths.settings_file()
    with tarfile.open(path, 'r:gz') as tar:
        members = _safe_members(tar)
        f = tar.extractfile('manifest.json')
        man = json.loads(f.read(MAX_META_BYTES)) if f else {}
        with tempfile.TemporaryDirectory(dir=sessions_dir(state_dir)) as tmp:
            tar.extractall(tmp, members=members)
            if backup:                                   # AFTER the target is safely out
                snapshot(name='before-load', auto=True, state_dir=state_dir,
                         settings_path=settings_path, skip_unchanged=True)
            # transactional swap: the current session-owned files are parked, not deleted —
            # a mid-swap failure puts every one of them back
            undo = tempfile.mkdtemp(prefix='.undo-', dir=sessions_dir(state_dir))
            parked = []                                  # (parked_path, original_path)
            try:
                for f in _owned_state_files(state_dir):
                    b = os.path.join(undo, os.path.basename(f))
                    os.replace(f, b)
                    parked.append((b, f))
                src_state = os.path.join(tmp, 'state')
                if os.path.isdir(src_state):
                    for b in sorted(os.listdir(src_state)):
                        os.replace(os.path.join(src_state, b), os.path.join(state_dir, b))
                # forward.json is global: not owned, so never parked above, and the archive's
                # copy (if an old one carries it) was skipped by _safe_members — untouched
            except BaseException:
                for f in _owned_state_files(state_dir):  # anything already placed is new
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                for b, f in parked:
                    try:
                        os.replace(b, f)
                    except OSError:
                        pass
                shutil.rmtree(undo, ignore_errors=True)
                raise
            shutil.rmtree(undo, ignore_errors=True)
            _settle_status(state_dir)
            try:
                incoming = json.load(open(os.path.join(tmp, 'settings.json'), encoding='utf-8'))
            except Exception:                            # noqa: BLE001
                incoming = None
            if isinstance(incoming, dict):
                try:
                    cur = json.load(open(settings_path, encoding='utf-8'))
                except Exception:                        # noqa: BLE001
                    cur = {}
                for k in SECRET_KEYS:                    # the machine keeps its own licence
                    if k in cur:
                        incoming[k] = cur[k]
                    else:
                        incoming.pop(k, None)
                tmp_s = settings_path + '.tmp'
                json.dump(incoming, open(tmp_s, 'w', encoding='utf-8'), indent=2)
                os.replace(tmp_s, settings_path)
    return man


def peek(path):
    """Manifest, travelling settings and the built portfolio from an archive — read
    in-memory with size caps, for a details view. Nothing touches the workspace."""
    out = {'manifest': None, 'settings': None, 'portfolio': None}
    try:
        with tarfile.open(path, 'r:gz') as tar:
            def _get(name):
                try:
                    f = tar.extractfile(name)
                except KeyError:
                    return None
                if f is None:
                    return None
                raw = f.read(MAX_META_BYTES + 1)
                if len(raw) > MAX_META_BYTES:
                    return None
                try:
                    return json.loads(raw)
                except ValueError:
                    return None
            out['manifest'] = _get('manifest.json')
            out['settings'] = _get('settings.json')
            out['portfolio'] = _get('state/portfolio.json')
    except Exception:                                    # noqa: BLE001 — a details view
        pass                                             # never breaks on a bad archive
    return out


def rotate(state_dir=None, keep=10):
    """Auto snapshots beyond the newest `keep` are deleted; named ones are never touched.
    Auto-ness comes from the MANIFEST — a user naming a session '..._auto' cannot end up
    in the rotation pool. Archives whose manifest cannot be read are left alone and do
    not occupy keep-slots. Stale .partial leftovers (crashed writes) are swept."""
    state_dir = state_dir or apppaths.state_dir()
    d = sessions_dir(state_dir)
    import time
    autos = []
    for n in os.listdir(d):
        p = os.path.join(d, n)
        if n.endswith('.partial'):
            try:                                         # only stale ones — a live writer
                if time.time() - os.path.getmtime(p) > 3600:   # finishes within seconds
                    os.remove(p)
            except OSError:
                pass
            continue
        if not n.endswith('.tar.gz'):
            continue
        man = _read_manifest(p)
        if man and man.get('auto'):
            try:
                autos.append((os.path.getmtime(p), p))
            except OSError:
                continue
    autos.sort(reverse=True)
    for _, p in autos[keep:]:
        try:
            os.remove(p)
        except OSError:
            pass

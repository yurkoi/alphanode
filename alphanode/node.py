"""AlphaNode — background trading-strategy search node.

Runs the evolutionary search (evolution/ engine) in ROUNDS non-stop, accumulating discovered
champions into a library (dedup), consuming a set percentage of the machine's resources. Minimal
interface — a live status page at http://localhost:PORT.

Config — via ALPHANODE_* environment variables (see alphanode.env), layered over
evolution/config.ini (which provides the TRAIN/VAL/TEST segments, vol, penalties, etc.).
"""
import os
import re
import sys
import json
import math
import time
import hashlib
import signal
import threading
import http.server
import socketserver
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
EVO = os.path.join(PROJ, 'evolution')
if EVO not in sys.path:
    sys.path.insert(0, EVO)

import warnings                                        # noqa: E402
warnings.filterwarnings('ignore')
import numpy as np                                     # noqa: E402
np.seterr(divide='ignore', invalid='ignore')
import pandas as pd                                     # noqa: E402

from config import MAX_PAIRS, load_config              # noqa: E402
from evolution import evolve                           # noqa: E402
from round_eta import RoundEta                        # noqa: E402


def env(k, d):
    return os.environ.get('ALPHANODE_' + k, d)


def iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


# ---- node config ----
CPU_PERCENT = max(5, min(95, int(env('CPU_PERCENT', '50'))))
UNIVERSE = env('UNIVERSE', 'all')
POP = int(env('POP', '200'))
GENS = int(env('GENS', '25'))
PAUSE = float(env('PAUSE', '5'))
MAX_ROUNDS = int(env('MAX_ROUNDS', '0'))               # 0 = infinite
SEED_FROM_LIB = env('SEED_FROM_LIBRARY', '1') not in ('0', 'false', 'no', 'off')
EXPLORE_EVERY = max(1, int(env('EXPLORE_EVERY', '4')))  # every Nth round — pure exploration
def _default_state_dir():
    try:
        import apppaths                                # not HERE/state: frozen, HERE is the read-only
        return apppaths.state_dir()                    # bundle — a direct `<exe> --role node` (cron,
    except Exception:                                  # noqa: BLE001   docker) died on makedirs; the
        return os.path.join(HERE, 'state')             # GUI masked it by always passing the env


STATE_DIR = env('STATE_DIR', '') or _default_state_dir()
STATUS_PORT = int(env('STATUS_PORT', '8787'))
KEEP = int(env('LEADERBOARD', '20'))
TF = (env('TF', '') or '1d').strip().lower()           # bar size; also read by load_config (ALPHANODE_TF)
FORWARD = env('FORWARD', '1').strip().lower() not in ('0', 'false', 'no', 'off')

os.makedirs(STATE_DIR, exist_ok=True)


def _resolve_seed():
    """Base seed for the whole run. ALPHANODE_SEED unset / '' / 'auto' / '0' -> derived from a
    persistent random node ID (state/node_id, minted on first run): every install walks its own
    trajectory through formula space, so two nodes never mine identical libraries. An explicit
    integer keeps the old fully reproducible behavior. Returns (seed, node_id, is_auto)."""
    nid_path = os.path.join(STATE_DIR, 'node_id')
    try:
        nid = open(nid_path, encoding='utf-8').read().strip().lower()
    except OSError:
        nid = ''
    if not (len(nid) == 8 and all(c in '0123456789abcdef' for c in nid)):
        import secrets
        nid = secrets.token_hex(4)
        try:
            with open(nid_path, 'w', encoding='utf-8') as f:
                f.write(nid + '\n')
        except OSError:
            pass
    raw = str(env('SEED', 'auto')).strip().lower()
    if raw in ('', 'auto', '0'):
        return int(nid, 16) % 900_000 + 1, nid, True
    return int(raw), nid, False


BASE_SEED, NODE_ID, SEED_AUTO = _resolve_seed()


def _device_id():
    """The hub-facing id of this install (state/device_id) — the SAME file the GUI's
    _device_id mints for /activate, so the boxes this node seals are owned by the seat this
    machine occupies. File semantics must stay in lockstep with alphanode_gui._device_id.
    O_EXCL + re-read: if the GUI and a freshly spawned node mint concurrently, exactly one
    write wins and BOTH processes come away holding the winner's value."""
    path = os.path.join(STATE_DIR, 'device_id')
    try:
        did = open(path, encoding='utf-8').read().strip()
        if did:
            return did
    except OSError:
        pass
    import secrets
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(secrets.token_hex(8) + '\n')
    except OSError:
        pass                                             # someone else minted first — fine
    try:
        return open(path, encoding='utf-8').read().strip()
    except OSError:
        return ''                                        # unwritable state dir: seal unbound

# per-timeframe library/history: alphas mined on different bar sizes are NOT comparable
# (different annualization, different dynamics) and must never mix in one leaderboard.
# 1d keeps the historical file names.
_SUF = '' if TF == '1d' else f'_{TF}'
LIB = os.path.join(STATE_DIR, f'library{_SUF}.jsonl')
HIST = os.path.join(STATE_DIR, f'history{_SUF}.jsonl')  # one line per round (for the progress chart)
STATUS_FILE = os.path.join(STATE_DIR, 'status.json')

# vault mode: every formula is SEALED before it reaches disk or status.json — plaintext stays
# only in this process's memory (refine seeding needs it). The key comes from
# ALPHANODE_VAULT_PUB (dev/self-host override) or the key SHIPPED IN THE BUNDLE — the node
# resolves the bundled key ITSELF, so wiping the env is not an unseal button. vault (and its
# `cryptography` dependency) is imported only when a key exists, so a dev source tree with no
# key runs with exactly its previous imports.
VAULT_PUB = None
VAULT_OWNER = None


def _bundled_pub():
    """The vendor key shipped with the app (RES_ROOT/alphanode/, where the spec puts it)."""
    try:
        import apppaths
        p = os.path.join(apppaths.RES_ROOT, 'alphanode', 'vault_server_key.pub')
        return p if os.path.isfile(p) else None
    except Exception:                                    # noqa: BLE001
        return None


def _vault_open_ok():
    """OPEN (plaintext) mining is a privilege of an ACTIVE subscription, and the node verifies
    that with the hub ITSELF — the GUI's request alone is not enough, or plaintext mining
    would be one exported env var away for anyone. Fail CLOSED: no flag, no licence, hub
    unreachable or refusing -> seal as usual."""
    if os.environ.get('ALPHANODE_VAULT_OPEN') != '1':
        return False
    tok = (os.environ.get('ALPHANODE_VAULT_LICENSE') or '').strip()
    if not tok:
        return False
    try:
        import urllib.request
        import buildinfo
        url = ((os.environ.get('ALPHANODE_VAULT_URL') or '').strip()
               or buildinfo.vault_url() or 'http://127.0.0.1:8790')
        req = urllib.request.Request(
            url.rstrip('/') + '/activate',
            data=json.dumps({'token': tok, 'device_id': _device_id() or ''}).encode(),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=5) as r:
            ok = bool(json.load(r).get('ok'))
        print(f'[vault] open-mining check: {"subscription active" if ok else "REFUSED"}',
              flush=True)
        return ok
    except Exception as e:                               # noqa: BLE001
        print(f'[vault] open-mining check failed ({type(e).__name__}) — sealing', flush=True)
        return False


_VAULT_OPEN = _vault_open_ok()
_pub_src = os.environ.get('ALPHANODE_VAULT_PUB') or _bundled_pub()
_fp = None
if _pub_src and not _VAULT_OPEN:
    import vault
    VAULT_PUB = vault.load_pub(_pub_src)
    VAULT_OWNER = _device_id() or None                   # '' (unwritable state) -> unbound v1
    # tamper-evidence: name the key we seal to (fp) and the build, so a swapped key or an
    # unexpected build is visible in the node's own log, not just the hub's.
    import hashlib as _hl
    _fp = _hl.sha256(VAULT_PUB).hexdigest()[:16]
    print(f'[vault] sealing to key {_fp} as owner {VAULT_OWNER or "none/v1"}', flush=True)

# A SHIPPED build must never quietly mine in the open, and must seal to the key STAMPED at
# build time: a missing key (the resolver bug that shipped plaintext libraries) and a
# substituted keypair (seal-to-self, unseal locally) both die HERE instead of downgrading.
# Dev builds carry no stamped fingerprint, so nothing changes outside a release.
if getattr(sys, 'frozen', False) and not _VAULT_OPEN:
    try:
        import buildinfo
        _want_fp = buildinfo.build_info().get('vault_pub_fp')
    except Exception:                                    # noqa: BLE001
        _want_fp = None
    if _want_fp and _fp is None:
        print('[vault] FATAL: sealing key not found in this installation', flush=True)
        sys.exit(3)
    if _want_fp and _fp != _want_fp:
        print(f'[vault] FATAL: sealing key {_fp} does not match the build stamp {_want_fp}',
              flush=True)
        sys.exit(3)


def _id_key(formula):
    """The dedup key a locked doc is stored under ('id:<md5-tail>'), computed the same way as
    vault.formula_id but WITHOUT importing vault — so a library that mixes sealed and plaintext
    rows dedups correctly even with sealing off and cryptography absent."""
    return 'id:' + hashlib.md5(formula.encode()).hexdigest()[:12]


def _disk_doc(c):
    """What a champion looks like on disk / in status.json. Vault mode strips the plaintext
    formula and stores a sealed token + a public id; otherwise the doc passes verbatim."""
    if VAULT_PUB is None or 'formula' not in c:
        return c
    d = {k: v for k, v in c.items() if k != 'formula'}
    d['locked'] = True
    d['id'] = vault.formula_id(c['formula'])
    d['formula_enc'] = vault.seal(c['formula'], VAULT_PUB, owner=VAULT_OWNER)
    return d
CORES = os.cpu_count() or 4
N_JOBS = max(1, round(CPU_PERCENT / 100 * CORES))      # resources -> number of parallel workers

try:
    os.nice(10)                                        # background priority (don't disturb interactive use)
except (AttributeError, OSError):
    pass

STOP = False


def _sig(*_a):
    global STOP
    STOP = True


for _s in (signal.SIGTERM, signal.SIGINT):
    signal.signal(_s, _sig)

try:
    import buildinfo as _bi
    _BUILD = _bi.build_info()
except Exception:                                          # noqa: BLE001 — never block the node
    _BUILD = {'version': '?', 'build_id': 'dev'}

status = {'app': 'AlphaNode', 'state': 'starting', 'started': iso(), 'updated': iso(),
          'rounds': 0, 'trials_total': 0, 'found': 0, 'cpu_percent': CPU_PERCENT, 'n_jobs': N_JOBS,
          'cores': CORES, 'universe': UNIVERSE, 'tf': TF, 'pop': POP, 'gens': GENS,
          'explore_every': EXPLORE_EVERY, 'seed_from_lib': SEED_FROM_LIB,
          'node_id': NODE_ID, 'seed_base': BASE_SEED, 'seed_auto': SEED_AUTO,
          'version': _BUILD.get('version'), 'build_id': _BUILD.get('build_id'),
          'current': '', 'gen': '', 'best': []}


def save_status():
    status['updated'] = iso()
    try:
        # encoding is EXPLICIT: events carry '▶'/'★', and the locale default (cp1251 on a Russian
        # Windows) can't encode them — the node died right after 'Start node' on such machines.
        with open(STATUS_FILE, 'w', encoding='utf-8') as f:
            json.dump(status, f, indent=2, ensure_ascii=False, default=str)
    except OSError:
        pass


def log_event(kind, text):
    """Append to the human-readable activity feed (GUI 'LIVE LOG' + status page).
    kinds: round | best | polish | warn | err — the GUI colors by them."""
    ev = status.setdefault('events', [])
    ev.append({'ts': time.strftime('%H:%M:%S'), 'k': kind, 't': str(text)})
    del ev[:-80]


# ---- library (dedup by formula) + leaderboard by fitness base=min(train,val) + round history ----
seen = set()
leaderboard = []
history = []


def _testsh(c):
    t = c.get('test')
    return t['sharpe'] if (t and t.get('sharpe') is not None) else -1e9


def _basesh(c):
    """Selection fitness = base = min(train,val) Sharpe. TEST does NOT enter selection (kept closed)."""
    b = c.get('base')
    return b if b is not None else -1e9


def _rm(m):
    if not m:
        return None
    return {k: (round(float(v), 4) if math.isfinite(float(v)) else None) for k, v in m.items()}


def load_existing():
    if os.path.exists(LIB):
        for line in open(LIB, encoding='utf-8'):
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
                # one dedup key per doc (so status['found'] = len(seen) stays the doc count):
                # plaintext rows key by formula, locked rows by their public id. The mining loop
                # checks BOTH forms of a new champion, so a sealed twin of a plaintext row (or the
                # reverse) is still caught without a second key here.
                seen.add(c['formula'] if c.get('formula') else 'id:' + str(c.get('id', '')))
                leaderboard.append(c)
            except json.JSONDecodeError:
                pass
    leaderboard.sort(key=_rank_key)                    # active objective first, then fitness;
    del leaderboard[KEEP:]                             # never by TEST
    if os.path.exists(HIST):
        for line in open(HIST, encoding='utf-8'):
            line = line.strip()
            if line:
                try:
                    history.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    # resume the round counter from HISTORY (every round is logged there), not from the trimmed
    # top-KEEP leaderboard: evolve() is seed-deterministic, so rewinding the counter to a stale
    # leaderboard round would replay every round since with identical seeds for zero new alphas.
    for e in history:                                  # ETA priors: the last learned slope per mode
        if isinstance(e.get('eta_delta'), (int, float)):
            _eta_priors['refine' if str(e.get('mode', '')).startswith('refin') else 'explore'] = e['eta_delta']
    status['rounds'] = max(max((e.get('round', 0) for e in history), default=0),
                           max((c.get('round', 0) for c in leaderboard), default=0))
    try:                                               # keep the lifetime trials counter across restarts
        with open(STATUS_FILE, encoding='utf-8') as f:
            status['trials_total'] = int(json.load(f).get('trials_total', 0) or 0)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    status['found'] = len(seen)
    status['best'] = [_disk_doc(c) for c in leaderboard[:KEEP]]   # mask any plaintext docs too
    if leaderboard:                                    # honest champion metrics right at startup
        ch = leaderboard[0]
        bb, bt = _basesh(ch), _testsh(ch)
        status['best_base'] = round(bb, 3) if bb > -1e8 else None
        status['best_test'] = round(bt, 3) if bt > -1e8 else None
    status['history'] = history[-300:]


FIT_METRIC = (env('ALPHANODE_FIT_METRIC', '') or 'sharpe').strip().lower() or 'sharpe'


def _rank_key(c):
    """Leaderboard order: rows mined under the ACTIVE objective first (their 'base' values
    share a scale), then by base. Winrate bases (<= 1.0) and Sharpe bases (~1-2.5) are
    different units — one raw ladder drowned every winrate champion under old Sharpe rows,
    so refine kept seeding Sharpe formulas during a winrate run (confirmed by review)."""
    return ((c.get('fit_metric') or 'sharpe') != FIT_METRIC, -_basesh(c))


def _fmt_fit(c, v):
    """A row's fitness, in its own units: '57%' for winrate-mined rows, '+1.85' Sharpe."""
    if v is None or v <= -1e8:
        return '—'
    if (c.get('fit_metric') or 'sharpe') == 'winrate':
        return f'{v * 100:.0f}%'
    return f'{v:+.2f}'


def champions_from_hof(hof, metric='sharpe'):
    # 'fit_metric' tags what 'base' measures: a library mixes rows mined under different
    # objectives (Sharpe ~1.5 vs win rate ~0.55 are different scales), the tag lets every
    # display format the number honestly
    return [{'rank': i, 'formula': h['canon'], 'size': h['size'], 'base': round(h['base'], 3),
             'train': _rm(h.get('train')), 'val': _rm(h.get('val')), 'test': _rm(h.get('test')),
             'blocks': h.get('blocks'), 'eff_n': h.get('eff_n'),   # robust-fitness evidence
             'origin': h.get('origin', 'ga'), 'fit_metric': metric}
            for i, h in enumerate(hof)]


# ---- override ANY search parameter via ALPHANODE_* (empty/unset -> taken from config.ini) ----
def _envset(name):
    v = os.environ.get('ALPHANODE_' + name)
    return v if v not in (None, '') else None


def _override(cfg, key, name, cast):
    v = _envset(name)
    if v is not None:
        try:
            cfg[key] = cast(v)
        except ValueError:
            pass


def _apply_segments(cfg):
    order = ('TRAIN_START', 'VAL_START', 'TEST_START', 'TEST_END')
    raw = {k: _envset(k) for k in order}
    if not any(raw.values()):
        return
    sp = cfg['splits']
    cur = [sp['train'][0], sp['val'][0], sp['test'][0], sp['test'][1]]
    tr, va, te, en = [pd.Timestamp(raw[k], tz='UTC') if raw[k] else cur[i]
                      for i, k in enumerate(order)]
    cfg['splits'] = {'train': (tr, va), 'val': (va, te), 'test': (te, en)}
    cfg['start'] = tr.tz_localize(None).to_pydatetime()
    cfg['end'] = en.tz_localize(None).to_pydatetime()


def _apply_overrides(cfg):
    _override(cfg, 'vol', 'TARGET_VOL', float)
    _override(cfg, 'exec', 'EXEC_COST', float)
    _override(cfg, 'max_depth', 'MAX_DEPTH', int)
    _override(cfg, 'max_size', 'MAX_SIZE', int)
    _override(cfg, 'tourn', 'TOURNAMENT', int)
    _override(cfg, 'elitism', 'ELITISM', int)
    _override(cfg, 'random_inject', 'RANDOM_INJECT', int)
    _override(cfg, 'cx_prob', 'CROSSOVER_PROB', float)
    _override(cfg, 'parsimony', 'PARSIMONY', float)
    _override(cfg, 'corr_thresh', 'CORR_THRESHOLD', float)
    _override(cfg, 'corr_penalty', 'CORR_PENALTY', float)
    _override(cfg, 'hof_cap', 'HOF_CAPACITY', int)
    _override(cfg, 'fit_blocks', 'FIT_BLOCKS', int)    # robust fitness; 0 = legacy min(train,val)
    _override(cfg, 'fit_metric', 'FIT_METRIC',         # 'sharpe' | 'winrate'
              lambda s: s.strip().lower())
    _apply_segments(cfg)


def build_cfg(seed, seeds=None):
    cfg = load_config()                                # segments/vol/penalties from evolution/config.ini
    if UNIVERSE.lower() not in ('all', '*', ''):
        cfg['instruments'] = list(dict.fromkeys(
            x.strip().upper() for x in UNIVERSE.split(',') if x.strip()))[:MAX_PAIRS]
    cfg.update(pop=POP, gens=GENS, seed=seed, n_jobs=N_JOBS)
    _apply_overrides(cfg)                              # target_vol, genome, GA, fitness, date segments
    if seeds:                                          # warm-start: seed with the best from the library
        cfg['seed_formulas'] = list(seeds)
    return cfg




# ---- minimal status server (stdlib) ----
def _html_formula(c):
    """The formula cell on the :8787 page. Vault mode (or a locked doc loaded after a restart)
    shows the public id, never plaintext; the in-memory leaderboard keeps plaintext for refine
    seeding, so masking has to happen at render time, not just on the file write."""
    if VAULT_PUB is not None or c.get('locked') or not c.get('formula'):
        return '🔒 locked · ' + str(c.get('id', ''))[:12]
    return c['formula']


def render_html():
    rows = ''.join(
        f"<tr><td>{i + 1}</td>"
        f"<td class=t>{_fmt_fit(c, _basesh(c))}</td>"
        f"<td>{('%+.2f' % _testsh(c)) if _testsh(c) > -1e8 else '—'}</td>"
        f"<td class=f>{_html_formula(c)}</td></tr>"
        for i, c in enumerate(leaderboard[:KEEP]))
    evs = status.get('events') or []
    ev_lines = ''.join(
        f"<div class='e {e.get('k', '')}'><span>{e.get('ts', '')}</span>{e.get('t', '')}</div>"
        for e in reversed(evs[-16:]))
    adv_log = (f"<div class=card style='margin-bottom:16px'>"
               f"<div class=k style='margin-bottom:6px'>live log — what the node is doing</div>"
               f"{ev_lines}</div>") if ev_lines else ''
    fwd = status.get('forward') or []
    fwd_rows = ''.join(
        f"<tr><td class=f>{e['id']}</td><td>{e.get('tf', '1d')}</td><td>{e['steps']}</td>"
        f"<td>{e['ret'] * 100:+.1f}%</td>"
        f"<td>{('%+.2f' % e['sharpe']) if e['sharpe'] is not None else '—'}</td></tr>"
        for e in fwd)
    fwd_card = (f"<div class=card style='margin-bottom:16px'>"
                f"<div class=k style='margin-bottom:8px'>forward track — append-only paper steps "
                f"(stepped by this node; no GUI needed)</div>"
                f"<div class=tw><table><thead><tr><th class=f>id</th><th>tf</th><th>steps</th>"
                f"<th>return</th><th>sharpe</th></tr></thead><tbody>{fwd_rows}</tbody></table>"
                f"</div></div>" if fwd_rows else '')
    # Visual language modeled on nixtla.io: warm paper background, near-black ink, hairline
    # borders, flat white cards, uppercase mono micro-labels, periwinkle + orange accents.
    # Self-contained on purpose (system font stacks, no CDN): the node may run offline.
    return f"""<!doctype html><meta charset=utf-8><title>AlphaNode</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{{--paper:#f6f4f0;--card:#ffffff;--ink:#222121;--mut:#6f6b66;--line:#dedddd;--soft:#eae9e6;
--acc:#7d8cff;--accsoft:#bfd1ff;--orange:#f99c00;--green:#1e7f4e;--red:#c14b36}}
@media(prefers-color-scheme:dark){{:root{{--paper:#262421;--card:#2e2b27;--ink:#f6f4f0;--mut:#a5a09a;
--line:#3a3733;--soft:#35322e;--accsoft:#4a5285;--green:#5abd8c;--red:#e57a63}}}}
*{{box-sizing:border-box}}
body{{font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--paper);
color:var(--ink);margin:0 auto;padding:44px 30px 60px;max-width:1180px}}
h1{{margin:0;font-size:27px;font-weight:600;letter-spacing:-.02em}}
.sub{{color:var(--mut);margin:4px 0 26px;font-size:14px}}
.k{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10.5px;letter-spacing:.09em;
text-transform:uppercase;color:var(--mut)}}
b{{color:var(--ink);font-weight:600}}
.grid{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}}
.grid .card{{min-width:150px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px}}
.num{{font-size:20px;font-weight:600;letter-spacing:-.01em}}
table{{width:100%;border-collapse:collapse}}
td,th{{padding:7px 10px;border-bottom:1px solid var(--soft);text-align:right;font-size:12.5px;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
tr:last-child td{{border-bottom:none}}
th{{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--mut);font-weight:500}}
td.f,th.f{{text-align:left;color:var(--ink)}} td.t{{color:var(--acc);font-weight:600}}
.dot{{width:9px;height:9px;border-radius:50%;background:var(--acc);display:inline-block;
margin-right:10px;animation:p 1.2s infinite}}
@keyframes p{{50%{{opacity:.3}}}}
.gen{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;color:var(--mut);
margin:0 0 16px;white-space:pre-wrap}}
.tw{{overflow-x:auto}} .tw td{{white-space:nowrap}}
.e{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;padding:2.5px 0;
white-space:pre-wrap;word-break:break-word;color:var(--mut)}}
.e span{{color:var(--accsoft);margin-right:10px}} .e.best{{color:var(--green)}}
.e.round{{color:var(--ink)}} .e.polish{{color:var(--acc)}} .e.err,.e.warn{{color:var(--orange)}}
</style>
<h1><span class=dot></span>AlphaNode <span style="color:var(--mut);font-weight:400;font-size:14px">— {status['state']} · node {status.get('node_id', '—')}</span></h1>
<p class=sub>background alpha-search node · page refreshes itself</p>
<div class=grid>
  <div class=card><div class=k>rounds</div><div class=num>{status['rounds']}</div></div>
  <div class=card><div class=k>formulas tried</div><div class=num>{status['trials_total']:,}</div></div>
  <div class=card><div class=k>alphas found</div><div class=num>{len(seen)}</div></div>
  <div class=card><div class=k>resources</div><div class=num>{status['cpu_percent']}%</div><span class=k>{status['n_jobs']}/{status['cores']} cores</span></div>
  <div class=card><div class=k>universe</div><div class=num>{status['universe']}</div><span class=k>pop {status['pop']} · gens {status['gens']}</span></div>
</div>
<div class=gen>{status.get('current','')} &nbsp; {status.get('gen','')}</div>
{adv_log}
{fwd_card}
<div class=card><div class=k style="margin-bottom:8px">best by fitness min(train,val) · TEST — honest held-out (read-only, does NOT enter selection)</div>
<div class=tw><table><thead><tr><th>#</th><th>fitness</th><th>TEST (OOS)</th><th class=f>formula</th></tr></thead><tbody>{rows}</tbody></table></div></div>
<p class=sub style="margin-top:14px">AlphaNode v{status.get('version','?')} · build {status.get('build_id','dev')}</p>
<script>setTimeout(()=>location.reload(),4000)</script>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.rstrip('/') == '/status.json':
            body = json.dumps(status, ensure_ascii=False, default=str).encode()
            ctype = 'application/json'
        else:
            body = render_html().encode()
            ctype = 'text/html; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)



class _NoDNSHTTPServer(http.server.ThreadingHTTPServer):
    """server_bind WITHOUT socket.getfqdn(): the stock HTTPServer reverse-DNS-resolves the
    bind address, which hangs for minutes on hosts with broken PTR lookup (every Windows CI
    runner, plenty of customer desktops). The GUI and the CI watchdog poll this server, so
    it must bind instantly; nothing here needs the FQDN."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


def serve():
    try:
        _NoDNSHTTPServer(('0.0.0.0', STATUS_PORT), Handler).serve_forever()
    except OSError as e:
        print('status server off:', e)


# ---- forward track, headless ----
def _fwd_summary(ft, track):
    out = []
    for e in track['entries']:
        if e.get('archived'):
            continue
        m = ft.metrics(e)
        out.append({'id': e['id'], 'tf': e.get('tf', '1d'), 'steps': m['days'],
                    'ret': m['ret'], 'sharpe': m['sharpe']})
    return out


def forward_loop():
    """Step the forward track WITHOUT the GUI. Historically only the desktop app advanced the
    enrolled strategies (its 5-minute tick), so a server/Docker node silently froze the honest
    forward test the moment the window closed. Same cadence and the same code path as the GUI
    (forward_track.is_due / step_all — append-only, closed bars only, 2-bar lag), so the two
    never double-step: whoever wakes up first appends the bar, the other sees it as done.
    Disable with ALPHANODE_FORWARD=0."""
    import forward_track as ft
    while not STOP:
        try:
            track = ft.load_track()
            active = [e for e in track['entries'] if not e.get('archived')]
            due = [e for e in active if ft.is_due(e)]
            if due:
                log_event('round', f'forward track: {len(due)}/{len(active)} entries have a '
                                   f'new closed bar — stepping…')
                ft.step_all(log=lambda m: log_event('polish', f'[forward] {m}'))
                track = ft.load_track()
            status['forward'] = _fwd_summary(ft, track)
            save_status()
        except Exception as ex:                        # noqa: BLE001 — never kill the loop
            log_event('warn', f'forward step failed: {type(ex).__name__}: {ex}')
        for _ in range(300):                           # 5 min, responsive to shutdown
            if STOP:
                return
            time.sleep(1)


# ---- main loop ----
_last_save = [0.0]
_eta = [None]            # RoundEta of the round in flight (None between rounds)
_eta_priors = {}         # mode -> dedup-decay slope learned from finished rounds


def _vault_scrub(m):
    """Vault mode: the engine logs formula text on two lines — '★ … — <canon>' and
    '  fine-tune: <a> -> <b>  <canon>'. Both flow through _cb into status/log/HTTP, so the
    formula must be cut off HERE (a no-op when the vault is off). Keeps the numbers, drops
    the canon tail."""
    if VAULT_PUB is None:
        return m
    if ' — ' in m:                                       # ★ new-best line
        m = m.split(' — ')[0]
    if 'fine-tune:' in m and '->' in m:                  # per-champion polish line (not the summary)
        m = re.sub(r'(->\s*[-+][\d.]+).*$', r'\1', m)    # keep up to the improved base, drop the canon
    return m


def _cb(msg):
    m = _vault_scrub(str(msg).rstrip())
    ms = m.strip()
    if ms.startswith('★'):
        log_event('best', ms)
    elif 'window polish' in ms:
        log_event('polish', ms)
    elif ms.startswith('WARNING'):
        log_event('warn', ms)
    else:                                              # per-generation progress -> the live ticker
        status['gen'] = m
    now = time.time()
    if _eta[0] is not None:                            # remaining time of THIS round (round_eta.py)
        _eta[0].feed(now, ms)
        eta = _eta[0].eta(now)
        status['eta_s'] = None if eta is None else round(eta)
        status['eta_at'] = now                         # readers subtract their own clock drift
        status['progress'] = round(_eta[0].progress(now), 4)
        status['gen_i'] = _eta[0].g + 1                # finished generations, 0..gens
    if now - _last_save[0] > 1.0:                      # live progress for GUI/page (throttle 1s)
        _last_save[0] = now
        save_status()
    if STOP:
        raise KeyboardInterrupt('stop requested')


def ensure_data():
    """Headless twin of the GUI's Start-time data gate: make sure the snapshot exists AND
    holds every configured pair. ALPHANODE_UNIVERSE names the basket ('all'/empty falls back
    to the 10-major starter set); a missing file or a missing pair triggers a fetch of that
    exact basket at the active timeframe."""
    global UNIVERSE
    path = load_config()['data']
    if UNIVERSE.lower() in ('all', '*', ''):
        want = None                                    # starter set — presence check on file only
    else:                                              # dedup, upper, order kept, capped
        want = list(dict.fromkeys(x.strip().upper()
                                  for x in UNIVERSE.split(',')
                                  if x.strip()))[:MAX_PAIRS] or None

    def _tickers():
        try:
            import pickle
            return list(pickle.load(open(path, 'rb'))[0])
        except Exception:                              # noqa: BLE001 — unreadable = absent
            return None
    why, have = None, None
    if not os.path.exists(path):
        why = f'no market data at {path}'
    elif want:
        have = _tickers()
        if have is None:
            why = f'snapshot at {path} is unreadable'
        else:
            missing = [s for s in want if s not in set(have)]
            if missing:
                why = f'snapshot lacks {", ".join(missing)}'
    if why is None:
        return
    if PROJ not in sys.path:                           # fetch_data.py lives at the repo root
        sys.path.insert(0, PROJ)
    import fetch_data
    symbols = want or list(fetch_data.DEFAULT_SYMBOLS)
    if want and have:                                  # top up a live snapshot WITHOUT dropping
        symbols = list(dict.fromkeys(list(have) + want))   # pairs other universes rely on — the
        #                                                    old ensure_data never touched an
        #                                                    existing file, we must not shrink it
    names = ', '.join(s.replace('USDT', '') for s in symbols[:10])
    print(f'{why} — downloading {len(symbols)} pairs ({names}) as {TF} candles…', flush=True)
    rc = fetch_data.run(path, interval=TF, symbols=symbols)
    if rc != 0 or not os.path.exists(path):
        raise SystemExit(f'✗ data bootstrap failed (code {rc}) — check the internet connection '
                         'or run fetch_data.py manually')
    if want:                                           # the fetch SKIPS pairs Binance does not
        got = set(_tickers() or [])                    # serve and still exits 0 — verify, drop
        gone = [s for s in want if s not in got]       # the unfetchable ones LOUDLY, and mine on
        if gone:
            keep = [s for s in want if s in got]
            if not keep:
                raise SystemExit(f'✗ none of the configured pairs exist on Binance futures: '
                                 f'{", ".join(gone)} — fix ALPHANODE_UNIVERSE')
            print(f'⚠ Binance futures does not serve: {", ".join(gone)} — '
                  f'mining on {", ".join(keep)}', flush=True)
            UNIVERSE = ','.join(keep)                  # build_cfg and status read this global
            os.environ['ALPHANODE_UNIVERSE'] = UNIVERSE
    print('✓ market data ready', flush=True)


def main():
    ensure_data()
    load_existing()
    c0 = build_cfg(BASE_SEED)
    status['target_vol'] = c0.get('vol')                     # effective target vol (env or config.ini)
    threading.Thread(target=serve, daemon=True).start()
    if FORWARD:
        threading.Thread(target=forward_loop, daemon=True).start()
    print(f'AlphaNode [{NODE_ID}]: {CPU_PERCENT}% -> {N_JOBS}/{CORES} cores | universe={UNIVERSE} '
          f'tf={TF} pop={POP} gens={GENS} | status: http://localhost:{STATUS_PORT}')
    if SEED_AUTO:
        log_event('i', f'node {NODE_ID}: unique search — base seed {BASE_SEED} derived from this '
                       f'install\'s node ID; no two nodes mine the same library '
                       f'(set ALPHANODE_SEED=<int> for a reproducible run)')
    else:
        log_event('i', f'node {NODE_ID}: fixed seed {BASE_SEED} — reproducible run '
                       f'(ALPHANODE_SEED=auto for a per-install unique search)')
    if SEED_FROM_LIB and EXPLORE_EVERY == 1:
        # rnd % 1 != 0 is never true -> the refine branch below is unreachable
        print('WARNING: explore_every=1 makes EVERY round a from-scratch exploration — '
              'warm-start refinement of library champions never runs. Set explore_every to 3-4.')
    status['state'] = 'running'
    save_status()

    rnd = status['rounds']
    refine_explained = False                             # the warm-start lesson is logged once
    while not STOP and (MAX_ROUNDS == 0 or rnd < MAX_ROUNDS):
        rnd += 1
        seed = BASE_SEED + rnd
        # refine on the best from the library (warm-start); periodically — pure exploration.
        # Vault docs loaded from disk carry no plaintext, so only this session's finds can
        # seed a warm start — with none available the round honestly falls back to explore.
        seed_pool = [c['formula'] for c in leaderboard if c.get('formula')]
        refine = SEED_FROM_LIB and bool(seed_pool) and (rnd % EXPLORE_EVERY != 0)
        seeds = seed_pool if refine else None
        mode = 'refining best' if refine else 'exploring new'
        status['mode'] = mode
        status['current'] = f'round {rnd}: {mode} (seed {seed})…'
        if refine:
            extra = ('; evolution mutates around what already works '
                     f'(1 round in {EXPLORE_EVERY} explores from scratch)'
                     if not refine_explained else '')
            refine_explained = True                     # the lesson once — not every 2nd round
            log_event('round', f'▶ round {rnd}: refine — improving {len(seeds)} champions '
                               f'from the library{extra}')
        else:
            log_event('round', f'▶ round {rnd}: explore — a fresh random population, '
                               f'hunting new formula families')
        t0 = time.time()
        _eta[0] = RoundEta(mode, POP, GENS, t0, _eta_priors)
        status.update(round_t0=t0, round_n=rnd, eta_s=None, eta_at=t0, progress=0.0, gen_i=0)
        save_status()                                  # the new round's bar starts with its banner
        cfg = build_cfg(seed, seeds)
        try:
            hof, _hist, cache = evolve(cfg, log=_cb)
        except KeyboardInterrupt:
            break
        except Exception as e:                         # noqa: BLE001
            _eta[0] = None
            status.update(eta_s=None, progress=0.0)
            status['current'] = f'round {rnd}: error {type(e).__name__}: {e}'
            log_event('err', f'✗ round {rnd} failed: {type(e).__name__}: {e}')
            save_status()
            time.sleep(PAUSE)
            continue

        dur = round(time.time() - t0, 1)
        ld = _eta[0].learned_delta()
        if ld is not None:                             # next round of this mode starts from what this one showed
            _eta_priors[_eta[0].mode] = ld
        _eta[0] = None
        status.update(eta_s=0, eta_at=time.time(), progress=1.0, gen_i=GENS)
        new = 0
        with open(LIB, 'a', encoding='utf-8') as f:
            for c in champions_from_hof(hof, metric=cfg.get('fit_metric', 'sharpe')):
                if c['formula'] in seen or _id_key(c['formula']) in seen:
                    continue                             # already mined (plaintext OR a locked doc)
                seen.add(c['formula'])
                c['round'], c['ts'] = rnd, iso()
                f.write(json.dumps(_disk_doc(c), ensure_ascii=False) + '\n')
                leaderboard.append(c)                    # memory keeps plaintext (refine seeding)
                new += 1
        leaderboard.sort(key=_rank_key)                # champion = best under the ACTIVE objective;
        del leaderboard[KEEP:]                         # TEST closed
        champ = leaderboard[0] if leaderboard else None
        bb = _basesh(champ) if champ else None          # optimized fitness
        bt = _testsh(champ) if champ else None          # honest held-out OOS of the same champion (read-only)
        bb_val = round(bb, 3) if (bb is not None and bb > -1e8) else None
        bt_val = round(bt, 3) if (bt is not None and bt > -1e8) else None
        bb_s = _fmt_fit(champ or {}, bb) if bb_val is not None else '—'
        bt_s = f'{bt_val:+.2f}' if bt_val is not None else '—'
        entry = {'round': rnd, 'best_base': bb_val, 'best_test': bt_val,
                 'found': len(seen), 'mode': mode, 'ts': iso(), 'dur': dur,
                 'eta_delta': None if ld is None else round(ld, 3)}
        history.append(entry)
        try:
            with open(HIST, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except OSError:
            pass
        status.update(rounds=rnd, trials_total=status['trials_total'] + len(cache),
                      # status.json reaches disk AND the :8787 status HTTP — vault mode must
                      # seal here too, or the library lock would leak through the side door
                      found=len(seen), best=[_disk_doc(c) for c in leaderboard[:KEEP]],
                      best_base=bb_val, best_test=bt_val, fit_metric=FIT_METRIC,
                      history=history[-300:],
                      current=f'round {rnd} done [{mode}]: +{new} new · fitness {bb_s} · '
                              f'TEST(OOS) {bt_s} · {time.time()-t0:.0f}s')
        champs_s = (f'+{new} champion{"s" if new != 1 else ""} kept'
                    if new else 'none kept — the library held its bar')
        log_event('round', f'✓ round {rnd} · {time.time() - t0:.0f}s · {len(cache):,} formulas '
                           f'tried · {champs_s} · best fitness {bb_s} · held-out TEST {bt_s}')
        save_status()
        print(status['current'])

        for _ in range(int(PAUSE * 2)):                # interruptible pause
            if STOP:
                break
            time.sleep(0.5)

    status['state'] = 'stopped'
    save_status()
    print('AlphaNode stopped.')


if __name__ == '__main__':
    main()

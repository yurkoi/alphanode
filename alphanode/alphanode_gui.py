"""AlphaNode — desktop interface (CustomTkinter).

Control panel for the background node. On the left — the FULL set of search settings (resources,
universe, population/generations, node mode, simulation/target-vol, genome, GA selection, fitness,
date segments) — everything the engine understands is tunable by hand and passed to the node via
ALPHANODE_* variables. On the right — live status (with the best fitness as a stat tile) and a
leaderboard of found alphas. Launches the node as a subprocess (node.py) and reads its
state/status.json.

Theming: every colour comes from PALETTE[light|dark] and is published as the module-level constants
below (BG, CARD, TXT, …). Switching the theme re-applies the palette and rebuilds the window — the
parts we don't own (ttk.Treeview, the Canvas chart, matplotlib PNGs) can't restyle themselves live,
so a rebuild is both simpler and the only way to keep them consistent.

CustomTkinter notes: CTk widgets reject .config() (use .configure()) and name the text colour
text_color, not foreground. The leaderboard stays a ttk.Treeview — CustomTkinter has no table.

NO COLOUR EMOJI in widget text — labels are plain words. Beyond taste, a colour emoji is rendered by
Noto Color Emoji, and that font inside a CustomTkinter widget SEGFAULTS Tk on Linux/Xft (plain
tk/ttk survives it, which is why the pre-CTk build could carry emoji and this one cannot). The X
error is asynchronous, so it surfaces at whatever unrelated Tcl call runs next — the traceback will
point somewhere innocent. Monochrome BMP marks (▶ ■ ● ✓ ✕ ⚠) are safe and are all we use.

Run:  python alphanode/alphanode_gui.py
"""
import os
import sys
import csv
import json
import math
import time
import queue
import random
import re
from datetime import datetime, timezone, timedelta
import signal
import pickle
import difflib
import hashlib
import threading
import subprocess

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox, filedialog

import customtkinter as ctk

# customtkinter bug (5.x/6.x): CTkScrollbar._on_motion reads self._motion_center_offset, but
# only _clicked ever sets it — a drag that reaches the slider without a clean press inside it
# crashes the Tk callback with AttributeError. A class-level default turns that first motion
# into a plain no-offset drag instead of a traceback.
ctk.CTkScrollbar._motion_center_offset = 0

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)                             # for import apppaths on direct launch
import apppaths                                          # noqa: E402
import favorites as favdb                                # noqa: E402
PROJ = apppaths.PROJ
EVO = apppaths.engine_dir()
if EVO not in sys.path:
    sys.path.insert(0, EVO)                              # to pull in the engine for the equity chart
if apppaths.RES_ROOT not in sys.path:
    sys.path.insert(0, apppaths.RES_ROOT)               # for import quantpylib in the frozen build
NODE_PY = os.path.join(HERE, 'node.py')                 # dev: scripts via the real python
FETCH_PY = os.path.join(PROJ, 'fetch_data.py')
PORTFOLIO_PY = os.path.join(HERE, 'portfolio_build.py')
SIGNAL_PY = os.path.join(HERE, 'signal_service.py')     # local live-signal API
METRICS_PY = os.path.join(HERE, 'metrics_worker.py')    # leaderboard trade stats (own process)
PDF_PY = os.path.join(HERE, 'pdf_worker.py')            # analytics PDF dashboard (own process)
SIGNAL_PORT = 8799                                      # BASE port: each service takes the next free one
DATA_PICKLE = apppaths.user_data_pickle()               # where the data fetcher writes fresh data
def _seg_res(tf, field, v):
    """One date box resolved ('auto'/'today' sentinels -> real dates for this timeframe;
    evolution/timeframe.seg_value)."""
    from timeframe import seg_value
    return seg_value(tf, field, v)


STATE_DIR = apppaths.state_dir()
try:                                                     # a native crash (Tk/X11 segfault) then
    import faulthandler                                  # leaves every thread's Python stack in
    os.makedirs(STATE_DIR, exist_ok=True)                # state/crash.log instead of a bare
    faulthandler.enable(open(os.path.join(STATE_DIR, 'crash.log'), 'w'))   # 'core dumped'
except Exception:                                        # noqa: BLE001 — diagnostics must never
    pass                                                 # block the launch
STATUS_FILE = os.path.join(STATE_DIR, 'status.json')
SIGNALS_JSON = os.path.join(STATE_DIR, 'signals.json')  # registry of served APIs (survives a restart)
PORTFOLIO_JSON = os.path.join(STATE_DIR, 'portfolio.json')
SESSION_PARAMS_JSON = os.path.join(STATE_DIR, 'session_params.json')  # the frozen search
#                                        contract per timeframe (see _session_conflict)
PORTFOLIO_PNG = os.path.join(STATE_DIR, 'portfolio_equity.png')
SETTINGS = apppaths.settings_file()
CORES = os.cpu_count() or 4
# Children print '→'/'✓' progress lines; on a cp1251 Windows a dev-mode python child would die
# with UnicodeEncodeError on the first arrow. Frozen children force UTF-8 themselves (app_entry),
# dev children pick it up from the environment. Read-side: every pipe below decodes as UTF-8.
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
# vault prototype: where locked formulas get revealed (subscription check lives server-side)
def _resolve_vault_url():
    """The hub URL, in precedence: an explicit env var (self-host / dev override) wins, then the
    URL baked into the build stamp (the only channel a Windows .exe has — no launcher wrapper),
    then the localhost default for a bare dev run."""
    env = (os.environ.get('ALPHANODE_VAULT_URL') or '').strip()
    if env:
        return env
    try:
        import buildinfo
        baked = buildinfo.vault_url()
        if baked:
            return baked
    except Exception:                                    # noqa: BLE001
        pass
    return 'http://127.0.0.1:8790'


VAULT_URL = _resolve_vault_url()


def _vault_pub_path():
    """The vault is always on — the node seals every mined formula to the VENDOR's public key,
    and a subscription (checked by the vault server) is what unlocks the formula text + signals.
    There is no user setting for this. In a shipped build the key ships with the app; for the
    prototype it's the local server key. ALPHANODE_VAULT_PUB overrides (self-host / dev). An
    empty result (the vendor's server has never created a key yet) means the node runs unsealed
    until one exists."""
    p = os.environ.get('ALPHANODE_VAULT_PUB')
    if p:
        return p
    # RES_ROOT, not PROJ: frozen, the key ships INSIDE _internal (apppaths.RES_ROOT/alphanode/),
    # while PROJ points one level ABOVE _internal — that miss made every shipped node silently
    # mine IN THE OPEN (plaintext library_*.jsonl), the exact leak the vault exists to prevent.
    # In dev RES_ROOT == PROJ, so the path is unchanged there. selfcheck asserts this resolves.
    cand = os.path.join(apppaths.RES_ROOT, 'alphanode', 'vault_server_key.pub')
    return cand if os.path.isfile(cand) else ''


def _build_id():
    """This build's id (provenance), sent to the hub on activate. 'dev' outside a release."""
    try:
        import buildinfo
        return buildinfo.build_info()['build_id']
    except Exception:                                    # noqa: BLE001 — never block activation
        return 'dev'


TF_CHOICES = ('1d', '4h', '1h', '15m')

def _tf_suffix(tf):
    """File suffix for per-timeframe state: '' for the historical daily files, '_1h' etc. else."""
    return '' if (tf or '1d') == '1d' else f'_{tf}'


# Hard cap on the basket. Deliberately a second copy of evolution/config.py's MAX_PAIRS rather
# than an import: config pulls in pandas, and the GUI defers that to keep the splash instant.
# tests/test_pair_cap.py fails the moment the two numbers disagree.
MAX_PAIRS = 20


def _parse_universe(raw, cap=MAX_PAIRS):
    """'btc , ethusdt,,ETHUSDT' -> unique upper tickers, order kept. The ONE parser every
    consumer shares — the panel, the env producers and the worker payloads.

    The cap is applied HERE, not in the editor, so no consumer can be handed a basket over
    the limit: a settings file written by an older build, a pasted list, a session archive
    and a worker payload all funnel through this call. `cap=None` reads the raw list, which
    the editor needs to say HOW MANY pairs it had to drop."""
    out = list(dict.fromkeys(x.strip().upper() for x in (raw or '').split(',') if x.strip()))
    return out if cap is None else out[:cap]


def _tf_clean(tf):
    """Coerce a stored timeframe to a supported one — settings saved before a timeframe was
    dropped from the product ('5m') must fall back to daily instead of crashing the pipeline."""
    t = (tf or '1d').strip().lower()
    return t if t in TF_CHOICES else '1d'


def _child_cmd(role):
    """Command for the child process of role `role`: in the frozen build — the exe itself with
    --role, in dev — the real python with the script."""
    if apppaths.FROZEN:
        return [sys.executable, '--role', role]
    script = {'node': NODE_PY, 'fetch': FETCH_PY, 'portfolio': PORTFOLIO_PY,
              'signal': SIGNAL_PY, 'metrics': METRICS_PY, 'pdfreport': PDF_PY,
              'rescore': os.path.join(HERE, 'rescore_library.py'),
              'forward': os.path.join(HERE, 'forward_track.py')}[role]
    return [sys.executable, '-u', script]

DEFAULTS = {
    # resources / universe
    'cpu': 50,
    'universe_list': 'BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT',
    # search
    'pop': 200, 'gens': 25, 'seed': 0, 'pause': 5, 'port': 8787,   # seed 0 = auto (unique per node)
    # data
    'timeframe': '1d',      # bar size for the whole pipeline: 1d | 4h | 1h | 15m
    # node mode
    'explore_every': 4, 'seed_from_lib': True, 'max_rounds': 0, 'leaderboard': 20,
    # vault (prototype): sealing is always on (no user setting). The subscription key the customer
    # entered to unlock is remembered here, so unlocking is one click after the first time.
    'vault_license': '',
    # simulation
    'target_vol': 0.25, 'exec_cost': 0.001,
    # genome
    'max_depth': 6, 'max_size': 22,
    # selection (GA)
    'tournament': 5, 'elitism': 6, 'random_inject': 10, 'crossover_prob': 0.6,
    # fitness
    'parsimony': 0.010, 'corr_threshold': 0.70, 'corr_penalty': 0.5, 'hof_capacity': 15,
    'fit_blocks': 0,        # robust multi-block fitness (experimental); 0 = legacy min(TRAIN,VAL)
    'opt_winrate': False,   # objective: False = Sharpe, True = per-bar win rate (min TRAIN/VAL)
    # date segments (TRAIN < VAL < TEST)
    'train_start': 'auto', 'val_start': 'auto',      # 'auto' = the tf's window back from today
    'test_start': 'auto', 'test_end': 'today',
    # appearance
    'theme': '',            # 'light' | 'dark' | '' = follow the OS on first run
    'ui_mode': 'simple',    # first screen: 'simple' (autopilot + animation) | 'advanced' (dashboard).
    #                         Fresh installs open simple; _load flips a pre-existing settings file
    #                         to advanced so nobody's dashboard vanishes on update.
    'settings_open': False,  # settings pane hidden by default; toggled by the header button
    'lb_mode': 'all',       # leaderboard: 'all' = every alpha | 'families' = best per family (deduped)
    'card_order': [],       # dashboard card order (drag a card to reorder); [] = default layout
    'lb_rows': 12,          # leaderboard rows at natural height (when lb_h is 0)
    'lb_h': 0,              # leaderboard table height, dp; 0 = natural (lb_rows rows)
    'lb_cols': None,        # Advanced leaderboard: enabled OPTIONAL columns; None = default set
    'fwd_rows': 4,          # forward-track rows at natural height (when fwd_h is 0)
    'fwd_h': 0,             # forward-track table height, dp; 0 = natural (fwd_rows rows)
    'pf_h': 0,              # portfolio equity-plot height, px; 0 = automatic (from width)
}

# --- design palette: Linear/Stripe style, light + dark ---
# One entry per colour role. _apply_palette() publishes the active theme into the module-level
# constants below, which the whole file reads — so a widget just says fg=TXT and stays theme-correct.
PALETTE = {
    'light': dict(
        BG='#F5F3EC',        # warm cream ground (the 2026-09 redesign language)
        CARD='#FFFFFF',
        BORDER='#D3D1C7',    # hairline borders
        TXT='#2C2C2A',
        MUT='#5F5E5A',
        FAINT='#888780',
        ACC='#534AB7',       # muted indigo — text accents, lines, chart strokes
        ACC_HI='#463DA0',
        ACC_DN='#3C3489',
        ACC_SOFT='#EEEDFE',
        ACC_FILL='#534AB7',  # accent BUTTON fill: white text must hold on it in BOTH themes,
        ACC_FILL_HI='#463DA0',   # so the fill is its own token — dark ACC is far too light
        POS='#0F6E56',       # gain (deep green)
        NEG='#A32D2D',       # loss (brick red)
        LONGC='#534AB7',     # book side: indigo / amber. Deliberately NOT the P&L green/red —
        SHORTC='#854F0B',    # a position is a direction, not a profit, and the cool/warm
        #                      pair stays legible to a red-green colourblind eye
        HEAD_BG='#F1EFE8',   # tonal surfaces: tiles, buttons, fields
        HEAD_HI='#EAE7DC',
        STRIPE='#F1EFE8',    # row zebra / chart washes (the mock's --soft)
        GRID='#E9E6DC',
        CARD_BW=1,
        RUN_BG='#26215C', RUN_RING='#3C3489', RUN_ARC='#AFA9EC',   # the run pill + progress ring
        TIP_BG='#2C2C2A', TIP_FG='#F1EFE8', TIP_BD='#4A4945',
    ),
    'dark': dict(
        BG='#121212',
        CARD='#1C1C1C',
        BORDER='#2E2E2E',
        TXT='#F1EFE8',
        MUT='#B4B2A9',
        FAINT='#888780',
        ACC='#AFA9EC',       # lifted indigo for dark text accents / strokes
        ACC_HI='#CECBF6',
        ACC_DN='#3C3489',
        ACC_SOFT='#26215C',
        ACC_FILL='#3C3489',  # deep fill so the white button label stays readable
        ACC_FILL_HI='#4A41A6',
        POS='#5DCAA5',
        NEG='#F09595',
        LONGC='#AFA9EC',
        SHORTC='#FAC775',
        HEAD_BG='#262626',
        HEAD_HI='#2E2E2E',
        STRIPE='#212121',
        GRID='#282828',
        CARD_BW=1,           # the redesign keeps hairlines in the dark theme too
        RUN_BG='#3C3489', RUN_RING='#26215C', RUN_ARC='#CECBF6',
        TIP_BG='#262626', TIP_FG='#F1EFE8', TIP_BD='#3E3E3E',
    ),
}
# Published by _apply_palette(); declared here so the names exist at import time.
BG = CARD = BORDER = TXT = MUT = FAINT = ACC = ACC_HI = ACC_DN = ACC_SOFT = ''
POS = NEG = HEAD_BG = HEAD_HI = STRIPE = GRID = TIP_BG = TIP_FG = TIP_BD = ''
LONGC = SHORTC = ACC_FILL = ACC_FILL_HI = RUN_BG = RUN_RING = RUN_ARC = ''
CARD_BW = 0


def _apply_palette(theme):
    """Publish PALETTE[theme] into this module's globals and tell CustomTkinter which mode its own
    widgets should draw in. Returns the resolved theme name ('light'/'dark')."""
    if theme not in PALETTE:
        theme = 'light'
    globals().update(PALETTE[theme])
    ctk.set_appearance_mode('Dark' if theme == 'dark' else 'Light')
    return theme


def _system_theme():
    """What the OS is set to — the default for a first run, so we open in the user's own mode."""
    try:
        import darkdetect
        return 'dark' if (darkdetect.theme() or '').lower() == 'dark' else 'light'
    except Exception:                                       # noqa: BLE001 — optional, any failure -> light
        return 'light'


def _mix(c1, c2, t):
    """Blend two '#rrggbb' colours; t=0 -> c1, t=1 -> c2. Canvas has no alpha — this is it."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return '#%02x%02x%02x' % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _fix_corner_rendering():
    """CustomTkinter draws rounded corners by stamping glyphs from a bundled shapes font, and at a
    fractional widget scaling (any HiDPI screen: ours is 1.752) the glyph is sized up while the
    rectangle it caps is sized down — the corners bulge OUTSIDE the widget and every accent button
    looks like a cloud. Only saturated fills show it; on the near-white buttons the same bulge is
    invisible, which is why it survived this long. 'polygon_shapes' draws the same corners as
    smoothed polygons — no font, no rounding mismatch, correct at any scale."""
    try:
        from customtkinter.windows.widgets.core_rendering.draw_engine import DrawEngine
        DrawEngine.preferred_drawing_method = 'polygon_shapes'
    except Exception:                                    # noqa: BLE001 — a CTk layout change must
        pass                                             # never stop the app from starting


class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.logq = queue.Queue()
        self._panel_cache = {}                           # (instruments,start,end) -> (tk,panel,market,basket)
        self._plot_lock = threading.Lock()               # pyplot is global -> build one at a time
        self._plot_seq = 0
        self._metrics_cache = {}                          # formula -> {'long','short','win'} (on TEST)
        self._metrics_lock = threading.Lock()             # one worker batch at a time
        self._metrics_proc = None                         # the metrics child process
        self._metrics_seq = 0                             # to discard stale background computations
        self._row_items = {}                              # formula -> table row id (to update cells)
        self._pf_proc = None                              # portfolio-build subprocess
        self._fwd_proc = None                             # forward-track step subprocess
        self._fwd_entries = []                            # rows currently shown in the FORWARD table
        self._sigs = []                                   # running signal-API services (one per port)
        self._sig_health = {}                             # port -> status text (written by poll workers)
        self._sig_status_lbl = {}                         # port -> status Label (main thread only)
        self._sig_shown = None                            # ports currently rendered (rebuild only on change)
        self._sig_pending = None                          # services found by _sig_restore, for the tick
        self._sig_signal = {}                             # port -> last /signal payload (the positions)
        self._sig_seen = {}                               # port -> updated_at already pulled from /signal
        self._sig_canvas = {}                             # port -> positions canvas (main thread only)
        self._sig_drawn = {}                              # port -> updated_at last painted on that canvas
        self._pf_img_ref = None                           # keep a ref to the equity PhotoImage (else GC)
        self._pf_doc = None                               # last portfolio result (for re-render on resize)
        self._pf_resize_after = None                      # debounce id for resize re-render
        self._pf_last_w = 0                               # last render width (skip tiny resizes)
        self._shown = []                                 # what is actually shown in the table (for clicks)
        self._sort_col = 'fit'                            # leaderboard sort column (click a header)
        self._sort_desc = True                            # descending (best first)
        self._lb_select = 'fit'                           # POPULATION key: 'fit' = min(train,val); 'test' = held-out OOS
        self._lb_mode = 'all'                            # 'all' = every alpha | 'families' = best per family
        self._lib_cache = {'mtime': None, 'all': [], 'families': [], 'computing': False,
                           'dirty': False, 'ts': 0.0, 'computed': False, 'select': None}
        self._lb_target = 20                             # 'families' mode: how many DISTINCT families to keep
        self._vis_after = None                           # debounce handle for lazy per-viewport metrics
        self._fonts = {}                                  # (family,size,weight) -> named Tk font
        self._tip_win = None                             # tooltip window + deferred display
        self._tip_after = None
        self._fetching = False                           # a fetch_data child is running (one at a time)
        self._activating = False                         # a vault activation is rewriting library
        self._starting = False                           # files / a Start hub-check is in flight
        self.cfg = dict(DEFAULTS)
        self._load()
        self._adopt_shared_licence()                     # one activation per machine
        self._lb_mode = self.cfg.get('lb_mode') or 'all'   # remembered across restarts
        self.cfg['theme'] = _apply_palette(self.cfg.get('theme') or _system_theme())
        self._init_window()
        self._style()
        self._splash()                                    # logo intro; hides the window until done
        self._shell_mode = 'advanced'
        if (self.cfg.get('ui_mode') or 'advanced') == 'simple':
            self._build_simple()
        else:
            self._enter_advanced()
        if not getattr(self, '_sig_tick_on', False):      # runs under BOTH shells (every touch it
            self._sig_tick_on = True                      # makes is widget-guarded): adopted
            self._sig_tick()                              # services stay owned even before the
        #                                                  dashboard is ever built
        threading.Thread(target=self._sig_restore, daemon=True).start()   # re-adopt ones left running
        if not getattr(self, '_splash_on', False):       # first run: no data -> fetch 10 majors.
            self._boot_arm()                             # With a splash up its dialog must not pop
        #                                                  mid-intro — finish() arms it instead.
        root.protocol('WM_DELETE_WINDOW', self._on_close)
        self.root.after(400, self._eula_gate)            # one-time licence acceptance (post-splash)
        self.root.after(1500, self._auto_activate)       # seat + reveal for THIS install, no click

    # ---------- settings (persist) ----------
    def _load(self):
        try:
            saved = json.load(open(SETTINGS, encoding='utf-8'))
        except Exception:
            return                                       # fresh install -> DEFAULTS
        saved.pop('welcomed', None)                      # retired key — drop it from legacy files
        if saved.get('ui_mode') not in ('simple', 'advanced'):
            saved['ui_mode'] = 'advanced'                # a saved file predating (or from the OLD,
        #                                                  since-retired) Simple mode: its user has
        #                                                  a dashboard — keep it; only fresh
        #                                                  installs open on the simple screen
        self.cfg.update(saved)
        # 2026-08: the universe simplifies to ONE explicit list. 'All loaded pairs' migrates to
        # the pairs actually in that user's active snapshot — same basket, zero surprise; the
        # top-N / min-history download knobs retire (Start now fetches the configured list).
        if self.cfg.pop('universe_all', False):
            tick = self._snapshot_tickers()
            if tick:
                self.cfg['universe_list'] = ','.join(tick)
        for dead in ('fetch_n', 'fetch_years'):
            self.cfg.pop(dead, None)
        if not _parse_universe(self.cfg.get('universe_list') or ''):
            self.cfg['universe_list'] = DEFAULTS['universe_list']
        # The date boxes ship with the DAILY recommendation, so a settings file that names an
        # intraday timeframe without ever having passed through the timeframe selector carries
        # a span its bar size cannot hold — the 1d window is 59,880 bars of 1h, well past the
        # limit. Re-fill it from the saved timeframe's own recommendation, but ONLY when the
        # stored dates are verbatim some timeframe's defaults: that means nobody chose them,
        # so there is no user intent to overwrite. A hand-typed window is left alone and the
        # note under the fields argues with it instead.
        try:
            from timeframe import known as _tf_known, resolve as _rtf
            _t = _rtf(_tf_clean(self.cfg.get('timeframe', '1d')))
            _seg = {k: str(self.cfg.get(k, '')) for k in ('train_start', 'val_start',
                                                          'test_start')}

            def _legacy3(n):                             # the pinned pre-'auto' defaults a stale
                _x = _rtf(n)                             # settings file may still carry
                return {'train_start': _x.history, 'val_start': _x.val_start,
                        'test_start': _x.test_start}
            if any(_seg == _legacy3(n) for n in _tf_known()):
                self.cfg.update(train_start='auto', val_start='auto',
                                test_start='auto', test_end='today')
        except (ImportError, ValueError):                # no engine on the path: keep the file
            pass

    def _adopt_shared_licence(self):
        """One activation per MACHINE, whichever screen or install it happened on: a key the hub
        accepted anywhere here (dev checkout, .deb, AppImage; simple or advanced) sits in
        licence_store, and an install whose own settings hold none adopts it at launch. The
        startup auto-activation then makes this install whole — its seat, its sealed rows."""
        self._licence_adopted = False
        try:
            import licence_store
            tok = licence_store.load()
            if self.cfg.get('vault_license'):            # this install already has one: seed the
                if not tok:                              # store, so the others stop asking
                    licence_store.save(self.cfg['vault_license'])
                return
        except Exception:                                # noqa: BLE001 — never block a launch
            return
        if tok:
            self.cfg['vault_license'] = tok
            self._licence_adopted = True

    @staticmethod
    def _sealed_rows_exist(state_dir):
        """Does any local library still hold a row the vault sealed and nothing has revealed?"""
        try:
            names = sorted(os.listdir(state_dir))
        except OSError:
            return False
        for name in names:
            if not (name.startswith('library') and name.endswith('.jsonl')):
                continue
            try:
                with open(os.path.join(state_dir, name), encoding='utf-8') as f:
                    for ln in f:
                        if '"locked"' not in ln:
                            continue
                        try:
                            d = json.loads(ln)
                        except json.JSONDecodeError:
                            continue
                        if d.get('locked') and d.get('formula_enc') and not d.get('formula'):
                            return True
            except OSError:
                continue
        return False

    def _after_activation(self):
        """Every screen learns the library is open — whichever one the key was entered on:
        caches dropped, the dashboard table re-read, the simple cards re-rendered and the
        portfolio rebuilt with the real engine (a sealed preview is stale the moment rows open)."""
        self._lib_cache['ts'] = 0
        self._treesig = None
        self._sm_champ_sig = None
        if getattr(self, '_shell_mode', '') == 'simple':
            self._sm_lic_refresh()
            if (not (self.proc and self.proc.poll() is None)
                    and self._count_lines(self._lib_file()) >= 2):
                self._simple_build_portfolio()

    def _auto_activate(self):
        """Startup: with a known key, make THIS install whole without a click — claim its seat
        and reveal whatever its libraries still hold sealed: rows mined while the hub was down,
        or by an install that only just adopted the machine's key. Skipped when nothing is
        sealed (no hub round-trip for nothing), while a node runs, or while an activation is
        already in flight. A refusal is logged; for an adopted key it also hands the Activate
        button back instead of pretending."""
        tok = str(self.cfg.get('vault_license') or '')
        if not tok or self._activating or (self.proc and self.proc.poll() is None):
            return
        self._activating = True
        res = {}

        def work():
            try:
                if not self._sealed_rows_exist(STATE_DIR):
                    res['out'] = (None, None)
                else:
                    res['out'] = (self._vault_activate_all(tok), None)
            except Exception as ex:                      # noqa: BLE001
                res['out'] = ('', str(ex) or type(ex).__name__)

        def tick():
            if 'out' not in res:
                self.root.after(200, tick)
                return
            self._activating = False
            msg, err = res['out']
            if err:
                print(f'[vault] auto-activation skipped: {err}', flush=True)
                if self._licence_adopted:                # the hub said no to the shared key —
                    self.cfg['vault_license'] = ''       # this install is not activated, say so
                    self._licence_adopted = False
                    self._sm_lic_refresh()
                return
            if msg is not None:
                print(f'[vault] auto-activation: {msg}', flush=True)
                self._after_activation()
        threading.Thread(target=work, daemon=True).start()
        self.root.after(200, tick)

    @staticmethod
    def _gi(var, d):
        try:
            return int(float(var.get()))
        except Exception:
            return d

    @staticmethod
    def _gf(var, d):
        try:
            return float(var.get())
        except Exception:
            return d

    def _collect(self):
        if hasattr(self, 'e_uni'):
            self._uni_commit()                       # a pair still sitting in the add-box counts:
                                                     # CTk buttons never steal focus, so Start /
                                                     # Save / theme switch would read a stale list
        d = DEFAULTS
        return dict(
            cpu=self._gi(self.v_cpu, d['cpu']),
            universe_list=(','.join(_parse_universe(self.v_unilist.get()))
                           or d['universe_list']),
            pop=self._gi(self.v_pop, d['pop']), gens=self._gi(self.v_gens, d['gens']),
            seed=self._gi(self.v_seed, d['seed']), pause=self._gi(self.v_pause, d['pause']),
            port=self._gi(self.v_port, d['port']),
            timeframe=_tf_clean(self.v_tf.get()),
            explore_every=max(1, self._gi(self.v_explore, d['explore_every'])),
            seed_from_lib=bool(self.v_seedlib.get()),
            opt_winrate=bool(self.v_optwr.get()),
            max_rounds=self._gi(self.v_maxrounds, d['max_rounds']),
            leaderboard=self._gi(self.v_leader, d['leaderboard']),
            target_vol=self._gf(self.v_vol, d['target_vol']),
            exec_cost=self._gf(self.v_exec, d['exec_cost']),
            max_depth=self._gi(self.v_depth, d['max_depth']),
            max_size=self._gi(self.v_size, d['max_size']),
            tournament=self._gi(self.v_tourn, d['tournament']),
            elitism=self._gi(self.v_elit, d['elitism']),
            random_inject=self._gi(self.v_inject, d['random_inject']),
            crossover_prob=self._gf(self.v_cx, d['crossover_prob']),
            parsimony=self._gf(self.v_pars, d['parsimony']),
            corr_threshold=self._gf(self.v_corrt, d['corr_threshold']),
            corr_penalty=self._gf(self.v_corrp, d['corr_penalty']),
            hof_capacity=self._gi(self.v_hof, d['hof_capacity']),
            fit_blocks=self._gi(self.v_fitblocks, d['fit_blocks']),
            **self._collect_segments(),
        )

    def _save(self):
        self.cfg.update(self._collect())
        if (hasattr(self, 'v_unilist')
                and self.v_unilist.get() != self.cfg['universe_list']):
            self.v_unilist.set(self.cfg['universe_list'])   # empty list fell back to the default
                                                            # five — show what actually runs
        try:
            json.dump(self.cfg, open(SETTINGS, 'w', encoding='utf-8'), indent=2)
        except Exception:
            pass

    # ---------- style ----------
    def _px(self, n):
        """A size in CustomTkinter units (pixels at 100%) as a raw Tk font size.

        CTkFont measures in PIXELS and CTk multiplies by widget_scaling; a plain Tk/ttk font tuple
        measures in POINTS and is multiplied by `tk scaling` instead. Same tuple, different unit —
        so ttk and Canvas text must be asked for in negative (= pixel) sizes, pre-scaled by hand,
        or they come out ~2.3x the size of everything else on a HiDPI screen."""
        return -max(1, int(round(n * self.SCALE)))

    def _init_window(self):
        """One-time window setup. Kept out of _style() — that runs again on every theme switch, and
        re-applying geometry there would snap a window the user had resized back to the default."""
        # Tk reports the display's own scaling (pixels per point; 1.333 at 96 dpi). CustomTkinter
        # defaults to 1.0 on Linux and would draw a doll's-house UI next to the point-sized ttk
        # widgets, so hand it the display's real factor.
        self.SCALE = min(max(round(self.root.tk.call('tk', 'scaling') / 1.3333, 3), 1.0), 3.0)
        ctk.set_widget_scaling(self.SCALE)
        ctk.set_window_scaling(self.SCALE)
        _fix_corner_rendering()                          # must precede the first CTk widget
        try:
            import buildinfo
            self.root.title(f'AlphaNode  {buildinfo.build_label()}')
        except Exception:                                # noqa: BLE001
            self.root.title('AlphaNode')
        self.root.geometry('1100x860')                   # CTk scales this by window_scaling
        self.root.minsize(980, 680)                      # raw, like geometry: CTk scales it too, and
        #                                                  pre-scaling made the floor 1.75x too big

    # ---------- splash (logo intro) ----------
    def _boot_arm(self):
        """Schedule the first-run bootstrap check exactly once — it is armed either straight from
        __init__ (no splash) or from the splash's finish(), and a skip-click racing __init__ could
        otherwise arm it twice."""
        if getattr(self, '_shell_mode', 'advanced') == 'simple':
            return                                       # simple: Start owns the data step —
        #                                                  no first-run dialog over the intro
        if not getattr(self, '_boot_armed', False):
            self._boot_armed = True
            self.root.after(900, self._maybe_bootstrap)

    def _splash(self):
        """The logo animation before the dashboard opens: a swarm of candidate formulas pops in,
        selection culls all but one, and the surviving dot flies in to become the logo mark. Plays
        centered on the SCREEN while the main window stays hidden; a click skips it. The window is
        shown when the animation ends — and on ANY failure too: the splash must never brick the
        app. ALPHANODE_NO_SPLASH=1 disables it (smoke tests, headless runs)."""
        if os.environ.get('ALPHANODE_NO_SPLASH'):
            return
        top = None
        try:
            S = self.SCALE
            W, H = int(640 * S), int(340 * S)
            top = tk.Toplevel(self.root)
            top.overrideredirect(True)                   # bare rectangle: no WM title bar
            sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
            top.geometry(f'{W}x{H}+{(sw - W) // 2}+{max(0, (sh - H) // 2 - int(20 * S))}')
            try:
                top.attributes('-topmost', True)
            except tk.TclError:
                pass
            # An X11 toplevel is forever square, so the ROUNDING is painted: the canvas ground
            # is a shadow tone and the card itself is a smooth polygon with soft corners —
            # the four notches read as shadow on any desktop (user: 'углы не такие острые').
            back = _mix(BG, '#000000', 0.6)
            top.configure(bg=back)
            cv = tk.Canvas(top, width=W, height=H, bg=back, highlightthickness=0, bd=0)
            cv.place(x=0, y=0)
            r = 26 * S                                   # smoothing eats ~30%: lands near 16px
            x1, y1 = W - 2, H - 2
            cv.create_polygon(
                2 + r, 2, x1 - r, 2, x1, 2, x1, 2 + r, x1, y1 - r, x1, y1, x1 - r, y1,
                2 + r, y1, 2, y1, 2, y1 - r, 2, 2 + r, 2, 2,
                smooth=True, fill=BG, outline=BORDER)    # the hairline rides the curve
            self.root.withdraw()
            self._splash_on = True

            cx, cy = W / 2, H / 2
            # the lockup: [dot] [gap] AlphaNode — measured to center it as a whole
            # the app-wide font CACHE, not a transient Font: a transient's __del__ calls
            # Tcl from whatever thread runs the GC later — the X-protocol race behind the
            # SIGSEGV in Tk_MeasureChars/_XReply (core dump, 2026-09-01)
            f_word = self._font(self.UI, 52, 'bold')
            widths = [f_word.measure(ch) for ch in 'AlphaNode']
            dot_d, gap = 25 * S, 15 * S
            x0 = cx - (dot_d + gap + sum(widths)) / 2
            dcx, dcy = x0 + dot_d / 2, cy                # where the surviving dot lands
            lxs, ax = [], x0 + dot_d + gap
            for w in widths:
                lxs.append(ax)
                ax += w
            letters = [cv.create_text(lxs[i], cy, text=ch, font=f_word, fill=BG, anchor='w',
                                      state='hidden')
                       for i, ch in enumerate('AlphaNode')]
            f_mono = (self.MONO, self._px(11))
            tagline = cv.create_text(cx, cy + 46 * S, text='alpha-mining node · runs on your machine',
                                     font=f_mono, fill=BG, state='hidden')
            chip = cv.create_text(x0, cy - 48 * S, text='* new best · fitness +2.49',
                                  font=f_mono, fill=BG, anchor='w', state='hidden')

            # the swarm: formula fragments + candidate dots on a loose ellipse around the center
            FR = ('cs_zscore(x)', 'ts_delta:12', 'ema:132(close)', 'cs_rank(v)', 'cs_demean(p)',
                  'ts_zscore:197', 'range_hl', 'cs_scale(r)', 'funding_8h', 'oi_delta')
            C_FRAG = _mix(BG, FAINT, 0.55)               # canvas has no alpha: bake the opacity in
            frags = [(cv.create_text((0.05 + random.random() * 0.78) * W,
                                     (0.06 + random.random() * 0.84) * H,
                                     text=FR[i], font=f_mono, fill=BG, anchor='w'),
                      random.random() * 400) for i in range(len(FR))]
            N, R0, RL = 11, 4.5 * S, 12.5 * S
            surv_i = random.randrange(N)
            dots = []
            for j in range(N):
                a = (j / N) * 6.2832 + random.random() * 0.5
                r = (60 + random.random() * 85) * S
                x = min(max(cx + math.cos(a) * r * 1.7, 16 * S), W - 16 * S)
                y = min(max(cy - 14 * S + math.sin(a) * r * 0.72, 14 * S), H - 40 * S)
                dots.append({'it': cv.create_oval(x, y, x, y, fill=BG, outline=''),
                             'x': x, 'y': y, 'pop': 120 + j * 45,
                             'die': 850 + len(dots) * 55 if j != surv_i else 10 ** 9})
            surv = dots[surv_i]
            sx, sy = surv['x'], surv['y']
            ring = cv.create_oval(sx, sy, sx, sy, fill='', outline=BG,
                                  width=max(1, round(1.5 * S)), state='hidden')
            for it in (*letters, tagline, chip):         # canvas hides by colour, not alpha: swarm
                cv.tag_raise(it)                         # leftovers must sit UNDER the lockup text

            T_END = 4400
            sp = {'t0': None, 'done': False, 'after': None}
            self._sp = sp

            def win(t, at, dur):
                return max(0.0, min(1.0, (t - at) / dur))

            def ease_out(k):
                return 1 - (1 - k) ** 3

            def back(k, c):                              # ease-out with a slight overshoot past 1
                k -= 1
                return 1 + (c + 1) * k ** 3 + c * k ** 2

            def frame(t):
                for it, dly in frags:
                    k = win(t, dly, 500) * (1 - win(t, 1100 + dly, 500))
                    cv.itemconfig(it, fill=_mix(BG, C_FRAG, k))
                for d in dots:
                    k = win(t, d['pop'], 350)
                    if k <= 0:
                        continue
                    rr = R0 * (0.3 + 0.7 * back(k, 1.4))
                    x, y, col = d['x'], d['y'], _mix(BG, FAINT, k)
                    if d is surv:
                        if t >= 1550:
                            col = POS
                        f = win(t, 1950, 640)
                        if f > 0:                        # the flight into the logo mark
                            b = back(f, 0.7)
                            x, y = sx + (dcx - sx) * b, sy + (dcy - sy) * b
                            rr, col = R0 + (RL - R0) * f, _mix(POS, ACC, f)
                    else:
                        e = win(t, d['die'], 420)
                        if e >= 1:                       # fully faded: stop painting (a BG-coloured
                            cv.itemconfig(d['it'], state='hidden')   # blob still occludes things)
                            continue
                        if e > 0:                        # culled: flush red, sink, fade
                            col = (_mix(FAINT, NEG, e / 0.2) if e < 0.2
                                   else _mix(NEG, BG, (e - 0.2) / 0.8))
                            y += 16 * S * e * e
                            rr *= 1 - 0.6 * e
                    cv.coords(d['it'], x - rr, y - rr, x + rr, y + rr)
                    cv.itemconfig(d['it'], fill=col)
                rk = win(t, 1550, 650)
                if 0 < rk < 1:
                    rr = 6 * S + 21 * S * ease_out(rk)
                    cv.coords(ring, sx - rr, sy - rr, sx + rr, sy + rr)
                    cv.itemconfig(ring, outline=_mix(POS, BG, rk), state='normal')
                elif rk >= 1:
                    cv.itemconfig(ring, state='hidden')
                for i, it in enumerate(letters):
                    k = win(t, 2590 + i * 42, 520)
                    cv.coords(it, lxs[i], cy + 18 * S * (1 - (back(k, 0.5) if k > 0 else 0)))
                    cv.itemconfig(it, fill=_mix(BG, TXT, min(1.0, k / 0.55)),
                                  state='normal' if k > 0 else 'hidden')
                kt = win(t, 3240, 500)
                cv.itemconfig(tagline, fill=_mix(BG, MUT, kt),
                              state='normal' if kt > 0 else 'hidden')
                kc = win(t, 3390, 200) * (1 - win(t, 4090, 200))
                cv.itemconfig(chip, fill=_mix(BG, POS, kc),
                              state='normal' if kc > 0 else 'hidden')

            def finish(_=None):
                if sp['done']:
                    return
                sp['done'] = True
                self._splash_on = False
                if sp['after'] is not None:
                    try:
                        top.after_cancel(sp['after'])
                    except Exception:                    # noqa: BLE001
                        pass
                try:
                    top.destroy()
                except Exception:                        # noqa: BLE001
                    pass
                try:
                    self.root.deiconify()
                    self.root.lift()
                    self.root.focus_force()
                except tk.TclError:
                    pass
                self._boot_arm()

            def tick():
                if sp['done']:
                    return
                if sp['t0'] is None:                     # clock starts when the UI is actually live,
                    sp['t0'] = time.monotonic()          # not while __init__ is still building
                t = (time.monotonic() - sp['t0']) * 1000.0
                try:
                    frame(t)
                except tk.TclError:                      # window died under us — just show the app
                    finish()
                    return
                if t >= T_END:
                    finish()
                else:
                    sp['after'] = top.after(33, tick)

            cv.bind('<Button-1>', finish)
            top.bind('<Escape>', finish)
            frame(0)                                     # paint the empty stage right away…
            top.update_idletasks()
            top.update()                                 # …so the window isn't a grey flash
            sp['after'] = top.after(15, tick)
        except Exception:                                # noqa: BLE001 — the intro is optional
            self._splash_on = False
            try:
                if top is not None:
                    top.destroy()
            except Exception:                            # noqa: BLE001
                pass
            try:
                self.root.deiconify()
            except tk.TclError:
                pass

    def _style(self):
        """Fonts + the ttk styles for the widgets CustomTkinter has no answer for (the leaderboard
        table and the numeric spinboxes). Everything else is styled per-widget from the palette."""
        self.root.configure(fg_color=BG)

        fams = set(tkfont.families(self.root))

        def pick(prefs, dflt):
            for f in prefs:
                if f in fams:
                    return f
            return dflt
        self.UI = pick(['Inter', 'SF Pro Text', 'Helvetica Neue', 'Segoe UI', 'Ubuntu',
                        'Cantarell', 'Noto Sans', 'Roboto', 'DejaVu Sans'], 'TkDefaultFont')
        self.MONO = pick(['JetBrains Mono', 'Fira Code', 'Cascadia Code', 'SF Mono', 'Menlo',
                          'Consolas', 'Ubuntu Mono', 'DejaVu Sans Mono'], 'TkFixedFont')
        for nm in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont'):  # nice font everywhere
            try:
                tkfont.nametofont(nm).configure(family=self.UI)
            except tk.TclError:
                pass
        try:
            tkfont.nametofont('TkFixedFont').configure(family=self.MONO)
        except tk.TclError:
            pass

        F = self.UI
        s = ttk.Style()
        try:
            s.theme_use('clam')                          # the only built-in theme that honours colours
        except tk.TclError:
            pass
        s.configure('.', background=CARD, foreground=TXT, font=(F, self._px(13)))

        # numeric spinboxes — CustomTkinter has no spinbox, so these stay ttk (tonal, like _entry).
        # The up/down arrows are dropped from the LAYOUT rather than the widget: every setting is
        # typed, not nudged one step at a time, and the stepper column only ate width and drew a
        # gray smear next to each value. The widget itself stays a Spinbox — same variable types,
        # same from_/to, same styling hooks — it just renders as a plain field.
        try:
            s.layout('TSpinbox', [('Spinbox.field', {
                'side': 'top', 'sticky': 'we', 'children': [
                    ('Spinbox.padding', {'sticky': 'nswe', 'children': [
                        ('Spinbox.textarea', {'sticky': 'nswe'})]})]})])
        except tk.TclError:                              # a theme without these elements: keep
            pass                                         # the default layout, arrows and all
        s.configure('TSpinbox', fieldbackground=HEAD_BG, background=HEAD_BG, foreground=TXT,
                    arrowcolor=MUT, bordercolor=BORDER, lightcolor=HEAD_BG, darkcolor=HEAD_BG,
                    borderwidth=1, padding=int(4 * self.SCALE), insertcolor=TXT,
                    font=self._font(F, 13))
        s.map('TSpinbox', bordercolor=[('focus', ACC)], lightcolor=[('focus', ACC)],
              darkcolor=[('focus', ACC)])

        # comboboxes (Timeframe, portfolio "by …") — clam's default is a light-gray box that
        # glares on a dark card, and in 'readonly' the text is drawn as a SELECTION, which is
        # what read as a white smear. Same tonal look as the spinboxes/entries.
        s.configure('TCombobox', fieldbackground=HEAD_BG, background=HEAD_BG, foreground=TXT,
                    arrowcolor=MUT, bordercolor=BORDER, lightcolor=HEAD_BG, darkcolor=HEAD_BG,
                    borderwidth=1, padding=int(4 * self.SCALE), insertcolor=TXT,
                    font=self._font(F, 13))
        s.map('TCombobox',
              fieldbackground=[('readonly', HEAD_BG), ('disabled', CARD)],
              background=[('readonly', HEAD_BG), ('active', HEAD_HI)],
              foreground=[('readonly', TXT), ('disabled', FAINT)],
              selectbackground=[('readonly', HEAD_BG)],
              selectforeground=[('readonly', TXT)],
              bordercolor=[('focus', ACC)], lightcolor=[('focus', ACC)],
              darkcolor=[('focus', ACC)], arrowcolor=[('disabled', FAINT)])
        # the drop-down list is a plain Tk listbox created by ttk — reachable only via the
        # option database; re-adding on a theme switch overrides the previous values
        for opt, val in (('background', HEAD_BG), ('foreground', TXT),
                         ('selectBackground', ACC_SOFT), ('selectForeground', TXT),
                         ('borderWidth', 0), ('font', self._font(F, 13))):
            self.root.option_add(f'*TCombobox*Listbox.{opt}', val)

        # the leaderboard — CustomTkinter has no table, so this stays a ttk.Treeview.
        # rowheight/fonts are deliberately roomier than ttk's defaults: this table IS the app.
        # bordercolor matters: clam draws a frame around the field, which reads as a stray light
        # rectangle on a dark card unless it matches the card
        s.configure('Treeview', rowheight=int(32 * self.SCALE), fieldbackground=CARD, background=CARD,
                    foreground=TXT, borderwidth=0, relief='flat', bordercolor=CARD,
                    lightcolor=CARD, darkcolor=CARD, font=(self.MONO, self._px(12)))
        s.configure('Treeview.Heading', font=(F, self._px(10.5), 'bold'), foreground=FAINT,
                    background=CARD, relief='flat',
                    padding=(int(8 * self.SCALE), int(9 * self.SCALE)), bordercolor=CARD)
        s.map('Treeview.Heading', background=[('active', HEAD_BG)])
        s.map('Treeview', background=[('selected', ACC_SOFT)], foreground=[('selected', TXT)])
        s.layout('Treeview.Item',                        # drop the indent reserved for tree handles
                 [('Treeitem.padding', {'sticky': 'nswe', 'children':
                  [('Treeitem.text', {'side': 'left', 'sticky': ''})]})])

    # ---------- cheap widgets ----------
    # CustomTkinter draws everything on canvases: a CTkLabel is three X widgets (frame + canvas +
    # label) and costs ~2x a tk.Label to create, for pixels that are identical when the background
    # is flat. Widget count is what startup time tracks here, so CTk is reserved for the widgets
    # whose look it actually changes — buttons, entry, slider, switch, checkbox, scrollbar, cards —
    # and static text / plain containers use these.
    def _font(self, family, size, weight='normal'):
        """A NAMED font, created once per (family, size, weight) and referenced by name.

        A font tuple makes Tk resolve the family again for every widget it is handed to; a named
        font is resolved once. On this display that is ~2ms vs ~4ms per label — with ~70 labels it
        is worth the cache. Names live in Tk, so the cache survives a theme rebuild."""
        key = (family, size, weight)
        f = self._fonts.get(key)
        if f is None:
            # Hold the Font OBJECT, not just its name: tkinter's Font.__del__ deletes the named font
            # from Tk, and every widget still pointing at it silently falls back to the default.
            f = tkfont.Font(name=f'AN{len(self._fonts)}', family=family, size=self._px(size),
                            weight=weight, exists=False)
            self._fonts[key] = f
        return f

    def _lbl(self, parent, text='', text_color=None, font=None, bg=None, **kw):
        """Static text. Same call shape as CTkLabel, one X widget instead of three."""
        fam, size, *rest = font if font else (self.UI, 13)
        if 'wraplength' in kw:                           # CTk scales it for you; tk does not
            kw['wraplength'] = int(kw['wraplength'] * self.SCALE)
        kw.setdefault('anchor', 'w')                     # tk centres by default, so a label wider
        kw.setdefault('justify', 'left')                 # than its slot loses BOTH ends, not one
        return tk.Label(parent, text=text, fg=(text_color or TXT), bg=(bg or CARD),
                        font=self._font(fam, size, rest[0] if rest else 'normal'), **kw)

    def _box(self, parent, bg=None, **kw):
        """A plain layout container (what CTkFrame(fg_color='transparent') was for)."""
        return tk.Frame(parent, bg=(bg or CARD), **kw)

    def _hide_when_tight(self, container, widget, pad=24):
        """Show `widget` only while `container` fits ALL its children at natural width — a tk
        label clips per-pixel, and a half-word of metadata ('n' from 'node …', 'Ctrl+' from a
        shortcut list) is worse than no metadata. The widget must be packed LAST in its row so
        re-showing restores the original order."""
        info = {}

        def fit(_e=None):
            shown = widget.winfo_manager()
            need = sum(w.winfo_reqwidth() for w in container.winfo_children()
                       if w is widget or w.winfo_manager()) + int(pad * self.SCALE)
            if container.winfo_width() < need:
                if shown:
                    info.update({k: v for k, v in widget.pack_info().items() if k != 'in'})
                    widget.pack_forget()
            elif not shown and info:
                widget.pack(**info)
        container.bind('<Configure>', fit, add='+')

    def _card(self, parent, **kw):
        """A card: the surface every panel sits on. Stays CTk — the rounded corner is the point.
        Dark theme drops the border entirely (CARD_BW=0): depth comes from surface tones."""
        return ctk.CTkFrame(parent, fg_color=CARD, border_color=BORDER, border_width=CARD_BW,
                            corner_radius=14, **kw)

    def _pad(self, card):
        """Inner padding frame for a card (CTkFrame has no padding option)."""
        f = self._box(card)
        f.pack(fill='both', expand=True, padx=18, pady=16)
        return f

    # ---------- layout ----------
    def _build(self):
        # Everything lives inside _shell so a theme switch can drop and rebuild the whole UI without
        # touching the root's other children (open dialogs, tooltips).
        self._shell = self._box(self.root, bg=BG)
        self._shell.pack(fill='both', expand=True)
        bar = tk.Canvas(self._shell, height=3, bg=ACC, highlightthickness=0)   # accent gradient bar
        bar.pack(fill='x')

        def _grad(e, cv=bar):
            cv.delete('all')
            n = 64
            for i in range(n):
                cv.create_rectangle(e.width * i / n, 0, e.width * (i + 1) / n + 1, 4,
                                    fill=_mix(ACC, ACC_DN, i / (n - 1)), outline='')
        bar.bind('<Configure>', _grad)
        top = self._box(self._shell, bg=BG)
        top.pack(fill='x', padx=20, pady=(14, 11))
        brand = self._box(top, bg=BG)
        brand.pack(side='left')
        # wordmark only — no logo glyph in front of it
        self._lbl(brand, text='Alpha', font=(self.UI, 24, 'bold'), text_color=TXT, bg=BG).pack(side='left')
        self._lbl(brand, text='Node', font=(self.UI, 24, 'bold'), text_color=ACC, bg=BG).pack(side='left')
        self._build_theme_pick(top)
        # header controls: the node is driven from here — defaults just work.
        self.btn_settings = self._btn(top, '⚙  Settings', self._toggle_settings, kind='soft',
                                      height=34, width=112)
        self.btn_settings.pack(side='right', padx=(0, 16), pady=(2, 0))
        self._tip(self.btn_settings, 'Show / hide the search settings panel.')
        b_simple = self._btn(top, '◱  Simple', lambda: self._switch_mode('simple'), kind='soft',
                             height=34, width=96)
        b_simple.pack(side='right', padx=(0, 8), pady=(2, 0))
        self._tip(b_simple, 'Back to the simple screen. The search, the served APIs and the\n'
                            'forward track all keep running — only the face changes.')
        self.btn_sessions = self._btn(top, '⧉  Sessions', self._sessions_open, kind='soft',
                                      height=34, width=112)
        self.btn_sessions.pack(side='right', padx=(0, 8), pady=(2, 0))
        self._tip(self.btn_sessions, 'Snapshots of the whole workspace — formulas, forward\n'
                                     'track, portfolio, settings. Save one, load an older one;\n'
                                     'an auto snapshot is taken every time the node stops.')
        pill = tk.Canvas(top, width=int(150 * self.SCALE), height=int(38 * self.SCALE),
                         bg=BG, highlightthickness=0, bd=0, cursor='hand2')
        pill.pack(side='right', padx=(0, 8), pady=(2, 0))
        pill.bind('<Button-1>', lambda _e: self._run_pill_click())
        self.run_pill = pill
        self._pill_paint(pill, 'idle', None, 'Start node')
        self._tip(pill, 'One control for the node: press to start the search, press again to\n'
                        'stop it gently (the current round finishes). While running, the ring\n'
                        'is the round progress from the ETA model.')
        # metadata packs LAST: on a narrow window pack squeezes the latest widgets first, and the
        # subtitle/node-id must lose that fight — never the Start button (it clipped to 'tart n')
        self._lbl(top, text='background search for trading strategies', text_color=MUT,
                     font=(self.UI, 13), bg=BG).pack(side='left', padx=(14, 0), pady=(6, 0))
        # node + session ids hide as ONE unit: _hide_when_tight manages a single widget
        # that must be packed last, and two independently-hidden labels would re-show in
        # whichever order their handlers fired
        meta = self._box(top, bg=BG)
        meta.pack(side='left', padx=(10, 0), pady=(7, 0))
        nid_lbl = self._lbl(meta, text=f'node {self._node_id()}', text_color=FAINT,
                            font=(self.MONO, 12), bg=BG)
        nid_lbl.pack(side='left')
        self._tip(nid_lbl, 'This install\'s node ID. It mints the search seed, so every node\n'
                           'walks its own path through formula space — no two nodes mine\n'
                           'the same library.')
        self.sid_lbl = self._lbl(meta, text=f'session {self._session_id()}', text_color=FAINT,
                                 font=(self.MONO, 12), bg=BG)
        self.sid_lbl.pack(side='left', padx=(10, 0))
        self._tip(self.sid_lbl,
                  'The CURRENT working session. A new id is minted by \'Clear all history\';\n'
                  'loading a saved session adopts its id; reopening the app or pressing\n'
                  'Start continues the same session. Forward-track entries record this id\n'
                  'at enrollment — the track outlives sessions, and the stamp says where\n'
                  'each strategy came from.')
        self._hide_when_tight(top, meta)
        self._box(self._shell, bg=BORDER, height=1).pack(fill='x')       # hairline
        # settings | dashboard live in a PanedWindow for the hide/show plumbing; the split itself
        # is fixed — always the settings content's natural width (see _sash_restore)
        body = tk.PanedWindow(self._shell, orient='horizontal', bg=BG, bd=0,
                              sashwidth=max(8, int(8 * self.SCALE)), sashpad=0, sashrelief='flat',
                              opaqueresize=True)
        body.pack(fill='both', expand=True, padx=20, pady=16)
        self._paned = body

        self._build_settings(body)
        self._build_status(body)
        self._apply_settings_vis()
        # the split is NOT draggable: the settings pane is always exactly as wide as its content.
        # A user-movable sash kept reopening the pane too narrow for its own input fields.
        body.bind('<Button-1>', lambda e: 'break' if body.identify(e.x, e.y) else None)

    # ---------- theme ----------
    def _build_theme_pick(self, top):
        # A switch rather than a segmented button: CTkSegmentedButton has one text_color for both
        # states, so the selected label would lose its contrast against the accent fill.
        self.v_dark = tk.BooleanVar(value=self.cfg.get('theme') == 'dark')
        self.sw_theme = ctk.CTkSwitch(
            top, text=('Dark' if self.v_dark.get() else 'Light'), variable=self.v_dark,
            command=self._on_theme_pick, font=(self.UI, 13), text_color=MUT,
            progress_color=ACC, button_color=CARD, button_hover_color=CARD,
            fg_color=HEAD_BG, border_color=BORDER, switch_width=40, switch_height=20)
        self.sw_theme.pack(side='right', pady=(5, 0))
        self._tip(self.sw_theme, 'Light / dark appearance. The tables and the equity images\n'
                                 'are redrawn to match.')

    def _on_theme_pick(self):
        self._set_theme('dark' if self.v_dark.get() else 'light')

    def _set_theme(self, theme):
        """Re-palette and rebuild the window. A rebuild (rather than a live restyle) is what keeps
        the non-CTk parts — Treeview, the Canvas chart, the matplotlib PNGs — in the same theme."""
        if theme == self.cfg.get('theme'):
            return
        self.cfg['theme'] = theme
        self._save()                                     # keep the current field values across it
        self._tip_hide()
        if self._pf_resize_after:
            self.root.after_cancel(self._pf_resize_after)
            self._pf_resize_after = None
        _apply_palette(theme)
        self._style()
        self._shell.destroy()
        if self._shell_mode == 'simple':                 # the simple shell re-paints itself whole;
            self._build_simple()                         # none of the dashboard re-renders below
            return                                       # exist in this mode
        self._pf_last_w = 0                              # force the equity image to re-render
        self._treesig = None                             # and the table to re-fill
        self._sig_shown = None
        self._build()
        self._set_running(bool(self.proc and self.proc.poll() is None))
        self._render_signal_rows()
        if self._lib_cache.get('computed'):              # the fresh (empty) table repaints from the
            self._render_lb(self._lb_rows())             # cache NOW — its dirty flag is long spent,
        #                                                  and mtime won't budge until the next round
        elif self._shown:                                # no library file yet (fresh timeframe):
            self._fill_tree(list(self._shown))           # keep the status-fed rows, don't go blank
        if self._pf_doc:
            self._render_portfolio(self._pf_doc)

    def _node_id(self):
        """This install's persistent node ID (state/node_id) — minted here on the very first
        run, or by node.py, whichever comes first; both write the same format. It derives the
        auto seed, which is what makes every install's search trajectory unique."""
        path = os.path.join(STATE_DIR, 'node_id')
        try:
            nid = open(path, encoding='utf-8').read().strip().lower()
        except OSError:
            nid = ''
        if not (len(nid) == 8 and all(c in '0123456789abcdef' for c in nid)):
            import secrets
            nid = secrets.token_hex(4)
            try:
                os.makedirs(STATE_DIR, exist_ok=True)
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(nid + '\n')
            except OSError:
                pass
        return nid

    # ---------- left panel: ALL settings (scrollable, hidden by default) ----------
    def _toggle_settings(self):
        self.cfg['settings_open'] = not self.cfg.get('settings_open')
        self._apply_settings_vis()
        self._save()

    def _apply_settings_vis(self):
        """Show/hide the settings pane (tk paned '-hide': the pane and its sash vanish together).
        The default view is just the dashboard; the choice persists across restarts."""
        shown = bool(self.cfg.get('settings_open'))
        try:
            self._paned.paneconfigure(self._settings_outer, hide=not shown)
        except tk.TclError:
            pass
        if shown:
            self._sash_restore()                         # place NOW — the old 120ms delay made the
            self.root.after_idle(self._sash_restore)     # pane visibly stretch a beat after opening
            self.root.after(150, self._sash_restore)     # once more after CTk's late relayout
        if self.btn_settings is not None:
            self.btn_settings.configure(border_color=(ACC if shown else BORDER),
                                        text_color=(ACC if shown else TXT))

    # ---------- fixed split (settings | dashboard) ----------
    def _sash_restore(self):
        """The settings pane is EXACTLY as wide as its content — always. No user sash, no saved
        width: the old draggable/persisted split kept drifting and reopened the pane too narrow
        for its own input fields. Idempotent; re-run whenever the content width changes."""
        nat = self._settings_inner.winfo_reqwidth()
        if nat <= 1:
            return                                       # pre-layout: nothing to measure yet
        try:
            self._paned.sash_place(0, nat + int(48 * self.SCALE), 1)
        except tk.TclError:
            pass

    def _build_settings(self, body):
        # Always built (every tk variable must exist for _collect/start), but shown only while
        # cfg['settings_open'] — see _apply_settings_vis().
        outer = self._card(body)
        self._settings_outer = outer
        body.add(outer, minsize=int(230 * self.SCALE), stretch='never')
        # hand-rolled scroller rather than CTkScrollableFrame: the width has to come from the content
        # itself (_sync), which stays correct under HiDPI font scaling — a fixed width would clip.
        canvas = tk.Canvas(outer, bg=CARD, highlightthickness=0)
        vsb = ctk.CTkScrollbar(outer, orientation='vertical', command=canvas.yview,
                               fg_color=CARD, button_color=BORDER, button_hover_color=FAINT, width=14)
        canvas.configure(yscrollcommand=vsb.set)
        self._settings_canvas = canvas
        # the scrollbar packs FIRST: when the pane is dragged narrower than the content, whoever
        # packed last is squeezed out first — it must be the canvas that clips, not the scrollbar
        vsb.pack(side='right', fill='y', padx=(0, 6), pady=14)
        canvas.pack(side='left', fill='both', expand=True, padx=(14, 0), pady=14)
        inner = self._box(canvas)
        self._settings_inner = inner                     # its reqwidth IS the pane width
        canvas.create_window((0, 0), window=inner, anchor='nw')

        def _sync(_e=None):     # width is set by the content ITSELF — correct even with HiDPI font scaling
            canvas.configure(width=inner.winfo_reqwidth(), scrollregion=canvas.bbox('all'))
            if self.cfg.get('settings_open'):
                self._sash_restore()                     # content width changed (e.g. the timeframe
        inner.bind('<Configure>', _sync)                 # note) — the pane follows, fields never clip
        self._bind_wheel(canvas)

        self._head(inner, 'SEARCH SETTINGS').pack(anchor='w', pady=(0, 2))
        self._lbl(inner, text='★ locked to the session — to change these, stop and Clear node',
                  text_color=FAINT, font=(self.UI, 11), wraplength=self.UNI_WRAP,
                  justify='left').pack(anchor='w', pady=(0, 10))

        # --- resources ---
        self._lbl(inner, text='Resources (CPU share)', text_color=MUT,
                     font=(self.UI, 13)).pack(anchor='w')
        self.v_cpu = tk.IntVar(value=self.cfg['cpu'])
        self.lbl_cpu = self._lbl(inner, text='', text_color=TXT, font=(self.UI, 15, 'bold'))
        self.lbl_cpu.pack(anchor='w', pady=(2, 0))
        sc = ctk.CTkSlider(inner, from_=5, to=95, variable=self.v_cpu, command=lambda e: self._cpu_lbl(),
                           button_color=ACC, button_hover_color=ACC_HI, progress_color=ACC,
                           fg_color=HEAD_BG, height=14)
        sc.pack(fill='x', pady=(4, 12))
        self._cpu_lbl()
        cpu_tip = 'How many cores to give the search. More — faster, but higher load on the PC.'
        self._tip(self.lbl_cpu, cpu_tip)
        self._tip(sc, cpu_tip)

        # --- pairs universe (ONE explicit list — the search, signals and metrics all
        #     run on exactly this basket; market data downloads itself at Start) ---
        self._lbl(inner, text='★ Which pairs to trade', text_color=MUT,
                     font=(self.UI, 13)).pack(anchor='w', pady=(0, 2))
        self.v_unilist = tk.StringVar(value=self.cfg['universe_list'])
        self._uni_build(inner)
        tfrow = self._box(inner)
        tfrow.pack(fill='x', pady=(6, 0))
        self._lbl(tfrow, text='Timeframe (bar size)', text_color=TXT,
                     font=(self.UI, 13)).pack(side='left')
        self.v_tf = tk.StringVar(value=_tf_clean(self.cfg.get('timeframe', '1d')))
        tf_box = ttk.Combobox(tfrow, textvariable=self.v_tf, values=TF_CHOICES,
                              state='readonly', width=6)
        tf_box.pack(side='right')
        tf_box.bind('<<ComboboxSelected>>', self._on_tf_change)
        self._tip(tf_box, 'Bar size for the WHOLE pipeline: search, metrics, signals.\n'
                          '1d — the classic daily engine. Intraday (4h/1h/15m):\n'
                          '• each timeframe keeps its own data snapshot (data_<tf>.pickle)\n'
                          '  and its own alpha library — data downloads itself at Start;\n'
                          '• picking a timeframe fills in its recommended date segments\n'
                          '  (intraday history is shorter and denser);\n'
                          '• portfolio build and the forward track are daily-only for now.')
        self.lbl_tf_note = self._lbl(inner, text='', text_color=MUT, font=(self.UI, 11),
                                     anchor='w', justify='left', wraplength=330)
        # wraplength: the intraday note is the widest line in the pane — unwrapped it would
        # change the pane's width on every timeframe pick
        self.lbl_tf_note.pack(anchor='w', pady=(3, 0))
        self._lbl(inner, text='Market data for these pairs downloads itself when the node starts:'
                              ' first run, a new pair,\na new timeframe or a stale snapshot all'
                              ' top up automatically.',
                  text_color=FAINT, font=(self.UI, 11), anchor='w', justify='left',
                  wraplength=330).pack(anchor='w', pady=(10, 0))

        # --- search ---
        g = self._section(inner, 'SEARCH')
        self.v_pop = self._num(g, 'Population', self.cfg['pop'], 0, 4, 4000, 10,
                               tip='How many candidate formulas per generation. More — broader coverage, but slower.')
        self.v_gens = self._num(g, 'Generations', self.cfg['gens'], 1, 1, 500, 1,
                                tip='How many generations of evolution per round.')
        self.v_seed = self._num(g, 'Seed (0 = auto)', self.cfg['seed'], 2, 0, 999999, 1,
                                tip='0 / auto — a unique per-install seed minted from this node\'s ID:\n'
                                    'every node walks its own path through formula space, so no two\n'
                                    'installs mine the same library. Set an integer to reproduce a run.')
        self.v_pause = self._num(g, 'Pause, sec', self.cfg['pause'], 3, 0, 3600, 1,
                                 tip='Pause between rounds so the machine gets a breather.')
        self.v_port = self._num(g, 'Status port', self.cfg['port'], 4, 1024, 65535, 1,
                                tip='Port for the status web page (http://localhost:PORT).')
        self.v_optwr = self._chk(g, '★ Optimize by win rate', self.cfg.get('opt_winrate', False), 5,
                                 tip='The search maximizes min(TRAIN, VAL) of the per-bar win rate,\n'
                                     'shrunk by evidence (damped by activity share, minus a binomial SE)\n'
                                     'so sparse lucky streaks can\'t outrank dense honest ones.\n'
                                     'CAUTION: win rate ignores magnitude — many small wins can hide rare\n'
                                     'big losses; judge champions by TEST Sharpe and maxDD as usual.\n'
                                     'Takes effect at the next Start.')

        # --- node mode ---
        g = self._section(inner, 'NODE MODE (continuous search)')
        self.v_explore = self._num(g, 'Explore every N-th', self.cfg['explore_every'], 0, 1, 100, 1,
                                   tip='Every N-th round — a search from scratch (for diversity); the other\n'
                                       'rounds refine the library champions (warm-start). N=1 means EVERY\n'
                                       'round explores from scratch and refinement never runs. Recommended: 3-4.')
        self.v_maxrounds = self._num(g, 'Max. rounds (0=∞)', self.cfg['max_rounds'], 1, 0, 999999, 1,
                                     tip='How many rounds to run before stopping. 0 — run forever.')
        self.v_leader = self._num(g, 'Leaderboard size', self.cfg['leaderboard'], 2, 1, 200, 1,
                                  tip='How many best alphas to keep in the top list.')
        self.v_seedlib = self._chk(g, 'Warm-start from library', self.cfg['seed_from_lib'], 3,
                                   tip='Seed the new generation with the best found alphas (fine-tuning). Off — always from scratch.')

        # --- simulation ---
        g = self._section(inner, 'SIMULATION')
        self.v_vol = self._numf(g, '★ Target-vol (ann.)', self.cfg['target_vol'], 0, 0.01, 3.0, 0.01,
                                tip='Target annual portfolio volatility — sets the scale of positions/leverage.')
        self.v_exec = self._numf(g, '★ Fee (turnover)', self.cfg['exec_cost'], 1, 0.0, 0.05, 0.0005,
                                 tip='Fee per trade, as a fraction of turnover. 0.001 = 10 basis points.')

        # --- genome ---
        g = self._section(inner, 'GENOME (formula complexity)')
        self.v_depth = self._num(g, 'Max. depth', self.cfg['max_depth'], 0, 2, 12, 1,
                                 tip='Maximum nesting of the formula tree.')
        self.v_size = self._num(g, 'Max. nodes', self.cfg['max_size'], 1, 3, 80, 1,
                                tip='Maximum operations in a formula — the main complexity limiter.')

        # --- selection ---
        g = self._section(inner, 'SELECTION (GA)')
        self.v_tourn = self._num(g, 'Tournament size', self.cfg['tournament'], 0, 2, 50, 1,
                                 tip='How many candidates to compare during selection. More — stricter selection.')
        self.v_elit = self._num(g, 'Elitism', self.cfg['elitism'], 1, 0, 50, 1,
                                tip='How many best pass to the next generation unchanged.')
        self.v_inject = self._num(g, 'Random/generation', self.cfg['random_inject'], 2, 0, 200, 1,
                                  tip='How many fresh random formulas to inject each generation (influx of novelty).')
        self.v_cx = self._numf(g, 'Crossover share', self.cfg['crossover_prob'], 3, 0.0, 1.0, 0.05,
                               tip='Share of crossover vs mutations (0..1).')

        # --- fitness ---
        g = self._section(inner, 'FITNESS')
        self.v_pars = self._numf(g, 'Complexity penalty', self.cfg['parsimony'], 0, 0.0, 1.0, 0.005,
                                 tip='Penalty for formula size — against over-complexity.')
        self.v_corrt = self._numf(g, 'Correlation threshold', self.cfg['corr_threshold'], 1, 0.0, 1.0, 0.05,
                                  tip='From what correlation to treat alphas as duplicates (for dedup).')
        self.v_corrp = self._numf(g, 'Similarity penalty', self.cfg['corr_penalty'], 2, 0.0, 2.0, 0.1,
                                  tip='Penalty for similarity to an already found alpha — for diversity.')
        self.v_hof = self._num(g, 'Hall of Fame size', self.cfg['hof_capacity'], 3, 1, 100, 1,
                               tip='How many champions to keep as output per round.')
        self.v_fitblocks = self._num(g, '★ Robust blocks (0 = legacy)', self.cfg.get('fit_blocks', 5),
                                     4, 0, 12, 1,
                                     tip='Robust fitness: the selection span is cut into K regime\n'
                                         'blocks; each block\'s Sharpe is shrunk by its standard\n'
                                         'error and the fitness is the near-worst block — a formula\n'
                                         'must work everywhere, not shine once. 0 = the legacy\n'
                                         'min(TRAIN, VAL) fitness.')

        # --- segments ---
        g = self._section(inner, 'DATE SEGMENTS  (TRAIN < VAL < TEST)')
        self.v_train = self._txt(g, '★ TRAIN start', self._seg_disp('train_start'), 0,
                                 tip="'auto' = this timeframe's window measured back from today\n"
                                     '(same shape on every bar size). Or a fixed date.\n'
                                     'Start of the training period (evolution runs on it).\n'
                                     'Also the start of the whole window, so this is the field\n'
                                     'that decides whether the span fits the bar size\'s limit —\n'
                                     'see the line under the timeframe selector.')
        self.v_val = self._txt(g, '★ VAL start', self._seg_disp('val_start'), 1,
                               tip='Start of validation — a robustness check.')
        self.v_test = self._txt(g, '★ TEST start', self._seg_disp('test_start'), 2,
                                tip='Start of the held-out test — an honest OOS, not part of selection.')
        self.v_end = self._txt(g, '★ TEST end', self._seg_disp('test_end'), 3,
                               tip='End of the entire data period. Strictly after TEST start —\n'
                                   'the four dates must read TRAIN < VAL < TEST < end.\n'
                                   "Write the word 'today' and the end follows the current\n"
                                   'date — the freshest bars always land inside TEST.')
        # The four boxes are free text, and until now nothing read them until the node was
        # already running: a reversed pair silently produced an empty slice (numbers computed
        # on no data), a typo'd date killed the node at load_config with no window to show it
        # in, and a span the bar size cannot carry just ran for hours. The note answers all
        # three while you are still typing; Start re-checks it as the hard gate.
        self.lbl_seg = self._lbl(g, text='', text_color=NEG, font=(self.UI, 11),
                                 wraplength=self.UNI_WRAP, justify='left', anchor='w')
        self.lbl_seg.grid(row=4, column=0, columnspan=2, sticky='w', pady=(5, 0))
        self.lbl_seg.grid_remove()                       # takes no space while the dates are fine
        for _v in (self.v_train, self.v_val, self.v_test, self.v_end):
            _v.trace_add('write', lambda *_a: self._seg_check())
        self._seg_check()

        # --- buttons (Start/Stop live in the header — always visible) ---
        btns = self._box(inner)
        btns.pack(fill='x', pady=(16, 0))
        b_reset = self._btn(btns, 'Reset to defaults', self._reset)
        b_reset.pack(fill='x', pady=(0, 6))
        b_wipe = self._btn(btns, 'Clear all history', self._wipe_history, kind='danger')
        b_wipe.pack(fill='x', pady=(14, 0))
        self._tip(b_reset, 'Return all settings to their default values.')
        self._tip(b_wipe, 'Delete all history and found alphas (with confirmation).')

    # ---------- widget factories (one place where the palette meets CustomTkinter) ----------
    # Tonal buttons (soft fills, no outlines) — the filled surface IS the affordance.
    _BTN = {                                             # kind -> (fill, hover, text)
        'plain':  lambda: (HEAD_BG, HEAD_HI, TXT),
        'accent': lambda: (ACC_FILL, ACC_FILL_HI, '#ffffff'),
        'soft':   lambda: (HEAD_BG, HEAD_HI, TXT),
        'danger': lambda: (_mix(NEG, CARD, 0.88), _mix(NEG, CARD, 0.80), NEG),
    }

    def _btn(self, parent, text, command, kind='plain', height=32, **kw):
        fill, hover, fg = self._BTN[kind]()
        return ctk.CTkButton(parent, text=text, command=command, height=height, corner_radius=10,
                             fg_color=fill, hover_color=hover, text_color=fg, border_width=0,
                             text_color_disabled=FAINT,
                             font=(self.UI, 11, 'bold' if kind == 'accent' else 'normal'), **kw)

    def _entry(self, parent, var, width=None, placeholder=None):
        """var=None: no textvariable is bound — the ONLY mode where CTkEntry's placeholder_text
        ever renders (a bound StringVar, even an empty one, disables the placeholder machinery
        entirely). Callers wanting the ghost hint pass var=None and read .get() themselves."""
        kw = {'width': width} if width else {}
        if placeholder:
            kw['placeholder_text'] = placeholder             # ghost hint while the field is empty
        if var is not None:
            kw['textvariable'] = var
        return ctk.CTkEntry(parent, height=30, corner_radius=9,
                            fg_color=HEAD_BG, border_color=BORDER, border_width=1,
                            text_color=TXT, font=(self.UI, 13), **kw)


    # ----- pairs editor: the universe as removable chips ---------------------------------
    # v_unilist (a CSV StringVar) stays the single source of truth — _collect, the
    # universe_all migration and _start_after_fetch's reconcile all read/write IT; the
    # chips are only a view. Every var write re-renders them (trace), every chip action
    # writes the var back. The old single Entry hid the tail of the list past its edge.

    UNI_WRAP = 330                                       # pre-scale px — the pane's label wraplength

    def _uni_build(self, parent):
        self.uni_chips = self._box(parent)
        self.uni_chips.pack(fill='x', pady=(3, 0))
        self.e_uni = self._entry(parent, None,
                                 placeholder='add pairs — paste a list, or type + Enter')
        self.e_uni.pack(fill='x', pady=(4, 4))
        self.e_uni.bind('<Return>', self._uni_commit)
        self.e_uni.bind('<FocusOut>', self._uni_commit)
        self.e_uni.bind('<KeyPress-BackSpace>', self._uni_backspace)
        self.e_uni.bind('<<Paste>>', self._uni_paste)          # Ctrl+V / Shift+Insert
        self.e_uni.bind('<ButtonPress-2>', self._uni_paste)    # middle-click PRIMARY
        self._tip(self.e_uni,
                  'The search, signals and metrics run on exactly this basket.\n'
                  f'Up to {MAX_PAIRS} pairs — commas, spaces or line breaks separate\n'
                  f'them — then ENTER turns the whole box into chips. A longer list\n'
                  f'keeps its first {MAX_PAIRS} and tells you what it dropped.\n'
                  'A paste lands the same way, all at once.\n'
                  '✕ removes a pair; clicking a chip pulls it back down for editing;\n'
                  'Backspace in the empty box pulls the last chip down.\n'
                  'Remove everything and the default five come back at Start/Save.')
        self.v_unilist.trace_add('write', lambda *_: self._uni_render())
        self._uni_render()

    def _uni_render(self):
        box = getattr(self, 'uni_chips', None)
        if not (box and box.winfo_exists()):
            return
        for w in box.winfo_children():
            w.destroy()
        syms = _parse_universe(self.v_unilist.get())
        if not syms:
            self._lbl(box, text='no pairs — Start or Save brings the default five back',
                      text_color=FAINT, font=(self.UI, 11)).pack(anchor='w')
            return
        ft = self._font(self.UI, 12, 'bold')
        fx = self._font(self.UI, 11, 'normal')
        wrap = int(self.UNI_WRAP * self.SCALE)
        gap = max(4, int(5 * self.SCALE))
        pad = int(7 * self.SCALE)
        xpad = int(4 * self.SCALE)
        tail = int(2 * self.SCALE)
        _locked = getattr(self, '_settings_locked', False)   # a frozen basket: no ✕, no hover
        row, used = None, 0
        for s in syms:
            # exact, not estimated: both labels are bd=0, so a chip is text + ✕ + paddings
            # + the frame's 1px highlight ring — an undercount here widens the whole pane
            # (the settings canvas takes its width from the content's reqwidth)
            w = (ft.measure(s) + 2 * pad) + (fx.measure('✕') + 2 * xpad + tail) + 2
            if row is None or (used and used + w > wrap):
                row = self._box(box)
                row.pack(anchor='w', pady=(0, gap))
                used = 0
            chip = tk.Frame(row, bg=HEAD_BG, highlightbackground=BORDER, highlightthickness=1)
            chip.pack(side='left', padx=(0, gap))
            t = tk.Label(chip, text=s, fg=TXT, bg=HEAD_BG, font=ft, bd=0, anchor='w',
                         padx=pad, pady=int(3 * self.SCALE),
                         cursor=('arrow' if _locked else 'hand2'))
            x = tk.Label(chip, text='✕', fg=(FAINT if _locked else MUT), bg=HEAD_BG, font=fx,
                         bd=0, padx=xpad, cursor=('arrow' if _locked else 'hand2'))
            if w > wrap:                             # one unbroken pasted blob: clamp the chip
                chip.pack_propagate(False)           # so it cannot widen the pane — ✕ is packed
                chip.configure(width=wrap - gap,     # first and stays visible to remove it
                               height=ft.metrics('linespace') + 2 * int(3 * self.SCALE) + 2)
                w = wrap - gap
            x.pack(side='right', padx=(0, tail))     # ✕ before the text: it survives clipping
            t.pack(side='left', fill='x')
            x.bind('<ButtonRelease-1>', lambda e, sym=s: self._uni_hit(e, self._uni_remove, sym))
            if not _locked:                          # no hover glow on a frozen chip
                x.bind('<Enter>', lambda _e, l=x: l.configure(fg=NEG))
                x.bind('<Leave>', lambda _e, l=x: l.configure(fg=MUT))
            t.bind('<ButtonRelease-1>', lambda e, sym=s: self._uni_hit(e, self._uni_edit, sym))
            used += w + gap
        # a silently shortened paste reads as the field eating input — say what happened.
        # Consumed on render: the warning belongs to the edit that caused it, not for ever.
        over, self._uni_over = getattr(self, '_uni_over', 0), 0
        if over:
            self._lbl(box, text=f'{over} pair{"s" if over > 1 else ""} did not fit — '
                                f'the basket is capped at {MAX_PAIRS}',
                      text_color=NEG, font=(self.UI, 11)).pack(anchor='w', pady=(2, 0))
        elif len(syms) >= MAX_PAIRS:
            self._lbl(box, text=f'{len(syms)}/{MAX_PAIRS} pairs — remove one to add another',
                      text_color=FAINT, font=(self.UI, 11)).pack(anchor='w', pady=(2, 0))

    def _uni_hit(self, e, fn, sym):
        """Chip actions run on ButtonRelease, and only when the pointer is still over the
        widget (drag off = cancel). The re-render slides the NEXT chip under the cursor, so
        the second press of a double-click would edit/remove an unintended pair — any chip
        action within 350ms of the previous one is that ghost, and is dropped."""
        if getattr(self, '_settings_locked', False):     # frozen while the node runs (the chips
            return                                       # are tk.Labels: state= can't lock them)
        try:
            if e.widget.winfo_containing(e.x_root, e.y_root) is not e.widget:
                return
        except Exception:                            # noqa: BLE001 — teardown mid-click
            return
        if e.time - getattr(self, '_uni_act_t', -10 ** 9) < 350:
            return
        self._uni_act_t = e.time
        fn(sym)

    @staticmethod
    def _uni_raw(s):
        """Whitespace of ANY kind separates tickers — a newline/tab paste from a
        spreadsheet must split into chips, not become one giant token."""
        return ''.join(',' if ch.isspace() else ch for ch in (s or ''))

    def _uni_commit(self, _e=None):
        """The WHOLE entry -> chips, splitting on commas and whitespace. THE commit: bound to
        Enter and FocusOut, called by _uni_paste and by _collect, so a pair still sitting in
        the box is never lost to Start/Save/theme switch.

        A typed comma deliberately does nothing. It used to commit everything before it, and
        the box emptying itself mid-word is what a person reads as the field losing their
        input — you watch the box you are typing in, not the chip row above it. Writing
        'XMRUSDT, XLMUSDT' and pressing Enter once is now exactly what it looks like."""
        if getattr(self, '_settings_locked', False):
            return None                              # session running: the basket is frozen
        try:
            raw = self._uni_raw(self.e_uni.get())
        except Exception:                            # noqa: BLE001 — teardown mid-FocusOut
            return None
        if raw.strip(','):
            full = _parse_universe(self.v_unilist.get() + ',' + raw, cap=None)
            self._uni_over = max(0, len(full) - MAX_PAIRS)     # _uni_render reports it
            self.v_unilist.set(','.join(full[:MAX_PAIRS]))
        try:
            self.e_uni.delete(0, 'end')
        except Exception:                            # noqa: BLE001
            pass
        return 'break'

    def _uni_backspace(self, e=None):
        """KeyPress fires BEFORE the class binding erases a character — an empty box here
        means it was ALREADY empty, so Backspace pulls the LAST CHIP down into the box for
        editing (never deletes it outright: X11 auto-repeat delivers a held key as a
        machine-gun of presses, and outright deletion emptied a whole basket in under a
        second). The 500ms guard stops auto-repeat from eating chip after chip."""
        if self.e_uni.get():
            return None                              # ordinary backspace inside text
        t = getattr(e, 'time', None)
        if t is not None:
            if t - getattr(self, '_uni_bs_t', -10 ** 9) < 500:
                return 'break'
            self._uni_bs_t = t
        syms = _parse_universe(self.v_unilist.get())
        if syms:
            self._uni_edit(syms[-1])
        return 'break'

    def _uni_paste(self, _e=None):
        """A paste is finished text, so it commits itself — no Enter needed. Bound for
        Ctrl+V/Shift+Insert and for middle-click PRIMARY, which reaches the inner tk.Entry
        through the Entry class binding and bypasses CTkEntry.insert() entirely — hence the
        placeholder has to be dismissed here or the pasted text glues itself onto the ghost
        string. Widget-tag bindings run BEFORE the class binding that does the actual insert,
        so the commit waits for the idle after it."""
        try:
            self.e_uni._deactivate_placeholder()
        except Exception:                            # noqa: BLE001
            pass
        self.root.after_idle(self._uni_commit)

    def _uni_remove(self, sym):
        self.v_unilist.set(','.join(
            s for s in _parse_universe(self.v_unilist.get()) if s != sym))

    def _uni_edit(self, sym):
        """Click a chip -> it drops back into the entry for editing."""
        self._uni_commit()                           # half-typed text becomes its own chip first
        self._uni_remove(sym)
        self.e_uni.delete(0, 'end')
        self.e_uni.insert(0, sym)
        self.e_uni.focus_set()
        self.e_uni.icursor('end')

    def _head(self, parent, text):
        """A panel heading — the small caps line above every card's content."""
        return self._lbl(parent, text=text, text_color=FAINT, font=(self.UI, 12, 'bold'))

    def _section(self, parent, title):
        row = self._box(parent)
        row.pack(anchor='w', fill='x', pady=(16, 6))
        tick = self._box(row, bg=ACC, width=max(2, int(3 * self.SCALE)),
                         height=int(13 * self.SCALE))
        tick.pack(side='left', padx=(0, 7))
        self._lbl(row, text=title, text_color=ACC, font=(self.UI, 12, 'bold')).pack(side='left')
        f = self._box(parent)
        f.pack(fill='x')
        f.columnconfigure(0, weight=1)
        return f

    def _row(self, parent, label, row, widget, tip):
        lbl = self._lbl(parent, text=label, text_color=MUT, font=(self.UI, 13))
        lbl.grid(row=row, column=0, sticky='w', pady=3)
        widget.grid(row=row, column=1, sticky='e', pady=3)
        if tip:
            self._tip(lbl, tip)
            self._tip(widget, tip)

    def _num(self, parent, label, val, row, lo, hi, step, tip=None):
        v = tk.IntVar(value=int(val))
        sp = ttk.Spinbox(parent, from_=lo, to=hi, increment=step, textvariable=v, width=9)
        self._row(parent, label, row, sp, tip)
        return v

    def _numf(self, parent, label, val, row, lo, hi, step, tip=None):
        v = tk.DoubleVar(value=float(val))
        sp = ttk.Spinbox(parent, from_=lo, to=hi, increment=step, textvariable=v, width=9, format='%.4f')
        self._row(parent, label, row, sp, tip)
        return v

    def _txt(self, parent, label, val, row, tip=None):
        v = tk.StringVar(value=str(val))
        e = self._entry(parent, v, width=104)
        self._row(parent, label, row, e, tip)
        v.widget = e                                     # so _seg_check can redden the bad box
        return v

    def _chk(self, parent, label, val, row, tip=None):
        v = tk.BooleanVar(value=bool(val))
        cb = ctk.CTkCheckBox(parent, text=label, variable=v, font=(self.UI, 13), text_color=TXT,
                             fg_color=ACC, hover_color=ACC_HI, border_color=FAINT,
                             checkbox_width=18, checkbox_height=18, corner_radius=5, border_width=2)
        cb.grid(row=row, column=0, columnspan=2, sticky='w', pady=(6, 3))
        if tip:
            self._tip(cb, tip)
        return v

    # ---------- dialogs ----------
    def _dialog(self, title, geometry):
        win = ctk.CTkToplevel(self.root)
        win.title(title)
        win.configure(fg_color=CARD)
        win.geometry(geometry)
        win.after(200, lambda: win.lift())               # CTkToplevel can open behind the main window
        return win

    def _console(self, win):
        """The log view of a child process — a terminal, so it stays dark in both themes."""
        txt = ctk.CTkTextbox(win, fg_color='#0f1115', text_color='#d7dce3', font=(self.MONO, 15),
                             wrap='word', border_width=0, corner_radius=8,
                             scrollbar_button_color='#333a46')
        txt.pack(fill='both', expand=True, padx=12, pady=12)
        self._text_selectable(txt)
        return txt

    # ---------- tooltips (short hints on hover) ----------
    def _tip(self, widget, text):
        widget.bind('<Enter>', lambda e, t=text: self._tip_schedule(e, t), add='+')
        widget.bind('<Leave>', lambda e: self._tip_hide(), add='+')
        widget.bind('<ButtonPress>', lambda e: self._tip_hide(), add='+')

    def _tip_schedule(self, e, text):
        self._tip_hide()
        self._tip_xy = (e.x_root + 16, e.y_root + 18)
        self._tip_after = self.root.after(400, lambda: self._tip_show(text))

    def _tip_show(self, text):
        self._tip_after = None
        if self._tip_win or not text:
            return
        win = tk.Toplevel(self.root)                     # plain Toplevel: CTkToplevel can't go borderless
        win.wm_overrideredirect(True)
        try:
            win.attributes('-topmost', True)
        except tk.TclError:
            pass
        tk.Label(win, text=text, bg=TIP_BG, fg=TIP_FG, justify='left', font=(self.UI, 12),
                 padx=9, pady=6, wraplength=250, highlightbackground=TIP_BD,
                 highlightthickness=1).pack()
        x, y = self._tip_xy
        win.wm_geometry(f'+{x}+{y}')
        self._tip_win = win

    def _tip_hide(self):
        if self._tip_after:
            self.root.after_cancel(self._tip_after)
            self._tip_after = None
        if self._tip_win:
            self._tip_win.destroy()
            self._tip_win = None

    def _bind_wheel(self, canvas, through=False):
        """Wheel-scroll `canvas` while the pointer is anywhere inside it. With through=True,
        events over widgets that scroll themselves (Treeview / Text) are left to them."""
        def _w(e):
            if through:
                w = e.widget
                while w is not None and w is not canvas:
                    if isinstance(w, (ttk.Treeview, tk.Text)):
                        return
                    w = getattr(w, 'master', None)
            d = -1 if (getattr(e, 'num', None) == 4 or getattr(e, 'delta', 0) > 0) else 1
            canvas.yview_scroll(d, 'units')

        def _on(_e):
            canvas.bind_all('<Button-4>', _w)
            canvas.bind_all('<Button-5>', _w)
            canvas.bind_all('<MouseWheel>', _w)

        def _off(_e):
            for seq in ('<Button-4>', '<Button-5>', '<MouseWheel>'):
                canvas.unbind_all(seq)
        canvas.bind('<Enter>', _on)
        canvas.bind('<Leave>', _off)

    # ---------- right panel: status / leaderboard ----------
    def _hgrip(self, parent, on_drag, on_reset, tip):
        """An IN-CARD resize strip (packed at the card's bottom): a thin accent bar that
        lives inside the card, so it travels with it when cards are drag-reordered."""
        strip = tk.Frame(parent, bg=CARD, height=max(16, int(16 * self.SCALE)),
                         cursor='sb_v_double_arrow')
        strip.pack(fill='x', pady=(6, 0))
        strip.pack_propagate(False)
        bar = tk.Frame(strip, bg=BORDER, height=max(4, int(4 * self.SCALE)),
                       width=int(64 * self.SCALE), cursor='sb_v_double_arrow')
        bar.place(relx=0.5, rely=0.5, anchor='center')
        for w in (strip, bar):
            w.bind('<ButtonPress-1>', self._grip_freeze)
            w.bind('<B1-Motion>', on_drag)
            w.bind('<ButtonRelease-1>', self._grip_release)
            w.bind('<Double-1>', on_reset)
            w.bind('<Enter>', lambda e: bar.configure(bg=ACC))
            w.bind('<Leave>', lambda e: bar.configure(bg=BORDER))
        self._tip(strip, tip)

    def _grip_freeze(self, _e=None):
        """While ANY grip is dragged, the leaderboard must stop soaking window slack: with
        its grid row weighted, every drag pixel of a card ABOVE it re-allocates and redraws
        the whole Treeview — which read as jerky resizing of a completely unrelated card.
        For the drag's duration the column simply grows into the scroll; release re-grids
        once and hands the slack back."""
        self._grip_live = True
        self._regrid_cards()

    def _grip_release(self, _e=None):
        self._grip_live = False
        self._regrid_cards()
        self._save()

    def _wrap_drag(self, wrap, key, e, lo, hi):
        """Shared SMOOTH resize for a table card: the table lives in a wrap frame whose pixel
        height follows the pointer (pack_propagate off), exactly like the live-log card. The
        old version set the Treeview's row COUNT instead — the card resized in 32px jumps and
        read as broken. cfg[key] stores dp (unscaled px); 0 means natural height."""
        h = max(lo, min(hi, e.y_root - wrap.winfo_rooty()))
        wrap.pack_propagate(False)
        wrap.configure(height=h)
        self.cfg[key] = round(h / self.SCALE)

    def _wrap_apply_saved(self, wrap, key):
        """On build: restore a persisted pixel height (or leave the natural one for 0)."""
        h = int(self.cfg.get(key) or 0)
        if h > 0:
            wrap.pack_propagate(False)
            wrap.configure(height=int(h * self.SCALE))

    def _on_lb_rows_drag(self, e):
        entering = not int(self.cfg.get('lb_h') or 0)
        self._wrap_drag(self._lbwrap, 'lb_h', e, int(140 * self.SCALE), int(1400 * self.SCALE))
        if entering:                                     # first drag pixel: take the card's grid
            self._regrid_cards()                         # row out of the window-slack game

    def _lb_rows_reset(self, _e=None):
        self.cfg['lb_h'] = 0
        self._lbwrap.pack_propagate(True)                # back to the table's natural height
        self.tree.configure(height=int(self.cfg.get('lb_rows') or 12))
        self._regrid_cards()                             # the row soaks window slack again
        self._save()

    def _on_fwd_rows_drag(self, e):
        self._wrap_drag(self._fwdwrap, 'fwd_h', e, int(90 * self.SCALE), int(1000 * self.SCALE))

    def _fwd_rows_reset(self, _e=None):
        self.cfg['fwd_h'] = 0
        self._fwdwrap.pack_propagate(True)
        self.fwd_tree.configure(height=int(self.cfg.get('fwd_rows') or 4))
        self._save()

    def _build_status(self, body):
        # The dashboard column SCROLLS: on a short window the cards keep their natural heights
        # and the scrollbar moves between blocks; on a tall one the inner frame is stretched to
        # the viewport and the leaderboard absorbs the slack exactly as before (grid weight).
        outer = self._box(body, bg=BG)
        body.add(outer, minsize=int(420 * self.SCALE), stretch='always')
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        vsb = ctk.CTkScrollbar(outer, orientation='vertical', command=canvas.yview,
                               fg_color=BG, button_color=BORDER, button_hover_color=FAINT,
                               width=12)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y', padx=(6, 0))
        canvas.pack(side='left', fill='both', expand=True)
        right = self._box(canvas, bg=BG)
        item = canvas.create_window((0, 0), window=right, anchor='nw')

        def _fit(_e=None):
            w, h = canvas.winfo_width(), canvas.winfo_height()
            H = max(h, right.winfo_reqheight())
            if (w, H) == getattr(canvas, '_fit_last', None):
                return
            canvas._fit_last = (w, H)
            canvas.itemconfigure(item, width=w, height=H)
            canvas.configure(scrollregion=(0, 0, w, H))
        canvas.bind('<Configure>', _fit)
        right.bind('<Configure>', _fit)      # cards appear/grow -> refresh the scrollregion

        def _fit_watch():
            # Configure alone is not enough: the inner frame is PINNED to the computed height, so
            # when a card's REQUESTED height changes later (portfolio chart renders, CTk's late
            # relayout in the frozen build) its actual size — and thus Configure — never fires.
            # The scrollregion then stays viewport-sized: the scrollbar is dead and the grid
            # crushes the leaderboard row to 0. Poll the request cheaply instead.
            if not canvas.winfo_exists():
                return
            _fit()
            canvas.after(400, _fit_watch)
        canvas.after(400, _fit_watch)
        self._bind_wheel(canvas, through=True)
        self._dash_canvas = canvas
        self._dash_right = right                         # row weights are set by _regrid_cards
        right.columnconfigure(0, weight=1)

        card = self._card(right)
        card.grid(row=0, column=0, sticky='ew')
        pad = self._pad(card)
        head = self._box(pad)
        head.pack(fill='x')
        self.pill_state = ctk.CTkFrame(head, fg_color=HEAD_BG, corner_radius=999, border_width=0)
        self.pill_state.pack(side='left')
        self.lbl_state = self._lbl(self.pill_state, text='● stopped', font=(self.UI, 14, 'bold'),
                                   text_color=MUT, bg=HEAD_BG)
        self.lbl_state.pack(padx=int(13 * self.SCALE), pady=int(5 * self.SCALE))
        self.lbl_res = self._lbl(head, text='', text_color=MUT, font=(self.UI, 13))
        self.lbl_res.pack(side='right', pady=(4, 0))

        stats = self._box(pad)
        stats.pack(fill='x', pady=(14, 0))
        self.s_rounds = self._stat(stats, 'rounds', 0)
        self.s_trials = self._stat(stats, 'formulas tried', 1)
        self.s_found = self._stat(stats, 'alphas found', 2)
        self.s_fit = self._stat(stats, 'best fitness', 3,   # the old PROGRESS chart, as one number
                                accent=True)             # — and the row's anchor, not a fourth twin
        self.s_fit.configure(text='—')
        for i in range(4):
            stats.columnconfigure(i, weight=1)           # share the card width: half of the
        #                                                  flagship card was dead space
        self._tip(self.s_fit, 'Best fitness so far — min(TRAIN, VAL) Sharpe of the top alpha.\n'
                              'Grows round by round as the search improves; TEST stays held-out\n'
                              '(see the TEST OOS column in the leaderboard).')
        # (the fitness sparkline + round-ticker strip lived here; removed on user request —
        #  the simple screen keeps the curve, the tiles and the log carry the numbers)
        # ROUND PROGRESS — one bar per round: fills from the node's ETA model (round_eta.py),
        # resets when the next round starts. Hidden while nothing is running.
        self.round_card = self._box(pad)
        rrow = self._box(self.round_card)
        rrow.pack(fill='x')
        self.lbl_round_l = self._lbl(rrow, text='', text_color=TXT, font=(self.UI, 12, 'bold'))
        self.lbl_round_l.pack(side='left')
        self.lbl_round_r = self._lbl(rrow, text='', text_color=MUT, font=(self.MONO, 12), anchor='e')
        self.lbl_round_r.pack(side='right')
        self.round_bar = ctk.CTkProgressBar(self.round_card, height=int(7 * self.SCALE),
                                            corner_radius=999, progress_color=ACC,
                                            fg_color=HEAD_BG, border_width=0)
        self.round_bar.set(0.0)
        self.round_bar.pack(fill='x', pady=(5, 0))
        self._round_shown = False
        self._tip(self.round_bar, 'Progress of the current round. The remaining time comes from the\n'
                                  'measured cost of one formula evaluation × the formulas still to\n'
                                  'score (dedup and formula growth are modelled) — not from pop × gens.\n'
                                  'Accurate to a few % from mid-round; ±15% in the first generations.')
        # LIVE LOG — the node's human-readable activity feed (status.json 'events')
        logwrap = ctk.CTkFrame(pad, fg_color=STRIPE, corner_radius=10, border_width=0)
        logwrap.pack(fill='x', pady=(10, 0))
        self.logbox = tk.Text(logwrap, height=7, bg=STRIPE, fg=MUT, bd=0, highlightthickness=0,
                              font=(self.MONO, 11), wrap='word', state='disabled',
                              cursor='arrow', padx=6, pady=4)
        gut = self._font(self.MONO, 11).measure('13:42:44  ')   # wrapped lines align under the
        for tag, col in (('round', TXT), ('roundsum', TXT), ('llm', ACC), ('best', POS),
                         ('polish', ACC_HI), ('warn', NEG), ('err', NEG), ('ts', FAINT),
                         ('i', MUT)):
            self.logbox.tag_configure(tag, foreground=col,       # message, not under the timestamp
                                      **({} if tag == 'ts' else {'lmargin2': gut}))
        self.logbox.tag_configure('roundsum', font=self._font(self.MONO, 11, 'bold'))
        self.logbox.pack(fill='both', expand=True, padx=8, pady=6)
        self._logwrap = logwrap
        # the status card is deliberately STATIC: a fixed 7-line log window, no grip. It is
        # the dashboard's masthead — a constant-size instrument panel reads calmer than one
        # more stretchable card, and the log scrolls inside itself anyway.
        self._events_last = None
        self._text_selectable(self.logbox)               # the log is text — let the mouse treat it as text
        self._log_placeholder()

        self._build_signals_card(right)                  # row 1 — hidden while nothing is served

        card2 = self._card(right)
        card2.grid(row=4, column=0, sticky='nsew')
        p2 = self._pad(card2)
        hrow = self._box(p2)
        hrow.pack(fill='x', pady=(0, 8))
        self._lb_head_text = self._lb_head_text_for('fit')
        self.lbl_lb_head = self._head(hrow, self._lb_head_text)
        # Packed BEFORE the heading: the heading is a long line, and whoever packs first wins the
        # space — pack it first and the button gets squeezed to nothing on a narrow window.
        # width/height are raw: CTk scales them itself (set_widget_scaling), unlike a tk pixel.
        self.btn_lb_csv = self._btn(hrow, 'CSV', self._export_library, height=24, width=52)
        self.btn_lb_csv.pack(side='right', padx=(10, 0))
        # node activation: ONE subscription key unlocks the whole local library at once and lets
        # the node mine in the open — the customer-facing CTA.
        # (No emoji in the label: this Tk build segfaults drawing non-BMP glyphs in a CTkButton.)
        self.btn_activate = self._btn(hrow, 'Activate node', self._open_activate,
                                      height=24, width=118)
        self.btn_activate.pack(side='right', padx=(10, 0))
        self._tip(self.btn_activate,
                  'Paste your subscription key once: this machine claims one of the plan\'s\n'
                  'node seats, EVERY sealed formula in the local library is unlocked in one\n'
                  'go, and mining continues in the open while the subscription is live.')
        self.btn_favs = self._btn(hrow, '★ Favorites', self._open_favorites, height=24, width=96)
        self.btn_favs.pack(side='right', padx=(10, 0))
        self._tip(self.btn_favs,
                  'Your starred formulas — stored OUTSIDE the library, so they survive\n'
                  "'Clear all history' and session loads. Star a row by clicking its ★ cell;\n"
                  'open a favorite from the list exactly like a leaderboard row.')
        # All ↔ Families toggle. ON collapses the table to the best alpha per family (the old view);
        # OFF (default) shows every alpha the node has mined. A CTkSwitch, not a segmented button:
        # 6.0.0's segmented button has no selected_text_color, so its active label loses contrast.
        self.v_lbfam = tk.BooleanVar(value=(self._lb_mode == 'families'))
        self.sw_lbfam = ctk.CTkSwitch(hrow, text='families only', variable=self.v_lbfam,
                                      command=self._toggle_lb_mode, onvalue=True, offvalue=False,
                                      font=(self.UI, 11), text_color=MUT, progress_color=ACC,
                                      button_color=TXT, fg_color=HEAD_BG, switch_width=34,
                                      switch_height=16)
        self.sw_lbfam.pack(side='right', padx=(0, 4))
        self.v_lb_q = tk.StringVar()
        self.e_lb_q = ctk.CTkEntry(hrow, textvariable=self.v_lb_q, width=170, height=24,
                                   font=(self.UI, 12), placeholder_text='find: id or formula',
                                   fg_color=HEAD_BG, border_color=BORDER, text_color=TXT)
        self.e_lb_q.pack(side='right', padx=(0, 12))
        self.v_lb_q.trace_add('write', lambda *_a: self._on_lb_query())
        self.e_lb_q.bind('<Escape>', lambda _e: self.v_lb_q.set(''))
        self._tip(self.e_lb_q, 'Filter the table live: substring of the ID (the 6-char md5)\n'
                               'or of the formula text, case-insensitive. Esc clears.')
        self.lbl_lb_head.pack(side='left', anchor='w')
        # interaction hints pack AFTER everything and hide as a whole when the row gets tight —
        # the heading used to clip mid-shortcut ('right-click / Ctrl+') and read as the switch's
        # label. The full hints stay in the heading tooltip either way.
        hints = self._lbl(hrow, text='·  click column: sort  ·  double-click: equity  ·  '
                                     'click a cell: select text  ·  Ctrl+C: copy',
                          text_color=FAINT, font=(self.UI, 12), bg=CARD)
        hints.pack(side='left', anchor='w', padx=(10, 0))
        self._hide_when_tight(hrow, hints)
        self._tip(self.btn_lb_csv, 'Download EVERY alpha the node has mined — the whole library,\n'
                                   'no dedup, no TEST filter, with all TRAIN/VAL/TEST numbers.')
        self._tip(self.sw_lbfam, 'OFF: show every alpha in the library (scroll the full list).\n'
                                 'ON: collapse to the best alpha per family (distinct formulas),\n'
                                 'the old compact view. Sorting by any column works in both.')
        wrap = self._box(p2)
        wrap.pack(fill='both', expand=True)
        self._lbwrap = wrap                              # smooth pixel resize (its in-card grip)
        cols = ('fav', 'rank', 'fit', 'test', 'dd', 'cagr', 'srt',
                'tup', 'tdown', 'tflat', 'ls', 'bal', 'act', 'wup', 'wdown', 'id', 'formula')
        self.tree = ttk.Treeview(wrap, columns=cols, show='headings',
                                 height=int(self.cfg.get('lb_rows') or 12))
        self._HEAD = {}
        # Widths fit the WIDEST real value plus the sort arrow the heading grows by (' ▼'), and are
        # scaled with the display: a Treeview column is raw pixels while its text follows the DPI,
        # which is what cut "3069/2100" down to "3069/:" and clipped the "tr/yr·a" heading itself.
        for c, txt, w, anc in (('fav', '★', 34, 'center'),
                               ('rank', '#', 40, 'center'), ('fit', 'fitness', 86, 'e'),
                               ('test', 'TEST OOS', 86, 'e'), ('dd', 'maxDD', 74, 'e'),
                               ('cagr', 'CAGR', 72, 'e'), ('srt', 'sortino', 80, 'e'),
                               ('tup', 'T ↑', 64, 'e'), ('tdown', 'T ↓', 64, 'e'),
                               ('tflat', 'T ~', 64, 'e'),
                               ('ls', 'L/S /yr·a', 100, 'center'),
                               ('bal', 'L/S %', 84, 'center'),
                               ('act', 'tr/yr·a', 72, 'e'),
                               ('wup', 'win ↑', 64, 'e'), ('wdown', 'win ↓', 64, 'e'),
                               ('id', 'ID', 72, 'center'),
                               ('formula', 'formula', 260, 'w')):
            self._HEAD[c] = txt
            kw = {} if c in ('fav', 'rank', 'id') else {'command': (lambda c=c: self._sort_by(c))}
            w = int(w * self.SCALE)
            self.tree.heading(c, text=txt, anchor=anc, **kw)   # headings share the values' edge
            self.tree.column(c, width=w, anchor=anc, stretch=(c == 'formula'), minwidth=w)
        # A focused default set — the heavy analysis columns are one right-click away
        # ('Columns…' on any header). Data, sorting and CSV export always carry every column.
        disp = self._adv_cols()
        self.tree.configure(displaycolumns=disp)
        self._lb_cols_fixed = [c for c in disp if c != 'formula']
        self._update_headings()                          # show the sort arrow on the active column
        if hasattr(self, '_trend_tip_base'):
            del self._trend_tip_base                     # theme rebuild rewrote the tips — rebase
        self._apply_trend_bars()                         # restamp known bucket sizes after a rebuild
        # one-line definitions per column header (the card-title tooltip nobody hovered is gone)
        self._HEAD_TIP = {
            'fav': 'favorites — click a row\'s ★ cell to star/unstar the formula;\n'
                   'the ★ Favorites button opens the starred list',
            'rank': 'position in the current sort',
            'fit': 'fitness = min(TRAIN, VAL) Sharpe — the number the search optimizes.\n'
                   'With \'Optimize by win rate\' on: min(TRAIN, VAL) of the evidence-\n'
                   'shrunk win rate (damped by activity share, minus a binomial SE) —\n'
                   'such rows show a percentage, slightly below the raw win% column.',
            'test': 'held-out TEST Sharpe (out-of-sample) — never optimized;\n'
                    'picking rows by it is peeking',
            'dd': 'worst peak-to-trough drawdown on TEST',
            'cagr': 'annualized growth on TEST',
            'srt': 'Sortino on TEST — like Sharpe, but only downside vol counts',
            'tup': 'TEST Sharpe on TRENDING-UP market bars only.\n'
                   'Direction regime: t-stat of the EW basket\'s drift over the trailing\n'
                   '~30 calendar days — |t| ≥ 1.28 labels the bar with the drift\'s sign.\n'
                   'A bar is judged by the regime known at its OPEN (labels lag one bar),\n'
                   'so a formula can\'t get credit for a move its own bar created.\n'
                   'Causal, TEST-only. \'—\' = under 30 such bars on TEST.',
            'tdown': 'TEST Sharpe on TRENDING-DOWN market bars only.\n'
                     'Same direction regime as T↑ — see that column\'s tooltip.',
            'tflat': 'TEST Sharpe on FLAT market bars — no statistically confident drift\n'
                     '(|t| below 1.28). Same direction regime as T↑ — see its tooltip.',
            'ls': 'positions opened per asset per year on TEST: long / short\n'
                  '(an annualized rate — comparable across universes and periods)',
            'bal': 'how the book SITS on TEST: share of exposure long / short,\n'
                   'time-averaged and inverse-vol weighted (the weights the simulator\n'
                   'actually trades). 50/50 = market neutral. 100/0 is not an alpha —\n'
                   'a long-only basket inherits the market\'s Sharpe, and in a bull leg\n'
                   'it scores well on TRAIN and VAL for reasons that have nothing to do\n'
                   'with the formula. The search docks fitness past 75/25 (max_net).',
            'act': 'trades per asset per year — relative activity',
            'wup': 'accuracy of the formula\'s UP calls: every (bar, asset) where it\n'
                   'held a LONG position at the prior close, judged by that asset\'s\n'
                   'next bar — right when the price rose. A bar where the price didn\'t\n'
                   'move judges nothing. \'—\' = under 30 such calls on TEST, or fewer\n'
                   'than 5 where the price actually moved.',
            'wdown': 'accuracy of the formula\'s DOWN calls — short positions, right\n'
                     'when the price fell. Same evidence floors as win ↑.',
            'id': 'stable ID — the md5 tail of the formula; the forward track uses the SAME id',
            'formula': 'the alpha itself — right-click: copy / choose columns',
        }
        self.tree.bind('<Motion>', self._on_tree_motion, add='+')
        self.tree.bind('<Leave>', lambda e: self._head_tip_hide(), add='+')
        self._tip(self.lbl_lb_head, 'maxDD = worst peak-to-trough drawdown; CAGR = annualized growth;\n'
                                    'sortino = like Sharpe but only downside vol counts (upside is free);\n'
                                    'T↑ / T↓ / T~ = the alpha\'s Sharpe on trending-up / trending-down /\n'
                                    'flat halves of TEST (market direction regime; causal, lagged one bar).\n'
                                    'Analysis, not selection: picking alphas by TEST numbers is another\n'
                                    'layer of TEST peeking;\n'
                                    'L/S /yr·a = positions opened per asset per year on TEST, long / short\n'
                                    '(a trade = crossing into long/short from flat or the opposite side);\n'
                                    'tr/yr·a = trades per asset per year (relative activity — the "min tr/yr"\n'
                                    'filter drops barely-trading alphas); win% = share of days with profit.\n'
                                    'All on TEST (OOS), on target weights (daily rebalance).')
        # colour economy: green tint marks ONLY rows whose held-out TEST survived (>= 0) — the
        # minority. Ink stays neutral everywhere: whole-row red carried one bit per row, painted
        # positive fitness in 'loss' red, and made the rare green rows the strongest focal point.
        self.tree.tag_configure('pos', background=_mix(CARD, POS, 0.12))
        self.tree.tag_configure('odd', background=STRIPE)
        self.tree.tag_configure('even', background=CARD)
        vsb = ctk.CTkScrollbar(wrap, orientation='vertical', command=self.tree.yview, fg_color=CARD,
                               button_color=BORDER, button_hover_color=FAINT, width=14)
        self._vsb = vsb
        # formulas render FULL length: the column is sized to the widest row and the horizontal
        # bar scrolls to the tail (_fit_formula_col); measuring needs the tree's own font
        self._tree_font = tkfont.Font(family=self.MONO, size=self._px(12))
        self._lb_need_px = 0
        hsb = ctk.CTkScrollbar(wrap, orientation='horizontal', command=self.tree.xview,
                               fg_color=CARD, button_color=BORDER, button_hover_color=FAINT,
                               height=14)
        self.tree.configure(yscrollcommand=self._on_tree_scroll,   # scroll -> load metrics for the viewport
                            xscrollcommand=hsb.set)
        vsb.pack(side='right', fill='y', padx=(4, 0))
        hsb.pack(side='bottom', fill='x', pady=(4, 0))
        self.tree.pack(side='left', fill='both', expand=True)
        self._wrap_apply_saved(wrap, 'lb_h')
        self._hgrip(p2, self._on_lb_rows_drag, self._lb_rows_reset,
                    'Drag: taller/shorter leaderboard. Double-click — natural height.')
        self.tree.bind('<Configure>', lambda e: self._fit_formula_col())
        self.tree.bind('<Double-1>', self._on_row_open)
        self.tree.bind('<Button-3>', self._on_row_menu)             # right-click — context menu
        self.tree.bind('<Control-c>', lambda e: self._copy_formula())
        self.tree.bind('<Control-C>', lambda e: self._copy_formula())
        self.tree.bind('<Button-1>', self._on_lb_star, add='+')    # BEFORE the overlay: 'break' wins
        self._selectable_cells(self.tree)                # click a cell — its text turns selectable
        self._menu = tk.Menu(self.root, tearoff=0, bg=CARD, fg=TXT, activebackground=ACC_SOFT,
                             activeforeground=TXT, borderwidth=0, font=(self.UI, 13))
        self._menu.add_command(label='Copy formula', command=self._copy_formula)
        self._menu.add_command(label='Copy formula + metrics', command=self._copy_full)
        self._menu.add_command(label='★ Star / unstar', command=self._fav_toggle_selected)
        self._menu.add_separator()
        self._menu.add_command(label='Export table (CSV)…', command=self._export_visible)
        self._menu.add_command(label='Export full library (CSV)…', command=self._export_library)
        self._menu.add_separator()
        self._menu.add_command(label='Show equity', command=self._open_selected_plot)
        # right-click on a column HEADER: pick which analysis columns are visible
        self._cols_menu = tk.Menu(self.root, tearoff=0, bg=CARD, fg=TXT,
                                  activebackground=ACC_SOFT, activeforeground=TXT,
                                  borderwidth=0, font=(self.UI, 13))
        self._cols_vars = {}
        shown = set(self._lb_cols_fixed)
        for c in self._LB_OPT_ORDER:
            v = tk.BooleanVar(value=(c in shown))
            self._cols_vars[c] = v                       # keep refs: Tk vars die with their menu
            self._cols_menu.add_checkbutton(label=self._HEAD[c], variable=v,
                                            command=lambda c=c: self._lb_toggle_col(c))

        # ---- PORTFOLIO panel (combine top-N via the real engine; TEST- or fitness-ranked) ----
        card3 = self._card(right)
        card3.grid(row=6, column=0, sticky='ew')
        self.pf_card = card3
        p3 = self._pad(card3)
        hp = self._box(p3)
        hp.pack(fill='x')
        self._head(hp, 'PORTFOLIO — top-N combined via the real engine').pack(side='left')
        ctl = self._box(hp)
        ctl.pack(side='right')
        self._lbl(ctl, text='top', text_color=MUT, font=(self.UI, 13)).pack(side='left', padx=(0, 5))
        self.v_pfn = tk.IntVar(value=6)
        ttk.Spinbox(ctl, from_=2, to=20, width=4, textvariable=self.v_pfn).pack(side='left', padx=(0, 8))
        self._lbl(ctl, text='by', text_color=MUT, font=(self.UI, 13)).pack(side='left', padx=(0, 5))
        self.v_pfsel = tk.StringVar(value='TEST')
        sel_box = ttk.Combobox(ctl, textvariable=self.v_pfsel, values=('TEST', 'fitness', 'combo'),
                               state='readonly', width=7)
        sel_box.pack(side='left', padx=(0, 8))
        self._tip(sel_box, 'How the top-N members are picked from the library:\n'
                           '• TEST — by held-out TEST Sharpe: what actually worked on 2023+.\n'
                           '  ⚠ the shown combined TEST becomes optimistic (the same window\n'
                           '  picked the members) — validate on the forward track first.\n'
                           '• fitness — by min(train,val): TEST never enters selection, so\n'
                           '  the combined TEST numbers are honest out-of-sample.\n'
                           '• combo — the best COMBINATION of N, not the N best: a pool of\n'
                           '  top-fitness alphas is simulated once and a greedy+swap search\n'
                           '  maximizes the mix\'s Sharpe on TRAIN+VAL only. TEST never\n'
                           '  enters the search, so the combined TEST stays honest — and the\n'
                           '  objective itself hunts diversification (uncorrelated members).\n'
                           '  Slower: the whole pool is simulated, not just N.')
        self.btn_pf = self._btn(ctl, '▶ Build portfolio', self._build_portfolio, kind='accent')
        self.btn_pf.pack(side='left')
        self._tip(self.btn_pf, 'Runs the top-N library alphas (ranked per the "by" selector)\n'
                               'through the project Portfolio engine (real simulation, ~1–2 min\n'
                               'in the background) and shows the combined dollar-neutral equity\n'
                               'on TEST.')
        self.btn_pf_csv = self._btn(ctl, 'CSV', self._pf_download_signals, width=76)
        self.btn_pf_csv.configure(state='disabled')
        self.btn_pf_csv.pack(side='left', padx=(8, 0))
        self.btn_pf_sig = self._btn(ctl, 'Serve', self._pf_serve_signal, width=86)
        self.btn_pf_sig.configure(state='disabled')
        self.btn_pf_sig.pack(side='left', padx=(6, 0))
        self.btn_pf_pdf = self._btn(ctl, 'PDF', self._pf_pdf_report, width=64)
        self.btn_pf_pdf.configure(state='disabled')
        self.btn_pf_pdf.pack(side='left', padx=(6, 0))
        self.btn_pf_track = self._btn(ctl, 'Track', self._pf_fwd_enroll, width=76)
        self.btn_pf_track.configure(state='disabled')
        self.btn_pf_track.pack(side='left', padx=(6, 0))
        self._tip(self.btn_pf_track, 'Enroll this exact portfolio into the FORWARD TRACK below:\n'
                                     'the strategy is frozen (formulas + universe + vol/fee) and\n'
                                     'paper-stepped once per closed daily bar, append-only.')
        self._tip(self.btn_pf_pdf, 'Portfolio analytics dashboard as PDF: KPIs, equity,\n'
                                   'exposure and turnover, weight structure, monthly\n'
                                   'returns and conclusions (TEST period).')
        self._tip(self.btn_pf_csv, 'Download a CSV of the combined portfolio signals — the target\n'
                                   'weight per asset per bar over TRAIN/VAL/TEST (each row labeled\n'
                                   'with its segment), with the asset\'s OHLCV on that bar —\n'
                                   'same as for a single alpha. Intraday timeframes: TEST only.')
        self._tip(self.btn_pf_sig,'Start a local signal API for the whole portfolio — serves the\n'
                                   'combined live target positions as JSON on localhost. Each\n'
                                   'service takes the next free port from 8799 and appears in the\n'
                                   'SIGNAL API card above (URL, log, "free the port").')
        self.lbl_pf = self._lbl(p3, text_color=FAINT, font=(self.UI, 12), wraplength=900,
                                   anchor='w', justify='left',
                                   text='⚠ selecting by TEST inflates the number (cherry-pick); the '
                                        'diversification gain — combined ≫ any single alpha — is the real part.')
        self.lbl_pf.pack(anchor='w', fill='x', pady=(8, 2))
        self.lbl_pf_m = self._lbl(p3, text='', text_color=TXT, font=(self.UI, 16, 'bold'),
                                     anchor='w')
        self.lbl_pf_m.pack(anchor='w')
        # WHICH alphas got combined. The card's whole claim is that the mix beats every one of
        # its members, and that was unfalsifiable while the members were not on screen: the
        # summary named a count, the chart drew one line, and the formulas lived only inside
        # portfolio.json. SOLO is each member's TEST Sharpe on its own — read the combined
        # number above against this column and the diversification gain is either there or not.
        self._pfwrap = self._box(p3)
        self._pfwrap.pack(fill='x', pady=(10, 0))
        self.pf_tree = ttk.Treeview(self._pfwrap, columns=self._PF_COLS, show='headings',
                                    height=1, selectmode='browse')
        for c, txt, w, anch in (('n', '#', 40, 'center'), ('id', 'ID', 72, 'center'),
                                ('solo', 'SOLO TEST', 92, 'e'), ('fit', 'FITNESS', 86, 'e'),
                                ('test', 'TEST OOS', 86, 'e'), ('formula', 'FORMULA', 260, 'w')):
            self.pf_tree.heading(c, text=txt, anchor=anch)
            self.pf_tree.column(c, width=int(w * self.SCALE), anchor=anch,
                                stretch=(c == 'formula'), minwidth=int(w * self.SCALE))
        # tags are per-widget in ttk: the leaderboard's are configured on ITS tree only.
        # Same colour economy — the green tint marks the rows the combination actually beat.
        self.pf_tree.tag_configure('pos', background=_mix(CARD, POS, 0.12))
        self.pf_tree.tag_configure('odd', background=STRIPE)
        self.pf_tree.tag_configure('even', background=CARD)
        self._pf_vsb = ctk.CTkScrollbar(self._pfwrap, orientation='vertical',
                                        command=self.pf_tree.yview, fg_color=CARD,
                                        button_color=BORDER, button_hover_color=FAINT, width=14)
        self.pf_tree.configure(yscrollcommand=self._pf_vsb.set)
        self.pf_tree.pack(side='left', fill='both', expand=True)
        self.pf_tree.bind('<Double-1>', self._pf_member_plot)
        self._selectable_cells(self.pf_tree)
        self._tip(self.pf_tree,
                  'The alphas this portfolio is made of, in pick order — equal weight,\n'
                  'no member is sized larger than another.\n'
                  'SOLO TEST — that member\'s held-out Sharpe ON ITS OWN. The combined\n'
                  'Sharpe above should beat every row here; if it does not, the mix is\n'
                  'carrying dead weight.\n'
                  'FITNESS and TEST OOS are the leaderboard\'s own numbers for the row.\n'
                  'Double-click a member for its equity chart.')
        self.pf_img = tk.Label(p3, bg=CARD, borderwidth=0, cursor='hand2')
        self.pf_img.pack(fill='x', pady=(8, 0))
        self.pf_img.bind('<Double-1>', self._pf_interactive)  # live zoomable copy of the chart
        self._tip(self.pf_img, 'Double-click — interactive chart: zoom with the mouse wheel,\n'
                               'pan, reset. The card itself keeps the auto-fitted image.')
        card3.bind('<Configure>', self._on_pf_resize)         # re-render equity to the panel width
        self.root.after(500, self._load_portfolio_on_start)   # show last build, if any
        # ---- FORWARD TRACK (append-only paper stepping of enrolled strategies) ----
        card4 = self._card(right)
        card4.grid(row=7, column=0, sticky='ew', pady=(14, 0))
        p4 = self._pad(card4)
        hf = self._box(p4)
        hf.pack(fill='x')
        self._head(hf, 'FORWARD TRACK — daily paper steps, append-only').pack(side='left')
        fctl = self._box(hf)
        fctl.pack(side='right')
        self.btn_fwd_step = self._btn(fctl, '▶ Step now', self._fwd_step, width=110)
        self.btn_fwd_step.pack(side='left')
        self._tip(self.btn_fwd_step, 'Run one paper step for every enrolled strategy: download the\n'
                                     'latest CLOSED daily bars from Binance, recompute the target\n'
                                     'positions with the real engine, mark-to-market, rebalance, fees.\n'
                                     'Runs automatically once per closed bar while the app is open.')
        self.btn_fwd_chart = self._btn(fctl, 'Chart', self._fwd_chart, width=76)
        self.btn_fwd_chart.pack(side='left', padx=(6, 0))
        self._tip(self.btn_fwd_chart, 'Forward equity of the selected row — only live steps,\n'
                                      'nothing recomputed backwards.')
        self.btn_fwd_sig = self._btn(fctl, 'Signals', self._fwd_signals, width=86)
        self.btn_fwd_sig.pack(side='left', padx=(6, 0))
        self._tip(self.btn_fwd_sig, 'Step-by-step log of the selected strategy: the held book\n'
                                    '(signed % of equity per asset), the executed rebalances,\n'
                                    'P&L and fees of every live step — exportable as CSV.')
        self.btn_fwd_serve = self._btn(fctl, 'Serve', self._fwd_serve, width=76)
        self.btn_fwd_serve.pack(side='left', padx=(6, 0))
        self._tip(self.btn_fwd_serve, 'Serve the selected strategy as a live signal API — exactly\n'
                                      'as frozen in the track (same formulas, pairs, bar size,\n'
                                      'vol and fee). Shows up in the SIGNAL API card and\n'
                                      'survives an app restart.')
        self.btn_fwd_arch = self._btn(fctl, 'Delete', self._fwd_delete, width=90)
        self.btn_fwd_arch.pack(side='left', padx=(6, 0))
        self._tip(self.btn_fwd_arch, 'Remove the selected strategy and its paper history from the\n'
                                     'track for good. The formula stays in your library — enroll it\n'
                                     'again anytime; the fresh track starts from a clean $10,000.')
        self.lbl_fwd = self._lbl(p4, text='', text_color=FAINT, font=(self.UI, 12),
                                 wraplength=900, anchor='w', justify='left')
        self.lbl_fwd.pack(anchor='w', fill='x', pady=(8, 2))
        self._fwdwrap = self._box(p4)
        self._fwdwrap.pack(fill='x', pady=(4, 0))
        fcols = ('id', 'kind', 'session', 'enrolled', 'days', 'equity', 'ret', 'sharpe', 'dd', 'last')
        self.fwd_tree = ttk.Treeview(p4, columns=fcols, show='headings',
                                     height=int(self.cfg.get('fwd_rows') or 4))
        for c, txt, w, anch in (('id', 'STRATEGY', 240, 'w'), ('kind', 'KIND', 96, 'w'),
                                ('session', 'SESSION', 80, 'center'),
                                ('enrolled', 'ENROLLED', 100, 'center'), ('days', 'STEPS', 64, 'e'),
                                ('equity', 'EQUITY', 100, 'e'), ('ret', 'RETURN', 90, 'e'),
                                ('sharpe', 'SHARPE', 80, 'e'), ('dd', 'MAXDD', 80, 'e'),
                                ('last', 'LAST STEP', 110, 'center')):
            self.fwd_tree.heading(c, text=txt)
            self.fwd_tree.column(c, width=int(w * self.SCALE), anchor=anch,
                                 stretch=(c == 'id'))
        self.fwd_tree.pack(in_=self._fwdwrap, fill='both', expand=True)
        self._wrap_apply_saved(self._fwdwrap, 'fwd_h')
        self._hgrip(p4, self._on_fwd_rows_drag, self._fwd_rows_reset,
                    'Drag: taller/shorter forward-track table. Double-click — natural height.')
        self.fwd_tree.bind('<Double-1>', lambda _e: self._fwd_chart())
        self._selectable_cells(self.fwd_tree)
        self.root.after(900, self._fwd_refresh)
        if not getattr(self, '_fwd_tick_on', False):          # _build reruns on theme switch —
            self._fwd_tick_on = True                          # keep exactly one tick loop
            self.root.after(20_000, self._fwd_tick)

        # ---- the dashboard cards are REORDERABLE: drag one by its padding / header strip ----
        self._cards = {'status': card, 'signals': self.sig_card,
                       'leaderboard': card2, 'portfolio': card3, 'forward': card4}
        self._wire_card_drag('status', pad, head, stats)
        self._wire_card_drag('leaderboard', p2, hrow)
        self._hgrip(p3, self._on_pf_grip, self._pf_grip_reset,
                    'Drag: taller/shorter equity plot. Double-click — automatic height.')
        self._wire_card_drag('portfolio', p3, hp)
        self._wire_card_drag('forward', p4, hf)
        self._regrid_cards()

    # ---------- reorderable dashboard cards ----------
    DASH_CARDS = ('status', 'signals', 'leaderboard', 'portfolio', 'forward')

    def _card_order(self):
        saved = [k for k in (self.cfg.get('card_order') or []) if k in self.DASH_CARDS]
        return saved + [k for k in self.DASH_CARDS if k not in saved]

    def _regrid_cards(self):
        """Grid the dashboard cards in the user's order. The leaderboard's row soaks up the
        window slack wherever it lands; a card hidden via grid_remove (signals while idle)
        keeps its slot hidden."""
        right = self._dash_right
        hidden = {n for n, w in self._cards.items() if not w.winfo_manager()}
        for r in range(3 * len(self._cards)):
            right.rowconfigure(r, weight=0, minsize=0)
        row = 0
        lb_manual = int(self.cfg.get('lb_h') or 0) > 0   # user pinned a height: the row must
        for i, name in enumerate(self._card_order()):    # not soak window slack anymore
            w = self._cards[name]
            stretch = (name == 'leaderboard' and not lb_manual
                       and not getattr(self, '_grip_live', False))
            w.grid(row=row, column=0, sticky=('nsew' if stretch else 'ew'),
                   pady=((0 if i == 0 else 16), 0))
            if name in hidden:
                w.grid_remove()                          # grid() above re-showed it — undo
            if stretch:
                # weight=1 makes this THE shrinkable row — when the fixed-height neighbours
                # (a dragged-tall console, portfolio, forward) outgrow the window, Tk squeezes
                # it to 0px and the whole leaderboard card vanishes, header included. Pin a
                # floor (~header + a few rows); the bottom cards clip instead, which at least
                # shows WHAT is fighting for space.
                right.rowconfigure(row, weight=1, minsize=int(220 * self.SCALE))
            row += 1

    def _wire_card_drag(self, name, *widgets):
        for w in widgets:
            w.bind('<ButtonPress-1>', lambda e, n=name: self._card_press(e, n))
            w.bind('<B1-Motion>', self._card_motion)
            w.bind('<ButtonRelease-1>', self._card_release)

    def _card_press(self, e, name):
        self._cdrag = {'name': name, 'y0': e.y_root, 'live': False}

    def _card_motion(self, e):
        d = getattr(self, '_cdrag', None)
        if d is None:
            return
        if not d['live']:
            if abs(e.y_root - d['y0']) < int(12 * self.SCALE):
                return                                   # a stray click, not a drag yet
            d['live'] = True
            try:                                         # accent border marks the card in flight
                self._cards[d['name']].configure(border_width=2, border_color=ACC)
            except tk.TclError:
                pass
        order = self._card_order()
        vis = [n for n in order if n != d['name'] and self._cards[n].winfo_manager()]
        place = len(vis)
        for i, n in enumerate(vis):                      # insert before the first card whose
            w = self._cards[n]                           # midpoint the pointer is above
            if e.y_root < w.winfo_rooty() + w.winfo_height() / 2:
                place = i
                break
        new = vis[:place] + [d['name']] + vis[place:]
        for h in (n for n in order if n != d['name'] and n not in vis):   # weave hidden back
            nxt = next((s for s in order[order.index(h) + 1:] if s in new), None)
            new.insert(new.index(nxt) if nxt is not None else len(new), h)
        if new != order:
            self.cfg['card_order'] = new
            self._regrid_cards()                         # live feedback while dragging

    def _card_release(self, _e):
        d = getattr(self, '_cdrag', None)
        self._cdrag = None
        if d and d.get('live'):
            try:
                self._cards[d['name']].configure(border_width=CARD_BW, border_color=BORDER)
            except tk.TclError:
                pass
            self._save()

    def _load_portfolio_on_start(self):
        try:
            doc = json.load(open(PORTFOLIO_JSON, encoding='utf-8'))
        except Exception:                                # noqa: BLE001
            return
        self._render_portfolio(doc)

    def _reset_portfolio_ui(self):
        """Clear the Portfolio panel (after a history wipe, or when nothing is built)."""
        self._pf_doc = None
        self._pf_last_w = 0
        self.lbl_pf.configure(text='no portfolio yet — set "top" and click "Build portfolio".')
        self.lbl_pf_m.configure(text='')
        if getattr(self, 'pf_tree', None) is not None:
            self.pf_tree.delete(*self.pf_tree.get_children())
            self.pf_tree.configure(height=1)
        self.pf_img.config(image='')
        self._pf_img_ref = None
        for b in (self.btn_pf_csv, self.btn_pf_sig, self.btn_pf_track):
            b.configure(state='disabled')

    def _stat(self, parent, label, col, accent=False):
        """A stat tile: big number + caption on its own soft rounded surface. `accent` marks the
        one tile the eye should land on (value in the accent colour, hairline accent border)."""
        tile = ctk.CTkFrame(parent, fg_color=(ACC_SOFT if accent else HEAD_BG),
                            corner_radius=12, border_width=(1 if accent else 0),
                            border_color=_mix(ACC_SOFT, ACC, 0.45))
        tile.grid(row=0, column=col, sticky='ew', padx=(0, 12))
        bgc = ACC_SOFT if accent else HEAD_BG
        f = self._box(tile, bg=bgc)
        f.pack(anchor='w', padx=int(16 * self.SCALE), pady=int(9 * self.SCALE))
        val = self._lbl(f, text='0', text_color=(ACC if accent else TXT),
                        font=(self.UI, 28, 'bold'), bg=bgc, anchor='w')
        val.pack(anchor='w')
        self._lbl(f, text=label.upper(), text_color=(ACC if accent else FAINT),
                  font=(self.UI, 10, 'bold'), bg=bgc, anchor='w').pack(anchor='w')
        return val

    # ---------- helpers ----------
    def _cpu_lbl(self):
        pct = int(self.v_cpu.get())
        self.lbl_cpu.configure(text=f'{pct}%  →  {max(1, round(pct/100*CORES))} of {CORES} cores')

    def _reset(self):
        keep = {k: self.cfg.get(k) for k in ('theme', 'settings_open', 'ui_mode')}   # appearance,
        #                                                                               not search
        self.cfg = dict(DEFAULTS)
        self.cfg.update(keep)
        try:
            os.remove(SETTINGS)
        except OSError:
            pass
        self._apply_cfg_to_widgets()

    def _count_lines(self, path):
        try:
            with open(path, encoding='utf-8') as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0

    def _wipe_history(self):
        if self.proc and self.proc.poll() is None:
            messagebox.showwarning('Node is running',
                                   'Stop the node first (it writes to these files), then clear.',
                                   parent=self.root)
            return
        n_alphas = self._count_lines(self._lib_file())
        n_rounds = self._count_lines(os.path.join(STATE_DIR, f'history{_tf_suffix(self._tf())}.jsonl'))
        if not (n_alphas or n_rounds or os.path.exists(STATUS_FILE)):
            messagebox.showinfo('Empty', 'History is already empty — nothing to clear.', parent=self.root)
            return
        msg = ('Delete ALL run history? This action is irreversible.\n\n'
               f'• {n_alphas} found alphas  (library.jsonl)\n'
               f'• {n_rounds} rounds of history  (history.jsonl)\n'
               '• current status  (status.json)\n'
               '• the built portfolio  (portfolio.json)\n\n'
               'Nothing is saved automatically — if you want a way back, save a session '
               'first (Sessions → Save current…).\n'
               'Search settings, starred favorites (★) and the FORWARD TRACK remain — enrolled '
               'strategies keep stepping. The next search runs under a fresh session id.')
        if not messagebox.askyesno('Full clear', msg, icon='warning',
                                    default='no', parent=self.root):
            return
        import glob
        removed = 0
        wipe = ['status.json', 'portfolio.json', 'session_params.json']   # + the frozen
        #                                       contract: a cleared node may be reconfigured freely
        for pat in ('library*.jsonl', 'history*.jsonl'):
            wipe += [os.path.basename(p) for p in glob.glob(os.path.join(STATE_DIR, pat))]
        for name in wipe:
            try:
                os.remove(os.path.join(STATE_DIR, name))
                removed += 1
            except OSError:
                pass
        for p in glob.glob(os.path.join(STATE_DIR, 'equity_view_*.png')):
            try:
                os.remove(p)
            except OSError:
                pass
        import shutil
        for p in glob.glob(os.path.join(STATE_DIR, 'series*')):   # the vault's cached return
            if os.path.isdir(p):                                    # series (see series_cache)
                shutil.rmtree(p, ignore_errors=True)
        try:
            os.remove(PORTFOLIO_PNG)                      # the portfolio equity image
        except OSError:
            pass
        try:                                             # the work that follows a clear is a NEW
            self._sessions_lib().begin_new_session(STATE_DIR)   # session — forward entries
        except Exception:                                # noqa: BLE001    enrolled from it say so
            pass
        try:
            self.sid_lbl.configure(text=f'session {self._session_id()}')
        except (AttributeError, tk.TclError):
            pass
        if self._shell_mode == 'simple':                 # the advanced reset touches widgets
            self._sm_reset_after_wipe()                  # the simple shell never builds
        else:
            self._reset_ui_after_wipe()
        messagebox.showinfo('Done', 'History cleared. You can start the search from scratch.', parent=self.root)

    STALE_DAYS = 5                                       # snapshot older than this -> refresh at Start

    def _data_gap(self):
        """What (if anything) Start must download first: (symbols_to_fetch|None, reason).
        A gap is a missing/unreadable snapshot, a configured pair the snapshot lacks, or a
        snapshot whose newest bar is more than STALE_DAYS behind — the presence check the
        user asked to replace the manual Download button with."""
        want = self._universe_tickers() or _parse_universe(DEFAULTS['universe_list'])
        path = self._data_file()
        if not os.path.exists(path):
            return want, 'no market data for this timeframe yet'
        try:
            tick, dfs = pickle.load(open(path, 'rb'))
        except Exception:                                # noqa: BLE001 — unreadable = absent
            return want, 'the data snapshot is unreadable'
        missing = [s for s in want if s not in set(tick)]
        if missing:
            gone = ', '.join(missing[:4]) + ('…' if len(missing) > 4 else '')
            return want, f'the snapshot has no {gone}'
        try:
            last = max(df.index[-1] for df in dfs if len(df))
            if last.tzinfo is None:
                last = last.tz_localize('UTC')
            age = (datetime.now(timezone.utc) - last.to_pydatetime()).days
            if age > self.STALE_DAYS:
                return want, f'the snapshot is {age} days old'
        except Exception:                                # noqa: BLE001 — can't date it, don't block
            pass
        return None, ''

    def _start_after_fetch(self):
        """START resumes after its data download. Pairs the fetch did NOT deliver — delisted
        or typo'd, Binance simply has nothing to give — leave the universe here, loudly.
        Without this, one such ticker looped Start into download-then-error forever (the
        universe_all migration inherits whatever an old snapshot held, the user may never
        have typed the name being complained about)."""
        need, _why = self._data_gap()
        if need:
            have = set(self._snapshot_tickers() or [])
            want = self._universe_tickers() or []
            gone = [s for s in want if s not in have]
            if gone and have:
                keep = [s for s in want if s in have]
                self.cfg['universe_list'] = ','.join(keep) or DEFAULTS['universe_list']
                if hasattr(self, 'v_unilist'):
                    self.v_unilist.set(self.cfg['universe_list'])
                self._save()
                messagebox.showwarning(
                    'Market data',
                    f'Binance futures does not serve: {", ".join(gone)}.\n'
                    'Dropped from the pairs universe — continuing with '
                    f'{self.cfg["universe_list"]}.', parent=self.root)
            else:                                        # nothing to drop, yet the gap persists
                self._fetch_tried = repr((sorted(need), self._tf()))   # (unreadable file, …) —
        self.start()                                     # arm the backstop and let start() speak

    def _auto_fetch(self, symbols, why, on_success=None):
        """Start's own data step: download the configured basket at the active timeframe.
        Replaces both the manual 'Download fresh data' button and the first-run bootstrap —
        one path, always the exact pairs from Settings."""
        if self._fetching:                               # a silent no-op read as a dead button
            messagebox.showinfo('Market data', 'A data download is already running — wait '
                                'for it to finish, then press START again.', parent=self.root)
            return
        tf = self._tf()
        if getattr(self, '_shell_mode', 'advanced') == 'simple':
            self._run_fetch_quiet(['--symbols', ','.join(symbols), '--interval', tf,
                                   '--out', self._data_file()],
                                  f'downloading {len(symbols)} pairs ({tf}) — {why}…',
                                  on_success=on_success)
            return
        self._run_fetch(['--symbols', ','.join(symbols), '--interval', tf,
                         '--out', self._data_file()],
                        f'Market data — {len(symbols)} pairs ({tf})',
                        f'{why} — downloading {len(symbols)} pairs '
                        f'({", ".join(s.replace("USDT", "") for s in symbols[:8])}'
                        f'{"…" if len(symbols) > 8 else ""}) as {tf} candles from Binance…\n\n',
                        '✓ Data ready.' + ('' if on_success else ' Press START to begin.'),
                        on_success=on_success)

    def _run_fetch(self, args, title, intro, done_msg, on_success=None):
        """Console dialog + fetch_data subprocess; shared by the manual data update and the
        first-run bootstrap. on_success fires (on the Tk thread) only on exit code 0."""
        if self._fetching:
            return
        self._fetching = True
        win = self._dialog(title, '760x440')
        txt = self._console(win)

        def add(s):
            if not win.winfo_exists():
                return
            txt.configure(state='normal')
            txt.insert('end', s)
            txt.see('end')
            txt.configure(state='disabled')

        add(intro)
        try:
            proc = subprocess.Popen(_child_cmd('fetch') + args,
                                    cwd=(apppaths.USER_DIR if apppaths.FROZEN else PROJ),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding='utf-8', errors='replace')
        except Exception as e:                       # noqa: BLE001
            add(f'Failed to launch fetch_data.py: {e}\n')
            self._fetching = False
            return
        q = queue.Queue()

        def _reader():
            for line in proc.stdout:
                q.put(line)
            q.put(None)
        threading.Thread(target=_reader, daemon=True).start()

        def _cancel():                                   # closing the console cancels the fetch:
            try:                                         # a detached writer would race the next
                if proc.poll() is None:                  # Start's own download onto the same file
                    proc.terminate()                     # (fetch writes .tmp + os.replace, so a
            except Exception:                            # noqa: BLE001 — mid-write kill leaves
                pass                                     # the old snapshot untouched)
            win.destroy()
        win.protocol('WM_DELETE_WINDOW', _cancel)

        def pump():
            if not win.winfo_exists():
                self._fetching = False                   # the process will finish on its own
                return
            try:
                while True:
                    line = q.get_nowait()
                    if line is None:
                        code = proc.poll()
                        add('\n' + (done_msg if code == 0
                                    else f'✗ Error (code {code}). Data left untouched.') + '\n')
                        self._fetching = False
                        self._lib_cache['mtime'] = None
                        self._metrics_cache = {}         # fresh data: stats measured on the old
                        #                                  snapshot must not survive the swap
                        if code == 0 and on_success is not None:
                            win.after(700, on_success)
                        return
                    add(line)
            except queue.Empty:
                pass
            win.after(150, pump)
        win.after(150, pump)

    def _maybe_bootstrap(self):
        """First run: no market data next to the node -> download the configured basket."""
        if os.path.exists(self._data_file()) or (self.proc and self.proc.poll() is None):
            return
        need, why = self._data_gap()
        if need:
            self._auto_fetch(need, why)

    def _reset_ui_after_wipe(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        self._treesig = None
        self._shown = []
        self._lb_need_px = 0                              # collapse the formula column back
        self._fit_formula_col()
        self._lib_cache = {'mtime': None, 'all': [], 'families': [], 'computing': False,
                           'dirty': False, 'ts': 0.0, 'computed': False, 'select': None}
        self.s_fit.configure(text='—')
        self.s_rounds.configure(text='0')
        self.s_trials.configure(text='0')
        self.s_found.configure(text='0')
        self._state_pill('● stopped', MUT)
        self._reset_portfolio_ui()                        # clear the Portfolio panel too

    def _apply_cfg_to_widgets(self):
        c = self.cfg
        self.v_cpu.set(c['cpu']); self._cpu_lbl()
        self.v_unilist.set(c['universe_list'])
        try:
            self.e_uni.delete(0, 'end')              # a half-typed pair must not outlive a
        except Exception:                            # noqa: BLE001 — reset / session load
            pass
        self.v_pop.set(c['pop']); self.v_gens.set(c['gens']); self.v_seed.set(c['seed'])
        self.v_pause.set(c['pause']); self.v_port.set(c['port'])
        self.v_tf.set(_tf_clean(c.get('timeframe', '1d'))); self._tf_note()
        self.v_explore.set(c['explore_every']); self.v_maxrounds.set(c['max_rounds'])
        self.v_leader.set(c['leaderboard']); self.v_seedlib.set(c['seed_from_lib'])
        self.v_vol.set(c['target_vol']); self.v_exec.set(c['exec_cost'])
        self.v_depth.set(c['max_depth']); self.v_size.set(c['max_size'])
        self.v_tourn.set(c['tournament']); self.v_elit.set(c['elitism'])
        self.v_inject.set(c['random_inject']); self.v_cx.set(c['crossover_prob'])
        self.v_pars.set(c['parsimony']); self.v_corrt.set(c['corr_threshold'])
        self.v_corrp.set(c['corr_penalty']); self.v_hof.set(c['hof_capacity'])
        self.v_fitblocks.set(c.get('fit_blocks', 5))
        self.v_optwr.set(c.get('opt_winrate', False))
        self.v_train.set(self._seg_disp('train_start')); self.v_val.set(self._seg_disp('val_start'))
        self.v_test.set(self._seg_disp('test_start')); self.v_end.set(self._seg_disp('test_end'))

    def _spark_paint(self, cv, base, test):
        """The curve itself — the simple search card's chart (the advanced copy of this
        strip was removed on user request).
        Gridlines put NUMBERS on the shape, both endpoints carry their value, and the TEST
        line wears the warm direction colour — the muted hairline dash was unreadable on the
        dark theme (user-reported)."""
        w = cv.winfo_width()
        cv.delete('all')
        if len(base) < 2 or w <= 1:
            return
        h = int(cv['height'])
        both = base + [v for v in test if isinstance(v, (int, float))]
        lo, hi = min(both), max(both)
        span = max(hi - lo, 0.6)                         # a run whose best barely moves is a
        lo -= (span - (hi - lo)) / 2                     # fact, not a line glued to the ceiling

        def _xy(i, v, n):
            return (i / (n - 1) * (w - 6) + 3,
                    h - 4 - (v - lo) / span * (h - 8))
        pts = [c for i, v in enumerate(base) for c in _xy(i, v, len(base))]
        cv.create_polygon(*pts, pts[-2], h, pts[0], h,   # the soft area under the curve
                          fill=_mix(ACC, CARD, 0.88), outline='')
        f_tick = self._font(self.MONO, 8)
        step = next((s for s in (0.1, 0.2, 0.25, 0.5, 1.0, 2.0, 5.0)
                     if span / s <= 4.5), 10.0)          # a few NICE values, never a thicket
        k = math.ceil(lo / step)
        while k * step <= lo + span + 1e-9:
            v = round(k * step, 2)
            gy = h - 4 - (v - lo) / span * (h - 8)
            cv.create_line(3, gy, w - 3, gy, fill=GRID)
            cv.create_text(6, gy - 1, anchor='sw', text='%+g' % v, fill=MUT, font=f_tick)
            k += 1
        tp = [c for i, v in enumerate(test) if isinstance(v, (int, float))
              for c in _xy(i, v, len(test))]
        if len(tp) >= 4:
            cv.create_line(*tp, fill=SHORTC, width=max(1, round(1.4 * self.SCALE)),
                           dash=(4, 3))
        cv.create_line(*pts, fill=ACC, width=max(2, round(2 * self.SCALE)))
        x, y = _xy(len(base) - 1, base[-1], len(base))
        r = 3.2 * self.SCALE
        cv.create_oval(x - r, y - r, x + r, y + r, fill=ACC, outline='')
        f_val = self._font(self.MONO, 9, 'bold')
        ly = min(max(y, 9), h - 9)                       # clamped inside the canvas
        cv.create_text(x - 7, ly, anchor='e', text='%+.2f' % base[-1], fill=ACC, font=f_val)
        if len(tp) >= 4:
            tv = next(v for v in reversed(test) if isinstance(v, (int, float)))
            ty = min(max(tp[-1], 9), h - 9)
            if abs(ty - ly) < 13 * self.SCALE:           # endpoints can meet — keep both readable
                ty = ly + 13 * self.SCALE if ty >= ly else ly - 13 * self.SCALE
            cv.create_text(tp[-2] - 7, ty, anchor='e', text='%+.2f' % tv,
                           fill=SHORTC, font=f_val)

    def _run_pill_click(self):
        if self.proc and self.proc.poll() is None:
            self.stop()
            self._pill_paint(getattr(self, 'run_pill', None), 'run', None,
                             'stopping', 'the round finishes first')
        else:
            self.start()

    def _set_running(self, running):
        for name, state in (('btn_start', 'disabled' if running else 'normal'),
                            ('btn_stop', 'normal' if running else 'disabled')):
            w = getattr(self, name, None)                # kept for any straggler references —
            try:                                         # the header control is the pill now
                if w is not None and w.winfo_exists():
                    w.configure(state=state)
            except tk.TclError:
                pass
        pill = getattr(self, 'run_pill', None)
        if pill is not None and running != getattr(self, '_pill_running', None):
            self._pill_running = running                 # paint on the TRANSITION only: _poll
            if running:                                  # calls _set_running every 1.5s, and the
                self._pill_paint(pill, 'run', None, 'running', '')   # detailed live paint follows
            else:
                self._pill_paint(pill, 'idle', None, 'Start node')
        if running != getattr(self, '_settings_locked', None):
            self._settings_locked = running              # freeze the session's parameters while
            self._lock_settings(running)                 # the node runs — edits only after Stop
            if hasattr(self, 'uni_chips'):
                self._uni_render()                       # repaint chips in their locked look

    def _lock_settings(self, locked):
        """Disable every input in the settings pane while the node runs (user: no changes
        mid-run, only after Stop). Idempotent; driven by the Start/Stop transition."""
        inner = getattr(self, '_settings_inner', None)
        if inner is None:
            return
        interactive = (ctk.CTkEntry, ctk.CTkCheckBox, ctk.CTkSlider, ctk.CTkButton,
                       ctk.CTkOptionMenu, ctk.CTkComboBox, ctk.CTkSwitch)

        def walk(w):
            for c in w.winfo_children():
                try:
                    if isinstance(c, ttk.Combobox):      # readonly, not free-text, when enabled
                        c.configure(state='disabled' if locked else 'readonly')
                    elif isinstance(c, (ttk.Spinbox, ttk.Entry, ttk.Scale)):   # the number boxes
                        c.configure(state='disabled' if locked else 'normal')  # (Population etc.)
                    elif isinstance(c, interactive):
                        c.configure(state='disabled' if locked else 'normal')
                except tk.TclError:
                    pass
                walk(c)
        walk(inner)

    def _state_pill(self, text, color):
        """The status pill: text + a soft wash of the state colour behind it."""
        tint = HEAD_BG if color == MUT else _mix(color, CARD, 0.87)
        self.pill_state.configure(fg_color=tint)
        self.lbl_state.configure(text=text, fg=color, bg=tint)

    # ---------- timeframe-aware paths ----------
    def _on_tf_change(self, _evt=None):
        """Picking a timeframe fills in its recommended date segments and pair count (from
        evolution/timeframe.py). These are only sensible defaults — the user can still edit the
        fields. Finer bars get a shorter, later window (intraday history is denser and heavier)."""
        try:
            from timeframe import resolve as _rtf
            t = _rtf(self.v_tf.get())
        except Exception:                                # noqa: BLE001
            return
        seg = {f: _seg_res(t.name, f, 'auto')                # the new tf's auto window, as
               for f in ('train_start', 'val_start',         # REAL dates — cfg stores the
                         'test_start', 'test_end')}          # sentinel back on save
        self.v_train.set(seg['train_start']); self.v_val.set(seg['val_start'])
        self.v_test.set(seg['test_start']); self.v_end.set(seg['test_end'])
        self._tf_note()

    def _tf_note(self):
        """Refresh the one-line hint under the timeframe selector (no field changes)."""
        try:
            from timeframe import resolve as _rtf
            t = _rtf(self.v_tf.get())
        except Exception:                                # noqa: BLE001
            return
        span = f'window ≤ {t.max_bars:,} bars (~{t.max_span_days / 365.0:.1f} yr)'
        note = ('daily engine — full history · ' + span if t.name == '1d' else
                f'{t.name} bars · history from {t.history} · {span} · '
                f'download {t.name} data first, then Start')
        try:
            self.lbl_tf_note.configure(text=note)
        except (AttributeError, tk.TclError):
            pass

    # ---------- date segments: the four boxes are checked, not trusted ----------
    def _seg_problems(self):
        """(field, message) pairs for the four date boxes against the bar size CURRENTLY
        selected in the widget — not the saved one, because the user may be mid-edit and the
        ceiling that matters is the one they are about to mine on. [] when they are fine."""
        try:
            from timeframe import check_segments
            return check_segments(self.v_tf.get(), self.v_train.get(), self.v_val.get(),
                                  self.v_test.get(), self.v_end.get())
        except (ImportError, ValueError, AttributeError, tk.TclError):
            return []                                    # no timeframe module / torn-down widgets

    def _seg_check(self):
        """Repaint the note under the date fields and redden whichever box is at fault.
        Returns the problems so Start can reuse the same verdict as the hard gate."""
        probs = self._seg_problems()
        bad = {f for f, _m in probs}
        for lbl, v in (('TRAIN start', self.v_train), ('VAL start', self.v_val),
                       ('TEST start', self.v_test), ('TEST end', self.v_end)):
            w = getattr(v, 'widget', None)
            if w is not None:
                try:
                    w.configure(border_color=(NEG if lbl in bad else BORDER))
                except tk.TclError:                      # widget died under a theme rebuild
                    pass
        lbl_seg = getattr(self, 'lbl_seg', None)
        if lbl_seg is not None:
            try:
                if probs:
                    lbl_seg.configure(text='\n'.join(m for _f, m in probs), fg=NEG)
                    lbl_seg.grid()
                else:
                    segs = self._collect_segments()
                    n_auto = sum(1 for v in segs.values() if v in ('auto', 'today'))
                    if n_auto:                           # the boxes already carry the dates —
                        lbl_seg.configure(                # the note names the MODE
                            text=('auto window — these dates follow today; '
                                  'edit a box to pin it' if n_auto == 4 else
                                  f'{n_auto} of 4 dates follow today, the rest are pinned'),
                            fg=MUT)
                        lbl_seg.grid()
                    else:
                        lbl_seg.grid_remove()
            except (tk.TclError, AttributeError):        # mid-build: not all boxes exist yet
                pass
        return probs

    def _collect_segments(self):
        """The four date boxes -> cfg. A box left at today's auto value (or the literal
        sentinel) is stored as the sentinel — the window keeps sliding; anything else the
        user typed is stored verbatim — pinned."""
        auto = self._seg_auto_now()
        out = {}
        for field, v in (('train_start', self.v_train), ('val_start', self.v_val),
                         ('test_start', self.v_test), ('test_end', self.v_end)):
            raw = v.get().strip()
            if raw.lower() in ('auto', 'today', '') or raw == auto[field]:
                out[field] = 'today' if field == 'test_end' else 'auto'
            else:
                out[field] = raw
        return out

    def _seg_auto_now(self):
        """The active timeframe's auto window, resolved to real dates (for display and for
        telling 'still auto' apart from 'pinned by hand')."""
        try:
            tf0 = _tf_clean(self.v_tf.get())
        except (AttributeError, tk.TclError):
            tf0 = self._tf()
        return {f: _seg_res(tf0, f, 'auto')
                for f in ('train_start', 'val_start', 'test_start', 'test_end')}

    def _seg_disp(self, field):
        """What a date box SHOWS: the real date (user: 'выводи, что за даты берёшь') — the
        sentinel itself lives only in cfg, so the window keeps sliding day to day."""
        try:
            return _seg_res(_tf_clean(self.cfg.get('timeframe')), field,
                            self.cfg.get(field, ''))
        except Exception:                                # noqa: BLE001 — engine off the path
            return str(self.cfg.get(field, ''))

    def _tf(self):
        """The configured bar size ('1d','4h','1h','15m'); the whole pipeline follows it."""
        return _tf_clean(self.cfg.get('timeframe'))

    def _data_file(self):
        """Per-timeframe data snapshot: the classic data.pickle for 1d, data_<tf>.pickle else."""
        return self._data_file_for(self._tf())

    def _data_file_for(self, tf):
        """Snapshot path of an ARBITRARY bar size — a frozen forward entry may sit on a
        different tf than the one configured now (the served copy must seed from ITS data)."""
        root, ext = os.path.splitext(apppaths.data_path())
        return f'{root}{_tf_suffix(_tf_clean(tf))}{ext}'

    def _lib_file(self):
        """Per-timeframe alpha library: alphas mined on different bar sizes never mix."""
        return os.path.join(STATE_DIR, f'library{_tf_suffix(self._tf())}.jsonl')

    # ---------- the session contract: search parameters are frozen once a library exists ----------
    # The dates/pairs/costs/objective a library was mined under define the MEANING of every score
    # in it. Change them and restart, and the node would append alphas scored under a DIFFERENT
    # rule into the same file — the leaderboard would rank apples against oranges. So once a
    # session's library is non-empty, these parameters are locked; changing them needs a fresh
    # node (Clear). Snapshot is per timeframe (each bar size keeps its own library).
    CONTRACT_KEYS = (
        ('universe_list', 'Pairs universe'),
        ('train_start', 'TRAIN start'), ('val_start', 'VAL start'),
        ('test_start', 'TEST start'), ('test_end', 'TEST end'),
        ('target_vol', 'Target volatility'), ('exec_cost', 'Execution cost'),
        ('opt_winrate', 'Objective (win-rate)'), ('fit_blocks', 'Robust-fitness blocks'),
    )

    def _contract(self):
        """The frozen search parameters as they stand in cfg (sentinels kept raw, so an 'auto'
        window stays 'auto' day to day and never false-conflicts)."""
        return {k: str(self.cfg.get(k, '')) for k, _lbl in self.CONTRACT_KEYS}

    def _contract_load(self):
        try:
            return json.load(open(SESSION_PARAMS_JSON, encoding='utf-8'))
        except Exception:                                # noqa: BLE001 — absent/corrupt = fresh
            return {}

    def _session_conflict(self):
        """(diffs, snapshot) — diffs is [(label, was, now), …] when the current parameters
        disagree with the contract this timeframe's non-empty library was mined under; []
        otherwise (a fresh/empty library, or no snapshot to honour = grandfathered)."""
        if self._count_lines(self._lib_file()) == 0:     # nothing mined yet: any params are fine
            return [], None
        snap = self._contract_load().get(self._tf())
        if not snap:
            return [], None
        cur = self._contract()
        diffs = [(lbl, snap.get(k, ''), cur.get(k, ''))
                 for k, lbl in self.CONTRACT_KEYS if str(snap.get(k, '')) != str(cur.get(k, ''))]
        return diffs, snap

    def _contract_seal(self):
        """Record this timeframe's contract when a session begins — overwrite on a fresh
        (empty) library so a new session may redefine it; otherwise grandfather an existing
        library once and never move its contract afterwards."""
        tf, snaps = self._tf(), self._contract_load()
        if self._count_lines(self._lib_file()) == 0 or tf not in snaps:
            snaps[tf] = self._contract()
            try:
                json.dump(snaps, open(SESSION_PARAMS_JSON, 'w', encoding='utf-8'), indent=1)
            except OSError:
                pass

    def _session_conflict_dialog(self, diffs, snap):
        """Block the restart and let the user choose: revert to the session's settings, or
        clear the node and start fresh under the new ones."""
        win = self._dialog('Session settings changed', '560x' + str(280 + len(diffs) * 26))
        win.transient(self.root)
        self.root.after(200, win.grab_set)
        pad = self._pad(win)
        self._lbl(pad, text='These search settings differ from the ones this library was '
                            'mined under:', text_color=TXT, font=(self.UI, 14, 'bold'),
                  wraplength=500, justify='left').pack(anchor='w', pady=(4, 8))
        for lbl, was, now in diffs:
            self._lbl(pad, text=f'•  {lbl}:   {was or "—"}   →   {now or "—"}',
                      text_color=MUT, font=(self.MONO, 12), justify='left').pack(anchor='w')
        self._lbl(pad, text='Mining new alphas under different rules into the same library '
                            'corrupts the ranking. Choose how to continue:', text_color=TXT,
                  font=(self.UI, 12), wraplength=500, justify='left').pack(anchor='w', pady=(12, 12))
        row = self._box(pad)
        row.pack(fill='x')

        def revert():
            self.cfg.update({k: snap.get(k, self.cfg.get(k)) for k, _l in self.CONTRACT_KEYS})
            self._apply_cfg_to_widgets()
            self._save()
            self._seg_check()
            win.destroy()

        def fresh():
            win.destroy()
            self._wipe_history()                         # asks its own confirm; clears the snapshot

        ctk.CTkButton(row, text='Revert to session settings', command=revert,
                      fg_color=ACC, hover_color=ACC_HI, width=210).pack(side='left')
        ctk.CTkButton(row, text='Clear node & start fresh', command=fresh,
                      fg_color='transparent', border_width=1, border_color=NEG,
                      text_color=NEG, width=190).pack(side='right')
        self._lbl(pad, text='Or close this and keep editing — the node will not start until '
                            'the settings match or the library is cleared.', text_color=FAINT,
                  font=(self.UI, 11), wraplength=500, justify='left').pack(anchor='w', pady=(12, 0))

    def _tf_gate(self, what):
        """True (and explains itself) when `what` is daily-only and an intraday tf is active."""
        if self._tf() == '1d':
            return False
        messagebox.showinfo(
            'Daily only (for now)',
            f'{what} runs through the real quantpylib engine, which is tuned for daily bars — '
            f'on {self._tf()} its numbers would be silently wrong.\n\n'
            'On intraday timeframes use the search, the leaderboard, equity charts, CSV signals, '
            'PDF reports, the forward track and the live signal API. Portfolio/paper support '
            'is the next phase.', parent=self.root)
        return True

    # ---------- sessions (workspace snapshots) ----------
    def _sessions_lib(self):
        import sessions
        return sessions

    def _session_id(self):
        """The current working session's id — '?' only if the state dir is unwritable."""
        try:
            return self._sessions_lib().current_session_id(STATE_DIR)
        except Exception:                                # noqa: BLE001
            return '?'

    def _sessions_open(self):
        S = self._sessions_lib()
        win = tk.Toplevel(self.root)
        win.title('Sessions')
        win.configure(bg=BG)
        win.transient(self.root)
        win.geometry(f'{int(760 * self.SCALE)}x{int(420 * self.SCALE)}')
        pad = tk.Frame(win, bg=BG)
        pad.pack(fill='both', expand=True, padx=14, pady=12)
        self._head(pad, 'SESSIONS — the whole workspace as one file').pack(anchor='w')
        self._lbl(pad, text='Formulas, portfolio, ★ favourites, the run counters and settings '
                            '(the FORWARD TRACK is global — it belongs to no session). ID is '
                            'the SESSION id — the one in the header when '
                            'it was saved; two saves of one session share it and differ by '
                            'date. The licence key never travels inside a session. '
                            'Double-click a row for details.',
                  text_color=MUT, font=(self.UI, 12)).pack(anchor='w', pady=(2, 8))
        cols = ('id', 'created', 'name', 'alphas', 'size', 'kind')
        tree = ttk.Treeview(pad, columns=cols, show='headings', height=9)
        for c, txt, w, anc in (('id', 'ID', 72, 'center'),
                               ('created', 'CREATED', 150, 'w'), ('name', 'NAME', 190, 'w'),
                               ('alphas', 'ALPHAS', 90, 'center'),
                               ('size', 'SIZE', 80, 'e'), ('kind', '', 70, 'center')):
            tree.heading(c, text=txt)
            tree.column(c, width=int(w * self.SCALE), anchor=anc, stretch=(c == 'name'))
        tree.pack(fill='both', expand=True)
        self._selectable_cells(tree)

        def _fill():
            tree.delete(*tree.get_children())
            for m in S.list_sessions(STATE_DIR):
                al = ' · '.join(f'{k}:{v}' for k, v in sorted(m.get('alphas', {}).items()))
                tree.insert('', 'end', iid=m['path'], values=(
                    m.get('id', '—'),
                    m.get('created', '')[:16].replace('T', ' '), m.get('name') or '—',
                    al or '—',
                    f"{m['size'] // 1024} KB", 'auto' if m.get('auto') else 'named'))
        _fill()

        def _sel():
            it = tree.selection()
            return it[0] if it else None

        def _save():
            dlg = ctk.CTkInputDialog(text='Name this session:', title='Save session')
            name = (dlg.get_input() or '').strip()
            if not name:
                return
            try:
                saved = S.snapshot(name=name, state_dir=STATE_DIR, settings_path=SETTINGS)
            except Exception as e:                       # noqa: BLE001
                messagebox.showerror('Sessions', f'Could not save the session:\n{e}',
                                     parent=win)
                _fill()                                  # a partial write leaves nothing, but
                return                                   # the list may still be stale
            _fill()
            if saved and tree.exists(saved):             # land on the new row: names repeat,
                tree.selection_set(saved)                # so its ID is the thing to look at
                tree.see(saved)

        def _load():
            path = _sel()
            if not path:
                messagebox.showinfo('Sessions', 'Select a session in the list first.', parent=win)
                return
            def _busy():                                 # every writer of an OWNED file must be
                if self.proc and self.proc.poll() is None:   # idle: a child finishing AFTER the
                    return 'the node is searching'       # swap would overwrite restored files.
                # (a forward-track step is NOT a reason to wait: the track is global and a
                #  load never touches forward.json — the only file the stepper writes)
                if self._pf_proc and self._pf_proc.poll() is None:
                    return 'the portfolio build is running'
                return None
            busy = _busy()
            if busy:
                messagebox.showwarning('Sessions', f'Not now — {busy}. Wait for it to finish '
                                       '(usually under a minute), then load.', parent=win)
                return
            if not messagebox.askyesno('Sessions',
                    'Load this session? The current workspace will be REPLACED — nothing is '
                    "saved automatically. Use 'Save current…' first if you want a way "
                    'back.\n\nLibrary, portfolio, ★ favorites and the run counters are '
                    'replaced by this session\'s. The FORWARD TRACK is global — it is not '
                    'part of a session and keeps running untouched.',
                    parent=win):
                return
            busy = _busy()                               # the modal pumped after-timers while it
            if busy:                                     # sat open — the 5-minute forward tick
                messagebox.showwarning(                  # may have spawned a step meanwhile
                    'Sessions', f'Not now — {busy}. Wait for it to finish '
                    '(usually under a minute), then load.', parent=win)
                return
            try:
                man = S.restore(path, state_dir=STATE_DIR, settings_path=SETTINGS)
            except Exception as e:                       # noqa: BLE001
                messagebox.showerror('Sessions',
                                     f'Load failed — the workspace was left as it was.\n\n{e}',
                                     parent=win)
                _fill()
                return
            win.destroy()
            self._sessions_rebuild()
            n_alphas = sum((man.get('alphas') or {}).values())
            n_fav = man.get('favorites') or 0             # absent in pre-favorites archives
            if n_alphas:
                messagebox.showinfo('Sessions',
                                    f'Session loaded: {man.get("name") or man.get("created", "")}\n'
                                    f'{n_alphas} alphas · {n_fav} ★ favorites.\n'
                                    'Counters, live log, portfolio and ★ came back with it. '
                                    'The forward track is global and was left untouched.',
                                    parent=self.root)
            else:
                messagebox.showwarning('Sessions',
                                       'Session loaded — but this archive carried no alphas '
                                       '(it was saved from an empty workspace).',
                                       parent=self.root)

        _UI_KEYS = {'theme', 'settings_open', 'lb_mode', 'card_order', 'lb_rows', 'lb_h',
                    'lb_cols', 'fwd_rows', 'fwd_h', 'pf_h', 'eula_accepted', 'ui_mode'}

        def _details(_event=None):
            path = _sel()
            if not path:
                return
            pk = S.peek(path)
            man = pk.get('manifest') or {}
            cfg = pk.get('settings') or {}
            pf = pk.get('portfolio')

            L = []
            title = man.get('name') or os.path.basename(path)
            L.append(f"SESSION  {title}   ·   id {man.get('id', '?')}"
                     + ('  (derived from the filename — saved before ids)'
                        if man.get('id_derived') else ''))
            if man.get('session') and man.get('session') != man.get('id'):
                # a transitional archive: written while saves minted their own id
                L.append(f"           (workspace session at save time: {man['session']})")
            L.append(f"created  {man.get('created', '?')[:19].replace('T', ' ')}   "
                     f"app v{man.get('version', '?')}   "
                     f"{'auto' if man.get('auto') else 'saved by hand'}")
            al = man.get('alphas') or {}
            L.append('alphas   ' + (' · '.join(f'{k}: {v}' for k, v in sorted(al.items()))
                                    or 'none'))
            L.append(f"stars    {man.get('favorites', 0)} ★ favorites")
            rn = man.get('run') or {}
            L.append('run      ' + (f"{rn.get('rounds', 0)} rounds · "
                                    f"{rn.get('trials_total', 0):,} formulas tried · "
                                    f"{rn.get('found', 0)} kept"
                                    if rn else 'no search had run yet'))
            L.append('')
            L.append('PORTFOLIO')
            if pf is None:
                L.append('  — none was built in this session')
            elif pf.get('ok') is False:
                L.append(f"  build failed: {pf.get('error', '?')}")
            else:
                m = pf.get('metrics') or {}
                if m:
                    L.append('  ' + '   '.join(f'{k} {v:.3f}' if isinstance(v, float)
                                               else f'{k} {v}' for k, v in sorted(m.items())))
                tk_list = (pf.get('weights') or {}).get('tickers') or []
                if tk_list:
                    L.append(f"  universe ({len(tk_list)}): {', '.join(tk_list[:12])}"
                             + (' …' if len(tk_list) > 12 else ''))
                for i, f in enumerate(pf.get('formulas') or [], 1):
                    L.append(f'  {i}. {f}')
                if not m and not pf.get('formulas'):
                    L.append('  ' + json.dumps(pf)[:600])
            L.append('')
            L.append('SETTINGS')
            for k in sorted(k for k in cfg if k not in _UI_KEYS):
                L.append(f'  {k:<16} {cfg[k]}')
            ui = [k for k in sorted(cfg) if k in _UI_KEYS]
            if ui:
                L.append('')
                L.append('INTERFACE')
                for k in ui:
                    L.append(f'  {k:<16} {cfg[k]}')

            dlg = tk.Toplevel(win)
            dlg.title(f'Session — {title}')
            dlg.configure(bg=BG)
            dlg.transient(win)
            dlg.geometry(f'{int(680 * self.SCALE)}x{int(560 * self.SCALE)}')
            box = tk.Frame(dlg, bg=BG)
            box.pack(fill='both', expand=True, padx=14, pady=12)
            txt = tk.Text(box, bg=CARD, fg=TXT, insertbackground=TXT, relief='flat',
                          font=(self.MONO, self._px(11)), wrap='none', padx=12, pady=10)
            sb = ttk.Scrollbar(box, orient='vertical', command=txt.yview)
            txt.configure(yscrollcommand=sb.set)
            sb.pack(side='right', fill='y')
            txt.pack(side='left', fill='both', expand=True)
            txt.insert('1.0', '\n'.join(L))
            txt.configure(state='disabled')
            self._text_selectable(txt)
            self._btn(dlg, 'Close', dlg.destroy, kind='soft', height=30,
                      width=80).pack(anchor='e', padx=14, pady=(0, 12))

        tree.bind('<Double-1>', _details)

        def _delete():
            path = _sel()
            if not path:
                return
            if messagebox.askyesno('Sessions', 'Delete the selected session file? This cannot '
                                   'be undone.', parent=win):
                try:
                    os.remove(path)
                except OSError:
                    pass
                _fill()

        btns = tk.Frame(pad, bg=BG)
        btns.pack(fill='x', pady=(10, 0))
        self._btn(btns, 'Save current…', _save, kind='accent', height=30, width=130).pack(side='left')
        self._btn(btns, 'Load selected', _load, kind='soft', height=30, width=120).pack(side='left', padx=(8, 0))
        self._btn(btns, 'Details', _details, kind='soft', height=30, width=90).pack(side='left', padx=(8, 0))
        self._btn(btns, 'Delete', _delete, kind='danger', height=30, width=90).pack(side='left', padx=(8, 0))
        self._btn(btns, 'Close', win.destroy, kind='soft', height=30, width=80).pack(side='right')

    def _sessions_rebuild(self):
        """After a restore the window shows a DIFFERENT workspace: reload settings from disk
        and rebuild the UI the same way a theme switch does."""
        self.cfg = dict(DEFAULTS)
        self._load()
        self.cfg['theme'] = _apply_palette(self.cfg.get('theme') or _system_theme())
        self._lb_mode = self.cfg.get('lb_mode') or 'all'
        self._tip_hide()
        if self._pf_resize_after:
            self.root.after_cancel(self._pf_resize_after)
            self._pf_resize_after = None
        self._style()
        self._shell.destroy()
        self._lib_cache = {'mtime': None, 'all': [], 'families': [], 'computing': False,
                           'dirty': False, 'ts': 0.0, 'computed': False, 'select': None}
        self._pf_doc = None
        self._pf_last_w = 0
        self._treesig = None
        self._sig_shown = None
        self._fav_ids = None                             # the restored session brought its OWN
        self._fwd_migrated = False                       # re-run the one-shot track cleanup: the
        #                                                  restored settings may point elsewhere
        self._build()                                    # stars — re-read, don't paint the last
        #                                                  workspace's ★ onto this one's rows
        self._set_running(bool(self.proc and self.proc.poll() is None))
        self._render_signal_rows()
        self._start_lb_compute(force=True)               # show the restored library NOW,
        self._poll_once_soon()                           # not on the node's next status

    # ---------- licence gate (first run) ----------
    def _eula_gate(self):
        """Show the EULA once and require acceptance. Secondary to the Windows installer's own
        accept page — this covers the AppImage/mac paths, which have no installer. Declining
        quits the app. Re-prompts only if the accepted version string changes."""
        import buildinfo
        ver = buildinfo.build_info().get('version', '')
        if self.cfg.get('eula_accepted') == ver:
            return
        if getattr(self, '_splash_on', False):           # let the intro finish first
            self.root.after(300, self._eula_gate)
            return
        try:
            text = open(apppaths.license_file(), encoding='utf-8').read()
        except OSError:
            text = ('The AlphaNode End User License Agreement applies to your use of this '
                    'software. A copy is available from support@alphanode.tech. By clicking '
                    '"I Agree" you accept its terms.')
        win = ctk.CTkToplevel(self.root)
        win.title('AlphaNode — License Agreement')
        win.geometry('720x560')
        win.transient(self.root)
        win.protocol('WM_DELETE_WINDOW', lambda: None)   # a choice is required
        self.root.after(200, win.grab_set)               # ctk toplevels need a beat before grab
        ctk.CTkLabel(win, text='Please read and accept the License Agreement to continue'
                     ).pack(padx=16, pady=(14, 6), anchor='w')
        box = ctk.CTkTextbox(win, wrap='word')
        box.pack(fill='both', expand=True, padx=16, pady=(0, 10))
        box.insert('1.0', text)
        box.configure(state='disabled')
        self._text_selectable(box)
        row = ctk.CTkFrame(win, fg_color='transparent')
        row.pack(fill='x', padx=16, pady=(0, 14))

        def accept():
            self.cfg['eula_accepted'] = ver
            self._save()
            win.grab_release()
            win.destroy()

        def decline():
            win.grab_release()
            win.destroy()
            self.root.after(0, self.root.destroy)

        ctk.CTkButton(row, text='Decline & Quit', fg_color='transparent', border_width=1,
                      width=140, command=decline).pack(side='left')
        ctk.CTkButton(row, text='I Agree', width=140, command=accept).pack(side='right')

    # ---------- start/stop ----------
    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        if self._activating:                             # activation is rewriting library files —
            messagebox.showwarning(                      # a miner started now would have its
                'AlphaNode', 'Activation is unlocking your library right now.\n'
                             'Wait for it to finish, then start the node.')   # appends clobbered
            return
        if self._starting:                               # a Start hub-check is already in flight
            return
        self._save()
        probs = self._seg_check()                        # bad dates never announce themselves:
        if probs:                                        # a typo kills the node inside
            messagebox.showerror(                        # load_config with no window to say so,
                'Date segments',                         # and a reversed pair mines an EMPTY
                '\n\n'.join(m for _f, m in probs),       # slice while reporting real-looking
                parent=self.root)                        # Sharpes for it
            return
        diffs, snap = self._session_conflict()           # the library's parameters are frozen:
        if diffs:                                        # changing them mid-session would mix
            self._session_conflict_dialog(diffs, snap)   # incomparable scores into one file
            return
        os.makedirs(STATE_DIR, exist_ok=True)
        need, why = self._data_gap()
        if need:                                         # missing/stale/short snapshot: fetch the
            sig = repr((sorted(need), self._tf()))       # basket, then START resumes through the
            if getattr(self, '_fetch_tried', '') == sig:   # reconcile step below. The guard arms
                messagebox.showerror(                    # only THERE — after a download that
                    'Market data',                       # succeeded yet closed nothing — so a
                    f'Still {why} even after a successful download.\n\n'   # failed/offline fetch
                    'Check the pair names in Settings — they must be Binance USDT-perpetual '
                    'tickers (e.g. BTCUSDT, SOLUSDT).', parent=self.root)    # retries freely
                self._fetch_tried = ''
                return
            self._auto_fetch(need, why, on_success=self._start_after_fetch)
            return
        self._fetch_tried = ''                           # gap closed — the guard resets
        # the vault is always on: seal every mined formula to the vendor's key. No user toggle —
        # the subscription decides what the customer can unlock, not whether the library is sealed.
        vault_pub = _vault_pub_path()
        if apppaths.FROZEN and not vault_pub:            # a shipped build must NEVER mine open
            messagebox.showerror(
                'AlphaNode', 'The sealing key is missing from this installation, so mining '
                'cannot start.\nPlease reinstall AlphaNode.', parent=self.root)
            return
        # …unless this node is ACTIVATED: a live subscription means the customer sees everything
        # anyway, so an activated node mines in the open (checked against the hub at every Start —
        # instant revocation). Hub down or subscription lapsed -> fail CLOSED, seal as before;
        # rows mined sealed unlock later via Activate. The check runs OFF the Tk thread: urlopen's
        # timeout doesn't bound DNS resolution, and a Start click must never freeze the GUI.
        if vault_pub and self.cfg.get('vault_license'):
            self._starting = True
            res = {}                                     # worker -> Tk handoff: ONE key, polled

            def check():                                 # (after() itself is NOT thread-safe, so
                ok = False                               # the worker never touches Tk)
                try:
                    act = self._hub_request('/activate', {'token': self.cfg['vault_license'],
                                                          'device_id': self._device_id()},
                                            timeout=3)
                    ok = bool(act.get('ok'))
                except RuntimeError:
                    pass                                 # hub unreachable -> fail closed: seal
                res['open'] = ok

            def tick():
                if 'open' in res:
                    self._start_node(vault_pub, open_ok=res['open'])
                else:
                    self.root.after(120, tick)
            threading.Thread(target=check, daemon=True).start()
            self.root.after(120, tick)
            return
        self._start_node(vault_pub)

    def _start_node(self, vault_pub, open_ok=False):
        """The second half of Start — runs on the Tk thread after the (possibly async) vault
        decision. vault_pub: the key to seal to ('' only in an unsealed DEV build). open_ok:
        the hub confirmed an active subscription — ask the node to mine in the open; the node
        RE-VERIFIES that with the hub itself, so plaintext mining is a server-side decision,
        never a local env knob."""
        self._starting = False
        if self.proc and self.proc.poll() is None:       # re-check: the world may have moved
            return                                       # while the hub check was in flight
        if self._activating:
            return                                       # activation began meanwhile: no miner
        c = self.cfg
        env = dict(os.environ)
        if vault_pub:
            env['ALPHANODE_VAULT_PUB'] = vault_pub
        else:
            env.pop('ALPHANODE_VAULT_PUB', None)         # unsealed DEV build (frozen refused above)
        if open_ok and self.cfg.get('vault_license'):
            env['ALPHANODE_VAULT_OPEN'] = '1'
            env['ALPHANODE_VAULT_LICENSE'] = str(self.cfg['vault_license'])
        else:
            env.pop('ALPHANODE_VAULT_OPEN', None)
            env.pop('ALPHANODE_VAULT_LICENSE', None)
        env.update(
            ALPHANODE_CPU_PERCENT=str(c['cpu']),
            ALPHANODE_UNIVERSE=c['universe_list'],
            ALPHANODE_POP=str(c['pop']), ALPHANODE_GENS=str(c['gens']),
            ALPHANODE_SEED=(str(c['seed']) if c['seed'] else 'auto'),   # 0 -> per-install node-ID seed
            ALPHANODE_PAUSE=str(c['pause']),
            ALPHANODE_STATE_DIR=STATE_DIR, ALPHANODE_STATUS_PORT=str(c['port']),
            ALPHANODE_TF=self._tf(),
            ALPHANODE_DATA=self._data_file(),      # per-timeframe snapshot (fresh/bundled)
            ALPHANODE_CONFIG_INI=apppaths.config_ini(),
            ALPHANODE_EXPLORE_EVERY=str(c['explore_every']),
            ALPHANODE_SEED_FROM_LIBRARY=('1' if c['seed_from_lib'] else '0'),
            ALPHANODE_MAX_ROUNDS=str(c['max_rounds']),
            ALPHANODE_LEADERBOARD=str(c['leaderboard']),
            ALPHANODE_TARGET_VOL=str(c['target_vol']), ALPHANODE_EXEC_COST=str(c['exec_cost']),
            ALPHANODE_MAX_DEPTH=str(c['max_depth']), ALPHANODE_MAX_SIZE=str(c['max_size']),
            ALPHANODE_TOURNAMENT=str(c['tournament']), ALPHANODE_ELITISM=str(c['elitism']),
            ALPHANODE_RANDOM_INJECT=str(c['random_inject']),
            ALPHANODE_CROSSOVER_PROB=str(c['crossover_prob']),
            ALPHANODE_PARSIMONY=str(c['parsimony']),
            ALPHANODE_CORR_THRESHOLD=str(c['corr_threshold']),
            ALPHANODE_CORR_PENALTY=str(c['corr_penalty']),
            ALPHANODE_HOF_CAPACITY=str(c['hof_capacity']),
            ALPHANODE_FIT_BLOCKS=str(c['fit_blocks']),
            ALPHANODE_FIT_METRIC=('winrate' if c.get('opt_winrate') else 'sharpe'),
            ALPHANODE_TRAIN_START=c['train_start'], ALPHANODE_VAL_START=c['val_start'],
            ALPHANODE_TEST_START=c['test_start'], ALPHANODE_TEST_END=c['test_end'],
        )
        self._contract_seal()                            # this session's frozen parameters
        self.proc = subprocess.Popen(_child_cmd('node'), env=env,
                                     cwd=(apppaths.USER_DIR if apppaths.FROZEN else PROJ),
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding='utf-8', errors='replace')
        threading.Thread(target=self._reader, daemon=True).start()
        self._set_running(True)

    def _reader(self):
        for line in self.proc.stdout:
            self.logq.put(line.rstrip())

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGINT)     # the node gently finishes the round and exits
            except Exception:
                self.proc.terminate()
        w = getattr(self, 'btn_stop', None)               # simple mode has no Stop button
        if w is not None and w.winfo_exists():
            w.configure(state='disabled')

    def _on_close(self):
        try:
            if self._sigs:                               # they outlive the GUI unless asked to stop
                if messagebox.askyesno(
                        'Signal API', f'{len(self._sigs)} signal API service(s) are still running.\n\n'
                        'Stop them and free their ports?\n\n'
                        'No — leave them serving; AlphaNode picks them up again on the next start.',
                        parent=self.root):
                    self._stop_all_signals()
                else:
                    self._sig_save()
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
            if self._metrics_proc and self._metrics_proc.poll() is None:
                self._metrics_proc.terminate()           # a batch can outlive the window otherwise
        finally:
            self.root.destroy()

    # ---------- local signal API (serve live positions of a formula / portfolio) ----------
    # Several services run side by side: each takes the next free port from SIGNAL_PORT upward and
    # gets a row in the SIGNAL API card on the main screen (URL + log path + "free the port").
    # A service is a detached process: it survives the GUI, so the registry is persisted to
    # signals.json and re-adopted (verified over /health) on the next start.
    def _build_signals_card(self, right):
        card = self._card(right)
        self.sig_card = card
        card.grid(row=1, column=0, sticky='ew', pady=(16, 0))
        pad = self._pad(card)
        hs = self._box(pad)
        hs.pack(fill='x')
        self.lbl_sig_head = self._head(hs, 'SIGNAL API — running services')
        self.lbl_sig_head.pack(side='left')
        self.btn_sig_all = self._btn(hs, '✕ Free all ports', self._stop_all_signals_ui,
                                     kind='danger', height=26, width=118)
        self.btn_sig_all.pack(side='right')
        self._tip(self.btn_sig_all, 'Stop every running signal API and release its port.')
        self._sig_rows = self._box(pad)
        self._sig_rows.pack(fill='x', pady=(8, 0))
        self._wire_card_drag('signals', pad, hs)
        card.grid_remove()                               # shown only while something is being served

    @staticmethod
    def _pid_alive(pid):
        """Is this PID still running? (os.kill(pid, 0) is an existence check on POSIX — but on
        Windows os.kill TERMINATES on any signal, so ask the kernel there instead.)"""
        if not pid:
            return False
        try:
            if os.name == 'nt':
                import ctypes
                h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFO
                if not h:
                    return False
                code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                ctypes.windll.kernel32.CloseHandle(h)
                return bool(ok) and code.value == 259                              # STILL_ACTIVE
            os.kill(int(pid), 0)
            return True
        except Exception:                                # noqa: BLE001
            return False

    @staticmethod
    def _kill_pid(pid):
        try:
            if os.name == 'nt':                          # see _pid_alive: no signals on Windows
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                               capture_output=True, creationflags=0x08000000)      # CREATE_NO_WINDOW
            else:
                os.kill(int(pid), signal.SIGTERM)
        except Exception:                                # noqa: BLE001
            pass

    @staticmethod
    def _pid_on_port(port):
        """Best effort: who listens on `port`. Last resort for freeing a port held by a service we
        have neither a process handle nor a PID for (e.g. started by an older build)."""
        try:
            if os.name == 'nt':
                out = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True,
                                     text=True, creationflags=0x08000000).stdout
                for ln in out.splitlines():
                    f = ln.split()
                    if len(f) >= 5 and f[3].upper() == 'LISTENING' and f[1].endswith(f':{port}'):
                        return int(f[-1])
            else:
                out = subprocess.run(['lsof', '-ti', f'tcp:{port}', '-sTCP:LISTEN'],
                                     capture_output=True, text=True).stdout.split()
                if out:
                    return int(out[0])
        except Exception:                                # noqa: BLE001
            pass
        return None

    def _sig_save(self):
        """Persist the registry so a restarted (or crashed) GUI can find these services again."""
        keys = ('port', 'pid', 'label', 'log', 'n_formulas', 'n_tickers', 'started', 'fsig')
        try:
            json.dump([{k: s.get(k) for k in keys} for s in self._sigs],
                      open(SIGNALS_JSON, 'w', encoding='utf-8'), indent=1)
        except Exception:                                # noqa: BLE001
            pass

    def _sig_probe(self, port, timeout=0.6):
        """/health of `port` — the dict if an AlphaNode signal API answers there, else None."""
        try:
            import urllib.request
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=timeout) as r:
                h = json.load(r)
            return h if isinstance(h, dict) and 'name' in h and 'computing' in h else None
        except Exception:                                # noqa: BLE001
            return None

    def _sig_restore(self):
        """Background: find signal APIs that outlived the GUI. Scans the port range rather than
        trusting signals.json alone — that way a service survives even a lost registry; the file
        only restores the nice-to-haves (label, log path). Hands the result to the main thread."""
        meta = {}
        try:
            for e in json.load(open(SIGNALS_JSON, encoding='utf-8')):
                meta[int(e.get('port') or 0)] = e
        except Exception:                                # noqa: BLE001
            pass
        found = []
        # scan a bit past the cap: SIG_MAX services normally sit on 8799..8808, but another
        # app squatting on one of those pushes ours further up — a detached service past the
        # window would keep serving invisibly, un-adoptable and un-stoppable from the GUI
        for p in range(SIGNAL_PORT, SIGNAL_PORT + 2 * self.SIG_MAX):
            h = self._sig_probe(p)
            if not h:
                continue
            m = meta.get(p, {})
            log = m.get('log')
            if not log or not os.path.exists(log):
                cand = os.path.join(STATE_DIR, f'signal_{p}.log')
                log = cand if os.path.exists(cand) else None
            # a service from an older build has no pid in /health — ask the OS who holds the port
            pid = h.get('pid') or m.get('pid') or self._pid_on_port(p)
            found.append({'port': p, 'pid': pid, 'proc': None, 'fh': None,
                          'label': h.get('name') or m.get('label') or f'signal_{p}', 'log': log,
                          'n_formulas': int(h.get('n_formulas') or m.get('n_formulas') or 0),
                          'n_tickers': int(h.get('n_tickers') or m.get('n_tickers') or 0),
                          'started': m.get('started'), 'adopted': True})
        self._sig_pending = found or None                 # no Tk from a worker — _sig_tick picks it up

    def _sig_adopt(self, found):
        """Main thread: add the re-found services to the registry (the tick draws them)."""
        have = {s['port'] for s in self._sigs}
        for e in found:
            if e['port'] in have:
                continue
            self._sigs.append(e)
            self._sig_health[e['port']] = 'reconnecting…'
        self._sig_save()

    def _free_signal_port(self, tries=50):
        """First free TCP port at/after SIGNAL_PORT that we aren't already using."""
        import socket
        busy = {s['port'] for s in self._sigs}
        for p in range(SIGNAL_PORT, SIGNAL_PORT + tries):
            if p in busy:
                continue
            with socket.socket() as sk:
                # same option the service's HTTP server binds with (allow_reuse_address) — without it
                # a port we just freed reads as busy while old connections linger in TIME_WAIT
                sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sk.bind(('127.0.0.1', p))
                    return p
                except OSError:                           # taken by someone else — try the next
                    continue
        return None

    def _universe_tickers(self):
        """The configured basket, parsed. Empty/garbage -> None (callers warn)."""
        return _parse_universe(self.cfg.get('universe_list', '')) or None

    def _snapshot_tickers(self):
        """Tickers inside the ACTIVE timeframe's snapshot; None if unreadable. Deliberately
        NO 1d fallback: migrating a 1h user off the 50-pair daily file would hand them a
        50-pair intraday universe and a monster first download — better to keep the saved
        list (or the default five) than to invent a basket from the wrong timeframe."""
        try:
            return list(pickle.load(open(self._data_file(), 'rb'))[0])
        except Exception:                                 # noqa: BLE001  missing/odd -> None
            return None

    def _serve_signal(self, formulas, label, *, tickers=None, tf=None, vol=None,
                      exec_cost=None, start=None, quiet=False):
        """Start a signal API for `formulas`. Without the keyword args the service follows the
        panel — the active timeframe's basket, the configured vol/fee, config.ini's start —
        which is what the leaderboard and the built portfolio want. A FROZEN strategy (a
        forward-track entry) passes its own universe/tf/vol/fee/warm-up start instead, so the
        served numbers are the bot's numbers, not whatever Settings says today.

        quiet=True (the simple screen's autopilot): failures come back as a short string
        instead of a modal dialog — nothing may pop over an unattended screen. Returns
        None on success (and on the show-existing no-op) either way."""
        # tf-aware since the intraday branch landed in signal_service (fastsim math,
        # same numbers the forward track trades) — no daily-only gate here anymore
        formulas = [f for f in (formulas or []) if f and f.strip()]
        if not formulas:
            return
        tf = _tf_clean(tf) if tf else self._tf()
        tickers = list(tickers) if tickers else self._universe_tickers()
        if not tickers:
            if quiet:
                return f'no pairs for {tf} — download market data first'
            messagebox.showwarning('Signal API', 'The pairs universe is empty — download data '
                                   f'for the {tf} timeframe first.', parent=self.root)
            return
        if any(s['label'] == label for s in self._sigs):   # already serving this one — just show it
            self._render_signal_rows()
            return
        alive = sum(1 for s in self._sigs if self._sig_alive(s))
        if alive >= self.SIG_MAX:                        # each service is a whole engine process
            if quiet:                                    # re-simulating on every refresh — ten of
                return f'{alive} services running — {self.SIG_MAX} is the limit'
            messagebox.showwarning(
                'Signal API',
                f'{alive} signal services are already running — {self.SIG_MAX} is the limit.\n'
                'Free a port first (✕ Free port on a row you no longer need), '
                'then serve this one.', parent=self.root)
            return
        port = self._free_signal_port()
        if port is None:
            if quiet:
                return 'no free port available'
            messagebox.showerror('Signal API', 'No free port available.', parent=self.root)
            return
        env = dict(os.environ)
        env.update(ALPHANODE_STATE_DIR=STATE_DIR, ALPHANODE_DATA=self._data_file_for(tf),
                   ALPHANODE_TF=tf,
                   ALPHANODE_CONFIG_INI=apppaths.config_ini(),
                   # served numbers must match the leaderboard/forward: same vol & fee as here
                   ALPHANODE_TARGET_VOL=str(self.cfg['target_vol'] if vol is None else vol),
                   ALPHANODE_EXEC_COST=str(self.cfg['exec_cost'] if exec_cost is None
                                           else exec_cost),
                   ALPHANODE_SIGNAL_FORMULAS=json.dumps(formulas), ALPHANODE_SIGNAL_NAME=label,
                   ALPHANODE_SIGNAL_TICKERS=','.join(tickers),
                   ALPHANODE_SIGNAL_PORT=str(port),
                   ALPHANODE_SIGNAL_REFRESH=('300' if tf in ('15m', '1h') else '900'))
        if start:                                        # a frozen entry's warm-up start; the
            env['ALPHANODE_SIGNAL_START'] = str(start)[:10]   # panel callers keep config.ini's
        log_path = os.path.join(STATE_DIR, f'signal_{port}.log')
        try:
            fh = open(log_path, 'w', buffering=1, encoding='utf-8')
            proc = subprocess.Popen(                       # each service logs to its own file
                _child_cmd('signal'), env=env,
                cwd=(apppaths.USER_DIR if apppaths.FROZEN else PROJ),
                stdout=fh, stderr=subprocess.STDOUT)
        except Exception as e:                            # noqa: BLE001
            if quiet:
                return f'could not start the signal service: {e}'
            messagebox.showerror('Signal API', f'Could not start the signal service: {e}', parent=self.root)
            return
        self._sigs.append({'port': port, 'proc': proc, 'pid': proc.pid, 'fh': fh, 'log': log_path,
                           'label': label, 'n_formulas': len(formulas), 'n_tickers': len(tickers),
                           'started': time.strftime('%Y-%m-%d %H:%M')})
        self._sig_health[port] = 'starting — fetching live data, computing the first signal…'
        self._sig_save()
        self._render_signal_rows()

    def _pf_serve_signal(self):
        doc = self._pf_doc
        formulas = (doc or {}).get('formulas_full')
        if not formulas:
            messagebox.showinfo('Signal API', 'Build the portfolio first.', parent=self.root)
            return
        self._serve_signal(list(formulas), f'portfolio_top{doc.get("n", len(formulas))}')

    def _stop_signal(self, entry):
        """Stop ONE service, free its port, drop it from the registry. Works both for services we
        started (process handle) and for ones adopted from an earlier session (PID only)."""
        try:
            proc = entry.get('proc')
            if proc is not None and proc.poll() is None:
                proc.terminate()
            elif proc is None:                            # adopted: no handle, kill by PID
                pid = entry.get('pid') or self._pid_on_port(entry['port'])
                if pid:
                    self._kill_pid(pid)
        except Exception:                                 # noqa: BLE001
            pass
        try:
            if entry.get('fh'):
                entry['fh'].close()
        except Exception:                                 # noqa: BLE001
            pass
        # drop the book too: the next service on this port serves a DIFFERENT formula, and
        # its stale positions must not sit under it while the new one computes its first signal
        for d in (self._sig_health, self._sig_signal, self._sig_seen, self._sig_drawn):
            d.pop(entry['port'], None)
        if entry in self._sigs:
            self._sigs.remove(entry)
        self._sig_save()

    def _stop_all_signals(self):
        for entry in list(self._sigs):
            self._stop_signal(entry)

    def _stop_signal_ui(self, entry):
        self._stop_signal(entry)
        self._render_signal_rows()

    def _stop_all_signals_ui(self):
        if not self._sigs or messagebox.askyesno(
                'Signal API', f'Stop all {len(self._sigs)} service(s) and free their ports?',
                parent=self.root):
            self._stop_all_signals()
            self._render_signal_rows()

    def _sig_alive(self, s):
        """Is this service's process still up? Spawned ones answer via poll(); adopted ones
        (no Popen handle) via the recorded pid — and an adopted row with no pid at all is
        presumed alive, letting /health speak."""
        proc, pid = s.get('proc'), s.get('pid')
        if proc is not None:
            return proc.poll() is None
        return not (bool(pid) and not self._pid_alive(pid))

    def _sig_tick(self):
        """Every 3s: refresh the health of each service. Main thread — the HTTP call itself is
        handed to a worker (see _sig_poll_worker)."""
        pending, self._sig_pending = self._sig_pending, None
        if pending:
            self._sig_adopt(pending)
        for s in list(self._sigs):
            if not self._sig_alive(s):
                self._sig_health[s['port']] = '○ stopped (the process exited) — port is free'
            else:
                threading.Thread(target=self._sig_poll_worker, args=(s['port'],), daemon=True).start()
        ports = tuple(s['port'] for s in self._sigs)
        if ports != self._sig_shown:                      # the set changed -> rebuild the rows
            self._render_signal_rows()
        else:                                             # same set -> only refresh the status text
            for p, lbl in list(self._sig_status_lbl.items()):
                if lbl.winfo_exists():
                    txt = self._sig_health.get(p, 'starting…')
                    lbl.configure(text=txt, fg=self._sig_status_color(txt))
        for p, cv in list(self._sig_canvas.items()):      # a fresh signal -> repaint its book
            if cv.winfo_exists() and self._sig_drawn.get(p) != (
                    self._sig_signal.get(p) or {}).get('updated_at'):
                self._draw_signal_positions(p)
        self.root.after(3000, self._sig_tick)

    def _render_signal_rows(self):
        """Rebuild the service list on the main screen. Main thread only."""
        holder = getattr(self, '_sig_rows', None)
        if holder is None or not holder.winfo_exists():
            return
        for w in holder.winfo_children():
            w.destroy()
        self._sig_status_lbl = {}
        self._sig_canvas = {}
        self._sig_shown = tuple(s['port'] for s in self._sigs)
        if not self._sigs:
            self.sig_card.grid_remove()                   # nothing served -> the card gets out of the way
            return
        self.sig_card.grid()
        n = len(self._sigs)
        self.lbl_sig_head.configure(text=f'SIGNAL API — {n} running service{"s" if n > 1 else ""}  ·  '
                                         f'live JSON on localhost  ·  refresh 15 min')
        for s in self._sigs:
            url = f'http://127.0.0.1:{s["port"]}/signal'
            row = self._box(holder)
            row.pack(fill='x', pady=(0, 8))
            btns = self._box(row)
            btns.pack(side='right', anchor='n')
            self._btn(btns, 'Copy URL', lambda u=url: (self.root.clipboard_clear(),
                                                       self.root.clipboard_append(u)),
                      height=26, width=76).pack(side='left')
            log_btn = self._btn(btns, 'Log', lambda p=s.get('log'): self._open_folder(p),
                                height=26, width=44)
            log_btn.configure(state=('normal' if s.get('log') else 'disabled'))
            log_btn.pack(side='left', padx=(4, 0))
            self._btn(btns, '✕ Free port', lambda e=s: self._stop_signal_ui(e), kind='danger',
                      height=26, width=92).pack(side='left', padx=(4, 0))
            info = self._box(row)
            info.pack(side='left', fill='x', expand=True)
            head = (f'● {s["port"]}  ·  {s["label"]}  ·  {s["n_formulas"]} alpha(s)  ·  '
                    f'{s["n_tickers"]} pairs')
            if s.get('started'):
                head += f'  ·  since {s["started"]}'
            if s.get('adopted'):
                head += '  ·  from an earlier session'
            self._lbl(info, text=head, text_color=TXT, font=(self.UI, 13, 'bold'),
                         wraplength=620, justify='left', anchor='w').pack(anchor='w')
            self._lbl(info, text=url + (f'   ·   {s["log"]}' if s.get('log') else ''),
                         text_color=FAINT, font=(self.MONO, 11), wraplength=620,
                         justify='left', anchor='w').pack(anchor='w')
            htxt = self._sig_health.get(s['port'], 'starting…')
            lbl = self._lbl(info, text=htxt, text_color=self._sig_status_color(htxt),
                               font=(self.UI, 11), wraplength=620, justify='left', anchor='w')
            lbl.pack(anchor='w')
            self._sig_status_lbl[s['port']] = lbl
            cv = tk.Canvas(info, height=1, bg=CARD, highlightthickness=0, bd=0)
            cv.pack(fill='x', pady=(6, 0))
            cv.bind('<Configure>', lambda _e, pt=s['port']: self._draw_signal_positions(pt))
            self._sig_canvas[s['port']] = cv
            self._draw_signal_positions(s['port'])

    SIG_POS_MAX = 16                                  # pills drawn; the rest is one summary line
    SIG_TINT = (0.90, 0.66)                           # pill fill: lightest weight -> heaviest

    @staticmethod
    def _capsule(cv, x0, x1, ymid, h, fill):
        """A fully rounded bar/pill. Tk's canvas has no rounded rectangle, so it is a rect
        between two circles — the caps are what separate a designed shape from a hairline."""
        r = h / 2.0
        if x1 - x0 <= h:                              # too short for a body: just the cap
            cv.create_oval(x0, ymid - r, x0 + h, ymid + r, fill=fill, outline='')
            return
        cv.create_rectangle(x0 + r, ymid - r, x1 - r, ymid + r, fill=fill, outline='')
        cv.create_oval(x0, ymid - r, x0 + h, ymid + r, fill=fill, outline='')
        cv.create_oval(x1 - h, ymid - r, x1, ymid + r, fill=fill, outline='')

    def _draw_signal_positions(self, port):
        """Paint the served signal under its service row as a row of pills — ticker and weight
        together inside one rounded shape, the fill tinted deeper the heavier the position.

        No bars: a bar stretched to the card's width told you nothing, because the card is far
        wider than five numbers need and every bar came out nearly full. A pill is sized by its
        own text, so five pairs take a fifth of the row and twenty wrap onto three lines.

        Direction is the fill (indigo long, amber short) plus the sign, NOT green/red: a
        position is a direction, not a profit, and cool/warm survives a red-green colourblind
        eye. A canvas, not a stack of widgets — the tick repaints it in place, without churn."""
        cv = self._sig_canvas.get(port)
        if cv is None or not cv.winfo_exists():
            return
        sig = self._sig_signal.get(port) or {}
        pos = sig.get('positions') or []
        cv.delete('all')
        if not pos:                                       # nothing served yet -> take no space
            if int(cv['height']) != 1:
                cv.configure(height=1)
            return
        S = self.SCALE
        shown = pos[:self.SIG_POS_MAX]
        hidden = len(pos) - len(shown)
        pillh, gap, vgap, headh = 28 * S, 9 * S, 8 * S, 30 * S
        f_tick = self._font(self.MONO, 10)
        f_pct = self._font(self.MONO, 10, 'bold')
        f_side = self._font(self.UI, 11, 'bold')
        f_meta = self._font(self.UI, 11)

        w = cv.winfo_width()
        # ---- lay the pills out first: the height depends on how many lines they wrap onto ----
        peak = max(abs(p['weight']) for p in pos) or 1.0
        lo, hi = self.SIG_TINT
        items = []
        for p in shown:
            pct = p.get('weight_pct') or f"{p['weight'] * 100:+.1f}%"
            tw, pw = f_tick.measure(p['ticker']), f_pct.measure(pct)
            items.append((p, pct, min(tw + pw + 32 * S, max(pillh, w))))
        lines, cur, used = [], [], 0.0
        for it in items:
            if cur and used + it[2] > w:                  # w<=1 -> one pill per line; harmless,
                lines.append(cur); cur, used = [], 0.0    # the real layout comes on <Configure>
            cur.append(it)
            used += it[2] + gap
        if cur:
            lines.append(cur)
        h = int(headh + len(lines) * pillh + (len(lines) - 1) * vgap
                + (20 * S if hidden else 0) + 2 * S)
        if int(cv['height']) != h:                        # only on a real change: configuring the
            cv.configure(height=h)                        # height re-fires <Configure> -> a loop
        if w <= 1:                                        # not laid out yet; the bind calls back
            return

        # ---- header: the two sides first, the metadata after ----
        gross = sum(abs(p['weight']) for p in pos)
        lw = sum(p['weight'] for p in pos if p['weight'] > 0)
        sw = -sum(p['weight'] for p in pos if p['weight'] < 0)
        x = 0.0
        for label, val, col in (('LONG', lw, LONGC), ('SHORT', sw, SHORTC)):
            d = 7 * S
            cv.create_oval(x, 9 * S - d / 2, x + d, 9 * S + d / 2, fill=col, outline='')
            t = f'{label} {val * 100:.1f}%'
            cv.create_text(x + d + 5 * S, 9 * S, text=t, anchor='w', font=f_side, fill=col)
            x += d + 5 * S + f_side.measure(t) + 18 * S
        bits = [b for b in (str(sig.get('as_of') or ''), str(sig.get('tf') or ''),
                            (f'lev {sig["leverage"]:g}' if sig.get('leverage') is not None else ''),
                            f'gross {gross * 100:.1f}%',
                            f'net {(lw - sw) * 100:+.1f}%') if b]
        cv.create_text(x, 9 * S, text='  ·  '.join(bits), anchor='w', font=f_meta, fill=MUT)

        # ---- pills ----
        for li, line in enumerate(lines):
            x = 0.0
            ymid = headh + li * (pillh + vgap) + pillh / 2
            for p, pct, pw in line:
                col = LONGC if p['weight'] > 0 else SHORTC
                # deeper fill = heavier position, so the book's shape reads before any number
                t = lo - (lo - hi) * (abs(p['weight']) / peak)
                self._capsule(cv, x, x + pw, ymid, pillh, _mix(col, CARD, t))
                cv.create_text(x + pw - 12 * S, ymid, text=pct, anchor='e', font=f_pct, fill=col)
                room = pw - f_pct.measure(pct) - 32 * S
                cv.create_text(x + 12 * S, ymid, text=self._ellipsize(f_tick, p['ticker'], room),
                               anchor='w', font=f_tick, fill=TXT)
                x += pw + gap
        if hidden:
            cv.create_text(0, headh + len(lines) * (pillh + vgap) + 4 * S,
                           text=f'+{hidden} more — the full book is in the JSON', anchor='w',
                           font=self._font(self.UI, 10), fill=FAINT)
        self._sig_drawn[port] = sig.get('updated_at')

    @staticmethod
    def _ellipsize(font, text, room):
        """`text` trimmed with an ellipsis until it fits `room` px. Returns '' if nothing does."""
        if room <= 0:
            return ''
        if font.measure(text) <= room:
            return text
        while text and font.measure(text + '…') > room:
            text = text[:-1]
        return (text + '…') if text else ''

    @staticmethod
    def _sig_status_color(txt):
        """Colour of a service's status line: the answer to 'is my API up?' should be read
        from across the room. Green ONLY for '● serving' — a warning is amber-red, a stopped
        process fades out, and everything transitional stays neutral."""
        if txt.startswith('● serving'):
            return POS
        if txt.startswith('⚠'):
            return NEG
        if txt.startswith('○ stopped'):
            return FAINT
        return MUT

    def _sig_poll_worker(self, port):
        """Background: fetch /health for ONE service and stash the text. NO Tk here — Tk is not
        thread-safe; the main-thread tick renders from _sig_health."""
        try:
            import urllib.request
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3) as r:
                h = json.load(r)
            if h.get('ok'):
                age = h.get('age_secs')
                txt = (f'● serving · updated {h.get("updated_at", "")} ({age:.0f}s ago)'
                       if age is not None else '● serving')
                # /health is the cheap heartbeat; /signal carries the positions and only changes
                # once per refresh (15 min by default). Pull the heavy one on a new stamp only.
                stamp = h.get('updated_at')
                if stamp and self._sig_seen.get(port) != stamp:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/signal', timeout=5) as r:
                        sig = json.load(r)
                    if sig.get('ok'):
                        self._sig_signal[port] = sig
                        self._sig_seen[port] = stamp
            elif h.get('error'):
                txt = f'⚠ {h["error"]}'
            else:                                         # first compute: show the live progress
                txt = '○ ' + (h.get('progress') or 'computing the first signal…')   # battle-tested glyph:
                #  U+23F3 (hourglass) has emoji presentation and segfaults libXft 2.3.4 — the
                #  crash.log of 2026-09-02 died one X call after painting it
        except Exception:                                 # noqa: BLE001
            txt = 'starting…'
        self._sig_health[port] = txt

    # ---------- status polling ----------
    def _poll_once_soon(self):
        """Render freshly computed leaderboard rows without waiting for the 1.5s tick.
        NOT via _poll: that reschedules itself and would spawn extra poll loops."""
        for delay in (120, 400, 900):                    # compute is a background thread:
            self.root.after(delay, lambda: self._refresh_leaderboard([]))

    def _log_placeholder(self):
        self.logbox.configure(state='normal')
        self.logbox.delete('1.0', 'end')
        self.logbox.insert('end', 'live log — round starts, new champions and '
                                  'round summaries will appear here once the node runs.', 'i')
        self.logbox.configure(state='disabled')

    def _render_events(self, evs):
        """Rebuild the LIVE LOG feed (newest at the bottom, auto-scrolled, colored by kind)."""
        at_end = self.logbox.yview()[1] > 0.999          # don't yank the view if the user scrolled up
        self.logbox.configure(state='normal')
        self.logbox.delete('1.0', 'end')
        for e in evs[-80:]:
            txt = e.get('t', '')
            kind = e.get('k', 'i')
            if txt.startswith('▶') and self.logbox.index('end-1c') != '1.0':
                self.logbox.insert('end', '\n')          # a breath between rounds
            if kind == 'round' and txt.startswith('✓'):
                kind = 'roundsum'                        # the round verdict pops in bold
            self.logbox.insert('end', f"{e.get('ts', '')}  ", 'ts')
            self.logbox.insert('end', f"{txt}\n", kind)
        if at_end:
            self.logbox.see('end')
        self.logbox.configure(state='disabled')

    def _log_sel_busy(self):
        """True while the user is mid-selection in the live log (pointer still over it) —
        rebuilding the feed would yank the text out from under the mouse."""
        try:
            if not self.logbox.tag_ranges('sel'):
                return False
            px, py = self.root.winfo_pointerxy()
            x, y = self.logbox.winfo_rootx(), self.logbox.winfo_rooty()
            return (x <= px < x + self.logbox.winfo_width()
                    and y <= py < y + self.logbox.winfo_height())
        except Exception:                                # noqa: BLE001 — never stall the poll
            return False

    def _maybe_render_events(self, evs):
        if evs and evs != self._events_last and not self._log_sel_busy():
            self._events_last = evs
            self._render_events(evs)

    @staticmethod
    def _round_eta_left(st):
        """Seconds left in the round in flight, or None. The node stamps eta_s at eta_at
        (round_eta.py); the GUI polls status.json every 1.5 s, so the seconds between that stamp
        and now are subtracted here — the countdown runs smoothly instead of jumping once per
        generation."""
        eta, at = st.get('eta_s'), st.get('eta_at')
        if not isinstance(eta, (int, float)) or not isinstance(at, (int, float)):
            return None
        return max(0.0, eta - (time.time() - at))

    @staticmethod
    def _fmt_left(sec):
        """'~MM:SS' (or 'H:MM:SS'), rounded to 10 s — the estimate is not better than that."""
        left = (int(sec) + 5) // 10 * 10
        h, rem = divmod(left, 3600)
        m, s_ = divmod(rem, 60)
        return f'~{h}:{m:02d}:{s_:02d}' if h else f'~{m:02d}:{s_:02d}'

    def _show_round_card(self, show):
        if show and not self._round_shown:
            self.round_card.pack(fill='x', pady=(10, 0), before=self._logwrap)
        elif not show and self._round_shown:
            self.round_card.pack_forget()
        self._round_shown = show

    def _update_round_bar(self, st, live):
        """One progress bar per round. Fraction = elapsed / (elapsed + eta) — by TIME, so the
        bar keeps moving inside a generation. Before the model has a rate (gen 0..1) it falls
        back to finished generations / gens and says so."""
        t0, rnd = st.get('round_t0'), st.get('round_n')
        if not live or not isinstance(t0, (int, float)) or not rnd:
            self._show_round_card(False)
            return
        gens = int(st.get('gens') or 0)
        gen_i = int(st.get('gen_i') or 0)
        mode = 'refine' if str(st.get('mode', '')).startswith('refin') else 'explore'
        done = float(st.get('progress') or 0.0) >= 1.0
        elapsed = max(0.0, time.time() - t0)
        left = self._round_eta_left(st)
        if done:
            frac, right = 1.0, f'done · {int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}'
            color = POS
        elif left is None:
            frac = (gen_i / gens) if gens else 0.0
            right, color = 'estimating…', ACC
        else:
            frac = elapsed / max(1e-9, elapsed + left)
            right = f'{self._fmt_left(left)} left · {int(frac * 100):d}%'
            color = ACC
        gen_s = f' · gen {min(gen_i, gens)}/{gens}' if gens else ''
        self.lbl_round_l.configure(text=f'round {rnd} · {mode}{gen_s}')
        self.lbl_round_r.configure(text=right)
        self.round_bar.configure(progress_color=color)
        self.round_bar.set(max(0.0, min(1.0, frac)))
        self._show_round_card(True)

    # ---------- Simple mode: the autopilot screen ----------
    # A second, smaller face over the SAME machinery: the same start()/stop(), the same
    # portfolio builder, the same serve/track calls. Exactly one branch picks the shell (the
    # __init__ mount and _set_theme) — the previous Simple mode died as twenty scattered
    # if-simple checks (8e83b1a), so this one is deliberately a layer, not a mode.

    def _enter_advanced(self):
        """Build the full dashboard and start its poll loop exactly once. After-loops belong
        to root, not the shell: they survive theme rebuilds and mode switches (each loop
        early-outs while the other shell is up), so a second start would double them."""
        self._shell_mode = 'advanced'
        self._build()
        if not getattr(self, '_poll_on', False):
            self._poll_on = True
            self._poll()

    def _switch_mode(self, mode):
        if mode == getattr(self, '_shell_mode', 'advanced'):
            return
        self.cfg['ui_mode'] = mode
        self._save()                                     # the hidden pane's vars are live — keep them
        self._tip_hide()
        if self._pf_resize_after:
            self.root.after_cancel(self._pf_resize_after)
            self._pf_resize_after = None
        self._shell.destroy()
        if mode == 'simple':
            self._build_simple()
            return
        self._treesig = None                             # the dashboard rebuild, _set_theme-style
        self._sig_shown = None
        self._pf_last_w = 0
        self._enter_advanced()
        self._boot_arm()                                 # simple suppressed it; the dashboard wants it
        self._set_running(bool(self.proc and self.proc.poll() is None))
        self._render_signal_rows()
        if self._lib_cache.get('computed'):
            self._render_lb(self._lb_rows())
        elif self._shown:
            self._fill_tree(list(self._shown))
        if self._pf_doc:
            self._render_portfolio(self._pf_doc)
        self.root.after(900, self._fwd_refresh)

    def _build_simple(self):
        self._shell_mode = 'simple'
        self._sm_fetch_line = getattr(self, '_sm_fetch_line', '')
        self._shell = self._box(self.root, bg=BG)
        self._shell.pack(fill='both', expand=True)
        bar = tk.Canvas(self._shell, height=3, bg=ACC, highlightthickness=0)
        bar.pack(fill='x')

        def _grad(e, cv=bar):
            cv.delete('all')
            n = 64
            for i in range(n):
                cv.create_rectangle(e.width * i / n, 0, e.width * (i + 1) / n + 1, 4,
                                    fill=_mix(ACC, ACC_DN, i / (n - 1)), outline='')
        bar.bind('<Configure>', _grad)
        sm = self._sm = {}

        # The full settings pane, alive but never packed, and built FIRST: the header's own
        # timeframe box binds v_tf, and _collect()/start() dereference the rest of its ~30
        # tk vars unconditionally — they must all exist before anything visible mounts.
        ghost = tk.PanedWindow(self._shell, orient='horizontal', bg=BG, bd=0)
        self._paned = ghost
        self._build_settings(ghost)

        # ---- header: brand · serving pill · pair chips · bar size · [theme, Advanced, run] ----
        top = self._box(self._shell, bg=BG)
        top.pack(fill='x', padx=20, pady=(12, 8))
        brand = self._box(top, bg=BG)
        brand.pack(side='left')
        self._lbl(brand, text='● ', font=(self.UI, 16, 'bold'), text_color=ACC,
                  bg=BG).pack(side='left')
        self._lbl(brand, text='AlphaNode', font=(self.UI, 19, 'bold'), text_color=TXT,
                  bg=BG).pack(side='left')
        sm['brand'] = brand
        act = ctk.CTkFrame(top, fg_color=_mix(POS, BG, 0.86), corner_radius=999)
        self._lbl(act, text='● activated', text_color=POS, font=(self.MONO, 10),
                  bg=_mix(POS, BG, 0.86)).pack(padx=int(9 * self.SCALE), pady=int(2 * self.SCALE))
        sm['act_lbl'] = act                              # packed by _sm_lic_refresh, by the brand
        self._tip(act, 'This node is activated: the library is open, serving and exports\n'
                       'are available. If the subscription lapses, re-activate from Advanced.')
        srv = ctk.CTkFrame(top, fg_color=_mix(POS, BG, 0.86), corner_radius=999)
        sm['serve_pill'] = self._lbl(srv, text='', text_color=POS, font=(self.MONO, 10),
                                     bg=_mix(POS, BG, 0.86))
        sm['serve_pill'].pack(padx=int(9 * self.SCALE), pady=int(2 * self.SCALE))
        sm['serve_frame'] = srv                          # packed by the tick when a service lives
        self._build_theme_pick(top)
        # hug the widest strings the pill ever shows (a fixed 210 read as a slab) — the
        # same padding formula as _pill_paint's hug branch, frozen once so nothing jitters
        h_go = int(38 * self.SCALE)
        self._sm_go_w = int(h_go / 2 + (13 + 10 + 18) * self.SCALE
                            + max(self._font(self.UI, 12, 'bold').measure('Start node'),
                                  self._font(self.MONO, 9).measure('fetching market data')))
        go = tk.Canvas(top, width=self._sm_go_w, height=h_go,
                       bg=BG, highlightthickness=0, bd=0, cursor='hand2')
        go.pack(side='right', padx=(0, 14))
        go.bind('<Button-1>', lambda _e: self._simple_go())
        sm['go'] = go
        self._sm_paint_go('idle', None, 'Start node')
        self._tip(go, 'Downloads market data if needed, then searches round after round.\n'
                      'The portfolio below rebuilds after every finished round.\n'
                      'Runs until you press Stop.')
        b_adv = self._btn(top, '⚙  Advanced', lambda: self._switch_mode('advanced'),
                          kind='soft', height=34, width=112)
        b_adv.pack(side='right', padx=(0, 8))
        b_wipe = self._btn(top, 'Clear node', self._wipe_history, kind='danger',
                           height=34, width=100)
        b_wipe.pack(side='right', padx=(0, 8))
        self._tip(b_wipe, 'Delete ALL run history — found alphas, rounds, status, the built\n'
                          'portfolio (same as Advanced → Clear all history). Asks first;\n'
                          'settings, favorites and the forward track survive.')
        self._tip(b_adv, 'The full dashboard: leaderboard, portfolio, forward track, signal\n'
                         'APIs and every setting. Whatever is running keeps running.')
        # activation lives on THIS screen too (user spec): without a key the node mines sealed —
        # the cards show numbers instead of formulas, and nothing can be opened or served
        sm['act_btn'] = self._btn(top, 'Activate', self._open_activate, kind='accent',
                                  height=34, width=96)
        self._tip(sm['act_btn'], 'Paste your subscription key once: this machine claims a node\n'
                                 'seat, every sealed formula mined here is unlocked, and the\n'
                                 'Signal API, Forward track and Report PDF open up.')
        tf_box = ttk.Combobox(top, textvariable=self.v_tf, values=TF_CHOICES,
                              state='readonly', width=4)
        tf_box.pack(side='right', padx=(0, 12), pady=(4, 0))
        tf_box.bind('<<ComboboxSelected>>', self._on_tf_change)
        sm['tf_box'] = tf_box
        self._sm_lic_refresh()                           # packs Activate / 'activated' before it
        sm['hdr_pairs'] = self._box(top, bg=BG)
        sm['hdr_pairs'].pack(side='right', padx=(12, 10), pady=(3, 0))

        body = self._box(self._shell, bg=BG)
        body.pack(fill='both', expand=True, padx=20, pady=(4, 16))

        # ---- the pairs editor: a card summoned by the ✎ chip in the header ----
        sm['pairs_card'] = self._card(body)
        pp = self._pad(sm['pairs_card'])
        self._lbl(pp, text='★ Which pairs to trade', text_color=MUT,
                  font=(self.UI, 12, 'bold')).pack(anchor='w')
        self._uni_build(pp)                              # mounted AFTER the ghost pane: the chip
        #                                                  editor is a singleton, the last mount
        #                                                  is the live one
        self._sm_hdr_chips()
        self.v_unilist.trace_add('write', lambda *_: self._sm_hdr_chips())

        # ---- row: Search card | Champions card ----
        row = self._box(body, bg=BG)
        row.pack(fill='x')
        row.columnconfigure(0, weight=11, uniform='smrow')   # the mock's 1.1fr / 1fr — and
        row.columnconfigure(1, weight=10, uniform='smrow')   # 'uniform' makes the split RIGID:
        #                                                      a longer formula cannot widen its
        #                                                      card at the neighbour's expense
        search = self._card(row)
        search.grid(row=0, column=0, sticky='nsew')
        sp = self._pad(search)
        sh = self._box(sp)
        sh.pack(fill='x')
        sm['state'] = self._lbl(sh, text='Search — pick your pairs and press Start',
                                text_color=TXT, font=(self.UI, 14, 'bold'))
        sm['state'].pack(side='left')
        sm['sr_tri'] = self._lbl(sh, text='', text_color=FAINT, font=(self.UI, 12), anchor='e')
        sm['sr_tri'].pack(side='right')
        spark = tk.Canvas(sp, height=int(150 * self.SCALE), bg=CARD, highlightthickness=0, bd=0)
        spark.pack(fill='x', pady=(10, 4))
        sm['spark'] = spark
        self._sm_spark_sig = None
        spark.bind('<Configure>', lambda _e: setattr(self, '_sm_spark_sig', None))
        self._tip(spark, 'Best fitness round by round (solid) and the best held-out TEST\n'
                         'found so far (dashed) — the same number the by-test champions top shows.')
        leg = self._box(sp)
        leg.pack(fill='x', pady=(0, 6))
        self._lbl(leg, text='—', text_color=ACC, font=(self.UI, 11, 'bold')).pack(side='left')
        self._lbl(leg, text=' best fit', text_color=MUT, font=(self.UI, 11)).pack(side='left')
        self._lbl(leg, text='   – –', text_color=SHORTC, font=(self.UI, 11)).pack(side='left')
        self._lbl(leg, text=' best TEST', text_color=MUT,
                  font=(self.UI, 11)).pack(side='left')
        tiles = self._box(sp)
        tiles.pack(fill='x', pady=(2, 0))
        for key, label in (('t_fit', 'best fitness'), ('t_found', 'champions'),
                           ('t_gen', 'gen / round')):
            cell = ctk.CTkFrame(tiles, fg_color=HEAD_BG, corner_radius=10)
            cell.pack(side='left', padx=(0, 8), fill='x', expand=True)
            pad2 = self._box(cell, bg=HEAD_BG)
            pad2.pack(fill='x', padx=12, pady=8)
            self._lbl(pad2, text=label, text_color=FAINT, font=(self.UI, 11),
                      bg=HEAD_BG).pack(anchor='w')
            sm[key] = self._lbl(pad2, text='—', text_color=TXT, font=(self.UI, 19, 'bold'),
                                bg=HEAD_BG)
            sm[key].pack(anchor='w')
        sm['event'] = self._lbl(sp, text='', text_color=FAINT, font=(self.MONO, 10))
        sm['event'].pack(anchor='w', pady=(8, 0))

        champs = self._card(row)
        champs.grid(row=0, column=1, sticky='nsew', padx=(12, 0))
        cp = self._pad(champs)
        chh = self._box(cp)
        chh.pack(fill='x')
        self._lbl(chh, text='Champions', text_color=TXT,
                  font=(self.UI, 14, 'bold')).pack(side='left')
        self._sm_champ_by = getattr(self, '_sm_champ_by', 'fit')
        byl = self._lbl(chh, text=('by fitness' if self._sm_champ_by == 'fit' else 'by test'),
                        text_color=ACC, font=(self.UI, 12), anchor='e', cursor='hand2')
        byl.pack(side='right')
        byl.bind('<ButtonRelease-1>', lambda _e: self._sm_toggle_champ_sort())
        sm['champ_by'] = byl
        self._tip(byl, 'Click to rank the list by TEST Sharpe instead of fitness (and back).\n'
                       'TEST ranking shows who held up out-of-sample — remember the search\n'
                       'never optimized for it.')
        box = self._box(cp)
        box.pack(fill='both', expand=True, pady=(8, 0))
        hdr = self._box(box)
        hdr.pack(fill='x')
        self._lbl(hdr, text='#', text_color=FAINT, font=(self.UI, 10), width=3).pack(side='left')
        self._lbl(hdr, text='test', text_color=FAINT, font=(self.UI, 10), width=6,
                  anchor='e').pack(side='right')
        self._lbl(hdr, text='fit', text_color=FAINT, font=(self.UI, 10), width=7,
                  anchor='e').pack(side='right')
        self._lbl(hdr, text='formula', text_color=FAINT, font=(self.UI, 10)).pack(side='left')
        # a FIXED bank of row slots, updated in place: destroying and rebuilding rows made the
        # card breathe with every round and the whole screen shifted under the user
        sm['champ_slots'] = []
        for i in range(self.SM_CHAMPS):
            self._box(box, bg=BORDER, height=1).pack(fill='x', pady=(5, 0))
            r = self._box(box)
            r.pack(fill='x', pady=(4, 0))
            self._lbl(r, text=str(i + 1), text_color=FAINT, font=(self.UI, 10),
                      width=3).pack(side='left')
            tl = self._lbl(r, text='', text_color=TXT, font=(self.MONO, 10, 'bold'),
                           width=6, anchor='e')
            tl.pack(side='right')
            fl = self._lbl(r, text='', text_color=TXT, font=(self.MONO, 10), width=7,
                           anchor='e')
            fl.pack(side='right')
            fr = self._lbl(r, text='', text_color=MUT, font=(self.MONO, 10))
            fr.pack(side='left', fill='x', expand=True)
            sm['champ_slots'].append((fr, fl, tl))
        self._sm_champ_sig = None

        def _champ_refit(e):                             # a WIDTH change re-fits the formulas —
            if abs(e.width - getattr(self, '_sm_champ_w', 0)) > 24:   # with the delta guard, or
                self._sm_champ_w = e.width               # the update itself would loop the bind
                self._sm_champ_sig = None
        box.bind('<Configure>', _champ_refit)

        # ---- the portfolio the autopilot maintains ----
        rep = self._card(body)
        rep.pack(fill='x', pady=(12, 0))                 # natural height: the void below the
        #                                                  buttons was the ugliest thing on the
        #                                                  screen, and it said nothing
        rp = self._pad(rep)
        rh = self._box(rp)
        rh.pack(fill='x')
        sm['rep_head'] = self._lbl(rh, text='Portfolio', text_color=TXT,
                                   font=(self.UI, 14, 'bold'))
        sm['rep_head'].pack(side='left')
        warn = ctk.CTkFrame(rh, fg_color=_mix(POS, CARD, 0.87), corner_radius=999)
        warn.pack(side='left', padx=(10, 0))
        self._lbl(warn, text='best combo · TEST clean', text_color=POS,
                  font=(self.UI, 10, 'bold'),
                  bg=_mix(POS, CARD, 0.87)).pack(padx=int(8 * self.SCALE),
                                                 pady=int(2 * self.SCALE))
        self._tip(warn, 'Members AND size (2–10) are the best combination found on\n'
                        'TRAIN+VAL only — TEST never enters the search, so the TEST\n'
                        'numbers below are a genuine out-of-sample evaluation.')
        self._lbl(rh, text='rebuilds after every round', text_color=FAINT,
                  font=(self.UI, 12), anchor='e').pack(side='right')
        eq = tk.Canvas(rp, height=int(130 * self.SCALE), bg=CARD, highlightthickness=0, bd=0)
        eq.pack(fill='x', pady=(10, 4))
        sm['equity'] = eq
        eq.bind('<Configure>', lambda _e: self._pf_doc and self._sm_draw_equity(self._pf_doc))
        foot = self._box(rp)
        foot.pack(fill='x', pady=(6, 0))
        sm['serve_btn'] = self._btn(foot, '⇪  Signal API', self._pf_serve_signal,
                                    kind='soft', height=32, width=130)
        sm['serve_btn'].configure(state='disabled')
        sm['serve_btn'].pack(side='left')
        self._tip(sm['serve_btn'], 'Start a local JSON API serving this portfolio\'s live\n'
                                   'positions — the same numbers a bot would trade.')
        sm['pdf_btn'] = self._btn(foot, 'Report PDF', self._pf_pdf_report,
                                  kind='soft', height=32, width=120)
        sm['pdf_btn'].configure(state='disabled')
        sm['pdf_btn'].pack(side='left', padx=(8, 0))
        sm['track_btn'] = self._btn(foot, 'Forward track', self._pf_fwd_enroll,
                                    kind='soft', height=32, width=130)
        sm['track_btn'].configure(state='disabled')
        sm['track_btn'].pack(side='left', padx=(8, 0))
        self._tip(sm['track_btn'], 'Freeze this exact portfolio and paper-trade it forward on\n'
                                   'live closed bars. Selection used TEST, so THIS is the\n'
                                   'clean out-of-sample check.')
        sm['rep_line'] = self._lbl(foot, text='', text_color=MUT, font=(self.MONO, 11),
                                   anchor='e')
        sm['rep_line'].pack(side='right')
        sm['rep_note'] = self._lbl(rp, text='no portfolio yet — it builds itself after the '
                                            'first round with 2+ champions',
                                   text_color=FAINT, font=(self.UI, 11))
        sm['rep_note'].pack(anchor='w', pady=(4, 0))

        # ---- the served signal, at the bottom: the book of live positions. The canvas is
        # registered into _sig_canvas[port], so the SAME machinery that feeds the advanced
        # SIGNAL API card (_sig_poll_worker + the _sig_tick repaint) paints it here. ----
        sig_card = self._card(body)                      # packed by the tick when a service lives
        sm['sig_card'] = sig_card
        gp = self._pad(sig_card)
        gh = self._box(gp)
        gh.pack(fill='x')
        sm['sig_head'] = self._lbl(gh, text='Serving', text_color=TXT,
                                   font=(self.UI, 14, 'bold'))
        sm['sig_head'].pack(side='left')
        b_all = self._btn(gh, '✕ Free all ports', self._sm_stop_all, kind='danger',
                          height=26, width=118)             # the dashboard's twin (user spec)
        b_all.pack(side='right')
        self._tip(b_all, 'Stop every running signal API and release its port —\n'
                         'yours and the autopilot\'s alike.')
        sm['sig_status'] = self._lbl(gh, text='', text_color=MUT, font=(self.UI, 11),
                                     anchor='e')
        sm['sig_status'].pack(side='right', padx=(0, 12))
        self._tip(gh, 'Up to 3 services (user cap). When the node stops, the autopilot\n'
                      'serves the final portfolio (auto_portfolio) and the two best TEST\n'
                      'formulas (auto_top1/2) — each replaced on the next stop only if its\n'
                      'content changed. Services keep running after the app closes —\n'
                      '✕ Free port stops one, ✕ Free all ports stops every one.')
        sm['sig_rows'] = []
        for i in range(self.SM_SIG_MAX):                 # a fixed bank, packed per service
            fr = self._box(gp)
            ln = self._box(fr)
            ln.pack(fill='x')
            hd = self._lbl(ln, text='', text_color=TXT, font=(self.UI, 12, 'bold'))
            hd.pack(side='left')
            self._btn(ln, '✕ Free port', lambda k=i: self._sm_free_row(k), kind='danger',
                      height=24, width=88).pack(side='right')
            stx = self._lbl(ln, text='', text_color=MUT, font=(self.UI, 11), anchor='e')
            stx.pack(side='right', padx=(0, 10))
            cv = tk.Canvas(fr, height=1, bg=CARD, highlightthickness=0, bd=0)
            cv.pack(fill='x', pady=(6, 0))
            cv.bind('<Configure>',
                    lambda _e, k=i: (sm['sig_rows'][k].get('port') is not None
                                     and self._draw_signal_positions(sm['sig_rows'][k]['port'])))
            sm['sig_rows'].append({'fr': fr, 'hd': hd, 'st': stx, 'cv': cv, 'port': None})

        doc = self._pf_doc
        if doc is None:
            try:
                doc = json.load(open(PORTFOLIO_JSON, encoding='utf-8'))
            except Exception:                            # noqa: BLE001
                doc = None
        if doc and doc.get('ok'):
            self._pf_doc = doc
            self._simple_render_report(doc)

        if not getattr(self, '_fwd_tick_on', False):      # tracks enrolled here must step here
            self._fwd_tick_on = True
            self.root.after(20_000, self._fwd_tick)
        if not getattr(self, '_sm_tick_on', False):
            self._sm_tick_on = True
            self.root.after(300, self._simple_tick)

    def _sm_hdr_chips(self):
        """The header's read-only pair chips + the ✎ that summons the editor card."""
        box = (getattr(self, '_sm', None) or {}).get('hdr_pairs')
        if box is None or not box.winfo_exists():
            return
        for w in box.winfo_children():
            w.destroy()
        syms = _parse_universe(self.v_unilist.get())
        shown = syms[:6]
        for t in shown:
            self._lbl(box, text=t.replace('USDT', ''), text_color=MUT, font=(self.MONO, 10),
                      bg=HEAD_BG, padx=int(7 * self.SCALE),
                      pady=int(2 * self.SCALE)).pack(side='left', padx=(0, 4))
        if len(syms) > len(shown):
            self._lbl(box, text=f'+{len(syms) - len(shown)}', text_color=FAINT,
                      font=(self.MONO, 10), bg=HEAD_BG, padx=int(6 * self.SCALE),
                      pady=int(2 * self.SCALE)).pack(side='left', padx=(0, 4))
        pen = self._lbl(box, text='edit', text_color=ACC, font=(self.UI, 10, 'bold'), bg=BG,
                        cursor='hand2')
        pen.pack(side='left', padx=(2, 0))
        pen.bind('<ButtonRelease-1>', lambda _e: self._sm_toggle_pairs())
        self._tip(pen, 'Edit the basket (up to 20 pairs).')

    def _sm_stop_all(self):
        """The simple card's '✕ Free all ports': the dashboard's confirm + stop, then an instant
        repaint (the tick would catch up within 1.5 s, but a click wants an answer now)."""
        if not self._sigs or messagebox.askyesno(
                'Signal API', f'Stop all {len(self._sigs)} service(s) and free their ports?',
                parent=self.root):
            self._stop_all_signals()
            self._sm_sig_repaint()

    def _sm_free_row(self, k):
        """'✕ Free port' on row k of the simple serving card — whichever service sits there,
        the autopilot's or one raised on the dashboard."""
        rows = (getattr(self, '_sm', None) or {}).get('sig_rows') or []
        port = rows[k].get('port') if k < len(rows) else None
        ent = next((x for x in self._sigs if x.get('port') == port), None)
        if ent is not None:
            self._stop_signal(ent)
        self._sm_sig_repaint()

    def _sm_sig_repaint(self):
        """Drop the rows whose service is gone and hide the card when none is left."""
        sm = getattr(self, '_sm', None) or {}
        live = {x.get('port') for x in self._sigs}
        for row in sm.get('sig_rows') or []:
            if row.get('port') is not None and row['port'] not in live:
                row['port'] = None
                if row['fr'].winfo_manager():
                    row['fr'].pack_forget()
        card = sm.get('sig_card')
        try:
            if card is not None and card.winfo_exists() and not self._sigs and card.winfo_manager():
                card.pack_forget()
        except tk.TclError:
            pass

    def _sm_lic_refresh(self):
        """The header's activation affordance: the Activate button on the RIGHT (before the
        bar-size box, between Clear node and the timeframe) while this node has no
        subscription key; once it has one, a green 'activated' pill on the LEFT, right after
        the brand (user's placement) — the serving pill lines up after it. Re-activation
        after a lapse stays on the Advanced screen."""
        sm = getattr(self, '_sm', None) or {}
        btn, lbl = sm.get('act_btn'), sm.get('act_lbl')
        if btn is None or lbl is None or not btn.winfo_exists():
            return
        try:
            if self.cfg.get('vault_license'):
                if btn.winfo_manager():
                    btn.pack_forget()
                if not lbl.winfo_manager():
                    brand = sm.get('brand')
                    kw = {'after': brand} if (brand is not None and brand.winfo_manager()) else {}
                    lbl.pack(side='left', padx=(10, 0), pady=(3, 0), **kw)
            else:
                if lbl.winfo_manager():
                    lbl.pack_forget()
                if not btn.winfo_manager():
                    anchor = sm.get('tf_box')
                    kw = {'before': anchor} if (anchor is not None and anchor.winfo_manager()) else {}
                    btn.pack(side='right', padx=(0, 8), **kw)
        except tk.TclError:
            pass

    def _sm_toggle_pairs(self):
        card = (getattr(self, '_sm', None) or {}).get('pairs_card')
        if card is None or not card.winfo_exists():
            return
        if card.winfo_manager():
            card.pack_forget()
        else:
            card.pack(fill='x', pady=(0, 12), before=card.master.winfo_children()[1])

    SM_CHAMPS = 8                                        # rows in the simple champions card
    SM_SIG_MAX = 3                                       # services below the portfolio (user cap)

    def _sm_reset_after_wipe(self):
        """The simple shell's post-wipe reset: drop every cache the tick trusts and blank
        the canvases the tick repaints only on change — stale pixels must not outlive
        the files they came from."""
        self._lib_cache = {'mtime': None, 'all': [], 'families': [], 'computing': False,
                           'dirty': False, 'ts': 0.0, 'computed': False, 'select': None}
        self._sm_lib = {'mtime': None, 'rows': [], 'computing': False}
        self._pf_doc = None
        self._sm_best_cache = []
        self._sm_built_round = None
        self._sm_spark_sig = None
        self._sm_champ_sig = None
        sm = getattr(self, '_sm', None) or {}
        for key in ('spark', 'equity'):
            cv = sm.get(key)
            if cv is not None and cv.winfo_exists():
                cv.delete('all')
        self._sm_render_champs([])
        self._sm_set('t_fit', '—')
        self._sm_set('t_found', '0')
        self._sm_set('t_gen', '—')
        self._sm_set('sr_tri', '')
        self._sm_set('event', '')
        self._sm_set('rep_head', 'Portfolio', TXT)
        self._sm_set('rep_line', '')
        self._sm_set('rep_note', 'no portfolio yet — it builds itself after the first round '
                                 'with 2+ champions')
        for k in ('serve_btn', 'track_btn', 'pdf_btn'):
            w = sm.get(k)
            if w is not None and w.winfo_exists():
                w.configure(state='disabled')

    def _sm_auto_serve(self):
        """The serving autopilot (user spec, rev. 3): after the node stops — never mid-run —
        fill all three slots: the built PORTFOLIO plus the TWO best formulas by held-out
        TEST from the library (auto_top1/auto_top2, near-clones deduped). Each is replaced
        only when its content changed; an auto_top slot whose champion left the top is
        stopped. A service the user raised (any other label) is never touched."""
        jobs = []
        pf = list((self._pf_doc or {}).get('formulas_full') or [])
        if pf:
            jobs.append(('auto_portfolio', pf))
        rows = [r for r in self._sm_champ_test_rows(self.SM_CHAMPS)   # sealed rows (flagged,
                if r[0] and not (len(r) > 3 and r[3])                 # or the old 'id:' text)
                and not r[0].startswith('id:')]                       # are unservable
        picked = []
        for f in (r[0] for r in rows):
            if all(difflib.SequenceMatcher(None, f, g).ratio() < 0.85 for g in picked):
                picked.append(f)                         # variety, not the champion's twin
            if len(picked) >= 2:
                break
        for i, f in enumerate(picked, 1):
            jobs.append((f'auto_top{i}', [f]))
        want = {label for label, _ in jobs}
        if picked:                                       # a champion that left the top takes its
            for x in list(self._sigs):                   # service along (ours by label) — but a
                lbl = str(x.get('label') or '')          # cold library cache must not kill a
                if lbl.startswith('auto_top') and lbl not in want:   # perfectly good service
                    self._stop_signal(x)
        for label, formulas in jobs:
            cur = next((x for x in self._sigs if x.get('label') == label), None)
            fsig = hashlib.md5('\n'.join(formulas).encode()).hexdigest()
            if cur is not None and cur.get('fsig') == fsig and self._sig_alive(cur):
                continue                                 # same content, still up — no churn:
            if cur is not None:                          # a restart wastes minutes of warm-up
                self._stop_signal(cur)
            if sum(1 for x in self._sigs if self._sig_alive(x)) >= self.SM_SIG_MAX:
                self._sm_set('sig_status', f'{self.SM_SIG_MAX} services are up — free a '
                                           'port to let the autopilot serve', MUT)
                continue
            err = self._serve_signal(formulas, label, quiet=True)
            if err:
                self._sm_set('rep_note', err[:140], MUT)
                continue
            ent = next((x for x in self._sigs if x.get('label') == label), None)
            if ent is not None:
                ent['fsig'] = fsig                       # remembered across restarts (_sig_save)
                self._sig_save()

    @staticmethod
    def _sm_locked_text(c):
        """A sealed row's cell: the vault hides the formula, not the numbers — TEST Sharpe,
        win rate and PnL take the place of a bare id (user spec: an unactivated node still
        shows what each champion did). Rows mined before wr/ret were stored fall back to the
        annualized CAGR as PnL."""
        t = c.get('test') if isinstance(c.get('test'), dict) else {}
        sh, wr, ret, cagr = t.get('sharpe'), t.get('wr'), t.get('ret'), t.get('cagr')

        def num(v):
            return isinstance(v, (int, float))
        pnl = (f'PnL {ret * 100:+.0f}%' if num(ret)
               else f'PnL {cagr * 100:+.0f}%/yr' if num(cagr) else 'PnL —')
        return ('locked · ' + (f'Sharpe {sh:+.2f}' if num(sh) else 'Sharpe —') + ' · '
                + (f'win {wr * 100:.0f}%' if num(wr) else 'win —') + ' · ' + pnl)

    @staticmethod
    def _sm_row(c):
        """(text, fit, test, locked) — one champions-card row from a library / status doc."""
        f = str(c.get('formula') or '')
        t = c.get('test') if isinstance(c.get('test'), dict) else {}
        return (f or App._sm_locked_text(c), c.get('base'), t.get('sharpe'), not f)

    @staticmethod
    def _sm_champ_rows(best, cap):
        """(text, fit, test, locked) rows in the node's own fitness order — status.json's best."""
        return [App._sm_row(c) for c in (best or [])][:cap]

    def _sm_lib_kick(self):
        """Freshness check + background (re)read of the library; returns the cache dict."""
        lib = getattr(self, '_sm_lib', None)
        if lib is None:
            lib = self._sm_lib = {'mtime': None, 'rows': [], 'computing': False}
        path = self._lib_file()
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return lib
        if mt != lib['mtime'] and not lib['computing']:
            lib['computing'] = True
            threading.Thread(target=self._sm_lib_compute, args=(path, mt), daemon=True).start()
        return lib

    def _sm_champ_test_rows(self, cap):
        """Top of the LIBRARY by held-out TEST — the population the advanced table ranks when
        its TEST OOS header is clicked. status.json's best list is the top-KEEP by FITNESS,
        so re-sorting it would hide every high-TEST formula sitting below the fitness cut.
        Read + ranked in a background thread, cached by the library file's mtime."""
        return self._sm_lib_kick()['rows'][:cap]

    def _sm_best_test_series(self, hist):
        """The spark's dashed line: the best held-out TEST found SO FAR (running max over the
        library, by discovery round) — so its endpoint agrees with the by-test top of the
        champions table. The old line, TEST of the FITNESS champion, sat at +0.02 while the
        table's top read +1.57 (user-reported mismatch). Until the library loads, the
        champion's TEST from history stands in."""
        tmax = self._sm_lib_kick().get('tmax') or []
        if not tmax:
            return [x.get('best_test') for x in hist]
        out, i, run = [], 0, None
        for x in hist:
            r = x.get('round')
            if isinstance(r, (int, float)):
                while i < len(tmax) and tmax[i][0] <= r:
                    run = tmax[i][1] if run is None else max(run, tmax[i][1])
                    i += 1
            out.append(run)
        return out

    def _sm_lib_compute(self, path, mtime):
        """Mirror of _compute_lb's 'test' branch, shrunk to the card's needs: every library
        row with a TEST number, ranked by it — no dedup, so the top matches the advanced
        table row for row. The tick's sig check picks the fresh rows up on the next pass."""
        rows, rmax = [], {}
        try:
            for line in open(path, encoding='utf-8'):
                line = line.strip()
                if not line:
                    continue
                try:
                    c = json.loads(line)
                except json.JSONDecodeError:             # a half-written last line while the node appends
                    continue
                t = self._LB_TESTKEY(c)
                if t is None:
                    continue
                r = c.get('round')
                r = r if isinstance(r, (int, float)) else 0
                if t > rmax.get(r, -1e9):                # per-round best TEST, for the spark's
                    rmax[r] = t                          # running-max line
                rows.append(App._sm_row(c))
        except OSError:
            self._sm_lib.update(computing=False)
            return
        rows.sort(key=lambda r: r[2], reverse=True)
        del rows[self.SM_CHAMPS * 3:]
        self._sm_lib.update(rows=rows, mtime=mtime, computing=False,
                            tmax=sorted(rmax.items()))

    def _sm_toggle_champ_sort(self):
        self._sm_champ_by = 'test' if getattr(self, '_sm_champ_by', 'fit') == 'fit' else 'fit'
        lbl = (getattr(self, '_sm', None) or {}).get('champ_by')
        if lbl is not None and lbl.winfo_exists():
            lbl.configure(text=('by fitness' if self._sm_champ_by == 'fit' else 'by test'))
        self._sm_champ_sig = None                        # instant re-render; library rows land
        self._sm_render_champs(getattr(self, '_sm_best_cache', []))   # on the next tick if stale

    def _sm_render_champs(self, best):
        """Top rows of the round leaderboard, straight from status.json's best list. The row
        widgets are a fixed bank — only their TEXT changes, so the card's size never does."""
        slots = (getattr(self, '_sm', None) or {}).get('champ_slots') or []
        if not slots or not slots[0][0].winfo_exists():
            return
        by = getattr(self, '_sm_champ_by', 'fit')
        if by == 'test':
            rows = self._sm_champ_test_rows(len(slots))
            if not rows:                                 # first library read still in flight —
                rows = sorted(self._sm_champ_rows(best, 999),   # approximate from the node's top
                              key=lambda r: (r[2] if isinstance(r[2], (int, float)) else -1e9),
                              reverse=True)[:len(slots)]
        else:
            rows = self._sm_champ_rows(best, len(slots))
        sig = (by,) + tuple((r[0][:40], round(r[1], 3) if isinstance(r[1], (int, float)) else None,
                             round(r[2], 2) if isinstance(r[2], (int, float)) else None)
                            for r in rows)
        if sig == self._sm_champ_sig:
            return
        self._sm_champ_sig = sig
        f_mono = self._font(self.MONO, 10)
        for i, (fr, fl, tl) in enumerate(slots):
            if i >= len(rows):
                fr.configure(text='')
                fl.configure(text='')
                tl.configure(text='')
                continue
            formula, base, test = rows[i][:3]
            locked = len(rows[i]) > 3 and bool(rows[i][3])
            room = max(80, fr.winfo_width() - 8)
            fr.configure(text=self._ellipsize(f_mono, formula, room),
                         fg=(FAINT if locked else MUT))  # a sealed row reads as numbers, dimmed
            fl.configure(text=f'{base:+.2f}' if isinstance(base, (int, float)) else '—')
            tcol = POS if isinstance(test, (int, float)) and test >= 1.0 else \
                (NEG if isinstance(test, (int, float)) and test < 0 else TXT)
            tl.configure(text=(f'{test:+.2f}' if isinstance(test, (int, float)) else '—'),
                         fg=tcol)

    def _sm_paint_go(self, kind, progress, line1, line2=''):
        # the width MUST match the canvas from _build_simple: a wider capsule paints its
        # right cap past the edge and the button shows up square-cornered
        self._pill_paint((getattr(self, '_sm', None) or {}).get('go'), kind, progress,
                         line1, line2, w=getattr(self, '_sm_go_w', None) or int(210 * self.SCALE),
                         h=int(38 * self.SCALE))

    def _pill_paint(self, cv, kind, progress, line1, line2='', w=None, h=None):
        """The run pill (the redesign's signature control): a deep-indigo capsule with a
        progress ring. The ring shows the REAL round progress from status.json — when the
        model has no estimate yet, the ring simply stays a track, it never fakes a value.
        kind: 'idle' (play glyph, accent fill) | 'run' | 'fetch' (stop glyph, run fill).
        One painter serves both shells — the simple screen's big button and the advanced
        header's compact one differ only in geometry."""
        if cv is None or not cv.winfo_exists():
            return
        sig = (kind, progress, line1, line2)             # _poll repaints every 1.5s — identical
        if getattr(cv, '_pill_sig', None) == sig:        # frames would flicker for nothing
            return
        cv._pill_sig = sig
        S = self.SCALE
        h = h or int(38 * S)
        if w is None:                                    # the header pill hugs its text
            f1 = self._font(self.UI, 12, 'bold')
            f2 = self._font(self.MONO, 9)
            w = int(h / 2 + 13 * S + 10 * S + max(f1.measure(line1),
                                                  f2.measure(line2)) + 18 * S)
            cv.configure(width=w)
        cv.delete('all')
        fill = ACC_FILL if kind == 'idle' else RUN_BG
        self._capsule(cv, 0, w, h / 2, h, fill)
        rx, r = h / 2, min(13 * S, h * 0.30)             # the ring, centred in the cap
        if kind == 'idle':
            cv.create_polygon(rx - 4 * S, h / 2 - 6 * S, rx - 4 * S, h / 2 + 6 * S,
                              rx + 7 * S, h / 2, fill='#ffffff', outline='')
        else:
            box = (rx - r, h / 2 - r, rx + r, h / 2 + r)
            cv.create_oval(*box, outline=RUN_RING, width=max(2, round(3 * S)))
            if isinstance(progress, (int, float)) and 0.0 < progress <= 1.0:
                cv.create_arc(*box, start=90, extent=-progress * 359.9, style='arc',
                              outline=RUN_ARC, width=max(2, round(3 * S)))
            sq = 4.5 * S
            cv.create_rectangle(rx - sq, h / 2 - sq, rx + sq, h / 2 + sq,
                                fill='#ffffff', outline='')
        tx = rx + r + 10 * S
        if line2:
            cv.create_text(tx, h / 2 - 7 * S, text=line1, anchor='w', fill='#ffffff',
                           font=self._font(self.UI, 12, 'bold'))
            cv.create_text(tx, h / 2 + 9 * S, text=line2, anchor='w', fill=RUN_ARC,
                           font=self._font(self.MONO, 9))
        else:
            cv.create_text(tx, h / 2, text=line1, anchor='w', fill='#ffffff',
                           font=self._font(self.UI, 13, 'bold'))

    def _sm_set(self, key, text, color=None):
        w = (getattr(self, '_sm', None) or {}).get(key)
        try:
            if w is not None and w.winfo_exists():
                w.configure(text=text, **({'fg': color} if color else {}))
        except tk.TclError:
            pass

    def _simple_go(self):
        p = getattr(self, '_sm_fetch_proc', None)
        if p is not None and p.poll() is None:           # PREPARING: the button cancels the
            try:                                         # download (tmp+replace write: a mid-
                p.terminate()                            # write kill never corrupts the snapshot)
            except Exception:                            # noqa: BLE001
                pass
            return
        if self.proc and self.proc.poll() is None:
            self.stop()
        else:
            self._sm_built_round = None                  # a fresh run rebuilds from round 1
            self.start()

    def _simple_tick(self):
        if self._shell_mode != 'simple':
            self._sm_tick_on = False                     # _build_simple re-arms on return
            return
        sm = getattr(self, '_sm', None) or {}
        cap = sm.get('state')
        if cap is None or not cap.winfo_exists():
            self._sm_tick_on = False
            return
        running = bool(self.proc and self.proc.poll() is None)
        building = bool(self._pf_proc and self._pf_proc.poll() is None)
        st = {}
        try:
            st = json.load(open(STATUS_FILE, encoding='utf-8'))
        except Exception:                                # noqa: BLE001
            pass
        live = running and st.get('state') in ('running', 'starting')   # a SIGKILLed node leaves
        #                                                  a stale 'running' behind — the proc
        #                                                  liveness check is what tells the truth
        r_now = int(st.get('rounds') or 0)
        r_max = int(self.cfg.get('max_rounds') or 0)
        r_of = f' of {r_max}' if r_max > 0 else ''
        if self._fetching:
            mode, text = 'download', 'Search · downloading market data'
        elif running and not live:
            mode, text = 'download', 'Search · starting the engine'
        elif running:
            mode = 'build' if building else 'search'
            text = f'Search · round {r_now + 1}{r_of}' + (
                ' · updating the portfolio' if building
                else f" · {st.get('mode') or ''}".rstrip(' ·'))
        elif building:
            mode, text = 'build', 'Search · building the portfolio'
        else:
            mode = 'idle'
            text = ('Search · stopped — the report below is final' if self._pf_doc
                    else 'Search — pick your pairs and press Start')
        self._sm_set('state', text, TXT if mode != 'idle' else MUT)
        # counters: real fields from status.json, nothing else
        found = int(st.get('found') or 0)
        tri = f"{st.get('trials_total', 0):,} formulas" if st else ''
        if tri and not running and not building:
            tri = 'stopped · ' + tri
        self._sm_set('sr_tri', tri)
        self._sm_set('t_found', str(found))
        g = re.search(r'gen\s+(\d+)', st.get('gen', '') or '')
        self._sm_set('t_gen', g.group(1) if g and running else '—')
        self._sm_best_cache = st.get('best') or []
        self._sm_render_champs(self._sm_best_cache)
        hist_s = st.get('history') or []
        sp = (getattr(self, '_sm', None) or {}).get('spark')
        if sp is not None and sp.winfo_exists():
            base_s = [x.get('best_base', x.get('best_test')) for x in hist_s]
            base_s = [v for v in base_s if isinstance(v, (int, float))]
            test_s = self._sm_best_test_series(hist_s)
            t_last = next((v for v in reversed(test_s) if isinstance(v, (int, float))), None)
            sig_s = (len(base_s), round(base_s[-1], 4) if base_s else None,
                     round(t_last, 4) if t_last is not None else None, sp.winfo_width())
            if len(base_s) >= 2 and sig_s != self._sm_spark_sig:
                self._sm_spark_sig = sig_s
                self._spark_paint(sp, base_s, test_s)
        hist = st.get('history') or []
        fit = next((h.get('best_base', h.get('best_test')) for h in reversed(hist)
                    if h.get('best_base', h.get('best_test')) is not None), None)
        wr = (st.get('fit_metric') == 'winrate' and isinstance(fit, (int, float))
              and 0.0 <= fit <= 1.0)
        self._sm_set('t_fit', '—' if fit is None else (f'{fit * 100:.0f}%' if wr else f'{fit:+.2f}'))
        if self._fetching:
            self._sm_set('event', (self._sm_fetch_line or '')[:120])
        else:
            evs = st.get('events') or []
            if evs:
                last = evs[-1].get('t', '')
                if last and last != getattr(self, '_sm_ev_seen', None):
                    self._sm_ev_seen = last
                    self._sm_set('event', last[:120])
        if self._fetching:
            self._sm_paint_go('fetch', None, 'Cancel download', 'fetching market data')
        elif running:
            prog = st.get('progress')
            r_line = f"round {int(st.get('rounds') or 0) + 1}"
            sub = 'stop — fix the result'
            if fit is not None:
                sub = f'best {fit * 100:.0f}%' if wr else f'best {fit:+.2f}'
            left = self._round_eta_left(st)
            if left is not None and float(prog or 0.0) < 1.0:
                sub += f' · {self._fmt_left(left)}'
            self._sm_paint_go('run', (float(prog) if isinstance(prog, (int, float)) else None),
                              r_line, sub)
        else:
            self._sm_paint_go('idle', None, 'Start node')
        alive = [s2 for s2 in self._sigs if self._sig_alive(s2)]
        frame = sm.get('serve_frame')
        if frame is not None and frame.winfo_exists():
            if alive:
                self._sm_set('serve_pill',
                             'serving ' + ' '.join(f":{s2['port']}" for s2 in alive[:3]))
                if not frame.winfo_manager():
                    frame.pack(side='left', padx=(10, 0), pady=(3, 0))
            elif frame.winfo_manager():
                frame.pack_forget()
        card = sm.get('sig_card')
        if card is not None and card.winfo_exists():
            rows = sm.get('sig_rows') or []
            show = self._sigs[:self.SM_SIG_MAX]          # PERMANENT while a service exists —
            for i, row in enumerate(rows):               # a first signal takes minutes to
                s2 = show[i] if i < len(show) else None  # compute, and a hidden row read
                fr = row['fr']                           # as 'serve did nothing'
                if s2 is None:
                    row['port'] = None
                    if fr.winfo_manager():
                        fr.pack_forget()
                    continue
                port = s2['port']
                if row['port'] != port:
                    row['port'] = port                   # adopt: the shared repaint loop in
                    self._sig_canvas[port] = row['cv']   # _sig_tick paints this canvas now
                    self._sig_drawn.pop(port, None)      # force the first paint
                row['hd'].configure(text=f":{port} · {s2.get('label', '')} · live positions")
                htxt = self._sig_health.get(port, 'starting…')
                row['st'].configure(text=htxt, fg=self._sig_status_color(htxt))
                if not fr.winfo_manager():
                    fr.pack(fill='x', pady=(8, 0))
            if show:
                self._sm_set('sig_status',
                             f'{len(show)} of {self.SM_SIG_MAX} · re-serves after Stop')
                if not card.winfo_manager():
                    card.pack(fill='x', pady=(12, 0))
            elif card.winfo_manager():
                card.pack_forget()
        # ---- the autopilot: every finished round refreshes the portfolio; the last word
        # is a final build when the node stops. Never two builds at once (shared _pf_proc). ----
        r = int(st.get('rounds') or 0)
        if (running and r > 0 and r != getattr(self, '_sm_built_round', None)
                and found >= 2 and not building and not self._fetching):
            self._sm_built_round = r
            self._simple_build_portfolio()
        elif (getattr(self, '_sm_was_running', False) and not running and not building
                and found >= 2):
            self._sm_built_round = None
            self._simple_build_portfolio()
        self._sm_was_running = running
        try:
            while True:                                  # the node's stdout lands in logq and only
                self.logq.get_nowait()                   # _poll drains it — here that duty is ours,
        except queue.Empty:                              # or a long run grows the queue for hours
            pass
        self.root.after(1500, self._simple_tick)

    def _simple_build_portfolio(self, n=0):
        """The autopilot's build: the same child, the same env, the same doc file as the
        dashboard's Build button — only the messenger differs. Selection is combo with AUTO
        size (n=0): the builder simulates a fitness-ranked pool once, then keeps the best
        COMBINATION of 2..10 members found on TRAIN+VAL — the size is chosen, not fixed at 6
        (user request; ceiling 10, his cap), and TEST never enters the search, so the
        report reads clean."""
        if self._pf_proc and self._pf_proc.poll() is None:
            return
        env = dict(os.environ)
        env.update(ALPHANODE_STATE_DIR=STATE_DIR, ALPHANODE_DATA=self._data_file(),
                   ALPHANODE_TF=self._tf(),
                   ALPHANODE_CONFIG_INI=apppaths.config_ini(),
                   ALPHANODE_UNIVERSE=self.cfg.get('universe_list',
                                                   DEFAULTS['universe_list']),
                   ALPHANODE_TRAIN_START=self.cfg.get('train_start', ''),
                   ALPHANODE_VAL_START=self.cfg.get('val_start', ''),
                   ALPHANODE_TEST_START=self.cfg.get('test_start', ''),
                   ALPHANODE_TEST_END=self.cfg.get('test_end', ''))
        try:
            self._pf_proc = subprocess.Popen(
                _child_cmd('portfolio') + ['--top', str(n), '--select', 'combo',
                                           '--out', PORTFOLIO_JSON], env=env,
                cwd=(apppaths.USER_DIR if apppaths.FROZEN else PROJ),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace')
        except Exception as e:                           # noqa: BLE001
            self._sm_set('rep_note', f'portfolio build failed to start: {e}', NEG)
            return
        threading.Thread(target=self._simple_pf_reader, args=(self._pf_proc,),
                         daemon=True).start()

    def _simple_pf_reader(self, proc):
        for line in iter(proc.stdout.readline, ''):
            line = line.strip()
            if line:
                self.root.after(0, lambda t=line: self._sm_set('rep_note', t[:140]))
        proc.wait()
        self.root.after(0, self._simple_pf_done)

    def _simple_pf_done(self):
        try:
            doc = json.load(open(PORTFOLIO_JSON, encoding='utf-8'))
        except Exception:                                # noqa: BLE001
            return
        if not doc.get('ok'):
            err = str(doc.get('error', ''))[:120]
            self._sm_set('rep_note', 'portfolio build failed: ' + err, NEG)
            if 'unlock' in err.lower() or 'sealed' in err.lower() or 'licence' in err.lower():
                self._sm_set('rep_note', 'the library is sealed — activate your licence to '
                                         'let the autopilot curate a portfolio', NEG)
            return
        self._pf_doc = doc
        if self._shell_mode == 'simple':
            self._simple_render_report(doc)
            if not (self.proc and self.proc.poll() is None):   # the final build after Stop:
                self._sm_auto_serve()                    # leave the services serving the last word

    def _simple_render_report(self, doc):
        m = doc.get('metrics') or {}
        b = doc.get('basket') or {}
        sh = m.get('sharpe')
        sealed = bool(doc.get('sealed'))                 # vault preview: numbers, no formulas
        self._sm_set('rep_head', f"Portfolio · {doc.get('n', '?')} alphas"
                                 + (' · sealed preview' if sealed else ''), TXT)
        self._sm_set('rep_line',
                     f"test CAGR {m.get('cagr', 0) * 100:+.0f}% · "
                     f"maxDD {m.get('dd', 0) * 100:.0f}% · "
                     f"vs buy&hold {b.get('sharpe', 0):+.2f}",
                     POS if (sh or 0) >= 0 else NEG)
        self._sm_set('rep_note', ('the vault keeps the formulas sealed — this is the mix of '
                                  'their cached return series; activate the node to open them, '
                                  'serve the Signal API and export' if sealed else ''), MUT)
        for k in ('serve_btn', 'track_btn'):
            w = (self._sm or {}).get(k)
            if w is not None and w.winfo_exists():
                w.configure(state=('normal' if doc.get('formulas_full') else 'disabled'))
        w = (self._sm or {}).get('pdf_btn')
        if w is not None and w.winfo_exists():
            w.configure(state=('normal' if doc.get('weights') else 'disabled'))
        self._sm_draw_equity(doc)

    def _sm_draw_equity(self, doc):
        """The report's equity: the combined curve over the basket's, TEST shaded, segment
        boundaries ticked. portfolio_build writes equity as {dates, combined, basket} and
        bounds as the four segment DATES — everything is joined by bisecting the date list."""
        cv = (getattr(self, '_sm', None) or {}).get('equity')
        if cv is None or not cv.winfo_exists():
            return
        cv.delete('all')
        eq = doc.get('equity') or {}
        dates = eq.get('dates') or []
        comb = eq.get('combined') or []
        bask = eq.get('basket') or []
        n = len(comb)
        if n < 10 or len(dates) != n:
            return
        w, h = cv.winfo_width(), int(cv['height'])
        if w <= 1:
            return
        import bisect
        import math as _m

        def _lg(v):                                      # equity compounds: log scale, or the
            return _m.log(max(float(v), 1e-9))           # early years flatline against the peak
        both = [_lg(v) for v in comb] + ([_lg(v) for v in bask] if len(bask) == n else [])
        lo, hi = min(both), max(both)
        span = (hi - lo) or 1.0

        def _x(i):
            return i / (n - 1) * (w - 2) + 1

        def _pts(series):
            out = []
            for i, v in enumerate(series):
                out += [_x(i), h - 3 - (_lg(v) - lo) / span * (h - 6)]
            return out
        bounds = doc.get('bounds') or {}
        segs = doc.get('segments') or {}
        vi = bisect.bisect_left(dates, str(bounds.get('val_start') or ''))
        ti = bisect.bisect_left(dates, str(bounds.get('test_start') or ''))
        if 0 < vi < n:                                   # train and test wear a wash, val stays
            cv.create_rectangle(1, 0, _x(vi), h, fill=STRIPE, outline='')   # clear — the mock's
        if 0 < ti < n:                                   # three-band read
            cv.create_rectangle(_x(ti), 0, w, h, fill=STRIPE, outline='')
        for key, bi in (('val_start', vi), ('test_start', ti)):
            if 0 < bi < n:
                cv.create_line(_x(bi), 0, _x(bi), h, fill=GRID)

        def _seg_lbl(name, x):
            sh_v = (segs.get(name) or {}).get('sharpe')
            txt = f'{name} · {sh_v:.2f}' if isinstance(sh_v, (int, float)) else name
            cv.create_text(x + 6 * self.SCALE, h - 4 * self.SCALE, text=txt, anchor='sw',
                           font=self._font(self.UI, 10), fill=FAINT)
        _seg_lbl('train', 0)
        if 0 < vi < n:
            _seg_lbl('val', _x(vi))
        if 0 < ti < n:
            _seg_lbl('test', _x(ti))
        if len(bask) == n:                               # what holding the basket would have done
            cv.create_line(*_pts(bask), fill=_mix(FAINT, CARD, 0.45),
                           width=max(1, round(1.2 * self.SCALE)))
        cv.create_line(*_pts(comb), fill=ACC, width=max(1, round(1.8 * self.SCALE)))

    def _run_fetch_quiet(self, args, caption, on_success=None):
        """Simple mode's data step: the same fetch_data child as the console dialog, but the
        stream lands on the status line instead of a Toplevel. Cancel is the go button (a
        terminate — the writer uses .tmp + os.replace, so a mid-write kill never corrupts
        the snapshot). Flag semantics mirror _run_fetch exactly: _fetching for the tick,
        cache invalidation and on_success (Tk thread, exit 0 only)."""
        if self._fetching:
            return
        self._fetching = True
        self._sm_fetch_line = caption
        try:
            proc = subprocess.Popen(_child_cmd('fetch') + args,
                                    cwd=(apppaths.USER_DIR if apppaths.FROZEN else PROJ),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding='utf-8', errors='replace')
        except Exception as e:                           # noqa: BLE001
            self._fetching = False
            self._sm_fetch_line = f'download failed to start: {e}'
            return
        self._sm_fetch_proc = proc
        q = queue.Queue()

        def _reader():
            for line in proc.stdout:
                q.put(line)
            q.put(None)
        threading.Thread(target=_reader, daemon=True).start()

        def pump():
            try:
                while True:
                    line = q.get_nowait()
                    if line is None:
                        code = proc.poll()
                        self._fetching = False
                        self._sm_fetch_proc = None
                        self._lib_cache['mtime'] = None   # fresh data: stats measured on the old
                        self._metrics_cache = {}          # snapshot must not survive the swap
                        self._sm_fetch_line = ('✓ data ready' if code == 0 else
                                               f'✗ download error (code {code}) — data left '
                                               'untouched')
                        if code == 0 and on_success is not None:
                            self.root.after(700, on_success)
                        return
                    if line.strip():
                        self._sm_fetch_line = line.strip()[:140]
            except queue.Empty:
                pass
            self.root.after(150, pump)
        self.root.after(150, pump)

    def _poll(self):
        if getattr(self, '_shell_mode', 'advanced') != 'advanced':
            self.root.after(1500, self._poll)             # keep the loop alive across Simple —
            return                                        # its widgets are gone, not its duty
        running = bool(self.proc and self.proc.poll() is None)
        self._set_running(running)
        st = {}
        try:
            st = json.load(open(STATUS_FILE, encoding='utf-8'))
        except Exception:
            pass
        if st:
            state = st.get('state', '—')
            color = {'running': POS, 'starting': ACC}.get(state, MUT)
            self._state_pill(f'● {"running" if state == "running" else state}', color)
            live = running or state in ('running', 'starting')
            vol = st.get('target_vol')
            vol_s = f' · target vol {vol:g}' if isinstance(vol, (int, float)) else ''
            res = (f'CPU budget {st.get("cpu_percent", "?")}% '
                   f'({st.get("n_jobs", "?")}/{st.get("cores", "?")} cores) '
                   f'· universe {st.get("universe", "")}{vol_s}')
            self.s_rounds.configure(text=str(st.get('rounds', 0)))
            self.s_trials.configure(text=f'{st.get("trials_total", 0):,}')
            self.s_found.configure(text=str(st.get('found', len(st.get('best', [])))))
            if live:
                g = re.search(r'gen\s+(\d+)', st.get('gen', '') or '')
                l1 = f"round {int(st.get('rounds') or 0) + 1}" + (f' · gen {g.group(1)}' if g else '')
                l2 = f"{st.get('n_jobs', '?')}/{st.get('cores', '?')} cores"
                left_p = self._round_eta_left(st)
                if left_p is not None and float(st.get('progress') or 0.0) < 1.0:
                    l2 += f' · {self._fmt_left(left_p)} left'
                prog = st.get('progress')
                self._pill_paint(getattr(self, 'run_pill', None), 'run',
                                 (float(prog) if isinstance(prog, (int, float)) else None), l1, l2)
            if not live:                                 # stale config text must not read as
                res = 'last run — ' + res                # live telemetry next to a 'stopped' pill
            self.lbl_res.configure(text=res, fg=MUT if live else FAINT)
            self._update_round_bar(st, live)
            evs = st.get('events') or []
            self._maybe_render_events(evs)
            hist = st.get('history') or []               # the retired PROGRESS chart, as one number
            fit = next((p.get('best_base', p.get('best_test')) for p in reversed(hist)
                        if p.get('best_base', p.get('best_test')) is not None), None)
            wr = st.get('fit_metric') == 'winrate' and isinstance(fit, (int, float)) \
                and 0.0 <= fit <= 1.0                    # a winrate base is a share
            self.s_fit.configure(text=('—' if fit is None
                                       else f'{fit * 100:.0f}%' if wr else f'{fit:+.2f}'))
        # the leaderboard is a view of the LIBRARY FILE, not of the node: it must fill
        # even when no status.json exists yet (fresh start, restored session)
        self._refresh_leaderboard(st.get('best', []))
        if not running and (not st or st.get('state') != 'running'):
            if not (self.proc and self.proc.poll() is None):
                self._state_pill('● stopped', MUT)
        try:
            while True:
                self.logq.get_nowait()
        except queue.Empty:
            pass
        self.root.after(1500, self._poll)

    def _families(self, rows, target):
        """The best alpha per family: walk `rows` (already best-first) and keep one representative
        per formula shape (SequenceMatcher < 0.80), until `target` distinct families. The scan is
        capped so the O(N²) similarity can't freeze the GUI on a huge library."""
        kept = []

        def _shape(c):                                   # vault docs have no text — the id stands in,
            return c.get('formula') or 'id:' + str(c.get('id', ''))   # so each stays its own family

        for c in rows[:500]:
            f = _shape(c)
            if all(difflib.SequenceMatcher(None, f, _shape(k)).ratio() < 0.80 for k in kept):
                kept.append(c)
            if len(kept) >= target:
                break
        return kept

    _LB_TESTKEY = staticmethod(
        lambda c: (c.get('test') if isinstance(c.get('test'), dict) else {}).get('sharpe'))

    _SORTABLE = ('fit', 'test', 'dd', 'cagr', 'srt',
                 'tup', 'tdown', 'tflat', 'ls', 'bal', 'act', 'wup', 'wdown', 'formula')

    @staticmethod
    def _finite(v):
        """A finite number or None — NaN must never reach the sort (it poisons comparisons)."""
        return v if (isinstance(v, (int, float)) and v == v
                     and v not in (float('inf'), float('-inf'))) else None

    def _sort_key(self, c, col):
        if col == 'fit':
            return c.get('base')
        if col == 'test':
            return self._LB_TESTKEY(c)
        if col == 'formula':
            return c.get('formula', '')
        m = self._metrics_cache.get(c.get('formula', ''))    # the rest — from the metrics cache
        m = m if isinstance(m, dict) else {}
        if col in ('dd', 'cagr'):                            # library row first, worker fallback
            t = c.get('test') if isinstance(c.get('test'), dict) else {}
            return self._finite(t.get(col)) if self._finite(t.get(col)) is not None \
                else self._finite(m.get(col))
        if col == 'srt':
            return self._finite(m.get('sortino'))
        if col in ('tup', 'tdown', 'tflat', 'wup', 'wdown'):
            return self._finite(m.get(col))
        if col == 'ls':
            return m.get('long', 0) + m.get('short', 0)
        if col == 'act':
            return m.get('act')
        return None

    def _sorted(self, rows):
        col, desc = self._sort_col, self._sort_desc
        if col == 'formula':
            return sorted(rows, key=lambda c: c.get('formula', ''), reverse=desc)
        if col == 'fit':                                 # two-tier: active objective's rows ALWAYS
            act = 'winrate' if self.cfg.get('opt_winrate') else 'sharpe'

            def bk(c):                                   # first (their bases share a scale), the
                b = c.get('base')                        # click only flips base order inside tiers
                b = b if isinstance(b, (int, float)) else float('-inf')
                return ((c.get('fit_metric') or 'sharpe') != act, -b if desc else b)
            return sorted(rows, key=bk)

        def k(c):
            v = self._sort_key(c, col)
            return float('-inf') if v is None else v      # missing metrics sort to the bottom
        return sorted(rows, key=k, reverse=desc)

    def _update_headings(self):
        for c, txt in self._HEAD.items():
            arrow = ('  ▼' if self._sort_desc else '  ▲') if c == self._sort_col else ''
            self.tree.heading(c, text=txt.upper() + arrow)

    _PF_COLS = ('n', 'id', 'solo', 'fit', 'test', 'formula')
    SIG_MAX = 10                                         # running signal services at once — each
    #                                                      is a full engine process re-simulating
    #                                                      its formulas every refresh
    PF_ROWS_MAX = 12                                     # members shown before the list scrolls
    PF_TOP_MIN, PF_TOP_MAX = 2, 20                       # what the 'top' spinner advertises

    _LB_OPT_ORDER = ('id', 'dd', 'cagr', 'srt',                 # no plain win%: the raw share
                     'tup', 'tdown', 'tflat', 'ls', 'bal', 'act', 'wup', 'wdown')   # of up days
    _LB_OPT_DEFAULT = ('dd', 'cagr', 'id', 'wup', 'wdown', 'tup', 'tdown', 'tflat', 'bal')   # read
    #                                          as noise (~50% for every row) — dropped on request

    def _adv_cols(self):
        """Advanced display columns: the honest core (#/fitness/TEST/formula) plus the user's
        optional picks. ID rides right behind the rank number — it is the row's NAME, the token
        carried to the forward track and the CSVs, so it belongs beside '#' rather than adrift
        in the middle of the analysis block. The rest keep the _LB_OPT_ORDER sequence, which
        still holds ID ahead of the lazily computed columns — a '…' placeholder next to the ID
        used to read as a truncated ID."""
        saved = self.cfg.get('lb_cols')
        on = set(saved) if isinstance(saved, list) else set(self._LB_OPT_DEFAULT)
        head = ('id',) if 'id' in on else ()
        rest = [c for c in self._LB_OPT_ORDER if c in on and c != 'id']
        return ('fav', 'rank', *head, 'fit', 'test', *rest, 'formula')

    def _lb_toggle_col(self, c):
        """Header right-click menu: show/hide an optional column. Data, sorting and the CSV
        exports always carry every column — this flips displaycolumns only."""
        saved = self.cfg.get('lb_cols')
        on = set(saved) if isinstance(saved, list) else set(self._LB_OPT_DEFAULT)
        on.symmetric_difference_update({c})
        self.cfg['lb_cols'] = sorted(on)
        self._save()
        disp = self._adv_cols()
        self.tree.configure(displaycolumns=disp)
        self._lb_cols_fixed = [x for x in disp if x != 'formula']
        self._fit_formula_col()

    def _head_tip_hide(self):
        self._head_tip_col = None
        self._tip_hide()

    def _on_tree_motion(self, e):
        """One-line definition tooltips on column HEADERS — the old card-title tooltip sat where
        nobody hovers. Rows are left alone."""
        col = None
        if self.tree.identify_region(e.x, e.y) == 'heading':
            cid = self.tree.identify_column(e.x)
            try:
                col = self.tree['displaycolumns'][int(cid[1:]) - 1]
            except (ValueError, IndexError, tk.TclError):
                col = None
        if col == getattr(self, '_head_tip_col', None):
            return
        self._tip_hide()
        self._head_tip_col = col
        txt = self._HEAD_TIP.get(col) if col else None
        if txt:
            self._tip_xy = (e.x_root + 16, e.y_root + 18)
            self._tip_after = self.root.after(400, lambda: self._tip_show(txt))

    def _lb_head_text_for(self, select):
        scope = 'every alpha' if self._lb_mode == 'all' else 'best alpha per family'
        src = ('by TEST OOS — held-out, cherry-picked ⚠' if select == 'test'
               else 'by fitness min(train,val)')
        return f'LEADERBOARD — {scope}, {src}'

    def _sort_by(self, col):
        """Click a column header. 'fitness' and 'TEST OOS' also RE-SELECT the population from the
        WHOLE library by that metric (top-by-fitness vs top-by-held-out-TEST); ls/act/win/formula
        just reorder the current population. Repeat click on the same column toggles direction."""
        if col not in self._SORTABLE:
            return
        if col == self._sort_col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col, self._sort_desc = col, True
        self._update_headings()
        self._treesig = None                             # force a redraw in the new order
        select = col if col in ('fit', 'test') else self._lb_select
        if select != self._lb_select:                    # population key changed -> re-query the library
            self._lb_select = select
            self._lb_head_text = self._lb_head_text_for(select)
            self.lbl_lb_head.configure(text=self._lb_head_text)
            self._start_lb_compute(force=True)
        else:
            self._render_lb(self._lb_rows())

    def _start_lb_compute(self, force=False):
        lib = self._lib_file()
        if getattr(self, '_metrics_lib', None) != lib:   # another timeframe's library: its trade
            self._metrics_lib = lib                      # stats were computed in a different world
            self._metrics_cache = {}                     # (in-flight batches write to the old dict)
        try:
            mt = os.path.getmtime(lib)
        except OSError:
            return
        cache = self._lib_cache
        if cache['computing']:
            return
        if not force and mt == cache['mtime']:
            return
        cache['computing'] = True
        cache['ts'] = time.time()
        threading.Thread(target=self._compute_lb, args=(lib, mt), daemon=True).start()

    def _lb_rows(self):
        """The set the table should show for the current toggle: every alpha, or best-per-family."""
        c = self._lib_cache
        return c['families'] if self._lb_mode == 'families' else c['all']

    def _render_lb(self, best):
        self._fill_tree(best)

    def _refresh_leaderboard(self, status_best):
        """Into the table — EVERY alpha in the library (or best-per-family when the toggle is on).
        Read + sorted in the background and cached by mtime/population key: the whole library can be
        thousands of rows, and the family dedup is O(N²), so neither runs on the Tk thread."""
        cache = self._lib_cache
        now = time.time()
        stale_select = cache.get('select') != self._lb_select    # a header click changed the population key
        if not cache['computing'] and (stale_select or now - cache['ts'] > 6):
            self._start_lb_compute(force=stale_select)   # restart on file change / mode switch / period
        if cache['dirty']:
            cache['dirty'] = False
            self._treesig = None                         # force a redraw after recompute
            self._render_lb(self._lb_rows())
        elif not cache.get('computed'):
            self._fill_tree(status_best)                 # until computed — the top from the node (as before)

    def _compute_lb(self, path, mtime):
        """Load the WHOLE library, ranked by the active population key: 'fit' = fitness min(train,val)
        (honest), or 'test' = held-out TEST OOS (cherry-pick, chosen by clicking the TEST OOS header).
        Produces both sets — every alpha ('all') and best-per-family ('families') — so the toggle is
        instant. Row order in the table is set client-side in _sorted."""
        select = self._lb_select
        rows = []
        try:
            for line in open(path, encoding='utf-8'):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:             # a half-written last line while the node appends
                    pass
        except OSError:
            self._lib_cache['computing'] = False
            return
        if select == 'test':
            rows = [c for c in rows if self._LB_TESTKEY(c) is not None]
            rows.sort(key=self._LB_TESTKEY, reverse=True)        # top by held-out TEST OOS (cherry-pick)
        else:
            act = 'winrate' if self.cfg.get('opt_winrate') else 'sharpe'
            rows = [c for c in rows if c.get('base') is not None]
            # active objective first: winrate bases (<=1.0) and Sharpe bases (~1-2.5) are
            # different units — one raw ladder sinks every row of the other mode
            rows.sort(key=lambda c: ((c.get('fit_metric') or 'sharpe') != act,
                                     -c.get('base')))
        families = self._families(rows, self._lb_target)
        self._lib_cache.update(all=rows, families=families, mtime=mtime, select=select,
                               computing=False, dirty=True, computed=True)

    def _on_lb_query(self):
        self._lb_query = self.v_lb_q.get().strip().lower()
        self._treesig = None                             # the sig carries the query: force redraw
        self._render_lb(self._lb_rows())                 # instant — no waiting for the poll tick

    def _lb_filter(self, rows):
        """Live search: substring of the displayed 6-char id, the full library id, or the
        formula text — the same identifiers the table shows."""
        q = getattr(self, '_lb_query', '')
        if not q:
            return rows
        out = []
        for c in rows:
            f = c.get('formula') or ''
            aid = (hashlib.md5(f.encode()).hexdigest()[:6] if f
                   else str(c.get('id', ''))[:6])
            if q in f.lower() or q in aid.lower() or q in str(c.get('id', '')).lower():
                out.append(c)
        return out

    def _fill_tree(self, best):
        best = self._lb_filter(best)
        best = self._sorted(best)                        # order by the clicked column (no dedup — see the toggle)
        sig = (self._lb_mode, self._sort_col, self._sort_desc, len(best),
               getattr(self, '_lb_query', ''),
               (best[0].get('formula') or best[0].get('id', '')) if best else '')
        if getattr(self, '_treesig', None) == sig:
            return
        self._treesig = sig
        self._kill_cell_overlay(self.tree)               # rows are about to move under the overlay
        self._shown = best                               # for clicks: row -> champion
        top = self.tree.yview()[0] if self.tree.get_children() else 0.0   # keep the viewport across redraws
        for i in self.tree.get_children():
            self.tree.delete(i)
        self._row_items = {}
        if getattr(self, '_fav_ids', None) is None:      # None = re-read favorites.json
            self._fav_ids = favdb.ids(STATE_DIR)
        need = 0
        for i, c in enumerate(best):
            t = c.get('test') if isinstance(c.get('test'), dict) else {}
            ts = t.get('sharpe')                         # honest held-out OOS — tints the survivors
            base = c.get('base')
            stripe = 'odd' if i % 2 else 'even'
            tags = ('pos',) if (ts is not None and ts >= 0) else (stripe,)
            formula = c.get('formula', '')
            locked = bool(c.get('locked')) and not formula   # vault doc: metrics visible, text sealed
            if locked:
                aid = str(c.get('id', ''))[:6]
                f = '  ' + self._sm_locked_text(c) + ' — a subscription reveals the formula'
                m = 'err'                                # the worker can't simulate a sealed formula
            else:
                aid = hashlib.md5(formula.encode()).hexdigest()[:6]   # cell shows the tail only: the
                #                                                       'alpha_' prefix was 7 dead chars/row
                f = '  ' + formula                       # full length — the column fits the widest
                m = self._metrics_cache.get(formula)
            need = max(need, self._tree_font.measure(f))   # row; the two spaces are the gutter to
            #                                                the neighbour cell's right-flush value
            ls, bal, act, win, wup, wdn, srt, tup, tdn, tfl = self._fmt_metrics(m)
            dd = self._lb_test_ratio(c, m, 'dd', pct=True)
            cagr = self._lb_test_ratio(c, m, 'cagr', pct=True)
            fitcell = ('—' if base is None                        # win-rate-mined rows carry a
                       else f'{base * 100:.0f}%'                  # fit_metric tag: their 'base'
                       if c.get('fit_metric') == 'winrate'        # is a share, not a Sharpe
                       else f'{base:+.2f}')
            item = self.tree.insert('', 'end', values=(
                ('★' if aid in self._fav_ids else ''),
                i + 1, fitcell,
                f'{ts:+.2f}' if ts is not None else '—', dd, cagr, srt,
                tup, tdn, tfl, ls, bal, act, wup, wdn, aid, f),
                tags=tags)
            self._row_items[formula or ('id:' + aid)] = item
        self._lb_need_px = need + int(28 * self.SCALE)   # + cell padding / a breath of air
        self._fit_formula_col()
        if top:
            self.tree.yview_moveto(top)                  # a background recompute must not yank you to the top
        self.root.after_idle(self._pump_metrics)         # trade stats for what is actually on screen

    def _toggle_lb_mode(self):
        """All ↔ families. Both sets are already cached, so this is instant — just re-render."""
        self._lb_mode = 'families' if self.v_lbfam.get() else 'all'
        self.cfg['lb_mode'] = self._lb_mode
        self._save()
        self._lb_head_text = self._lb_head_text_for(self._lb_select)   # 'every alpha' vs 'best per family'
        self.lbl_lb_head.configure(text=self._lb_head_text)
        self._treesig = None                             # force a redraw with the other set
        self._render_lb(self._lb_rows())

    def _fit_formula_col(self):
        """Size the formula column to the widest row (full formulas, no ellipsis). Wider than
        the visible area -> the horizontal scrollbar takes over; narrower -> the column still
        stretches to fill the card. Re-run on every render and on tree resize."""
        try:
            fixed = sum(int(self.tree.column(c, 'width')) for c in self._lb_cols_fixed)
        except tk.TclError:                              # theme rebuild mid-flight
            return
        avail = self.tree.winfo_width() - fixed - int(4 * self.SCALE)
        w = max(self._lb_need_px, avail, int(260 * self.SCALE))
        try:                                             # height-only drags land here per pixel:
            if int(self.tree.column('formula', 'width')) == w:
                return                                   # unchanged width = nothing to relayout
        except tk.TclError:
            return
        self.tree.column('formula', width=w, stretch=False)

    def _on_tree_scroll(self, first, last):
        """Tk's yscrollcommand: keep the scrollbar in sync AND (debounced) load metrics for the rows
        that just scrolled into view. Trade stats are computed per viewport, not for the whole
        library — that is what keeps scrolling a 600-row table smooth."""
        self._vsb.set(first, last)
        if self._vis_after:
            try:
                self.root.after_cancel(self._vis_after)
            except (ValueError, tk.TclError):
                pass
        self._vis_after = self.root.after(140, lambda: self._start_metrics(self._visible_champs()))

    def _visible_champs(self):
        """The champions in (or just around) the current viewport — the batch whose trade stats we
        compute next. Bounded, so a not-yet-laid-out yview of (0,1) can't request the whole library."""
        n = len(self._shown)
        if not n:
            return []
        try:
            first, last = self.tree.yview()
        except tk.TclError:
            first, last = 0.0, 1.0
        lo = max(0, int(first * n) - 3)
        hi = min(n, int(round(last * n)) + 3)
        hi = min(hi, lo + 60)
        return self._shown[lo:hi]

    def _pump_metrics(self):
        """After a redraw: compute the visible rows' stats. Sorting BY a stat column is the one case
        that needs every value at once (else the order is wrong), so there we compute the full set."""
        if self._sort_col in ('ls', 'bal', 'act', 'wup', 'wdown', 'srt',
                              'tup', 'tdown', 'tflat', 'dd', 'cagr'):
            self._start_metrics(self._shown)
        else:
            self._start_metrics(self._visible_champs())

    @staticmethod
    def _fmt_ratio(v, pct=False):
        """'—' unless v is a finite number; percents rounded to whole, ratios to 2 decimals."""
        if not isinstance(v, (int, float)) or v != v or v in (float('inf'), float('-inf')):
            return '—'
        return f'{v * 100:+.0f}%' if pct else f'{v:+.2f}'

    def _lb_test_ratio(self, c, m, key, pct):
        """dd/cagr cell: the library row's own TEST metrics first (stored at mining time),
        the metrics worker's number as a fallback for old rows; '…' while it may still come."""
        t = c.get('test') if isinstance(c.get('test'), dict) else {}
        v = t.get(key)
        if not isinstance(v, (int, float)) and isinstance(m, dict):
            v = m.get(key)
        if not isinstance(v, (int, float)) and m is None:
            return '·'                                   # still computing — quieter than '…',
        return self._fmt_ratio(v, pct=pct)               # which read as truncated content

    @staticmethod
    def _fmt_winpct(v):
        return f'{v * 100:.0f}%' if isinstance(v, (int, float)) else '—'

    @staticmethod
    def _fmt_metrics(m):
        """('L/S', 'L/S %', 'tr/yr·a', 'win%', 'win↑', 'win↓', 'sortino', 'T↑', 'T↓', 'T~')
        strings from the cache: None=still computing, 'err'=failed."""
        if m is None:
            return ('·',) * 10                           # still computing (quiet placeholder)
        if m == 'err':
            return ('—',) * 10
        a = m.get('act', 0.0)
        astr = f'{a:.1f}' if a < 10 else f'{a:.0f}'
        if m.get('long_yr') is not None and m.get('short_yr') is not None:
            ls = f'{m["long_yr"]:.1f}/{m["short_yr"]:.1f}'   # entries / asset / year, by side
        else:                                            # a cache doc from an older worker
            ls = f'{m["long"]:.0f}/{m["short"]:.0f}'
        net = m.get('net')
        if isinstance(net, (int, float)):                # net/gross in [-1,+1] -> long/short %
            bal = f'{(1 + net) * 50:.0f}/{(1 - net) * 50:.0f}'
        else:
            bal = '—'                                    # a cache doc from an older worker
        return (ls, bal, astr, f'{m["win"] * 100:.0f}%',
                App._fmt_winpct(m.get('wup')), App._fmt_winpct(m.get('wdown')),
                App._fmt_ratio(m.get('sortino')), App._fmt_ratio(m.get('tup')),
                App._fmt_ratio(m.get('tdown')), App._fmt_ratio(m.get('tflat')))

    def _start_metrics(self, champs):
        """Background computation of long/short/win (on TEST) for the shown alphas; cached by formula."""
        todo = [c for c in champs if c.get('formula') and c['formula'] not in self._metrics_cache]
        if not todo:
            return
        self._metrics_seq += 1
        seq = self._metrics_seq
        threading.Thread(target=self._compute_metrics, args=(todo, seq), daemon=True).start()

    def _compute_metrics(self, champs, seq):
        with self._metrics_lock:
            todo = [c['formula'] for c in champs
                    if not isinstance(self._metrics_cache.get(c.get('formula', '')), dict)]
            try:
                if todo and seq == self._metrics_seq:
                    got = self._run_metrics_worker(todo)
                    for f in todo:
                        self._metrics_cache[f] = got.get(f, 'err')
            except Exception:                            # noqa: BLE001  (no data/config — quietly)
                for c in champs:
                    self._metrics_cache.setdefault(c.get('formula', ''), 'err')
            finally:
                try:
                    self.root.after(0, lambda s=seq: self._apply_metrics(s))
                except (RuntimeError, tk.TclError):      # window already closed / no loop
                    pass

    def _run_metrics_worker(self, formulas):
        """Hand the batch to a child process and wait on its pipe.

        This is the whole point of the worker: the computation is GIL-bound Python, and running it
        in-process (thread or not) starves the Tk main loop — status polls went from ~14ms to
        ~800ms and the window stalled while the table filled in. Waiting on a pipe releases the GIL.
        Returns {formula: stats|'err'}; raises if the worker could not produce a batch at all."""
        c = self.cfg
        payload = {
            'formulas': list(formulas),
            'instruments': (_parse_universe(c.get('universe_list', '')) or None),
            'vol': float(c.get('target_vol', 0.25)), 'exec': float(c.get('exec_cost', 0.001)),
            'train_start': _seg_res(self._tf(), 'train_start', c.get('train_start')),
            'test_start': _seg_res(self._tf(), 'test_start', c.get('test_start')),
            'test_end': _seg_res(self._tf(), 'test_end', c.get('test_end')),
            #                                            a frozen entry pins the real dates
        }
        env = dict(os.environ)
        env.update(ALPHANODE_STATE_DIR=STATE_DIR, ALPHANODE_DATA=self._data_file(),
                   ALPHANODE_TF=self._tf(),
                   ALPHANODE_CONFIG_INI=apppaths.config_ini())
        proc = subprocess.Popen(_child_cmd('metrics'), env=env,
                                cwd=(apppaths.USER_DIR if apppaths.FROZEN else PROJ),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL,
                                text=True, encoding='utf-8', errors='replace')
        self._metrics_proc = proc
        out, _ = proc.communicate(json.dumps(payload), timeout=600)
        doc = json.loads(out.strip().splitlines()[-1])   # the engine may print warnings first
        if not doc.get('ok'):
            raise RuntimeError(doc.get('error', 'metrics worker failed'))
        bars = doc.get('trend_bars')
        if isinstance(bars, dict) and bars != getattr(self, '_trend_bars', None):
            self._trend_bars = bars                      # worker thread -> headers on the main one
            self.root.after(0, self._apply_trend_bars)
        return doc.get('metrics') or {}

    def _apply_trend_bars(self):
        """Stamp the TEST bucket sizes into the T↑/T↓/T~ headers ('T ↑ ·196') and their
        tooltips. ONE number per column, not per cell: the split is the market's calendar,
        identical for every row — a per-cell count would repeat itself all the way down."""
        bars = getattr(self, '_trend_bars', None)
        if not isinstance(bars, dict):
            return
        if not hasattr(self, '_trend_tip_base'):
            self._trend_tip_base = {c: self._HEAD_TIP[c] for c in ('tup', 'tdown', 'tflat')}
        for col, key, base in (('tup', 'up', 'T ↑'), ('tdown', 'down', 'T ↓'),
                               ('tflat', 'flat', 'T ~')):
            n = bars.get(key)
            if isinstance(n, int):
                self._HEAD[col] = f'{base} ·{n}'
                self._HEAD_TIP[col] = (self._trend_tip_base[col] +
                                       f'\nBars in this bucket on TEST: {n} — the sample size\n'
                                       'behind every number in the column.')
        self._update_headings()

    def _apply_metrics(self, seq):
        """Set the computed metric cells into the already shown rows (main thread). The paint is
        UNCONDITIONAL — values come from the cache and repainting is idempotent — because batches
        finish out of order around rebuilds/scrolls: gate the paint on seq and a stale batch's
        results are dropped with no later batch coming, freezing the columns at '…' until the
        user pokes one. Only the follow-up work belongs to the newest batch."""
        for formula, item in list(self._row_items.items()):
            if not self.tree.exists(item):
                continue
            if formula.startswith('id:'):                # locked row: no plaintext to simulate —
                continue                                 # leave its '—' cells, don't repaint to '·'
            m = self._metrics_cache.get(formula)
            ls, bal, act, win, wup, wdn, srt, tup, tdn, tfl = self._fmt_metrics(m)
            self.tree.set(item, 'ls', ls)
            self.tree.set(item, 'bal', bal)
            self.tree.set(item, 'act', act)
            self.tree.set(item, 'wup', wup)
            self.tree.set(item, 'wdown', wdn)
            self.tree.set(item, 'srt', srt)
            self.tree.set(item, 'tup', tup)
            self.tree.set(item, 'tdown', tdn)
            self.tree.set(item, 'tflat', tfl)
            if isinstance(m, dict):                      # dd/cagr fallback for legacy library rows
                for col in ('dd', 'cagr'):
                    if self.tree.set(item, col) in ('·', '—'):
                        self.tree.set(item, col, self._fmt_ratio(m.get(col), pct=True))
        if seq != self._metrics_seq:
            return
        if self._sort_col in ('ls', 'bal', 'act', 'wup', 'wdown', 'srt',
                              'tup', 'tdown', 'tflat', 'dd', 'cagr'):
            self._treesig = None
            self._render_lb(self._lb_rows() or self._shown)
        elif any(c.get('formula') and c['formula'] not in self._metrics_cache
                 for c in self._visible_champs()):
            self.root.after_idle(self._pump_metrics)     # a stale batch skipped its slice while we
        #                                                  scrolled past — pick the viewport back up

    # ---------- equity chart on click (TRAIN|VAL|TEST + B&H) ----------
    def _on_row_open(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        bx = self.tree.bbox(item, '#1')                  # the ★ strip is a button, not data
        if bx and bx[0] <= event.x < bx[0] + bx[2]:
            return
        idx = self.tree.index(item)
        if 0 <= idx < len(self._shown):
            self._open_plot(self._shown[idx])

    # ---------- copy formula ----------
    def _selected_champ(self):
        item = self.tree.focus() or (self.tree.selection()[0] if self.tree.selection() else '')
        if not item:
            return None
        idx = self.tree.index(item)
        return self._shown[idx] if 0 <= idx < len(self._shown) else None

    def _on_row_menu(self, event):
        if (self._cols_menu is not None
                and self.tree.identify_region(event.x, event.y) == 'heading'):
            try:                                         # header right-click: the Columns picker
                self._cols_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self._cols_menu.grab_release()
            return
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.tree.focus(item)
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _flash_lb(self, msg, ms=1300):
        """Say something in the leaderboard heading, then put the heading back."""
        self.lbl_lb_head.configure(text=msg)
        self.root.after(ms, lambda: self.lbl_lb_head.configure(text=self._lb_head_text))

    def _to_clipboard(self, text, msg):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()                               # so the buffer is handed to the X server right away
        self._flash_lb(msg)

    # ---------- mouse selection & copy-as-text ----------
    def _text_selectable(self, box):
        """A read-only log/text view the mouse can work: I-beam cursor, click focuses it
        (a disabled Text won't always take focus on click), Ctrl+C copies the selection."""
        t = getattr(box, '_textbox', box)                # CTkTextbox wraps a real tk.Text
        try:
            t.configure(cursor='xterm', selectbackground=TXT, selectforeground=CARD)
        except Exception:                                # noqa: BLE001 — cosmetics only
            pass
        t.bind('<Button-1>', lambda _e: t.focus_set(), add='+')

        def copy(_e=None):
            try:
                sel = t.get('sel.first', 'sel.last')
            except Exception:                            # noqa: BLE001 — nothing selected
                return 'break'
            if sel:
                self.root.clipboard_clear()
                self.root.clipboard_append(sel)
                self.root.update()                       # hand the buffer to X right away
            return 'break'
        t.bind('<Control-c>', copy)
        t.bind('<Control-C>', copy)
        t._copy_sel = copy                               # the tests drive this directly
        return t

    def _selectable_cells(self, tree):
        """Real text selection for a ttk.Treeview: a click parks a borderless read-only Entry
        over the cell, full text pre-selected — drag over it, Ctrl+C, done. It waits out the
        double-click window so double-click actions still fire; Esc, scroll, another click or
        a table rebuild dismisses it."""
        def cancel():
            job = getattr(tree, '_cell_job', None)
            if job is not None:
                tree._cell_job = None
                try:
                    tree.after_cancel(job)
                except Exception:                        # noqa: BLE001
                    pass

        def kill(_e=None):
            cancel()
            ov = getattr(tree, '_cell_ov', None)
            if ov is not None:
                tree._cell_ov = None
                try:
                    ov.destroy()
                except Exception:                        # noqa: BLE001
                    pass

        def show(item, col):
            tree._cell_job = None
            try:
                bx = tree.bbox(item, col)
                txt = str(tree.set(item, col)).strip()
            except Exception:                            # noqa: BLE001 — row gone mid-flight
                return
            if not bx or not txt:
                return
            x, y, w, h = bx
            w = max(w, min(self._tree_font.measure(txt) + int(24 * self.SCALE),
                           tree.winfo_width() - x - 4))
            ov = tk.Entry(tree, font=self._tree_font, relief='flat', bd=0,
                          highlightthickness=1, highlightcolor=ACC, highlightbackground=ACC,
                          readonlybackground=ACC_SOFT, fg=TXT, insertbackground=TXT,
                          selectbackground=TXT, selectforeground=CARD)
            ov.insert(0, txt)
            ov.configure(state='readonly')
            ov.place(x=x, y=y, width=w, height=h)
            ov.selection_range(0, 'end')
            ov.icursor('end')
            ov.focus_set()
            ov.bind('<Escape>', lambda _e: (kill(), tree.focus_set()))
            ov.bind('<FocusOut>', kill)
            tree._cell_ov = ov

        def click(e):
            kill()
            if tree.identify('region', e.x, e.y) != 'cell':
                return
            item, col = tree.identify_row(e.y), tree.identify_column(e.x)
            if item and col:                             # past the double-click window, so a
                tree._cell_job = tree.after(400, lambda: show(item, col))   # double still fires

        tree._cell_ov = None
        tree._cell_job = None
        tree.bind('<Button-1>', click, add='+')
        tree.bind('<B1-Motion>', lambda _e: cancel(), add='+')   # a drag means rows, not text
        tree.bind('<Double-1>', lambda _e: kill(), add='+')
        tree.bind('<Button-3>', lambda _e: kill(), add='+')
        for seq in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            tree.bind(seq, lambda _e: kill(), add='+')
        tree._cell_show = show                           # the tests drive these directly
        tree._cell_kill = kill

    @staticmethod
    def _kill_cell_overlay(tree):
        k = getattr(tree, '_cell_kill', None)
        if k:
            k()

    def _copy_formula(self):
        c = self._selected_champ()
        if c and c.get('formula'):
            self._to_clipboard(c['formula'], '✓ formula copied to clipboard')

    def _copy_full(self):
        c = self._selected_champ()
        if not c:
            return

        def sh(seg):
            v = (c.get(seg) or {}).get('sharpe')
            return f'{v:+.2f}' if v is not None else '—'
        txt = (f"{c.get('formula', '')}\n"
               f"fitness(base)={c.get('base')}{' (win rate)' if c.get('fit_metric') == 'winrate' else ''}  train={sh('train')}  val={sh('val')}  TEST(OOS)={sh('test')}")
        self._to_clipboard(txt, '✓ formula + metrics copied')

    # ---------- favorites (starred formulas) ----------
    def _on_lb_star(self, e):
        """A click inside the ★ cell toggles the row's star. Hit-testing goes through bbox,
        NOT identify_column: identify's column ranges sit a few px off the drawn cells, so a
        click in the neighbour cell's first pixels reads as '#1' and silently unstars.
        'break' keeps the cell overlay and the row-select press away from a button strip."""
        item = self.tree.identify_row(e.y)
        if not item:
            return None
        try:
            bx = self.tree.bbox(item, '#1')              # 'fav' is always the first display column
        except Exception:                                # noqa: BLE001 — row vanished mid-click
            return None
        if not bx or not (bx[0] <= e.x < bx[0] + bx[2]):
            return None
        idx = self.tree.index(item)
        if not (0 <= idx < len(self._shown)):
            return None
        self._fav_toggle(self._shown[idx])
        return 'break'

    def _fav_toggle(self, c):
        formula = c.get('formula') or ''
        if not formula:
            self._flash_lb('locked formulas can\'t be starred — activate to reveal them first')
            return
        _favs, added = favdb.toggle(STATE_DIR, c, self._tf())
        self._fav_ids = None                             # repaint re-reads favorites.json
        self._treesig = None
        self._render_lb(self._lb_rows() or self._shown)
        self._flash_lb('★ saved to favorites' if added else '☆ removed from favorites')

    def _fav_toggle_selected(self):
        c = self._selected_champ()
        if c:
            self._fav_toggle(c)

    def _open_favorites(self):
        """The starred list: same gestures as the leaderboard — double-click an equity
        chart, click a cell for selectable text, Ctrl+C / a button copies the formula."""
        win = self._dialog('AlphaNode — Favorites', f'{int(940 * self.SCALE)}x{int(470 * self.SCALE)}')
        pad = tk.Frame(win, bg=CARD)
        pad.pack(fill='both', expand=True, padx=14, pady=12)
        self._head(pad, 'FAVORITES — your starred formulas').pack(anchor='w')
        self._lbl(pad, text='Stored outside the library: stars survive \'Clear all history\' and '
                            'session loads. Double-click a row — its equity chart, like the leaderboard.',
                  text_color=MUT, font=(self.UI, 12)).pack(anchor='w', pady=(2, 8))
        cols = ('added', 'id', 'tf', 'fit', 'test', 'formula')
        tv = ttk.Treeview(pad, columns=cols, show='headings', height=12)
        for c, txt, w, anc in (('added', 'ADDED', 100, 'center'), ('id', 'ID', 72, 'center'),
                               ('tf', 'TF', 50, 'center'), ('fit', 'FITNESS', 84, 'e'),
                               ('test', 'TEST OOS', 84, 'e'), ('formula', 'FORMULA', 420, 'w')):
            tv.heading(c, text=txt)
            tv.column(c, width=int(w * self.SCALE), anchor=anc, stretch=(c == 'formula'))
        tv.pack(fill='both', expand=True)
        self._selectable_cells(tv)

        rows = {}

        def fill():
            rows.clear()
            tv.delete(*tv.get_children())
            for f in favdb.load(STATE_DIR):
                t = f.get('test') if isinstance(f.get('test'), dict) else {}
                ts, base = t.get('sharpe'), f.get('base')
                fitc = ('—' if not isinstance(base, (int, float))
                        else f'{base * 100:.0f}%' if f.get('fit_metric') == 'winrate'
                        else f'{base:+.2f}')
                iid = tv.insert('', 'end', values=(
                    f.get('added', '—'), favdb.alpha_id(f['formula']), f.get('tf', '—'),
                    fitc,
                    f'{ts:+.2f}' if isinstance(ts, (int, float)) else '—',
                    '  ' + f['formula']))
                rows[iid] = f
            n = len(tv.get_children())
            note.configure(text=(f'{n} starred' if n else
                                 'no favorites yet — click the ★ cell on a leaderboard row'))

        def picked():
            sel = tv.focus() or (tv.selection()[0] if tv.selection() else '')
            return rows.get(sel)

        def open_plot(_e=None):
            f = picked()
            if f:
                self._open_plot(f)

        def copy():
            f = picked()
            if f:
                self._to_clipboard(f['formula'], '✓ formula copied to clipboard')

        def unstar(_e=None):
            f = picked()
            if not f:
                return
            favdb.remove(STATE_DIR, favdb.alpha_id(f['formula']))
            self._fav_ids = None                         # the leaderboard drops the star too
            self._treesig = None
            self._render_lb(self._lb_rows() or self._shown)
            fill()

        tv.bind('<Double-1>', open_plot, add='+')
        tv.bind('<Delete>', unstar)
        tv.bind('<Control-c>', lambda _e: copy())
        tv.bind('<Control-C>', lambda _e: copy())
        row = self._box(pad)
        row.pack(fill='x', pady=(10, 0))
        note = self._lbl(row, text='', text_color=FAINT, font=(self.UI, 11))
        note.pack(side='left')
        self._btn(row, 'Close', win.destroy, kind='soft', height=30, width=80).pack(side='right')
        self._btn(row, 'Remove ☆', unstar, kind='soft', height=30,
                  width=100).pack(side='right', padx=(0, 10))
        self._btn(row, 'Copy formula', copy, kind='soft', height=30,
                  width=120).pack(side='right', padx=(0, 10))
        self._btn(row, 'Show equity', open_plot, height=30,
                  width=110).pack(side='right', padx=(0, 10))
        fill()
        win._tv, win._fill, win._unstar, win._open, win._rows = tv, fill, unstar, open_plot, rows
        self._fav_win = win                              # the tests drive these directly
        return win

    def _open_selected_plot(self):
        c = self._selected_champ()
        if c:
            self._open_plot(c)

    # ---------- LEADERBOARD: download the table ----------
    @staticmethod
    def _seg(champ, seg, key):
        """One TRAIN/VAL/TEST number, or '' — an alpha may be missing a segment entirely."""
        v = (champ.get(seg) if isinstance(champ.get(seg), dict) else {}).get(key)
        return round(v, 4) if isinstance(v, (int, float)) else ''

    def _save_csv(self, path, header, rows, what):
        """csv.writer, not a join: every formula is full of commas (div(pmin(a,b),c)) and would
        otherwise split into columns. newline='' is what the csv module requires of its file."""
        try:
            with open(path, 'w', newline='', encoding='utf-8') as fh:
                w = csv.writer(fh)
                w.writerow(header)
                w.writerows(rows)
        except OSError as e:
            messagebox.showerror('Export', f'Could not write {path}:\n{e}', parent=self.root)
            return
        self._flash_lb(f'✓ {len(rows)} {what} saved to {os.path.basename(path)}', ms=2600)

    def _export_visible(self):
        """The table as shown: same rows, same order, same columns. The trade stats come from the
        metrics cache, which only ever holds the rows on screen — hence the two separate exports."""
        rows = list(self._shown)
        if not rows:
            messagebox.showinfo('Export table', 'The leaderboard is empty — nothing to export yet.',
                                parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title='Export leaderboard', defaultextension='.csv',
            initialfile=f'leaderboard_top{len(rows)}.csv',
            filetypes=[('CSV', '*.csv'), ('All files', '*.*')])
        if not path:
            return
        out = []
        for i, c in enumerate(rows):
            formula = c.get('formula', '')
            locked = bool(c.get('locked')) and not formula
            # locked rows: the doc's real public id, a 'locked' formula cell — never the
            # empty-string md5 that would collide every locked row onto one bogus id
            aid = str(c.get('id', ''))[:6] if locked else hashlib.md5(formula.encode()).hexdigest()[:6]
            fcell = 'locked (subscription reveals)' if locked else formula
            m = self._metrics_cache.get(formula)
            m = m if isinstance(m, dict) else {}          # None = still computing, 'err' = failed -> blanks
            base = c.get('base')
            out.append([i + 1,
                        round(base, 4) if isinstance(base, (int, float)) else '',
                        c.get('fit_metric', ''),
                        self._seg(c, 'train', 'sharpe'), self._seg(c, 'val', 'sharpe'),
                        self._seg(c, 'test', 'sharpe'), self._seg(c, 'test', 'dd'),
                        self._seg(c, 'test', 'cagr'),
                        round(m['sortino'], 3) if isinstance(m.get('sortino'), (int, float)) else '',
                        round(m['tup'], 3) if isinstance(m.get('tup'), (int, float)) else '',
                        round(m['tdown'], 3) if isinstance(m.get('tdown'), (int, float)) else '',
                        round(m['tflat'], 3) if isinstance(m.get('tflat'), (int, float)) else '',
                        m.get('long', ''), m.get('short', ''),
                        round(m['long_yr'], 2) if isinstance(m.get('long_yr'), (int, float)) else '',
                        round(m['short_yr'], 2) if isinstance(m.get('short_yr'), (int, float)) else '',
                        round(m['act'], 2) if 'act' in m else '',
                        round(m['net'], 3) if isinstance(m.get('net'), (int, float)) else '',
                        round(m['win'] * 100, 1) if 'win' in m else '',
                        round(m['wup'] * 100, 1) if isinstance(m.get('wup'), (int, float)) else '',
                        round(m['wdown'] * 100, 1) if isinstance(m.get('wdown'), (int, float)) else '',
                        aid, fcell])
        self._save_csv(path, ('rank', 'fitness', 'fit_metric', 'train_sharpe', 'val_sharpe', 'test_sharpe',
                              'test_dd', 'test_cagr', 'test_sortino',
                              'test_sh_trend_up', 'test_sh_trend_down',
                              'test_sh_flat', 'long', 'short', 'long_yr_a', 'short_yr_a',
                              'tr_yr_a', 'net_over_gross', 'win_pct', 'call_acc_up_pct',
                              'call_acc_down_pct',
                              'id', 'formula'), out, 'rows')

    def _export_library(self):
        """Everything the node has ever mined — no dedup, no TEST filter. The table on screen is a
        diverse SLICE of this; here you get all of it, ordered by honest fitness min(train,val)."""
        rows = []
        try:
            with open(self._lib_file(), encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:          # a half-written last line while the node appends
                        pass
        except OSError:
            rows = []
        if not rows:
            messagebox.showinfo('Export library', 'The library is empty — the node has not mined '
                                                  'anything yet.', parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title='Export full library', defaultextension='.csv',
            initialfile=f'library_{len(rows)}.csv',
            filetypes=[('CSV', '*.csv'), ('All files', '*.*')])
        if not path:
            return
        rows.sort(key=lambda c: c.get('base') if isinstance(c.get('base'), (int, float)) else -1e9,
                  reverse=True)
        header = ['id', 'formula', 'size', 'fitness', 'round', 'found_at', 'origin']
        for seg in ('train', 'val', 'test'):
            header += [f'{seg}_{k}' for k in ('sharpe', 'dd', 'cagr', 'n')]
        out = []
        for c in rows:
            base = c.get('base')
            f = c.get('formula', '')
            # locked docs (vault mode) have no plaintext — export the public id, mark the cell
            aid = c.get('id') or hashlib.md5(f.encode()).hexdigest()[:12]
            fcell = f if f else 'locked (subscription reveals)'
            r = [aid, fcell, c.get('size', ''),
                 round(base, 4) if isinstance(base, (int, float)) else '',
                 c.get('round', ''), c.get('ts', ''), c.get('origin', 'ga')]
            for seg in ('train', 'val', 'test'):
                r += [self._seg(c, seg, k) for k in ('sharpe', 'dd', 'cagr', 'n')]
            out.append(r)
        self._save_csv(path, header, out, 'alphas')

    # ---------- PORTFOLIO: combine top-N (1d: real engine; intraday: fastsim, same combiner) ----------
    def _build_portfolio(self):
        if self._pf_proc and self._pf_proc.poll() is None:
            return                                       # already building
        # ttk.Spinbox enforces from_/to on its ARROWS only — a typed 150 reads back as 150,
        # and went to the builder unchallenged. Two things then went quietly wrong: 'combo'
        # caps its search pool at 30, so asking for a combination of anything above that
        # silently stopped searching and took the whole pool ('pool has only 30 distinct
        # alphas'), and the build simply took as long as the library was deep. Clamp to the
        # advertised range and say so in the status line rather than in a modal.
        raw = self._gi(self.v_pfn, 6)
        n = max(self.PF_TOP_MIN, min(self.PF_TOP_MAX, raw))
        clamped = '' if n == raw else (f'  ·  top {raw} is outside {self.PF_TOP_MIN}–'
                                       f'{self.PF_TOP_MAX} — building {n}')
        if clamped:
            self.v_pfn.set(n)                            # the box shows what is being built
        sel = {'fitness': 'base', 'combo': 'combo'}.get(self.v_pfsel.get(), 'test')
        eng = ('real engine, ~1–2 min' if self._tf() == '1d'
               else f'{self._tf()} fastsim, ~seconds')
        if sel == 'combo':
            eng = (f'pool of ~{min(max(4 * n, 12), 30)} on the ' + eng
                   + ' each — grab a coffee' if self._tf() == '1d'
                   else eng + ', pool included')
        self.btn_pf.configure(state='disabled')
        self.lbl_pf_m.configure(text='', fg=MUT)
        self.lbl_pf.configure(text=(
            f'searching the best combination of {n} on TRAIN+VAL ({eng})…' if sel == 'combo'
            else f'building portfolio from top-{n} by '
                 f'{"TEST" if sel == "test" else "fitness min(train,val)"} ({eng})…') + clamped)
        env = dict(os.environ)
        env.update(ALPHANODE_STATE_DIR=STATE_DIR, ALPHANODE_DATA=self._data_file(),
                   ALPHANODE_TF=self._tf(),
                   ALPHANODE_CONFIG_INI=apppaths.config_ini(),
                   # combine on the SAME universe the search optimizes — not all of data.pickle
                   ALPHANODE_UNIVERSE=self.cfg.get('universe_list',
                                                   DEFAULTS['universe_list']),
                   # the GUI's date fields, node.py-style — the ini only has daily defaults
                   ALPHANODE_TRAIN_START=self.cfg.get('train_start', ''),
                   ALPHANODE_VAL_START=self.cfg.get('val_start', ''),
                   ALPHANODE_TEST_START=self.cfg.get('test_start', ''),
                   ALPHANODE_TEST_END=self.cfg.get('test_end', ''))
        try:
            self._pf_proc = subprocess.Popen(
                _child_cmd('portfolio') + ['--top', str(n), '--select', sel,
                                           '--out', PORTFOLIO_JSON], env=env,
                cwd=(apppaths.USER_DIR if apppaths.FROZEN else PROJ),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace')
        except Exception as e:                           # noqa: BLE001
            self.lbl_pf.configure(text=f'could not start portfolio build: {e}')
            self.btn_pf.configure(state='normal')
            return
        threading.Thread(target=self._pf_reader, args=(self._pf_proc,), daemon=True).start()

    def _pf_reader(self, proc):
        for line in iter(proc.stdout.readline, ''):
            line = line.strip()
            if line:
                self.root.after(0, lambda s=line: self.lbl_pf.configure(text=s))
        proc.wait()
        self.root.after(0, self._portfolio_done)

    def _portfolio_done(self):
        self.btn_pf.configure(state='normal')
        try:
            doc = json.load(open(PORTFOLIO_JSON, encoding='utf-8'))
        except Exception as e:                           # noqa: BLE001
            self.lbl_pf.configure(text=f'portfolio build did not finish: {e}')
            return
        self._render_portfolio(doc)

    def _render_portfolio(self, doc):
        if not doc.get('ok'):
            self.lbl_pf.configure(text='portfolio build failed: ' + str(doc.get('error', ''))[:120])
            return
        self._pf_doc = doc                               # remember for re-render on resize
        self.btn_pf_csv.configure(state=('normal' if doc.get('weights') else 'disabled'))
        self.btn_pf_sig.configure(state=('normal' if doc.get('formulas_full') else 'disabled'))
        self.btn_pf_pdf.configure(state=('normal' if doc.get('weights') else 'disabled'))
        self.btn_pf_track.configure(state=('normal' if doc.get('formulas_full') else 'disabled'))
        m = doc.get('metrics') or {}
        b = doc.get('basket') or {}
        if doc.get('sel') == 'base':                     # selection never saw TEST
            note = 'TEST held out of selection — the numbers below are honest OOS'
            picked = 'by fitness min(train,val)'
        elif doc.get('sel') == 'combo':                  # searched on TRAIN+VAL only
            cb = doc.get('combo') or {}
            obj = cb.get('obj_tv')
            obj_s = f'{obj:+.2f}' if isinstance(obj, (int, float)) else '?'
            note = (f'best combination from a pool of {cb.get("pool", "?")} '
                    f'(TRAIN+VAL Sharpe {obj_s} over '
                    f'{cb.get("evals", "?")} mixes) — TEST never entered the search: honest OOS')
            picked = 'as the best combination'
        else:                                            # 'test' or legacy docs without 'sel'
            note = ('⚠ members picked by TEST — its numbers are optimistic (cherry-pick); '
                    'validate on the forward track')
            picked = 'by TEST OOS'
        if doc.get('sealed'):                            # vault: cached return series, no plaintext
            eng = 'cached return series (sealed preview — activate to build with the real engine)'
        else:
            eng = 'the real engine' if doc.get('tf', '1d') == '1d' else f'fastsim on {doc["tf"]} bars'
        span = (f'TRAIN+VAL+TEST {doc["span"]}' if doc.get('span')
                else f'TEST {doc.get("test", "")}')     # docs built before the full-span change
        self.lbl_pf.configure(text=f'top-{doc.get("n")} {picked} combined via {eng}  ·  '
                                f'{span}  ·  built in '
                                f'{doc.get("built_secs", "?")}s  ·  {note}')
        sh = m.get('sharpe')
        segs = doc.get('segments') or {}
        if segs:
            def _s(name):
                s = (segs.get(name) or {}).get('sharpe')
                return f'{s:+.2f}' if s is not None else '—'
            text = (f'Sharpe  TRAIN {_s("train")} · VAL {_s("val")} · TEST {_s("test")}   ·   '
                    f'TEST: CAGR {m.get("cagr", 0) * 100:+.0f}%  MaxDD {m.get("dd", 0) * 100:.0f}%'
                    f'      (vs buy&hold TEST {b.get("sharpe", 0):+.2f})')
        else:
            text = (f'Sharpe {sh:+.2f}   ·   CAGR {m.get("cagr", 0) * 100:+.0f}%   ·   '
                    f'MaxDD {m.get("dd", 0) * 100:.0f}%      (vs buy&hold Sharpe {b.get("sharpe", 0):+.2f})')
        self.lbl_pf_m.configure(text=text, fg=(POS if (sh is not None and sh >= 0) else NEG))
        self._fill_pf_members(doc, sh)
        threading.Thread(target=self._render_pf_equity, args=(doc, self._pf_width()),
                         daemon=True).start()

    def _fill_pf_members(self, doc, combined_sharpe=None):
        """One row per member: pick order, id, how it does ALONE on TEST, and the two numbers
        the leaderboard shows for the same alpha. FITNESS/TEST OOS are joined from the library
        by formula text — a member whose row has since been cleared shows dashes rather than
        disappearing, because it is still in the built portfolio either way.

        Rows whose SOLO Sharpe the combination beats are tinted: that is the diversification
        gain, and it is the reason to combine at all."""
        tree = getattr(self, 'pf_tree', None)
        if tree is None:
            return
        tree.delete(*tree.get_children())
        solo = doc.get('indiv_sharpe') or []
        if doc.get('sealed'):                            # vault preview: the doc carries each
            members = [(str(mm.get('id', ''))[:6], mm,   # member's id + stored metrics, no text
                        '  ' + self._sm_locked_text(mm))
                       for mm in (doc.get('members') or [])]
        else:
            lib = {c['formula']: c for c in (self._lib_cache.get('all') or []) if c.get('formula')}
            members = [(hashlib.md5(f.encode()).hexdigest()[:6], lib.get(f) or {}, '  ' + f)
                       for f in (doc.get('formulas_full') or doc.get('formulas') or [])]
        for i, (aid, c, text) in enumerate(members):
            base = c.get('base')
            t = c.get('test') if isinstance(c.get('test'), dict) else {}
            ts = t.get('sharpe')
            s0 = solo[i] if i < len(solo) else None
            beaten = (isinstance(s0, (int, float)) and isinstance(combined_sharpe, (int, float))
                      and combined_sharpe > s0)
            tree.insert('', 'end', values=(
                i + 1, aid,
                f'{s0:+.2f}' if isinstance(s0, (int, float)) else '—',
                ('—' if base is None else f'{base * 100:.0f}%'
                 if c.get('fit_metric') == 'winrate' else f'{base:+.2f}'),
                f'{ts:+.2f}' if isinstance(ts, (int, float)) else '—',
                text),
                tags=(('pos',) if beaten else ('odd' if i % 2 else 'even',)))
        # The table sizes itself to the membership so the usual top-6 needs no scrolling —
        # but only up to PF_ROWS_MAX. 'top' does not enforce its own 2–20 range on a TYPED
        # value, and an unbounded height turned a 150-member build into a card eight thousand
        # pixels tall (measured: 8,462px against a 2,466px screen).
        tree.configure(height=max(1, min(len(members), self.PF_ROWS_MAX)))
        if len(members) > self.PF_ROWS_MAX:
            self._pf_vsb.pack(side='right', fill='y', padx=(4, 0))
        else:
            self._pf_vsb.pack_forget()

    def _pf_member_plot(self, _e=None):
        """Double-click a member -> its own equity chart, the same one the leaderboard opens.
        Only possible while the library still holds the row: the chart is re-simulated from
        the champion doc, and a portfolio outlives the library it was built from."""
        sel = self.pf_tree.selection()
        if not sel:
            return
        i = self.pf_tree.index(sel[0])
        members = (self._pf_doc or {}).get('formulas_full') or []
        if i >= len(members):
            return
        c = next((x for x in (self._lib_cache.get('all') or [])
                  if x.get('formula') == members[i]), None)
        if c is None:
            self._flash_lb('that member is no longer in the library — nothing to chart')
            return
        self._open_plot(c)

    def _pf_width(self):
        """Target equity-image width = current panel width (so it fills the space, expandable)."""
        w = self.pf_card.winfo_width()
        if w <= 1:                                       # not laid out yet
            w = self.tree.winfo_width() or 900
        return max(700, min(w - 34, 3400))

    def _pf_rerender(self, delay=250):
        """Debounced background re-render of the equity PNG at the current width/height."""
        if not self._pf_doc:
            return
        if self._pf_resize_after:
            self.root.after_cancel(self._pf_resize_after)
        self._pf_resize_after = self.root.after(
            delay, lambda: threading.Thread(target=self._render_pf_equity,
                                            args=(self._pf_doc, self._pf_width()), daemon=True).start())

    def _on_pf_resize(self, event):
        if not self._pf_doc:
            return
        if abs(self._pf_width() - self._pf_last_w) < 40:   # ignore tiny/noise resizes
            return
        self._pf_rerender()                              # debounce: re-render after resize settles

    def _on_pf_grip(self, e):
        """The in-card grip UNDER the plot (bottom, like every other card): pointer distance
        from the image top = new equity-plot height."""
        top = (self.pf_img.winfo_rooty() if self._pf_img_ref is not None
               else self.pf_card.winfo_rooty())           # no plot yet: measure from the card
        self.cfg['pf_h'] = max(160, min(800, e.y_root - top))
        self._pf_rerender(delay=180)                     # height is picked up by the render itself

    def _pf_grip_reset(self, _e=None):
        self.cfg['pf_h'] = 0
        self._save()
        self._pf_rerender(delay=0)

    def _embed_fig(self, parent, fig):
        """A LIVE matplotlib canvas inside Tk: the stock pan/zoom toolbar, wheel = zoom around
        the cursor (log-aware on log axes), double-click = reset view. Drawing happens on the
        main thread — the very reason the figure is built pyplot-free (no global state)."""
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

        class _FlatDPICanvas(FigureCanvasTkAgg):
            """CTk sets Tk's global 'scaling' from the FONT dpi; matplotlib's TkAgg reads that
            as a device PIXEL ratio (<Map> binding) and renders a buffer ~1.75x larger than its
            photo slot — the visible chart becomes a magnified crop. Tk windows here are 1x
            device pixels, so the ratio update is simply wrong: neutralize it at CLASS level
            (the Tk binding captures the bound method, instance patches don't intercept)."""
            def _update_device_pixel_ratio(self, event=None):
                pass

        canvas = _FlatDPICanvas(fig, master=parent)
        tb = NavigationToolbar2Tk(canvas, parent, pack_toolbar=False)
        try:                                              # theme the stock toolbar
            tb.configure(background=CARD)
            for ch in tb.winfo_children():
                try:
                    ch.configure(background=CARD)
                except tk.TclError:
                    continue
                if isinstance(ch, (tk.Button, tk.Checkbutton)):
                    # icons were rendered against the DEFAULT (light) Tk palette at toolbar
                    # creation; recolor the button first, then let mpl re-derive the icon —
                    # on a dark background it swaps in the foreground-tinted variant
                    ch.configure(foreground=TXT, activebackground=BORDER,
                                 activeforeground=TXT, highlightthickness=0)
                    if isinstance(ch, tk.Checkbutton):
                        ch.configure(selectcolor=BORDER)  # Pan/Zoom pressed state
                    if getattr(ch, '_image_file', None):
                        tb._set_image_for_button(ch)
                elif isinstance(ch, tk.Label):
                    ch.configure(foreground=MUT)
            # Tk paints DISABLED image-buttons with a checkerboard stipple (ugly, and RGBA
            # icons flatten to a light block). Back/Forward are safe no-ops on an empty
            # history, so keep them enabled instead of letting mpl gray them out.
            tb.set_history_buttons = lambda: None
            for k in ('Back', 'Forward'):
                if k in tb._buttons:
                    tb._buttons[k]['state'] = 'normal'
        except (tk.TclError, AttributeError):
            pass
        tb.update()
        tb.pack(anchor='w', fill='x')
        w = canvas.get_tk_widget()
        w.configure(background=CARD, highlightthickness=0)
        w.pack(fill='both', expand=True)

        def _zoomed(lo, hi, c, f, log):
            if log and lo > 0 and c > 0:
                import math
                l0, l1, lc = math.log10(lo), math.log10(hi), math.log10(c)
                return 10 ** (lc - (lc - l0) * f), 10 ** (lc + (l1 - lc) * f)
            return c - (c - lo) * f, c + (hi - c) * f

        def on_scroll(ev):
            # every axes under the cursor, each in ITS OWN data coords — a twinx sibling
            # (e.g. library size on the progress chart) must zoom its own y scale too;
            # a shared x-axis is zoomed ONCE per group or twins would compound to f²
            f = 1 / 1.25 if ev.button == 'up' else 1.25
            hit, done_x = False, []
            for ax in canvas.figure.axes:
                if not ax.in_axes(ev):
                    continue
                xd, yd = ax.transData.inverted().transform((ev.x, ev.y))
                if not any(ax.get_shared_x_axes().joined(ax, o) for o in done_x):
                    ax.set_xlim(_zoomed(*ax.get_xlim(), xd, f, ax.get_xscale() == 'log'))
                    done_x.append(ax)
                ax.set_ylim(_zoomed(*ax.get_ylim(), yd, f, ax.get_yscale() == 'log'))
                hit = True
            if hit:
                canvas.draw_idle()

        def on_press(ev):
            if getattr(ev, 'dblclick', False):
                tb.home()
                canvas.draw_idle()

        canvas.mpl_connect('scroll_event', on_scroll)
        canvas.mpl_connect('button_press_event', on_press)
        canvas.draw()

        def _sync(_e=None):
            """CTk fixes the toplevel geometry a beat AFTER creation; the last <Configure> can
            race the canvas redraw and leave a stale, larger buffer on screen. Force the figure
            to the widget's FINAL pixel size and re-blit."""
            try:
                wpx, hpx = w.winfo_width(), w.winfo_height()
                if wpx > 50 and hpx > 50:
                    fig.set_size_inches(wpx / fig.dpi, hpx / fig.dpi, forward=False)
                    canvas.draw()
            except Exception:                             # noqa: BLE001 — cosmetic safety net
                pass
        w.after(350, _sync)
        return canvas

    @staticmethod
    def _mpl_rc():
        """rcParams matching the active theme — an equity PNG is an image, so it inherits nothing
        from the widgets around it and has to be told the colours."""
        return {'figure.facecolor': CARD, 'axes.facecolor': CARD, 'savefig.facecolor': CARD,
                'text.color': TXT, 'axes.labelcolor': MUT, 'axes.titlecolor': TXT,
                'axes.edgecolor': BORDER, 'xtick.color': MUT, 'ytick.color': MUT,
                'grid.color': GRID, 'legend.facecolor': CARD, 'legend.edgecolor': BORDER,
                'legend.labelcolor': TXT}

    def _pf_figure(self, doc, width, fig_h, dpi=100):
        """The portfolio chart as a plain Figure (no pyplot, no global state): shared by the
        card's PNG render (background thread) and the interactive zoom window (main thread)."""
        from matplotlib.figure import Figure
        import matplotlib.transforms as mtrans
        import numpy as np
        import pandas as pd
        eq = doc['equity']
        x = pd.to_datetime(eq['dates'])
        op = doc.get('open_pnl') or None
        if op is not None and len(op) != len(eq['dates']):
            op = None                                     # malformed doc — draw without the panel
        fig = Figure(figsize=(width / dpi, fig_h), dpi=dpi, facecolor=CARD)
        if op is not None:
            gs = fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.08)
            ax = fig.add_subplot(gs[0])
            ax2 = fig.add_subplot(gs[1], sharex=ax)
        else:
            ax = fig.add_subplot(111)
            ax2 = None
        ax.plot(x, eq['combined'], lw=2.0, color=ACC, label=f'Portfolio (top-{doc.get("n")})')
        ax.plot(x, eq['basket'], lw=1.2, color='#f9a825', ls=':', label='buy & hold (EW)')
        ax.set_yscale('log'); ax.grid(True, which='both', alpha=0.3)
        bounds = doc.get('bounds') or {}
        # full-span: segment names sit along the top edge -> legend goes bottom-right
        ax.legend(loc=('lower right' if bounds and doc.get('span') else 'upper left'), fontsize=8)
        if bounds and doc.get('span'):                    # full-span doc: mark the segments
            trans = mtrans.blended_transform_factory(ax.transData, ax.transAxes)
            lo0, hi0 = x[0], x[-1]
            edges = [lo0] + [min(max(pd.Timestamp(bounds[k], tz=lo0.tz), lo0), hi0)
                             for k in ('val_start', 'test_start')] + [hi0]
            for e in edges[1:-1]:
                if lo0 < e < hi0:                         # a late --sim-start can clip a segment
                    ax.axvline(e, color=MUT, lw=0.9, ls='--', alpha=0.55)
                    if ax2 is not None:
                        ax2.axvline(e, color=MUT, lw=0.9, ls='--', alpha=0.55)
            for name, lo, hi in zip(('TRAIN', 'VAL', 'TEST'), edges[:-1], edges[1:]):
                if hi > lo:
                    ax.text(lo + (hi - lo) / 2, 0.985, name, transform=trans,
                            ha='center', va='top', fontsize=7.5, color=MUT, alpha=0.9)
            title = f'combined equity — TRAIN / VAL / TEST ({doc["span"]})'
        else:                                             # docs built before the full-span change
            title = f'combined equity — TEST ({doc.get("test", "")})'
        ax.set_title(title, fontsize=9)
        ax.tick_params(labelsize=8)
        if ax2 is not None:
            opv = np.asarray(op, dtype=float)
            ax2.fill_between(x, opv, 0, where=opv >= 0, color=POS, alpha=0.30, lw=0)
            ax2.fill_between(x, opv, 0, where=opv < 0, color=NEG, alpha=0.30, lw=0)
            ax2.plot(x, opv, lw=0.9, color=MUT)
            ax2.axhline(0, color=MUT, lw=0.8)
            ax2.grid(True, alpha=0.3)
            ax2.tick_params(labelsize=8)
            ax2.set_ylabel('open PnL', fontsize=8)
            ax2.text(0.005, 0.93, 'open PnL — unrealized gain/loss of the held '
                                  'positions (share of book; a close/flip realizes it away)',
                     transform=ax2.transAxes, fontsize=7, color=MUT, va='top')
        import warnings as _w
        with _w.catch_warnings():                         # tight_layout grumbles about sharex axes
            _w.simplefilter('ignore')
            fig.tight_layout()
        return fig

    def _pf_interactive(self, _e=None):
        """Double-click on the portfolio PNG: the same chart as a LIVE canvas — wheel zoom,
        pan, toolbar. The card keeps the static PNG (it auto-fits the layout)."""
        doc = self._pf_doc
        if not doc or not (doc.get('equity') or {}).get('dates'):
            return
        import matplotlib
        # CTk multiplies the geometry string by window_scaling, while the figure is raw pixels —
        # divide back (same trick as _open_plot) so the canvas is not stretched vs the figure
        win = self._dialog('Portfolio equity — interactive (wheel: zoom · drag with ✥: pan · '
                           'double-click: reset)',
                           f'{int(1200 / self.SCALE)}x{int(660 / self.SCALE)}')
        body = self._box(win)
        body.pack(fill='both', expand=True, padx=14, pady=12)
        with self._plot_lock, matplotlib.rc_context(self._mpl_rc()):
            fig = self._pf_figure(doc, width=1150, fig_h=5.3)
        self._embed_fig(body, fig)

    def _render_pf_equity(self, doc, width=900):
        eq = doc.get('equity') or {}
        if not eq.get('dates'):
            return
        try:
            self._pf_last_w = width
            with self._plot_lock:
                import matplotlib
                dpi = 100
                ph = self.cfg.get('pf_h') or 0            # user-dragged height (px), 0 = automatic
                fig_h = (ph / dpi) if ph else min(3.8, max(2.4, width / dpi / 4.5))
                if doc.get('open_pnl') and not ph:
                    fig_h = min(4.8, fig_h * 1.3)         # room for the lower panel
                with matplotlib.rc_context(self._mpl_rc()):
                    fig = self._pf_figure(doc, width, fig_h, dpi=dpi)
                    fig.savefig(PORTFOLIO_PNG, dpi=dpi, facecolor=CARD)
            self.root.after(0, self._show_pf_img)
        except Exception:                                # noqa: BLE001
            pass

    def _show_pf_img(self):
        try:
            img = tk.PhotoImage(file=PORTFOLIO_PNG)
            self._pf_img_ref = img                        # keep ref
            self.pf_img.config(image=img)
        except tk.TclError:
            pass

    def _build_plot_cfg(self):
        """The same config the node searched with (self.cfg = last run): pairs, vol/fee,
        TRAIN/VAL/TEST segments — so the curve and metrics match the leaderboard."""
        from config import load_config
        import pandas as pd
        cfg = load_config()
        c = self.cfg
        lst = _parse_universe(c.get('universe_list', ''))
        if lst:
            cfg['instruments'] = lst
        cfg['vol'] = float(c.get('target_vol', cfg['vol']))
        cfg['exec'] = float(c.get('exec_cost', cfg['exec']))
        # the GUI's timeframe selector wins over config.ini (load_config in THIS process only
        # sees the ini/env, not the widget), and the data snapshot follows the timeframe
        if cfg.get('tf', '1d') != self._tf():
            from timeframe import resolve as _rtf
            _t = _rtf(self._tf())
            cfg.update(tf=_t.name, ann=_t.periods_per_year, freq=_t.pandas_freq,
                       vol_window=_t.vol_window, ewma_lambda=_t.ewma_lambda,
                       binance_interval=_t.binance_interval)
        cfg['data'] = self._data_file()
        # the GUI's date boxes ('auto'/'today' sentinels resolved for THIS timeframe) replace the
        # ini's [segments] — the ini knows only the daily defaults, and a chart cut at them while
        # the leaderboard was mined on the user's TEST window mislabels every segment
        tf_name = self._tf()
        try:
            def _sv(f):
                return _seg_res(tf_name, f, c.get(f) or DEFAULTS[f])
            tr = pd.Timestamp(_sv('train_start'), tz='UTC'); va = pd.Timestamp(_sv('val_start'), tz='UTC')
            te = pd.Timestamp(_sv('test_start'), tz='UTC'); en = pd.Timestamp(_sv('test_end'), tz='UTC')
            cfg['splits'] = {'train': (tr, va), 'val': (va, te), 'test': (te, en)}
            cfg['start'] = tr.tz_localize(None).to_pydatetime()
            cfg['end'] = en.tz_localize(None).to_pydatetime()
        except Exception as e:                           # noqa: BLE001 — never silent: a chart on
            print(f'[plot] segment dates unreadable ({type(e).__name__}: {e}) — '   # the wrong
                  'config.ini defaults used', flush=True)                              # window lies
        return cfg

    def _get_market(self, cfg):
        from evaluator import build_panel, make_market, basket_returns
        key = (tuple(cfg['instruments']) if cfg.get('instruments') else 'all',
               str(cfg['start']), str(cfg['end']), cfg.get('tf', '1d'))
        cached = self._panel_cache.get(key)
        if cached is None:
            tk_, raw, panel = build_panel(cfg['data'], cfg['start'], cfg['end'],
                                          cfg.get('instruments'), freq=cfg.get('freq', 'D'))
            cached = (tk_, panel, make_market(panel, tk_, raw,
                                              vol_window=cfg.get('vol_window', 30)),
                      basket_returns(panel))
            self._panel_cache = {key: cached}            # keep only the last one (memory)
        return cached

    # ---------- vault: the locked card + the Unlock flow (talks to AlphaHub) ----------
    def _device_id(self):
        """A stable per-install id — one of this account's seats. Minted once and kept next to
        the state; the hub counts distinct device_ids against the plan's node limit, and the
        miner seals every formula with this id as its OWNER (node._device_id reads the same
        file — keep the semantics in lockstep). O_EXCL + re-read: if the GUI and a freshly
        spawned node mint concurrently, one write wins and both end up with the winner."""
        path = os.path.join(STATE_DIR, 'device_id')
        try:
            did = open(path, encoding='utf-8').read().strip()
            if did:
                return did
        except OSError:
            pass
        import secrets
        did = secrets.token_hex(8)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(did + '\n')
        except OSError:
            pass                                         # lost the race (or read-only state)
        try:
            did = open(path, encoding='utf-8').read().strip() or did
        except OSError:
            pass
        return did

    def _hub_request(self, path, payload, timeout=5):
        """POST JSON to the hub; return the parsed body. AlphaHub answers success as {ok:true,…}
        and errors as FastAPI's {detail:…} with a 4xx — both are JSON, so callers check 'ok' and
        fall back to 'detail' for the message. Raises RuntimeError only on transport/format faults."""
        import urllib.error
        import urllib.request
        url = VAULT_URL.rstrip('/')                      # the vendor's hub (ALPHANODE_VAULT_URL)
        req = urllib.request.Request(url + path, data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:              # 4xx still carries a JSON {detail} body
            raw = e.read().decode(errors='replace')
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(f'hub unreachable at {url} — is it running?') from e
        try:                                             # a 200 from a captive portal isn't JSON
            return json.loads(raw)
        except (ValueError, json.JSONDecodeError) as e:
            raise RuntimeError(f'{url} did not answer like the AlphaHub server') from e

    def _vault_reveal(self, formula_enc, account_token):
        """Claim a seat for this machine (activate), then reveal — the hub checks the subscription
        and the node limit and does the unseal. Returns the plaintext formula or raises with a
        human message (limit reached / subscription needed / server down)."""
        if not account_token:
            raise RuntimeError('enter your subscription key first')
        device_id = self._device_id()
        act = self._hub_request('/activate', {'token': account_token, 'device_id': device_id,
                                              'build': _build_id()})
        if not act.get('ok'):                            # seat / subscription problem
            raise RuntimeError(str(act.get('detail') or 'activation denied'))
        out = self._hub_request('/reveal', {'token': account_token, 'device_id': device_id,
                                            'formula_enc': formula_enc})
        if not out.get('ok'):
            raise RuntimeError(str(out.get('detail') or 'unlock denied'))
        return str(out.get('formula', ''))

    @staticmethod
    def _read_library(path):
        """Lines + index-aligned parsed docs; a line that doesn't parse maps to None and is
        preserved verbatim by every rewrite."""
        lines = open(path, encoding='utf-8').read().splitlines()
        docs = []
        for ln in lines:
            try:
                docs.append(json.loads(ln) if ln.strip() else None)
            except json.JSONDecodeError:
                docs.append(None)
        return lines, docs

    def _vault_activate_all(self, account_token):
        """Node activation: claim a seat, then unlock EVERY sealed formula in every local library
        (all timeframes) in chunked batches, rewriting the files in the open. This is the
        'subscription = the node works in the open' switch; the per-card Unlock stays as the
        single-formula path. Returns a human summary; raises RuntimeError on any gate.

        The rewrite is guarded against every writer we know about: the caller refuses to run
        while the GUI's own miner is up AND holds _activating so Start refuses to launch one;
        for writers we can't see (a headless cli.py miner, rescore) each file is swapped only if
        a fresh stat matches the snapshot the rewrite was computed from — on a mismatch the swap
        is retried on the fresh content and, failing that, the file is left alone (their rows
        must never be lost, ours can be re-revealed). Revealed rows KEEP formula_enc and only
        drop 'locked': the sealed token is the crash-proof original — anything torn mid-write is
        re-revealable forever."""
        if not account_token:
            raise RuntimeError('enter your subscription key first')
        device_id = self._device_id()
        act = self._hub_request('/activate', {'token': account_token, 'device_id': device_id,
                                              'build': _build_id()})
        if not act.get('ok'):
            raise RuntimeError(str(act.get('detail') or 'activation denied'))
        n_open = n_fail = 0
        contested = []
        for name in sorted(os.listdir(STATE_DIR)):
            if not (name.startswith('library') and name.endswith('.jsonl')):
                continue
            path = os.path.join(STATE_DIR, name)
            try:
                _, docs = self._read_library(path)
            except OSError:
                continue
            sealed = [d for d in docs if isinstance(d, dict) and d.get('locked')
                      and d.get('formula_enc') and not d.get('formula')]
            revealed = {}                                # row id -> verified plaintext
            for at in range(0, len(sealed), 500):        # chunked: the hub caps a batch at 2000
                chunk = sealed[at:at + 500]
                out = self._hub_request('/reveal_batch', {
                    'token': account_token, 'device_id': device_id,
                    'formulas': [d['formula_enc'] for d in chunk]}, timeout=30)
                if not out.get('ok'):
                    raise RuntimeError(str(out.get('detail') or 'unlock denied'))
                for d, item in zip(chunk, out.get('formulas') or []):
                    f = item.get('formula')
                    fid = str(d.get('id', ''))
                    # pin each reveal to its row: the id stored at mine time must match
                    if not f or not fid or hashlib.md5(f.encode()).hexdigest()[:12][:len(fid)] != fid:
                        n_fail += 1
                        continue
                    revealed[fid] = f
            if not revealed:
                continue
            # apply to a FRESH read, swap only if the file did not change in between: an outside
            # writer loses the swap (we retry on its content), never its rows
            tmp = f'{path}.activate.{os.getpid()}.tmp'   # per-run name: two runs can't truncate
            for _attempt in range(3):                    # each other's half-written tmp
                st0 = os.stat(path)
                lines, docs = self._read_library(path)
                applied = 0
                for i, d in enumerate(docs):
                    if (isinstance(d, dict) and d.get('locked') and not d.get('formula')
                            and str(d.get('id', '')) in revealed):
                        d['formula'] = revealed[str(d['id'])]
                        d.pop('locked', None)            # formula_enc stays: re-derivable forever
                        lines[i] = json.dumps(d)
                        applied += 1
                if not applied:
                    break
                with open(tmp, 'w', encoding='utf-8') as fh:
                    fh.write('\n'.join(lines) + '\n')
                    fh.flush()
                    os.fsync(fh.fileno())                # durable BEFORE it replaces the library
                st1 = os.stat(path)
                if (st1.st_mtime_ns, st1.st_size) == (st0.st_mtime_ns, st0.st_size):
                    os.replace(tmp, path)
                    n_open += applied
                    break
                try:                                     # someone appended mid-rewrite: drop the
                    os.unlink(tmp)                       # stale tmp, retry on the fresh content
                except OSError:
                    pass
            else:
                contested.append(name)
        plan = f"plan {act.get('plan')} · nodes {act.get('used')}/{act.get('node_limit')}"
        if n_open or n_fail:
            msg = f'unlocked {n_open} formulas'
            if n_fail:
                msg += f' ({n_fail} failed — still sealed)'
        else:
            msg = 'library already open — nothing left sealed'
        if contested:
            msg += (f' · {", ".join(contested)} kept changing (a miner is writing?) — '
                    'left sealed, stop it and retry')
        return msg + ' · ' + plan

    def _open_activate(self):
        """The one-key activation card: paste the subscription key once — this machine claims a
        seat, the whole local library is revealed in place, and Start mines in the open from then
        on (checked live against the hub each Start)."""
        win = self._dialog('Activate node', f'{int(720 / self.SCALE)}x{int(330 / self.SCALE)}')
        pad = self._box(win)
        pad.pack(fill='both', expand=True, padx=18, pady=16)
        self._lbl(pad, text='One key opens everything', text_color=TXT, anchor='w',
                  font=(self.UI, 15, 'bold')).pack(anchor='w')
        self._lbl(pad, text='Your subscription key activates this machine (one of the plan\'s '
                            'node seats),\nunlocks every sealed formula already mined here, and '
                            'lets the node mine\nin the open while the subscription is live.',
                  text_color=MUT, justify='left', anchor='w',
                  font=(self.UI, 12)).pack(anchor='w', pady=(6, 0))
        row = self._box(pad)
        row.pack(anchor='w', fill='x', pady=(14, 4))
        self._lbl(row, text='subscription key', text_color=MUT,
                  font=(self.UI, 11)).pack(side='left')
        ent = self._entry(row, None, width=260, placeholder='paste your subscription key')
        if self.cfg.get('vault_license'):
            ent.insert(0, self.cfg['vault_license'])
        ent.pack(side='left', padx=(8, 10))
        st = self._lbl(pad, text='', text_color=MUT, anchor='w', font=(self.UI, 11))

        def finalize(msg, err, token):
            self._activating = False                     # ALWAYS: Start is gated on this flag
            if not err:
                self.cfg['vault_license'] = token        # the key the hub actually accepted —
                self._save()                             # saved even if the dialog is gone (the
                self._licence_adopted = False            # seat is claimed and the files are
                try:                                     # already rewritten either way)
                    import licence_store                 # …and shared with every other install
                    licence_store.save(token)            # on this machine (one activation each)
                except Exception:                        # noqa: BLE001
                    pass
                self._after_activation()
            if not win.winfo_exists():
                return
            if err:
                st.configure(text=err, fg=NEG)
                btn.configure(state='normal')
            else:
                st.configure(text=msg + '  —  the leaderboard shows the formulas now', fg=POS)

        def activate():
            if self._activating:
                st.configure(text='an activation is already running', fg=NEG)
                return
            if self.proc and self.proc.poll() is None:   # the rewrite swaps library.jsonl —
                st.configure(text='stop the node first — activation rewrites the library file',
                             fg=NEG)                     # never under a running miner's append
                return
            btn.configure(state='disabled')
            st.configure(text='checking your subscription…', fg=MUT)
            token = ent.get().strip()
            self._activating = True                      # process-wide: start() refuses while set
            res = {}                                     # thread -> main-loop handoff, one key

            def work():
                try:
                    res['out'] = (self._vault_activate_all(token), None)
                except Exception as ex:                  # noqa: BLE001 — any failure reaches the card
                    res['out'] = ('', str(ex) or type(ex).__name__)

            def tick():                                  # polls on ROOT, not the dialog: closing
                if 'out' in res:                         # the window must not orphan a finished
                    finalize(res['out'][0], res['out'][1], token)   # activation (flag + key!)
                else:
                    self.root.after(120, tick)
            threading.Thread(target=work, daemon=True).start()
            self.root.after(120, tick)

        btn = self._btn(row, 'Activate', activate, kind='accent', width=120)
        btn.pack(side='left')
        st.pack(anchor='w', pady=(6, 0))

    def _open_locked(self, champ):
        """A vault doc opened from the leaderboard: stored metrics in full, the formula sealed.
        Unlock asks the server to reveal; success re-opens the row as a normal equity window.
        Prototype: the reveal lives in memory only — the library file on disk stays sealed."""
        aid = str(champ.get('id', ''))[:6]
        win = self._dialog(f'Locked alpha {aid}', '620x300')
        head = self._box(win)
        head.pack(fill='x', padx=18, pady=(16, 8))

        def seg(name, m, accent=False):
            m = m or {}
            sh, cg, dd = m.get('sharpe'), m.get('cagr'), m.get('dd')
            txt = f'{name}:  Sharpe {sh:+.2f}' if sh is not None else f'{name}:  —'
            if cg is not None:
                txt += f'   CAGR {cg*100:+.0f}%'
            if dd is not None:
                txt += f'   DD {dd*100:.0f}%'
            self._lbl(head, text=txt, text_color=(NEG if accent else TXT), anchor='w',
                      font=(self.UI, 11, 'bold' if accent else 'normal')).pack(anchor='w')

        seg('TRAIN', champ.get('train'))
        seg('VAL', champ.get('val'))
        seg('TEST (held-out)', champ.get('test'), accent=True)
        self._lbl(head, text=f'formula {aid} is sealed in your vault',
                  text_color=TXT, anchor='w', font=(self.UI, 13, 'bold')).pack(anchor='w', pady=(12, 2))
        self._lbl(head, text='It was mined on this machine and encrypted before touching the disk.\n'
                             'A subscription unlocks live signals and the formula text — the vault\n'
                             'server checks the license and reveals it. The metrics above are yours\n'
                             'to inspect either way.',
                  text_color=MUT, justify='left', anchor='w', font=(self.UI, 11)).pack(anchor='w')
        row = self._box(head)
        row.pack(anchor='w', pady=(12, 4))
        self._lbl(row, text='subscription key', text_color=MUT, font=(self.UI, 11)).pack(side='left')
        ent = self._entry(row, None, width=180,          # var=None: the ghost hint only renders
                          placeholder='paste your subscription key')   # with no textvariable bound
        if self.cfg.get('vault_license'):                # remembered after the 1st unlock
            ent.insert(0, self.cfg['vault_license'])
        ent.pack(side='left', padx=(8, 10))
        st = self._lbl(head, text='', text_color=MUT, anchor='w', font=(self.UI, 11))

        def done(formula, err, account):
            if not win.winfo_exists():
                return
            if err:
                st.configure(text=err, fg=NEG)
                btn.configure(state='normal')
                return
            fid = str(champ.get('id', ''))
            import vault
            if fid and vault.formula_id(formula)[:len(fid)] != fid:
                st.configure(text='server returned a DIFFERENT formula — refusing it', fg=NEG)
                btn.configure(state='normal')
                return                                   # the id pins the reveal to what was mined
            champ['formula'] = formula
            champ['locked'] = False
            if account:                                  # the key the hub ACCEPTED (not whatever
                self.cfg['vault_license'] = account      # the still-editable field holds now) —
                self._save()                             # the next unlock is one click
            win.destroy()
            self._treesig = None                         # session-only: the row now shows its text
            self._fill_tree(list(self._shown))
            self._open_plot(champ)

        def unlock():
            btn.configure(state='disabled')
            st.configure(text='checking your subscription…', fg=MUT)
            box, account = str(champ.get('formula_enc', '')), ent.get().strip()
            res = {}                                     # thread -> main-loop handoff: the worker

            def work():                                  # writes ONE key ('out') as a single store,
                try:                                     # so the ticker never sees a half-written
                    res['out'] = (self._vault_reveal(box, account), None)   # result (no f/e race)
                except Exception as ex:                  # noqa: BLE001 — any failure reaches the card
                    res['out'] = ('', str(ex) or type(ex).__name__)

            def tick():
                if 'out' in res:
                    done(res['out'][0], res['out'][1], account)
                elif win.winfo_exists():
                    win.after(120, tick)
            threading.Thread(target=work, daemon=True).start()
            win.after(120, tick)

        btn = self._btn(row, 'Unlock', unlock, kind='accent', width=120)
        btn.pack(side='left')
        st.pack(anchor='w', pady=(4, 0))
        self._btn(head, 'or activate the whole node — one key unlocks every formula at once',
                  lambda: (win.destroy(), self._open_activate()),
                  height=24).pack(anchor='w', pady=(12, 0))

    def _open_plot(self, champ):
        if champ.get('locked') and not champ.get('formula'):
            return self._open_locked(champ)              # vault doc: the card with the Unlock flow
        self._plot_seq += 1
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        img_w = int(min(1680, max(1000, sw * 0.80)))     # large chart, but within the screen
        img_h = int(img_w / 1.7)
        avail_h = int(sh * 0.90) - 200                   # room for the header/buttons
        if img_h > avail_h:
            img_h = max(480, avail_h)
            img_w = int(img_h * 1.7)
        dpi = 110
        holder = {'done': False, 'fig': None, 'err': None, 'dpi': dpi,
                  'figsize': (img_w / dpi, (img_h - 40) / dpi)}   # -40: room for the zoom toolbar
        # img_w/img_h are REAL pixels (the PNG is shown 1:1), but CTk multiplies a geometry string by
        # window_scaling — so divide it back out, or the window opens ~SCALE times wider than its chart.
        win = self._dialog('Equity — ' + champ.get('formula', '')[:60],
                           f'{int((img_w + 44) / self.SCALE)}x{int((img_h + 200) / self.SCALE)}')

        head = self._box(win)
        head.pack(fill='x', padx=16, pady=(14, 6))

        def seg(name, m, accent=False):
            m = m or {}
            sh, cg, dd = m.get('sharpe'), m.get('cagr'), m.get('dd')
            txt = f'{name}:  Sharpe {sh:+.2f}' if sh is not None else f'{name}:  —'
            if cg is not None:
                txt += f'   CAGR {cg*100:+.0f}%'
            if dd is not None:
                txt += f'   DD {dd*100:.0f}%'
            self._lbl(head, text=txt, text_color=(NEG if accent else TXT), anchor='w',
                         font=(self.UI, 11, 'bold' if accent else 'normal')).pack(anchor='w')

        seg('TRAIN', champ.get('train'))
        seg('VAL', champ.get('val'))
        seg('TEST (held-out)', champ.get('test'), accent=True)
        _m = self._metrics_cache.get(champ.get('formula', ''))
        if isinstance(_m, dict) and any(_m.get(k) is not None for k in ('tup', 'tdown', 'tflat')):
            self._lbl(head, text=f"TEST by market direction:   T↑ {self._fmt_ratio(_m.get('tup'))}"
                                 f"   ·   T↓ {self._fmt_ratio(_m.get('tdown'))}"
                                 f"   ·   T~ {self._fmt_ratio(_m.get('tflat'))}  (Sharpe)",
                      text_color=MUT, anchor='w', font=(self.UI, 11)).pack(anchor='w')
        if isinstance(_m, dict) and any(_m.get(k) is not None for k in ('wup', 'wdown')):
            self._lbl(head, text=f"call accuracy:   long {self._fmt_winpct(_m.get('wup'))}"
                                 f"   ·   short {self._fmt_winpct(_m.get('wdown'))}",
                      text_color=MUT, anchor='w', font=(self.UI, 11)).pack(anchor='w')
        self._lbl(head, text=champ.get('formula', ''), text_color=MUT, justify='left', anchor='w',
                     wraplength=img_w - 30, font=(self.MONO, 12)).pack(anchor='w', pady=(6, 0))
        btnrow = self._box(head)
        btnrow.pack(anchor='w', pady=(10, 0))
        self._btn(btnrow, 'Download signals (CSV)', lambda: self._download_signals(champ),
                  width=196).pack(side='left')
        _f = champ.get('formula', '')
        self._btn(btnrow, 'Serve signal (API)',
                  lambda: self._serve_signal([_f], 'alpha_' + hashlib.md5(_f.encode()).hexdigest()[:6]),
                  width=176).pack(side='left', padx=(8, 0))
        pdf_btn = self._btn(btnrow, 'PDF report', lambda: self._pdf_report_alpha(champ), width=110)
        pdf_btn.pack(side='left', padx=(8, 0))
        self._tip(pdf_btn, 'Analytics dashboard as PDF: KPIs, equity and drawdown,\n'
                           'exposure and turnover, weight structure, monthly returns,\n'
                           'TRAIN/VAL/TEST breakdown and conclusions.')
        pp_btn = self._btn(btnrow, 'Passport', lambda: self._formula_passport(champ), width=110)
        pp_btn.pack(side='left', padx=(8, 0))
        self._tip(pp_btn, 'Explain the formula: its tree with human labels, plain-English\n'
                          'reading steps, its position on one asset\'s price, which inputs\n'
                          'feed it (ablation), and which strategy archetype it behaves like.\n'
                          'An explanation is not evidence — TEST and forward still decide.')
        fwd_btn = self._btn(btnrow, 'Forward track +',
                            lambda: self._fwd_enroll(
                                [_f], hashlib.md5(_f.encode()).hexdigest()[:6], 'alpha'),
                            width=150)
        fwd_btn.pack(side='left', padx=(8, 0))
        self._tip(fwd_btn, 'Enroll this alpha into the FORWARD TRACK: freeze it (formula +\n'
                           'universe + vol/fee) and paper-step it once per closed daily bar\n'
                           'on live Binance data. Append-only — the honest forward test.')

        body = self._box(win)
        body.pack(fill='both', expand=True, padx=16, pady=(4, 14))
        status = self._lbl(body, text='building equity (TRAIN | VAL | TEST + basket B&H)…',
                              text_color=MUT, font=(self.UI, 15))
        status.pack(pady=40)

        threading.Thread(target=self._compute_equity, args=(champ, holder), daemon=True).start()
        self.root.after(200, lambda: self._check_plot(win, holder, status, body))

    # ---------- download portfolio signals (CSV) ----------
    def _alpha_weights_wide(self, formula, cfg, market, panel):
        """Target-weight table of one alpha over the whole panel — the same inverse-vol +
        chips normalization the engine trades with. Shared by the CSV export and the PDF report."""
        import numpy as np
        import pandas as pd
        from genome import parse
        from evaluator import eval_alpha_panel
        ap = eval_alpha_panel(parse(formula), panel)
        A = pd.DataFrame(ap[market['tk']].to_numpy(dtype=np.float64)).ffill().to_numpy()
        V = market['V']
        E = market['base_elig'] & np.isfinite(A)                # eligible & has a signal
        fc = np.where(E, A, 0.0) / V                            # inverse-vol (as in the engine)
        fc = np.where(E, fc, 0.0)
        chips = np.nansum(np.abs(fc), axis=1, keepdims=True)    # normalization by "chips"
        W = fc / np.where(chips == 0.0, 1.0, chips)             # target weight: + long / − short
        return pd.DataFrame(np.round(W, 6), index=market['index'], columns=market['tk'])

    def _download_signals(self, champ):
        formula = champ.get('formula', '')
        if not formula:
            return
        name = 'alpha_' + hashlib.md5(formula.encode()).hexdigest()[:6]
        path = filedialog.asksaveasfilename(
            parent=self.root, title='Save portfolio signals',
            defaultextension='.csv', initialfile=f'signals_{name}.csv',
            filetypes=[('CSV', '*.csv'), ('All files', '*.*')])
        if not path:
            return
        try:
            cfg = self._build_plot_cfg()
            _tk, panel, market, _basket = self._get_market(cfg)
            wide = self._alpha_weights_wide(formula, cfg, market, panel)
            self._signals_from_wide(wide, cfg['splits'], path, panel=panel)
        except Exception as e:                                     # noqa: BLE001
            messagebox.showerror('Error', f'Failed to build signals: {e}', parent=self.root)

    def _signals_from_wide(self, wide, splits, path, panel=None):
        """Wide target-weight table (index=date, cols=tickers) -> tidy CSV (row = one position,
        with the asset's OHLCV on that bar when `panel` is given) + the 'what to hold now'
        dialog. Shared by a single alpha and the combined portfolio."""
        import numpy as np
        wide = wide.copy()
        wide.index.name = 'date'
        wide = wide[wide.abs().sum(axis=1) > 0]                    # drop empty (pre-listing) days
        long = wide.reset_index().melt(id_vars='date', var_name='ticker', value_name='weight')
        long = long[long['weight'].abs() > 0.0005].copy()
        long['side'] = np.where(long['weight'] > 0, 'LONG', 'SHORT')
        long['weight_pct'] = long['weight'].map(lambda x: f'{x * 100:+.1f}%')
        d = long['date']
        long['segment'] = np.where(d < splits['val'][0], 'TRAIN',
                                   np.where(d < splits['test'][0], 'VAL', 'TEST'))
        long['_aw'] = long['weight'].abs()
        long = long.sort_values(['date', '_aw'], ascending=[True, False]).drop(columns='_aw')
        cols = ['date', 'segment', 'ticker', 'side', 'weight', 'weight_pct']
        if panel is not None:                                      # the asset's bar next to its weight
            ii = panel['close'].index.get_indexer(long['date'])
            jj = panel['close'].columns.get_indexer(long['ticker'])
            ok = (ii >= 0) & (jj >= 0)
            for c in ('open', 'high', 'low', 'close', 'volume'):
                arr = panel[c].to_numpy()
                long[c] = np.where(ok, arr[np.clip(ii, 0, None), np.clip(jj, 0, None)], np.nan)
            cols += ['open', 'high', 'low', 'close', 'volume']
        long = long[cols]
        long.to_csv(path, index=False)
        last = wide.iloc[-1]
        pos = sorted([(t, float(v)) for t, v in last.items() if abs(v) > 0.0005],
                     key=lambda kv: -abs(kv[1]))
        rng = f'{wide.index[0].date()}..{wide.index[-1].date()}'
        self._signals_dialog(path, wide.index[-1].date(), pos, len(wide), rng=rng)

    # ---------- forward track (append-only paper stepping inside the node) ----------
    def _fwd_lib(self):
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import forward_track
        if not getattr(self, '_fwd_migrated', False):    # legacy 'alpha_xxxxxx_yyyyyy' ids ->
            self._fwd_migrated = True                    # the bare leaderboard id, once
            try:
                track = forward_track.load_track()
                # + drop archived ghosts doubling a live id: a file poisoned by the old
                #   Archive button kept the stepper landing on the ghost (BUG_FIXES 2026-08-25)
                if forward_track.migrate_ids(track) + forward_track.drop_ghosts(track):
                    forward_track.save_track(track)
            except Exception:                            # noqa: BLE001 — cosmetics must not
                pass                                     # block enrollment/stepping
        return forward_track

    def _fwd_universe(self):
        """The universe to FREEZE into an enrollment — the active timeframe's basket, exactly
        what the strategy was mined and leaderboarded on (see _universe_tickers)."""
        return self._universe_tickers()

    def _fwd_enroll(self, formulas, name, kind):
        formulas = [f for f in (formulas or []) if f and f.strip()]
        if not formulas:
            return
        tickers = self._fwd_universe()
        if not tickers:
            messagebox.showwarning('Forward track', 'The pairs universe is empty.', parent=self.root)
            return
        tf = self._tf()                                   # frozen with the strategy: windows are in
        ft = self._fwd_lib()                              # bars of the tf the formula was mined on
        track = ft.load_track()
        # single alphas: the entry id IS the leaderboard id (md5 of the formula, 6 chars) —
        # the two panels must read as one list, so the id doubles as the uniqueness key
        entry_id = (hashlib.md5(formulas[0].encode()).hexdigest()[:6]
                    if kind == 'alpha' and len(formulas) == 1 else None)
        dup = next((e for e in track.get('entries', [])
                    if entry_id and e.get('id') == entry_id), None) \
            or ft.find_duplicate(track, formulas, tickers, tf)
        if dup:
            messagebox.showinfo('Forward track',
                                f'Already enrolled: {dup["id"]} (since {dup["enrolled"]}).',
                                parent=self.root)
            return
        c = self.cfg
        start = str(c.get('train_start', '2019-09-05'))
        if tf != '1d':
            try:                                          # don't page YEARS of 1h/15m klines every
                from timeframe import resolve as _rtf     # step — the tf's recommended history is
                start = max(start, _rtf(tf).history)      # plenty of warm-up (ISO strings compare)
            except Exception:                             # noqa: BLE001
                pass
        entry = ft.new_entry(entry_id or name, kind, formulas, tickers,
                             c.get('target_vol', 0.25),
                             c.get('exec_cost', 0.001), start, tf=tf, entry_id=entry_id,
                             session=self._session_id())   # the GUI's own dir: with an exported
        #                                                    ALPHANODE_STATE_DIR the module-level
        #                                                    default could stamp a DIFFERENT
        #                                                    directory's epoch than the header shows
        # a portfolio's id is the deterministic name_sig: an archived ghost of the same
        # portfolio (find_duplicate rightly ignores it) would double the id, and the stepper
        # syncs by id — every step of the new entry landed on the ghost and was dropped
        entry['id'] = ft.unique_id(track, entry['id'])
        track['entries'].append(entry)
        ft.save_track(track)
        self._fwd_refresh()
        intr = ('' if tf == '1d' else
                f'\n⚠ {tf} bars: steps only happen while the app is open — long gaps make the '
                'track sparse; fees/slippage weigh more intraday, read it conservatively.')
        if messagebox.askyesno(
                'Forward track',
                f'{entry["id"]} enrolled. The strategy is FROZEN as of today '
                f'({len(formulas)} formula{"s" if len(formulas) > 1 else ""}, {len(tickers)} assets, '
                f'{tf} bars, vol {c.get("target_vol", 0.25)}, fee {c.get("exec_cost", 0.001)}) and '
                f'will be paper-stepped once per closed {tf} bar while the app is open.\n'
                'History is append-only — forward numbers are never recomputed backwards.'
                f'{intr}\n\nRun the first step now (downloads live Binance data)?',
                parent=self.root):
            self._fwd_step()

    def _pf_fwd_enroll(self):
        doc = self._pf_doc
        formulas = (doc or {}).get('formulas_full')
        if not formulas:
            messagebox.showinfo('Forward track', 'Build the portfolio first.', parent=self.root)
            return
        self._fwd_enroll(list(formulas), f'portfolio_top{doc.get("n", len(formulas))}', 'portfolio')

    def _fwd_step(self, force=False):
        if self._fwd_proc and self._fwd_proc.poll() is None:
            return
        env = dict(os.environ)
        env.update(ALPHANODE_STATE_DIR=STATE_DIR)
        try:
            self._fwd_proc = subprocess.Popen(
                _child_cmd('forward') + ['step'] + (['--force'] if force else []), env=env,
                cwd=(apppaths.USER_DIR if apppaths.FROZEN else PROJ),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace')
        except Exception as e:                                 # noqa: BLE001
            self._fwd_ui('lbl_fwd', text=f'step failed to start: {e}')
            return
        self._fwd_ui('btn_fwd_step', state='disabled')
        self._fwd_ui('lbl_fwd', text='stepping — downloading live closed bars and running the engine…')
        threading.Thread(target=self._fwd_wait, args=(self._fwd_proc,), daemon=True).start()

    def _fwd_wait(self, proc):
        out = proc.stdout.read() if proc.stdout else ''
        proc.wait()
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        tail = lines[-1] if lines else ''
        self.root.after(0, lambda: self._fwd_step_done(tail))

    def _fwd_step_done(self, tail):
        try:
            self._fwd_ui('btn_fwd_step', state='normal')
            self._fwd_refresh(status=tail)
        except tk.TclError:                                    # window closed while stepping
            pass

    def _fwd_ui(self, name, **kw):
        """Touch a forward-card widget IF the dashboard is up. Steps run under the simple
        shell too (its enroll proposal arms the same tick), where none of these exist."""
        w = getattr(self, name, None)
        try:
            if w is not None and w.winfo_exists():
                w.configure(**kw)
        except tk.TclError:
            pass

    def _fwd_refresh(self, status=None):
        ft = self._fwd_lib()
        entries = [e for e in ft.load_track()['entries'] if not e.get('archived')]
        self._fwd_entries = entries
        tree = getattr(self, 'fwd_tree', None)            # simple shell: entries updated, no table
        if tree is None or not tree.winfo_exists():
            return
        self._kill_cell_overlay(self.fwd_tree)
        for i in self.fwd_tree.get_children():
            self.fwd_tree.delete(i)
        for e in entries:
            m = ft.metrics(e)
            tf = e.get('tf', '1d')
            kind = e['kind'] if tf == '1d' else f'{e["kind"]}·{tf}'
            self.fwd_tree.insert('', 'end', iid=e['id'], values=(
                e['id'], kind, e.get('session') or '—', e['enrolled'], m['days'],
                f'${m["equity"]:,.0f}', f'{m["ret"] * 100:+.1f}%',
                (f'{m["sharpe"]:+.2f}' if m['sharpe'] is not None else '—'),
                (f'{m["dd"] * 100:.0f}%' if m['dd'] is not None else '—'),
                m['last'] or '—'))
        if entries:
            base = (f'{len(entries)} enrolled · steps run automatically once per closed daily bar '
                    '(UTC) while the app is open · history is append-only — forward numbers are '
                    'written by live steps only, never recomputed.')
        else:
            base = ('empty — enroll a champion (double-click it in the leaderboard → "Forward '
                    'track") or the built portfolio ("Track"). Enrollment freezes the strategy; '
                    'the node then paper-steps it daily on live data, append-only.')
        self.lbl_fwd.configure(text=(f'{status}   ·   {base}' if status else base))

    def _fwd_tick(self):
        """Step whenever any enrolled strategy has an unseen CLOSED bar of its own timeframe.
        Re-checks every 5 minutes — cheap (reads one small JSON); missed bars are harmless
        (mark-to-market covers the gap on the next step)."""
        try:
            ft = self._fwd_lib()
            due = [e for e in ft.load_track()['entries']
                   if not e.get('archived') and ft.is_due(e)]
            if due and not (self._fwd_proc and self._fwd_proc.poll() is None):
                self._fwd_step()
        except Exception:                                      # noqa: BLE001 — never kill the loop
            pass
        self.root.after(5 * 60 * 1000, self._fwd_tick)

    def _fwd_selected(self):
        sel = self.fwd_tree.selection()
        if not sel:
            messagebox.showinfo('Forward track', 'Select a strategy in the table first.',
                                parent=self.root)
            return None
        return next((e for e in self._fwd_entries if e['id'] == sel[0]), None)

    def _fwd_delete(self):
        e = self._fwd_selected()
        if not e:
            return
        if not messagebox.askyesno('Forward track',
                                   f'Delete {e["id"]}? Its paper history is deleted with it — '
                                   'this cannot be undone. The formula itself stays in your '
                                   'library; you can enroll it again anytime.',
                                   parent=self.root):
            return
        ft = self._fwd_lib()
        track = ft.load_track()
        track['entries'] = [x for x in track['entries'] if x['id'] != e['id']]
        ft.save_track(track)                              # a step racing this save cannot bring
        self._fwd_refresh()                               # it back: sync_entry_to_disk drops it

    def _fwd_serve(self):
        """Serve the SELECTED paper bot as a live signal API — the exact frozen strategy the
        track steps (formulas, universe, bar size, vol, fee, warm-up start), not the panel's
        current settings. Label = entry id: the row dedups against itself, and a restarted
        GUI re-adopts the service under the same name (see _sig_restore)."""
        e = self._fwd_selected()
        if not e:
            return
        self._serve_signal(list(e.get('formulas') or []), e['id'],
                           tickers=list(e.get('tickers') or []), tf=e.get('tf', '1d'),
                           vol=e.get('vol'), exec_cost=e.get('exec'),
                           start=e.get('engine_start'))

    @staticmethod
    def _fwd_book_str(d):
        """{'BTCUSDT': -0.224, ...} -> 'BTC −22.4% · ETH +5.1%' (base asset, signed % of equity)."""
        if not d:
            return '—'
        items = sorted(d.items(), key=lambda kv: -abs(kv[1]))
        return ' · '.join(f'{t.replace("USDT", "")} {v * 100:+.1f}%' for t, v in items[:8]) \
            + (' …' if len(items) > 8 else '')

    def _fwd_signals(self):
        e = self._fwd_selected()
        if not e:
            return
        hist = e.get('history') or []
        if not hist:
            messagebox.showinfo('Forward signals', 'No steps yet — the log appears after the '
                                                   'first live step.', parent=self.root)
            return
        win = self._dialog(f'Forward signals — {e["id"]}', '1080x600')
        frm = self._box(win)
        frm.pack(fill='both', expand=True, padx=16, pady=14)
        st = e.get('state') or {}
        pos, prc = st.get('positions') or {}, st.get('prices') or {}
        eq = float(st.get('equity') or e['start_capital'])
        now_book = {t: u * float(prc.get(t, 0.0)) / eq for t, u in pos.items() if eq > 0}
        self._lbl(frm, text=f'Holding now:  {self._fwd_book_str(now_book)}',
                  text_color=TXT, font=(self.MONO, 13, 'bold')).pack(anchor='w')
        self._lbl(frm, text='book = signed % of equity per asset · trade = executed rebalance in $ '
                            '· rows before this feature show “—” (the log is append-only)',
                  text_color=FAINT, font=(self.UI, 11)).pack(anchor='w', pady=(2, 8))
        row = self._box(frm)                              # bottom bar FIRST — the table then takes
        row.pack(side='bottom', fill='x', pady=(10, 0))   # what's left and can't push it out of view
        self._btn(row, 'Export CSV (full log)', lambda: self._fwd_signals_csv(e),
                  width=190).pack(side='right')
        hist_note = self._lbl(row, text='', text_color=FAINT, font=(self.UI, 11))
        hist_note.pack(side='left')
        wrap = self._box(frm)
        wrap.pack(fill='both', expand=True)
        cols = ('date', 'book', 'trades', 'pnl', 'funding', 'fees')
        tv = ttk.Treeview(wrap, columns=cols, show='headings', height=12)
        for c, txt, w, anc in (('date', 'BAR', 130, 'center'), ('book', 'BOOK HELD', 330, 'w'),
                               ('trades', 'REBALANCE ($)', 300, 'w'),
                               ('pnl', 'P&L $', 90, 'e'), ('funding', 'FUND $', 76, 'e'),
                               ('fees', 'FEES $', 80, 'e')):
            tv.heading(c, text=txt)
            tv.column(c, width=int(w * self.SCALE), anchor=anc, stretch=(c in ('book', 'trades')))
        self._selectable_cells(tv)
        shown = hist[-400:]                               # a 1h track grows long — cap the widget
        for i, h in enumerate(reversed(shown)):           # latest first
            tr = h.get('trades')
            trs = ('—' if tr is None else
                   (' · '.join(f'{t.replace("USDT", "")} {v:+,.0f}'
                               for t, v in sorted(tr.items(), key=lambda kv: -abs(kv[1]))[:8])
                    or 'no trades'))
            fund = h.get('funding')                       # pre-feature rows have no key: show —
            tv.insert('', 'end', tags=('odd' if i % 2 else 'even',), values=(
                h['date'], self._fwd_book_str(h.get('pos')) if h.get('pos') is not None else '—',
                trs, f'{h.get("pnl", 0):+,.2f}',
                ('—' if fund is None else f'{fund:+,.2f}'), f'{h.get("fees", 0):,.2f}'))
        tv.tag_configure('odd', background=STRIPE)
        tv.tag_configure('even', background=CARD)
        vs = ctk.CTkScrollbar(wrap, orientation='vertical', command=tv.yview, fg_color=CARD,
                              button_color=BORDER, button_hover_color=FAINT, width=14)
        tv.configure(yscrollcommand=vs.set)
        tv.pack(side='left', fill='both', expand=True)
        vs.pack(side='right', fill='y')
        if len(hist) > len(shown):
            hist_note.configure(text=f'showing last {len(shown)} of {len(hist)} steps — '
                                     'the CSV has all of them')

    def _fwd_signals_csv(self, e):
        path = filedialog.asksaveasfilename(
            parent=self.root, title='Save forward signals log', defaultextension='.csv',
            initialfile=f'forward_{e["id"]}.csv',
            filetypes=[('CSV', '*.csv'), ('All files', '*.*')])
        if not path:
            return
        rows = []
        for h in e.get('history') or []:
            pos, tr = h.get('pos') or {}, h.get('trades') or {}
            fund = h.get('funding')                       # '' = pre-feature row OR unknown (null)
            fund = '' if fund is None else fund
            for t in sorted(set(pos) | set(tr), key=lambda x: -abs(pos.get(x, 0.0))):
                rows.append([h['date'], t, pos.get(t, ''), tr.get(t, ''),
                             h.get('pnl', ''), fund, h.get('fees', ''), h.get('equity', '')])
            if not pos and not tr:                        # pre-feature rows / flat bars stay visible
                rows.append([h['date'], '', '', '', h.get('pnl', ''), fund, h.get('fees', ''),
                             h.get('equity', '')])
        self._save_csv(path, ('bar', 'ticker', 'book_frac', 'trade_usd', 'step_pnl',
                              'step_funding', 'step_fees', 'equity'), rows, 'signal rows')

    def _fwd_chart(self):
        e = self._fwd_selected()
        if not e:
            return
        hist = e.get('history') or []
        if not hist:
            messagebox.showinfo('Forward track', 'No steps yet — the curve appears after the '
                                                 'first daily step.', parent=self.root)
            return
        import matplotlib
        from matplotlib.figure import Figure
        import pandas as pd
        win = self._dialog(f'Forward — {e["id"]}  (wheel: zoom · double-click: reset)',
                           f'{int(1040 / self.SCALE)}x{int(580 / self.SCALE)}')
        body = self._box(win)
        body.pack(fill='both', expand=True, padx=14, pady=12)
        x = pd.to_datetime([h['date'] for h in hist])
        y = [h['equity'] for h in hist]
        with self._plot_lock, matplotlib.rc_context(self._mpl_rc()):
            fig = Figure(figsize=(9.9, 4.5), dpi=100, facecolor=CARD)
            ax = fig.add_subplot(111)
            ax.plot(x, y, lw=2.0, color=ACC, label=f'{e["id"]} · {len(hist)} live steps')
            ax.axhline(e['start_capital'], color=MUT, lw=0.9, ls='--')
            ax.set_title(f'forward equity — enrolled {e["enrolled"]} · append-only '
                         '(live steps, nothing recomputed)', fontsize=9)
            ax.legend(loc='upper left', fontsize=8)
            ax.grid(True, alpha=0.3)
            ax.tick_params(labelsize=8)
            fig.tight_layout()
        self._embed_fig(body, fig)

    # ---------- PDF analytics report (single alpha and the portfolio) ----------
    def _worker_cfg(self):
        """The engine settings (universe, vol/fee, TRAIN/VAL/TEST dates) a worker process needs to
        reproduce what the leaderboard searched with. Shared by the PDF worker payloads."""
        c = self.cfg
        return {
            'instruments': (_parse_universe(c.get('universe_list', '')) or None),
            'vol': float(c.get('target_vol', 0.25)), 'exec': float(c.get('exec_cost', 0.001)),
            'train_start': _seg_res(self._tf(), 'train_start', c.get('train_start')),
            'val_start': _seg_res(self._tf(), 'val_start', c.get('val_start')),
            'test_start': _seg_res(self._tf(), 'test_start', c.get('test_start')),
            'test_end': _seg_res(self._tf(), 'test_end', c.get('test_end')),
        }

    def _run_pdf_report(self, payload, out_path):
        """Build the report in a CHILD process (pdf_worker) and wait on its pipe from a background
        thread. matplotlib's FreeType text rasterization segfaults when it runs while Tk's main loop
        is live — both drive Xft/FreeType and the X error lands asynchronously as a SIGSEGV. A
        subprocess has its own address space (no shared FreeType) and its own GIL, so the GUI merely
        waits on a pipe. Same pattern as the leaderboard metrics worker.

        The thread only writes into `holder`; the MAIN thread polls it via after() and shows the
        dialog — Tk is not thread-safe, so we never touch a widget from the worker thread (this is
        exactly how the equity chart delivers its result, see _open_plot/_check_plot)."""
        payload = dict(payload, out=out_path)
        holder = {'done': False, 'info': None, 'err': None}

        def work():
            try:
                env = dict(os.environ)
                env.update(ALPHANODE_STATE_DIR=STATE_DIR, ALPHANODE_DATA=self._data_file(),
                   ALPHANODE_TF=self._tf(),
                           ALPHANODE_CONFIG_INI=apppaths.config_ini())
                proc = subprocess.Popen(_child_cmd('pdfreport'), env=env,
                                        cwd=(apppaths.USER_DIR if apppaths.FROZEN else PROJ),
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL,
                                        text=True, encoding='utf-8', errors='replace')
                try:
                    out, _ = proc.communicate(json.dumps(payload), timeout=600)
                except Exception:
                    proc.kill()                          # don't leave a runaway child behind
                    raise
                doc = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
                if not doc.get('ok'):
                    raise RuntimeError(doc.get('error', 'pdf worker failed'))
                holder['info'] = doc.get('info') or {}
            except Exception as e:                       # noqa: BLE001
                holder['err'] = f'{type(e).__name__}: {e}'
            finally:
                holder['done'] = True

        threading.Thread(target=work, daemon=True).start()
        self.root.after(150, lambda: self._check_pdf(holder, out_path))

    def _check_pdf(self, holder, out_path):
        """Main-thread poll of the PDF worker; show the result dialog when it finishes."""
        if not holder['done']:
            self.root.after(150, lambda: self._check_pdf(holder, out_path))
            return
        if holder['err']:
            messagebox.showerror('PDF report', f'Failed to build report: {holder["err"]}',
                                 parent=self.root)
        else:
            self._pdf_done_dialog(out_path, holder['info'])

    def _pdf_done_dialog(self, path, info):
        win = self._dialog('PDF report', '440x190')
        frm = self._box(win)
        frm.pack(fill='both', expand=True, padx=18, pady=16)
        self._lbl(frm, text='Report saved', text_color=TXT,
                     font=(self.UI, 19, 'bold')).pack(anchor='w')
        self._lbl(frm, text=path, text_color=MUT, font=(self.MONO, 12), wraplength=390,
                     justify='left', anchor='w').pack(anchor='w', pady=(2, 6))
        self._lbl(frm, text=f'{(info or {}).get("pages", "?")} pages · '
                            f'{(info or {}).get("days", "?")} trading days',
                     text_color=FAINT, font=(self.UI, 12)).pack(anchor='w', pady=(0, 10))
        row = self._box(frm)
        row.pack(anchor='w')
        self._btn(row, 'Open folder', lambda: self._open_folder(path), width=120).pack(side='left')
        self._btn(row, 'Close', win.destroy, width=90).pack(side='left', padx=(8, 0))

    def _pdf_report_alpha(self, champ, status_lbl=None):
        formula = champ.get('formula', '')
        if not formula:
            return
        name = 'alpha_' + hashlib.md5(formula.encode()).hexdigest()[:6]
        path = filedialog.asksaveasfilename(
            parent=self.root, title='Save PDF report', defaultextension='.pdf',
            initialfile=f'report_{name}.pdf', filetypes=[('PDF', '*.pdf')])
        if not path:
            return
        if status_lbl is not None:
            status_lbl.configure(text='building PDF report…')
        seg = {k: dict(champ[k]) for k in ('train', 'val', 'test')
               if isinstance(champ.get(k), dict)}
        payload = {'kind': 'alpha', 'formula': formula, 'seg_metrics': seg,
                   'title': 'AlphaNode — alpha report', 'subtitle': formula,
                   'stamp': f'AlphaNode · {datetime.now():%d.%m.%Y %H:%M} · {name}',
                   **self._worker_cfg()}
        self._run_pdf_report(payload, path)

    def _pf_pdf_report(self):
        doc = self._pf_doc
        if not doc or not doc.get('weights'):
            messagebox.showinfo('PDF report',
                                'Build the portfolio first (rebuild it if it was built by an '
                                'older version).', parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title='Save PDF report', defaultextension='.pdf',
            initialfile=f'report_portfolio_top{doc.get("n", "")}.pdf',
            filetypes=[('PDF', '*.pdf')])
        if not path:
            return
        slim = {k: doc.get(k) for k in ('weights', 'equity', 'test', 'metrics', 'n')}
        payload = {'kind': 'portfolio', 'doc': slim,
                   'title': f'AlphaNode — top-{doc.get("n", "?")} portfolio',
                   'subtitle': ' + '.join(doc.get('formulas', []))[:110],
                   'stamp': f'AlphaNode · {datetime.now():%d.%m.%Y %H:%M} · portfolio '
                            f'top-{doc.get("n", "?")} · TEST {doc.get("test", "")}',
                   **self._worker_cfg()}
        self._run_pdf_report(payload, path)

    # ---------- PORTFOLIO: CSV signals (same as a single alpha) ----------
    def _pf_download_signals(self):
        doc = self._pf_doc
        if not doc or not doc.get('weights'):
            messagebox.showinfo('Portfolio signals',
                                'Build the portfolio first (rebuild it if it was built by an older version).',
                                parent=self.root)
            return
        if doc.get('tf', '1d') == '1d' and doc.get('weights_span') != 'full':
            if not messagebox.askyesno(
                    'Portfolio signals',
                    'This portfolio was built by an OLDER version — its stored weights cover '
                    'TEST only.\nPress "▶ Build portfolio" again and the CSV will span the whole '
                    'TRAIN/VAL/TEST history.\n\nExport the TEST-only CSV anyway?',
                    icon='warning', default='no', parent=self.root):
                return
        path = filedialog.asksaveasfilename(
            parent=self.root, title='Save portfolio signals', defaultextension='.csv',
            initialfile=f'signals_portfolio_top{doc.get("n", "")}.csv',
            filetypes=[('CSV', '*.csv'), ('All files', '*.*')])
        if not path:
            return
        try:
            import numpy as np
            import pandas as pd
            w = doc['weights']
            idx = pd.to_datetime(w['dates']).tz_localize('UTC')   # match the tz-aware splits
            wide = pd.DataFrame(np.array(w['W'], dtype=float), index=idx, columns=w['tickers'])
            cfg = self._build_plot_cfg()
            try:                                                   # OHLCV columns are best-effort:
                _tk, panel, _market, _b = self._get_market(cfg)    # no data snapshot -> weights-only CSV
            except Exception:                                      # noqa: BLE001
                panel = None
            self._signals_from_wide(wide, cfg['splits'], path, panel=panel)
        except Exception as e:                                     # noqa: BLE001
            messagebox.showerror('Error', f'Failed to build signals: {e}', parent=self.root)

    def _signals_dialog(self, path, latest_date, positions, n_days, rng=None):
        win = self._dialog('Portfolio signals', '460x580')
        frm = self._box(win)
        frm.pack(fill='both', expand=True, padx=18, pady=16)
        self._lbl(frm, text='Signals saved', text_color=TXT,
                     font=(self.UI, 19, 'bold')).pack(anchor='w')
        self._lbl(frm, text=path, text_color=MUT, font=(self.MONO, 12), wraplength=400,
                     justify='left', anchor='w').pack(anchor='w', pady=(2, 12))
        self._lbl(frm, text=f'What to hold on the last day ({latest_date}):', text_color=TXT,
                     font=(self.UI, 15, 'bold')).pack(anchor='w')
        self._lbl(frm, text='+ long · − short · %  = share of portfolio', text_color=FAINT,
                     font=(self.UI, 12)).pack(anchor='w', pady=(0, 8))
        tbl = self._box(frm)
        tbl.pack(fill='both', expand=True)
        for i, (t, w) in enumerate(positions[:16]):
            side, col = ('LONG', POS) if w > 0 else ('SHORT', NEG)
            rbg = STRIPE if i % 2 else CARD
            r = self._box(tbl, bg=rbg)
            r.pack(fill='x')
            self._lbl(r, text=side, text_color=col, font=(self.MONO, 13, 'bold'),
                         width=52, anchor='w').pack(side='left', padx=(6, 0))
            self._lbl(r, text=t, text_color=TXT, font=(self.MONO, 13), anchor='w').pack(side='left')
            self._lbl(r, text=f'{w * 100:+.1f}%', text_color=col, font=(self.MONO, 13, 'bold'),
                         anchor='e').pack(side='right', padx=(0, 8))
        covered = f'{n_days} bars' + (f', {rng}' if rng else '')
        self._lbl(frm, text=f'Full history ({covered}) — in the CSV: date, segment '
                            '(TRAIN/VAL/TEST), ticker, side, weight, weight_pct + the asset\'s '
                            'open/high/low/close/volume.',
                     text_color=MUT, font=(self.UI, 12), wraplength=400, justify='left',
                     anchor='w').pack(anchor='w', pady=(10, 0))
        self._btn(frm, 'Close', win.destroy, width=80).pack(anchor='e', pady=(10, 0))

    def _open_folder(self, path):
        """Open a file or a folder in the system viewer."""
        if not path:
            return
        start = getattr(os, 'startfile', None)           # Windows
        if start is not None:
            try:
                start(path)
                return
            except Exception:                            # noqa: BLE001
                pass
        for opener in ('xdg-open', 'open'):
            try:
                subprocess.Popen([opener, path])
                return
            except Exception:                            # noqa: BLE001
                continue

    @staticmethod
    def _storm_spans(reg):
        """Contiguous (t0, t1) runs of the high-vol regime out of evaluator.vol_regime's
        1/0/NaN series — what the equity chart tints. NaN warmup bars break a run."""
        spans, t0, prev_t = [], None, None
        for t, v in reg.items():
            if v == 1.0 and t0 is None:
                t0 = t
            elif v != 1.0 and t0 is not None:            # 0.0 or NaN ends the run
                spans.append((t0, t)); t0 = None
            prev_t = t
        if t0 is not None and prev_t is not None:
            spans.append((t0, prev_t))
        return spans

    # ---------- formula passport (anatomy + behavior; offline, deterministic) ----------
    def _formula_passport(self, champ):
        self._plot_seq += 1
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        img_w = int(min(1560, max(1000, sw * 0.78)))
        img_h = int(img_w * 0.82)
        avail_h = int(sh * 0.92) - 120
        if img_h > avail_h:
            img_h = max(560, avail_h)
            img_w = int(img_h / 0.82)
        dpi = 110
        holder = {'done': False, 'fig': None, 'err': None, 'dpi': dpi,
                  'figsize': (img_w / dpi, (img_h - 40) / dpi)}
        win = self._dialog('Formula passport — ' + champ.get('formula', '')[:56],
                           f'{int((img_w + 44) / self.SCALE)}x{int((img_h + 90) / self.SCALE)}')
        body = self._box(win)
        body.pack(fill='both', expand=True, padx=16, pady=(10, 14))
        status = self._lbl(body, text='building the passport (tree · ablation · archetypes)…',
                           text_color=MUT, font=(self.UI, 15))
        status.pack(pady=40)
        threading.Thread(target=self._compute_passport, args=(champ, holder), daemon=True).start()
        self.root.after(200, lambda: self._check_plot(win, holder, status, body))

    def _compute_passport(self, champ, holder):
        with self._plot_lock:                            # figure building is serialized
            try:
                import matplotlib
                import formula_passport as fp
                cfg = self._build_plot_cfg()
                tk_, panel, market, _basket = self._get_market(cfg)
                ann = float(cfg.get('ann', 365.0))
                with matplotlib.rc_context(self._mpl_rc()):
                    holder['fig'] = fp.build_figure(
                        champ['formula'], panel, market, tk_, cfg['vol'], cfg['exec'],
                        ann=ann, ewma=cfg.get('ewma_lambda', 0.06), ppd=ann / 365.0,
                        tf_name=cfg.get('tf', '1d'), figsize=holder['figsize'],
                        dpi=holder['dpi'], facecolor=CARD, fg=MUT, ink=TXT, accent=ACC)
            except Exception as e:                       # noqa: BLE001
                holder['err'] = f'{type(e).__name__}: {e}'
            finally:
                holder['done'] = True

    def _compute_equity(self, champ, holder):
        with self._plot_lock:                            # pyplot is global — one at a time
            try:
                import matplotlib
                from genome import parse
                from evaluator import simulate_returns
                import report
                cfg = self._build_plot_cfg()
                tk_, panel, market, basket = self._get_market(cfg)
                r = simulate_returns(parse(champ['formula']), tk_, panel, market, cfg['vol'],
                                     cfg['exec'], ann=cfg.get('ann', 365.0),
                                     ewma_lambda=cfg.get('ewma_lambda', 0.06))
                if r is None:
                    holder['err'] = 'the formula yields no valid returns on this data'
                else:
                    ts = (champ.get('test') or {}).get('sharpe')
                    label = 'strategy' + (f' · TEST Sharpe {ts:+.2f}' if ts is not None else '')
                    try:                                 # open-PnL panel: best-effort, chart survives without
                        from evaluator import open_pnl_series
                        wide = self._alpha_weights_wide(champ['formula'], cfg, market, panel)
                        op = open_pnl_series(wide, panel['ret'])
                    except Exception:                    # noqa: BLE001
                        op = None
                    try:                                 # storm tint: best-effort as well
                        from evaluator import vol_regime
                        storm = self._storm_spans(vol_regime(
                            panel, vol_window=cfg.get('vol_window', 30),
                            ann=cfg.get('ann', 365.0)))
                    except Exception:                    # noqa: BLE001
                        storm = None
                    with matplotlib.rc_context(self._mpl_rc()):
                        # a plain Figure (no pyplot): safe to build here in the worker thread and
                        # embed as a LIVE zoomable canvas on the main thread (_check_plot)
                        holder['fig'] = report.equity_figure(
                            {label: r}, basket, cfg['splits'],
                            'Growth of $1 (NET, log):  TRAIN | VAL | TEST   vs   EW basket (buy & hold)',
                            figsize=holder['figsize'], dpi=holder['dpi'],
                            facecolor=CARD, fg=MUT, axline=TXT, open_pnl=op, storm=storm)
            except Exception as e:                       # noqa: BLE001
                holder['err'] = f'{type(e).__name__}: {e}'
            finally:
                holder['done'] = True

    def _check_plot(self, win, holder, status, body):
        if not win.winfo_exists():
            return
        if not holder['done']:
            self.root.after(200, lambda: self._check_plot(win, holder, status, body))
            return
        if holder['err']:
            status.configure(text='Error: ' + holder['err'], fg=NEG)
            return
        try:
            self._embed_fig(body, holder['fig'])          # live canvas: wheel zoom / pan / reset
            status.destroy()                              # only AFTER a successful embed — the
        except Exception as e:                            # noqa: BLE001    reversed order left a
            status.configure(text=f'Failed to show the chart: {e}', fg=NEG)   # blank window with
            #                                             the error written to a destroyed label


def main():
    root = ctk.CTk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()

# -*- mode: python ; coding: utf-8 -*-
"""Cross-platform build of AlphaNode (Linux AppImage / Windows .exe) with a single entry point
(alphanode/app_entry.py) that, based on --role, launches the GUI / node / data fetcher.

We build onedir (a folder) — for AppImage this is optimal (the AppImage itself is the compression layer).
On Windows CI, the .exe and installer are assembled from this same folder.
"""
import os
import re
import sys
from PyInstaller.utils.hooks import collect_submodules

PROJ = os.path.dirname(SPECPATH)                     # packaging/ -> repository root
APP = os.path.join(PROJ, 'alphanode')

# The engine and its dependencies are imported dynamically (via sys.path) — list them explicitly, plus
# collect all quantpylib submodules (it's pulled in by the qt data fetcher and the portfolio engine).
hiddenimports = (
    ['fetch_data', 'apppaths', 'node', 'alphanode_gui', 'portfolio_build', 'cli',
     'signal_service', 'metrics_worker', 'pdf_report', 'pdf_worker', 'rescore_library',
     'forward_track', 'version', 'buildinfo', 'round_eta', 'series_cache', 'licence_store',
     'config', 'evolution', 'evaluator', 'fastsim', 'genome', 'primitives',
     'report', 'evolved_strategy', 'experiments',
     # imports of the COMPILED core, which modulegraph can no longer scan (.so has no source):
     # configparser is imported ONLY by config.py; timeframe would otherwise silently fall back
     # to daily annualization (config.py wraps it in try/except) — both must be forced in.
     'timeframe', 'configparser',
     'aiosonic', 'orjson', 'onecache', 'pytz',       # pytz: imported by quantpylib.wrappers.binance
     'PIL.ImageTk', 'PIL._tkinter_finder',           # loaded DYNAMICALLY by PIL.ImageTk when the
     #                              mpl Tk toolbar builds its icons — invisible to the import scan;
     #                              without them every chart window dies on NavigationToolbar2Tk
     'customtkinter', 'darkdetect']                  # GUI toolkit (assets come from the contrib hook)
    + collect_submodules('quantpylib')
)

# Bundled default data snapshot (so the app works right away, before the first fetch).
# If the file is missing (e.g. a clean CI checkout) — don't fail the build; the app will prompt to download data.
datas = []
_data = os.path.join(PROJ, 'data.pickle')
if os.path.exists(_data):
    datas.append((_data, '.'))
else:
    print('AlphaNode.spec: data.pickle not found — building without a bundled snapshot')

# The vendor's PUBLIC vault key. Without it the node mines UNSEALED — the whole subscription
# model silently stops protecting anything — so a release build must not be allowed to omit it.
# Fetch before building:  curl -s https://api.<DOMAIN>/pub.txt > alphanode/vault_server_key.pub
_pub = os.path.join(APP, 'vault_server_key.pub')
if os.path.exists(_pub):
    datas.append((_pub, 'alphanode'))
elif os.environ.get('ALPHANODE_ALLOW_UNSEALED') == '1':
    print('AlphaNode.spec: no vault_server_key.pub — building an UNSEALED node (dev only)')
else:
    raise SystemExit(
        'AlphaNode.spec: alphanode/vault_server_key.pub is missing.\n'
        '  curl -s https://api.<YOUR-DOMAIN>/pub.txt > alphanode/vault_server_key.pub\n'
        '  (or set ALPHANODE_ALLOW_UNSEALED=1 for a deliberately unprotected dev build)')

# The ONE engine data file read at runtime: config.ini (the shipped defaults the ALPHANODE_*
# overrides layer over). load_config hard-fails on a missing path (config.py), and the GUI/node
# point ALPHANODE_CONFIG_INI here (apppaths.py), so it must ship — but nothing else from
# evolution/ does. Shipped explicitly now that the source Tree below is gone.
datas.append((os.path.join(PROJ, 'evolution', 'config.ini'), 'evolution'))

# Build identity (packaging/make_build_stamp.py) + the EULA. The stamp ships beside buildinfo
# (bundle root == _internal, where buildinfo.__file__ resolves) so the app can report its
# build_id and the hub can log it; the licence ships at the bundle root so the app can show it.
_stamp = os.path.join(APP, '_build_stamp.json')
if os.path.exists(_stamp):
    datas.append((_stamp, '.'))
else:
    print('AlphaNode.spec: no _build_stamp.json — run packaging/make_build_stamp.py first '
          '(building with a dev build identity)')
_license = os.path.join(PROJ, 'LICENSE.txt')
if os.path.exists(_license):
    datas.append((_license, '.'))

# The engine core ships as NATIVE extensions (Cython), never bytecode: a .pyc decompiles back to
# near-source in seconds, machine code does not. packaging/cythonize_engine.py builds the five
# into packaging/cyext/; that dir goes FIRST on the search path, so Analysis resolves those names
# to the extensions and their .py never reaches the PYZ. fastsim is deliberately NOT compiled —
# numba must JIT its kernel from Python (see cythonize_engine.py). Like the vault key above, a
# release build must not be allowed to silently fall back to the unprotected form.
CYEXT = os.path.join(SPECPATH, 'cyext')
_cy_need = ('primitives', 'genome', 'evolution', 'evaluator', 'config')
_cy_have = {f.split('.')[0] for f in (os.listdir(CYEXT) if os.path.isdir(CYEXT) else [])
            if f.endswith(('.so', '.pyd'))}
if all(m in _cy_have for m in _cy_need):
    pathex = [CYEXT, PROJ, APP, os.path.join(PROJ, 'evolution')]
elif os.environ.get('ALPHANODE_ALLOW_BYTECODE_ENGINE') == '1':
    print('AlphaNode.spec: no cyext/ — the engine ships as DECOMPILABLE bytecode (dev only)')
    pathex = [PROJ, APP, os.path.join(PROJ, 'evolution')]
else:
    raise SystemExit(
        'AlphaNode.spec: packaging/cyext/ is missing or incomplete — the engine would ship as\n'
        'decompilable bytecode. Build the native extensions first:\n'
        '  python packaging/cythonize_engine.py\n'
        '  (or set ALPHANODE_ALLOW_BYTECODE_ENGINE=1 for a deliberately unprotected dev build)')

a = Analysis(
    [os.path.join(APP, 'app_entry.py')],
    pathex=pathex,
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    # matplotlib backends are NOT auto-discovered correctly: the hook's automatic mode only sees
    # the explicit matplotlib.use('Agg') calls (pdf_worker, report) and drops backend_tkagg —
    # which the GUI imports lazily inside _embed_fig, so every equity/progress chart in the
    # frozen app dies with ModuleNotFoundError. Pin both backends; selfcheck embeds a real
    # TkAgg canvas so this can never regress silently again.
    hooksconfig={'matplotlib': {'backends': ['Agg', 'TkAgg']}},
    runtime_hooks=[],
    excludes=['seaborn', 'statsmodels', 'IPython', 'pytest', 'notebook',
              'jupyterlab', 'tkinter.test'],
    noarchive=False,
)

# The engine core rides in a.binaries as the cyext native extensions; the glue (fastsim, report,
# evolved_strategy, experiments, timeframe) stays bytecode in the PYZ. It was once dumped here as
# raw .py via Tree('evolution') — which handed the entire search algorithm to anyone who unzipped
# the AppImage. The only evolution/ file on disk is config.ini, shipped explicitly above; a
# frozen `import evolution` still finds the .so, because FileFinder prefers a module file over
# the _internal/evolution data dir (a mere namespace-package candidate on the same path entry).
#
# quantpylib stays as source: it is a third-party dependency (throttler + exchange wrappers), not
# our IP, and collect_submodules can miss its dynamic imports.
a.datas += Tree(os.path.join(PROJ, 'quantpylib'), prefix='quantpylib',
                excludes=['__pycache__', '*.pyc', '*.pyo'])

# The no-source guarantee above rests on pathex ORDER, which nothing enforces at runtime — so
# prove it at build time: every core module must be an extension in a.binaries and absent from
# the PYZ. A regression here (a pathex reorder, a stray config.py in alphanode/, a package-style
# import) would otherwise re-admit the algorithm as decompilable bytecode without any error.
if pathex[0] == CYEXT:
    _pure = {e[0] for e in a.pure}
    _bins = {e[0] for e in a.binaries}
    _leaked = [m for m in _cy_need if m in _pure]
    _absent = [m for m in _cy_need
               if not any(b == m or (b.startswith(m + '.') and b.endswith(('.so', '.pyd')))
                          for b in _bins)]
    if _leaked or _absent:
        raise SystemExit(f'AlphaNode.spec: engine protection regressed — in PYZ: {_leaked}, '
                         f'missing from binaries: {_absent}')

pyz = PYZ(a.pure)

_ico = os.path.join(SPECPATH, 'alphanode.ico')
exe = EXE(
    pyz,
    a.scripts,
    # Python UTF-8 mode for the WHOLE frozen process: on a cp1251/… Windows every open() without
    # an explicit encoding= otherwise writes/reads the ANSI codepage, and the first '▶'/'→' in a
    # status/log file kills that role (json.dump of status.json did exactly this). Linux builds
    # already run UTF-8 via the locale; this makes Windows behave the same. Explicit encoding=
    # stays best practice — this is the safety net for the call nobody remembered.
    [('X utf8=1', None, 'OPTION')],                  # CPython's -X utf8 (NOT 'utf8_mode' — that is
    #                                                  the sys.flags name; a wrong key is silently
    #                                                  ignored, which selfcheck now catches)
    exclude_binaries=True,
    name='AlphaNode',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                                   # windowed application (no black console)
    disable_windowed_traceback=False,
    icon=(_ico if os.path.exists(_ico) else None),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='AlphaNode',
)

# macOS: wrap the onedir into a proper .app bundle (Finder/Dock/Gatekeeper all want one).
# The plain onedir stays alongside it — CI runs selfcheck/smoke against that binary directly.
# The bundle ships UNSIGNED until an Apple Developer ID exists: first launch is
# right-click -> Open (Gatekeeper), which the download page must say.
if sys.platform == 'darwin':
    _ver = '0.0.0'
    try:
        _ver = re.search(r"__version__\s*=\s*'([^']+)'",
                         open(os.path.join(APP, 'version.py'), encoding='utf-8').read()).group(1)
    except Exception:                                    # noqa: BLE001
        pass
    _icns = os.path.join(SPECPATH, 'alphanode.icns')
    app = BUNDLE(
        coll,
        name='AlphaNode.app',
        icon=(_icns if os.path.exists(_icns) else None),
        bundle_identifier='tech.alphanode.app',
        info_plist={
            'CFBundleShortVersionString': _ver,
            'NSHighResolutionCapable': True,
        },
    )

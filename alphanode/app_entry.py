"""Single entry point of the built application (PyInstaller).

The same executable can launch in different roles — the GUI spawns its own child processes via
`sys.executable --role <role>` (in the built form there's no plain python nearby, so we launch
ourselves):

    <exe>                      → GUI (default)
    <exe> --role node          → background search node (node.main)
    <exe> --role fetch [args]  → Binance data fetcher (fetch_data.main)

In development mode this file is not used (the GUI calls scripts through real python).
"""
import os
import sys


def _fix_std_streams():
    """In a windowed build on Windows sys.stdout/stderr = None -> print() crashes. Child roles
    (node/fetch) write a log that the parent reads through a pipe: we reconnect the stream to fd 1/2,
    otherwise to /dev/null. On Linux the streams are live, so this branch is harmless."""
    for name, fd in (('stdout', 1), ('stderr', 2)):
        if getattr(sys, name, None) is None:
            try:
                stream = os.fdopen(fd, 'w', buffering=1)
            except OSError:
                stream = open(os.devnull, 'w')
            setattr(sys, name, stream)


def _prep_path():
    """Make alphanode/, the resource root, and evolution/ importable (in the bundle and in dev)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (here,):
        if p not in sys.path:
            sys.path.insert(0, p)
    import apppaths                                       # noqa: E402  (after inserting here into path)
    for p in (apppaths.RES_ROOT, os.path.join(apppaths.RES_ROOT, 'evolution')):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)


def _selfcheck():
    """Windowless diagnostics of the built bundle: imports, engine, data, Tk. Writes selfcheck.log to
    cwd and exits with code 0/1 (in a windowed build stdout may be unavailable — CI checks the code and
    the log). Used during build and in CI (<exe> --role selfcheck)."""
    import io
    import traceback
    buf = io.StringIO()

    def out(*a):
        line = ' '.join(str(x) for x in a)
        print(line)
        buf.write(line + '\n')

    try:
        _selfcheck_body(out)
    except Exception:                                    # noqa: BLE001
        buf.write('SELFCHECK FAILED\n' + traceback.format_exc())
        try:
            with open('selfcheck.log', 'w', encoding='utf-8') as f:
                f.write(buf.getvalue())
        except OSError:
            pass
        print(buf.getvalue(), file=sys.stderr)
        sys.exit(1)
    try:
        with open('selfcheck.log', 'w', encoding='utf-8') as f:
            f.write(buf.getvalue())
    except OSError:
        pass
    sys.exit(0)


def _selfcheck_body(out):
    import os
    import pickle
    import apppaths
    out('frozen      :', apppaths.FROZEN)
    out('res_root    :', apppaths.RES_ROOT)
    out('user_dir    :', apppaths.USER_DIR)
    out('config_ini  :', apppaths.config_ini(), os.path.exists(apppaths.config_ini()))
    dp = apppaths.data_path()
    out('data_path   :', dp, os.path.exists(dp))

    import numpy, pandas, matplotlib                      # noqa: F401
    out('numpy/pandas/mpl:', numpy.__version__, pandas.__version__, matplotlib.__version__)
    # The GUI imports the INTERACTIVE backend lazily (only when a chart window opens), so a
    # bundle can pass every import test above and still have no backend_tkagg — every equity/
    # progress chart then dies blank. Import it here so a broken bundle fails in CI instead.
    from matplotlib.backends import backend_tkagg         # noqa: F401
    out('mpl tkagg   : importable')

    from config import load_config
    cfg = load_config()
    out('load_config : ok, instruments =', 'all' if cfg.get('instruments') is None
        else len(cfg['instruments']))

    from evaluator import build_panel                     # noqa: F401
    from evolved_strategy import make_evolved             # noqa: F401
    from quantpylib.simulator.alpha import Portfolio      # noqa: F401
    out('engine imports: ok')

    # Protection regression guard: the five core modules must arrive as native extensions
    # (cythonize_engine.py), not as decompilable bytecode. CI greps this line.
    import config as _c, evaluator as _ev, evolution as _e, genome as _g, primitives as _p
    _soft = [m.__name__ for m in (_c, _ev, _e, _g, _p)
             if not (m.__file__ or '').endswith(('.so', '.pyd'))]
    out('engine form :', 'BYTECODE (decompilable!): ' + ','.join(_soft) if _soft
        else 'native extensions (.so)')
    if _soft and os.environ.get('ALPHANODE_ALLOW_BYTECODE_ENGINE') != '1':
        raise AssertionError('engine must ship as native extensions, got bytecode: '
                             + ','.join(_soft))

    # build identity + tamper-evidence: the stamp records the fingerprint of the vault key the
    # build shipped with; a bundle whose key was swapped or corrupted fails HERE (tamper-EVIDENT,
    # not tamper-proof — the honest limit). The licence must ship too.
    import hashlib
    import buildinfo
    bi = buildinfo.build_info()
    out('build       :', buildinfo.build_label(), '·', bi.get('built_at') or 'dev')
    _pubf = os.path.join(apppaths.RES_ROOT, 'alphanode', 'vault_server_key.pub')
    if bi.get('vault_pub_fp') and os.path.exists(_pubf):
        raw = bytes.fromhex(open(_pubf, encoding='utf-8').read().strip())
        got = hashlib.sha256(raw).hexdigest()[:16]
        assert got == bi['vault_pub_fp'], (
            f'vault key fingerprint mismatch: bundle {got} != stamp {bi["vault_pub_fp"]} '
            '(the shipped vault key was swapped or corrupted)')
        out('vault key   : matches build stamp', got)
    else:
        out('vault key   : no fingerprint to check (dev / unsealed build)')
    out('licence     :', 'present' if os.path.exists(apppaths.license_file()) else 'MISSING')
    out('hub url      :', bi.get('vault_url') or 'LOCALHOST (dev — customers cannot activate)')

    # …and the GUI must actually FIND that key. _vault_pub_path once resolved a dev-only path,
    # so every shipped node silently mined IN THE OPEN — plaintext library_*.jsonl, the exact
    # leak the vault exists to prevent. Pure path logic, so it runs even on display-less CI.
    if bi.get('vault_pub_fp'):
        import alphanode_gui as _gui
        _vp = _gui._vault_pub_path()
        assert _vp and os.path.isfile(_vp), 'GUI cannot resolve the shipped sealing key'
        _raw2 = bytes.fromhex(open(_vp, encoding='utf-8').read().strip())
        assert hashlib.sha256(_raw2).hexdigest()[:16] == bi['vault_pub_fp'], (
            'the GUI-resolved sealing key differs from the build stamp')
        out('vault resolve: gui finds the shipped key (fp matches stamp)')
    else:
        out('vault resolve: skipped (unsealed dev build)')

    # The node seals EVERY mined formula, which drags `cryptography` into the bundle — a build
    # env missing it ships an app whose FIRST search round dies on `import vault`, while every
    # selfcheck above stays green (exactly v1.4.1: CI smoke red, selfcheck OK). Prove the whole
    # seal path here, so that class of bundle fails loudly at selfcheck instead.
    if os.path.exists(_pubf):
        import vault
        _probe_tok = vault.seal('selfcheck-probe', _pubf, owner='selfcheck')
        assert _probe_tok.startswith('v2:'), 'seal produced an unexpected envelope'
        out('vault seal  : ok (cryptography in the bundle, v2 envelope minted)')
    else:
        out('vault seal  : skipped (no public key — unsealed dev build)')

    # The data fetcher role pulls the Binance wrapper (import pytz). Exercise it here so a missing
    # transitive dep (e.g. pytz) fails the build in CI instead of shipping a broken --role fetch.
    import fetch_data                                      # noqa: F401
    import signal_service                                  # noqa: F401
    import pdf_worker                                       # noqa: F401  (--role pdfreport worker)
    out('fetch/signal/pdf imports: ok')

    # Forward-track state must land in the WRITABLE user dir, never inside the bundle: the GUI
    # imports forward_track IN-PROCESS (ALPHANODE_STATE_DIR is only set for child processes), and
    # frozen __file__ points into the read-only AppImage/deb — the old "state next to the module"
    # fallback made every "Forward track +" click die silently inside the Tk callback.
    import forward_track
    _fd = forward_track._state_dir()
    if apppaths.FROZEN and not os.environ.get('ALPHANODE_STATE_DIR'):
        assert not _fd.startswith(apppaths.RES_ROOT), f'forward state dir inside the bundle: {_fd}'
    assert os.access(_fd, os.W_OK), f'forward state dir not writable: {_fd}'
    out('forward dir :', _fd)

    # render a REAL 4-page analytics PDF on synthetic data: exercises matplotlib Agg + FreeType
    # (incl. Cyrillic labels) + PdfPages inside the frozen bundle — the same code path the GUI's
    # "PDF report" buttons reach via --role pdfreport. A bundle with broken fonts/backend fails
    # HERE in CI instead of on the user's first click.
    import tempfile
    import pdf_report
    _rng = numpy.random.default_rng(7)
    _pidx = pandas.date_range('2023-01-01', periods=120, freq='D', tz='UTC')
    _pw = pandas.DataFrame(_rng.normal(0, 1, (120, 4)), index=_pidx,
                           columns=['AUSDT', 'BUSDT', 'CUSDT', 'DUSDT'])
    _pw = _pw.div(_pw.abs().sum(axis=1), axis=0)
    _pr = (_pw.shift(1) * _rng.normal(0.001, 0.02, (120, 4))).sum(axis=1)
    _pdf = os.path.join(tempfile.gettempdir(), 'alphanode_selfcheck.pdf')
    _pinfo = pdf_report.build_report(_pdf, title='AlphaNode — selfcheck', subtitle='synthetic',
                                     wide=_pw, rets=_pr, stamp='selfcheck')
    assert _pinfo['pages'] == 4, f'pdf selfcheck: {_pinfo["pages"]} pages'
    out('pdf render  : 4 pages,', os.path.getsize(_pdf) // 1024, 'KB')
    os.remove(_pdf)

    # exercise the fast fitness kernel on a tiny synthetic market: forces numba to compile inside the
    # frozen process, so a bundle that failed to include numba is visible here (as a graceful numpy
    # fallback — never a crash, thanks to _run_kernel's guard). Reports which path is active.
    import fastsim
    _T, _N = 60, 3
    _C = numpy.cumprod(numpy.full((_T, _N), 1.003), axis=0)
    _R = numpy.zeros((_T, _N)); _R[1:] = _C[1:] / _C[:-1] - 1.0
    _mk = {'C': _C, 'R': _R, 'V': numpy.full((_T, _N), 0.02),
           'base_elig': numpy.ones((_T, _N), bool),
           'index': pandas.date_range('2020-01-01', periods=_T, freq='D', tz='UTC'), 'tk': ['A', 'B', 'C']}
    fastsim.fast_sim(numpy.tile(numpy.linspace(-1.0, 1.0, _N), (_T, 1)), _mk, 0.30, 0.001)
    out('fast_sim    : ok, numba', 'ACTIVE' if fastsim._kernel_jit is not None else 'fallback(numpy)')
    if os.environ.get('ALPHANODE_REQUIRE_NUMBA') == '1' and fastsim._kernel_jit is None:
        # CI sets this: the numpy fallback is ~20x slower per genome — a release built that way
        # would look alive and mine at a crawl (and its smoke round times out instead of failing
        # with a reason). Version drift in the numba/numpy pair lands HERE, with one clear line.
        raise AssertionError('numba is not active in this bundle (numpy fallback)')

    if os.path.exists(dp):
        with open(dp, 'rb') as f:
            tk_, _oh = pickle.load(f)
        out('dataset     :', len(tk_), 'pairs')
    else:
        out('dataset     : no embedded data.pickle (download it in the app)')

    if os.environ.get('DISPLAY') or sys.platform.startswith('win'):
        import customtkinter as ctk
        import alphanode_gui
        # a CTk root, as main() builds: App styles it with CTk options a plain tk.Tk lacks.
        # Building the UI in BOTH themes is the point — it exercises every widget twice and would
        # catch a bundle whose customtkinter assets (themes/fonts) never made it into the build.
        r = ctk.CTk()
        r.withdraw()
        app = alphanode_gui.App(r)
        app._set_theme('dark' if app.cfg.get('theme') == 'light' else 'light')
        r.update()
        out('gui build   : ok, both themes, child_cmd(node) =',
            alphanode_gui._child_cmd('node')[:2], '…')
        # BOTH shells, and the switch between them: a settings file that opens on the simple
        # screen used to leave the dashboard untested here — a bundle whose dashboard build
        # raised (lazy import, missing asset) passed selfcheck and shipped with an 'Advanced'
        # button that did nothing. Callback exceptions are collected, not just printed.
        _tk_errs = []
        r.report_callback_exception = lambda *a: _tk_errs.append(a)
        _first = app._shell_mode
        for _mode in (('advanced', 'simple') if _first == 'simple' else ('simple', 'advanced')):
            app._switch_mode(_mode)
            for _ in range(20):
                r.update()
            assert app._shell_mode == _mode, f'shell did not switch to {_mode}'
        assert not _tk_errs, 'Tk callback errors during the mode switch: ' + repr(_tk_errs[0][1])
        out('mode switch : ok, simple <-> advanced (opened on', _first + ')')
        out('ctk         :', ctk.__version__, '· theme assets',
            os.path.isdir(os.path.join(os.path.dirname(ctk.__file__), 'assets', 'themes')))
        # a REAL TkAgg canvas + toolbar — the exact path every equity/progress chart takes.
        # Importing the backend proves collection; drawing into Tk and building the toolbar
        # (its icons live in mpl-data/images) prove the whole _embed_fig chain works here.
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        _fig = Figure(figsize=(3, 2), dpi=72)
        _fig.add_subplot(111).plot([1, 2, 3], [1, 4, 2])
        _cv = FigureCanvasTkAgg(_fig, master=r)
        _cv.draw()
        NavigationToolbar2Tk(_cv, r, pack_toolbar=False)
        out('tkagg canvas: ok (drawn into Tk, toolbar built)')
        r.destroy()
    else:
        out('gui build   : skipped (no DISPLAY)')
    out('SELFCHECK OK')


def main():
    import multiprocessing
    _fix_std_streams()                                   # BEFORE freeze_support: pool workers never
    # return from it, and with sys.stderr = None (windowed exe) a dying worker's traceback print
    # raises 'NoneType has no write' -> PyInstaller error dialog per worker on a hard node stop
    multiprocessing.freeze_support()                     # portfolio build uses a process pool
    if os.environ.get('ALPHANODE_HANG_DUMP'):
        # CI forensics for a wedged frozen process: a windowed exe on Windows has no visible
        # stdout/stderr, so a hang is 900 silent seconds. Arm faulthandler to dump EVERY
        # thread's stack into a file after N seconds and hard-exit; the smoke prints the file.
        import faulthandler
        _hf = open(os.environ.get('ALPHANODE_HANG_DUMP_FILE') or 'hang_dump.txt', 'w')
        faulthandler.dump_traceback_later(int(os.environ['ALPHANODE_HANG_DUMP']),
                                          exit=True, file=_hf)
    _prep_path()
    argv = sys.argv
    role = os.environ.get('ALPHANODE_ROLE')
    if len(argv) >= 3 and argv[1] == '--role':
        role = argv[2]
        del argv[1:3]                                    # leave clean argv for the child code

    if role == 'node':
        import node
        node.main()
    elif role == 'fetch':
        import fetch_data
        fetch_data.main()                                # it calls os._exit() itself
    elif role == 'portfolio':
        import portfolio_build
        portfolio_build.main()                           # combines top-N by TEST via the real engine
    elif role == 'signal':
        import signal_service
        signal_service.main()                            # local live-signal HTTP API (JSON, localhost)
    elif role == 'metrics':
        import metrics_worker
        metrics_worker.main()                            # leaderboard trade stats (JSON on stdin/stdout)
    elif role == 'pdfreport':
        import pdf_worker
        pdf_worker.main()                                # analytics PDF dashboard (own process: no Tk/FreeType clash)
    elif role == 'rescore':
        import rescore_library
        rescore_library.main()                           # re-score library.jsonl with current metrics
    elif role == 'forward':
        import forward_track
        sys.exit(forward_track.main())                   # append-only paper steps of enrolled strategies
    elif role == 'cli':
        import cli
        cli.main(argv[1:])                               # remaining argv -> CLI subcommands
    elif role == 'selfcheck':
        _selfcheck()
    else:
        import alphanode_gui
        alphanode_gui.main()


if __name__ == '__main__':
    main()

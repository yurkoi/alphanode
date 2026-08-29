"""AlphaNode CLI — control the strategy-search node without a GUI (for Docker/server/ssh).

    python alphanode/cli.py run [flags]        # start continuous search (foreground, log to stdout)
    python alphanode/cli.py fetch [flags]      # download fresh Binance data
    python alphanode/cli.py top [flags]        # top alphas found in the library (table in the terminal)
    python alphanode/cli.py top --stats        # + trade stats on TEST (maxDD/CAGR/sortino/T↑T↓T~/win)
    python alphanode/cli.py status             # current node state (rounds, best)
    python alphanode/cli.py forward list       # forward track: enrolled strategies + their paper equity
    python alphanode/cli.py forward step       # advance the forward track now (run also does it itself)
    python alphanode/cli.py portfolio [flags]  # build a combined portfolio from top-N alphas by TEST
    python alphanode/cli.py signal [flags]     # serve live target positions over a local HTTP API (JSON)

Everything configurable in the GUI is here as flags too; an unset flag = taken from ALPHANODE_*/config.ini.
State (library, status) is read from ALPHANODE_STATE_DIR (in Docker — /data).
"""
import os
import sys
import json
import pickle
import difflib
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import apppaths                                          # noqa: E402
# resource root (dev — the repo, frozen — the bundle): where fetch_data.py and quantpylib/ live
for _p in (apppaths.RES_ROOT, apppaths.engine_dir()):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)


def _state_dir():
    return os.environ.get('ALPHANODE_STATE_DIR') or apppaths.state_dir()


def _data_path():
    return os.environ.get('ALPHANODE_DATA') or apppaths.data_path()


def _testsh(c):
    t = c.get('test') if isinstance(c.get('test'), dict) else {}
    return t.get('sharpe')


# ---- run: flag -> ALPHANODE_* variable (empty flag left untouched) --------------------------
_ENVMAP = [
    ('cpu', 'CPU_PERCENT'), ('universe', 'UNIVERSE'), ('pop', 'POP'), ('gens', 'GENS'),
    ('seed', 'SEED'), ('pause', 'PAUSE'), ('port', 'STATUS_PORT'), ('state_dir', 'STATE_DIR'),
    ('max_rounds', 'MAX_ROUNDS'), ('leaderboard', 'LEADERBOARD'), ('explore_every', 'EXPLORE_EVERY'),
    ('seed_from_library', 'SEED_FROM_LIBRARY'), ('target_vol', 'TARGET_VOL'), ('exec_cost', 'EXEC_COST'),
    ('max_depth', 'MAX_DEPTH'), ('max_size', 'MAX_SIZE'), ('tournament', 'TOURNAMENT'),
    ('elitism', 'ELITISM'), ('random_inject', 'RANDOM_INJECT'), ('crossover_prob', 'CROSSOVER_PROB'),
    ('parsimony', 'PARSIMONY'), ('corr_threshold', 'CORR_THRESHOLD'), ('corr_penalty', 'CORR_PENALTY'),
    ('hof_capacity', 'HOF_CAPACITY'), ('train_start', 'TRAIN_START'), ('val_start', 'VAL_START'),
    ('test_start', 'TEST_START'), ('test_end', 'TEST_END'), ('data', 'DATA'), ('config_ini', 'CONFIG_INI'),
]


def cmd_run(args):
    for flag, envk in _ENVMAP:
        v = getattr(args, flag, None)
        if v is not None and v != '':
            os.environ['ALPHANODE_' + envk] = str(v)
    os.environ.setdefault('ALPHANODE_DATA', _data_path())     # shared snapshot with status/export
    import node                                                # env is read at import
    node.main()


def cmd_fetch(args):
    out = args.out or _data_path()
    argv = ['fetch', '--top', str(args.top), '--min-years', str(args.min_years), '--out', out]
    if args.start:
        argv += ['--start', args.start]
    if args.end:
        argv += ['--end', args.end]
    if args.quote:
        argv += ['--quote', args.quote]
    argv += ['--concurrency', str(args.concurrency), '--timeout', str(args.timeout)]
    if args.source:
        argv += ['--source', args.source]
    import fetch_data
    sys.argv = argv
    fetch_data.main()                                         # it calls os._exit() itself


# ---- top: rank the library (like the GUI leaderboard) --------------------------------------
def _load_library(state_dir):
    # per-timeframe library names, same rule as node.py: 1d keeps the legacy 'library.jsonl'
    tf = (os.environ.get('ALPHANODE_TF', '') or '1d').strip().lower()
    path = os.path.join(state_dir, 'library.jsonl' if tf == '1d' else f'library_{tf}.jsonl')
    rows = []
    try:
        for line in open(path, encoding='utf-8'):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return rows, path


def _rank(rows, sort, min_test, n, diverse):
    keyf = _testsh if sort == 'test' else (lambda c: c.get('base'))
    rows = [c for c in rows if keyf(c) is not None]
    if min_test is not None:
        rows = [c for c in rows if _testsh(c) is not None and _testsh(c) > min_test]
    rows.sort(key=keyf, reverse=True)
    if not diverse:
        return rows[:n]
    kept = []                                                 # one alpha per family (<0.80 similarity)
    for c in rows[:600]:
        f = c.get('formula', '')
        if all(difflib.SequenceMatcher(None, f, k.get('formula', '')).ratio() < 0.80 for k in kept):
            kept.append(c)
        if len(kept) >= n:
            break
    return kept


def _fmt(v):
    return f'{v:+.2f}' if isinstance(v, (int, float)) else '—'


def _trade_stats(formulas):
    """The GUI leaderboard's TEST columns, in-process: one shared panel, fast_sim per alpha.
    Reuses metrics_worker (the exact code the GUI runs out of process), so the numbers match
    the desktop app cell for cell — including T↑/T↓/T~, the Sharpe on the trending-up /
    trending-down / flat bars of the market (direction regime, labels lagged one bar)."""
    import metrics_worker
    from config import load_config
    cfg = load_config()
    sp = cfg['splits']

    def ev(name, fallback):
        return os.environ.get('ALPHANODE_' + name) or fallback
    # the same ALPHANODE_* overrides the node applies on top of config.ini (node._apply_overrides
    # is what reads them for `run`) — otherwise the stats simulate with different vol/fees/dates
    # than the library was mined with and the columns stop matching the GUI
    opt = {'instruments': cfg.get('instruments'),
           'vol': float(ev('TARGET_VOL', cfg['vol'])),
           'exec': float(ev('EXEC_COST', cfg['exec'])),
           'train_start': ev('TRAIN_START', sp['train'][0].strftime('%Y-%m-%d')),
           'test_start': ev('TEST_START', sp['test'][0].strftime('%Y-%m-%d')),
           'test_end': ev('TEST_END', sp['test'][1].strftime('%Y-%m-%d'))}
    ctx = metrics_worker.build_ctx(opt)
    return {f: metrics_worker.trade_stats(f, ctx) for f in formulas}


def cmd_top(args):
    rows, path = _load_library(_state_dir())
    if not rows:
        print(f'library empty or not found: {path}')
        return
    picked = _rank(rows, args.sort, args.min_test, args.n, not args.all)
    try:
        width = int(os.environ.get('COLUMNS') or os.get_terminal_size().columns)
    except (OSError, ValueError):
        width = 120
    order = 'TEST OOS' if args.sort == 'test' else 'fitness min(train,val)'
    note = '' if args.sort != 'test' else '   ⚠ cherry-pick on held-out (number is inflated)'
    print(f'Top-{len(picked)} by {order}{note}   ·   {path}')
    stats = None
    if getattr(args, 'stats', False):
        print('computing TEST trade stats (builds the panel + simulates every row)…')
        stats = _trade_stats([c.get('formula', '') for c in picked])
        fcol = max(24, width - 102)
        print(f'{"#":>3}  {"fitness":>7}  {"TEST":>6}  {"maxDD":>6}  {"CAGR":>6}  {"srtno":>6}  '
              f'{"T↑":>6}  {"T↓":>6}  {"T~":>6}  {"L/S/yr·a":>9}  {"win%":>4}  '
              f'{"win↑":>4}  {"win↓":>4}  formula')
    else:
        fcol = max(30, width - 26)
        print(f'{"#":>3}  {"fitness":>7}  {"TEST":>6}  formula')
    print('─' * min(width, 140 if stats else 100))

    def fitcell(c):
        b = c.get('base')
        if not isinstance(b, (int, float)):
            return '—'
        return f'{b * 100:.0f}%' if c.get('fit_metric') == 'winrate' else _fmt(b)

    for i, c in enumerate(picked, 1):
        f = c.get('formula', '')
        if len(f) > fcol:
            f = f[:fcol - 1] + '…'
        if stats is None:
            print(f'{i:>3}  {fitcell(c):>7}  {_fmt(_testsh(c)):>6}  {f}')
            continue
        m = stats.get(c.get('formula', ''))
        m = m if isinstance(m, dict) else {}

        def pct(v):
            return f'{v * 100:+.0f}%' if isinstance(v, (int, float)) else '—'
        if isinstance(m.get('long_yr'), (int, float)) and isinstance(m.get('short_yr'), (int, float)):
            ls = f"{m['long_yr']:.1f}/{m['short_yr']:.1f}"         # entries / asset / year
        else:
            ls = f"{m['long']}/{m['short']}" if 'long' in m else '—'
        win = f"{m['win'] * 100:.0f}" if 'win' in m else '—'

        def wpc(v):
            return f'{v * 100:.0f}' if isinstance(v, (int, float)) else '—'
        print(f'{i:>3}  {fitcell(c):>7}  {_fmt(_testsh(c)):>6}  {pct(m.get("dd")):>6}  '
              f'{pct(m.get("cagr")):>6}  {_fmt(m.get("sortino")):>6}  {_fmt(m.get("tup")):>6}  '
              f'{_fmt(m.get("tdown")):>6}  {_fmt(m.get("tflat")):>6}  {ls:>9}  {win:>4}  '
              f'{wpc(m.get("wup")):>4}  {wpc(m.get("wdown")):>4}  {f}')
    if stats is not None:
        print('\nT↑/T↓/T~ = TEST Sharpe on trending-up / trending-down / flat market bars '
              '(direction regime: drift t-stat over ~30 calendar days, labels lagged one '
              'bar); win↑/win↓ = accuracy of the formula’s own long / short calls — each held '
              '(bar, asset) cell judged by that asset’s next-bar move; L/S/yr·a = positions '
              'opened per asset per year, by side. Analysis, not selection: picking by '
              'these is another layer of TEST peeking.')


def cmd_forward(args):
    """Forward track without the GUI. `list` shows the enrolled strategies and their paper
    equity; `step` advances every due entry right now (a running `run` node already does this
    itself every 5 minutes — see node.forward_loop)."""
    import forward_track as ft
    if args.action == 'step':
        return ft.step_all(force=args.force)
    track = ft.load_track()
    if not track['entries']:
        print('forward track is empty — enroll a champion or a portfolio (GUI: double-click '
              'an alpha → "Forward track ➕"); a headless node then steps it automatically')
        return
    for e in track['entries']:
        m = ft.metrics(e)
        sh = f'{m["sharpe"]:+.2f}' if m['sharpe'] is not None else '—'
        print(f'{"[arch] " if e.get("archived") else ""}{e["id"]}: {m["days"]} steps · '
              f'${m["equity"]:,.0f} ({m["ret"] * 100:+.1f}%) · Sharpe {sh} · since {e["enrolled"]}')


def cmd_status(args):
    sf = os.path.join(_state_dir(), 'status.json')
    try:
        st = json.load(open(sf, encoding='utf-8'))
    except OSError:
        print(f'status not found ({sf}) — node not started yet?')
        return
    print(f'state     : {st.get("state", "—")}')
    print(f'universe  : {st.get("universe", "—")}   vol {st.get("target_vol", "—")}')
    print(f'resources : {st.get("cpu_percent", "?")}%  ·  {st.get("n_jobs", "?")}/{st.get("cores", "?")} cores')
    print(f'rounds    : {st.get("rounds", 0)}   ·   formulas tried: {st.get("trials_total", 0):,}')
    print(f'found     : {st.get("found", 0)}   ·   best fitness {_fmt(st.get("best_base"))}  '
          f'TEST(OOS) {_fmt(st.get("best_test"))}')
    if st.get('current'):
        print(f'now       : {st["current"]}')
    best = st.get('best', [])[:args.n]
    if best:
        print(f'\ntop-{len(best)} (by fitness):')
        for i, c in enumerate(best, 1):
            f = c.get('formula', '')
            print(f'  {i:>2}  fit {_fmt(c.get("base")):>6}  TEST {_fmt(_testsh(c)):>6}  {f[:70]}')


def cmd_portfolio(args):
    os.environ.setdefault('ALPHANODE_STATE_DIR', _state_dir())
    os.environ.setdefault('ALPHANODE_DATA', _data_path())
    import portfolio_build
    out = args.out or os.path.join(_state_dir(), 'portfolio.json')
    argv = ['portfolio', '--top', str(args.top), '--sim-start', args.sim_start, '--out', out]
    if args.jobs is not None:
        argv += ['--jobs', str(args.jobs)]
    sys.argv = argv
    portfolio_build.main()                                    # writes portfolio.json; exits itself


def cmd_signal(args):
    os.environ.setdefault('ALPHANODE_STATE_DIR', _state_dir())
    os.environ.setdefault('ALPHANODE_DATA', _data_path())
    name = args.name
    if args.formula:
        formulas, name = [args.formula], name or 'alpha'
    elif args.portfolio:
        try:
            doc = json.load(open(os.path.join(_state_dir(), 'portfolio.json'), encoding='utf-8'))
        except OSError:
            print('portfolio.json not found — run "portfolio" first')
            return
        formulas = doc.get('formulas_full') or []
        name = name or f'portfolio_top{doc.get("n", len(formulas))}'
        if not formulas:
            print('no formulas in portfolio.json')
            return
    else:                                                     # Nth best from the library by --sort
        rows, _ = _load_library(_state_dir())
        picked = _rank(rows, args.sort, None, args.rank, diverse=False)
        if len(picked) < args.rank:
            print(f'library has fewer than {args.rank} alphas (total {len(picked)})')
            return
        formulas, name = [picked[args.rank - 1].get('formula')], name or f'alpha_rank{args.rank}'
    os.environ['ALPHANODE_SIGNAL_FORMULAS'] = json.dumps(formulas)
    os.environ['ALPHANODE_SIGNAL_NAME'] = name
    for flag, envk in (('port', 'PORT'), ('refresh', 'REFRESH'), ('host', 'HOST'),
                       ('universe', 'TICKERS'), ('start', 'START')):
        v = getattr(args, flag, None)
        if v not in (None, ''):
            os.environ['ALPHANODE_SIGNAL_' + envk] = str(v)
    import signal_service
    signal_service.main()                                     # serves until stopped (Ctrl-C / SIGTERM)


def build_parser():
    p = argparse.ArgumentParser(prog='alphanode', description='AlphaNode CLI (headless alpha-search node)')
    sub = p.add_subparsers(dest='cmd', required=True)

    r = sub.add_parser('run', help='start continuous search (foreground)')
    r.add_argument('--cpu', type=int, help='5..95 — share of CPU (workers = %% × cores)')
    r.add_argument('--universe', help='all or BTCUSDT,ETHUSDT,...')
    r.add_argument('--pop', type=int, help='population size per round')
    r.add_argument('--gens', type=int, help='generations per round')
    r.add_argument('--seed', type=int, help='base seed')
    r.add_argument('--pause', type=float, help='pause between rounds, sec')
    r.add_argument('--port', type=int, help='status page port (0/empty — no server)')
    r.add_argument('--state-dir', dest='state_dir', help='where to write library/status (in Docker /data)')
    r.add_argument('--max-rounds', dest='max_rounds', type=int, help='0 = infinite')
    r.add_argument('--leaderboard', type=int, help='how many best to keep in the top')
    r.add_argument('--explore-every', dest='explore_every', type=int, help='every Nth round — from scratch')
    r.add_argument('--seed-from-library', dest='seed_from_library', choices=['0', '1'],
                   help='1 = warm-start from own library')
    r.add_argument('--target-vol', dest='target_vol', type=float)
    r.add_argument('--exec-cost', dest='exec_cost', type=float)
    r.add_argument('--max-depth', dest='max_depth', type=int)
    r.add_argument('--max-size', dest='max_size', type=int)
    r.add_argument('--tournament', type=int)
    r.add_argument('--elitism', type=int)
    r.add_argument('--random-inject', dest='random_inject', type=int)
    r.add_argument('--crossover-prob', dest='crossover_prob', type=float)
    r.add_argument('--parsimony', type=float)
    r.add_argument('--corr-threshold', dest='corr_threshold', type=float)
    r.add_argument('--corr-penalty', dest='corr_penalty', type=float)
    r.add_argument('--hof-capacity', dest='hof_capacity', type=int)
    r.add_argument('--train-start', dest='train_start')
    r.add_argument('--val-start', dest='val_start')
    r.add_argument('--test-start', dest='test_start')
    r.add_argument('--test-end', dest='test_end')
    r.add_argument('--data', help='path to data.pickle')
    r.add_argument('--config-ini', dest='config_ini', help='path to config.ini')
    r.set_defaults(func=cmd_run)

    f = sub.add_parser('fetch', help='download fresh Binance data (top-N USDT perps)')
    f.add_argument('--top', type=int, default=150)
    f.add_argument('--min-years', dest='min_years', type=float, default=3.0)
    f.add_argument('--start', default=None)
    f.add_argument('--end', default=None)
    f.add_argument('--out', default=None, help='default — the active data.pickle')
    f.add_argument('--quote', default='USDT')
    f.add_argument('--concurrency', type=int, default=6)
    f.add_argument('--timeout', type=float, default=120)
    f.add_argument('--source', default=None, choices=('auto', 'api', 'vision'),
                   help='auto (default) probes fapi and falls back to the data.binance.vision '
                        'archive where fapi is geo-blocked (same bars, ~10-30h behind live)')
    f.set_defaults(func=cmd_fetch)

    t = sub.add_parser('top', help='top alphas found in the library')
    t.add_argument('--sort', choices=['fitness', 'test'], default='fitness',
                   help='rank by fitness min(train,val) or by TEST OOS (cherry-pick!)')
    t.add_argument('--min-test', dest='min_test', type=float, default=None,
                   help='show only alphas with TEST OOS > X')
    t.add_argument('-n', type=int, default=20, help='how many rows')
    t.add_argument('--all', action='store_true', help='no family dedup (raw top)')
    t.add_argument('--stats', action='store_true',
                   help='add TEST trade stats per row (maxDD, CAGR, sortino, T↑/T↓/T~ '
                        'direction-regime Sharpe, trades L/S, win%%) — simulates every row, '
                        'takes a few seconds')
    t.set_defaults(func=cmd_top)

    s = sub.add_parser('status', help='current node state')
    s.add_argument('-n', type=int, default=5, help='how many best to show')
    s.set_defaults(func=cmd_status)

    fw = sub.add_parser('forward', help='forward track: honest append-only paper test '
                                        '(a running node steps it automatically)')
    fw.add_argument('action', choices=('list', 'step'),
                    help='list — enrolled strategies + paper equity; step — advance due entries now')
    fw.add_argument('--force', action='store_true', help='re-step even if the bar was processed')
    fw.set_defaults(func=cmd_forward)

    pf = sub.add_parser('portfolio', help='build a combined portfolio from top-N alphas by TEST')
    pf.add_argument('--top', type=int, default=6)
    pf.add_argument('--sim-start', dest='sim_start', default='2022-06-01', help='warm-up start before TEST')
    pf.add_argument('--jobs', type=int, default=None, help='parallel workers (default auto)')
    pf.add_argument('--out', default=None, help='default — <state_dir>/portfolio.json')
    pf.set_defaults(func=cmd_portfolio)

    sg = sub.add_parser('signal', help='serve live target positions over a local HTTP API (JSON)')
    gg = sg.add_mutually_exclusive_group()
    gg.add_argument('--formula', help='serve this exact formula')
    gg.add_argument('--rank', type=int, default=1, help='serve the Nth best alpha by --sort (default 1)')
    sg.add_argument('--portfolio', action='store_true', help='serve the built portfolio (portfolio.json)')
    sg.add_argument('--sort', choices=['fitness', 'test'], default='fitness')
    sg.add_argument('--name', default=None, help='label for the served strategy')
    sg.add_argument('--port', type=int, default=None, help='default 8799')
    sg.add_argument('--refresh', type=int, default=None, help='recompute period, sec (default 900)')
    sg.add_argument('--host', default=None, help='bind host (Docker: 0.0.0.0)')
    sg.add_argument('--start', default=None, help='ISO date to warm the engine from')
    sg.add_argument('--universe', help='ticker list; default — all from data.pickle')
    sg.set_defaults(func=cmd_signal)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()

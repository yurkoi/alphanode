"""Re-score the whole alphanode library with the CURRENT metric convention.

Needed after the 2026-07 metrics fix: _metrics used to drop zero-return days before
annualizing, inflating Sharpe by ~1/sqrt(active_fraction) and CAGR far more — every stored
train/val/test/base in library.jsonl carries that bias, so old rows are not comparable with
newly mined ones. This tool recomputes every formula through the same evaluate() path the
search uses (honest calendar metrics), rewrites library.jsonl atomically, and drops rows that
are degenerate under the honest rules (never really traded). A one-time backup of the original
file is kept as library.jsonl.bak (never overwritten by later runs).

    python alphanode/rescore_library.py            # state dir from ALPHANODE_STATE_DIR (or alphanode/state)
    <exe> --role rescore                           # frozen build
"""
import os
import sys
import json
import time
import multiprocessing as mp

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
for _p in (os.path.join(PROJ, 'evolution'), PROJ, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import warnings                                          # noqa: E402
import numpy as np                                       # noqa: E402
warnings.filterwarnings('ignore'); np.seterr(all='ignore')


def _state_dir():
    d = os.environ.get('ALPHANODE_STATE_DIR')
    if d:
        return d
    import apppaths                                # not HERE/state: frozen, that's the read-only bundle
    return apppaths.state_dir()


_G = {}


def _winit():
    # A raising initializer makes mp.Pool respawn the worker forever (crash-dialog loop in the
    # windowed build) — record the error and fail fast on the first task instead.
    try:
        from config import load_config
        from evaluator import build_panel, make_market
        cfg = load_config()                              # timeframe fields honor ALPHANODE_TF
        uni = os.environ.get('ALPHANODE_UNIVERSE', 'all')
        if uni.lower() not in ('all', '*', ''):
            cfg['instruments'] = [x.strip().upper() for x in uni.split(',') if x.strip()]
        tk, raw, panel = build_panel(cfg['data'], cfg['start'], cfg['end'], cfg.get('instruments'),
                                     freq=cfg.get('freq', 'D'))
        _G.update(cfg=cfg, tk=tk, panel=panel,
                  market=make_market(panel, tk, raw, vol_window=cfg.get('vol_window', 30)))
    except Exception as e:                               # noqa: BLE001
        _G['init_error'] = e
        return
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass


def _rescore_one(row):
    from genome import parse
    from evaluator import evaluate
    if 'init_error' in _G:
        raise RuntimeError(f'worker init failed: {type(_G["init_error"]).__name__}: '
                           f'{_G["init_error"]}')
    cfg = _G['cfg']
    try:
        met = (row.get('fit_metric') or 'sharpe').strip().lower()
        res = evaluate(parse(row['formula']), _G['tk'], _G['panel'], _G['market'],
                       cfg['splits'], cfg['vol'], cfg['exec'],
                       ann=cfg.get('ann', 365.0), ewma_lambda=cfg.get('ewma_lambda', 0.06),
                       fit={'metric': met})
    except Exception:                                    # noqa: BLE001
        res = None
    if res is None:                                      # degenerate under honest rules
        return None
    rm = lambda m: ({k: round(float(v), 4) for k, v in m.items()} if m else None)  # noqa: E731
    out = dict(row)
    out['train'], out['val'], out['test'] = rm(res['train']), rm(res['val']), rm(res['test'])
    out['base'] = round(res['base_fit'], 3)          # in the row's OWN objective — a winrate
    #                                                  row must not wake up rescored in Sharpe
    #                                                  units under a still-'winrate' tag
    return out


def main():
    tf = (os.environ.get('ALPHANODE_TF') or '1d').strip().lower()
    suffix = '' if tf == '1d' else f'_{tf}'
    lib = os.path.join(_state_dir(), f'library{suffix}.jsonl')
    if not os.path.exists(lib):
        print(f'no library at {lib} — nothing to rescore')
        return
    rows = []
    for line in open(lib, encoding='utf-8'):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if not rows:
        print('library is empty — nothing to rescore')
        return

    bak = lib + '.bak'
    if not os.path.exists(bak):                          # one-time backup of the ORIGINAL scores
        with open(bak, 'w', encoding='utf-8') as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
        print(f'backup: {bak}')

    # sealed rows (vault: no plaintext formula) can't be re-evaluated — pass them through
    # verbatim rather than let the evaluate() KeyError drop them from the library entirely.
    sealed = [r for r in rows if not r.get('formula')]
    rows = [r for r in rows if r.get('formula')]
    if sealed:
        print(f'{len(sealed)} sealed rows kept as-is (no plaintext to rescore)')

    jobs = max(1, (os.cpu_count() or 4) // 2)
    print(f'rescoring {len(rows)} alphas with honest calendar metrics ({jobs} workers)…', flush=True)
    t0 = time.time()
    with mp.Pool(processes=jobs, initializer=_winit) as pool:
        scored = []
        for i, out in enumerate(pool.imap(_rescore_one, rows, chunksize=8), 1):
            scored.append(out)
            if i % 100 == 0 or i == len(rows):
                print(f'  {i}/{len(rows)}', flush=True)
    kept = [r for r in scored if r is not None] + sealed
    dropped = len(scored) + len(sealed) - len(kept)

    tmp = lib + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        for r in kept:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    os.replace(tmp, lib)

    kept.sort(key=lambda c: c.get('base') if c.get('base') is not None else -1e9, reverse=True)
    print(f'✓ rescored {len(kept)} kept, {dropped} degenerate dropped · {time.time()-t0:.0f}s')
    print('new top-5 by base:')
    for c in kept[:5]:
        te = (c.get('test') or {}).get('sharpe')
        f = c.get('formula') or f"locked · {c.get('id', '')}"   # sealed rows have no plaintext
        print(f"  base {c.get('base'):+.2f} · TEST {te if te is not None else '—'} · {f[:70]}")


if __name__ == '__main__':
    main()

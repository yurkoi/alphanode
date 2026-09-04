"""Per-champion NET return series, cached by the node so a SEALED library can still be curated.

Vault mode keeps every formula's plaintext inside the mining process: the portfolio builder
(a separate process reading library.jsonl) has nothing to simulate for a sealed row. What CAN
leave the miner without leaking the formula is the row's realised return series — the number
line a backtest chart is made of. It carries no forward signal (it ends at the last closed bar
and never says what to hold next), so it is useless for trading, yet it is exactly what a
combination search and an equal-weight mix need: enough for the portfolio PREVIEW an
unactivated node shows, and nothing that could be opened or served.

Layout: <state>/series[_<tf>]/<id>.npz with `t` (int64 epoch seconds, UTC) and `r` (float32
per-bar returns); the id is vault.formula_id — the md5 tail the sealed row already carries.
The node keeps the cache to the top-KEEP rows by fitness (the builder's pool is 30), so it
stays a few tens of MB even on 15m bars.
"""
import os

import numpy as np
import pandas as pd

KEEP = 100


def cache_dir(state_dir, tf='1d'):
    return os.path.join(state_dir, 'series' if tf == '1d' else f'series_{tf}')


def path(state_dir, tf, fid):
    return os.path.join(cache_dir(state_dir, tf), f'{fid}.npz')


def has(state_dir, tf, fid):
    return bool(fid) and os.path.isfile(path(state_dir, tf, fid))


def save(p, ret):
    """ret: a pd.Series of per-bar returns on a DatetimeIndex (UTC, or naive read as UTC).
    Atomic — tmp + replace — so a builder reading mid-write never sees a torn file."""
    idx = pd.DatetimeIndex(ret.index)
    idx = idx.tz_localize('UTC') if idx.tz is None else idx.tz_convert('UTC')
    t = (idx.asi8 // 1_000_000_000).astype(np.int64)
    r = np.nan_to_num(ret.to_numpy(dtype=np.float64), nan=0.0).astype(np.float32)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'wb') as f:                          # a file object: savez would append .npz
        np.savez_compressed(f, t=t, r=r)
    os.replace(tmp, p)


def load(p):
    with np.load(p) as z:
        t, r = z['t'], z['r']
    idx = pd.to_datetime(t.astype(np.int64), unit='s', utc=True)
    return pd.Series(r.astype(np.float64), index=idx)


def prune(state_dir, tf, keep_ids):
    """Remove every cached series whose id is not in keep_ids; returns how many went."""
    keep = set(keep_ids)
    try:
        names = os.listdir(cache_dir(state_dir, tf))
    except OSError:
        return 0
    n = 0
    for name in names:
        if not name.endswith('.npz') or name[:-4] in keep:
            continue
        try:
            os.remove(os.path.join(cache_dir(state_dir, tf), name))
            n += 1
        except OSError:
            pass
    return n

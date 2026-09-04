"""Load search settings from config.ini (stdlib configparser, no external dependencies).

Returns a single cfg dict understood by evolve()/evaluate(). The same config is read by both
run_evo.py (search) and validate_champions.py (validation) — so that vol/fees/segments do not
diverge between stages.
"""
import os
import configparser
from datetime import datetime

import pandas as pd

try:
    from timeframe import resolve as _resolve_tf     # bar size -> annualization / grid / vol params
except Exception:                                    # pragma: no cover  (present in the shipped tree)
    _resolve_tf = None

# The basket is capped here, at the engine's own read of the setting, and not only in the GUI:
# ALPHANODE_UNIVERSE and config.ini are both editable by hand, so a cap that lived in the panel
# alone would be advice, not a limit. alphanode_gui keeps its own copy of the number rather than
# importing this module — config pulls in pandas, and the GUI must not pay that at startup;
# tests/test_pair_cap.py fails if the two ever drift apart.
MAX_PAIRS = 20

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
# Paths can be overridden externally (the built application points to the bundle/user folder).
# Empty/unset — as before, next to the sources.
DEFAULT_INI = os.environ.get('ALPHANODE_CONFIG_INI') or os.path.join(HERE, 'config.ini')
DATA = os.environ.get('ALPHANODE_DATA') or os.path.join(PROJ, 'data.pickle')


def _ts(s):
    return pd.Timestamp(s.strip(), tz='UTC')


def load_config(path=None):
    path = path or DEFAULT_INI
    if not os.path.exists(path):
        raise FileNotFoundError(f'config not found: {path}')
    cp = configparser.ConfigParser(inline_comment_prefixes=('#', ';'))
    cp.read(path, encoding='utf-8')

    seg = dict(cp['segments'])
    jobs_raw = cp.get('search', 'jobs', fallback='auto').strip()
    jobs = max(1, (os.cpu_count() or 4) - 2) if jobs_raw.lower() == 'auto' else int(jobs_raw)

    # ALPHANODE_UNIVERSE (the GUI/node setting) wins over the ini — every consumer of load_config
    # (portfolio build, workers, validation) must simulate the SAME basket the search optimizes.
    uni_raw = (os.environ.get('ALPHANODE_UNIVERSE') or
               cp.get('universe', 'instruments', fallback='all')).strip()
    if uni_raw.lower() in ('', 'all', '*'):
        instruments = None                                  # None -> all from data.pickle
    else:                                                   # dedup, upper, order kept, then capped
        instruments = list(dict.fromkeys(
            x.strip().upper() for x in uni_raw.replace('\n', ',').split(',') if x.strip()
        ))[:MAX_PAIRS]

    tf_name = os.environ.get('ALPHANODE_TF') or cp.get('timeframe', 'tf', fallback='1d')
    if _resolve_tf is not None:
        _tf = _resolve_tf(tf_name)
        tf_fields = {'tf': _tf.name, 'ann': _tf.periods_per_year, 'freq': _tf.pandas_freq,
                     'vol_window': _tf.vol_window, 'ewma_lambda': _tf.ewma_lambda,
                     'binance_interval': _tf.binance_interval}
    else:                                            # daily fallback (identical to the original engine)
        tf_fields = {'tf': '1d', 'ann': 365.0, 'freq': 'D', 'vol_window': 30,
                     'ewma_lambda': 0.06, 'binance_interval': '1d'}
    if _resolve_tf is not None:                      # 'auto'/'today' box sentinels -> real dates
        from timeframe import seg_value
        for _f in ('train_start', 'val_start', 'test_start', 'test_end'):
            seg[_f] = seg_value(tf_fields['tf'], _f, seg.get(_f))

    # Per-timeframe data snapshot. An explicit ALPHANODE_DATA always wins (the GUI/workers pass
    # the right file); otherwise 1d keeps the historical data.pickle and intraday gets its own
    # data_<tf>.pickle — so switching timeframes never clobbers another timeframe's history.
    if os.environ.get('ALPHANODE_DATA'):
        data = os.environ['ALPHANODE_DATA']          # read at CALL time — the module-level DATA
        #                                              froze the env as it was at import, so a
        #                                              process that re-points ALPHANODE_DATA later
        #                                              silently kept loading the old snapshot
    else:
        suffix = '' if tf_fields['tf'] == '1d' else f'_{tf_fields["tf"]}'
        data = os.path.join(PROJ, f'data{suffix}.pickle')

    return {
        'data': data,
        'instruments': instruments,
        **tf_fields,
        'start': datetime.fromisoformat(seg['train_start'].strip()),
        'end': datetime.fromisoformat(seg['test_end'].strip()),
        'splits': {
            'train': (_ts(seg['train_start']), _ts(seg['val_start'])),
            'val':   (_ts(seg['val_start']),   _ts(seg['test_start'])),
            'test':  (_ts(seg['test_start']),  _ts(seg['test_end'])),
        },
        'vol': cp.getfloat('simulation', 'target_vol'),
        'exec': cp.getfloat('simulation', 'exec_cost'),
        'pop': cp.getint('search', 'population'),
        'gens': cp.getint('search', 'generations'),
        'seed': cp.getint('search', 'seed'),
        'n_jobs': jobs,
        'max_depth': cp.getint('genome', 'max_depth'),
        'max_size': cp.getint('genome', 'max_size'),
        'tourn': cp.getint('ga', 'tournament'),
        'elitism': cp.getint('ga', 'elitism'),
        'random_inject': cp.getint('ga', 'random_inject'),
        'cx_prob': cp.getfloat('ga', 'crossover_prob'),
        'parsimony': cp.getfloat('fitness', 'parsimony'),
        'corr_thresh': cp.getfloat('fitness', 'corr_threshold'),
        'corr_penalty': cp.getfloat('fitness', 'corr_penalty'),
        'hof_cap': cp.getint('fitness', 'hof_capacity'),
        # robust multi-block fitness (0 blocks = legacy min(TRAIN,VAL); see config.ini)
        'fit_blocks': cp.getint('fitness', 'blocks', fallback=0),
        'fit_quantile': cp.getfloat('fitness', 'block_quantile', fallback=0.25),
        'fit_se_penalty': cp.getfloat('fitness', 'se_penalty', fallback=1.0),
        'fit_conc_penalty': cp.getfloat('fitness', 'conc_penalty', fallback=0.3),
        'fit_min_eff_n': cp.getfloat('fitness', 'min_eff_n', fallback=3.0),
        # directional-tilt guard: one-sided books are the market in disguise
        'fit_net_penalty': cp.getfloat('fitness', 'net_penalty', fallback=0.5),
        'fit_max_net': cp.getfloat('fitness', 'max_net', fallback=0.5),
        # what base_fit measures: 'sharpe' (default) or 'winrate' — per-bar share of
        # winning active bars, min(TRAIN, VAL), same number the leaderboard's win% shows
        'fit_metric': (cp.get('fitness', 'metric', fallback='sharpe') or 'sharpe').strip().lower(),
        # final coordinate-descent tuning of champions' windows (continuous, off-grid)
        'window_polish': cp.getboolean('search', 'window_polish', fallback=True),
    }

"""The in-process charts (Equity window, passport, signals CSV) must cut the segments where the
GUI's date boxes say — not at config.ini's daily defaults. Regression: on 1d the box dates were
never applied (a NameError swallowed by a bare except), so a TEST pinned at 2025-09 drew as 2023."""
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'alphanode'))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'evolution'))
import alphanode_gui as G                                # noqa: E402


class _App:
    _build_plot_cfg = G.App._build_plot_cfg

    def __init__(self, tf, **cfg):
        self._tf_name = tf
        self.cfg = dict(universe_list='BTCUSDT,ETHUSDT', target_vol=0.25, exec_cost=0.001,
                        train_start='auto', val_start='auto', test_start='auto',
                        test_end='today')
        self.cfg.update(cfg)

    def _tf(self):
        return self._tf_name

    def _data_file(self):
        return '/nonexistent/data.pickle'


def test_pinned_boxes_win_over_the_ini_on_the_daily_timeframe():
    app = _App('1d', test_start='2025-09-14', val_start='2023-05-23')
    cfg = app._build_plot_cfg()
    assert cfg['splits']['test'][0] == pd.Timestamp('2025-09-14', tz='UTC')
    assert cfg['splits']['val'][0] == pd.Timestamp('2023-05-23', tz='UTC')
    assert cfg['splits']['val'][1] == cfg['splits']['test'][0]
    assert cfg['start'] == cfg['splits']['train'][0].tz_localize(None).to_pydatetime()
    assert cfg['end'] == cfg['splits']['test'][1].tz_localize(None).to_pydatetime()


def test_auto_boxes_resolve_for_the_selected_timeframe():
    """'auto'/'today' sentinels follow today for the GUI's bar size, not the ini's."""
    from timeframe import seg_value
    for tf in ('1d', '1h'):
        cfg = _App(tf)._build_plot_cfg()
        assert cfg['tf'] == tf
        for name, (lo, _hi) in cfg['splits'].items():
            assert lo == pd.Timestamp(seg_value(tf, f'{name}_start', 'auto'), tz='UTC'), (tf, name)
        assert cfg['splits']['test'][1] == pd.Timestamp(seg_value(tf, 'test_end', 'today'), tz='UTC')

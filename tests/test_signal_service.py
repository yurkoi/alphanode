"""Signal-service data layer, fully offline: snapshot seeding + incremental tail fetch.

Production failure this file guards against: the first intraday signal took ~17 MINUTES
because every cold start re-paged years of klines, serially, on every refresh. The fix
(_snapshot_dfs seeding + cache-driven tail refetch in fetch_live_dfs) must only change
WHERE bars come from — never WHAT the math sees. The specific regressions pinned here:
  * a snapshot's 'funding' column leaking into live dataframes (live fetches never had it,
    so the served math would silently diverge from the pre-fix service);
  * a partial/corrupt last snapshot bar surviving into the served signal instead of being
    replaced by the tail refetch of closed candles;
  * a "refresh" quietly falling back to a cold full-history page (the 17-minute bug);
  * one failing ticker taking down the whole fetch instead of just sitting out the cycle.
Everything runs against a deterministic synthetic kline source — fetch_klines and _now_ms
are monkeypatched, so no test can reach Binance or any URL.
"""
import os
import pickle
import threading
import zlib
from collections import defaultdict

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

import signal_service as ss

# ---------------- deterministic synthetic market ----------------
INTERVAL = '1h'
HOUR_MS = 3_600_000
N_BARS = 1100                                    # > vol_window(720) of the 1h timeframe
START_ISO = '2025-01-01'
GRID_START = pd.Timestamp('2025-01-01 00:00', tz='UTC')
GRID_START_MS = int(GRID_START.timestamp() * 1000)
FAKE_NOW_MS = GRID_START_MS + N_BARS * HOUR_MS   # exactly at the close of the last bar
TICKERS = ['AAAUSDT', 'BBBUSDT', 'CCCUSDT']

_BARS_CACHE = {}


def _source_bars(symbol):
    """Canonical OHLCV history for `symbol`: a seeded RNG walk on a fixed hourly grid.
    The single source of truth — snapshots, fetches and expectations all derive from it."""
    if symbol in _BARS_CACHE:
        return _BARS_CACHE[symbol]
    rng = np.random.default_rng(zlib.crc32(symbol.encode()) & 0xFFFFFFFF)
    idx = pd.date_range(GRID_START, periods=N_BARS, freq='h', tz='UTC')
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, N_BARS)))
    open_ = np.concatenate([[100.0], close[:-1]])
    spread = np.abs(rng.normal(0.0, 0.003, N_BARS))
    df = pd.DataFrame({'open': open_,
                       'high': np.maximum(open_, close) * (1 + spread),
                       'low': np.minimum(open_, close) * (1 - spread),
                       'close': close,
                       'volume': np.exp(rng.normal(10.0, 0.5, N_BARS))}, index=idx)
    df.index.name = 'datetime'
    _BARS_CACHE[symbol] = df
    return df


class FakeKlines:
    """Drop-in for signal_service.fetch_klines: serves [start_ms, end_ms] slices of the
    synthetic walk (closed bars only, like the real one), records every requested start_ms
    per symbol, and can be told to fail for chosen symbols."""

    def __init__(self):
        self.calls = defaultdict(list)           # symbol -> [start_ms, ...]
        self.fail = set()
        self._lock = threading.Lock()

    def __call__(self, symbol, start_ms, end_ms, interval='1d'):
        with self._lock:
            self.calls[symbol].append(int(start_ms))
        assert interval == INTERVAL, f'unexpected interval {interval!r}'
        if symbol in self.fail:
            raise RuntimeError('synthetic fetch failure')
        bars = _source_bars(symbol)
        open_ms = bars.index.asi8 // 1_000_000
        keep = ((open_ms >= int(start_ms)) & (open_ms <= int(end_ms))
                & (open_ms + HOUR_MS <= FAKE_NOW_MS))    # CLOSED candles only
        return bars.loc[keep].copy()


def _no_network(*a, **kw):
    raise AssertionError('vision_klines.fetch_rows called — a test tried to hit the network')


@pytest.fixture()
def fake_market(monkeypatch):
    """signal_service wired to the synthetic source; the real kline transport is booby-trapped
    so any escape past the fake is a hard failure, not a silent network call."""
    fake = FakeKlines()
    monkeypatch.setattr(ss, 'fetch_klines', fake)
    monkeypatch.setattr(ss, '_now_ms', lambda: FAKE_NOW_MS)
    monkeypatch.setattr(ss.vk, 'fetch_rows', _no_network)
    return fake


def _write_snapshot(path, tickers, n_bars=None, corrupt_last=False):
    """(tickers, [df]) pickle shaped like the GUI data fetcher's per-tf snapshot: OHLCV plus
    a 'funding' column that live fetches never have."""
    frames = []
    for t in tickers:
        df = _source_bars(t).copy()
        if n_bars is not None:
            df = df.iloc[:n_bars].copy()
        df['funding'] = 0.0001
        if corrupt_last:                          # a still-open / garbage last bar
            df.iloc[-1] = [999_999.0, 999_999.5, 999_998.5, 999_999.0, 0.0, 0.0001]
        frames.append(df)
    with open(path, 'wb') as f:
        pickle.dump((list(tickers), frames), f)
    return str(path)


def _cold(fake, tickers=TICKERS):
    return ss.fetch_live_dfs(tickers, START_ISO, interval=INTERVAL)


# ---------------- 1. _snapshot_dfs ----------------
def test_snapshot_dfs_filters_tickers_drops_funding_respects_start(tmp_path, fake_market):
    snap = _write_snapshot(tmp_path / 'snap.pickle', TICKERS + ['ZZZUSDT'])
    start_ts = GRID_START + pd.Timedelta(hours=100)
    out = ss._snapshot_dfs(snap, set(TICKERS), start_ts)

    assert set(out) == set(TICKERS)              # only requested tickers, ZZZUSDT dropped
    for t in TICKERS:
        assert list(out[t].columns) == ['open', 'high', 'low', 'close', 'volume']
        assert 'funding' not in out[t].columns   # live fetches never had it — math parity
        assert out[t].index.min() == start_ts    # start_ts respected
        src = _source_bars(t)
        pdt.assert_frame_equal(out[t], src[src.index >= start_ts], check_freq=False)


def test_snapshot_dfs_missing_file_returns_empty(tmp_path, fake_market):
    assert ss._snapshot_dfs(str(tmp_path / 'does_not_exist.pickle'), set(TICKERS),
                            GRID_START) == {}


def test_snapshot_dfs_corrupt_file_returns_empty(tmp_path, fake_market):
    bad = tmp_path / 'corrupt.pickle'
    bad.write_bytes(b'this is definitely not a pickle')
    assert ss._snapshot_dfs(str(bad), set(TICKERS), GRID_START) == {}


# ---------------- 2. seed changes WHERE, never WHAT ----------------
def test_seeded_fetch_equals_cold_fetch(tmp_path, fake_market):
    fake = fake_market
    cold = _cold(fake)
    assert set(cold) == set(TICKERS)
    for t in TICKERS:
        assert fake.calls[t] == [GRID_START_MS]  # cold = paged from the very start

    snap = _write_snapshot(tmp_path / 'snap.pickle', TICKERS, n_bars=300)
    fake.calls.clear()
    seeded = ss.fetch_live_dfs(TICKERS, START_ISO, interval=INTERVAL, data_path=snap)

    tail_ms = GRID_START_MS + 298 * HOUR_MS      # second-to-last snapshot bar (bar 298 of 300)
    for t in TICKERS:
        assert fake.calls[t] == [tail_ms]        # WHERE changed: one tail request, no cold page
        pdt.assert_frame_equal(seeded[t], cold[t], check_freq=False)   # WHAT did not
        assert len(seeded[t]) == N_BARS


# ---------------- 3. incremental refresh ----------------
def test_incremental_refresh_fetches_only_the_tail(fake_market):
    fake = fake_market
    first = _cold(fake)
    fake.calls.clear()

    second = ss.fetch_live_dfs(TICKERS, START_ISO, interval=INTERVAL, cache=first)

    penultimate_ms = GRID_START_MS + (N_BARS - 2) * HOUR_MS
    for t in TICKERS:
        assert len(fake.calls[t]) == 1           # one request per pair, not a paging loop
        for req in fake.calls[t]:
            assert req >= penultimate_ms         # near the cached tail...
            assert req > GRID_START_MS           # ...never the cold start (the 17-min bug)
        pdt.assert_frame_equal(second[t], first[t], check_freq=False)  # == a fresh cold fetch


# ---------------- 4. partial-bar healing ----------------
def test_seeded_fetch_replaces_corrupt_last_snapshot_bar(tmp_path, fake_market):
    fake = fake_market
    cold = _cold(fake)
    snap = _write_snapshot(tmp_path / 'snap.pickle', TICKERS, corrupt_last=True)
    fake.calls.clear()

    seeded = ss.fetch_live_dfs(TICKERS, START_ISO, interval=INTERVAL, data_path=snap)
    for t in TICKERS:
        last = seeded[t].iloc[-1]
        assert last['close'] != 999_999.0        # the garbage bar did not survive
        src_last = _source_bars(t).iloc[-1]
        assert last['close'] == src_last['close']
        assert last['volume'] == src_last['volume']
        pdt.assert_frame_equal(seeded[t], cold[t], check_freq=False)


# ---------------- 5. resilience ----------------
def test_failing_ticker_sits_out_others_intact(fake_market):
    fake = fake_market
    cold = _cold(fake)
    fake.calls.clear()
    fake.fail.add('BBBUSDT')

    out = ss.fetch_live_dfs(TICKERS, START_ISO, interval=INTERVAL)
    assert 'BBBUSDT' not in out                  # the failure is contained...
    assert set(out) == {'AAAUSDT', 'CCCUSDT'}
    for t in out:                                # ...and does not distort the survivors
        pdt.assert_frame_equal(out[t], cold[t], check_freq=False)


# ---------------- 6. compute_from_dfs_fast smoke ----------------
def test_compute_from_dfs_fast_smoke(fake_market):
    dfs = _cold(fake_market)
    formulas = ['ts_mean:20(ret)', 'neg(ts_mean:5(logret))']
    sig = ss.compute_from_dfs_fast(formulas, dfs, START_ISO, vol=0.25, exec_rate=0.0005,
                                   tf=INTERVAL)

    last_bar = GRID_START + pd.Timedelta(hours=N_BARS - 1)
    assert sig['as_of'] == f'{last_bar:%Y-%m-%d %H:%M}'    # last bar of the synthetic grid
    assert sig['leverage'] == 1.0                # intraday path: weight carries lev already
    assert sig['n_assets'] == len(TICKERS)
    assert isinstance(sig['positions'], list) and sig['positions']
    for p in sig['positions']:
        assert set(p) == {'ticker', 'side', 'weight', 'weight_pct'}
        assert p['ticker'] in TICKERS
        assert abs(p['weight']) <= 1.0
        assert abs(p['weight']) > ss.DUST_W
        assert p['side'] == ('LONG' if p['weight'] > 0 else 'SHORT')
    weights = [abs(p['weight']) for p in sig['positions']]
    assert weights == sorted(weights, reverse=True)        # served biggest-first


# ---- the GUI card: a green 'serving', and no more than ten services ---------------------

import types

import pytest


@pytest.mark.gui
def test_serving_reads_green_and_only_serving(gui_app):
    """'Is my API up?' should be readable from across the room: green belongs to '● serving'
    alone — a warning is loss-coloured, a stopped process fades, transitions stay neutral."""
    app, _rec, _state = gui_app
    import alphanode_gui as G
    assert app._sig_status_color('● serving · updated …Z (6s ago)') == G.POS
    assert app._sig_status_color('● serving') == G.POS
    assert app._sig_status_color('⚠ data source unreachable') == G.NEG
    assert app._sig_status_color('○ stopped (the process exited) — port is free') == G.FAINT
    for neutral in ('starting…', '○ computing the first signal…', 'reconnecting…'):
        assert app._sig_status_color(neutral) == G.MUT


@pytest.mark.gui
def test_the_row_label_actually_wears_the_color(gui_app):
    app, _rec, _state = gui_app
    import alphanode_gui as G
    app._sigs.append({'port': 8799, 'proc': types.SimpleNamespace(poll=lambda: None, pid=1),
                      'pid': 1, 'label': 'alpha_x', 'n_formulas': 1, 'n_tickers': 5,
                      'started': '2026-08-29 17:22'})
    app._sig_health[8799] = '● serving'
    app._render_signal_rows()
    app.root.update_idletasks()
    assert app._sig_status_lbl[8799].cget('fg') == G.POS
    app._sig_health[8799] = '⚠ trouble'
    app._sig_shown = tuple(s['port'] for s in app._sigs)  # same set -> tick refreshes in place
    app._sig_tick()
    assert app._sig_status_lbl[8799].cget('fg') == G.NEG
    app._sigs.clear()


def _fake_sig(port, dead=False):
    return {'port': port, 'proc': types.SimpleNamespace(poll=lambda: (0 if dead else None)),
            'pid': port, 'label': f'alpha_{port}', 'n_formulas': 1, 'n_tickers': 5}


@pytest.mark.gui
def test_the_eleventh_service_is_refused(gui_app, monkeypatch):
    """Ten engine processes re-simulating every refresh is already a small server farm —
    the eleventh gets a warning naming the way out, and no process is spawned."""
    app, rec, _state = gui_app
    import alphanode_gui as G
    app._sigs.extend(_fake_sig(8800 + i) for i in range(app.SIG_MAX))
    monkeypatch.setattr(G.subprocess, 'Popen',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('spawned!')))
    app._serve_signal(['tanh(low)'], 'one_too_many')
    kind, title, msg = rec.calls[-1]
    assert kind == 'showwarning' and 'limit' in msg and 'Free a port' in msg
    assert len(app._sigs) == app.SIG_MAX
    app._sigs.clear()


@pytest.mark.gui
def test_dead_rows_do_not_count_against_the_cap(gui_app, monkeypatch):
    """A row whose process exited still sits in the list saying 'port is free' — it must not
    occupy a slot, or ten dead rows would lock the feature shut."""
    app, rec, _state = gui_app
    import alphanode_gui as G
    app._sigs.extend(_fake_sig(8800 + i, dead=True) for i in range(app.SIG_MAX))
    spawned = {}

    def fake_popen(*a, **k):
        spawned['yes'] = True
        return types.SimpleNamespace(pid=424242, poll=lambda: None)

    monkeypatch.setattr(G.subprocess, 'Popen', fake_popen)
    app._serve_signal(['tanh(low)'], 'fits_fine')
    assert spawned.get('yes')                            # the cap ignored the corpses
    assert not [c for c in rec.calls if c[0] in ('showwarning', 'showerror')]
    app._sigs.clear()


@pytest.mark.gui
def test_reshowing_an_existing_label_is_never_capped(gui_app, monkeypatch):
    """Serving something already served just re-renders the card — even at the limit."""
    app, rec, _state = gui_app
    import alphanode_gui as G
    app._sigs.extend(_fake_sig(8800 + i) for i in range(app.SIG_MAX))
    monkeypatch.setattr(G.subprocess, 'Popen',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('spawned!')))
    app._serve_signal(['tanh(low)'], 'alpha_8801')       # that label is already on a row
    assert not [c for c in rec.calls if c[0] == 'showwarning']
    app._sigs.clear()


# ---------------- Serve on a paper bot: the service runs like the bot, not like Settings ----

def _bot_on_disk(state, **over):
    """A forward entry frozen on a DIFFERENT tf/universe/vol/fee than the fixture's panel
    (timeframe '1h', universe all) — so any leak from Settings shows in the env."""
    import json
    import forward_track as ft
    kw = dict(tf='4h', entry_id='b0t001')
    kw.update(over)
    e = ft.new_entry('b0t001', 'alpha', ['tanh(low)'], ['BTCUSDT', 'ETHUSDT'], 0.4, 0.002,
                     '2023-05-01', **kw)
    (state / 'forward.json').write_text(json.dumps({'entries': [e]}))
    return e


def _capture_popen(monkeypatch):
    import alphanode_gui as G
    seen = {}

    def fake_popen(*a, **k):
        seen['env'] = k['env']
        return types.SimpleNamespace(pid=424242, poll=lambda: None)
    monkeypatch.setattr(G.subprocess, 'Popen', fake_popen)
    return seen


@pytest.mark.gui
def test_serving_a_paper_bot_uses_its_frozen_params(gui_app, monkeypatch):
    import json
    app, rec, state = gui_app
    _bot_on_disk(state)
    seen = _capture_popen(monkeypatch)
    app._fwd_refresh()
    app.fwd_tree.selection_set('b0t001')
    app._fwd_serve()
    env = seen['env']
    assert env['ALPHANODE_TF'] == '4h'                   # the bot's bar size, not the panel's 1h
    assert json.loads(env['ALPHANODE_SIGNAL_FORMULAS']) == ['tanh(low)']
    assert env['ALPHANODE_SIGNAL_TICKERS'] == 'BTCUSDT,ETHUSDT'
    assert env['ALPHANODE_TARGET_VOL'] == '0.4'
    assert env['ALPHANODE_EXEC_COST'] == '0.002'
    assert env['ALPHANODE_SIGNAL_START'] == '2023-05-01'
    assert env['ALPHANODE_SIGNAL_NAME'] == 'b0t001'
    assert env['ALPHANODE_DATA'].endswith(app._data_file_for('4h').split(os.sep)[-1])
    assert env['ALPHANODE_DATA'] != app._data_file()     # the 4h snapshot, not the 1h one
    assert env['ALPHANODE_SIGNAL_REFRESH'] == '900'
    assert app._sigs[-1]['label'] == 'b0t001'
    assert not [c for c in rec.calls if c[0] in ('showwarning', 'showerror')]
    assert app._test_tk_errors == []
    app._sigs.clear()


@pytest.mark.gui
def test_serving_a_paper_bot_twice_just_reshows_it(gui_app, monkeypatch):
    app, rec, state = gui_app
    import alphanode_gui as G
    _bot_on_disk(state)
    app._sigs.append({**_fake_sig(8805), 'label': 'b0t001'})   # already on a row
    monkeypatch.setattr(G.subprocess, 'Popen',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('spawned!')))
    app._fwd_refresh()
    app.fwd_tree.selection_set('b0t001')
    app._fwd_serve()
    assert len(app._sigs) == 1
    assert not [c for c in rec.calls if c[0] in ('showwarning', 'showerror')]
    app._sigs.clear()


@pytest.mark.gui
def test_serve_with_nothing_selected_asks_to_select(gui_app, monkeypatch):
    app, rec, state = gui_app
    import alphanode_gui as G
    _bot_on_disk(state)
    monkeypatch.setattr(G.subprocess, 'Popen',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('spawned!')))
    app._fwd_refresh()
    app.fwd_tree.selection_remove(app.fwd_tree.selection())
    app._fwd_serve()
    assert rec.calls[-1][0] == 'showinfo' and 'Select' in rec.calls[-1][2]
    assert app._sigs == []


@pytest.mark.gui
def test_the_leaderboard_serve_still_uses_live_settings(gui_app, monkeypatch):
    """The refactor must not change the panel callers: no keyword args -> the active
    timeframe, the configured vol/fee, and config.ini's start (no SIGNAL_START at all)."""
    app, rec, _state = gui_app
    seen = _capture_popen(monkeypatch)
    app._serve_signal(['tanh(low)'], 'alpha_x')
    env = seen['env']
    assert env['ALPHANODE_TF'] == '1h'
    assert 'ALPHANODE_SIGNAL_START' not in env
    assert env['ALPHANODE_TARGET_VOL'] == str(app.cfg['target_vol'])
    assert env['ALPHANODE_DATA'] == app._data_file()
    app._sigs.clear()


@pytest.mark.gui
def test_the_forward_toolbar_has_a_serve_button(gui_app):
    app, _rec, _state = gui_app
    assert app.btn_fwd_serve.cget('text') == 'Serve'
    assert app.btn_fwd_serve.winfo_manager() == 'pack'

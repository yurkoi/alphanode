"""The basket is capped at MAX_PAIRS pairs — everywhere, not just in the panel.

The universe is parsed at three independent points: the GUI editor, the engine's config
loader and the node's own read of ALPHANODE_UNIVERSE. A cap in one of them is advice; these
tests pin all three, and pin the two copies of the number to each other.
"""
import pytest

import alphanode_gui as G
import config as C


def _many(n, prefix='T'):
    return ','.join(f'{prefix}{i}USDT' for i in range(n))


def test_the_two_copies_of_the_cap_agree():
    """The GUI keeps its own MAX_PAIRS so it need not import config (which pulls pandas in at
    startup). That is only safe while the numbers match."""
    assert G.MAX_PAIRS == C.MAX_PAIRS == 20


# ---------- the GUI editor ----------

def test_the_shared_parser_caps_the_list():
    assert len(G._parse_universe(_many(50))) == G.MAX_PAIRS


def test_the_parser_can_be_asked_for_the_uncapped_list():
    """The editor needs the true length to say how many pairs it had to drop."""
    assert len(G._parse_universe(_many(50), cap=None)) == 50


def test_duplicates_are_removed_before_the_cap_not_after():
    """21 entries where two are the same pair is 20 distinct pairs — it must fit, not lose one."""
    raw = _many(20) + ',T0USDT'
    assert len(G._parse_universe(raw)) == 20
    assert 'T19USDT' in G._parse_universe(raw)


def test_a_short_list_is_untouched():
    assert G._parse_universe(' btc , ethusdt,, BTC ') == ['BTC', 'ETHUSDT']


def test_an_oversized_settings_file_is_capped_on_read():
    """A config written by an older build, or edited by hand, must not smuggle 50 pairs in."""
    assert len(G._parse_universe(_many(50))) == G.MAX_PAIRS


# ---------- the engine ----------

def test_load_config_caps_the_env_universe(monkeypatch):
    monkeypatch.setenv('ALPHANODE_UNIVERSE', _many(50))
    assert len(C.load_config()['instruments']) == C.MAX_PAIRS


def test_load_config_dedupes_the_env_universe(monkeypatch):
    monkeypatch.setenv('ALPHANODE_UNIVERSE', 'btcusdt,BTCUSDT, ethusdt ')
    assert C.load_config()['instruments'] == ['BTCUSDT', 'ETHUSDT']


def test_all_still_means_the_whole_snapshot(monkeypatch):
    monkeypatch.setenv('ALPHANODE_UNIVERSE', 'all')
    assert C.load_config()['instruments'] is None


def test_a_newline_separated_universe_is_capped_too(monkeypatch):
    """config.ini allows a multi-line list; it must not be a way around the cap."""
    monkeypatch.setenv('ALPHANODE_UNIVERSE', _many(40).replace(',', '\n'))
    assert len(C.load_config()['instruments']) == C.MAX_PAIRS


# ---------- the node ----------

def test_the_node_caps_the_basket_it_mines(monkeypatch):
    import node as N
    monkeypatch.setattr(N, 'UNIVERSE', _many(50))
    assert len(N.build_cfg(0)['instruments']) == N.MAX_PAIRS


def test_the_node_caps_the_basket_it_downloads(monkeypatch, tmp_path):
    """ensure_data fetches exactly the configured pairs. Uncapped, it would pull 50 pairs of
    history for a search that can only ever look at 20 — minutes of download, then discarded."""
    import fetch_data
    import node as N
    asked = {}

    def fake_run(_path, interval=None, symbols=None):
        asked['symbols'] = list(symbols or [])
        return 0                                         # no file appears -> ensure_data bails

    monkeypatch.setattr(N, 'UNIVERSE', _many(50))
    monkeypatch.setattr(N, 'load_config', lambda *_a, **_k: {'data': str(tmp_path / 'absent.pkl')})
    monkeypatch.setattr(fetch_data, 'run', fake_run)
    with pytest.raises(SystemExit):                      # the missing snapshot, after the fetch
        N.ensure_data()
    assert len(asked['symbols']) == N.MAX_PAIRS


# ---------- what the editor tells the user ----------

class _Var:
    def __init__(self, v=''):
        self.v = v

    def get(self):
        return self.v

    def set(self, v):
        self.v = v


class _Entry:
    def __init__(self, text):
        self.text = text

    def get(self):
        return self.text

    def delete(self, *_a):
        self.text = ''


class _Editor:
    """Just enough of App for _uni_commit: it is the ONE place a paste can overflow."""
    _uni_raw = staticmethod(G.App._uni_raw)     # else it binds and eats an argument
    _uni_commit = G.App._uni_commit

    def __init__(self, existing, typed):
        self.v_unilist, self.e_uni = _Var(existing), _Entry(typed)


def test_an_oversized_paste_keeps_the_first_twenty():
    ed = _Editor('', _many(25))
    ed._uni_commit()
    assert len(G._parse_universe(ed.v_unilist.get(), cap=None)) == 20
    assert ed.v_unilist.get().startswith('T0USDT,T1USDT')


def test_the_editor_counts_what_it_had_to_drop():
    """Silently swallowing five pairs is what a person reads as the field losing their input."""
    ed = _Editor('', _many(25))
    ed._uni_commit()
    assert ed._uni_over == 5


def test_nothing_is_reported_when_the_paste_fits():
    ed = _Editor('BTCUSDT', 'ETHUSDT SOLUSDT')
    ed._uni_commit()
    assert ed._uni_over == 0
    assert ed.v_unilist.get() == 'BTCUSDT,ETHUSDT,SOLUSDT'


def test_adding_onto_a_full_basket_reports_the_overflow():
    ed = _Editor(_many(20), 'NEWUSDT')
    ed._uni_commit()
    assert ed._uni_over == 1
    assert 'NEWUSDT' not in ed.v_unilist.get(), 'the cap must hold; the newcomer is refused'

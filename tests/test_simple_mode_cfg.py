"""ui_mode: who opens on the simple screen and who keeps their dashboard.

Headless — App._load/_reset are exercised on a stand-in, no Tk root, no window."""
import json

import pytest

import alphanode_gui as G


class _Shim:
    """Just enough of App for the persistence methods."""
    _load = G.App._load
    _reset = G.App._reset

    def __init__(self):
        self.cfg = dict(G.DEFAULTS)

    def _snapshot_tickers(self):
        return []

    def _apply_cfg_to_widgets(self):
        pass


def _saved(tmp_path, monkeypatch, payload):
    p = tmp_path / 'gui_settings.json'
    p.write_text(json.dumps(payload), encoding='utf-8')
    monkeypatch.setattr(G, 'SETTINGS', str(p))
    return p


def test_a_fresh_install_opens_simple():
    assert G.DEFAULTS['ui_mode'] == 'simple'


def test_an_existing_settings_file_keeps_the_dashboard(tmp_path, monkeypatch):
    """The update must not vanish anyone's dashboard: a file saved before simple mode
    existed migrates to 'advanced'; only fresh installs open on the simple screen."""
    _saved(tmp_path, monkeypatch, {'cpu': 60})
    app = _Shim()
    app._load()
    assert app.cfg['ui_mode'] == 'advanced'


def test_a_legacy_ui_mode_value_is_sanitized(tmp_path, monkeypatch):
    """The RETIRED simple mode also wrote ui_mode — its values are not ours."""
    _saved(tmp_path, monkeypatch, {'ui_mode': 'normal'})
    app = _Shim()
    app._load()
    assert app.cfg['ui_mode'] == 'advanced'


@pytest.mark.parametrize('mode', ['simple', 'advanced'])
def test_a_valid_saved_mode_survives_the_load(tmp_path, monkeypatch, mode):
    _saved(tmp_path, monkeypatch, {'ui_mode': mode})
    app = _Shim()
    app._load()
    assert app.cfg['ui_mode'] == mode


def test_reset_to_defaults_does_not_flip_the_mode(tmp_path, monkeypatch):
    """Reset is about the search, not the face: an advanced user pressing it must not be
    teleported to the simple screen."""
    monkeypatch.setattr(G, 'SETTINGS', str(tmp_path / 'gui_settings.json'))
    app = _Shim()
    app.cfg['ui_mode'] = 'advanced'
    app.cfg['pop'] = 999
    app._reset()
    assert app.cfg['ui_mode'] == 'advanced'
    assert app.cfg['pop'] == G.DEFAULTS['pop']


def test_the_welcomed_key_still_dies(tmp_path, monkeypatch):
    _saved(tmp_path, monkeypatch, {'welcomed': True})
    app = _Shim()
    app._load()
    assert 'welcomed' not in app.cfg

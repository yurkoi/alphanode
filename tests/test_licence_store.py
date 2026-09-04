"""One activation per machine: the key is shared by every install (and every screen) here."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'alphanode'))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'evolution'))
import apppaths                                          # noqa: E402
import licence_store as LS                               # noqa: E402
import alphanode_gui as G                                # noqa: E402


def test_store_round_trip_is_private_and_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(apppaths, 'machine_dir', lambda: str(tmp_path))
    assert LS.load() == ''                               # nothing activated on this machine yet
    assert LS.save('  key-123  ')
    assert LS.load() == 'key-123'
    assert oct(os.stat(LS.path()).st_mode & 0o777) == '0o600'
    assert not os.path.exists(LS.path() + '.tmp')
    assert not LS.save('') and LS.load() == 'key-123', 'an empty key never clobbers the store'


class _App:
    _adopt_shared_licence = G.App._adopt_shared_licence

    def __init__(self, own=''):
        self.cfg = {'vault_license': own}


def test_an_install_without_a_key_adopts_the_machines(tmp_path, monkeypatch):
    monkeypatch.setattr(apppaths, 'machine_dir', lambda: str(tmp_path))
    LS.save('shared-key')
    app = _App()
    app._adopt_shared_licence()
    assert app.cfg['vault_license'] == 'shared-key' and app._licence_adopted
    own = _App(own='my-own-key')
    own._adopt_shared_licence()
    assert own.cfg['vault_license'] == 'my-own-key' and not own._licence_adopted


def test_sealed_rows_are_detected_across_timeframes(tmp_path):
    (tmp_path / 'library.jsonl').write_text('{"formula": "open(x)", "base": 1}\n', encoding='utf-8')
    assert not G.App._sealed_rows_exist(str(tmp_path))
    (tmp_path / 'library_1h.jsonl').write_text(
        json.dumps({'locked': True, 'id': 'a' * 12, 'formula_enc': 'v2:x'}) + '\n', encoding='utf-8')
    assert G.App._sealed_rows_exist(str(tmp_path))
    (tmp_path / 'library_1h.jsonl').write_text(       # revealed rows keep formula_enc, drop locked
        json.dumps({'id': 'a' * 12, 'formula_enc': 'v2:x', 'formula': 'open(x)'}) + '\n', encoding='utf-8')
    assert not G.App._sealed_rows_exist(str(tmp_path))
    assert not G.App._sealed_rows_exist(str(tmp_path / 'missing'))

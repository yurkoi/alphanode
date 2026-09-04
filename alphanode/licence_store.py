"""The subscription key, kept once per MACHINE rather than once per install.

An activation is a statement about the customer's machine, not about the folder the app runs
from — yet the key used to live in each install's own gui_settings.json, so a .deb, an AppImage
and a dev checkout on the same box each had to be activated by hand, and the one you forgot
mined sealed into a library nobody could open (seen on the vendor's own machine: 56 sealed rows
in the .deb's library next to an activated checkout). This store is the shared copy: the first
install a key is accepted on writes it here, every other install adopts it at launch and
activates itself (alphanode_gui._auto_activate) — seat claim plus the reveal of whatever it
holds sealed — without a click. 0600, JSON, atomic replace."""
import json
import os

import apppaths


def path():
    return os.path.join(apppaths.machine_dir(), 'licence.json')


def load():
    """The shared key, or '' when no install on this machine has been activated yet."""
    try:
        with open(path(), encoding='utf-8') as f:
            return str(json.load(f).get('token') or '').strip()
    except (OSError, ValueError, AttributeError):
        return ''


def save(token):
    """Remember an ACCEPTED key for every install here. Returns False (never raises) when the
    folder is not writable — the install that has the key in its own settings still works."""
    token = str(token or '').strip()
    if not token:
        return False
    p = path()
    tmp = p + '.tmp'
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'token': token}, f)
        os.replace(tmp, p)
        return True
    except OSError:
        return False

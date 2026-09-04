"""No emoji-presentation glyphs in anything the GUI renders.

Ubuntu 22.04 ships libXft 2.3.4, which SEGFAULTS on colour-emoji glyphs (Noto Color
Emoji); the victim is whichever X call comes next, so the crash lands far from the
culprit. Two real core dumps (2026-09-01, 2026-09-02) traced back to '⏳' in a service
health line. Monochrome symbols (● ○ ⚠ ★ ▸ ✕ …) are fine — this list is only the
codepoints whose DEFAULT presentation is emoji.
"""
import glob
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), 'alphanode')

EMOJI = set(range(0x1F000, 0x1FB00)) | {0x231A, 0x231B} | set(range(0x23E9, 0x23F4)) | {
    0x25FD, 0x25FE, 0x2614, 0x2615, 0x267F, 0x2693, 0x26A1, 0x26AA, 0x26AB, 0x26BD, 0x26BE,
    0x26C4, 0x26C5, 0x26CE, 0x26D4, 0x26EA, 0x26F2, 0x26F3, 0x26F5, 0x26FA, 0x26FD, 0x2705,
    0x270A, 0x270B, 0x2728, 0x274C, 0x274E, 0x2753, 0x2754, 0x2755, 0x2757, 0x2795, 0x2796,
    0x2797, 0x27B0, 0x27BF, 0x2B1B, 0x2B1C, 0x2B50, 0x2B55}


def test_no_colour_emoji_glyphs_in_the_app_sources():
    bad = []
    for path in sorted(glob.glob(os.path.join(APP, '*.py'))):
        for n, line in enumerate(io.open(path, encoding='utf-8'), 1):
            hits = ''.join(ch for ch in line if ord(ch) in EMOJI)
            if hits:
                bad.append(f'{os.path.basename(path)}:{n}: {hits}')
    assert not bad, 'colour-emoji glyphs segfault libXft — use monochrome symbols:\n' + '\n'.join(bad)

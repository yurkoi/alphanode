"""The served signal, drawn under its service row as a grid of position tiles.

No Tk here on purpose: _draw_signal_positions talks to a canvas through a handful of
methods, so a recording stand-in exercises the real geometry without a DISPLAY — these
tests run on a headless box and never flash a window at whoever is working on the machine.
"""
import json
import pytest

import alphanode_gui as G

G._apply_palette('light')                    # the drawing reads the palette globals


class _Font:
    """Roughly a 10pt mono: enough for the column-width maths to be exercised."""
    def __init__(self, per=7.2):
        self.per = per

    def measure(self, t):
        return int(len(t) * self.per)


class _Canvas:
    def __init__(self, width=1400):
        self._w, self._h, self.ops = width, 1, []

    def winfo_exists(self):
        return True

    def winfo_width(self):
        return self._w

    def delete(self, *_):
        self.ops.clear()

    def __getitem__(self, k):
        assert k == 'height'
        return self._h

    def configure(self, **kw):
        self._h = kw.get('height', self._h)

    def create_text(self, x, y, **kw):
        self.ops.append(('text', x, y, kw.get('text', ''), kw.get('anchor'), kw.get('fill')))

    def create_rectangle(self, *a, **kw):
        self.ops.append(('rect', a, kw.get('fill')))

    def create_oval(self, *a, **kw):
        self.ops.append(('oval', a, kw.get('fill')))


class _App:
    SCALE = 1.0
    SIG_POS_MAX = G.App.SIG_POS_MAX
    SIG_TINT = G.App.SIG_TINT
    UI = 'ui'
    MONO = 'mono'
    _capsule = staticmethod(G.App._capsule)
    _ellipsize = staticmethod(G.App._ellipsize)
    _draw_signal_positions = G.App._draw_signal_positions
    _sig_poll_worker = G.App._sig_poll_worker

    def __init__(self):
        self._sig_canvas, self._sig_signal, self._sig_drawn = {}, {}, {}
        self._sig_health, self._sig_seen = {}, {}

    def _font(self, *_a):
        return _Font()


def _pos(ticker, weight):
    return {'ticker': ticker, 'side': 'LONG' if weight > 0 else 'SHORT',
            'weight': weight, 'weight_pct': f'{weight * 100:+.1f}%'}


def _paint(positions, width=1400, **payload):
    app, cv = _App(), _Canvas(width)
    sig = {'ok': True, 'as_of': '2026-09-01 10:00', 'tf': '1h', 'leverage': 1.0,
           'updated_at': 'STAMP', 'positions': positions}
    sig.update(payload)
    app._sig_canvas[1], app._sig_signal[1] = cv, sig
    app._draw_signal_positions(1)
    return app, cv


HEADH = 30                                   # _draw_signal_positions' header band at SCALE 1


def _shapes(cv, color, tiles_only=True):
    """Every rect/oval painted in `color` — a capsule is a rect plus two caps, and a very
    short one is a lone cap, so a test must not assume either shape. The header draws a dot
    per side (including a 0.0% one), so tile assertions must skip that band."""
    return [o for o in cv.ops if o[0] in ('rect', 'oval') and o[2] == color
            and (not tiles_only or o[1][1] >= HEADH)]


def _extent(cv, color):
    """How far right the `color` capsule reaches from x=0 of its tile."""
    sh = _shapes(cv, color)
    return max(o[1][2] for o in sh) - min(o[1][0] for o in sh)


# ---------- the thing the redesign was for ----------

def _pills(cv):
    """One rect per pill (a capsule is a rect between two caps). The header draws ovals only,
    so rects are exactly the pills."""
    return [o for o in cv.ops if o[0] == 'rect']


def _dist(a, b):
    return sum(abs(int(a[i:i + 2], 16) - int(b[i:i + 2], 16)) for i in (1, 3, 5))


def test_the_book_is_not_painted_in_profit_and_loss_colours():
    """A position is a direction, not a profit. Green/red here was the complaint, and it is
    also the worst possible pair for a red-green colourblind reader."""
    _app, cv = _paint([_pos('AAAUSDT', -0.2), _pos('BBBUSDT', 0.1)])
    painted = {o[2] for o in cv.ops if o[0] in ('rect', 'oval')}
    assert G.POS not in painted and G.NEG not in painted


def test_a_pill_is_sized_by_its_own_text_not_by_the_card():
    """THE point of this design. The card is far wider than five numbers need; the previous
    two attempts stretched their bars to fill it and five pairs became five full-width slabs."""
    book = [_pos('AAAUSDT', -0.2), _pos('BBBUSDT', 0.1)]
    _app, wide = _paint(book, width=1800)
    _app, mid = _paint(book, width=900)
    assert sorted(round(r[1][2] - r[1][0]) for r in _pills(wide)) == \
           sorted(round(r[1][2] - r[1][0]) for r in _pills(mid))


def test_five_pairs_do_not_fill_a_wide_card():
    _app, cv = _paint([_pos(f'T{i}USDT', -0.1) for i in range(5)], width=1800)
    assert max(r[1][2] for r in _pills(cv)) < 1800 * 0.6


def test_pills_wrap_onto_more_lines_when_they_do_not_fit():
    book = [_pos(f'T{i}USDT', -0.1 - i / 100) for i in range(10)]
    _app, wide = _paint(book, width=1800)
    _app, narrow = _paint(book, width=400)
    assert narrow._h > wide._h
    assert len({round(r[1][1]) for r in _pills(narrow)}) > \
           len({round(r[1][1]) for r in _pills(wide)}), 'more lines expected'


def test_a_heavier_position_is_tinted_deeper():
    """The fill carries the book's shape, so it reads before you parse any number."""
    _app, cv = _paint([_pos('AAAUSDT', -0.20), _pos('BBBUSDT', -0.02)])
    fills = [r[2] for r in sorted(_pills(cv), key=lambda r: r[1][0])]
    assert _dist(fills[0], G.SHORTC) < _dist(fills[1], G.SHORTC)


def test_each_side_gets_its_own_hue():
    """Warm vs cool, not distance-to-token: the fills are heavy tints toward CARD, and
    a deep amber tinted 88% white sits nearer the indigo token by raw distance while
    still reading unmistakably warm."""
    def channels(c):
        return int(c[1:3], 16), int(c[5:7], 16)          # R, B
    _app, cv = _paint([_pos('AAAUSDT', -0.2), _pos('BBBUSDT', 0.2)])
    fills = [r[2] for r in sorted(_pills(cv), key=lambda r: r[1][0])]
    r0, b0 = channels(fills[0])
    r1, b1 = channels(fills[1])
    assert r0 > b0, 'the short pill must lean warm: ' + fills[0]
    assert b1 > r1, 'the long pill must lean cool: ' + fills[1]


def test_both_themes_define_the_side_colours():
    for theme in ('light', 'dark'):
        G._apply_palette(theme)
        assert G.LONGC.startswith('#') and G.SHORTC.startswith('#')
        assert G.LONGC != G.SHORTC
    G._apply_palette('light')


def test_pills_never_leave_the_canvas():
    _app, cv = _paint([_pos(f'T{i}USDT', -0.1) for i in range(9)], width=700)
    for kind, box, _fill in [o for o in cv.ops if o[0] in ('rect', 'oval')]:
        assert box[0] >= -0.01 and box[2] <= 700.01, f'{kind} out of bounds: {box}'


# ---------- the header ----------

def test_the_header_reports_each_side_separately():
    _app, cv = _paint([_pos('AAAUSDT', -0.20), _pos('BBBUSDT', 0.10), _pos('CCCUSDT', 0.05)])
    texts = [o[3] for o in cv.ops if o[0] == 'text']
    assert 'LONG 15.0%' in texts and 'SHORT 20.0%' in texts
    meta = next(t for t in texts if 'gross' in t)
    assert 'gross 35.0%' in meta and 'net -5.0%' in meta
    assert '1h' in meta and 'lev 1' in meta


def test_the_header_keeps_a_zero_side_visible():
    """A book that just went one-sided must say so, not silently drop the label."""
    _app, cv = _paint([_pos('AAAUSDT', -0.2)])
    assert 'LONG 0.0%' in [o[3] for o in cv.ops if o[0] == 'text']


def test_the_side_labels_wear_their_own_colour():
    _app, cv = _paint([_pos('AAAUSDT', -0.2), _pos('BBBUSDT', 0.1)])
    by_text = {o[3]: o[5] for o in cv.ops if o[0] == 'text'}
    assert by_text['LONG 10.0%'] == G.LONGC
    assert by_text['SHORT 20.0%'] == G.SHORTC


# ---------- edges ----------

def test_a_book_longer_than_the_cap_is_trimmed_and_says_so():
    n = G.App.SIG_POS_MAX + 9
    _app, cv = _paint([_pos(f'T{i}USDT', -0.1 - i / 1000) for i in range(n)])
    assert len(_pills(cv)) == G.App.SIG_POS_MAX
    assert any('+9 more' in str(o[3]) for o in cv.ops if o[0] == 'text')


def test_an_empty_book_takes_no_vertical_space():
    _app, cv = _paint([])
    assert cv._h == 1 and cv.ops == []


def test_a_canvas_that_is_not_laid_out_yet_paints_nothing():
    """winfo_width() is 1 before the first <Configure>. The height must still be claimed so
    tk settles the layout and fires the bind that paints for real."""
    _app, cv = _paint([_pos('AAAUSDT', -0.2)], width=1)
    assert cv.ops == []
    assert cv._h > 1


def test_all_zero_weights_do_not_divide_by_zero():
    _app, cv = _paint([{'ticker': 'AAAUSDT', 'side': 'LONG', 'weight': 0.0, 'weight_pct': '0.0%'}])
    assert len(_pills(cv)) == 1


@pytest.mark.parametrize('ticker', ['BTCUSDT', '1000PEPEUSDT', 'A' * 30 + 'USDT'])
def test_the_ticker_never_runs_under_its_own_percentage(ticker):
    _app, cv = _paint([_pos(ticker, -0.2)], width=300)
    body = [o for o in cv.ops if o[0] == 'text' and o[2] >= HEADH]   # skip the header band
    tick = next(o for o in body if o[4] == 'w')
    pct = next(o for o in body if o[4] == 'e')
    assert tick[1] + _Font().measure(tick[3]) <= pct[1] - _Font().measure(pct[3]) + 1


def test_ellipsize_gives_up_rather_than_overflow():
    assert G.App._ellipsize(_Font(), 'BTCUSDT', 0) == ''
    assert G.App._ellipsize(_Font(), 'BTCUSDT', 10_000) == 'BTCUSDT'


# ---------- the fetch contract ----------

def _fake_http(monkeypatch, health, signal, calls):
    import urllib.request

    class _R:
        def __init__(self, body):
            self._b = json.dumps(body).encode()

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def urlopen(url, timeout=None):
        calls.append(url)
        return _R(signal if url.endswith('/signal') else health)

    monkeypatch.setattr(urllib.request, 'urlopen', urlopen)


def test_positions_are_refetched_only_when_the_stamp_moves(monkeypatch):
    """/health is the 3s heartbeat; /signal only changes once per refresh (15 min).
    Pulling the heavy endpoint on every tick would be 300 needless fetches per refresh."""
    calls = []
    health = {'ok': True, 'age_secs': 5.0, 'updated_at': 'T1'}
    signal = {'ok': True, 'positions': [_pos('AAAUSDT', -0.2)], 'updated_at': 'T1'}
    _fake_http(monkeypatch, health, signal, calls)
    app = _App()

    app._sig_poll_worker(8799)
    assert sum(u.endswith('/signal') for u in calls) == 1
    assert app._sig_signal[8799]['positions'][0]['ticker'] == 'AAAUSDT'

    app._sig_poll_worker(8799)                       # same stamp -> heartbeat only
    app._sig_poll_worker(8799)
    assert sum(u.endswith('/signal') for u in calls) == 1

    health['updated_at'] = 'T2'                      # a fresh signal -> pull it once
    app._sig_poll_worker(8799)
    app._sig_poll_worker(8799)
    assert sum(u.endswith('/signal') for u in calls) == 2


def test_a_dead_service_leaves_the_last_book_alone(monkeypatch):
    """The tiles are the last thing the user saw serving; blanking them on a transient
    network blip would read as 'the signal changed', which is a lie."""
    calls = []
    _fake_http(monkeypatch, {'ok': True, 'age_secs': 1.0, 'updated_at': 'T1'},
               {'ok': True, 'positions': [_pos('AAAUSDT', -0.2)], 'updated_at': 'T1'}, calls)
    app = _App()
    app._sig_poll_worker(8799)

    import urllib.request

    def boom(*_a, **_k):
        raise OSError('connection refused')

    monkeypatch.setattr(urllib.request, 'urlopen', boom)
    app._sig_poll_worker(8799)
    assert app._sig_health[8799] == 'starting…'
    assert app._sig_signal[8799]['positions'][0]['ticker'] == 'AAAUSDT'


def test_stopping_a_service_drops_its_book():
    """Ports get reused: the next service on 8799 serves a different formula, and the old
    positions must not linger under it while it computes its first signal."""
    app = _App()
    app._stop_signal = G.App._stop_signal.__get__(app)
    app._sig_save = lambda: None
    app._pid_on_port = lambda _p: None
    app._sigs = [{'port': 8799, 'proc': None, 'pid': None}]
    app._sig_health[8799] = '● serving'
    app._sig_signal[8799] = {'positions': [_pos('AAAUSDT', -0.2)]}
    app._sig_seen[8799] = 'T1'
    app._sig_drawn[8799] = 'T1'

    app._stop_signal(app._sigs[0])

    assert 8799 not in app._sig_signal, 'the stale book would be painted under the next service'
    assert 8799 not in app._sig_seen, 'a stale stamp would suppress the first real fetch'
    assert app._sig_health == {} and app._sig_drawn == {}

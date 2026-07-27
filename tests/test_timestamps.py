import time
from array import array

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gtk, Vte, GLib

from terminatorlib.timestamps import Timestamps, HASH_UNKNOWN

if not hasattr(Vte.Terminal, "get_text_range_format"):
    pytest.skip("VTE too old for get_text_range_format",
                allow_module_level=True)


class FakeTerminal:
    """Minimal stand-in for Terminal; Timestamps only needs vte+config"""

    def __init__(self, vte):
        self.vte = vte
        self.config = {'scrollback_infinite': False,
                       'scrollback_lines': 500}
        self.fgcolor_active = None


def pump_gtk(ms=200):
    """Let the GTK main loop process pending events for a while"""
    end = GLib.get_monotonic_time() + ms * 1000
    while GLib.get_monotonic_time() < end:
        while Gtk.events_pending():
            Gtk.main_iteration()
        GLib.usleep(10000)


def make_terminal(lines, width=80, height=10):
    """Create a realized Vte.Terminal pre-filled with the given lines"""
    window = Gtk.Window()
    vte = Vte.Terminal()
    vte.set_size(width, height)
    window.add(vte)
    window.show_all()
    pump_gtk()
    for line in lines:
        if isinstance(line, str):
            line = line.encode("utf-8")
        vte.feed(line + b"\r\n")
    pump_gtk()
    return window, vte


def make_tracker(vte):
    tracker = Timestamps(FakeTerminal(vte))
    tracker._update()
    return tracker


def test_new_lines_get_stamped():
    window, vte = make_terminal(["line one", "line two", "line three"])
    tracker = make_tracker(vte)
    assert not tracker.suspended
    for row in range(3):
        assert tracker.hashes[row] != HASH_UNKNOWN
        assert tracker.times[row] > 0
    tracker.close()
    window.destroy()


def test_modified_row_gets_new_time():
    window, vte = make_terminal(["original text", "filler"])
    tracker = make_tracker(vte)
    old_hash = tracker.hashes[0]
    tracker.times[0] = 1.0  # ancient, any rewrite must bump it
    vte.feed(b"\x1b[1;1H")  # cursor home
    vte.feed(b"CHANGED")
    pump_gtk()
    tracker._update()
    assert tracker.hashes[0] != old_hash
    assert tracker.times[0] > 1.0
    tracker.close()
    window.destroy()


def test_get_labels_folds_identical_timestamps():
    window, vte = make_terminal(["filler"])
    tracker = make_tracker(vte)
    base = time.time()
    tracker.times = array('d', [base, base, base, base + 3600])
    labels = tracker.get_labels(0, 4)
    assert sorted(labels.keys()) == [0, 3]
    assert labels[0] == time.strftime('%H:%M:%S', time.localtime(base))
    assert labels[3] == time.strftime('%H:%M:%S',
                                      time.localtime(base + 3600))
    # rows without a timestamp never get a label
    tracker.times[3] = 0.0
    assert tracker.get_labels(0, 4) == {0: labels[0]}
    tracker.close()
    window.destroy()


def test_alternate_screen_suspends_and_preserves():
    lines = ["line %02d" % i for i in range(15)]
    window, vte = make_terminal(lines, height=10)
    tracker = make_tracker(vte)
    assert tracker.seen_scrollback
    snapshot = list(tracker.times)
    assert len(snapshot) == int(vte.get_vadjustment().get_upper())

    vte.feed(b"\x1b[?1049h")  # enter alternate screen
    pump_gtk()
    tracker._update()
    assert tracker.suspended

    vte.feed(b"alt screen junk\r\n")  # must not touch tracked state
    pump_gtk()
    tracker._update()
    assert tracker.suspended
    assert list(tracker.times) == snapshot

    vte.feed(b"\x1b[?1049l")  # leave alternate screen
    pump_gtk()
    tracker._update()
    assert not tracker.suspended
    assert list(tracker.times) == snapshot
    tracker.close()
    window.destroy()


def test_reset_wipes_tracking():
    lines = ["line %02d" % i for i in range(15)]
    window, vte = make_terminal(lines, height=10)
    tracker = make_tracker(vte)
    assert len(tracker.times) == int(vte.get_vadjustment().get_upper())
    vte.reset(True, True)
    pump_gtk()
    tracker._update()
    # a reset removes the scrollback just like entering the alternate
    # screen, so the tracker suspends; once scrollback regrows past the
    # stale array length it rebuilds from scratch
    assert tracker.suspended
    for i in range(12):
        vte.feed(("new %02d" % i).encode("utf-8") + b"\r\n")
    pump_gtk()
    tracker._update()
    upper = int(vte.get_vadjustment().get_upper())
    assert not tracker.suspended
    assert len(tracker.times) == upper
    cursor_row = vte.get_cursor_position()[1]
    assert all(stamp > 0 for stamp in tracker.times[:cursor_row + 1])
    tracker.close()
    window.destroy()


def test_output_while_scrolled_up_gets_stamped():
    lines = ["line %02d" % i for i in range(20)]
    window, vte = make_terminal(lines, height=10)
    tracker = make_tracker(vte)
    vte.get_vadjustment().set_value(0)  # user scrolls to the top
    pump_gtk()
    vte.feed(b"written while scrolled\r\n")
    pump_gtk()
    tracker._update()
    upper = int(vte.get_vadjustment().get_upper())
    # the new row never entered the viewport but must still be stamped
    assert tracker.times[upper - 2] > 0
    assert tracker.times[upper - 1] > 0
    tracker.close()
    window.destroy()


def test_blank_rows_stamp_when_cursor_enters():
    window, vte = make_terminal(["line one"], height=10)
    tracker = make_tracker(vte)
    # rows below the cursor have never been written: no timestamp
    assert tracker.times[0] > 0
    assert tracker.times[5] == 0.0
    # a blank row keeping an old stamp must be refreshed when output
    # flows through it (e.g. the empty line a prompt prints first)
    tracker.times[2] = 1.0
    vte.feed(b"\r\n")  # cursor enters the still-blank row 2
    pump_gtk()
    tracker._update()
    assert tracker.times[2] > 1.0
    tracker.close()
    window.destroy()


def test_draw_overlay_renders_without_error():
    import cairo
    window, vte = make_terminal(["line one", "line two"])
    tracker = make_tracker(vte)
    tracker.set_enabled(True)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 800, 400)
    context = cairo.Context(surface)
    assert tracker._on_vte_draw(vte, context) is False
    tracker.close()
    window.destroy()


def test_width_change_preserves_timestamps():
    # lines long enough to rewrap when the terminal gets narrower
    lines = [("x%02d" % i).ljust(50, ".") for i in range(8)]
    window, vte = make_terminal(lines, width=80, height=10)
    tracker = make_tracker(vte)
    snapshot = list(tracker.times)
    cursor_row = vte.get_cursor_position()[1]
    assert all(stamp > 0 for stamp in snapshot[:cursor_row + 1])

    char_width = vte.get_char_width()
    window.resize(30 * char_width, 400)  # narrow: lines wrap, buffer grows
    pump_gtk()
    tracker._update()
    # history is read-only: no row may be stamped later than before
    assert len(tracker.times) == int(vte.get_vadjustment().get_upper())
    assert any(stamp > 0 for stamp in tracker.times)
    assert max(tracker.times) <= max(snapshot)

    window.resize(120 * char_width, 400)  # widen: lines unwrap
    pump_gtk()
    tracker._update()
    assert len(tracker.times) == int(vte.get_vadjustment().get_upper())
    assert any(stamp > 0 for stamp in tracker.times)
    assert max(tracker.times) <= max(snapshot)
    tracker.close()
    window.destroy()


def test_width_change_realigns_anchors():
    """Lines unchanged by a rewrap must recover their exact times"""
    window, vte = make_terminal(["aaa", "b" * 60, "ccc"],
                                width=80, height=10)
    tracker = make_tracker(vte)
    tracker.times[0] = 111.0
    tracker.times[1] = 222.0
    tracker.times[2] = 333.0
    window.resize(30 * vte.get_char_width(), 400)  # the 60-char line wraps
    pump_gtk()
    tracker._update()
    upper = int(vte.get_vadjustment().get_upper())
    ccc_row = next(r for r in range(upper)
                   if tracker._row_text(r) == "ccc")
    assert tracker.times[0] == 111.0
    # the wrapped halves belong to the "b" line's logical line
    for row in range(1, ccc_row):
        assert tracker.times[row] == 222.0
    assert tracker.times[ccc_row] == 333.0
    tracker.close()
    window.destroy()

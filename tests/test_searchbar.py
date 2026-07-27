import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gtk, Vte, GLib

from terminatorlib.searchbar import Searchbar, text_column_map

if not hasattr(Vte.Terminal, "get_text_range_format"):
    pytest.skip("VTE too old for the custom search implementation",
                allow_module_level=True)

# rows used by most tests: two matches on row 0, none on row 1, one on row 2
THREE_ROW_LINES = [
    "foo needle bar needle baz",
    "no match here",
    "needle at row two",
]
# expected match spans (start_col, end_col) per row for "needle"
THREE_ROW_MATCHES = [(0, 4, 10), (0, 15, 21), (2, 0, 6)]


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


def make_searchbar(vte):
    searchbar = Searchbar()
    searchbar.vte = vte
    # force a deterministic state regardless of the user's config,
    # without triggering the toggled handlers (they would save config)
    searchbar.match_case.handler_block_by_func(searchbar.match_case_toggled)
    searchbar.match_case.set_active(False)
    searchbar.invert_search.handler_block_by_func(searchbar.wrap_invert_search)
    searchbar.invert_search.set_active(False)
    searchbar.search_is_inverted = False
    return searchbar


def search(searchbar, text):
    searchbar.entry.set_text(text)
    searchbar.do_search(searchbar.entry)


def collect_forward(searchbar, limit=20):
    """Collect the current match positions visited by next_search"""
    positions = [searchbar._current]
    for _ in range(limit):
        searchbar.next_search(None)
        if searchbar._current == positions[0]:
            break
        positions.append(searchbar._current)
    return positions


def test_text_column_map_ascii():
    assert text_column_map("abcd") == [0, 1, 2, 3, 4]


def test_text_column_map_cjk():
    # each CJK char occupies two terminal cells
    assert text_column_map("中文ab") == [0, 2, 4, 5, 6]


def test_text_column_map_tab():
    assert text_column_map("a\tb") == [0, 1, 8, 9]


def test_live_search_runs_without_enter():
    """Typing in the entry searches immediately, no Enter needed"""
    window, vte = make_terminal(["foo needle bar needle baz"])
    searchbar = make_searchbar(vte)
    searchbar.entry.set_text("needle")
    assert searchbar.searchre is not None
    assert searchbar._row_spans(0) == [(4, 10), (15, 21)]
    assert searchbar._current == (0, 4, 10)
    window.destroy()


def test_empty_text_clears_search():
    window, vte = make_terminal(["foo needle bar"])
    searchbar = make_searchbar(vte)
    search(searchbar, "needle")
    assert searchbar._matches
    searchbar.entry.set_text("")
    assert searchbar.searchre is None
    assert searchbar._current is None
    assert searchbar._matches == {}
    assert not searchbar.next.get_sensitive()
    assert not searchbar.prev.get_sensitive()
    window.destroy()


def test_enter_jumps_to_next_match():
    window, vte = make_terminal(THREE_ROW_LINES)
    searchbar = make_searchbar(vte)
    searchbar.entry.set_text("needle")
    assert searchbar._current == THREE_ROW_MATCHES[0]
    searchbar.entry.emit("activate")
    assert searchbar._current == THREE_ROW_MATCHES[1]
    searchbar.entry.emit("activate")
    assert searchbar._current == THREE_ROW_MATCHES[2]
    # keeps going and wraps around
    searchbar.entry.emit("activate")
    assert searchbar._current == THREE_ROW_MATCHES[0]
    window.destroy()


def test_multiple_matches_per_row_all_found():
    window, vte = make_terminal(["foo needle bar needle baz"])
    searchbar = make_searchbar(vte)
    search(searchbar, "needle")
    assert searchbar._row_spans(0) == [(4, 10), (15, 21)]
    window.destroy()


def test_cjk_match_spans_full_characters():
    window, vte = make_terminal(["中文abc中文"])
    searchbar = make_searchbar(vte)
    search(searchbar, "中文")
    # '中文' = 4 cells; matches at cells 0-4 and 7-11
    assert searchbar._row_spans(0) == [(0, 4), (7, 11)]
    window.destroy()


def test_case_insensitive_by_default():
    window, vte = make_terminal(["foo Needle bar"])
    searchbar = make_searchbar(vte)
    search(searchbar, "needle")
    assert searchbar._row_spans(0) == [(4, 10)]
    window.destroy()


def test_next_visits_second_match_on_same_row():
    """Regression test: VTE's own search only finds the first match per row"""
    window, vte = make_terminal(THREE_ROW_LINES)
    searchbar = make_searchbar(vte)
    search(searchbar, "needle")

    assert searchbar._current == THREE_ROW_MATCHES[0]
    positions = collect_forward(searchbar)
    assert positions == THREE_ROW_MATCHES
    window.destroy()


def test_prev_goes_backwards():
    window, vte = make_terminal(THREE_ROW_LINES)
    searchbar = make_searchbar(vte)
    search(searchbar, "needle")
    searchbar.next_search(None)
    searchbar.next_search(None)
    assert searchbar._current == THREE_ROW_MATCHES[2]
    searchbar.prev_search(None)
    assert searchbar._current == THREE_ROW_MATCHES[1]
    searchbar.prev_search(None)
    assert searchbar._current == THREE_ROW_MATCHES[0]
    window.destroy()


def test_search_scrolls_to_match_outside_viewport():
    window, vte = make_terminal(
        ["needle on first row"] + ["filler %02d" % i for i in range(30)],
        height=10,
    )
    searchbar = make_searchbar(vte)
    # viewport is at the bottom, the only match is on row 0
    search(searchbar, "needle")
    assert searchbar._current is not None
    assert searchbar._current[0] == 0
    top = vte.get_vadjustment().get_value()
    page = vte.get_vadjustment().get_page_size()
    assert top <= 0 < top + page
    window.destroy()


def test_invalid_regex_marks_entry_and_disables_buttons():
    window, vte = make_terminal(["some text"])
    searchbar = make_searchbar(vte)
    search(searchbar, "[")
    assert searchbar.searchre is None
    assert searchbar.entry.get_style_context().has_class("error")
    assert not searchbar.next.get_sensitive()
    assert not searchbar.prev.get_sensitive()
    window.destroy()


def test_end_search_clears_highlight_state():
    window, vte = make_terminal(["foo needle bar needle baz"])
    searchbar = make_searchbar(vte)
    search(searchbar, "needle")
    assert searchbar._matches
    assert searchbar._draw_handler is not None
    searchbar.end_search()
    assert searchbar.searchre is None
    assert searchbar._current is None
    assert searchbar._matches == {}
    assert searchbar._draw_handler is None
    assert searchbar._contents_handler is None
    assert searchbar._scroll_handler is None
    window.destroy()

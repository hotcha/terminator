# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""searchbar.py - classes necessary to provide a terminal search bar"""

import re
import unicodedata

import gi
from gi.repository import Gtk, Gdk
gi.require_version('Vte', '2.91')  # vte-0.38 (gnome-3.14)
from gi.repository import Vte
from gi.repository import GObject
from gi.repository import GLib

from .translation import _
from .config import Config
from . import regex
from .util import dbg

# highlight colors for search matches (r, g, b, a)
MATCH_COLOR = (0.98, 0.81, 0.16, 0.35)
CURRENT_MATCH_COLOR = (1.0, 0.55, 0.10, 0.60)
# safety cap on highlighted matches per row
MAX_MATCHES_PER_ROW = 1000


def char_width(char, col):
    """Width in terminal cells of a single character at column col"""
    if char == '\t':
        return 8 - (col % 8)
    if unicodedata.combining(char):
        return 0
    if unicodedata.east_asian_width(char) in ('W', 'F'):
        return 2
    return 1


def text_column_map(text):
    """Map string indices in text to terminal column numbers.

    Returns a list of len(text)+1 entries; entry i is the terminal column
    of the cell containing text[i], entry len(text) is the column just
    after the last character.
    """
    columns = [0] * (len(text) + 1)
    col = 0
    for i, char in enumerate(text):
        columns[i] = col
        col += char_width(char, col)
    columns[len(text)] = col
    return columns


# pylint: disable-msg=R0904
class Searchbar(Gtk.HBox):
    """Class implementing the Searchbar widget"""

    __gsignals__ = {
        'end-search': (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    entry = None
    mode_button = None
    _pad_css = None
    count_label = None
    next = None
    prev = None

    # search case mode: 'smart' (iTerm2-style smart case) or 'sensitive'
    search_mode = None

    vte = None
    config = None

    searchstring = None
    searchre = None

    # state for our own search implementation
    _vte_search_supported = None
    _matches = None          # {absolute_row: [(start_col, end_col), ...]}
    _current = None          # (row, start_col, end_col) of the current match
    _draw_handler = None
    _scroll_handler = None   # (adjustment, handler_id)
    _contents_handler = None
    _size_handler = None
    _refresh_pending = False

    def __init__(self):
        """Class initialiser"""
        GObject.GObject.__init__(self)

        self.config = Config()

        self.get_style_context().add_class("terminator-terminal-searchbar")

        # Search text
        self.entry = Gtk.Entry()
        self.entry.set_activates_default(True)
        # fixed width, roughly 42 characters
        self.entry.set_width_chars(42)
        self.entry.show()
        # search happens live while typing; Enter jumps to the next match
        self.entry.connect('changed', self.do_search)
        self.entry.connect('activate', self.on_activate)
        self.entry.connect('key-press-event', self.search_keypress)

        # Match counter overlaid inside the right edge of the entry,
        # like a secondary icon (e.g. "42/100")
        self.count_label = Gtk.Label()
        self.count_label.set_margin_end(6)
        self.count_label.set_halign(Gtk.Align.END)
        self.count_label.set_valign(Gtk.Align.CENTER)
        self.count_label.get_style_context().add_class('dim-label')
        self.count_label.show()

        entry_overlay = Gtk.Overlay()
        entry_overlay.add(self.entry)
        entry_overlay.add_overlay(self.count_label)

        # Search mode selector inside the left edge of the entry: a find
        # icon with a small down arrow, opening a dropdown menu
        self.mode_button = Gtk.Button()
        self.mode_button.set_relief(Gtk.ReliefStyle.NONE)
        self.mode_button.set_focus_on_click(False)
        self.mode_button.set_can_focus(False)
        self.mode_button.set_tooltip_text(_('Search mode'))
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        mode_box.pack_start(
            Gtk.Image.new_from_icon_name('edit-find-symbolic',
                                         Gtk.IconSize.MENU), False, False, 0)
        mode_box.pack_start(
            Gtk.Image.new_from_icon_name('pan-down-symbolic',
                                         Gtk.IconSize.MENU), False, False, 0)
        mode_box.show_all()
        self.mode_button.add(mode_box)
        self.mode_button.set_halign(Gtk.Align.START)
        self.mode_button.set_valign(Gtk.Align.CENTER)
        self.mode_button.connect('clicked', self._on_mode_button_clicked)
        # pad the entry text clear of the button once its width is known
        self.mode_button.connect('size-allocate', self._on_mode_button_size)
        self.mode_button.show()
        entry_overlay.add_overlay(self.mode_button)

        entry_overlay.show()

        # reserve space inside the entry so typed text stays clear
        # of the overlaid match counter (left padding set dynamically)
        self.entry.get_style_context().add_class('terminator-search-count')
        self._pad_css = Gtk.CssProvider()
        self._pad_css.load_from_data(
            b'entry.terminator-search-count {'
            b'  padding-left: 42px; padding-right: 64px; }')
        self.entry.get_style_context().add_provider(
            self._pad_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # Close Button
        close = Gtk.Button()
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_focus_on_click(False)
        icon = Gtk.Image()
        icon.set_from_stock(Gtk.STOCK_CLOSE, Gtk.IconSize.MENU)
        close.add(icon)
        close.set_name('terminator-search-close-button')
        if hasattr(close, 'set_tooltip_text'):
            close.set_tooltip_text(_('Close Search bar'))
        close.connect('clicked', self.end_search)
        close.show_all()

        # Next Button (icon: right arrow, no button frame)
        self.next = Gtk.Button.new_from_icon_name('go-next-symbolic',
                                                  Gtk.IconSize.MENU)
        self.next.set_relief(Gtk.ReliefStyle.NONE)
        self.next.set_tooltip_text(_('Next match'))
        self.next.show()
        self.next.set_sensitive(False)
        self.next.connect('clicked', self.next_search)

        # Previous Button (icon: left arrow, no button frame)
        self.prev = Gtk.Button.new_from_icon_name('go-previous-symbolic',
                                                  Gtk.IconSize.MENU)
        self.prev.set_relief(Gtk.ReliefStyle.NONE)
        self.prev.set_tooltip_text(_('Previous match'))
        # spacing between the entry and the prev/next buttons
        self.prev.set_margin_start(10)
        self.prev.show()
        self.prev.set_sensitive(False)
        self.prev.connect('clicked', self.prev_search)

        # search behaviour: wrap around, case handling selected via the
        # mode dropdown inside the entry (default: smart case)
        self.search_mode = 'smart'
        self.search_is_inverted = bool(self.config.base.get_item('invert_search'))

        # the entry does not expand, so everything packs left,
        # right next to the entry (close stays at the far right);
        # valign=CENTER keeps the widgets at their natural height
        # instead of stretching to the full height of the bar
        widgets = [entry_overlay, self.prev, self.next, close]
        for widget in widgets:
            widget.set_valign(Gtk.Align.CENTER)
        self.pack_start(entry_overlay, False, True, 0)
        self.pack_start(self.prev, False, False, 0)
        self.pack_start(self.next, False, False, 0)
        self.pack_end(close, False, False, 0)

        self.hide()
        self.set_no_show_all(True)

        self._matches = {}
        self._vte_search_supported = hasattr(Vte.Terminal, 'get_text_range_format')
        if not self._vte_search_supported:
            dbg('Vte.Terminal.get_text_range_format unavailable, '
                'falling back to VTE built-in search')

    def get_vte(self):
        """Find our parent widget"""
        parent = self.get_parent()
        if parent:
            self.vte = parent.vte
            #turn on wrap by default
            self.vte.search_set_wrap_around(True)

    # pylint: disable-msg=W0613
    def search_keypress(self, widget, event):
        """Handle keypress events"""
        key = Gdk.keyval_name(event.keyval)
        if key == 'Escape':
            self.end_search()
        elif (event.state & Gdk.ModifierType.SHIFT_MASK)\
                and (event.keyval == Gdk.KEY_Return or event.keyval == Gdk.KEY_KP_Enter):
            if self.search_is_inverted:
                self.next_search(None)
            else:
                self.prev_search(None)
            return True

    def start_search(self):
        """Show ourselves"""
        if not self.vte:
            self.get_vte()

        self.show()
        self.entry.grab_focus()
        # re-highlight a previous search, if any
        if self._custom_active():
            self._attach_highlight()
            self._refresh_matches()
            self._update_count_label()

    def _on_mode_button_size(self, button, _alloc):
        """Pad the entry text clear of the overlaid mode selector"""
        width = button.get_allocated_width() + 4
        self._pad_css.load_from_data(
            ('entry.terminator-search-count {'
             '  padding-left: %dpx; padding-right: 64px; }' % width).encode())

    def _on_mode_button_clicked(self, button):
        """Pop up the search mode menu below the entry"""
        menu = Gtk.Menu()
        group = None
        for mode, label in (('smart', _('Smart Case Substring')),
                            ('sensitive', _('Case-Sensitive Substring'))):
            item = Gtk.RadioMenuItem.new_with_label(group, label)
            if group is None:
                group = item.get_group()
            item.set_active(self.search_mode == mode)
            item.connect('toggled', self._on_mode_toggled, mode)
            item.show()
            menu.append(item)
        menu.attach_to_widget(button, None)
        menu.popup_at_widget(button, Gdk.Gravity.SOUTH_WEST,
                             Gdk.Gravity.NORTH_WEST, None)

    def _on_mode_toggled(self, item, mode):
        """A search mode menu item was selected"""
        if not item.get_active() or mode == self.search_mode:
            return
        self.search_mode = mode
        dbg('search mode: %s' % mode)
        # re-run the search with the new case handling
        if self.entry.get_text() != '':
            self.do_search(self.entry)

    def do_search(self, widget):
        """Search for the current entry text; runs live on every change"""
        dbg('entered do_search')
        searchtext = self.entry.get_text()
        dbg('searchtext: %s' % searchtext)
        if searchtext == '':
            self._clear_search()
            return

        if self._vte_search_supported:
            self._do_search_custom(searchtext)
        else:
            self._do_search_vte(searchtext)

    def on_activate(self, _widget):
        """Enter means "jump to the next match" (Shift+Enter = previous)"""
        if self.entry.get_text() == '':
            return
        if self.search_is_inverted:
            self.prev_search(None)
        else:
            self.next_search(None)

    def _clear_search(self):
        """Reset all search state, e.g. when the entry is emptied"""
        self.searchstring = None
        self.searchre = None
        self._current = None
        self._invalidate_matches()
        self.count_label.set_text('')
        self.entry.get_style_context().remove_class("error")
        self.next.set_sensitive(False)
        self.prev.set_sensitive(False)
        if self.vte:
            self._clear_vte_search()
            self.vte.queue_draw()

    def _custom_active(self):
        """True when our own search implementation is currently in use"""
        return self._vte_search_supported and isinstance(self.searchre, re.Pattern)

    def _is_case_sensitive(self, searchtext):
        """Effective case sensitivity for the given search text.

        'sensitive' mode is always case-sensitive; 'smart' mode is
        case-insensitive unless the text contains an uppercase letter
        (iTerm2-style smart case).
        """
        if self.search_mode == 'sensitive':
            return True
        return any(char.isupper() for char in searchtext)

    def _search_compile(self, searchtext):
        """Compile searchtext into a Python regex, applying the case mode"""
        flags = 0
        if not self._is_case_sensitive(searchtext):
            flags |= re.IGNORECASE
        return re.compile(searchtext, flags)

    def _do_search_custom(self, searchtext):
        """Search with our own matcher and highlight overlay"""
        try:
            self.searchre = self._search_compile(searchtext)
        except re.error as error:
            dbg('regex error: %s' % error)
            self.searchre = None
            self.entry.get_style_context().add_class("error")
            self.count_label.set_text('')
            self.next.set_sensitive(False)
            self.prev.set_sensitive(False)
            return

        dbg('search RE: %s' % self.searchre)
        self.entry.get_style_context().remove_class("error")

        # we draw the highlights ourselves; disable VTE's built-in search
        self._clear_vte_search()
        self._attach_highlight()

        self.next.set_sensitive(True)
        self.prev.set_sensitive(True)

        self._invalidate_matches()
        self._refresh_matches()
        # jump to the first match from the current viewport
        adj = self.vte.get_vadjustment()
        if not self.search_is_inverted:
            start = (int(adj.get_value()), -1)
            found = self._find_match(start[0], start[1], True, True)
        else:
            start = (int(adj.get_value() + adj.get_page_size()) - 1, 1 << 30)
            found = self._find_match(start[0], start[1], False, True)
        if found:
            self._set_current(found)
        else:
            self._current = None
            self.vte.queue_draw()
        self._update_count_label()

    def _do_search_vte(self, searchtext):
        """Legacy search using VTE's built-in search (old VTE versions)"""
        self.searchre = None
        regex_error = False

        # apply the case mode to the regex flags
        flags_pcre2 = regex.FLAGS_PCRE2
        flags_glib = regex.FLAGS_GLIB
        if not self._is_case_sensitive(searchtext):
            if flags_pcre2:
                flags_pcre2 |= regex.PCRE2_CASELESS
            flags_glib |= regex.GLIB_CASELESS

        if flags_pcre2:
            try:
                self.searchre = Vte.Regex.new_for_search(searchtext,
                                                         len(searchtext.encode('utf-8')),
                                                         flags_pcre2)
                dbg('search RE: %s' % self.searchre)
                self.vte.search_set_regex(self.searchre, 0)
            except GLib.Error as error:
                # happens when PCRE2 support is not builtin (Ubuntu < 19.10)
                # or when the regex pattern is invalid
                dbg('PCRE2 regex error: %s' % error)
                pass

        if not self.searchre:
            # fall back to old GLib regex
            try:
                self.searchre = GLib.Regex(searchtext, flags_glib, 0)
                dbg('search RE: %s' % self.searchre)
                self.vte.search_set_gregex(self.searchre, 0)
            except GLib.Error as error:
                # Invalid regex pattern - handle gracefully
                dbg('GLib regex error: %s' % error)
                regex_error = True
                # Set entry background to indicate error
                self.entry.get_style_context().add_class("error")

        if regex_error:
            # Don't enable search buttons for invalid regex
            self.next.set_sensitive(False)
            self.prev.set_sensitive(False)
            return

        # Clear any error styling on successful regex compilation
        self.entry.get_style_context().remove_class("error")

        self.next.set_sensitive(True)
        self.prev.set_sensitive(True)
        # switch search direction based on the configured inversion
        if not self.search_is_inverted:
            self.next_search(None)
        else:
            self.prev_search(None)

    def next_search(self, widget):
        """Search forwards and jump to the next result, if any"""
        if self._custom_active():
            self._advance(True)
        else:
            self.vte.search_find_next()
        # wrap is always on, so both directions stay available
        self.next.set_sensitive(True)
        self.prev.set_sensitive(True)
        return

    def prev_search(self, widget):
        """Jump back to the previous search"""
        if self._custom_active():
            self._advance(False)
        else:
            self.vte.search_find_previous()
        self.prev.set_sensitive(True)
        self.next.set_sensitive(True)
        return

    def _advance(self, forward):
        """Move the current match one step in the given direction"""
        if self._current is not None:
            row, start_col = self._current[0], self._current[1]
        else:
            # no current match: start from the edge of the viewport
            adj = self.vte.get_vadjustment()
            if forward:
                row, start_col = int(adj.get_value()), -1
            else:
                row = int(adj.get_value() + adj.get_page_size()) - 1
                start_col = 1 << 30
        found = self._find_match(row, start_col, forward, True)
        if found:
            self._set_current(found)
        return bool(found)

    def _find_match(self, row, col, forward, allow_wrap):
        """Find the next match after (row, col) scanning lazily row by row.

        Returns (row, start_col, end_col) or None. With allow_wrap, the scan
        continues from the other end of the buffer when it runs off an edge.
        """
        adj = self.vte.get_vadjustment()
        total = int(adj.get_upper())
        if total <= 0:
            return None

        if forward:
            first_pass = range(row, total)
            second_pass = range(0, row + 1) if allow_wrap else ()
        else:
            first_pass = range(row, -1, -1)
            second_pass = range(total - 1, row - 1, -1) if allow_wrap else ()

        for pass_rows, same_row in ((first_pass, True), (second_pass, False)):
            for current_row in pass_rows:
                spans = self._row_spans(current_row)
                if not spans:
                    continue
                if same_row and current_row == row:
                    if forward:
                        candidates = [s for s in spans if s[0] > col]
                    else:
                        candidates = [s for s in spans if s[0] < col]
                    if not candidates:
                        continue
                    span = candidates[0] if forward else candidates[-1]
                else:
                    span = spans[0] if forward else spans[-1]
                return (current_row, span[0], span[1])
        return None

    def _set_current(self, match):
        """Set the current match and scroll it into view if needed"""
        self._current = match
        adj = self.vte.get_vadjustment()
        page = adj.get_page_size()
        row = match[0]
        if not (adj.get_value() <= row < adj.get_value() + page):
            target = row - page // 2
            target = max(adj.get_lower(), min(target, adj.get_upper() - page))
            adj.set_value(target)
        self._update_count_label()
        self.vte.queue_draw()

    def _row_spans(self, row):
        """Match spans [(start_col, end_col), ...] for an absolute row"""
        if row in self._matches:
            return self._matches[row]
        spans = []
        if self._custom_active() and self.vte is not None:
            cols = self.vte.get_column_count()
            try:
                result = self.vte.get_text_range_format('text', row, 0, row, cols)
                text = result[0] if result else None
            except Exception as error:  # pylint: disable=broad-except
                dbg('get_text_range_format failed for row %d: %s' % (row, error))
                text = None
            if text:
                columns = text_column_map(text)
                for match in self.searchre.finditer(text):
                    if match.end() == match.start():
                        continue
                    spans.append((columns[match.start()], columns[match.end()]))
                    if len(spans) >= MAX_MATCHES_PER_ROW:
                        break
        # only cache rows with matches; empty rows are cheap to recompute
        # and caching them all could grow unbounded on huge scrollback
        if spans:
            self._matches[row] = spans
        return spans

    def _invalidate_matches(self):
        """Drop all cached per-row matches"""
        self._matches = {}

    def _update_count_label(self):
        """Update the 'current/total' match counter next to the entry"""
        if not self._custom_active() or not self.vte:
            self.count_label.set_text('')
            return
        total_rows = int(self.vte.get_vadjustment().get_upper())
        total = 0
        before = 0
        position = 0
        for row in range(total_rows):
            spans = self._row_spans(row)
            total += len(spans)
            if self._current is not None:
                if row < self._current[0]:
                    before += len(spans)
                elif row == self._current[0] and position == 0:
                    for i, span in enumerate(spans):
                        if span[0] == self._current[1] and span[1] == self._current[2]:
                            position = before + i + 1
                            break
        if total == 0:
            self.count_label.set_text('')
        else:
            self.count_label.set_text('%d/%d' % (position, total))

    def _refresh_matches(self):
        """Recompute matches for the visible rows and redraw"""
        if not self.vte or not self._custom_active():
            return
        adj = self.vte.get_vadjustment()
        top = int(adj.get_value())
        bottom = int(adj.get_value() + adj.get_page_size())
        for row in range(max(0, top), bottom):
            self._row_spans(row)
        # drop the current match if it no longer exists
        if self._current is not None:
            row, start_col, end_col = self._current
            if (start_col, end_col) not in self._row_spans(row):
                self._current = None
        self.vte.queue_draw()

    def _on_contents_changed(self, _vte):
        """Terminal content changed; recompute highlights, debounced"""
        if self._refresh_pending:
            return
        self._refresh_pending = True
        GLib.idle_add(self._on_idle_refresh)

    def _on_idle_refresh(self):
        self._refresh_pending = False
        self._invalidate_matches()
        self._refresh_matches()
        self._update_count_label()
        return GLib.SOURCE_REMOVE

    def _on_scroll(self, *_args):
        self._refresh_matches()

    def _on_size_allocate(self, *_args):
        self._invalidate_matches()
        self._refresh_matches()
        self._update_count_label()

    def _attach_highlight(self):
        """Connect the signals driving our highlight overlay"""
        if not self._vte_search_supported or not self.vte:
            return
        if self._draw_handler is None:
            self._draw_handler = self.vte.connect_after('draw', self._on_vte_draw)
        if self._contents_handler is None:
            self._contents_handler = self.vte.connect('contents-changed',
                                                      self._on_contents_changed)
        if self._size_handler is None:
            self._size_handler = self.vte.connect('size-allocate',
                                                  self._on_size_allocate)
        adj = self.vte.get_vadjustment()
        if self._scroll_handler is None or self._scroll_handler[0] is not adj:
            self._detach_scroll_handler()
            self._scroll_handler = (adj, adj.connect('value-changed', self._on_scroll))

    def _detach_scroll_handler(self):
        if self._scroll_handler is not None:
            adj, handler_id = self._scroll_handler
            adj.disconnect(handler_id)
            self._scroll_handler = None

    def _detach_highlight(self):
        """Disconnect all signals used by our highlight overlay"""
        if not self.vte:
            return
        if self._draw_handler is not None:
            self.vte.disconnect(self._draw_handler)
            self._draw_handler = None
        if self._contents_handler is not None:
            self.vte.disconnect(self._contents_handler)
            self._contents_handler = None
        if self._size_handler is not None:
            self.vte.disconnect(self._size_handler)
            self._size_handler = None
        self._detach_scroll_handler()

    def _clear_vte_search(self):
        """Disable VTE's own search highlighting"""
        if not self.vte:
            return
        try:
            self.vte.search_set_regex(None, 0)
        except (TypeError, GLib.Error):
            pass
        if hasattr(self.vte, 'search_set_gregex'):
            try:
                self.vte.search_set_gregex(None, 0)
            except (TypeError, GLib.Error):
                pass

    def _on_vte_draw(self, _vte, context):
        """Draw highlight rectangles over all visible matches"""
        if not self._custom_active() or not self._matches:
            return False
        vte = self.vte
        adj = vte.get_vadjustment()
        top = adj.get_value()
        char_width_px = vte.get_char_width()
        char_height_px = vte.get_char_height()
        if char_width_px <= 0 or char_height_px <= 0:
            return False
        allocation = vte.get_allocation()
        pad_x = (allocation.width - vte.get_column_count() * char_width_px) / 2
        pad_y = (allocation.height - vte.get_row_count() * char_height_px) / 2

        for row, spans in self._matches.items():
            y = pad_y + (row - top) * char_height_px
            if y + char_height_px < 0 or y > allocation.height:
                continue
            for start_col, end_col in spans:
                x = pad_x + start_col * char_width_px
                width = (end_col - start_col) * char_width_px
                if self._current is not None and self._current[0] == row \
                        and self._current[1] == start_col and self._current[2] == end_col:
                    color = CURRENT_MATCH_COLOR
                else:
                    color = MATCH_COLOR
                context.set_source_rgba(*color)
                context.rectangle(x, y, width, char_height_px)
                context.fill()
        return False

    def end_search(self, widget=None):
        """Trap and re-emit the end-search signal"""
        self._clear_search()
        self._detach_highlight()
        self.emit('end-search')

    def get_search_term(self):
        """Return the currently set search term"""
        return(self.entry.get_text())

GObject.type_register(Searchbar)

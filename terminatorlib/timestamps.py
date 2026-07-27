# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""timestamps.py - per-line last-modified timestamps for a terminal

Tracks the last time each line of the terminal buffer (scrollback plus
visible screen) was written, and can draw those times along the right
edge of the VTE widget, iTerm2-style. Tracking runs continuously once
the terminal exists; the menu toggle only controls visibility.

The index into the hash/time arrays is the VTE absolute row (row 0 is
the oldest line currently in the buffer), which VTE keeps stable for a
given line until the line is dropped from a full scrollback ring.
"""

import time
import zlib
from array import array

import gi
gi.require_version('Vte', '2.91')  # vte-0.38 (gnome-3.14)
gi.require_version('PangoCairo', '1.0')
from gi.repository import Gtk, GLib, GObject, Pango, PangoCairo, Vte

from .util import dbg, err

# sentinel for "row never scanned" in the hash array; crc32 colliding
# with it is remapped to 0
HASH_UNKNOWN = 0xFFFFFFFF

# debounce interval for content updates, in milliseconds
UPDATE_INTERVAL_MS = 100

# cursor span above which rows are time-stamped without being hashed:
# lines produced by a huge burst were all just written, so their time
# alone is accurate enough and hashing thousands of rows is wasted work
HASH_SCAN_LIMIT = 4096

# how many old rows a single new row may consume when realigning after
# a rewrap (a line merged from many wrapped rows)
RESIZE_LOOKAHEAD = 32


class Timestamps(object):
    """Per-terminal tracker for line modification times"""

    terminal = None
    vte = None
    enabled = False

    hashes = None
    times = None
    prev_raw_cursor = None
    cursor_offset = 0
    prev_cols = None
    pending_drops = 0
    seen_scrollback = False
    suspended = False

    dead = False
    update_source = None
    connections = None

    def __init__(self, terminal):
        self.terminal = terminal
        self.vte = terminal.vte
        self.hashes = array('I')
        self.times = array('d')
        self.connections = []
        self._connect(self.vte, 'contents-changed', self._schedule_update)
        self._connect(self.vte, 'size-allocate', self._schedule_update)
        self._connect(self.vte.get_vadjustment(), 'value-changed',
                      self._schedule_update)
        if GObject.signal_lookup('text-scrolled', Vte.Terminal):
            self._connect(self.vte, 'text-scrolled', self._on_text_scrolled)
        self._connect_after(self.vte, 'draw', self._on_vte_draw)

    def _connect(self, obj, signal, handler):
        self.connections.append((obj, obj.connect(signal, handler)))

    def _connect_after(self, obj, signal, handler):
        self.connections.append((obj, obj.connect_after(signal, handler)))

    def close(self):
        """Detach from the terminal; called from Terminal.close()"""
        self.dead = True
        if self.update_source is not None:
            GLib.source_remove(self.update_source)
            self.update_source = None
        for obj, handler_id in self.connections:
            obj.disconnect(handler_id)
        self.connections = []
        self.vte = None
        self.terminal = None

    def set_enabled(self, enabled):
        """Toggle visibility of the timestamp overlay"""
        self.enabled = enabled
        if self.vte:
            self.vte.queue_draw()

    def _row_text(self, row):
        """Text of a single absolute row"""
        cols = self.vte.get_column_count()
        try:
            result = self.vte.get_text_range_format('text', row, 0, row, cols)
        except Exception as error:  # pylint: disable=broad-except
            dbg('get_text_range_format failed for row %d: %s' % (row, error))
            return ''
        return result[0] if result and result[0] else ''

    @staticmethod
    def _hash(text):
        """Content hash of a row, never equal to HASH_UNKNOWN"""
        value = zlib.crc32(text.encode('utf-8'))
        return 0 if value == HASH_UNKNOWN else value

    def _schedule_update(self, *_args):
        """Debounced entry point for content/scroll/resize signals"""
        if self.dead or self.update_source is not None:
            return
        self.update_source = GLib.timeout_add(UPDATE_INTERVAL_MS,
                                              self._on_update)

    def _on_update(self):
        self.update_source = None
        if self.dead:
            return GLib.SOURCE_REMOVE
        try:
            self._update()
        except Exception as error:  # pylint: disable=broad-except
            err('Timestamps update failed: %s' % error)
        return GLib.SOURCE_REMOVE

    def _on_text_scrolled(self, _vte, delta):
        """Count lines dropped from a full scrollback ring"""
        if delta <= 0 or self.terminal.config['scrollback_infinite']:
            return
        max_lines = self.terminal.config['scrollback_lines']
        adj = self.vte.get_vadjustment()
        if int(adj.get_upper()) - self.vte.get_row_count() >= max_lines:
            self.pending_drops += delta

    def _scan_row(self, row, now, cursor_row):
        """Stamp a row if its content changed since the last scan.

        Rows never scanned before only get a time when they are at or
        above the cursor; rows below it are blank screen rows that have
        never been written and stay untimed until the cursor enters them.
        """
        value = self._hash(self._row_text(row))
        if self.hashes[row] == HASH_UNKNOWN:
            self.hashes[row] = value
            if self.times[row] == 0.0 and row <= cursor_row:
                self.times[row] = now
        elif self.hashes[row] != value:
            self.hashes[row] = value
            self.times[row] = now

    def _realign_on_resize(self, upper, adj):
        """Rebuild the arrays after a rewrap, keeping times stable.

        Rows unchanged by the rewrap are matched against the old hashes
        (two-pointer walk with a bounded lookahead for merged rows) and
        recover their exact original time. Rows a wrap point moved
        through inherit the time of the logical line they belong to.
        Only the visible area plus a margin is realigned; outside it the
        positional times are kept and the hashes rebuild lazily.
        """
        old_hashes = self.hashes
        old_times = self.times
        old_len = len(old_times)
        new_hashes = array('I', [HASH_UNKNOWN]) * upper
        new_times = array('d', [0.0]) * upper
        limit = min(old_len, upper)
        new_times[:limit] = old_times[:limit]

        rows = self.vte.get_row_count()
        top = max(0, int(adj.get_value()))
        bottom = min(top + rows, upper)
        i = max(0, top - 4 * rows)
        end = min(upper, bottom + 4 * rows)
        previous = old_times[i] if i < old_len else 0.0
        for row in range(i, end):
            value = self._hash(self._row_text(row))
            new_hashes[row] = value
            match = None
            lookahead = min(RESIZE_LOOKAHEAD, old_len - i)
            for offset in range(lookahead):
                if old_hashes[i + offset] == value:
                    match = i + offset
                    break
            if match is not None:
                stamp = old_times[match]
                i = match + 1
            elif i < old_len:
                # inside a wrap/merge region: attribute the row to the
                # next old line that has not been consumed yet
                stamp = old_times[i]
            else:
                stamp = previous
            previous = stamp
            new_times[row] = stamp
        self.hashes = new_hashes
        self.times = new_times

    def _update(self):
        """Reconcile the arrays with the buffer and stamp changed rows"""
        vte = self.vte
        adj = vte.get_vadjustment()
        rows = vte.get_row_count()
        cols = vte.get_column_count()
        upper = int(adj.get_upper())
        if rows <= 0:
            return

        if self.pending_drops:
            drops = min(self.pending_drops, len(self.hashes))
            del self.hashes[:drops]
            del self.times[:drops]
            # dropped rows shift the arrays but not VTE's raw cursor
            # numbering, so the offset between the two grows
            self.cursor_offset += drops
            self.pending_drops = 0

        if self.prev_cols is not None and cols != self.prev_cols:
            # Width change: VTE rewraps the text, moving content between
            # rows and changing the total line count. Realign against the
            # old hashes so lines keep their original times; recorded
            # times are history and are never re-stamped here.
            self._realign_on_resize(upper, adj)
            self.prev_raw_cursor = vte.get_cursor_position()[1]
            self.pending_drops = 0
        self.prev_cols = cols

        # Alternate-screen heuristic: the alternate screen has no
        # scrollback, so upper collapses to the row count. Only trust
        # this once scrollback has existed, otherwise a fresh terminal
        # would look suspended until its screen first fills up. While
        # suspended the arrays are left untouched, so the normal buffer
        # keeps its original timestamps when the app exits.
        if upper <= rows:
            if self.seen_scrollback:
                self.suspended = True
                return
        else:
            self.seen_scrollback = True
            if self.suspended:
                # the raw cursor moved while the alternate screen was
                # active; resync without stamping anything
                self.prev_raw_cursor = vte.get_cursor_position()[1]
            self.suspended = False

        if upper < len(self.hashes):
            # The buffer shrank outside the alternate screen: reset/clear
            # wiped the scrollback, start over. Everything now in the
            # buffer is fresh, so stamp it all. Note the raw cursor row
            # is NOT rebased by a reset, so re-anchor its offset too.
            raw = vte.get_cursor_position()[1]
            self.cursor_offset = raw - (upper - 1)
            self.prev_raw_cursor = raw
            self.hashes = array('I', [HASH_UNKNOWN]) * upper
            stamp = time.time()
            self.times = array('d', [stamp]) * upper

        missing = upper - len(self.hashes)
        if missing > 0:
            self.hashes += array('I', [HASH_UNKNOWN]) * missing
            self.times += array('d', [0.0]) * missing

        now = time.time()
        top = max(0, int(adj.get_value()))
        bottom = min(top + rows, upper)

        # A line is written when output reaches it, and output advances
        # the cursor: rows the cursor entered since the last update were
        # just written, so stamp them unconditionally, even when they are
        # still blank (e.g. the empty line a prompt prints before
        # redrawing itself). The raw cursor row is mapped into array
        # space through the maintained offset, and upward moves are
        # redraws/navigation, left to the content scan.
        _col, raw_cursor = vte.get_cursor_position()
        cursor_row = raw_cursor - self.cursor_offset
        if self.prev_raw_cursor is None:
            # first update: rows at or above the cursor were written
            # before tracking started; stamp them now
            self.prev_raw_cursor = raw_cursor
            for row in range(0, min(cursor_row + 1, upper)):
                self.hashes[row] = self._hash(self._row_text(row))
                self.times[row] = now
        else:
            prev_row = self.prev_raw_cursor - self.cursor_offset
            self.prev_raw_cursor = raw_cursor
            if cursor_row > prev_row:
                # in a burst beyond the scan limit the rows flew by too
                # fast to hash individually; they were still all written
                first = max(0, prev_row)
                last = min(cursor_row, upper - 1)
                if cursor_row - prev_row <= HASH_SCAN_LIMIT:
                    for row in range(first + 1, last + 1):
                        self.hashes[row] = self._hash(self._row_text(row))
                        self.times[row] = now
                else:
                    for row in range(first + 1, last + 1):
                        self.times[row] = now

        for row in range(top, bottom):
            self._scan_row(row, now, cursor_row)

    def get_labels(self, top, bottom):
        """Timestamps to show for rows [top, bottom), folded like iTerm2:
        only the first row of a run sharing a timestamp gets a label"""
        labels = {}
        previous = None
        for row in range(top, bottom):
            if row >= len(self.times):
                break
            stamp = self.times[row]
            if stamp == 0.0:
                continue
            label = time.strftime('%H:%M:%S', time.localtime(stamp))
            if label != previous:
                labels[row] = label
            previous = label
        return labels

    def _on_vte_draw(self, _vte, context):
        """Draw timestamp labels along the right edge of the widget"""
        if not self.enabled or self.suspended or not len(self.times):
            return False
        vte = self.vte
        char_height = vte.get_char_height()
        if char_height <= 0:
            return False
        adj = vte.get_vadjustment()
        top = max(0, int(adj.get_value()))
        bottom = min(top + vte.get_row_count(), int(adj.get_upper()))
        labels = self.get_labels(top, bottom)
        if not labels:
            return False

        allocation = vte.get_allocation()
        pad_y = (allocation.height - vte.get_row_count() * char_height) / 2

        layout = PangoCairo.create_layout(context)
        font = vte.get_font()
        desc = font.copy() if font else Pango.FontDescription('Monospace')
        desc.set_absolute_size(max(1, int(char_height * 0.7)) * Pango.SCALE)
        layout.set_font_description(desc)

        # the style context reports black for VTE; use the terminal's
        # configured foreground so labels are visible on any palette
        rgba = self.terminal.fgcolor_active
        if rgba is None:
            rgba = vte.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        context.set_source_rgba(rgba.red, rgba.green, rgba.blue, 0.6)

        # iTerm2-style double guide line centered under the hour digits,
        # drawn in segments that leave a gap at each label row
        layout.set_text('00:00:00', -1)
        sample_width, _height = layout.get_pixel_size()
        label_x = allocation.width - sample_width - 4
        hour_center = label_x + sample_width / 8.0
        context.set_line_width(1)
        segment_start = 0.0
        for row in sorted(labels.keys()):
            row_top = pad_y + (row - top) * char_height
            for line_x in (hour_center - 2, hour_center + 2):
                context.move_to(line_x + 0.5, segment_start)
                context.line_to(line_x + 0.5, row_top)
            segment_start = row_top + char_height
        for line_x in (hour_center - 2, hour_center + 2):
            context.move_to(line_x + 0.5, segment_start)
            context.line_to(line_x + 0.5, allocation.height)
        context.stroke()

        for row, label in labels.items():
            layout.set_text(label, -1)
            _width, height = layout.get_pixel_size()
            y = pad_y + (row - top) * char_height + (char_height - height) / 2
            context.move_to(label_x, y)
            PangoCairo.show_layout(context, layout)
        return False

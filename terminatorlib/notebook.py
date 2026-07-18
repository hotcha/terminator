# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""notebook.py - classes for the notebook widget"""

from functools import cmp_to_key
from gi.repository import GObject
from gi.repository import GLib
from gi.repository import Gtk
from gi.repository import Gdk
from gi.repository import Gio

from .terminator import Terminator
from .config import Config
from .factory import Factory
from .container import Container
from .editablelabel import EditableLabel
from .translation import _
from .util import err, dbg, enumerate_descendants, make_uuid

TAB_CSS_INSTALLED = False

def install_tab_css():
    """Install screen-wide CSS rules needed for tab colouring.

    Some themes (e.g. Fluent) give the box and the close button inside a
    tab negative margins that are calibrated to cancel the theme's own
    paddings. Since we set our own paddings, we must reset the margins
    to the same neutral baseline, otherwise the inherited negative
    margins push the close button and our painted background/frame
    outside the tab's edges. We also reset theme-supplied border-width,
    border-image, and margin so that the colour fills the full tab width
    without gaps on either side, and we reset padding and margin on the
    internal GTK3 GtkBox (the ``box`` child node between ``tab`` and
    ``TabLabel``) so that the ``TabLabel`` widget fills the entire
    content area of the notebook tab."""
    global TAB_CSS_INSTALLED
    if TAB_CSS_INSTALLED:
        return
    css = '''
notebook.terminator-notebook header tab { padding: 0px; margin-left: 0px; margin-right: 0px; border-width: 0px; border-image: none; }
notebook.terminator-notebook header tab box { padding: 0px; margin-left: 0px; margin-right: 0px; }
.terminator-tab-label { margin: 0px; padding: 2px 6px; }
.terminator-tab-label > button { margin: 0px; }
'''
    provider = Gtk.CssProvider()
    provider.load_from_data(css.encode('utf-8'))
    Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 100)
    TAB_CSS_INSTALLED = True

class Notebook(Container, Gtk.Notebook):
    """Class implementing a Gtk.Notebook container"""
    window = None
    last_active_term = None
    pending_on_tab_switch = None
    pending_on_tab_switch_args = None

    def __init__(self, window):
        """Class initialiser"""
        if isinstance(window.get_child(), Gtk.Notebook):
            err('There is already a Notebook at the top of this window')
            raise(ValueError)

        Container.__init__(self)
        GObject.GObject.__init__(self)
        self.terminator = Terminator()
        self.window = window
        GObject.type_register(Notebook)
        self.register_signals(Notebook)
        install_tab_css()
        self.get_style_context().add_class('terminator-notebook')
        self.connect('switch-page', self.deferred_on_tab_switch)
        self.connect('scroll-event', self.on_scroll_event)
        self.connect('create-window', self.create_window_detach)
        self.configure()

        self.set_can_focus(False)

        child = window.get_child()
        window.remove(child)
        window.add(self)
        window_last_active_term = window.last_active_term
        self.newtab(widget=child)
        if window_last_active_term:
            self.set_last_active_term(window_last_active_term)
            window.last_active_term = None

        self.show_all()

    def configure(self):
        """Apply widget-wide settings"""
        # FIXME: The old reordered handler updated Terminator.terminals with
        # the new order of terminals. We probably need to preserve this for
        # navigation to next/prev terminals.
        #self.connect('page-reordered', self.on_page_reordered)
        self.set_scrollable(self.config['scroll_tabbar'])

        if self.config['tab_position'] == 'hidden':
            self.set_show_tabs(False)
        else:
            self.set_show_tabs(True)
            pos = getattr(Gtk.PositionType, self.config['tab_position'].upper())
            self.set_tab_pos(pos)

        for tab in range(0, self.get_n_pages()):
            label = self.get_tab_label(self.get_nth_page(tab))
            label.update_angle()

#        style = Gtk.RcStyle()  # FIXME FOR GTK3 how to do it there? actually do we really want to override the theme?
#        style.xthickness = 0
#        style.ythickness = 0
#        self.modify_style(style)
        self.last_active_term = {}

    def create_window_detach(self, notebook, widget, x, y):
        """Create a window to contain a detached tab"""
        dbg('creating window for detached tab: %s' % widget)
        maker = Factory()

        window = maker.make('Window')
        window.move(x, y)
        size = self.window.get_size()
        window.resize(size.width, size.height)

        self.detach_tab(widget)
        self.disconnect_child(widget)
        self.hoover()
        window.add(widget)

        window.show_all()

    def create_layout(self, layout):
        """Apply layout configuration"""
        def child_compare(a, b):
            order_a = int(children[a]['order'])
            order_b = int(children[b]['order'])

            if (order_a == order_b):
                return 0
            if (order_a < order_b):
                return -1
            if (order_a > order_b):
                return 1

        if 'children' not in layout:
            err('layout specifies no children: %s' % layout)
            return

        children = layout['children']
        if len(children) <= 1:
            #Notebooks should have two or more children
            err('incorrect number of children for Notebook: %s' % layout)
            return

        num = 0
        keys = list(children.keys())
        keys = sorted(keys, key=cmp_to_key(child_compare))

        for child_key in keys:
            child = children[child_key]
            dbg('Making a child of type: %s' % child['type'])
            if child['type'] == 'Terminal':
                pass
            elif child['type'] == 'VPaned':
                page = self.get_nth_page(num)
                self.split_axis(page, True)
            elif child['type'] == 'HPaned':
                page = self.get_nth_page(num)
                self.split_axis(page, False)
            num = num + 1

        num = 0
        for child_key in keys:
            page = self.get_nth_page(num)
            if not page:
                # This page does not yet exist, so make it
                self.newtab(children[child_key])
                page = self.get_nth_page(num)
            if 'labels' in layout:
                labeltext = layout['labels'][num]
                if labeltext and labeltext != "None":
                    label = self.get_tab_label(page)
                    label.set_custom_label(labeltext)
            page.create_layout(children[child_key])

            if  layout.get('last_active_term',  None):
                self.last_active_term[page] = make_uuid(layout['last_active_term'][num])
            num = num + 1

        if 'active_page' in layout:
            # Need to do it later, or layout changes result
            GObject.idle_add(self.set_current_page, int(layout['active_page']))
        else:
            self.set_current_page(0)

    def split_axis(self, widget, vertical=True, cwd=None, sibling=None, widgetfirst=True):
        """Split the axis of a terminal inside us"""
        dbg('called for widget: %s' % widget)
        order = None
        page_num = self.page_num(widget)
        if page_num == -1:
            err('Notebook::split_axis: %s not found in Notebook' % widget)
            return

        label = self.get_tab_label(widget)
        self.remove(widget)

        maker = Factory()
        if vertical:
            container = maker.make('vpaned')
        else:
            container = maker.make('hpaned')

        self.get_toplevel().set_pos_by_ratio = True

        if not sibling:
            sibling = maker.make('terminal')
            sibling.set_cwd(cwd)
            if self.config['always_split_with_profile']:
                sibling.force_set_profile(None, widget.get_profile())
            sibling.spawn_child()
            if widget.group and self.config['split_to_group']:
                sibling.set_group(None, widget.group)
        elif self.config['always_split_with_profile']:
            sibling.force_set_profile(None, widget.get_profile())

        self.insert_page(container, None, page_num)
        self.set_tab_detachable(container, self.config['detachable_tabs'])
        self.child_set_property(container, 'tab-expand', True)
        self.child_set_property(container, 'tab-fill', True)
        self.set_tab_reorderable(container, True)
        self.set_tab_label(container, label)
        self.show_all()

        order = [widget, sibling]
        if widgetfirst is False:
            order.reverse()

        for terminal in order:
            container.add(terminal)
        self.set_current_page(page_num)

        self.show_all()

        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        self.get_toplevel().set_pos_by_ratio = False

        GObject.idle_add(terminal.ensure_visible_and_focussed)

    def add(self, widget, metadata=None):
        """Add a widget to the container"""
        dbg('adding a new tab')
        self.newtab(widget=widget, metadata=metadata)

    def remove(self, widget):
        """Remove a widget from the container"""
        page_num = self.page_num(widget)
        if page_num == -1:
            err('%s not found in Notebook. Actual parent is: %s' %
                    (widget, widget.get_parent()))
            return(False)
        self.remove_page(page_num)
        self.disconnect_child(widget)
        return(True)

    def replace(self, oldwidget, newwidget):
        """Replace a tab's contents with a new widget"""
        page_num = self.page_num(oldwidget)
        self.remove(oldwidget)
        self.add(newwidget)
        self.reorder_child(newwidget, page_num)

    def get_child_metadata(self, widget):
        """Fetch the relevant metadata for a widget which we'd need
        to recreate it when it's re-added"""
        metadata = {}
        metadata['tabnum'] = self.page_num(widget)
        label = self.get_tab_label(widget)
        if not label:
            dbg('unable to find label for widget: %s' % widget)
        else:
            if label.get_custom_label():
                metadata['label'] = label.get_custom_label()
            else:
                dbg('don\'t grab the label as it was not customised')
            if label.tab_color:
                metadata['color'] = label.tab_color
        return metadata

    def get_children(self):
        """Return an ordered list of our children"""
        children = []
        for page in range(0,self.get_n_pages()):
            children.append(self.get_nth_page(page))
        return(children)

    def newtab(self, debugtab=False, widget=None, cwd=None, metadata=None, profile=None):
        """Add a new tab, optionally supplying a child widget"""
        dbg('making a new tab')
        maker = Factory()
        top_window = self.get_toplevel()

        if not widget:
            widget = maker.make('Terminal')
            if cwd:
                widget.set_cwd(cwd)
            if profile and self.config['always_split_with_profile']:
                widget.force_set_profile(None, profile)
            widget.spawn_child(debugserver=debugtab)
        elif profile and self.config['always_split_with_profile']:
            widget.force_set_profile(None, profile)

        signals = {'close-term': self.wrapcloseterm,
                   'split-auto': self.split_auto,
                   'split-horiz': self.split_horiz,
                   'split-vert': self.split_vert,
                   'title-change': self.propagate_title_change,
                   'tab-change': top_window.tab_change,
                   'group-all': top_window.group_all,
                   'group-all-toggle': top_window.group_all_toggle,
                   'ungroup-all': top_window.ungroup_all,
                   'group-win': top_window.group_win,
                   'group-win-toggle': top_window.group_win_toggle,
                   'ungroup-win': top_window.ungroup_win,
                   'group-tab': top_window.group_tab,
                   'group-tab-toggle': top_window.group_tab_toggle,
                   'ungroup-tab': top_window.ungroup_tab,
                   'move-tab': top_window.move_tab,
                   'tab-new': [top_window.tab_new, widget],
                   'navigate': top_window.navigate_terminal,
                   'zoom': top_window.zoom,
                   'maximise': [top_window.zoom, False]}

        if maker.isinstance(widget, 'Terminal'):
            for signal in signals:
                args = []
                handler = signals[signal]
                if isinstance(handler, list):
                    args = handler[1:]
                    handler = handler[0]
                self.connect_child(widget, signal, handler, *args)

        if metadata and 'tabnum' in metadata:
            tabpos = metadata['tabnum']
        elif self.config['new_tab_after_current_tab'] == True:
            tabpos = self.get_current_page() + 1
        else:
            tabpos = -1

        label = TabLabel(self.window.get_title(), self)
        if metadata and 'label' in metadata:
            dbg('creating TabLabel with text: %s' % metadata['label'])
            label.set_custom_label(metadata['label'])
        if metadata and metadata.get('color'):
            label.set_tab_color(metadata['color'])
        label.connect('close-clicked', self.closetab)

        label.show_all()
        widget.show_all()

        dbg('inserting page at position: %s' % tabpos)
        self.insert_page(widget, None, tabpos)
        self.set_tab_detachable(widget, self.config['detachable_tabs'])

        if maker.isinstance(widget, 'Terminal'):
            containers, objects = ([], [widget])
        else:
            containers, objects = enumerate_descendants(widget)

        term_widget = None
        for term_widget in objects:
            if maker.isinstance(term_widget, 'Terminal'):
                self.set_last_active_term(term_widget.uuid)
                break

        self.set_tab_label(widget, label)
        self.child_set_property(widget, 'tab-expand', True)
        self.child_set_property(widget, 'tab-fill', True)

        self.set_tab_reorderable(widget, True)
        self.set_current_page(tabpos)
        self.update_tab_label_states()
        self.show_all()
        if maker.isinstance(term_widget, 'Terminal'):
            widget.grab_focus()

    def wrapcloseterm(self, widget):
        """A child terminal has closed"""
        dbg('called on %s' % widget)
        if self.closeterm(widget):
            dbg('closeterm succeeded')
            self.hoover()
        else:
            dbg('closeterm failed')

    def closetab(self, widget, label):
        """Close a tab"""
        tabnum = None
        try:
            nb = widget.notebook
        except AttributeError:
            err('TabLabel::closetab: called on non-Notebook: %s' % widget)
            return

        for i in range(0, nb.get_n_pages() + 1):
            if label == nb.get_tab_label(nb.get_nth_page(i)):
                tabnum = i
                break

        if tabnum is None:
            err('TabLabel::closetab: %s not in %s. Bailing.' % (label, nb))
            return

        maker = Factory()
        child = nb.get_nth_page(tabnum)

        confirm_close = self.construct_confirm_close(self.window, child)
        if confirm_close != Gtk.ResponseType.ACCEPT:
            dbg('user cancelled request')
            return

        if maker.isinstance(child, 'Terminal'):
            dbg('child is a single Terminal')

            del nb.last_active_term[child]
            child.close()
            # FIXME: We only do this del and return here to avoid removing the
            # page below, which child.close() implicitly does
            del(label)
        elif maker.isinstance(child, 'Container'):
            dbg('child is a Container')

            containers = None
            objects = None
            containers, objects = enumerate_descendants(child)

            while len(objects) > 0:
                descendant = objects.pop()
                descendant.close()
                while Gtk.events_pending():
                    Gtk.main_iteration()
        else:
            err('Notebook::closetab: child is unknown type %s' % child)

    def resizeterm(self, widget, keyname):
        """Handle a keyboard event requesting a terminal resize"""
        raise NotImplementedError('resizeterm')

    def zoom(self, widget, fontscale = False):
        """Zoom a terminal"""
        raise NotImplementedError('zoom')

    def unzoom(self, widget):
        """Unzoom a terminal"""
        raise NotImplementedError('unzoom')

    def find_tab_root(self, widget):
        """Look for the tab child which is or ultimately contains the supplied
        widget"""
        parent = widget.get_parent()
        previous = parent

        while parent is not None and parent is not self:
            previous = parent
            parent = parent.get_parent()

        if previous == self:
            return(widget)
        else:
            return(previous)

    def update_tab_label_text(self, widget, text):
        """Update the text of a tab label"""
        notebook = self.find_tab_root(widget)
        label = self.get_tab_label(notebook)
        if not label:
            err('Notebook::update_tab_label_text: %s not found' % widget)
            return

        label.set_label(text)

    def hoover(self):
        """Clean up any empty tabs and if we only have one tab left, die"""
        numpages = self.get_n_pages()
        while numpages > 0:
            numpages = numpages - 1
            page = self.get_nth_page(numpages)
            if not page:
                dbg('Removing empty page: %d' % numpages)
                self.remove_page(numpages)

        if self.get_n_pages() == 1:
            dbg('Last page, removing self')
            child = self.get_nth_page(0)
            self.remove_page(0)
            parent = self.get_parent()
            parent.remove(self)
            self.cnxids.remove_all()
            parent.add(child)
            del(self)
            # Find the last terminal in the new parent and give it focus
            terms = parent.get_visible_terminals()
            list(terms.keys())[-1].grab_focus()

    def page_num_descendant(self, widget):
        """Find the tabnum of the tab containing a widget at any level"""
        tabnum = self.page_num(widget)
        dbg("widget is direct child if not equal -1 - tabnum: %d" % tabnum)
        while tabnum == -1 and widget.get_parent():
            widget = widget.get_parent()
            tabnum = self.page_num(widget)
        dbg("found tabnum containing widget: %d" % tabnum)
        return tabnum

    def set_last_active_term(self, uuid):
        """Set the last active term for uuid"""
        widget = self.terminator.find_terminal_by_uuid(uuid.urn)
        if not widget:
            err("Cannot find terminal with uuid: %s, so cannot make it active" % (uuid.urn))
            return
        tabnum = self.page_num_descendant(widget)
        if tabnum == -1:
            err("No tabnum found for terminal with uuid: %s" % (uuid.urn))
            return
        nth_page = self.get_nth_page(tabnum)
        self.last_active_term[nth_page] = uuid

    def clean_last_active_term(self):
        """Clean up old entries in last_active_term"""
        if self.terminator.doing_layout == True:
            return
        last_active_term = {}
        for tabnum in range(0, self.get_n_pages()):
            nth_page = self.get_nth_page(tabnum)
            if nth_page in self.last_active_term:
                last_active_term[nth_page] = self.last_active_term[nth_page]
        self.last_active_term = last_active_term

    def deferred_on_tab_switch(self, notebook, page,  page_num,  data=None):
        """Prime a single idle tab switch signal, using the most recent set of params"""
        tabs_last_active_term = self.last_active_term.get(self.get_nth_page(page_num),  None)
        data = {'tabs_last_active_term':tabs_last_active_term}

        self.pending_on_tab_switch_args = (notebook, page,  page_num,  data)
        if self.pending_on_tab_switch == True:
            return
        GObject.idle_add(self.do_deferred_on_tab_switch)
        self.pending_on_tab_switch = True

    def do_deferred_on_tab_switch(self):
        """Perform the latest tab switch signal, and resetting the pending flag"""
        self.on_tab_switch(*self.pending_on_tab_switch_args)
        self.pending_on_tab_switch = False
        self.pending_on_tab_switch_args = None

    def on_tab_switch(self, notebook, page,  page_num,  data=None):
        """Do the real work for a tab switch"""
        tabs_last_active_term = data['tabs_last_active_term']
        if tabs_last_active_term:
            term = self.terminator.find_terminal_by_uuid(tabs_last_active_term.urn)
            # if we can't find a last active term we must be starting up
            if term is not None:
                GObject.idle_add(term.ensure_visible_and_focussed)
        self.update_tab_label_states()
        return True

    def update_tab_label_states(self):
        """Tell each tab label whether its tab is the active one"""
        current = self.get_current_page()
        for tabnum in range(0, self.get_n_pages()):
            label = self.get_tab_label(self.get_nth_page(tabnum))
            if label:
                label.set_tab_active(tabnum == current)

    def on_scroll_event(self, notebook, event):
        '''Handle scroll events for scrolling through tabs'''
        #print "self: %s" % self
        #print "event: %s" % event
        child = self.get_nth_page(self.get_current_page())
        if child == None:
            print("Child = None,  return false")
            return False

        event_widget = Gtk.get_event_widget(event)

        if event_widget == None or \
           event_widget == child or \
           event_widget.is_ancestor(child):
            print("event_widget is wrong one,  return false")
            return False

        # Not sure if we need these. I don't think wehave any action widgets
        # at this point.
        action_widget = self.get_action_widget(Gtk.PackType.START)
        if event_widget == action_widget or \
           (action_widget != None and event_widget.is_ancestor(action_widget)):
            return False
        action_widget = self.get_action_widget(Gtk.PackType.END)
        if event_widget == action_widget or \
           (action_widget != None and event_widget.is_ancestor(action_widget)):
            return False

        if event.direction in [Gdk.ScrollDirection.RIGHT,
                               Gdk.ScrollDirection.DOWN]:
            self.next_page()
        elif event.direction in [Gdk.ScrollDirection.LEFT,
                                 Gdk.ScrollDirection.UP]:
            self.prev_page()
        elif event.direction == Gdk.ScrollDirection.SMOOTH:
            if self.get_tab_pos() in [Gtk.PositionType.LEFT,
                                      Gtk.PositionType.RIGHT]:
                if event.delta_y > 0:
                    self.next_page()
                elif event.delta_y < 0:
                    self.prev_page()
            elif self.get_tab_pos() in [Gtk.PositionType.TOP,
                                        Gtk.PositionType.BOTTOM]:
                if event.delta_x > 0:
                    self.next_page()
                elif event.delta_x < 0:
                    self.prev_page()
        return True

class TabLabel(Gtk.HBox):
    """Class implementing a label widget for Notebook tabs"""
    notebook = None
    terminator = None
    config = None
    label = None
    icon = None
    button = None
    tab_color = None
    tab_active = False
    css_provider = None
    tab_popover = None

    __gsignals__ = {
            'close-clicked': (GObject.SignalFlags.RUN_LAST, None,
                (GObject.TYPE_OBJECT,)),
    }

    def __init__(self, title, notebook):
        """Class initialiser"""
        GObject.GObject.__init__(self)

        self.notebook = notebook
        self.terminator = Terminator()
        self.config = Config()

        self.connect("button-press-event", self.on_button_pressed)
        self.get_style_context().add_class('terminator-tab-label')

        self.label = EditableLabel(title)
        self.update_angle()

        self.pack_start(self.label, True, True, 0)

        self.update_button()
        self.show_all()

    def do_draw(self, cr):
        """Render our CSS background/frame before drawing the children"""
        context = self.get_style_context()
        width = self.get_allocated_width()
        height = self.get_allocated_height()
        Gtk.render_background(context, cr, 0, 0, width, height)
        Gtk.render_frame(context, cr, 0, 0, width, height)
        return Gtk.HBox.do_draw(self, cr)

    def set_label(self, text):
        """Update the text of our label"""
        self.label.set_text(text)

    def get_label(self):
        return self.label.get_text()

    def set_custom_label(self, text, force=False):
        """Set a permanent label as if the user had edited it"""
        self.label.set_text(text, force=force)
        self.label.set_custom()

    def get_custom_label(self):
        """Return a custom label if we have one, otherwise None"""
        if self.label.is_custom():
            return(self.label.get_text())
        else:
            return(None)

    def edit(self):
        self.label.edit()

    def update_button(self):
        """Update the state of our close button"""
        if not self.config['close_button_on_tab']:
            if self.button:
                self.button.remove(self.icon)
                self.remove(self.button)
                del(self.button)
                del(self.icon)
                self.button = None
                self.icon = None
            return

        if not self.button:
            self.button = Gtk.Button()
        if not self.icon:
            self.icon = Gio.ThemedIcon.new_with_default_fallbacks("window-close-symbolic")
            self.icon = Gtk.Image.new_from_gicon(self.icon, Gtk.IconSize.MENU)

        self.button.set_focus_on_click(False)
        self.button.set_relief(Gtk.ReliefStyle.NONE)
#        style = Gtk.RcStyle()  # FIXME FOR GTK3 how to do it there? actually do we really want to override the theme?
#        style.xthickness = 0
#        style.ythickness = 0
#        self.button.modify_style(style)
        self.button.add(self.icon)
        self.button.connect('clicked', self.on_close)
        self.button.set_name('terminator-tab-close-button')
        if hasattr(self.button, 'set_tooltip_text'):
            self.button.set_tooltip_text(_('Close Tab'))
        self.pack_start(self.button, False, False, 0)
        self.show_all()

    def update_angle(self):
        """Update the angle of a label"""
        position = self.notebook.get_tab_pos()
        if position == Gtk.PositionType.LEFT:
            if hasattr(self, 'set_orientation'):
                self.set_orientation(Gtk.Orientation.VERTICAL)
            self.label.set_angle(90)
        elif position == Gtk.PositionType.RIGHT:
            if hasattr(self, 'set_orientation'):
                self.set_orientation(Gtk.Orientation.VERTICAL)
            self.label.set_angle(270)
        else:
            if hasattr(self, 'set_orientation'):
                self.set_orientation(Gtk.Orientation.HORIZONTAL)
            self.label.set_angle(0)

    def on_close(self, _widget):
        """The close button has been clicked. Destroy the tab"""
        self.emit('close-clicked', self)

    def set_tab_color(self, color):
        """Set the tab color (a '#rrggbb' string) or None to clear it"""
        self.tab_color = color
        self.apply_tab_color()

    def set_tab_active(self, active):
        """Set whether our tab is the active one and restyle accordingly"""
        if active == self.tab_active:
            return
        self.tab_active = active
        self.apply_tab_color()

    def apply_tab_color(self):
        """Apply CSS styling for our tab color, depending on active state"""
        if self.css_provider is None:
            self.css_provider = Gtk.CssProvider()
            self.get_style_context().add_provider(
                    self.css_provider,
                    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 200)
        if not self.tab_color:
            self.css_provider.load_from_data(b'')
            self.queue_draw()
            return
        if self.tab_active:
            # Active tab: fill the whole tab with the color; text color is
            # inherited by the label and the close button
            fg = fg_color_for(self.tab_color)
            css = ('.terminator-tab-label { background-color: %s;'
                   ' color: %s; }' % (self.tab_color, fg))
        else:
            # Inactive tab: subtle background tint + thin border
            r = int(self.tab_color[1:3], 16)
            g = int(self.tab_color[3:5], 16)
            b = int(self.tab_color[5:7], 16)
            css = ('.terminator-tab-label {'
                   ' background-color: rgba(%d, %d, %d, 0.1);'
                   ' box-shadow: inset 0 0 0 1px %s; }'
                   % (r, g, b, self.tab_color))
        self.css_provider.load_from_data(css.encode('utf-8'))
        self.queue_draw()

    def show_tab_menu(self, x, y):
        """Pop up the tab context menu at the given coordinates,
        relative to this tab label"""
        if self.tab_popover:
            self.tab_popover.popdown()
        popover = TabColorPopover(self)
        popover.connect('closed', self.on_tab_popover_closed)
        self.tab_popover = popover
        rect = Gdk.Rectangle()
        rect.x, rect.y = x, y
        rect.width, rect.height = 1, 1
        popover.set_pointing_to(rect)
        popover.show_all()
        popover.popup()

    def on_tab_popover_closed(self, popover):
        """Drop our reference when the popover is dismissed"""
        if self.tab_popover is popover:
            self.tab_popover = None

    def on_button_pressed(self, _widget, event):
        if event.button == 2:
            self.on_close(_widget)
        elif event.button == 3:
            self._activate_tab()
            # Extract the coordinates now: the event struct is only valid
            # for the duration of the signal emission, and the deferred
            # callback runs after that.
            # Show the menu only after the tab switch has fully settled.
            # Switching pages schedules an idle grab_focus() on the terminal
            # (see deferred_on_tab_switch/on_tab_switch), which dismisses any
            # popover shown before it. PRIORITY_LOW puts this idle handler
            # behind that focus transfer.
            x, y = int(event.x), int(event.y)
            GLib.idle_add(self.show_tab_menu, x, y,
                          priority=GLib.PRIORITY_LOW)
            return True

    def _activate_tab(self):
        """Switch to the page this tab label belongs to, if any."""
        nb = self.notebook
        for i in range(nb.get_n_pages()):
            if nb.get_tab_label(nb.get_nth_page(i)) == self:
                nb.set_current_page(i)
                return

def fg_color_for(bg_hex):
    """Pick a readable foreground color for the given background color"""
    rgba = Gdk.RGBA()
    rgba.parse(bg_hex)
    luminance = 0.299 * rgba.red + 0.587 * rgba.green + 0.114 * rgba.blue
    if luminance > 0.55:
        return '#1a1a1a'
    return '#f2f2f2'

def get_tab_colors(config):
    """Return the configured list of tab colors, padded with defaults"""
    from .config import DEFAULTS
    defaults = DEFAULTS['global_config']['tab_colors'].split(':')
    colors = [c for c in config['tab_colors'].split(':') if c]
    while len(colors) < len(defaults):
        colors.append(defaults[len(colors)])
    return colors[:len(defaults)]

class TabColorSwatch(Gtk.DrawingArea):
    """A single color swatch in the tab color menu"""
    SIZE = 20
    INSET = 3

    def __init__(self, color, selected, on_pick):
        GObject.GObject.__init__(self)
        self.color = color          # None means "no color"
        self.selected = selected
        self.hover = False
        self.on_pick = on_pick
        self.set_size_request(self.SIZE, self.SIZE)
        if color is None:
            self.set_tooltip_text(_('No color'))
        else:
            self.set_tooltip_text(color)
        self.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK |
                        Gdk.EventMask.LEAVE_NOTIFY_MASK |
                        Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect('draw', self.on_draw)
        self.connect('enter-notify-event', self.on_enter)
        self.connect('leave-notify-event', self.on_leave)
        self.connect('button-press-event', self.on_button_press)

    def on_enter(self, _widget, _event):
        self.hover = True
        self.queue_draw()

    def on_leave(self, _widget, _event):
        self.hover = False
        self.queue_draw()

    def on_button_press(self, _widget, event):
        if event.button == 1:
            self.on_pick(self.color)
            return True
        return False

    def on_draw(self, _widget, cr):
        width = self.get_allocated_width()
        height = self.get_allocated_height()
        # Grow slightly on hover instead of moving pixels around
        inset = 0 if self.hover else self.INSET

        if self.color is None:
            # "No color" swatch: neutral box with a diagonal slash
            cr.rectangle(inset, inset, width - 2 * inset, height - 2 * inset)
            cr.set_source_rgb(0.85, 0.85, 0.85)
            cr.fill()
            cr.set_source_rgb(0.45, 0.45, 0.45)
            cr.set_line_width(1.4)
            cr.move_to(inset + 1.5, height - inset - 1.5)
            cr.line_to(width - inset - 1.5, inset + 1.5)
            cr.stroke()
        else:
            rgba = Gdk.RGBA()
            rgba.parse(self.color)
            cr.rectangle(inset, inset, width - 2 * inset, height - 2 * inset)
            cr.set_source_rgb(rgba.red, rgba.green, rgba.blue)
            cr.fill()
            # Thin outline so light colors stay visible on light themes
            cr.rectangle(inset + 0.5, inset + 0.5,
                         width - 2 * inset - 1, height - 2 * inset - 1)
            cr.set_source_rgba(0, 0, 0, 0.25)
            cr.set_line_width(1)
            cr.stroke()
            if self.selected:
                fg = Gdk.RGBA()
                fg.parse(fg_color_for(self.color))
                cr.set_source_rgb(fg.red, fg.green, fg.blue)
                cr.set_line_width(2.2)
                cr.set_line_cap(1)      # cairo.LineCap.ROUND
                cr.set_line_join(1)     # cairo.LineJoin.ROUND
                cr.move_to(width * 0.24, height * 0.54)
                cr.line_to(width * 0.44, height * 0.74)
                cr.line_to(width * 0.78, height * 0.28)
                cr.stroke()

class TabColorPopover(Gtk.Popover):
    """Tab context popup holding the move-to-new-window action and a
    horizontal row of tab color swatches.

    A Gtk.Popover is used instead of a Gtk.Menu because menus do not
    deliver pointer events to child widgets inside their items, which
    the interactive swatches need."""

    def __init__(self, tablabel):
        GObject.GObject.__init__(self, relative_to=tablabel)
        self.tablabel = tablabel
        self.set_position(Gtk.PositionType.BOTTOM)

        vbox = Gtk.VBox(spacing=6)
        vbox.set_border_width(6)

        move = Gtk.Button(label=_('_Move to New Window'), use_underline=True)
        move.set_relief(Gtk.ReliefStyle.NONE)
        move.get_style_context().add_class('flat')
        move.set_halign(Gtk.Align.FILL)
        move.get_child().set_xalign(0.0)
        move.connect('clicked', self.on_move_to_new_window_clicked)
        vbox.pack_start(move, False, False, 0)

        vbox.pack_start(Gtk.Separator(), False, False, 0)

        title = Gtk.Label(label=_('Tab color'))
        title.set_halign(Gtk.Align.START)
        vbox.pack_start(title, False, False, 0)

        hbox = Gtk.HBox(spacing=4)
        colors = [None] + get_tab_colors(tablabel.config)
        for color in colors:
            swatch = TabColorSwatch(color, color == tablabel.tab_color,
                                    self.on_pick)
            hbox.pack_start(swatch, False, False, 0)
        vbox.pack_start(hbox, False, False, 0)

        self.add(vbox)

    def on_move_to_new_window_clicked(self, _button):
        """Detach this tab into its own new window"""
        self.popdown()
        tablabel = self.tablabel
        nb = tablabel.notebook
        for i in range(nb.get_n_pages()):
            page = nb.get_nth_page(i)
            if nb.get_tab_label(page) is tablabel:
                # Use the pointer position as the drop point, like a drag
                seat = Gdk.Display.get_default().get_default_seat()
                _screen, x, y = seat.get_pointer().get_position()
                nb.create_window_detach(nb, page, x, y)
                return

    def on_pick(self, color):
        self.tablabel.set_tab_color(color)
        self.popdown()

# vim: set expandtab ts=4 sw=4:

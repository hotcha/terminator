import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from terminatorlib.config import Config
from terminatorlib.notebook import TabLabel


def make_tablabel(close_button=True):
    """Create a TabLabel with close_button_on_tab set as given"""
    config = Config()
    config['close_button_on_tab'] = close_button
    return TabLabel("tab", Gtk.Notebook())


def test_close_button_shown_by_default():
    tablabel = make_tablabel(True)
    assert tablabel.button is not None


def test_close_button_hidden_when_disabled():
    tablabel = make_tablabel(False)
    assert tablabel.button is None


def test_update_button_applies_config_change_to_existing_tab():
    """Toggling the config updates an already-open tab's close button"""
    config = Config()
    tablabel = make_tablabel(True)
    assert tablabel.button is not None

    config['close_button_on_tab'] = False
    tablabel.update_button()
    assert tablabel.button is None

    config['close_button_on_tab'] = True
    tablabel.update_button()
    assert tablabel.button is not None

    # repeated calls with an unchanged config are harmless
    tablabel.update_button()
    assert tablabel.button is not None


def test_apply_tab_color_css_loads_for_all_states():
    """The per-label CSS parses in every color/active/separator state"""
    tablabel = make_tablabel(True)
    for color in (None, '#d99a9a'):
        for active in (False, True):
            for separator in (False, True):
                tablabel.tab_color = color
                tablabel.tab_active = active
                tablabel.tab_separator = separator
                # raises GLib.Error on malformed CSS
                tablabel.apply_tab_color()

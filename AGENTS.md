# Repository Guidelines

## Project Overview

**Terminator** (v2.1.5) is a GPL-2.0-only, Python 3 + GTK 3 terminal emulator that arranges multiple VTE terminals in a single window via flexible tiling, tabs, and broadcasting. Supports DBus IPC for remote control, a plugin system, and ~90 locales via gettext. Canonical tagline: "The robot future of terminals."

## Architecture & Data Flow

### Borg Singleton Layer

Four global singletons share state via the monostate pattern (`borg.py` — `__dict__` keyed by class name):

| Borg class | File | Role |
|---|---|---|
| `Terminator` | `terminatorlib/terminator.py` | Master app: window/terminal/group registry, layout creation, CSS injection, reconfigure dispatch |
| `Factory` | `terminatorlib/factory.py` | Object creation hub — `make()` dispatches to `make_window`/`make_terminal`/`make_hpaned`/`make_vpaned`/`make_notebook` |
| `ConfigBase` | `terminatorlib/config.py` | Config storage: loads `~/.config/terminator/config` via ConfigObj+Validator, saves diffs |
| `DBusService` | `terminatorlib/ipc.py` | IPC server on DBus session bus (`net.tenshu.Terminator2` + display hash) |
| `PluginRegistry` | `terminatorlib/plugin.py` | Plugin discovery, loading, enable/disable |

The thin `Config` class wraps `ConfigBase` with profile-aware access and GSettings monitoring.

### Container Widget Tree

```
Container (abstract mixin + Gtk multiple inheritance)
├── Window        (Container, Gtk.Window)
├── Notebook      (Container, Gtk.Notebook)  — tab container
├── Paned         (Container)                — abstract split pane
│   ├── HPaned    (Paned, Gtk.HPaned)        — horizontal split
│   └── VPaned    (Paned, Gtk.VPaned)        — vertical split
Terminal          (Gtk.VBox wrapping Vte.Terminal)
```

- `Terminal` is NOT a `Container` — it's the leaf node. Contains `Titlebar`, `Searchbar`, scrollbar sub-widgets.
- `Container` is a **mixin** combined with Gtk widgets via multiple inheritance (e.g. `class Window(Container, Gtk.Window)`).

### Signal Propagation

Terminals emit ~29 custom GObject signals (`close-term`, `split-horiz`, `split-vert`, `zoom`, `tab-new`, `group-*`, `resize-term`, `navigate`, `title-change`, etc.). Containers wire these via `connect_child()`:

```
Terminal → Paned → Notebook → Window → Terminator
```

Each container level can intercept or propagate. The `Terminator` singleton receives top-level events.

### Key Patterns

- **Borg singleton**: `Borg.__init__(self, self.__class__.__name__)`. All attributes MUST be declared `= None` and initialized in `prepare_attributes()`.
- **Factory**: All object creation through `Factory.make()`. Uses lazy `from . import` inside make methods to avoid circular imports.
- **Signalman**: Per-instance Gtk signal tracker (`{widget: {signal_name: handler_id}}`). Used by `Container` and `Terminal` for clean disconnect.
- **Circular import resolution**: Deferred imports inside functions/methods, not at module level. Module dependency order is fragile.

### Plugin System

Plugins are Python classes in `terminatorlib/plugins/` or `~/.config/terminator/plugins/`. Each file exposes an `AVAILABLE = ['ClassName']` list. Classes subclass `Plugin`, `URLHandler`, or `MenuItem` from `plugin.py`, and declare a `capabilities` list.

Two capabilities consumed by core:
- `'url_handler'` — regex-based URL matching/transformation (consumed by `terminal.py`, `terminal_popup_menu.py`)
- `'terminal_menu'` — context menu entries (consumed by `terminal_popup_menu.py`)

Self-wired capabilities (plugins wire their own signals): `'command_watch'`, `'MouseFreeHandler'`, `'session'`.

### Configuration

Layered: `DEFAULTS` dict → `~/.config/terminator/config` (ConfigObj/INI) → CLI options (argparse). Sections: `[global_config]`, `[keybindings]`, `[profiles]`, `[layouts]`, `[plugins]`. Only non-default values are saved (`dict_diff`).

### IPC

DBus on session bus. If bus name is taken, new instance acts as client and delegates to the running instance. The `remotinator` script provides CLI remote control (16 commands: new_window, hsplit, vsplit, switch_profile, etc.).

## Key Directories

```
terminatorlib/         # Main package (~30 modules)
  plugins/             # Built-in plugins (~18 files)
  themes/              # Per-theme GTK CSS (Adwaita, Ambiance, Breeze, etc.)
tests/                 # Test suite (3 files)
doc/                   # AsciiDoc manpage sources + generated troff
po/                    # gettext translations (~90 locales)
data/                  # Desktop entry, AppStream metadata, icons, layout examples
completion/            # Bash completion script
.github/               # CI workflow (single python.yml)
```

## Development Commands

```bash
# Build (compiles .po → .mo, processes .desktop/.appdata templates)
python setup.py build

# Install (system-wide; use --record for uninstall manifest)
python setup.py install --single-version-externally-managed --record=install_files.txt

# Uninstall
python setup.py uninstall --manifest=install_files.txt

# Run tests (requires GTK runtime + xvfb)
xvfb-run -a pytest-3

# Or via setup.py alias
python setup.py test

# Compile-check all Python files
python -m compileall -f .

# Generate manpages
bash doc/gen_manpages.sh

# Update translation template
bash po/genpot.sh

# Merge all PO files against template
bash po/update_all_po.sh

# Run directly from checkout
./terminator
```

## Code Conventions & Common Patterns

### Naming
- Modules: lowercase, no underscores (`terminator.py`, `keybindings.py`, `terminal_popup_menu.py`)
- Classes: PascalCase (`Terminator`, `ConfigBase`, `PrefsEditor`)
- Methods/functions: snake_case (`split_axis`, `describe_layout`, `group_emit`)
- Class attributes for Borg: declared `= None` at class level

### GObject Signals
- Defined on classes via `GObject.signal_new()` or `__gsignals__` dict
- Custom signals use kebab-case names (`close-term`, `split-horiz`, `group-all`)
- `Signalman` tracks per-instance handler IDs for bulk disconnect

### Error Handling
- Uses `try/except` with specific exception types
- Debug logging via `util.dbg()` and `util.err()` (controlled by `--debug` flag)
- `util.DEBUG` global flags: `DEBUG` (bool), debug class/method filters

### Imports
- Lazy imports inside functions to break circular deps — see `Factory.make_window()`:
  ```python
  def make_window(self, **kwargs):
      from . import window
      return(window.Window(**kwargs))
  ```
- Module-level imports only for non-circular dependencies
- `gi.repository` (GObject Introspection) for GTK, VTE, Gdk, GLib, Pango

### GTK Patterns
- **GTK 3.0** with `gi.require_version('Gtk', '3.0')` — no GTK 4
- **VTE 2.91** (`Vte.Terminal.spawn_sync()` for processes, `Vte.Regex` for search/URL matching)
- **CSS injection** via `Gtk.CssProvider` for transparency, pane handles, tab colors, profile theming
- **Gtk.Builder** for UI: `preferences.glade` (281KB) and `layoutlauncher.glade`
- Theme CSS from `terminatorlib/themes/<name>/gtk-3.0/apps/terminator.css`

### Keyborg Pattern
```python
class Foo(Borg):
    attr = None
    def __init__(self):
        Borg.__init__(self, self.__class__.__name__)
        self.prepare_attributes()
    def prepare_attributes(self):
        if not self.attr:
            self.attr = []
```

## Important Files

| File | Purpose |
|---|---|
| `terminator` | Entrypoint script — checks GTK+DISPLAY, parses options, starts DBus or creates layout |
| `terminatorlib/terminator.py` | Master singleton (window/terminal/group registry, group broadcasting, CSS/reconfigure) |
| `terminatorlib/terminal.py` | Core terminal widget (~2286 lines) — VTE wrapper, spawning, URL matching, signals |
| `terminatorlib/container.py` | Abstract Container mixin — `split_axis`, `closeterm`, `describe_layout`, signal registration |
| `terminatorlib/window.py` | Top-level window (`~1204 lines`) — zoom, key shortcuts, geometry, fullscreen states |
| `terminatorlib/notebook.py` | Tab container (`~937 lines`) — detachable tabs, newtab, CSS injection |
| `terminatorlib/paned.py` | Split panes (`~551 lines`) — HPaned/VPaned, wrapcloseterm, rotation, ratios |
| `terminatorlib/config.py` | ConfigBase (Borg) + Config (profile-aware wrapper) (~974 lines) |
| `terminatorlib/configjson.py` | JSON layout/profile injection for `--config-json` |
| `terminatorlib/factory.py` | Object creation hub with lazy imports |
| `terminatorlib/borg.py` | Borg monostate base class |
| `terminatorlib/ipc.py` | DBus server + `@with_proxy` client decorators (~441 lines) |
| `terminatorlib/keybindings.py` | Accelerator parsing, lookup table from config |
| `terminatorlib/optionparse.py` | CLI argument parsing (argparse) |
| `terminatorlib/plugin.py` | Plugin base classes, PluginRegistry, KeyBindUtil |
| `terminatorlib/prefseditor.py` | Full preferences dialog (~2335 lines, loaded from `preferences.glade`) |
| `terminatorlib/signalman.py` | Per-instance Gtk signal tracker |
| `terminatorlib/util.py` | Debug logging, UUID, widget tree walking, X11/Wayland/Flatpak detection |
| `terminatorlib/version.py` | `APP_NAME='terminator'`, `APP_VERSION='2.1.5'` |
| `remotinator` | CLI DBus client (uses `terminatorlib.ipc`) |
| `setup.py` | setuptools build with custom compile/install/uninstall commands |

## Runtime/Tooling Preferences

- **Language**: Python 3 (tested on 3.10; no Python 2 support)
- **GUI toolkit**: GTK 3.0 (GObject Introspection) — **not** GTK 4
- **Terminal widget**: VTE 2.91 (vte-0.38+)
- **Build system**: setuptools (`setup.py` + `setup.cfg`) — **no** `pyproject.toml`, **no** `tox.ini`
- **Package manager**: pip is fine; OS packages preferred for native deps. **No** lock file or `requirements.txt`
- **Runtime deps**: pycairo, configobj, dbus-python, pygobject, psutil, gir1.2-vte-2.91, gir1.2-keybinder-3.0
- **Console scripts**: `terminator` and `remotinator` are bare scripts installed via `scripts=` (NOT `entry_points`)
- **i18n**: gettext via intltool; translations on Transifex
- **Manpages**: AsciiDoc source → `asciidoctor -b manpage` → troff
- **No type hints** — plain Python throughout
- **No formatter/linter configured** — no `.flake8`, `.pylintrc`, `ruff.toml`, or `pyproject.toml` tool configs

## Testing & QA

- **Framework**: pytest (configured via `pytest.ini`: `--doctest-modules --verbose`)
- **Test files**: 3 files in `tests/` — no `conftest.py`, no shared fixtures
- **Two styles coexist**:
  - `test_borg.py`, `test_signalman.py` — doctest-only; test logic in module docstrings
  - `test_prefseditor_keybindings.py` — real pytest with `@pytest.mark.parametrize`, real GTK objects, `assert`
- **Headless execution**: Tests need GTK runtime → run via `xvfb-run -a pytest-3`
- **Isolation**: Manual config reset (`reset_config_keybindings()`) — no fixture-based teardown
- **No mocking framework** — `test_signalman.py` hand-rolls a `TestWidget` mock class
- **CI**: GitHub Actions, Ubuntu 22.04, Python 3.10 only. Compiles all `.py` with `python -m compileall`, then `xvfb-run -a pytest-3`. Does **not** `pip install` — imports from checkout
- **No coverage, linting, or type checking in CI**

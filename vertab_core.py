"""VerTab storage and settings -- everything that touches disk, and no Tk.

Kept apart from the UI so it can be unit tested (tests/test_core.py) and so
the rules that protect the notebook live in one place:

  * every write is atomic (temp file + os.replace), so a crash or power cut
    mid-save can never leave a truncated notes.json;
  * the first save of a session keeps the previous notebook as notes.json.bak;
  * a notes.json that cannot be parsed is moved aside, never overwritten;
  * settings read from disk are type-checked, so a hand-edited or damaged
    config.json falls back to defaults instead of crashing the app.
"""
import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zlib

APP_NAME = "VerTab"
NOTES_FILE = "notes.json"
CONFIG_FILE = "config.json"
LOG_FILE = "vertab.log"
LOG_LIMIT = 256 * 1024

IS_WINDOWS = sys.platform == "win32"
FROZEN = getattr(sys, "frozen", False)

MIN_OPACITY, MAX_OPACITY = 0.25, 1.0

DEFAULT_CONFIG = {
    "overlay_mode": False,
    "topmost": False,       # window mode only; the overlay is always on top
    "opacity": 0.92,
    "theme": "journal",
    "window_geometry": "",
    "overlay_geometry": "",
    "sidebar_visible": True,
    "hide_from_taskbar": True,
    "autosave": True,
}

# Tk reports positions as +X+Y, using "+-8" for negative offsets.
GEOMETRY_RE = re.compile(r"^\d{1,5}x\d{1,5}(?:\+-?\d{1,6}\+-?\d{1,6})?$")
POSITIONED_RE = re.compile(r"^(\d{1,5})x(\d{1,5})\+(-?\d{1,6})\+(-?\d{1,6})$")


class NotebookError(Exception):
    """A save was refused or failed; the message is meant for the user."""


# -------------------------------
# Paths
# -------------------------------
def app_dir():
    """Folder the app lives in (the executable's folder when frozen)."""
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(*parts):
    """Path to a bundled read-only resource (handles the PyInstaller bundle)."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)


def data_dir(portable=False):
    """Folder holding notes.json and config.json."""
    if portable:
        return app_dir()
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_NAME)
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", APP_NAME)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_NAME.lower())


def system_dll(name, **kwargs):
    """Load a Windows DLL from System32 only.

    A bare name would also search the executable's folder and the working
    directory, so a planted same-named DLL could be loaded instead.
    """
    import ctypes
    LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x800
    return ctypes.WinDLL(name, winmode=LOAD_LIBRARY_SEARCH_SYSTEM32, **kwargs)


def instance_name(folder):
    """Mutex name unique to one data folder, so portable copies don't collide."""
    key = os.path.normcase(os.path.abspath(folder)).encode("utf-8", "replace")
    return "Local\\VerTab-%08x" % (zlib.crc32(key) & 0xFFFFFFFF)


# -------------------------------
# Low-level file helpers
# -------------------------------
def write_json_atomic(path, data):
    """Write JSON so readers only ever see the old file or the complete new one."""
    folder = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        replace_with_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def replace_with_retry(src, dst, attempts=5, delay=0.05):
    """os.replace, retried briefly: on Windows antivirus and the search
    indexer can hold the target open for a moment and cause PermissionError."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay * (attempt + 1))


def normalize_notes(raw):
    """Coerce parsed JSON into {title: text}; raise ValueError if it isn't a notebook."""
    if not isinstance(raw, dict):
        raise ValueError("the file does not contain a notebook")
    clean = {}
    for title, text in raw.items():
        clean[str(title)] = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return clean


def validate_config(saved):
    """Merge saved settings over the defaults, dropping anything malformed."""
    config = dict(DEFAULT_CONFIG)
    if not isinstance(saved, dict):
        return config
    for key, default in DEFAULT_CONFIG.items():
        if key not in saved:
            continue
        value = saved[key]
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, float):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        else:
            ok = isinstance(value, type(default))
        if ok:
            config[key] = value
    config["opacity"] = min(MAX_OPACITY, max(MIN_OPACITY, float(config["opacity"])))
    for key in ("window_geometry", "overlay_geometry"):
        if config[key] and not GEOMETRY_RE.match(config[key]):
            config[key] = ""
    if not re.match(r"^[a-z][a-z0-9_-]{0,31}$", config["theme"]):
        config["theme"] = DEFAULT_CONFIG["theme"]
    return config


# -------------------------------
# Window geometry maths (pure, so it can be tested without a screen)
# -------------------------------
def clamp_geometry(geometry, work_area):
    """Pull a saved 'WxH+X+Y' fully inside work_area = (left, top, right, bottom).

    A notebook saved on a monitor that has since been unplugged would
    otherwise reopen off-screen, which for a frameless overlay means lost.
    """
    match = POSITIONED_RE.match(geometry or "")
    if not match:
        return geometry
    w, h, x, y = (int(v) for v in match.groups())
    left, top, right, bottom = work_area
    w = max(1, min(w, right - left))
    h = max(1, min(h, bottom - top))
    x = max(left, min(x, right - w))
    y = max(top, min(y, bottom - h))
    return "%dx%d+%d+%d" % (w, h, x, y)


def snap_position(x, y, w, h, work_area, distance):
    """Return (x, y) pulled flush to any work-area edge within `distance` px."""
    left, top, right, bottom = work_area
    if abs(x - left) <= distance:
        x = left
    elif abs(right - (x + w)) <= distance:
        x = right - w
    if abs(y - top) <= distance:
        y = top
    elif abs(bottom - (y + h)) <= distance:
        y = bottom - h
    return x, y


# -------------------------------
# The data folder
# -------------------------------
class Storage:
    """Owns one data folder: the notebook, the settings and the error log."""

    def __init__(self, folder):
        self.folder = folder
        self.read_only = False      # set when a damaged notebook could not be moved aside
        self._backed_up = False

    def path(self, name):
        return os.path.join(self.folder, name)

    def prepare(self, legacy_dir=None):
        """Create the folder and adopt a notes.json left next to the script."""
        os.makedirs(self.folder, exist_ok=True)
        target = self.path(NOTES_FILE)
        if legacy_dir and not os.path.exists(target):
            legacy = os.path.join(legacy_dir, NOTES_FILE)
            if os.path.isfile(legacy) and os.path.abspath(legacy) != os.path.abspath(target):
                shutil.copy2(legacy, target)

    # --- notes ---
    def load_notes(self):
        """Return (notes, warning). warning is None unless the file was damaged."""
        path = self.path(NOTES_FILE)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return normalize_notes(json.load(f)), None
        except FileNotFoundError:
            return {}, None
        except (ValueError, UnicodeDecodeError, OSError) as exc:
            return {}, self._quarantine(path, exc)

    def _quarantine(self, path, reason):
        backup = "%s.damaged-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        try:
            replace_with_retry(path, backup)
        except OSError:
            # Could not move it, so never write over it either.
            self.read_only = True
            return ("Your notebook (%s) could not be read: %s\n\nVerTab could not move it "
                    "aside, so saving is disabled to avoid overwriting it." % (path, reason))
        return ("Your notebook could not be read: %s\n\nThe damaged file was kept as:\n%s\n\n"
                "VerTab started with an empty notebook." % (reason, backup))

    def save_notes(self, notes):
        if self.read_only:
            raise NotebookError("Saving is disabled because the existing notebook "
                                "could not be read or moved aside.")
        path = self.path(NOTES_FILE)
        try:
            if not self._backed_up and os.path.isfile(path):
                shutil.copy2(path, path + ".bak")
            self._backed_up = True
            write_json_atomic(path, notes)
        except OSError as exc:
            raise NotebookError("Could not save the notebook to %s:\n%s" % (path, exc)) from exc

    # --- settings ---
    def load_config(self, argv=()):
        try:
            with open(self.path(CONFIG_FILE), "r", encoding="utf-8") as f:
                config = validate_config(json.load(f))
        except (OSError, ValueError, UnicodeDecodeError):
            config = dict(DEFAULT_CONFIG)
        # Command line wins over the saved mode.
        if "--overlay" in argv:
            config["overlay_mode"] = True
        if "--window" in argv:
            config["overlay_mode"] = False
        return config

    def save_config(self, config):
        """Settings are a convenience: failures are logged, never raised."""
        try:
            write_json_atomic(self.path(CONFIG_FILE), config)
        except OSError as exc:
            self.log("could not save settings: %s" % exc)

    # --- log ---
    def log(self, text):
        """Append to vertab.log; the windowed exe has no console to print to."""
        path = self.path(LOG_FILE)
        try:
            if os.path.isfile(path) and os.path.getsize(path) > LOG_LIMIT:
                replace_with_retry(path, path + ".old")
            with open(path, "a", encoding="utf-8") as f:
                f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text.rstrip()))
        except OSError:
            pass

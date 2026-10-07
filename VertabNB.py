#!/usr/bin/env python
"""VerTab Notebook -- vertical-tab notes that can float on top of other apps.

Two ways to run it:

  Window mode   a normal resizable window.
  Overlay mode  a frameless, always-on-top, semi-transparent panel that sits
                over whatever you are working in, with global hotkeys so you
                can show, hide and click through it without leaving the other
                app.

Notes and settings live in a per-user folder (%APPDATA%\\VerTab on Windows);
see vertab_core.py for how they are stored safely.  Pass --portable to keep
them next to the executable instead.

Hotkeys (global on Windows -- they work while another app is focused):
  Ctrl+Alt+N    show / hide VerTab
  Ctrl+Alt+M    switch between overlay and window mode
  Ctrl+Alt+T    click-through on / off
  Ctrl+Alt+Up   more opaque        Ctrl+Alt+Down  more transparent
"""
import contextlib
import functools
import queue
import sys
import threading
import traceback
import tkinter as tk
from tkinter import ttk, messagebox

from ttkbootstrap import Style

import vertab_core as core
import vertab_ui as ui
from vertab_core import IS_WINDOWS

APP_NAME = core.APP_NAME
APP_TITLE = "VerTab Notebook"

SNAP_DISTANCE = 18          # px from a work-area edge before the overlay snaps to it
MIN_WIDTH, MIN_HEIGHT = 320, 240
COMPACT_WIDTH = 560         # px; narrower than this the note list and the editor take turns
READING_WIDTH = 720         # px; the writing column stops widening here and centres
SIDEBAR_WIDTH = 276         # px
OPACITY_STEP = 0.05
# Click-through needs a layered window, and Tk only keeps the window layered
# while alpha is below 1 -- at exactly 1.0 a click-through overlay can vanish.
CLICK_THROUGH_MAX_ALPHA = 0.99

# Global dictionary for notes and a variable for current note title.
notes = {}
current_note_title = None
dirty = False               # editor has edits that are not on disk

# Treeview item ids are generated, never note titles: a title such as "{oops"
# is not a valid Tcl list element and would crash tree.selection().
iid_by_title = {}
title_by_iid = {}

storage = core.Storage(core.data_dir(portable="--portable" in sys.argv))

# -------------------------------
# Windows API
# -------------------------------
# Click-through, no-activate, taskbar hiding, monitor work areas, DPI and the
# single-instance mutex are not exposed by Tk.  Everything that uses this is
# a no-op off Windows.
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    user32 = core.system_dll("user32.dll", use_last_error=True)
    kernel32 = core.system_dll("kernel32.dll", use_last_error=True)

    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TOOLWINDOW = 0x00000080
    MONITOR_DEFAULTTONEAREST = 2
    ERROR_ALREADY_EXISTS = 183

    if ctypes.sizeof(ctypes.c_void_p) == 8:
        _get_long = user32.GetWindowLongPtrW
        _set_long = user32.SetWindowLongPtrW
        _long = ctypes.c_longlong
    else:
        _get_long = user32.GetWindowLongW
        _set_long = user32.SetWindowLongW
        _long = ctypes.c_long
    _get_long.restype = _long
    _get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    _set_long.restype = _long
    _set_long.argtypes = [wintypes.HWND, ctypes.c_int, _long]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = wintypes.HANDLE
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE


def enable_dpi_awareness():
    """Render crisply on scaled displays instead of being bitmap-stretched.

    System-aware rather than per-monitor: Tk 8.6 does not handle
    WM_DPICHANGED, so per-monitor awareness would leave it mis-sized when
    dragged between monitors with different scaling.
    """
    if not IS_WINDOWS:
        return
    try:
        core.system_dll("shcore.dll").SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        with contextlib.suppress(AttributeError, OSError):
            user32.SetProcessDPIAware()


instance_lock = None


def acquire_instance_lock():
    """One VerTab per notebook -- two would overwrite each other's saves."""
    global instance_lock
    if IS_WINDOWS:
        ctypes.set_last_error(0)
        instance_lock = kernel32.CreateMutexW(None, False, core.instance_name(storage.folder))
        return ctypes.get_last_error() != ERROR_ALREADY_EXISTS
    import fcntl
    try:
        instance_lock = open(storage.path(".lock"), "w")
        fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def show_startup_message(kind, message):
    """Report something before the main window exists, then let the caller exit."""
    temp = tk.Tk()
    temp.withdraw()
    if kind == "error":
        messagebox.showerror(APP_TITLE, message, parent=temp)
    else:
        messagebox.showinfo(APP_TITLE, message, parent=temp)
    temp.destroy()


def window_handle():
    """HWND of the real top-level window (not Tk's inner child window)."""
    try:
        return int(root.wm_frame(), 16)
    except (ValueError, tk.TclError):
        return root.winfo_id()


def update_ex_style(add=0, remove=0):
    """Add/remove extended window style bits."""
    if not IS_WINDOWS:
        return
    hwnd = window_handle()
    style_bits = (_get_long(hwnd, GWL_EXSTYLE) | add) & ~remove
    _set_long(hwnd, GWL_EXSTYLE, _long(style_bits))


def work_area_at(x, y):
    """(left, top, right, bottom) of the usable desktop on the monitor nearest
    to (x, y) -- excludes the taskbar, and follows multi-monitor layouts."""
    if IS_WINDOWS:
        monitor = user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), MONITOR_DEFAULTTONEAREST)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r = info.rcWork
            return r.left, r.top, r.right, r.bottom
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def on_screen(geometry):
    """Clamp a saved geometry onto the monitor it is closest to."""
    match = core.POSITIONED_RE.match(geometry or "")
    if not match:
        return geometry
    w, h, x, y = (int(v) for v in match.groups())
    return core.clamp_geometry(geometry, work_area_at(x + w // 2, y + h // 2))


def apply_taskbar_visibility():
    """Keep the overlay out of the taskbar and Alt+Tab when asked to."""
    if not IS_WINDOWS:
        return
    hide = overlay_mode.get() and config["hide_from_taskbar"]
    # The style only takes effect while the window is hidden.
    was_visible = root.state() != "withdrawn"
    if was_visible:
        root.withdraw()
    if hide:
        update_ex_style(add=WS_EX_TOOLWINDOW)
    else:
        update_ex_style(remove=WS_EX_TOOLWINDOW)
    if was_visible:
        root.deiconify()


def apply_click_through():
    """Let mouse clicks fall through to the app underneath."""
    if not IS_WINDOWS:
        return
    refresh_alpha()  # first, so Tk has made the window layered
    if click_through.get():
        update_ex_style(add=WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
    else:
        update_ex_style(remove=WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        root.lift()


# -------------------------------
# Global hotkeys (Windows)
# -------------------------------
# RegisterHotKey delivers WM_HOTKEY to the thread that registered it, and Tk's
# own message pump swallows those, so the hotkeys get their own thread with a
# plain message loop.  It hands names back over a queue that Tk polls.
HOTKEY_QUEUE = queue.Queue()

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

# name -> (modifiers, virtual key code, label shown in the UI)
GLOBAL_HOTKEYS = {
    "toggle_visible": (MOD_CONTROL | MOD_ALT, 0x4E, "Ctrl+Alt+N"),       # N
    "toggle_overlay": (MOD_CONTROL | MOD_ALT, 0x4D, "Ctrl+Alt+M"),       # M
    "toggle_click_through": (MOD_CONTROL | MOD_ALT, 0x54, "Ctrl+Alt+T"),  # T
    "opacity_up": (MOD_CONTROL | MOD_ALT, 0x26, "Ctrl+Alt+Up"),          # Up
    "opacity_down": (MOD_CONTROL | MOD_ALT, 0x28, "Ctrl+Alt+Down"),      # Down
}

HOTKEY_HELP = {
    "toggle_visible": "show / hide VerTab",
    "toggle_overlay": "overlay <-> window mode",
    "toggle_click_through": "click-through on / off",
    "opacity_up": "more opaque",
    "opacity_down": "more transparent",
}


class HotkeyThread(threading.Thread):
    """Registers the global hotkeys and posts their names onto a queue."""

    def __init__(self):
        super().__init__(daemon=True, name="vertab-hotkeys")
        self.thread_id = None
        self.active = set()             # names that registered successfully
        self.ready = threading.Event()  # set once registration has been tried

    def run(self):
        ids = {}
        try:
            self.thread_id = kernel32.GetCurrentThreadId()
            for index, (name, (mods, vk, _label)) in enumerate(GLOBAL_HOTKEYS.items(), start=1):
                if user32.RegisterHotKey(None, index, mods | MOD_NOREPEAT, vk):
                    ids[index] = name
                    self.active.add(name)
            self.ready.set()
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam in ids:
                    HOTKEY_QUEUE.put(ids[msg.wParam])
        except Exception:  # a dead hotkey thread must not take the app down
            storage.log("hotkey thread failed:\n" + traceback.format_exc())
        finally:
            self.ready.set()
            for index in ids:
                user32.UnregisterHotKey(None, index)

    def stop(self):
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)


hotkey_thread = None


def start_hotkeys():
    global hotkey_thread
    if IS_WINDOWS and hotkey_thread is None:
        hotkey_thread = HotkeyThread()
        hotkey_thread.start()
        root.after(300, report_hotkey_conflicts)
        poll_hotkeys()


def hotkey_available(name):
    return bool(hotkey_thread and hotkey_thread.ready.is_set() and name in hotkey_thread.active)


def hotkey_taken(name):
    """True once registration has finished and another app already owns this hotkey."""
    return bool(hotkey_thread and hotkey_thread.ready.is_set() and name not in hotkey_thread.active)


def report_hotkey_conflicts():
    if not hotkey_thread.ready.is_set():
        root.after(200, report_hotkey_conflicts)
        return
    taken = [GLOBAL_HOTKEYS[n][2] for n in GLOBAL_HOTKEYS if n not in hotkey_thread.active]
    if taken:
        storage.log("hotkeys already in use by another app: " + ", ".join(taken))
        set_status("%s in use by another app (F1 for details)" % plural(len(taken), "hotkey"),
                   clear_after=8000)


def poll_hotkeys():
    """Run queued hotkey actions on the Tk thread."""
    while True:
        try:
            name = HOTKEY_QUEUE.get_nowait()
        except queue.Empty:
            break
        action = HOTKEY_ACTIONS.get(name)
        if action:
            action()
    root.after(100, poll_hotkeys)


# -------------------------------
# Note operations
# -------------------------------
def matching_titles():
    """Titles whose name or text contains the search box's text, case-insensitively."""
    query = search_var.get().strip().casefold()
    titles = sorted(notes, key=str.casefold)
    if not query:
        return titles
    return [t for t in titles if query in t.casefold() or query in notes[t].casefold()]


# Update the sidebar Treeview with the note titles that match the search.
def update_treeview():
    tree.delete(*tree.get_children())
    iid_by_title.clear()
    title_by_iid.clear()
    shown = matching_titles()
    for title in shown:
        iid = tree.insert("", tk.END, text=title)
        iid_by_title[title] = iid
        title_by_iid[iid] = title
    refresh_list_chrome(len(shown))


def on_search_changed(*_args):
    update_treeview()
    select_title(current_note_title)


def focus_search(event=None):
    reveal_list()
    search_entry.focus_set()
    search_entry.selection_range(0, tk.END)
    return "break"


def on_search_escape(event=None):
    """Esc clears the search; with nothing to clear it returns to the editor."""
    if search_var.get():
        search_var.set("")
    else:
        reveal_editor()
        content_text.focus_set()
    return "break"  # never fall through to the hide-window binding


def search_to_list(event=None):
    children = tree.get_children()
    if children:
        tree.focus_set()
        if not tree.selection():
            tree.focus(children[0])
            tree.selection_set(children[0])
    return "break"


def open_first_match(event=None):
    children = tree.get_children()
    if children and open_note(title_by_iid[children[0]]):
        reveal_editor()
        content_text.focus_set()
    return "break"


def on_list_up(event=None):
    """Up from the first row goes back to the search box."""
    children = tree.get_children()
    if children and tree.focus() == children[0]:
        search_entry.focus_set()
        return "break"


def on_list_enter(event=None):
    reveal_editor()
    content_text.focus_set()
    return "break"


def on_list_click(event):
    if tree.identify_row(event.y):
        reveal_editor()  # in a compact window the list gives way to the note


def select_title(title):
    """Highlight a note in the sidebar (or clear the highlight for None)."""
    iid = iid_by_title.get(title)
    if iid:
        tree.selection_set(iid)
        tree.see(iid)
    else:
        tree.selection_remove(tree.selection())


def load_into_editor(title):
    global current_note_title
    current_note_title = title
    title_var.set(title or "")
    content_text.delete("1.0", tk.END)
    if title is not None:
        content_text.insert(tk.END, notes.get(title, ""))
    content_text.edit_reset()  # undo history must not cross notes
    mark_clean()


def open_note(title):
    """Load a note after settling unsaved edits. False if the user cancelled."""
    if title == current_note_title:
        return True
    if not resolve_unsaved("open another note"):
        select_title(current_note_title)
        return False
    load_into_editor(title)
    select_title(title)
    return True


# When a note is selected in the sidebar, load it into the editor.
def on_tree_select(event):
    selected = tree.selection()
    if not selected:
        return
    title = title_by_iid.get(selected[0])
    if title is not None:
        open_note(title)
    place_selection()


# Create a new (empty) note to edit.
def new_note(event=None):
    if resolve_unsaved("start a new note"):
        load_into_editor(None)
        select_title(None)
        reveal_editor()
        title_entry.focus_set()
    return "break"


def commit_note(title, content):
    """Write one note to disk. Returns False (and keeps the edits) on failure."""
    global current_note_title
    before = dict(notes)
    renamed = current_note_title is not None and current_note_title != title
    if renamed:
        notes.pop(current_note_title, None)
    added = title not in notes
    notes[title] = content
    try:
        storage.save_notes(notes)
    except core.NotebookError as exc:
        notes.clear()
        notes.update(before)  # memory must keep matching what is on disk
        messagebox.showerror("Not saved", str(exc), parent=root)
        return False
    current_note_title = title
    if renamed or added:
        update_treeview()
    select_title(title)
    mark_clean()
    return True


# Save/update the current note.
def save_note(event=None):
    title = title_var.get().strip()
    if not title:
        messagebox.showerror("Error", "Title cannot be empty!", parent=root)
        return "break"
    if title != current_note_title and title in notes:
        replace = messagebox.askyesno(
            "Replace Note",
            "A note named “%s” already exists.\n\nReplace it with this one?" % title,
            icon="warning", parent=root,
        )
        if not replace:
            return "break"
    if commit_note(title, content_text.get("1.0", "end-1c").rstrip()):
        set_status("Saved “%s”" % title)
    return "break"


# Delete the currently loaded note.
def delete_note():
    if current_note_title is None:
        messagebox.showerror("Error", "No note selected to delete!", parent=root)
        return
    confirm = messagebox.askyesno(
        "Delete Note",
        "Are you sure you want to delete “%s”?" % current_note_title,
        icon="warning", parent=root,
    )
    if not confirm:
        return
    removed = notes.pop(current_note_title)
    try:
        storage.save_notes(notes)
    except core.NotebookError as exc:
        notes[current_note_title] = removed
        messagebox.showerror("Not deleted", str(exc), parent=root)
        return
    update_treeview()
    load_into_editor(None)  # clear the editor


def can_save_as(title):
    """A title can be saved without clobbering a different note."""
    return bool(title) and (title == current_note_title or title not in notes)


def resolve_unsaved(action):
    """Before the editor is replaced: save, discard or cancel. True = go ahead."""
    if not dirty:
        return True
    title = title_var.get().strip()
    text = content_text.get("1.0", "end-1c").rstrip()
    if not title and not text.strip():
        return True  # an empty draft has nothing to lose
    if can_save_as(title) and config["autosave"]:
        return commit_note(title, text)

    if not title:
        problem = "This note has no title yet, so it can't be saved."
    elif not can_save_as(title):
        problem = "Another note is already called “%s”." % title
    else:
        problem = "This note has unsaved changes."
    if can_save_as(title):
        answer = messagebox.askyesnocancel(
            "Unsaved changes", "%s\n\nSave it before you %s?" % (problem, action), parent=root)
        if answer is None:
            return False
        return commit_note(title, text) if answer else True
    return messagebox.askokcancel(
        "Unsaved changes", "%s\n\nDiscard it and %s?" % (problem, action),
        icon="warning", parent=root)


def quiet_autosave():
    """Save without asking anything -- used when the overlay is hidden."""
    if dirty and config["autosave"]:
        title = title_var.get().strip()
        if can_save_as(title):
            commit_note(title, content_text.get("1.0", "end-1c").rstrip())


def on_text_modified(event=None):
    # <<Modified>> is queued, so it can arrive after mark_clean(); trust the
    # widget's flag, not the event, to decide whether anything changed.
    global dirty
    if content_text.edit_modified():
        dirty = True
        content_text.edit_modified(False)
        refresh_editor_status()


def on_title_changed(*_args):
    global dirty
    dirty = True
    refresh_editor_status()


def mark_clean():
    global dirty
    dirty = False
    content_text.edit_modified(False)
    refresh_editor_status()


# Info button popup
def show_info():
    intro = ("Notes live in vertical tabs: search them, switch between them and edit "
             "in place. Overlay mode floats VerTab on top of other apps -- drag the "
             "title bar to move it, drag the bottom-right corner to resize, and use "
             "the menu for opacity, themes and click-through. Right click a text box "
             "for cut / copy / paste.")
    if not IS_WINDOWS:
        intro += " The global hotkeys only work while VerTab is focused."
    shortcuts = [
        (label, HOTKEY_HELP[name], "in use by another app" if hotkey_taken(name) else "")
        for name, (_mods, _vk, label) in GLOBAL_HOTKEYS.items()
    ] + [
        ("Ctrl+F", "search notes", ""),
        ("Ctrl+N", "new note", ""),
        ("Ctrl+S", "save the open note", ""),
        ("Esc", "clear the search, or hide VerTab", ""),
    ]
    footer = "Notes are stored in:\n" + storage.path(core.NOTES_FILE)
    x, y = root.winfo_rootx(), root.winfo_rooty()
    ui.InfoDialog(root, look, "VerTab Notebook", intro, shortcuts, footer).show(work_area_at(x, y))


# -------------------------------
# Startup checks (before any window)
# -------------------------------
enable_dpi_awareness()

try:
    storage.prepare(legacy_dir=core.app_dir())
except OSError as exc:
    show_startup_message("error", "VerTab cannot use its data folder:\n%s\n\n%s"
                         % (storage.folder, exc))
    sys.exit(1)

if not acquire_instance_lock():
    show_startup_message("info", "VerTab is already running.\n\n%s" % (
        "Press Ctrl+Alt+N to show it." if IS_WINDOWS else "Switch to the open VerTab window."))
    sys.exit(0)

config = storage.load_config(sys.argv)
notes, notes_warning = storage.load_notes()

# -------------------------------
# Create the main window
# -------------------------------
root = tk.Tk()
root.withdraw()  # stay hidden until the mode is applied, to avoid a flash
root.title(APP_TITLE)

# Geometry is in physical pixels once DPI-aware; scale designed sizes by DPI.
SCALE = max(1.0, root.winfo_fpixels("1i") / 96.0)


def px(value):
    return int(round(value * SCALE))


root.minsize(px(MIN_WIDTH), px(MIN_HEIGHT))


def report_error(exc_type, exc, tb):
    """Tk callback errors: the windowed exe has no console, so log and show."""
    storage.log("".join(traceback.format_exception(exc_type, exc, tb)))
    messagebox.showerror("Unexpected error", "%s\n\nDetails were written to:\n%s"
                         % (exc, storage.path(core.LOG_FILE)), parent=root)


root.report_callback_exception = report_error

# ttkbootstrap provides the ttk engine; the two Midnight palettes are registered
# with it as themes, so "midnight" / "midnight-light" are real theme names.
# config["theme"] may also be "auto", which follows Windows light/dark mode.
style = Style()
ui.register_themes(style)
if config["theme"] not in ui.THEMES:  # an older version's theme, or a hand-edited name
    config["theme"] = ui.DEFAULT_THEME
style.theme_use(ui.resolve_theme(config["theme"]))
look = ui.Look(root, SCALE)
look.use(ui.resolve_theme(config["theme"]))

with contextlib.suppress(tk.TclError, OSError):  # icon is cosmetic
    if IS_WINDOWS:
        root.iconbitmap(default=core.resource_path("assets", "vertab.ico"))
    else:
        root.iconphoto(True, tk.PhotoImage(file=core.resource_path("assets", "vertab.png")))

overlay_mode = tk.BooleanVar(value=config["overlay_mode"])
topmost = tk.BooleanVar(value=config["topmost"])
click_through = tk.BooleanVar(value=False)  # always starts off, see README
sidebar_visible = tk.BooleanVar(value=config["sidebar_visible"])
hide_from_taskbar = tk.BooleanVar(value=config["hide_from_taskbar"])
autosave_enabled = tk.BooleanVar(value=config["autosave"])
title_var = tk.StringVar()
search_var = tk.StringVar()
opacity = config["opacity"]

# Every widget that carries colours is registered here, so apply_theme can
# repaint the whole window when the palette changes.
THEMED = []


def themed(widget, **roles):
    """Register a widget for repainting; roles map a widget option to a palette key."""
    THEMED.append((widget, roles))
    return widget


def use_style(widget, name):
    """Apply a custom ttk style. ttkbootstrap's own configure(style=...) would swap
    a name it did not build for a stock one, so go straight to Tk."""
    widget.tk.call(str(widget), "configure", "-style", name)
    return widget


def divider(parent):
    """A one-pixel rule: horizontal when packed to the top or bottom."""
    return themed(ui.plain(tk.Frame, parent, height=1), bg="hairline")


# The shell is inset by one pixel in overlay mode, so the root's own colour
# shows through as a hairline border around the floating panel.
shell = themed(ui.plain(tk.Frame, root), bg="frame")
shell.pack(fill=tk.BOTH, expand=True)

# Title bar -- doubles as the toolbar; in overlay mode it is also the drag handle.
titlebar = themed(ui.plain(tk.Frame, shell), bg="frame")
titlebar.pack(side=tk.TOP, fill=tk.X)
titlebar_row = themed(ui.plain(tk.Frame, titlebar), bg="frame")
titlebar_row.pack(fill=tk.X, padx=px(8), pady=px(5))

menu_button = themed(ui.IconButton(titlebar_row, look, "menu", None, "Menu"))
menu_button.pack(side=tk.LEFT)

brand_mark = themed(ui.plain(tk.Label, titlebar_row, bd=0), bg="frame")
brand_mark.pack(side=tk.LEFT, padx=px(8))

titlebar_label = themed(ui.plain(tk.Label, titlebar_row, text=APP_NAME, font=look.strong),
                        bg="frame", fg="text")
titlebar_label.pack(side=tk.LEFT)

window_button = themed(ui.IconButton(
    titlebar_row, look, "pin", None,
    lambda: ("Window mode" if overlay_mode.get() else "Overlay mode")
    + " (%s)" % GLOBAL_HOTKEYS["toggle_overlay"][2]))
window_button.pack(side=tk.RIGHT, padx=(px(2), 0))

notes_button = themed(ui.IconButton(titlebar_row, look, "notes", None, "Note list"))
notes_button.pack(side=tk.RIGHT, padx=(px(2), 0))

new_button = themed(ui.IconButton(titlebar_row, look, "add", None, "New note (Ctrl+N)", kind="accent"))
new_button.pack(side=tk.RIGHT, padx=(px(8), px(4)))

# Overlay-only: the OS frame is gone there, so VerTab draws its own.
collapse_button = themed(ui.IconButton(titlebar_row, look, "minimize", None, "Roll up"))
close_button = themed(ui.IconButton(titlebar_row, look, "close", None, "Quit", kind="close"))

divider(titlebar).pack(side=tk.BOTTOM, fill=tk.X)

# Status strip along the bottom: messages or counts, the note total, the resize grip.
# Packed before the body so it is never squeezed out when the window shrinks.
statusbar = themed(ui.plain(tk.Frame, shell), bg="frame")
statusbar.pack(side=tk.BOTTOM, fill=tk.X)
divider(statusbar).pack(side=tk.TOP, fill=tk.X)
status_row = themed(ui.plain(tk.Frame, statusbar), bg="frame")
status_row.pack(fill=tk.X, padx=(px(16), px(6)), pady=px(5))

status_label = themed(ui.plain(tk.Label, status_row, font=look.small, anchor="w"), bg="frame")
status_label.pack(side=tk.LEFT)

notes_label = themed(ui.plain(tk.Label, status_row, font=look.small), bg="frame", fg="faint")
notes_label.pack(side=tk.RIGHT, padx=(px(8), 0))

grip = use_style(ttk.Sizegrip(status_row), "Notes.TSizegrip")

# Body holds the two panes:
#   sidebar_frame for the search box and the note titles in a Treeview,
#   editor_frame for editing the open note.
body = themed(ui.plain(tk.Frame, shell), bg="frame")
body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

sidebar_frame = themed(ui.plain(tk.Frame, body, width=px(SIDEBAR_WIDTH)), bg="frame")
sidebar_frame.pack_propagate(False)

editor_frame = themed(ui.plain(tk.Frame, body), bg="editor")

# Sidebar: search pill, a small heading and the Treeview.
search_pill = themed(ui.SearchPill(sidebar_frame, look, search_var))
search_pill.pack(side=tk.TOP, fill=tk.X, padx=px(12), pady=(px(8), px(12)))
search_entry = search_pill.entry

list_heading = themed(ui.plain(tk.Frame, sidebar_frame), bg="frame")
list_heading.pack(side=tk.TOP, fill=tk.X, padx=px(22), pady=(0, px(4)))
themed(ui.plain(tk.Label, list_heading, text="NOTES", font=look.caps),
       bg="frame", fg="faint").pack(side=tk.LEFT)
list_count = themed(ui.plain(tk.Label, list_heading, font=look.small), bg="frame", fg="faint")
list_count.pack(side=tk.RIGHT)

list_frame = themed(ui.plain(tk.Frame, sidebar_frame), bg="frame")
list_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=(px(8), px(4)), pady=(0, px(8)))

list_scroll = use_style(ttk.Scrollbar(list_frame, orient=tk.VERTICAL), "Notes.Vertical.TScrollbar")
tree = use_style(ttk.Treeview(list_frame, show="tree", selectmode="browse",
                              yscrollcommand=lambda first, last: on_list_scroll(first, last)),
                 "Notes.Treeview")
list_scroll.configure(command=tree.yview)
tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

# The selected row is drawn as a pill with an accent bar, floating over the tree;
# the row under the pointer gets a quieter one.
selection_pill = themed(ui.RowPill(tree, look, bar=True))
hover_pill = themed(ui.RowPill(tree, look, fill="raised"))
empty_label = themed(ui.plain(tk.Label, tree, font=look.text, justify=tk.CENTER), bg="frame", fg="faint")

# Editor: a title line with the unsaved dot and actions, then the text.
editor_header = themed(ui.plain(tk.Frame, editor_frame), bg="editor")
editor_header.pack(side=tk.TOP, fill=tk.X, padx=(px(30), px(12)), pady=(px(14), px(2)))

delete_button = themed(ui.IconButton(editor_header, look, "delete", None, "Delete note",
                                     kind="danger", surface="editor"))
delete_button.pack(side=tk.RIGHT)
save_button = themed(ui.IconButton(editor_header, look, "save", None, "Save (Ctrl+S)", surface="editor"))
save_button.pack(side=tk.RIGHT, padx=(px(4), px(2)))
dirty_dot = themed(ui.plain(tk.Label, editor_header, bd=0, image=look.blank), bg="editor")
dirty_dot.pack(side=tk.RIGHT, padx=px(8))
ui.Tooltip(dirty_dot, look, lambda: "Unsaved changes" if dirty else "")

title_entry = themed(ui.plain(tk.Entry, editor_header, textvariable=title_var, font=look.title, bd=0,
                              highlightthickness=0, relief="flat"),
                     bg="editor", fg="text", insertbackground="text",
                     selectbackground="selection", selectforeground="text")
title_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=px(6))
themed(ui.Placeholder(title_entry, title_var, "Untitled note", look.title))

title_rule = divider(editor_frame)  # turns accent-coloured while the title has focus
title_rule.pack(side=tk.TOP, fill=tk.X, padx=px(30), pady=(px(6), 0))

text_frame = themed(ui.plain(tk.Frame, editor_frame), bg="editor")
text_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(px(4), px(6)))

content_text = themed(
    ui.plain(tk.Text, text_frame, wrap="word", width=20, height=5, relief="flat", borderwidth=0,
            highlightthickness=0, undo=True, font=look.body, padx=px(30), pady=px(8),
            spacing2=px(3), spacing3=px(2), insertwidth=px(2)),
    bg="editor", fg="text", insertbackground="accent",
    selectbackground="selection", selectforeground="text")
note_scroll = use_style(
    ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=content_text.yview),
    "Editor.Vertical.TScrollbar")
content_text.configure(yscrollcommand=lambda first, last: on_text_scroll(first, last))
content_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

measure = {"side": None}


def fit_measure(event=None):
    """Keep lines a comfortable length: beyond READING_WIDTH the title, rule and
    text share one centred column, while the scrollbar stays at the edge."""
    side = max(px(30), (editor_frame.winfo_width() - px(READING_WIDTH)) // 2)
    if side == measure["side"]:
        return
    measure["side"] = side
    editor_header.pack_configure(padx=(side, side - px(18)))
    title_rule.pack_configure(padx=side)
    content_text.configure(padx=side)


editor_frame.bind("<Configure>", fit_measure)

# A message on the left; the open note's counts and the note total on the right.
status = {"message": "", "words": "", "chars": "", "notes": ""}


def ellipsize(text, room):
    """Shorten text to `room` px with an ellipsis, so nothing clips."""
    if look.small.measure(text) <= room:
        return text
    while text and look.small.measure(text + "…") > room:
        text = text[:-1]
    return text + "…" if text else ""


def show_status():
    """Both sides stay visible. As the window narrows the right side sheds
    detail first (characters, then words), then the message is shortened."""
    room = status_row.winfo_width() - px(24)
    if grip.winfo_ismapped():
        room -= grip.winfo_reqwidth()
    parts = [p for p in (status["words"], status["chars"], status["notes"]) if p]
    choices = [parts, [p for p in parts if p != status["chars"]], parts[-1:]]
    right = next((" · ".join(c) for c in choices
                  if look.small.measure(" · ".join(c)) <= max(px(90), room * 0.6)),
                 " · ".join(choices[-1]))
    notes_label.configure(text=right)
    room -= look.small.measure(right)
    status_label.configure(text=ellipsize(status["message"], max(0, room)), fg=look.palette["text"])


def set_status(text, clear_after=2500):
    status["message"] = text
    show_status()
    if clear_after:
        root.after(clear_after, lambda: clear_status(text))


def clear_status(text):
    if status["message"] == text:
        status["message"] = ""
        show_status()


def plural(count, noun):
    return "%s %s%s" % (format(count, ","), noun, "" if count == 1 else "s")


def refresh_editor_status():
    """Counts, the unsaved dot and the Save button all follow the editor."""
    text = content_text.get("1.0", "end-1c")
    status["words"] = plural(len(text.split()), "word")
    status["chars"] = plural(len(text), "character")
    show_status()
    dirty_dot.configure(image=look.dot(px(8), look.palette["warning"]) if dirty else look.blank)
    save_button.set_kind("accent" if dirty else "ghost")


def refresh_list_chrome(shown):
    """Note totals, the list heading and the empty-list message follow the tree."""
    total = len(notes)
    status["notes"] = plural(total, "note")
    list_count.configure(text=str(total) if shown == total else "%d of %d" % (shown, total))
    if shown:
        empty_label.place_forget()
    else:
        empty_label.configure(text="No notes match “%s”" % search_var.get().strip() if total
                              else "No notes yet\nPress Ctrl+N to start one")
        empty_label.place(relx=0.5, rely=0.2, anchor="n")
    show_status()
    root.after_idle(place_selection)


def place_selection():
    """Float the selection pill over the selected row, or hide it."""
    selected = tree.selection()
    if selected:
        selection_pill.show(selected[0])
    else:
        selection_pill.hide()


def on_row_hover(event):
    selected = tree.selection()
    iid = tree.identify_row(event.y_root - tree.winfo_rooty())
    if iid and not (selected and iid == selected[0]):
        hover_pill.show(iid)
    else:
        hover_pill.hide()


def on_row_leave(event):
    if root.winfo_containing(event.x_root, event.y_root) not in (tree, hover_pill, selection_pill):
        hover_pill.hide()


def on_hover_press(event):
    """A click on the hover pill lands on the row beneath it."""
    iid = hover_pill.iid
    hover_pill.hide()
    tree.selection_set(iid)
    tree.focus(iid)
    tree.focus_set()


def on_list_scroll(first, last):
    hover_pill.hide()
    list_scroll.set(first, last)
    if float(first) <= 0.0 and float(last) >= 1.0:
        list_scroll.pack_forget()
    elif not list_scroll.winfo_ismapped():
        list_scroll.pack(side=tk.RIGHT, fill=tk.Y)
    place_selection()


def on_text_scroll(first, last):
    note_scroll.set(first, last)
    if float(first) <= 0.0 and float(last) >= 1.0:
        note_scroll.pack_forget()
    elif not note_scroll.winfo_ismapped():
        note_scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, px(4)))


# Right-click menu for cut / copy / paste, in the app's own style.
def show_context_menu(event):
    widget = event.widget
    menu = ui.PopupMenu(root, look, width=190)
    menu.add_command("Cut", lambda: widget.event_generate("<<Cut>>"), hint="Ctrl+X")
    menu.add_command("Copy", lambda: widget.event_generate("<<Copy>>"), hint="Ctrl+C")
    menu.add_command("Paste", lambda: widget.event_generate("<<Paste>>"), hint="Ctrl+V")
    menu.add_separator()
    menu.add_command("Select all", lambda: widget.event_generate("<<SelectAll>>"), hint="Ctrl+A")
    menu.popup(event.x_root, event.y_root, work_area_at(event.x_root, event.y_root))


# Right click for copy and paste
for field in (title_entry, search_entry, content_text):
    field.bind("<Button-3>", show_context_menu)


# -------------------------------
# Overlay mode
# -------------------------------
def style_ttk():
    """Restyle the few ttk widgets still in use: the note list, scrollbars, grip."""
    p = look.palette
    # ttkbootstrap's own configure() would register these names and then try to
    # rebuild them as stock styles on every theme change; ttk's base one does not.
    configure = functools.partial(ttk.Style.configure, style)
    configure("Notes.Treeview", background=p["frame"], fieldbackground=p["frame"],
              foreground=p["soft"], bordercolor=p["frame"], lightcolor=p["frame"],
              darkcolor=p["frame"], borderwidth=0, relief="flat", rowheight=px(38),
              font=look.text)
    # The selected row is painted by selection_pill, so the tree itself shows nothing.
    style.map("Notes.Treeview", background=[("selected", p["frame"])],
              foreground=[("selected", p["soft"])])
    style.layout("Notes.Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
        ("Treeitem.text", {"side": "left", "sticky": ""})]})])
    configure("Notes.Treeview.Item", padding=(px(18), 0, 0, 0))
    for name, trough in (("Notes", p["frame"]), ("Editor", p["editor"])):
        scrollbar = "%s.Vertical.TScrollbar" % name
        style.layout(scrollbar, [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        configure(scrollbar, troughcolor=trough, background=p["hover"], bordercolor=trough,
                  lightcolor=p["hover"], darkcolor=p["hover"], arrowsize=0,
                  width=px(8), gripcount=0, relief="flat")
        style.map(scrollbar, background=[("active", p["faint"]), ("pressed", p["muted"])],
                  lightcolor=[("active", p["faint"]), ("pressed", p["muted"])],
                  darkcolor=[("active", p["faint"]), ("pressed", p["muted"])])
    configure("Notes.TSizegrip", background=p["frame"])


def apply_frame_style():
    """Dark or light title bar, and rounded corners, on the real window frame."""
    root.update_idletasks()  # a window that was never shown has no final frame yet
    ui.style_native_frame(window_handle(), look.palette["dark"])


def apply_theme(name=None):
    """Switch the palette (and the ttk theme behind it) and repaint every widget.

    `name` is a theme setting; "auto" draws whichever palette Windows' light/dark
    mode calls for.  With no name, re-resolve the current setting.
    """
    setting = name or config["theme"]
    palette = ui.resolve_theme(setting)
    if palette != style.theme.name:
        try:
            style.theme_use(palette)
        except Exception as exc:
            set_status("Theme unavailable: %s" % exc)
            return
    if setting != config["theme"]:
        config["theme"] = setting
        schedule_config_save()
    look.use(palette)
    p = look.palette
    style_ttk()
    for widget, roles in THEMED:
        if roles:
            widget.configure(**{option: p[key] for option, key in roles.items()})
        if hasattr(widget, "recolor"):
            widget.recolor(p)
    brand_mark.configure(image=look.brand_mark(px(22), p["accent"], p["on_accent"]))
    root.configure(background=p["rim"])
    refresh_editor_status()
    root.after_idle(place_selection)
    apply_frame_style()


def follow_system_theme():
    """While the theme is "auto", pick up Windows light/dark switches live."""
    if config["theme"] == "auto" and ui.resolve_theme("auto") != style.theme.name:
        apply_theme()
    root.after(3000, follow_system_theme)


def effective_alpha():
    if not overlay_mode.get():
        return 1.0  # opacity is an overlay affordance; a normal window stays solid
    if click_through.get():
        return min(opacity, CLICK_THROUGH_MAX_ALPHA)
    return opacity


def refresh_alpha():
    root.attributes("-alpha", effective_alpha())


def set_opacity(value, announce=True):
    global opacity
    opacity = max(core.MIN_OPACITY, min(core.MAX_OPACITY, round(value, 2)))
    refresh_alpha()
    config["opacity"] = opacity
    if announce and overlay_mode.get():
        set_status("Opacity %d%%" % round(opacity * 100))
    schedule_config_save()


def nudge_opacity(delta):
    if overlay_mode.get():
        set_opacity(opacity + delta)


def apply_topmost():
    """The overlay is always on top; the window only when asked to."""
    root.attributes("-topmost", True if overlay_mode.get() else bool(topmost.get()))
    config["topmost"] = bool(topmost.get())
    schedule_config_save()


def default_overlay_geometry():
    """Top-right corner of the primary monitor, a comfortable panel size."""
    left, top, right, _bottom = work_area_at(0, 0)
    width, height = px(380), px(460)
    return "%dx%d+%d+%d" % (width, height, max(left, right - width - px(24)), top + px(48))


def default_window_geometry():
    left, top, right, bottom = work_area_at(0, 0)
    width = min(px(760), right - left)
    height = min(px(520), bottom - top)
    return "%dx%d+%d+%d" % (width, height, left + (right - left - width) // 2,
                            top + (bottom - top - height) // 2)


def set_chrome(on):
    """The overlay has no OS frame, so it adds its own buttons, grip and hairline border."""
    if on:
        close_button.pack(side=tk.RIGHT, before=window_button)
        collapse_button.pack(side=tk.RIGHT, before=window_button, padx=(0, px(2)))
        grip.pack(side=tk.RIGHT, before=notes_label)
    else:
        close_button.pack_forget()
        collapse_button.pack_forget()
        grip.pack_forget()
    window_button.set_icon("restore" if on else "pin")
    border = 1 if on else 0
    shell.pack_configure(padx=border, pady=border)


def apply_mode(initial=False):
    """Switch between the frameless overlay and the normal window."""
    on = overlay_mode.get()
    if not initial:
        expand_if_collapsed()
        # overlay_mode has already flipped: the current geometry belongs to
        # the mode we are leaving.
        remember_geometry(overlay=not on)
    if not on:
        click_through.set(False)  # click-through only makes sense on the overlay

    root.withdraw()
    root.overrideredirect(on)

    set_chrome(on)
    if on:
        root.geometry(on_screen(config["overlay_geometry"]) or default_overlay_geometry())
    else:
        root.geometry(on_screen(config["window_geometry"]) or default_window_geometry())

    config["overlay_mode"] = on
    apply_topmost()
    refresh_alpha()
    apply_frame_style()  # before the window is shown, or Windows keeps the old frame
    root.deiconify()
    apply_taskbar_visibility()
    apply_click_through()
    root.lift()
    if not click_through.get():
        root.focus_force()
    schedule_config_save()


def toggle_overlay_mode():
    overlay_mode.set(not overlay_mode.get())
    apply_mode()
    set_status("Overlay mode" if overlay_mode.get() else "Window mode")


def toggle_click_through():
    if not IS_WINDOWS:
        click_through.set(False)
        messagebox.showinfo("Click-through", "Click-through is only available on Windows.",
                            parent=root)
        return
    if click_through.get() and not overlay_mode.get():
        click_through.set(False)
        set_status("Click-through works in overlay mode")
        return
    if click_through.get() and not hotkey_available("toggle_click_through"):
        # Without its hotkey there would be no way to click the window again.
        click_through.set(False)
        set_status("Click-through needs the %s hotkey, which another app is using"
                   % GLOBAL_HOTKEYS["toggle_click_through"][2], clear_after=6000)
        return
    apply_click_through()
    if click_through.get():
        set_status("Click-through on — %s to turn it off"
                   % GLOBAL_HOTKEYS["toggle_click_through"][2], clear_after=4000)
    else:
        set_status("Click-through off")


# --- responsive layout ---
# Wide windows show the note list beside the editor.  Compact ones (the 380 px
# overlay) show one at a time: the editor, or the list when it is opened.
list_open = {"on": False}       # compact windows only; not saved
layout_state = {"shown": None}


def is_compact():
    return body.winfo_width() < px(COMPACT_WIDTH)


def sidebar_shown():
    return bool(layout_state["shown"] and layout_state["shown"][1])


def layout_body(event=None):
    width = body.winfo_width()
    if width <= 1:
        return  # not laid out yet; the first <Configure> brings us back
    compact = width < px(COMPACT_WIDTH)
    show_list = list_open["on"] if compact else sidebar_visible.get()
    shown = (compact, show_list)
    if shown == layout_state["shown"]:
        return
    layout_state["shown"] = shown
    sidebar_frame.pack_forget()
    editor_frame.pack_forget()
    if show_list:
        sidebar_frame.pack(side=tk.LEFT, fill=tk.BOTH if compact else tk.Y, expand=compact)
    if not (compact and show_list):
        editor_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    notes_button.set_active(show_list)
    root.after_idle(place_selection)


def toggle_sidebar():
    """Show or hide the note list; in a compact window it swaps with the editor."""
    if is_compact():
        list_open["on"] = not list_open["on"]
    else:
        sidebar_visible.set(not sidebar_visible.get())
        config["sidebar_visible"] = bool(sidebar_visible.get())
        schedule_config_save()
    layout_body()
    if sidebar_shown() and is_compact():
        search_entry.focus_set()


def reveal_list():
    """Make sure the note list is on screen (Ctrl+F needs somewhere to type)."""
    if not sidebar_shown():
        toggle_sidebar()


def reveal_editor():
    """In a compact window, give the screen back to the editor."""
    if is_compact() and list_open["on"]:
        list_open["on"] = False
        layout_body()


def toggle_visible():
    """Global show/hide -- the main reason the overlay is usable at all."""
    if root.state() == "withdrawn":
        root.deiconify()
        root.lift()
        if not click_through.get():
            root.focus_force()
            content_text.focus_set()
    else:
        quiet_autosave()
        root.withdraw()


def hide_window(event=None):
    """Esc / Hide. Only withdraw when the global hotkey can bring VerTab back;
    otherwise a hidden window (with no taskbar entry) would be unreachable."""
    if overlay_mode.get():
        if hotkey_available("toggle_visible"):
            quiet_autosave()
            root.withdraw()
        else:
            toggle_collapse()  # overrideredirect windows cannot be minimised
    else:
        root.iconify()
    return "break"


# --- dragging and resizing the frameless window ---
drag_origin = {"x": 0, "y": 0}


def start_move(event):
    drag_origin["x"] = event.x_root - root.winfo_x()
    drag_origin["y"] = event.y_root - root.winfo_y()


def do_move(event):
    if overlay_mode.get():
        root.geometry("+%d+%d" % (event.x_root - drag_origin["x"], event.y_root - drag_origin["y"]))


def end_move(event):
    if not overlay_mode.get():
        return
    snap_to_edges()
    remember_geometry()
    schedule_config_save()


def snap_to_edges():
    """Pull the overlay flush to an edge of its monitor's work area."""
    x, y = root.winfo_x(), root.winfo_y()
    w, h = root.winfo_width(), root.winfo_height()
    x, y = core.snap_position(x, y, w, h, work_area_at(x + w // 2, y + h // 2), px(SNAP_DISTANCE))
    root.geometry("+%d+%d" % (x, y))


collapsed = {"on": False, "height": 0}


def toggle_collapse():
    """Roll the overlay up into just its title bar."""
    if not overlay_mode.get():
        root.iconify()
        return
    if collapsed["on"]:
        expand_if_collapsed()
    else:
        remember_geometry()
        collapsed["height"] = root.winfo_height()
        body.pack_forget()
        statusbar.pack_forget()
        root.minsize(px(MIN_WIDTH), 1)  # the normal minimum height would stop the roll-up
        root.update_idletasks()
        root.geometry("%dx%d" % (root.winfo_width(), titlebar.winfo_reqheight() + 2))  # + border
        collapsed["on"] = True


def expand_if_collapsed():
    if not collapsed["on"]:
        return
    statusbar.pack(side=tk.BOTTOM, fill=tk.X)
    body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
    root.minsize(px(MIN_WIDTH), px(MIN_HEIGHT))
    root.geometry("%dx%d" % (root.winfo_width(), collapsed["height"]))
    collapsed["on"] = False


def remember_geometry(overlay=None):
    if collapsed["on"] or root.state() in ("withdrawn", "iconic", "zoomed"):
        return
    if overlay is None:
        overlay = overlay_mode.get()
    config["overlay_geometry" if overlay else "window_geometry"] = root.geometry()


config_save_job = {"id": None}


def schedule_config_save(delay=700):
    """Debounce config writes -- <Configure> fires constantly while dragging."""
    if config_save_job["id"]:
        root.after_cancel(config_save_job["id"])
    config_save_job["id"] = root.after(delay, lambda: storage.save_config(config))


def on_configure(event):
    if event.widget is root:
        remember_geometry()
        schedule_config_save()


def quit_app(event=None):
    if root.state() == "withdrawn":
        root.deiconify()  # any question below needs a visible parent
    if not resolve_unsaved("quit"):
        return
    remember_geometry()
    storage.save_config(config)
    if hotkey_thread:
        hotkey_thread.stop()
    root.destroy()


def on_taskbar_setting():
    config["hide_from_taskbar"] = bool(hide_from_taskbar.get())
    apply_taskbar_visibility()
    schedule_config_save()


def on_autosave_setting():
    config["autosave"] = bool(autosave_enabled.get())
    schedule_config_save()


# --- menu ---
def toggled(variable, then):
    """A menu command that flips a BooleanVar, then runs `then`."""
    def run():
        variable.set(not variable.get())
        then()
    return run


def build_menu():
    overlay = overlay_mode.get()
    menu = ui.PopupMenu(root, look)

    menu.add_check("Overlay mode", overlay, toggled(overlay_mode, apply_mode),
                   hint=GLOBAL_HOTKEYS["toggle_overlay"][2])
    menu.add_check("Always on top", topmost.get() or overlay, toggled(topmost, apply_topmost),
                   enabled=not overlay)
    menu.add_check("Click-through", click_through.get(),
                   toggled(click_through, toggle_click_through),
                   hint=GLOBAL_HOTKEYS["toggle_click_through"][2],
                   enabled=overlay and IS_WINDOWS)
    menu.add_check("Note list", sidebar_shown(), toggle_sidebar)
    if IS_WINDOWS:
        menu.add_check("Hide from taskbar", hide_from_taskbar.get(),
                       toggled(hide_from_taskbar, on_taskbar_setting))
    menu.add_check("Autosave", autosave_enabled.get(), toggled(autosave_enabled, on_autosave_setting))
    if overlay:
        menu.add_stepper("Opacity", lambda: "%d%%" % round(opacity * 100),
                         lambda direction: nudge_opacity(direction * OPACITY_STEP))

    menu.add_separator()
    menu.add_heading("Theme")
    for name, label in ui.THEMES.items():
        menu.add_choice(label, name == config["theme"], lambda n=name: apply_theme(n))

    menu.add_separator()
    menu.add_command("New note", new_note, hint="Ctrl+N")
    menu.add_command("Save note", save_note, hint="Ctrl+S")
    menu.add_command("Search notes", focus_search, hint="Ctrl+F")
    menu.add_command("Info / hotkeys", show_info, hint="F1")
    menu.add_separator()
    menu.add_command("Hide", hide_window, hint="Esc")
    menu.add_command("Quit", quit_app)
    return menu


def popup_menu(event=None):
    if event is not None:
        x, y = event.x_root, event.y_root
    else:
        x = menu_button.winfo_rootx()
        y = menu_button.winfo_rooty() + menu_button.winfo_height() + px(4)
    build_menu().popup(x, y, work_area_at(x, y))


def hotkey_click_through():
    click_through.set(not click_through.get())
    toggle_click_through()


HOTKEY_ACTIONS = {
    "toggle_visible": toggle_visible,
    "toggle_overlay": toggle_overlay_mode,
    "toggle_click_through": hotkey_click_through,
    "opacity_up": lambda: nudge_opacity(OPACITY_STEP),
    "opacity_down": lambda: nudge_opacity(-OPACITY_STEP),
}

# -------------------------------
# Wire everything up
# -------------------------------
menu_button.command = popup_menu
new_button.command = new_note
notes_button.command = toggle_sidebar
collapse_button.command = toggle_collapse
window_button.command = toggle_overlay_mode
close_button.command = quit_app
save_button.command = save_note
delete_button.command = delete_note

for mover in (titlebar, titlebar_label):
    mover.bind("<Button-1>", start_move)
    mover.bind("<B1-Motion>", do_move)
    mover.bind("<ButtonRelease-1>", end_move)
titlebar.bind("<Double-Button-1>", lambda e: toggle_collapse() if overlay_mode.get() else None)
titlebar.bind("<Button-3>", popup_menu)
titlebar_label.bind("<Button-3>", popup_menu)

content_text.bind("<<Modified>>", on_text_modified)
title_var.trace_add("write", on_title_changed)
title_entry.bind("<Return>", lambda e: content_text.focus_set() or "break")
title_entry.bind("<FocusIn>", lambda e: title_rule.configure(bg=look.palette["accent"]))
title_entry.bind("<FocusOut>", lambda e: title_rule.configure(bg=look.palette["hairline"]))

search_var.trace_add("write", on_search_changed)
search_entry.bind("<Escape>", on_search_escape)
search_entry.bind("<Down>", search_to_list)
search_entry.bind("<Return>", open_first_match)
# Bind selection event to load note.
tree.bind("<<TreeviewSelect>>", on_tree_select)
tree.bind("<Up>", on_list_up)
tree.bind("<Return>", on_list_enter)
tree.bind("<ButtonRelease-1>", on_list_click)
hover_pill.bind("<ButtonPress-1>", on_hover_press)
for surface in (tree, hover_pill, selection_pill):
    surface.bind("<Motion>", on_row_hover)
    surface.bind("<Leave>", on_row_leave)
for pill in (hover_pill, selection_pill):
    pill.bind("<ButtonRelease-1>", lambda e: reveal_editor())
tree.bind("<Configure>", lambda e: place_selection())
for scrolled in (tree, hover_pill, selection_pill, empty_label):
    scrolled.bind("<MouseWheel>", lambda e: tree.yview_scroll(-1 if e.delta > 0 else 1, "units"))
body.bind("<Configure>", layout_body)
statusbar.bind("<Configure>", lambda e: show_status())

for key in ("s", "S"):
    root.bind("<Control-%s>" % key, save_note)
for key in ("n", "N"):
    root.bind("<Control-%s>" % key, new_note)
for key in ("f", "F"):
    root.bind("<Control-%s>" % key, focus_search)
root.bind("<Control-Shift-O>", lambda e: toggle_overlay_mode())
root.bind("<F1>", lambda e: show_info())
root.bind("<Escape>", hide_window)
root.bind("<Configure>", on_configure)
root.protocol("WM_DELETE_WINDOW", quit_app)

# -------------------------------
# On startup, load notes and populate the sidebar.
# -------------------------------
update_treeview()
apply_theme()
apply_mode(initial=True)
start_hotkeys()
follow_system_theme()
load_into_editor(None)

# --open TITLE opens a note at launch (handy for desktop shortcuts).
if "--open" in sys.argv[:-1]:
    requested = sys.argv[sys.argv.index("--open") + 1]
    if requested in notes:
        load_into_editor(requested)
        select_title(requested)

if notes_warning:
    root.after(300, lambda: messagebox.showwarning("Notebook recovered", notes_warning, parent=root))
elif overlay_mode.get() and IS_WINDOWS:
    set_status("Esc hides VerTab — %s brings it back"
               % GLOBAL_HOTKEYS["toggle_visible"][2], clear_after=5000)

root.mainloop()

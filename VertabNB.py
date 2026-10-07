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
import queue
import sys
import threading
import traceback
import tkinter as tk
from tkinter import ttk, messagebox

from ttkbootstrap import Style

import vertab_core as core
from vertab_core import IS_WINDOWS

APP_NAME = core.APP_NAME
APP_TITLE = "VerTab Notebook"

SNAP_DISTANCE = 18          # px from a work-area edge before the overlay snaps to it
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

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

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
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
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


def report_hotkey_conflicts():
    if not hotkey_thread.ready.is_set():
        root.after(200, report_hotkey_conflicts)
        return
    taken = [GLOBAL_HOTKEYS[n][2] for n in GLOBAL_HOTKEYS if n not in hotkey_thread.active]
    if taken:
        storage.log("hotkeys already in use by another app: " + ", ".join(taken))
        set_status("In use by another app: " + ", ".join(taken), clear_after=8000)


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
# Update the sidebar Treeview with the note titles.
def update_treeview():
    tree.delete(*tree.get_children())
    iid_by_title.clear()
    title_by_iid.clear()
    for title in sorted(notes, key=str.casefold):
        iid = tree.insert("", tk.END, text=title)
        iid_by_title[title] = iid
        title_by_iid[iid] = title


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


# When a note is selected in the sidebar, load it into the editor.
def on_tree_select(event):
    selected = tree.selection()
    if not selected:
        return
    title = title_by_iid.get(selected[0])
    if title is None or title == current_note_title:
        return
    if not resolve_unsaved("open another note"):
        select_title(current_note_title)
        return
    load_into_editor(title)
    select_title(title)


# Create a new (empty) note to edit.
def new_note(event=None):
    if resolve_unsaved("start a new note"):
        load_into_editor(None)
        select_title(None)
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


def on_title_changed(*_args):
    global dirty
    dirty = True


def mark_clean():
    global dirty
    dirty = False
    content_text.edit_modified(False)


# Info button popup
def show_info():
    lines = [
        "VerTab Notebook",
        "",
        "Right click in the title or content box for cut / copy / paste.",
        "",
        "Overlay mode floats VerTab on top of other apps: drag the title bar to",
        "move it, drag the bottom-right corner to resize, and use the menu",
        "button for opacity, themes and click-through.",
        "",
        "Hotkeys%s:" % ("" if IS_WINDOWS else " (while VerTab is focused)"),
    ]
    for name, (_mods, _vk, label) in GLOBAL_HOTKEYS.items():
        taken = IS_WINDOWS and hotkey_thread and hotkey_thread.ready.is_set() \
            and name not in hotkey_thread.active
        lines.append("  %-14s %s%s" % (label, HOTKEY_HELP[name],
                                       "  (in use by another app)" if taken else ""))
    lines += [
        "  %-14s %s" % ("Ctrl+S", "save the open note"),
        "  %-14s %s" % ("Ctrl+N", "new note"),
        "  %-14s %s" % ("Esc", "hide the overlay"),
        "",
        "Notes are stored in:",
        "  " + storage.path(core.NOTES_FILE),
    ]
    messagebox.showinfo("Info", "\n".join(lines), parent=root)


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


root.minsize(px(320), px(240))


def report_error(exc_type, exc, tb):
    """Tk callback errors: the windowed exe has no console, so log and show."""
    storage.log("".join(traceback.format_exception(exc_type, exc, tb)))
    messagebox.showerror("Unexpected error", "%s\n\nDetails were written to:\n%s"
                         % (exc, storage.path(core.LOG_FILE)), parent=root)


root.report_callback_exception = report_error

# Use ttkbootstrap style.
style = Style()
try:
    style.theme_use(config["theme"])
except Exception:  # unknown or broken theme name in config.json
    config["theme"] = core.DEFAULT_CONFIG["theme"]
    style.theme_use(config["theme"])

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
opacity = config["opacity"]

# Custom title bar -- only shown in overlay mode, where the OS frame is gone.
titlebar = ttk.Frame(root, padding=(6, 4))

menu_button = ttk.Button(titlebar, text="☰", width=3, style="secondary.TButton")
menu_button.pack(side=tk.LEFT)

titlebar_label = ttk.Label(titlebar, text=APP_NAME, font=("TkDefaultFont", 10, "bold"))
titlebar_label.pack(side=tk.LEFT, padx=8)

close_button = ttk.Button(titlebar, text="✕", width=3, style="danger.TButton")
close_button.pack(side=tk.RIGHT)

window_button = ttk.Button(titlebar, text="❐", width=3, style="secondary.TButton")
window_button.pack(side=tk.RIGHT, padx=(0, 4))

collapse_button = ttk.Button(titlebar, text="–", width=3, style="secondary.TButton")
collapse_button.pack(side=tk.RIGHT, padx=(0, 4))

# Status strip along the bottom: short messages plus the overlay resize grip.
# Packed before the body so it is never squeezed out when the window shrinks.
statusbar = ttk.Frame(root, padding=(8, 2))
statusbar.pack(side=tk.BOTTOM, fill=tk.X)

status_label = ttk.Label(statusbar, text="", font=("TkDefaultFont", 8))
status_label.pack(side=tk.LEFT)

grip = ttk.Sizegrip(statusbar)

# Body holds the two original frames:
#   sidebar_frame for the note titles in a Treeview,
#   editor_frame for editing note content.
body = ttk.Frame(root)
body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

sidebar_frame = ttk.Frame(body, padding=(10, 10))
sidebar_frame.pack(side=tk.LEFT, fill=tk.Y)

editor_frame = ttk.Frame(body, padding=(10, 10))
editor_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

# Sidebar: Add a label and the Treeview.
sidebar_label = ttk.Label(sidebar_frame, text="Saved Notes", font=("TkDefaultFont", 12, "bold"))
sidebar_label.pack(pady=(0, 5))

tree = ttk.Treeview(sidebar_frame, show="tree", height=20, selectmode="browse")
tree.pack(fill=tk.Y, expand=True)

# Bind selection event to load note.
tree.bind("<<TreeviewSelect>>", on_tree_select)

# Editor frame: Create fields for note title and content.
title_label = ttk.Label(editor_frame, text="Title:")
title_label.grid(row=0, column=0, padx=5, pady=5, sticky="W")

title_entry = ttk.Entry(editor_frame, width=50, textvariable=title_var)
title_entry.grid(row=0, column=1, padx=5, pady=5, sticky="EW")

content_label = ttk.Label(editor_frame, text="Content:")
content_label.grid(row=1, column=0, padx=5, pady=5, sticky="NW")

content_text = tk.Text(editor_frame, wrap="word", width=50, height=20,
                       relief="flat", borderwidth=1, undo=True)
content_text.grid(row=1, column=1, padx=5, pady=5, sticky="NSEW")

# Make the editor frame expandable.
editor_frame.columnconfigure(1, weight=1)
editor_frame.rowconfigure(1, weight=1)

# Add the control buttons in a separate frame at the bottom of the editor.
button_frame = ttk.Frame(editor_frame)
button_frame.grid(row=2, column=1, pady=10, sticky="E")

# buttons
info_button = ttk.Button(button_frame, text="Info", command=show_info, style="warning.TButton")
info_button.pack(side=tk.LEFT, padx=(0, 5))

new_button = ttk.Button(button_frame, text="New Note", command=new_note, style="info.TButton")
new_button.pack(side=tk.LEFT, padx=(0, 5))

save_button = ttk.Button(button_frame, text="Save", command=save_note, style="secondary.TButton")
save_button.pack(side=tk.LEFT, padx=(0, 5))

delete_button = ttk.Button(button_frame, text="Delete", command=delete_note, style="primary.TButton")
delete_button.pack(side=tk.LEFT)


def set_status(text, clear_after=2500):
    status_label.configure(text=text)
    if clear_after:
        root.after(clear_after,
                   lambda: status_label.configure(text="")
                   if status_label.cget("text") == text else None)


# copy and paste menu
def show_context_menu(event):
    widget = event.widget
    menu = tk.Menu(widget, tearoff=0)
    menu.add_command(label="Cut", command=lambda w=widget: w.event_generate("<<Cut>>"))
    menu.add_command(label="Copy", command=lambda w=widget: w.event_generate("<<Copy>>"))
    menu.add_command(label="Paste", command=lambda w=widget: w.event_generate("<<Paste>>"))
    menu.add_separator()
    menu.add_command(label="Select All", command=lambda w=widget: w.event_generate("<<SelectAll>>"))
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        menu.grab_release()


# Right click for copy and paste
title_entry.bind("<Button-3>", show_context_menu)
content_text.bind("<Button-3>", show_context_menu)


# -------------------------------
# Overlay mode
# -------------------------------
def apply_theme(name=None):
    """Switch the ttk theme and restyle the plain tk widgets to match."""
    if name and name != style.theme.name:
        try:
            style.theme_use(name)
        except Exception as exc:
            set_status("Theme unavailable: %s" % exc)
            return
        config["theme"] = name
        schedule_config_save()
    colors = style.colors
    content_text.configure(
        background=colors.inputbg, foreground=colors.inputfg,
        insertbackground=colors.fg,
        selectbackground=colors.selectbg, selectforeground=colors.selectfg,
        highlightthickness=1, highlightbackground=colors.border,
        highlightcolor=colors.border,
    )
    root.configure(background=colors.bg)


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

    if on:
        slaves = root.pack_slaves()
        titlebar.pack(side=tk.TOP, fill=tk.X, **({"before": slaves[0]} if slaves else {}))
        grip.pack(side=tk.RIGHT)
        root.geometry(on_screen(config["overlay_geometry"]) or default_overlay_geometry())
    else:
        titlebar.pack_forget()
        grip.pack_forget()
        root.geometry(on_screen(config["window_geometry"]) or default_window_geometry())

    config["overlay_mode"] = on
    apply_topmost()
    refresh_alpha()
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


def toggle_sidebar():
    if sidebar_visible.get():
        sidebar_frame.pack(side=tk.LEFT, fill=tk.Y, before=editor_frame)
    else:
        sidebar_frame.pack_forget()
    config["sidebar_visible"] = bool(sidebar_visible.get())
    schedule_config_save()


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
        root.update_idletasks()
        root.geometry("%dx%d" % (root.winfo_width(), titlebar.winfo_reqheight()))
        collapsed["on"] = True


def expand_if_collapsed():
    if not collapsed["on"]:
        return
    statusbar.pack(side=tk.BOTTOM, fill=tk.X)
    body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
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


# --- overlay menu ---
def build_overlay_menu():
    menu = tk.Menu(root, tearoff=0)

    menu.add_checkbutton(label="Overlay mode", variable=overlay_mode, command=apply_mode)
    menu.add_checkbutton(label="Always on top", variable=topmost, command=apply_topmost,
                         state=tk.DISABLED if overlay_mode.get() else tk.NORMAL)
    menu.add_checkbutton(label="Click-through", variable=click_through,
                         command=toggle_click_through,
                         state=tk.NORMAL if overlay_mode.get() and IS_WINDOWS else tk.DISABLED)
    menu.add_checkbutton(label="Show sidebar", variable=sidebar_visible, command=toggle_sidebar)
    if IS_WINDOWS:
        menu.add_checkbutton(label="Hide from taskbar", variable=hide_from_taskbar,
                             command=on_taskbar_setting)
    menu.add_checkbutton(label="Autosave", variable=autosave_enabled, command=on_autosave_setting)

    opacity_menu = tk.Menu(menu, tearoff=0)
    for percent in (100, 90, 80, 70, 60, 50, 35):
        opacity_menu.add_command(label="%d%%" % percent,
                                 command=lambda p=percent: set_opacity(p / 100))
    menu.add_cascade(label="Opacity", menu=opacity_menu,
                     state=tk.NORMAL if overlay_mode.get() else tk.DISABLED)

    theme_menu = tk.Menu(menu, tearoff=0)
    for name in ("journal", "litera", "flatly", "sandstone",
                 "darkly", "superhero", "cyborg", "solar"):
        theme_menu.add_command(label=name, command=lambda n=name: apply_theme(n))
    menu.add_cascade(label="Theme", menu=theme_menu)

    menu.add_separator()
    menu.add_command(label="New note", command=new_note)
    menu.add_command(label="Save note", command=save_note)
    menu.add_command(label="Info / hotkeys", command=show_info)
    menu.add_separator()
    menu.add_command(label="Hide (Esc)", command=hide_window)
    menu.add_command(label="Quit", command=quit_app)
    return menu


def popup_menu(event=None):
    menu = build_overlay_menu()
    if event is not None:
        x, y = event.x_root, event.y_root
    else:
        x = menu_button.winfo_rootx()
        y = menu_button.winfo_rooty() + menu_button.winfo_height()
    try:
        menu.tk_popup(x, y)
    finally:
        menu.grab_release()


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
menu_button.configure(command=popup_menu)
collapse_button.configure(command=toggle_collapse)
window_button.configure(command=toggle_overlay_mode)
close_button.configure(command=quit_app)

for mover in (titlebar, titlebar_label):
    mover.bind("<Button-1>", start_move)
    mover.bind("<B1-Motion>", do_move)
    mover.bind("<ButtonRelease-1>", end_move)
titlebar.bind("<Double-Button-1>", lambda e: toggle_collapse())
titlebar.bind("<Button-3>", popup_menu)
titlebar_label.bind("<Button-3>", popup_menu)

content_text.bind("<<Modified>>", on_text_modified)
title_var.trace_add("write", on_title_changed)

for key in ("s", "S"):
    root.bind("<Control-%s>" % key, save_note)
for key in ("n", "N"):
    root.bind("<Control-%s>" % key, new_note)
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
toggle_sidebar()
apply_mode(initial=True)
start_hotkeys()
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

#!/usr/bin/env python
"""VerTab Notebook -- vertical-tab notes that can float on top of other apps.

Two ways to run it:

  Window mode   a normal resizable window (what the app always did).
  Overlay mode  a frameless, always-on-top, semi-transparent panel that sits
                over whatever you are working in, with global hotkeys so you
                can show, hide and click through it without leaving the other
                app.

Notes and settings live in a per-user folder (%APPDATA%\\VerTab on Windows) so
the packaged executable keeps working wherever it is started from.  Pass
--portable to keep them next to the executable instead.

Hotkeys (global on Windows -- they work while another app is focused):
  Ctrl+Alt+N    show / hide VerTab
  Ctrl+Alt+M    switch between overlay and window mode
  Ctrl+Alt+T    click-through on / off
  Ctrl+Alt+Up   more opaque        Ctrl+Alt+Down  more transparent
"""
import json
import os
import queue
import shutil
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from ttkbootstrap import Style

APP_NAME = "VerTab"
APP_TITLE = "VerTab Notebook"
NOTES_FILE = "notes.json"
CONFIG_FILE = "config.json"

IS_WINDOWS = sys.platform == "win32"
FROZEN = getattr(sys, "frozen", False)

MIN_WIDTH, MIN_HEIGHT = 320, 240
SNAP_DISTANCE = 18          # px from a screen edge before the overlay snaps to it
MIN_OPACITY, MAX_OPACITY = 0.25, 1.0
OPACITY_STEP = 0.05

# Global dictionary for notes and a variable for current note title.
notes = {}
current_note_title = None
dirty = False               # editor has unsaved edits

# -------------------------------
# Where things are stored
# -------------------------------
# A frozen executable can be launched from anywhere, so "notes.json" relative
# to the working directory is not good enough -- resolve real paths up front.

portable = "--portable" in sys.argv


def app_dir():
    """Folder the app lives in (the executable's folder when frozen)."""
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(*parts):
    """Path to a bundled read-only resource (handles the PyInstaller bundle)."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)


def data_dir():
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


def data_path(name):
    return os.path.join(data_dir(), name)


def prepare_data_dir():
    """Create the data folder and adopt a notes.json left next to the script."""
    os.makedirs(data_dir(), exist_ok=True)
    target = data_path(NOTES_FILE)
    legacy = os.path.join(app_dir(), NOTES_FILE)
    if not os.path.exists(target) and os.path.exists(legacy):
        if os.path.abspath(legacy) != os.path.abspath(target):
            shutil.copy2(legacy, target)


# -------------------------------
# Notes + settings on disk
# -------------------------------
DEFAULT_CONFIG = {
    "overlay_mode": False,
    "topmost": True,
    "opacity": 0.92,
    "theme": "journal",
    "window_geometry": "700x500",
    "overlay_geometry": "",
    "sidebar_visible": True,
    "hide_from_taskbar": True,
    "autosave": True,
}

config = dict(DEFAULT_CONFIG)


# Load saved notes from JSON.
def load_notes_from_file():
    global notes
    try:
        with open(data_path(NOTES_FILE), "r", encoding="utf-8") as f:
            notes = json.load(f)
    except (FileNotFoundError, ValueError):
        notes = {}


# Save notes dictionary to file.
def save_notes_to_file():
    with open(data_path(NOTES_FILE), "w", encoding="utf-8") as f:
        json.dump(notes, f, indent=4, ensure_ascii=False)


def load_config():
    global config
    config = dict(DEFAULT_CONFIG)
    try:
        with open(data_path(CONFIG_FILE), "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            config.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
    except (FileNotFoundError, ValueError):
        pass
    # Command line wins over the saved mode.
    if "--overlay" in sys.argv:
        config["overlay_mode"] = True
    if "--window" in sys.argv:
        config["overlay_mode"] = False


def save_config():
    try:
        with open(data_path(CONFIG_FILE), "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
    except OSError:
        pass  # settings are a convenience; never block the app on them


# -------------------------------
# Windows window-style helpers
# -------------------------------
# Click-through, no-activate and hiding from the taskbar are not exposed by
# Tk, so they are set straight on the window's extended style.  Everything in
# this section is a no-op off Windows.
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TOOLWINDOW = 0x00000080

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


def window_handle():
    """HWND of the real top-level window (not Tk's inner child window)."""
    if not IS_WINDOWS:
        return None
    try:
        return int(root.wm_frame(), 16)
    except (ValueError, tk.TclError):
        return root.winfo_id()


def update_ex_style(add=0, remove=0):
    """Add/remove extended window style bits; returns the new style."""
    if not IS_WINDOWS:
        return 0
    hwnd = window_handle()
    style_bits = _get_long(hwnd, GWL_EXSTYLE)
    style_bits = (style_bits | add) & ~remove
    _set_long(hwnd, GWL_EXSTYLE, _long(style_bits))
    return style_bits


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
        self.registered = []

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        ids = {}
        for index, (name, (mods, vk, _label)) in enumerate(GLOBAL_HOTKEYS.items(), start=1):
            if user32.RegisterHotKey(None, index, mods | MOD_NOREPEAT, vk):
                ids[index] = name
                self.registered.append(index)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                name = ids.get(msg.wParam)
                if name:
                    HOTKEY_QUEUE.put(name)
        for index in self.registered:
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
        poll_hotkeys()


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
    root.after(80, poll_hotkeys)


# -------------------------------
# Note operations
# -------------------------------
# Update the sidebar Treeview with the note titles.
def update_treeview():
    tree.delete(*tree.get_children())
    for title in sorted(notes.keys()):
        tree.insert("", tk.END, iid=title, text=title)


# When a note is selected in the sidebar, load it into the editor.
def on_tree_select(event):
    global current_note_title
    selected = tree.selection()
    if not selected:
        return
    title = selected[0]
    if title == current_note_title:
        return
    autosave_current()
    current_note_title = title
    # Set the two editor fields.
    title_entry.delete(0, tk.END)
    title_entry.insert(0, title)
    content_text.delete("1.0", tk.END)
    content_text.insert(tk.END, notes.get(title, ""))
    mark_clean()


# Create a new (empty) note to edit.
def new_note(event=None):
    global current_note_title
    autosave_current()
    current_note_title = None
    title_entry.delete(0, tk.END)
    content_text.delete("1.0", tk.END)
    # Also clear Treeview selection.
    tree.selection_remove(tree.selection())
    title_entry.focus_set()
    mark_clean()
    return "break"


# Save/update the current note.
def save_note(event=None):
    global current_note_title
    title = title_entry.get().strip()
    content = content_text.get("1.0", tk.END).rstrip()
    if not title:
        messagebox.showerror("Error", "Title cannot be empty!", parent=root)
        return "break"

    # If renaming an existing note, remove the old key.
    if current_note_title and current_note_title != title:
        notes.pop(current_note_title, None)

    notes[title] = content
    current_note_title = title
    save_notes_to_file()
    update_treeview()
    # Optionally, select the updated note in the sidebar.
    tree.selection_set(title)
    mark_clean()
    set_status("Saved “%s”" % title)
    return "break"


# Delete the currently loaded note.
def delete_note():
    global current_note_title
    if not current_note_title:
        messagebox.showerror("Error", "No note selected to delete!", parent=root)
        return
    confirm = messagebox.askyesno(
        "Delete Note",
        "Are you sure you want to delete '%s'?" % current_note_title,
        parent=root,
    )
    if confirm:
        notes.pop(current_note_title, None)
        save_notes_to_file()
        update_treeview()
        mark_clean()
        new_note()  # clear the editor


def mark_dirty(event=None):
    global dirty
    dirty = True
    content_text.edit_modified(False)


def mark_clean():
    global dirty
    dirty = False
    content_text.edit_modified(False)


def autosave_current():
    """Quietly persist the open note -- used when hiding, switching or quitting."""
    if not (config["autosave"] and dirty):
        return
    title = title_entry.get().strip()
    if not title:
        return
    content = content_text.get("1.0", tk.END).rstrip()
    if current_note_title and current_note_title != title:
        notes.pop(current_note_title, None)
    notes[title] = content
    save_notes_to_file()
    mark_clean()


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
        lines.append("  %-14s %s" % (label, HOTKEY_HELP[name]))
    lines += [
        "  %-14s %s" % ("Ctrl+S", "save the open note"),
        "  %-14s %s" % ("Ctrl+N", "new note"),
        "  %-14s %s" % ("Esc", "hide the overlay"),
        "",
        "Notes are stored in:",
        "  " + data_path(NOTES_FILE),
    ]
    messagebox.showinfo("Info", "\n".join(lines), parent=root)


# -------------------------------
# Create the main window
# -------------------------------
prepare_data_dir()
load_config()

root = tk.Tk()
root.title(APP_TITLE)
root.minsize(MIN_WIDTH, MIN_HEIGHT)
root.withdraw()  # stay hidden until the mode is applied, to avoid a flash

# Use ttkbootstrap style.
style = Style(theme=config["theme"])

try:
    root.iconbitmap(resource_path("assets", "vertab.ico"))
except (tk.TclError, OSError):
    pass  # icon is cosmetic; a missing file must not stop the app

overlay_mode = tk.BooleanVar(value=config["overlay_mode"])
topmost = tk.BooleanVar(value=config["topmost"])
click_through = tk.BooleanVar(value=False)  # always starts off, see README
sidebar_visible = tk.BooleanVar(value=config["sidebar_visible"])
hide_from_taskbar = tk.BooleanVar(value=config["hide_from_taskbar"])
autosave_enabled = tk.BooleanVar(value=config["autosave"])
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

tree = ttk.Treeview(sidebar_frame, show="tree", height=20)
tree.pack(fill=tk.Y, expand=True)

# Bind selection event to load note.
tree.bind("<<TreeviewSelect>>", on_tree_select)

# Editor frame: Create fields for note title and content.
title_label = ttk.Label(editor_frame, text="Title:")
title_label.grid(row=0, column=0, padx=5, pady=5, sticky="W")

title_entry = ttk.Entry(editor_frame, width=50)
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

# Status strip along the bottom: short messages plus the overlay resize grip.
statusbar = ttk.Frame(root, padding=(8, 2))
statusbar.pack(side=tk.BOTTOM, fill=tk.X)

status_label = ttk.Label(statusbar, text="", font=("TkDefaultFont", 8))
status_label.pack(side=tk.LEFT)

grip = ttk.Sizegrip(statusbar)


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
        style.theme_use(name)
        config["theme"] = name
    colors = style.colors
    content_text.configure(
        background=colors.inputbg, foreground=colors.inputfg,
        insertbackground=colors.fg,
        selectbackground=colors.selectbg, selectforeground=colors.selectfg,
        highlightthickness=1, highlightbackground=colors.border,
        highlightcolor=colors.border,
    )
    root.configure(background=colors.bg)


def set_opacity(value, announce=True):
    global opacity
    opacity = max(MIN_OPACITY, min(MAX_OPACITY, round(value, 2)))
    # Opacity is an overlay affordance; a normal window stays solid.
    root.attributes("-alpha", opacity if overlay_mode.get() else 1.0)
    config["opacity"] = opacity
    if announce and overlay_mode.get():
        set_status("Opacity %d%%" % round(opacity * 100))
    schedule_config_save()


def nudge_opacity(delta):
    if not overlay_mode.get():
        return
    set_opacity(opacity + delta)


def apply_topmost():
    root.attributes("-topmost", bool(topmost.get()))
    config["topmost"] = bool(topmost.get())
    schedule_config_save()


def default_overlay_geometry():
    """Top-right corner of the screen, a comfortable panel size."""
    width, height = 380, 460
    x = max(0, root.winfo_screenwidth() - width - 24)
    return "%dx%d+%d+%d" % (width, height, x, 48)


def apply_mode(initial=False):
    """Switch between the frameless overlay and the normal window."""
    on = overlay_mode.get()
    if not initial:
        # Remember where the mode we are leaving had its window.
        remember_geometry()

    root.withdraw()
    root.overrideredirect(on)

    if on:
        titlebar.pack(side=tk.TOP, fill=tk.X, before=body)
        grip.pack(side=tk.RIGHT)
        root.geometry(config["overlay_geometry"] or default_overlay_geometry())
        root.attributes("-alpha", opacity)
        root.attributes("-topmost", True)
        topmost.set(True)
    else:
        titlebar.pack_forget()
        grip.pack_forget()
        root.geometry(config["window_geometry"] or "700x500")
        root.attributes("-alpha", 1.0)
        root.attributes("-topmost", bool(topmost.get()))

    config["overlay_mode"] = on
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
        messagebox.showinfo("Click-through", "Click-through is only available on Windows.",
                            parent=root)
        click_through.set(False)
        return
    apply_click_through()
    if click_through.get():
        label = GLOBAL_HOTKEYS["toggle_click_through"][2]
        set_status("Click-through on — %s to turn it off" % label, clear_after=4000)
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
        autosave_current()
        root.withdraw()


def hide_window(event=None):
    autosave_current()
    root.withdraw()
    return "break"


# --- dragging and resizing the frameless window ---
drag_origin = {"x": 0, "y": 0}


def start_move(event):
    drag_origin["x"] = event.x_root - root.winfo_x()
    drag_origin["y"] = event.y_root - root.winfo_y()


def do_move(event):
    if not overlay_mode.get():
        return
    root.geometry("+%d+%d" % (event.x_root - drag_origin["x"], event.y_root - drag_origin["y"]))


def end_move(event):
    if not overlay_mode.get():
        return
    snap_to_edges()
    remember_geometry()
    schedule_config_save()


def snap_to_edges():
    """Pull the overlay flush to a screen edge when it is dropped near one."""
    x, y = root.winfo_x(), root.winfo_y()
    w, h = root.winfo_width(), root.winfo_height()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    if abs(x) <= SNAP_DISTANCE:
        x = 0
    elif abs(sw - (x + w)) <= SNAP_DISTANCE:
        x = sw - w
    if abs(y) <= SNAP_DISTANCE:
        y = 0
    elif abs(sh - (y + h)) <= SNAP_DISTANCE:
        y = sh - h
    root.geometry("+%d+%d" % (x, y))


collapsed = {"on": False, "height": 0}


def toggle_collapse():
    """Roll the overlay up into just its title bar."""
    if not overlay_mode.get():
        root.iconify()
        return
    if collapsed["on"]:
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        statusbar.pack(side=tk.BOTTOM, fill=tk.X)
        root.geometry("%dx%d" % (root.winfo_width(), collapsed["height"]))
        collapsed["on"] = False
    else:
        collapsed["height"] = root.winfo_height()
        body.pack_forget()
        statusbar.pack_forget()
        root.update_idletasks()
        root.geometry("%dx%d" % (root.winfo_width(), titlebar.winfo_reqheight()))
        collapsed["on"] = True


def remember_geometry():
    if collapsed["on"]:
        return
    geometry = root.geometry()
    if overlay_mode.get():
        config["overlay_geometry"] = geometry
    else:
        config["window_geometry"] = geometry


config_save_job = {"id": None}


def schedule_config_save(delay=700):
    """Debounce config writes -- <Configure> fires constantly while dragging."""
    if config_save_job["id"]:
        root.after_cancel(config_save_job["id"])
    config_save_job["id"] = root.after(delay, save_config)


def on_configure(event):
    if event.widget is root and root.state() != "withdrawn":
        remember_geometry()
        schedule_config_save()


def quit_app(event=None):
    autosave_current()
    remember_geometry()
    config["autosave"] = bool(autosave_enabled.get())
    save_config()
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
    menu.add_checkbutton(label="Always on top", variable=topmost, command=apply_topmost)
    menu.add_checkbutton(label="Click-through", variable=click_through,
                         command=toggle_click_through)
    menu.add_checkbutton(label="Show sidebar", variable=sidebar_visible, command=toggle_sidebar)
    menu.add_checkbutton(label="Hide from taskbar", variable=hide_from_taskbar,
                         command=on_taskbar_setting)
    menu.add_checkbutton(label="Autosave", variable=autosave_enabled, command=on_autosave_setting)

    opacity_menu = tk.Menu(menu, tearoff=0)
    for percent in (100, 90, 80, 70, 60, 50, 35):
        opacity_menu.add_command(label="%d%%" % percent,
                                 command=lambda p=percent: set_opacity(p / 100))
    menu.add_cascade(label="Opacity", menu=opacity_menu)

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
    menu.add_command(label="Hide (%s)" % GLOBAL_HOTKEYS["toggle_visible"][2], command=hide_window)
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


def on_title_key(event):
    global dirty
    dirty = True


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

content_text.bind("<<Modified>>", mark_dirty)
title_entry.bind("<KeyRelease>", on_title_key)

root.bind("<Control-s>", save_note)
root.bind("<Control-n>", new_note)
root.bind("<Control-Shift-O>", lambda e: toggle_overlay_mode())
root.bind("<F1>", lambda e: show_info())
root.bind("<Escape>", hide_window)
root.bind("<Configure>", on_configure)
root.protocol("WM_DELETE_WINDOW", quit_app)

# -------------------------------
# On startup, load notes and populate the sidebar.
# -------------------------------
load_notes_from_file()
update_treeview()
apply_theme()
toggle_sidebar()
set_opacity(config["opacity"], announce=False)
apply_mode(initial=True)
apply_topmost()
start_hotkeys()
mark_clean()

if overlay_mode.get() and IS_WINDOWS:
    set_status("%s hides VerTab" % GLOBAL_HOTKEYS["toggle_visible"][2], clear_after=5000)

root.mainloop()

"""VerTab's look -- palettes, fonts, icons and the few custom widgets Tk lacks.

Kept apart from VertabNB.py so the app script stays about behaviour while this
file owns everything visual that is reusable:

  * two palettes ("Midnight" dark and "Midnight Light") built from one design
    language: layered surfaces, hairline borders and a single violet accent;
  * fonts that exist on stock Windows, and icons drawn with Pillow from the
    Segoe MDL2 icon font (plain characters when that font is missing);
  * antialiased rounded shapes drawn with Pillow, used for pills and plates;
  * IconButton, Tooltip, Placeholder, SearchPill, RowPill, PopupMenu and
    InfoDialog.

Nothing here touches notes or settings, and nothing imports the app script.
"""
import contextlib
import os
import tkinter as tk
import tkinter.font as tkfont

from PIL import Image, ImageDraw, ImageFont, ImageTk
from ttkbootstrap.style import ThemeDefinition

import vertab_core as core
from vertab_core import IS_WINDOWS

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

# -------------------------------
# Palettes
# -------------------------------
# Surfaces get lighter towards the content in dark mode (frame < editor <
# raised < hover), and the editor is the brightest sheet in light mode.
PALETTES = {
    "midnight": dict(
        dark=True, frame="#0B0D12", editor="#11141B", raised="#181C25", hover="#222736",
        hairline="#222733", text="#E8EAF0", muted="#8E95A8", faint="#5A6275",
        accent="#7C6CF6", accent_hover="#9488FF", on_accent="#FFFFFF",
        warning="#F2B35A", danger="#F26D78",
    ),
    "midnight-light": dict(
        dark=False, frame="#F1F2F6", editor="#FFFFFF", raised="#E8EAF1", hover="#DCDFE9",
        hairline="#DFE2EA", text="#151823", muted="#5E667A", faint="#8F96A8",
        accent="#5B49E8", accent_hover="#4A39D3", on_accent="#FFFFFF",
        warning="#C77800", danger="#D63A4D",
    ),
}
# name -> menu label.  "auto" is not a palette: resolve_theme() maps it to one.
THEMES = {"auto": "Match Windows", "midnight": "Midnight", "midnight-light": "Midnight Light"}
DEFAULT_THEME = "auto"


def windows_prefers_light():
    """The Windows "app mode" setting; dark when it can't be read (or off Windows)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return bool(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])
    except (ImportError, OSError):
        return False


def resolve_theme(name):
    """The palette to draw with for a theme setting, following Windows for "auto"."""
    if name == "auto":
        return "midnight-light" if windows_prefers_light() else "midnight"
    return name if name in PALETTES else "midnight"


def mix(top, bottom, amount):
    """`amount` (0..1) of the #rrggbb colour `top` laid over `bottom`."""
    pairs = zip((int(top[i:i + 2], 16) for i in (1, 3, 5)),
                (int(bottom[i:i + 2], 16) for i in (1, 3, 5)))
    return "#%02x%02x%02x" % tuple(round(t * amount + b * (1 - amount)) for t, b in pairs)


def build_palette(name):
    """The named palette plus the colours derived from it."""
    p = dict(PALETTES[name])
    p["selected"] = mix(p["accent"], p["frame"], 0.22 if p["dark"] else 0.14)
    p["selection"] = mix(p["accent"], p["editor"], 0.40)      # selected text in the editor
    p["soft"] = mix(p["text"], p["muted"], 0.55)               # list titles at rest
    p["danger_soft"] = mix(p["danger"], p["frame"], 0.16)
    p["rim"] = mix(p["accent"], p["hairline"], 0.32)           # the overlay's outer border
    return p


def register_themes(style):
    """Teach ttkbootstrap both palettes, so style.theme_use("midnight") works."""
    for name, raw in PALETTES.items():
        p = build_palette(name)
        style.register_theme(ThemeDefinition(name, {
            "primary": p["accent"], "secondary": p["raised"], "success": "#2DBE96",
            "info": p["accent"], "warning": p["warning"], "danger": p["danger"],
            "light": p["raised"], "dark": p["frame"], "bg": p["frame"], "fg": p["text"],
            "selectbg": p["selected"], "selectfg": p["text"], "border": p["hairline"],
            "inputfg": p["text"], "inputbg": p["raised"], "active": p["hover"],
        }, "dark" if raw["dark"] else "light"))


# -------------------------------
# Icons
# -------------------------------
# Icons are drawn with Pillow, not Tk text, so they get clean greyscale edges
# instead of ClearType colour fringes.  Fonts are looked up by file name in the
# Windows font folder: Segoe Fluent Icons (Windows 11) or Segoe MDL2 Assets
# (Windows 10), then Segoe UI Symbol with the plain characters below.
ICON_FONT_FILES = ("SegoeIcons.ttf", "segmdl2.ttf")
FALLBACK_FONT_FILES = ("seguisym.ttf", "segoeui.ttf", "DejaVuSans.ttf")
FONT_DIR = os.path.join(os.environ.get("WINDIR") or r"C:\Windows", "Fonts")
# name -> (icon font glyph, plain fallback character)
ICONS = {
    "menu": ("\ue700", "☰"), "add": ("\ue710", "+"), "search": ("\ue721", "⌕"),
    "save": ("\ue74e", "✓"), "delete": ("\ue74d", "⌫"), "info": ("\ue946", "ⓘ"),
    "close": ("\ue711", "✕"), "clear": ("\ue711", "✕"), "minimize": ("\ue921", "–"),
    "restore": ("\ue923", "❐"), "pin": ("\ue718", "⌃"), "notes": ("\ue8fd", "▤"),
    "check": ("\ue73e", "✓"), "minus": ("\ue738", "−"),
}


class Look:
    """Fonts, the active palette and a cache of Pillow-drawn images for one root."""

    def __init__(self, root, scale):
        self.root = root
        self.scale = scale
        self.palette = {}
        self._images = {}
        families = set(tkfont.families(root))

        def pick(*names, default="TkDefaultFont"):
            return next((n for n in names if n in families), default)

        text_family = pick("Segoe UI Variable Text", "Segoe UI")
        display = pick("Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI")
        weight = "normal" if "Semibold" in display else "bold"
        self.text = tkfont.Font(root, family=text_family, size=10)
        self.small = tkfont.Font(root, family=text_family, size=9)
        self.caps = tkfont.Font(root, family=text_family, size=8, weight="bold")
        self.strong = tkfont.Font(root, family=display, size=10, weight=weight)
        self.title = tkfont.Font(root, family=display, size=17, weight=weight)
        self.body = tkfont.Font(root, family=text_family, size=11)
        self._icon_fonts = {}
        self.has_icon_font = self._truetype(ICON_FONT_FILES, 12) is not None
        self.blank = tk.PhotoImage(master=root, width=1, height=1)  # transparent spacer image

    def px(self, value):
        return int(round(value * self.scale))

    def use(self, name):
        self.palette.clear()
        self.palette.update(build_palette(name))

    @staticmethod
    def _truetype(files, size):
        for name in files:
            # Full paths on Windows: given a bare name, Pillow tries the working
            # directory before the Fonts folder.
            path = os.path.join(FONT_DIR, name) if IS_WINDOWS else name
            with contextlib.suppress(OSError):
                return ImageFont.truetype(path, size)
        return None

    def _icon_font(self, size):
        if size not in self._icon_fonts:
            self._icon_fonts[size] = (
                self._truetype(ICON_FONT_FILES if self.has_icon_font else FALLBACK_FONT_FILES, size)
                or ImageFont.load_default())
        return self._icon_fonts[size]

    def icon(self, name, box, colour, plate=None, radius=0, size=16):
        """A box x box square holding the named icon (`size` px tall) in `colour`,
        optionally on a rounded plate of colour `plate`.  Cached."""
        key = ("icon", name, box, colour, plate, radius, size)
        if key not in self._images:
            grain = 4
            glyph = ICONS[name][0 if self.has_icon_font else 1]
            font = self._icon_font(size * grain)
            mask = Image.new("L", (box * grain, box * grain), 0)
            draw = ImageDraw.Draw(mask)
            left, top, right, bottom = draw.textbbox((0, 0), glyph, font=font)
            draw.text(((box * grain - (right - left)) / 2 - left,
                       (box * grain - (bottom - top)) / 2 - top), glyph, font=font, fill=255)
            ink = Image.new("RGBA", (box, box), colour)
            ink.putalpha(mask.resize((box, box), Image.LANCZOS))
            image = (self._draw_plate(box, box, radius, plate, None, 0) if plate
                     else Image.new("RGBA", (box, box), (0, 0, 0, 0)))
            image.alpha_composite(ink)
            self._images[key] = ImageTk.PhotoImage(image, master=self.root)
        return self._images[key]

    # --- antialiased shapes (Tk's own canvas shapes are jagged on Windows) ---
    def plate(self, width, height, radius, fill, outline=None, border=1):
        """A rounded rectangle (a pill when radius >= height / 2), cached."""
        key = (width, height, radius, fill, outline, border)
        if key not in self._images:
            self._images[key] = ImageTk.PhotoImage(
                self._draw_plate(width, height, radius, fill, outline, border), master=self.root)
        return self._images[key]

    @staticmethod
    def _draw_plate(w, h, radius, fill, outline, border, grain=4):
        size = (w * grain, h * grain)
        outer = Image.new("L", size, 0)
        ImageDraw.Draw(outer).rounded_rectangle(
            (0, 0, size[0] - 1, size[1] - 1), radius=radius * grain, fill=255)
        colour = Image.new("RGB", size, outline or fill)
        if outline:
            inner = Image.new("L", size, 0)
            b = border * grain
            ImageDraw.Draw(inner).rounded_rectangle(
                (b, b, size[0] - 1 - b, size[1] - 1 - b),
                radius=max(0, radius - border) * grain, fill=255)
            colour.paste(fill, mask=inner)
        image = colour.resize((w, h), Image.LANCZOS)
        image.putalpha(outer.resize((w, h), Image.LANCZOS))
        return image

    def spacer(self, size):
        """A transparent square image, to hold a place open in a layout."""
        key = ("spacer", size)
        if key not in self._images:
            self._images[key] = tk.PhotoImage(master=self.root, width=size, height=size)
        return self._images[key]

    def dot(self, diameter, colour):
        return self.plate(diameter, diameter, diameter // 2, colour)

    def brand_mark(self, size, accent, ink):
        """The app icon's motif: a column of tabs beside lines of text."""
        key = ("mark", size, accent, ink)
        if key not in self._images:
            grain = 8
            s = size * grain
            image = Image.new("RGB", (s, s), accent)
            d = ImageDraw.Draw(image)
            for row in range(3):
                top = s * (0.24 + row * 0.2)
                d.rounded_rectangle((s * 0.2, top, s * 0.34, top + s * 0.12), radius=s * 0.03,
                                    fill=ink if row == 1 else mix(ink, accent, 0.55))
                d.rounded_rectangle((s * 0.43, top + s * 0.03, s * (0.8 - row * 0.08), top + s * 0.09),
                                    radius=s * 0.03, fill=ink)
            mask = Image.new("L", (s, s), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=s * 0.26, fill=255)
            image = image.resize((size, size), Image.LANCZOS)
            image.putalpha(mask.resize((size, size), Image.LANCZOS))
            self._images[key] = ImageTk.PhotoImage(image, master=self.root)
        return self._images[key]


def style_native_frame(hwnd, dark):
    """Dark title bar and rounded corners where Windows supports them (best effort)."""
    if not (IS_WINDOWS and hwnd):
        return
    with contextlib.suppress(OSError, AttributeError):
        dwm = core.system_dll("dwmapi.dll")
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                              ctypes.c_void_p, wintypes.DWORD]

        def attribute(index, value):
            flag = ctypes.c_int(value)
            return dwm.DwmSetWindowAttribute(hwnd, index, ctypes.byref(flag), 4)

        for index in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE, pre-2004 builds use 19
            if attribute(index, int(dark)) == 0:
                break
        attribute(33, 2)  # DWMWA_WINDOW_CORNER_PREFERENCE = round (Windows 11 only)
        # A visible window repaints its frame only when its active state flips.
        user32 = core.system_dll("user32.dll")
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        active = user32.GetForegroundWindow() == hwnd
        for state in (not active, active):
            user32.SendMessageW(hwnd, 0x0086, int(state), 0)  # WM_NCACTIVATE


# -------------------------------
# Widgets
# -------------------------------
def plain(widget_class, *args, **options):
    """A tk widget that ttkbootstrap leaves alone: it would otherwise restyle
    every plain widget on each theme change, overriding the palette below."""
    return widget_class(*args, autostyle=False, **options)


class Tooltip:
    """A small floating label shown after the pointer rests on a widget."""

    def __init__(self, widget, look, text, delay=600):
        self.widget, self.look, self.text, self.delay = widget, look, text, delay
        self.window = None
        self.job = None
        widget.bind("<Enter>", self._schedule, add="+")
        for sequence in ("<Leave>", "<ButtonPress>", "<Destroy>"):
            widget.bind(sequence, self._hide, add="+")

    def _schedule(self, _event=None):
        self._hide()
        self.job = self.widget.after(self.delay, self._show)

    def _show(self):
        text = self.text() if callable(self.text) else self.text
        if not text:
            return
        look, p = self.look, self.look.palette
        self.window = plain(tk.Toplevel, self.widget, bg=p["hairline"])
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        plain(tk.Label, self.window, text=text, font=look.small, bg=p["raised"], fg=p["text"],
                 padx=look.px(9), pady=look.px(4)).pack(padx=1, pady=1)
        self.window.update_idletasks()
        w, h = self.window.winfo_reqwidth(), self.window.winfo_reqheight()
        x = self.widget.winfo_rootx() + (self.widget.winfo_width() - w) // 2
        x = max(0, min(x, self.widget.winfo_screenwidth() - w))
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + look.px(6)
        if y + h > self.widget.winfo_screenheight():
            y = self.widget.winfo_rooty() - h - look.px(6)
        self.window.geometry("+%d+%d" % (x, y))
        self.window.deiconify()

    def _hide(self, _event=None):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.window:
            self.window.destroy()
            self.window = None


class IconButton(tk.Label):
    """A flat icon button with rounded hover and press plates.

    kind: "ghost" (quiet), "accent" (filled), "danger" (turns red on hover) or
    "close" (fills red on hover).  surface: palette key of what it sits on.
    Wire it up by assigning .command; tip may be text or a function returning it.
    """

    def __init__(self, parent, look, icon, command, tip, kind="ghost", surface="frame", size=34):
        self.look, self.icon, self.command = look, icon, command
        self.kind, self.surface, self.active = kind, surface, False
        self.size = look.px(size)
        super().__init__(parent, autostyle=False, bd=0, padx=0, pady=0, cursor="hand2")
        self.bind("<Enter>", lambda e: self._paint("hover"))
        self.bind("<Leave>", lambda e: self._paint("normal"))
        self.bind("<ButtonPress-1>", lambda e: self._paint("press"))
        self.bind("<ButtonRelease-1>", self._release)
        Tooltip(self, look, tip)
        self._paint("normal")

    def set_icon(self, icon):
        self.icon = icon
        self._paint("normal")

    def set_kind(self, kind):
        self.kind = kind
        self._paint("normal")

    def set_active(self, active):
        """Mark a toggle button as 'on' with a tinted plate and an accent glyph."""
        self.active = active
        self._paint("normal")

    def recolor(self, _palette=None):
        self._paint("normal")

    def _release(self, event):
        inside = 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        self._paint("hover" if inside else "normal")
        if inside:
            self.command()

    def _paint(self, state):
        p = self.look.palette
        if self.kind == "accent":
            plate = {"normal": p["accent"], "hover": p["accent_hover"],
                     "press": mix(p["accent"], "#000000", 0.82)}[state]
            fg = p["on_accent"]
        elif self.kind == "close" and state != "normal":
            plate, fg = p["danger"], "#FFFFFF"
        elif self.kind == "danger" and state != "normal":
            plate, fg = p["danger_soft"], p["danger"]
        else:
            plate = {"normal": None, "hover": p["hover"], "press": p["selected"]}[state]
            fg = p["text"] if state != "normal" else p["muted"]
            if self.active:
                plate = plate or p["selected"]
                fg = p["accent"]
        self.configure(bg=p[self.surface], image=self.look.icon(
            self.icon, self.size, fg, plate, self.look.px(8), size=round(self.size * 0.47)))


class Placeholder(tk.Label):
    """Grey hint text inside an empty Entry; vanishes as soon as there is text."""

    def __init__(self, entry, variable, text, font):
        super().__init__(entry, autostyle=False, text=text, font=font, bd=0, padx=0, pady=0,
                         anchor="w")
        self.variable = variable
        self.place(x=1, rely=0.5, anchor="w")
        self.bind("<Button-1>", lambda e: entry.focus_set())
        variable.trace_add("write", lambda *_: self.sync())

    def sync(self):
        if self.variable.get():
            self.place_forget()
        elif not self.winfo_ismapped():
            self.place(x=1, rely=0.5, anchor="w")

    def recolor(self, palette):
        self.configure(bg=self.master.cget("bg"), fg=palette["faint"])


class SearchPill(tk.Canvas):
    """A pill-shaped search field: magnifier, entry, clear button or shortcut hint."""

    def __init__(self, parent, look, variable, hint="Ctrl F", height=36):
        self.look, self.variable, self.hint = look, variable, hint
        self.pill_height = look.px(height)
        super().__init__(parent, autostyle=False, height=self.pill_height, bd=0,
                         highlightthickness=0)
        self.focused = False
        self.entry = plain(tk.Entry, self, textvariable=variable, bd=0, highlightthickness=0,
                              relief="flat", font=look.text)
        self.placeholder = Placeholder(self.entry, variable, "Search notes", look.text)
        self.window = self.create_window(0, 0, window=self.entry, anchor="w")
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", self._click)
        self.entry.bind("<FocusIn>", lambda e: self._focus(True), add="+")
        self.entry.bind("<FocusOut>", lambda e: self._focus(False), add="+")
        variable.trace_add("write", lambda *_: self.redraw())

    def recolor(self, palette):
        p = palette
        self.configure(bg=p["frame"])
        self.entry.configure(bg=p["raised"], fg=p["text"], insertbackground=p["text"],
                             selectbackground=p["selection"], selectforeground=p["text"])
        self.placeholder.recolor(p)
        self.redraw()

    def _focus(self, focused):
        self.focused = focused
        self.redraw()

    def _click(self, event):
        if self.variable.get() and event.x > self.winfo_width() - self.look.px(36):
            self.variable.set("")
        self.entry.focus_set()

    def redraw(self):
        look, p = self.look, self.look.palette
        w, h = self.winfo_width(), self.pill_height
        if w < 2 or not p:
            return
        self.delete("deco")
        edge = p["accent"] if self.focused else p["hairline"]
        self.create_image(0, 0, anchor="nw", tags="deco",
                          image=look.plate(w, h, h // 2, p["raised"], edge))
        glyph = look.px(28)
        self.create_image(look.px(10), h // 2, anchor="w", tags="deco", image=look.icon(
            "search", glyph, p["accent"] if self.focused else p["muted"], size=look.px(14)))
        if self.variable.get():
            self.create_image(w - look.px(10), h // 2, anchor="e", tags="deco", image=look.icon(
                "clear", glyph, p["muted"], size=look.px(11)))
        else:
            self.create_text(w - look.px(14), h // 2, text=self.hint, tags="deco", anchor="e",
                             font=look.small, fill=p["faint"])
        right = look.px(40)
        self.coords(self.window, look.px(38), h // 2)
        self.itemconfigure(self.window, width=max(10, w - look.px(38) - right), height=h - look.px(14))
        self.tag_raise(self.window)


class RowPill(tk.Canvas):
    """A rounded highlight floated over one Treeview row, carrying the row's text.

    ttk cannot round a row's highlight or draw an accent bar beside it, so the
    tree paints nothing for these rows and a pill like this one covers them.
    """

    def __init__(self, tree, look, bar=False, fill="selected"):
        super().__init__(tree, autostyle=False, bd=0, highlightthickness=0, cursor="hand2")
        self.tree, self.look, self.bar, self.fill = tree, look, bar, fill
        self.iid = None

    def recolor(self, palette):
        self.configure(bg=palette["frame"])
        if self.iid:
            self.show(self.iid)

    def show(self, iid):
        """Cover row `iid`, or hide when it is scrolled out of view.

        A row cut off at the bottom keeps its pill: the pill is a child of the
        tree, so Tk clips it to the tree's edge like the row itself.
        """
        look, p = self.look, self.look.palette
        box = self.tree.bbox(iid) if self.tree.exists(iid) else None
        if not box or box[1] >= self.tree.winfo_height():
            return self.hide()
        gap = look.px(2)
        w, h = self.tree.winfo_width() - look.px(4), box[3] - 2 * gap
        self.iid = iid
        self.place(x=look.px(2), y=box[1] + gap, width=w, height=h)
        self.delete("all")
        self.create_image(0, 0, anchor="nw", image=look.plate(w, h, look.px(9), p[self.fill]))
        if self.bar:
            self.create_image(look.px(5), h // 2, anchor="w",
                              image=look.plate(look.px(3), h // 2, look.px(2), p["accent"]))
        self.create_text(look.px(16), h // 2, anchor="w", text=self.tree.item(iid, "text"),
                         font=look.strong if self.bar else look.text, fill=p["text"])

    def hide(self):
        self.iid = None
        self.place_forget()


class PopupMenu(tk.Toplevel):
    """A dark, flat replacement for tk.Menu, which Windows draws in system colours.

    Build it with add_* calls, then popup().  Closes on Esc, on a click outside,
    or when it loses focus; Up/Down/Enter work as in a normal menu.
    """

    def __init__(self, root, look, width=250):
        p = look.palette
        super().__init__(root, autostyle=False, bg=p["hairline"])
        self.look, self.min_width = look, look.px(width)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.body = plain(tk.Frame, self, bg=p["raised"], padx=look.px(5), pady=look.px(5))
        self.body.pack(padx=1, pady=1, fill=tk.BOTH, expand=True)
        self.rows = []      # (widgets of one row, activate callback or None)
        self.hot = None
        self.bind("<Escape>", lambda e: self.close() or "break")
        self.bind("<Up>", lambda e: self._move(-1))
        self.bind("<Down>", lambda e: self._move(1))
        self.bind("<Return>", lambda e: self._activate(self.hot))
        self.bind("<ButtonPress-1>", self._outside_click)
        self.bind("<FocusOut>", lambda e: self.after(50, self._lost_focus))

    # --- building ---
    def _row(self, label, hint="", checked=False, enabled=True, activate=None):
        look, p = self.look, self.look.palette
        row = plain(tk.Frame, self.body, bg=p["raised"], height=look.px(32))
        row.pack(fill=tk.X)
        row.pack_propagate(False)
        fg = p["text"] if enabled else p["faint"]
        mark = plain(tk.Label, row, bg=p["raised"], bd=0, image=look.icon(
            "check", look.px(22), p["accent"], size=look.px(12)) if checked else look.spacer(look.px(22)))
        mark.pack(side=tk.LEFT, padx=(look.px(6), 0))
        name = plain(tk.Label, row, text=label, font=look.text, bg=p["raised"], fg=fg, anchor="w")
        name.pack(side=tk.LEFT)
        widgets = [row, mark, name]
        if hint:
            note = plain(tk.Label, row, text=hint, font=look.small, bg=p["raised"], fg=p["faint"])
            note.pack(side=tk.RIGHT, padx=(look.px(24), look.px(10)))
            widgets.append(note)
        index = len(self.rows)
        self.rows.append((widgets, activate if enabled else None))
        for widget in widgets:
            widget.bind("<Enter>", lambda e, i=index: self._highlight(i))
            widget.bind("<ButtonRelease-1>", lambda e, i=index: self._activate(i))
        return row

    def add_command(self, label, command, hint="", enabled=True):
        self._row(label, hint, enabled=enabled, activate=command)

    def add_check(self, label, checked, command, hint="", enabled=True):
        self._row(label, hint, checked=checked, enabled=enabled, activate=command)

    def add_choice(self, label, selected, command):
        self._row(label, checked=selected, activate=command)

    def add_stepper(self, label, read, step):
        """A row with - and + that stays open; read() returns the text shown between."""
        look, p = self.look, self.look.palette
        row = plain(tk.Frame, self.body, bg=p["raised"], height=look.px(34))
        row.pack(fill=tk.X)
        row.pack_propagate(False)
        plain(tk.Label, row, text=label, font=look.text, bg=p["raised"], fg=p["text"]).pack(
            side=tk.LEFT, padx=(look.px(30), 0))
        value = plain(tk.Label, row, text=read(), font=look.text, bg=p["raised"], fg=p["muted"],
                         width=5)

        def bump(direction):
            step(direction)
            value.configure(text=read())

        for icon, direction in (("add", 1), (None, 0), ("minus", -1)):
            if icon:
                IconButton(row, look, icon, lambda d=direction: bump(d), "", surface="raised",
                           size=26).pack(side=tk.RIGHT, padx=(0, look.px(4)))
            else:
                value.pack(side=tk.RIGHT)

    def add_separator(self):
        plain(tk.Frame, self.body, bg=self.look.palette["hairline"], height=1).pack(
            fill=tk.X, padx=self.look.px(4), pady=self.look.px(4))

    def add_heading(self, text):
        plain(tk.Label, self.body, text=text.upper(), font=self.look.caps, bg=self.look.palette["raised"],
                 fg=self.look.palette["faint"], anchor="w").pack(
            fill=tk.X, padx=self.look.px(12), pady=(self.look.px(6), self.look.px(2)))

    # --- showing ---
    def popup(self, x, y, bounds):
        """Show with its top-left at (x, y), kept inside bounds = (left, top, right, bottom)."""
        self.update_idletasks()
        w = max(self.min_width, self.winfo_reqwidth())
        h = self.winfo_reqheight()
        left, top, right, bottom = bounds
        x = max(left, min(x, right - w))
        y = max(top, min(y, bottom - h))
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.deiconify()
        self.update_idletasks()
        with contextlib.suppress(tk.TclError):
            self.grab_set()
        self.focus_force()

    def close(self):
        if self.winfo_exists():
            with contextlib.suppress(tk.TclError):
                self.grab_release()
            self.destroy()

    # --- behaviour ---
    def _highlight(self, index):
        p = self.look.palette
        self.hot = index
        for i, (widgets, activate) in enumerate(self.rows):
            colour = p["hover"] if i == index and activate else p["raised"]
            for widget in widgets:
                widget.configure(bg=colour)

    def _move(self, step):
        live = [i for i, (_widgets, activate) in enumerate(self.rows) if activate]
        if live:
            position = live.index(self.hot) + step if self.hot in live else (0 if step > 0 else -1)
            self._highlight(live[position % len(live)])
        return "break"

    def _activate(self, index):
        if index is None or not self.rows[index][1]:
            return
        command = self.rows[index][1]
        self.close()
        command()

    def _outside_click(self, event):
        inside = (self.winfo_rootx() <= event.x_root < self.winfo_rootx() + self.winfo_width()
                  and self.winfo_rooty() <= event.y_root < self.winfo_rooty() + self.winfo_height())
        if not inside:
            self.close()

    def _lost_focus(self):
        if self.winfo_exists() and self.focus_displayof() is None:
            self.close()


class InfoDialog(tk.Toplevel):
    """The help panel: an intro, a table of shortcuts and a footer line."""

    def __init__(self, root, look, title, intro, shortcuts, footer):
        p = look.palette
        super().__init__(root, autostyle=False, bg=p["editor"])
        self.dark = p["dark"]
        self.withdraw()
        self.title(title)
        self.resizable(False, False)
        self.transient(root)
        if root.attributes("-topmost"):
            self.attributes("-topmost", True)
        body = plain(tk.Frame, self, bg=p["editor"], padx=look.px(28), pady=look.px(24))
        body.pack()
        head = plain(tk.Frame, body, bg=p["editor"])
        head.pack(fill=tk.X)
        plain(tk.Label, head, bd=0, bg=p["editor"],
              image=look.brand_mark(look.px(30), p["accent"], p["on_accent"])).pack(side=tk.LEFT)
        plain(tk.Label, head, text=title, font=look.title, bg=p["editor"], fg=p["text"]).pack(
            side=tk.LEFT, padx=look.px(12))
        plain(tk.Label, body, text=intro, font=look.text, bg=p["editor"], fg=p["muted"],
              justify=tk.LEFT, anchor="w", wraplength=look.px(430)).pack(
            fill=tk.X, pady=(look.px(14), look.px(14)))
        table = plain(tk.Frame, body, bg=p["editor"])
        table.pack(fill=tk.X)
        for row, (keys, action, note) in enumerate(shortcuts):
            plain(tk.Label, table, text=keys, font=look.small, bg=p["raised"], fg=p["text"],
                  padx=look.px(8), pady=look.px(2)).grid(
                row=row, column=0, sticky="w", pady=look.px(3))
            plain(tk.Label, table, text=action + ("  (%s)" % note if note else ""), font=look.text,
                  bg=p["editor"], fg=p["text"] if not note else p["faint"]).grid(
                row=row, column=1, sticky="w", padx=(look.px(16), 0))
        plain(tk.Frame, body, bg=p["hairline"], height=1).pack(fill=tk.X, pady=look.px(16))
        plain(tk.Label, body, text=footer, font=look.small, bg=p["editor"], fg=p["faint"],
              justify=tk.LEFT, anchor="w", wraplength=look.px(430)).pack(fill=tk.X)
        close = plain(tk.Label, body, text="Close", font=look.strong, fg=p["on_accent"], bd=0,
                      image=look.plate(look.px(96), look.px(34), look.px(9), p["accent"]),
                      compound="center", cursor="hand2", bg=p["editor"])
        close.pack(anchor="e", pady=(look.px(18), 0))
        close.bind("<ButtonRelease-1>", lambda e: self.destroy())
        for key in ("<Escape>", "<Return>"):
            self.bind(key, lambda e: self.destroy())

    def show(self, bounds):
        """Centre over the main window, keep inside bounds, and wait until closed."""
        self.update_idletasks()
        root, w, h = self.master, self.winfo_reqwidth(), self.winfo_reqheight()
        left, top, right, bottom = bounds
        x = root.winfo_rootx() + (root.winfo_width() - w) // 2
        y = root.winfo_rooty() + (root.winfo_height() - h) // 2
        self.geometry("+%d+%d" % (max(left, min(x, right - w)), max(top, min(y, bottom - h))))
        style_native_frame(int(self.wm_frame(), 16), self.dark)
        self.deiconify()
        self.grab_set()
        self.focus_force()
        self.wait_window()

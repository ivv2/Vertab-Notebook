**********************************
___ivv2___Vertical__Tab_Notebook
**********************************

VerTab is a multipourpose notes app with great features like:
1. Tree-style vertical tabs for easy finding of all the notes,
2. Easy to understand graphics.
3. Copy & paste buttons for fast replication of the notes.
4. **Overlay mode** — float VerTab on top of whatever app you are working in.
5. A single-file **VerTab.exe** you can run without installing Python.

---

## Running it

From source:

```bash
pip install -r requirements.txt
python VertabNB.py
```

Or just double-click `dist\VerTab.exe` after building (see below).

Command line flags:

| Flag | What it does |
| --- | --- |
| `--overlay` | start in overlay mode |
| `--window`  | start as a normal window |
| `--portable` | keep notes next to the executable instead of in `%APPDATA%` |

With no flag it reopens in whichever mode you used last.

## Overlay mode

Overlay mode turns VerTab into a frameless, always-on-top, semi-transparent
panel that sits over your other apps — handy for keeping notes next to a
video, a terminal or a document.

* **Move it** — drag the title bar. Drop it near a screen edge and it snaps flush.
* **Resize it** — drag the grip in the bottom-right corner.
* **Roll it up** — double-click the title bar, or use the `–` button.
* **Menu** — the `☰` button (or right-click the title bar) has opacity, themes,
  click-through, sidebar and autosave settings.
* **Click-through** — makes the mouse pass straight through VerTab to the app
  underneath, so the notes stay readable without getting in the way. It always
  starts **off**; while it is on, nothing in the window is clickable, so use the
  hotkey to turn it back off.
* The overlay stays out of the taskbar and Alt+Tab (switch that off in the menu).

### Hotkeys

These are registered system-wide on Windows, so they work while another app has
focus:

| Hotkey | Action |
| --- | --- |
| `Ctrl+Alt+N` | show / hide VerTab |
| `Ctrl+Alt+M` | switch between overlay and window mode |
| `Ctrl+Alt+T` | click-through on / off |
| `Ctrl+Alt+↑` / `Ctrl+Alt+↓` | more / less opaque |

While VerTab is focused: `Ctrl+S` save, `Ctrl+N` new note, `Esc` hide, `F1` info.

Overlay mode is built on Windows window styles. On macOS and Linux the frameless
always-on-top panel still works, but click-through, taskbar hiding and the
*global* hotkeys are Windows-only — the hotkeys fall back to working while
VerTab is focused.

## Where notes are stored

Notes and settings live in a per-user folder so the executable works from
anywhere:

| OS | Folder |
| --- | --- |
| Windows | `%APPDATA%\VerTab\` |
| macOS | `~/Library/Application Support/VerTab/` |
| Linux | `~/.config/vertab/` |

It holds `notes.json` (your notes) and `config.json` (window position, opacity,
theme and so on). A `notes.json` sitting next to the script is picked up
automatically the first time you run this version, so existing notes carry over.
Use `--portable` to keep both files beside the executable instead.

## Building the executable

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

That installs the build requirements, regenerates the icon and runs PyInstaller,
producing a single **`dist\VerTab.exe`** (~17 MB, no Python needed on the target
machine).

To run the steps yourself:

```bash
pip install -r requirements-build.txt
python tools/make_icon.py
python -m PyInstaller --noconfirm --clean VertabNB.spec
```

The icon is drawn by `tools/make_icon.py` with Pillow rather than being checked
in as a binary. Note that Pillow must stay out of the spec's `excludes` list —
`ttkbootstrap.style` imports it, and the frozen app dies silently without it.

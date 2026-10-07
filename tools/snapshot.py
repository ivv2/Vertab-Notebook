"""Screenshot VerTab's own window to a PNG (Windows only).

Launches the app against a throwaway %APPDATA% seeded with a demo notebook,
captures the window with PrintWindow -- so other windows covering it don't
matter -- and closes it again.

    python tools/snapshot.py shot.png
    python tools/snapshot.py dark.png --theme darkly --overlay --open "Recipes"
    python tools/snapshot.py big.png --size 1100x700 --app path/to/VertabNB.py
"""
import argparse
import contextlib
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEMO_NOTES = {
    "Welcome": "VerTab keeps your notes in vertical tabs.\n\n"
               "• Ctrl+N new note\n• Ctrl+S save\n• Ctrl+Alt+N show / hide the overlay",
    "Recipes": "Pancakes\n\n2 eggs\n250 ml milk\n150 g flour\npinch of salt\n\n"
               "Whisk, rest 10 minutes, fry thin.",
    "Meeting notes": "Q3 planning\n\n- ship overlay mode\n- package VerTab.exe\n- collect feedback",
    "Math": "Quadratic: x = (-b ± √(b² - 4ac)) / 2a\nEuler: e^(iπ) + 1 = 0",
    "Latviešu": "Sveiki! Šī ir piezīme latviešu valodā.",
    "Reading list": "1. The Pragmatic Programmer\n2. A Philosophy of Software Design\n3. Refactoring",
    "Ideas": "Tag notes with colours?\nPin favourites to the top?",
    "Shopping": "coffee, oat milk, lemons, basil",
    "Travel": "Riga → Tallinn, Lux Express 09:00",
    "Art": "  /\\_/\\\n ( o.o )  meow\n  > ^ <",
}

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
for fn, res, args in [
    (user32.GetWindowDC, ctypes.c_void_p, [wintypes.HWND]),
    (user32.ReleaseDC, ctypes.c_int, [wintypes.HWND, ctypes.c_void_p]),
    (user32.PrintWindow, wintypes.BOOL, [wintypes.HWND, ctypes.c_void_p, wintypes.UINT]),
    (gdi32.CreateCompatibleDC, ctypes.c_void_p, [ctypes.c_void_p]),
    (gdi32.CreateCompatibleBitmap, ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]),
    (gdi32.SelectObject, ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p]),
    (gdi32.DeleteObject, wintypes.BOOL, [ctypes.c_void_p]),
    (gdi32.DeleteDC, wintypes.BOOL, [ctypes.c_void_p]),
    (gdi32.GetDIBits, ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
                                     ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]),
]:
    fn.restype, fn.argtypes = res, args


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def windows_of(pid, cls="TkTopLevel"):
    found = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, name, 256)
        if owner.value == pid and name.value == cls and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(proc(callback), 0)
    return found


def capture(hwnd, path):
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        user32.PrintWindow(hwnd, mem, 2)  # PW_RENDERFULLCONTENT
        info = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w,
                                biHeight=-h, biPlanes=1, biBitCount=32)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), 0)
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)
    Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).save(path)
    return w, h


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output")
    parser.add_argument("--app", default=os.path.join(ROOT, "VertabNB.py"))
    parser.add_argument("--theme")
    parser.add_argument("--overlay", action="store_true")
    parser.add_argument("--open", dest="open_title", default="Recipes")
    parser.add_argument("--size", default="900x600", help="WxH of the window")
    parser.add_argument("--wait", type=float, default=2.5, help="seconds to let it settle")
    opts = parser.parse_args()

    with contextlib.suppress(AttributeError, OSError):
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # match the app's pixel space

    appdata = tempfile.mkdtemp(prefix="vertab-shot-")
    folder = os.path.join(appdata, "VerTab")
    os.makedirs(folder)
    with open(os.path.join(folder, "notes.json"), "w", encoding="utf-8") as f:
        json.dump(DEMO_NOTES, f, ensure_ascii=False, indent=2)
    geometry = opts.size + "+60+60"
    config = {"overlay_mode": opts.overlay,
              "overlay_geometry" if opts.overlay else "window_geometry": geometry}
    if opts.theme:
        config["theme"] = opts.theme
    with open(os.path.join(folder, "config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f)

    args = [sys.executable, os.path.abspath(opts.app), "--open", opts.open_title]
    proc = subprocess.Popen(args, cwd=appdata, env=dict(os.environ, APPDATA=appdata),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        hwnd = None
        end = time.time() + 15
        while time.time() < end and not hwnd:
            if proc.poll() is not None:
                print(proc.stdout.read().decode(errors="replace"))
                sys.exit("app exited before a window appeared")
            found = windows_of(proc.pid)
            hwnd = found[0] if found else None
            time.sleep(0.2)
        if not hwnd:
            sys.exit("no window appeared")
        time.sleep(opts.wait)
        errors = windows_of(proc.pid, "#32770")
        size = capture(hwnd, opts.output)
        print("saved %s (%dx%d)%s" % (opts.output, size[0], size[1],
                                      "  WARNING: a dialog is open" if errors else ""))
        if errors:
            capture(errors[0], os.path.splitext(opts.output)[0] + "-dialog.png")
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)


if __name__ == "__main__":
    main()

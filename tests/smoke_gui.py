"""End-to-end smoke test: launch the real app and inspect its windows (Windows only).

Runs against a throwaway %APPDATA%, so your real notebook is never touched.

    python tests/smoke_gui.py                  # test VertabNB.py
    python tests/smoke_gui.py dist/VerTab.exe  # test the built executable
"""
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TITLE = "VerTab Notebook"
user32 = ctypes.windll.user32
user32.GetWindowLongPtrW.restype = ctypes.c_longlong
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]

WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_LAYERED = 0x8, 0x80, 0x80000
TRICKY_NOTES = {
    "{oops": "unbalanced brace in the title",
    "a b": "spaces",
    "latvian": "Darbs maximā nav jauks",
    "math": "x = (-b +/- sqrt(b^2 - 4ac))/(2a)",
}


def windows(title=None, cls=None, pids=None):
    """Visible top-level windows, optionally only those owned by `pids`.

    Scoping by process lets several copies of this test run side by side.
    """
    found = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        text = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, text, n + 1)
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, name, 256)
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if pids is not None and owner.value not in pids:
            return True
        if (title is None or text.value == title) and (cls is None or name.value == cls):
            found.append(hwnd)
        return True

    user32.EnumWindows(proc(callback), 0)
    return found


def wait_for(predicate, timeout):
    end = time.time() + timeout
    while time.time() < end:
        result = predicate()
        if result:
            return result
        time.sleep(0.25)
    return None


class Run:
    def __init__(self, target):
        self.target = target
        self.appdata = tempfile.mkdtemp(prefix="vertab-smoke-")
        self.folder = os.path.join(self.appdata, "VerTab")
        os.makedirs(self.folder)
        self.procs = []
        # The exe re-launches itself as a child process, so only a script
        # run can be scoped to process ids; the exe is matched by title.
        self.pids = None if target.endswith(".exe") else set()

    def seed(self, raw):
        with open(os.path.join(self.folder, "notes.json"), "w", encoding="utf-8") as f:
            f.write(raw)

    def launch(self, *args):
        cmd = [self.target] if self.target.endswith(".exe") else [sys.executable, self.target]
        env = dict(os.environ, APPDATA=self.appdata)
        proc = subprocess.Popen(cmd + list(args), cwd=self.appdata, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.procs.append(proc)
        if self.pids is not None:
            self.pids.add(proc.pid)
        return proc

    def windows(self, title=TITLE, cls=None):
        return windows(title, cls, self.pids)

    def close(self):
        for proc in self.procs:
            if proc.poll() is None:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
        time.sleep(0.5)


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "VertabNB.py")
    target = os.path.abspath(target)
    timeout = 25 if target.endswith(".exe") else 12
    failures = []

    def check(name, ok, detail=""):
        print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail else ""))
        if not ok:
            failures.append(name)

    if target.endswith(".exe") and windows(TITLE):
        print("A VerTab window is already open; close it first.")
        return 2

    # 1. window mode, with titles that used to crash the Treeview
    run = Run(target)
    try:
        run.seed(json.dumps(TRICKY_NOTES))
        proc = run.launch("--window")
        hwnd = wait_for(lambda: run.windows(TITLE, "TkTopLevel"), timeout)
        check("window mode opens", bool(hwnd))
        time.sleep(1.5)
        check("window mode stays running", proc.poll() is None,
              "" if proc.poll() is None else proc.stdout.read().decode(errors="replace")[-600:])
        check("no error dialog", not run.windows(None, "#32770"))
        if hwnd:
            ex = user32.GetWindowLongPtrW(hwnd[0], -20)
            check("window mode is not topmost by default", not ex & WS_EX_TOPMOST)
        run.close()

        # 2. overlay mode styles
        proc = run.launch("--overlay")
        hwnd = wait_for(lambda: run.windows(TITLE, "TkTopLevel"), timeout)
        check("overlay mode opens", bool(hwnd))
        if hwnd:
            time.sleep(1.0)
            ex = user32.GetWindowLongPtrW(hwnd[0], -20)
            check("overlay is topmost", bool(ex & WS_EX_TOPMOST))
            check("overlay hidden from taskbar", bool(ex & WS_EX_TOOLWINDOW))
            check("overlay is translucent (layered)", bool(ex & WS_EX_LAYERED))
            check("click-through starts off", not ex & 0x20)

        # 3. a second instance must refuse to start
        second = run.launch("--overlay")
        dialog = wait_for(lambda: run.windows(TITLE, "#32770"), timeout)
        check("second instance is refused", bool(dialog) and len(run.windows(TITLE, "TkTopLevel")) == 1)
        run.close()

        # 4. a damaged notebook is moved aside, not overwritten
        run.seed('{"math": "x = 1",')
        proc = run.launch("--window")
        dialog = wait_for(lambda: run.windows("Notebook recovered", "#32770"), timeout)
        check("damaged notebook is reported", bool(dialog))
        kept = [n for n in os.listdir(run.folder) if ".damaged-" in n]
        check("damaged notebook is kept aside", len(kept) == 1, ", ".join(kept))
        run.close()

        # 5. a hostile config.json must not stop startup
        with open(os.path.join(run.folder, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"opacity": "lots", "theme": "no-such-theme", "overlay_geometry": "x",
                       "window_geometry": "200x200+99999+99999"}, f)
        proc = run.launch()
        hwnd = wait_for(lambda: run.windows(TITLE, "TkTopLevel"), timeout)
        check("starts with a hostile config.json", bool(hwnd) and proc.poll() is None)
        if hwnd:
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd[0], ctypes.byref(rect))
            check("off-screen geometry is pulled on-screen",
                  rect.left < user32.GetSystemMetrics(78) + user32.GetSystemMetrics(76),
                  "left=%d" % rect.left)
    finally:
        run.close()

    print("\n%d failure(s)" % len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

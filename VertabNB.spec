# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for VerTab -- one windowed .exe in dist/VerTab.exe.

    python -m PyInstaller --noconfirm --clean VertabNB.spec

ttkbootstrap ships its themes as package data, so it is collected whole;
without that the frozen app starts and then fails looking up the theme.
"""
import os

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = collect_all("ttkbootstrap")

ICON = os.path.join("assets", "vertab.ico")
if os.path.exists(ICON):
    datas += [(ICON, "assets")]

a = Analysis(
    ["VertabNB.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Trim the heavyweight scientific stack; keep the exe small.
    # Pillow stays: ttkbootstrap.style imports it, so excluding it kills the app.
    excludes=["numpy", "pandas", "matplotlib", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="VerTab",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=False,          # GUI app: no console window behind it
    disable_windowed_traceback=False,
    icon=ICON if os.path.exists(ICON) else None,
)

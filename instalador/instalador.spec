# -*- mode: python ; coding: utf-8 -*-
"""Empacota o instalador (arquivo único) com o programa (exe + DLLs) embutido."""

a = Analysis(
    ["instalar.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("../dist/GNRE", "app"),
        ("../assets/gnre.ico", "."),
        ("../assets/gnre.png", "."),
    ],
    hiddenimports=["win32com.client", "pythoncom", "win32com.shell"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter.test", "pydoc_data", "matplotlib", "pandas", "IPython"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Instalar GNRE",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="../assets/gnre.ico",
    version="versao_instalador.txt",
)

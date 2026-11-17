# -*- mode: python ; coding: utf-8 -*-
"""Empacota o emissor como pasta (GNRE.exe + _internal com as DLLs).

Sem autoextração em pasta temporária: o antivírus trata muito melhor um
executável que carrega as bibliotecas do próprio diretório.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = [
    ("assets", "assets"),
    ("schemas_gnre", "schemas_gnre"),
    ("regras_uf.json", "."),
    ("config/municipios.json", "config"),
]
datas += collect_data_files("ttkbootstrap")
datas += collect_data_files("playwright")

hiddenimports = [
    "playwright",
    "playwright.sync_api",
    "playwright._impl._driver",
    "greenlet",
    "pyee",
    "pymupdf",
    "pypdf",
]
hiddenimports += collect_submodules("bypassguia")

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name="GNRE",
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
    icon="assets/gnre.ico",
    version="versao_exe.txt",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="GNRE",
)

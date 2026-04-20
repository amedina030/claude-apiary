# PyInstaller spec for the apiary GUI (one-folder Windows build).
#
# Build via:
#     poetry run python gui/packaging/build.py
#
# Or directly:
#     poetry run pyinstaller gui/packaging/apiary_gui.spec
#
# Output: dist/apiary-gui/apiary-gui.exe (+ _internal/ sibling folder).

from pathlib import Path

# PyInstaller injects `SPEC` (path to this file) and `workpath`/`distpath` at runtime.
PACKAGING_DIR = Path(SPEC).resolve().parent  # noqa: F821 (SPEC is PyInstaller-injected)
GUI_DIR = PACKAGING_DIR.parent
REPO_ROOT = GUI_DIR.parent

WEB_DIR = GUI_DIR / "web"
MANIFEST = PACKAGING_DIR / "apiary_gui.manifest"
ICON = PACKAGING_DIR / "apiary_gui.ico"
ENTRY = GUI_DIR / "app.py"


a = Analysis(  # noqa: F821
    [str(ENTRY)],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=[
        # Frontend assets — gui/app.py resolves these via Path(__file__).parent / "web",
        # so they must land at <bundle>/_internal/gui/web/.
        (str(WEB_DIR), "gui/web"),
    ],
    hiddenimports=[
        # pywebview's Windows backend pulls these in via runtime probes that
        # PyInstaller's static analysis sometimes misses.
        "webview.platforms.winforms",
        "clr_loader",
        "clr_loader.netfx",
        "clr_loader.types",
        # pywinpty exposes a small native shim; keep its top-level import explicit.
        "winpty",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim cross-platform pywebview backends we don't ship on Windows.
        "webview.platforms.cocoa",
        "webview.platforms.gtk",
        "webview.platforms.qt",
        "webview.platforms.android",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="apiary-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    manifest=str(MANIFEST),
    icon=str(ICON),
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="apiary-gui",
)

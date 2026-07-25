# PyInstaller spec for the Park Tile Archiver desktop app.
#
#   pyinstaller packaging/ParkTileArchiver.spec
#
# Produces dist/Park Tile Archiver.app on macOS and dist/ParkTileArchiver.exe
# on Windows. `build-app.sh` wraps this with the venv setup.

import sys

block_cipher = None

# Qt ships a great deal that a form with a progress bar has no use for.
# Excluding it keeps the bundle to a sane size.
EXCLUDES = [
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtNfc", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning", "PySide6.QtQml", "PySide6.QtQuick",
    "PySide6.QtQuick3D", "PySide6.QtQuickControls2", "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSpatialAudio", "PySide6.QtSql",
    "PySide6.QtStateMachine", "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtTest", "PySide6.QtTextToSpeech", "PySide6.QtUiTools",
    "PySide6.QtWebChannel", "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets",
    "matplotlib", "numpy", "PIL", "tkinter", "pytest",
]

analysis = Analysis(
    ["../src/tilearc_gui/__main__.py"],
    pathex=["../src"],
    binaries=[],
    datas=[],
    hiddenimports=["tilearc", "tilearc_gui"],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="ParkTileArchiver",
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
)

collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ParkTileArchiver",
)

if sys.platform == "darwin":
    app = BUNDLE(
        collection,
        name="Park Tile Archiver.app",
        icon=None,
        bundle_identifier="com.magicparksexplorer.parktilearchiver",
        info_plist={
            "CFBundleName": "Park Tile Archiver",
            "CFBundleDisplayName": "Park Tile Archiver",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "NSHighResolutionCapable": True,
            # Downloads go to a folder the user picks in a standard open panel.
            "NSHumanReadableCopyright": "Magic Parks Explorer",
            "LSMinimumSystemVersion": "11.0",
        },
    )

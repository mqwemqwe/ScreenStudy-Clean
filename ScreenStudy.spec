# Build locally on macOS: python -m PyInstaller ScreenStudy.spec
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

root = SPECPATH
hidden = []
for module in ['ScreenCaptureKit', 'Vision', 'Quartz', 'ApplicationServices', 'AppKit']:
    hidden += collect_submodules(module)
hidden += ['keyring.backends.macOS']
data = collect_data_files('certifi')
a = Analysis([os.path.join(root, 'main.py')], pathex=[root], binaries=[],
             datas=data, hiddenimports=hidden, hookspath=[], hooksconfig={},
             runtime_hooks=[], excludes=['tkinter', 'PySide6.QtWebEngineCore',
             'PySide6.QtWebEngineWidgets', 'PySide6.QtQml', 'PySide6.QtQuick'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='ScreenStudy', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          argv_emulation=False, target_arch='arm64', codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='ScreenStudy')
app = BUNDLE(coll, name='ScreenStudy.app', icon=None,
    bundle_identifier='app.screenstudy.desktop', info_plist={
        'CFBundleName': 'ScreenStudy', 'CFBundleDisplayName': 'ScreenStudy',
        'CFBundleShortVersionString': '1.3.0', 'CFBundleVersion': '7',
        'NSHighResolutionCapable': True, 'LSMinimumSystemVersion': '14.0',
        'NSPrincipalClass': 'NSApplication',
        'NSScreenCaptureUsageDescription': 'ScreenStudy читает выбранную область экрана, чтобы объяснять задания.',
    })

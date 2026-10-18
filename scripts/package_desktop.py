"""Build the self-contained Windows console from the repository root."""
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
extra = ['--add-data', str(root / '.local/client-defaults.json') + ';.'] if (root / '.local/client-defaults.json').exists() else []
subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
    '--name', '0x4b6f757a7a', '--add-data', str(root / 'client') + ';client',
    *extra,
    '--collect-all', 'webview', '--hidden-import', 'webview.platforms.edgechromium',
    str(root / 'desktop.py')], cwd=root, check=True)

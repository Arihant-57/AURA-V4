"""Create an on-demand Desktop shortcut for AURA V4 Jarvis (no boot autostart)."""

from __future__ import annotations

import base64
from pathlib import Path
import subprocess
import sys


def main() -> int:
    if sys.platform != "win32":
        print("Desktop shortcut creation is supported only on Windows.")
        return 1

    repo_root = Path(__file__).resolve().parent.parent
    target_bat = repo_root / "launch_aura_jarvis.bat"
    if not target_bat.is_file():
        print(f"Missing launcher: {target_bat}")
        return 1

    desktop_dir = Path.home() / "Desktop"
    if not desktop_dir.is_dir():
        print(f"Desktop folder not found: {desktop_dir}")
        return 1

    lnk_path = desktop_dir / "AURA Jarvis.lnk"
    ps = f"""
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut('{str(lnk_path)}')
$sc.TargetPath = '{str(target_bat)}'
$sc.WorkingDirectory = '{str(repo_root)}'
$sc.Description = 'Launch AURA V4 Jarvis on demand (0 idle RAM/VRAM when off)'
$sc.Save()
"""
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], check=False)
    if res.returncode == 0:
        print(f"Created manual shortcut: {lnk_path}")
    return res.returncode


if __name__ == "__main__":
    raise SystemExit(main())

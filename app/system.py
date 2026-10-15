"""Ярлык на рабочем столе и автозапуск при включении компьютера.

Работает только в собранной программе (MiniPOS.exe / MiniPOS.app).
Windows: ярлык в папке «Автозагрузка» (Startup) текущего пользователя.
Mac:     LaunchAgent в ~/Library/LaunchAgents.
"""
import os
import subprocess
import sys
from pathlib import Path

from . import VERSION, config

FROZEN = getattr(sys, "frozen", False)
MAC_AGENT = Path.home() / "Library" / "LaunchAgents" / "kz.mizonyalyan.minipos.plist"


def _win_startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _mac_app_path() -> Path:
    return Path(sys.executable).resolve().parents[2]          # …/MiniPOS.app/Contents/MacOS/MiniPOS → MiniPOS.app


def _win_shortcut(folder_expr: str) -> None:
    """Создаёт MiniPOS.lnk. folder_expr — выражение PowerShell для папки."""
    exe = sys.executable.replace("'", "''")
    ps = (f"$d={folder_expr};"
          "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\\MiniPOS.lnk\");"
          f"$s.TargetPath='{exe}';$s.WorkingDirectory='{os.path.dirname(exe)}';$s.IconLocation='{exe},0';$s.Save()")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=0x08000000,
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300] or "PowerShell error")


def info() -> dict:
    return {
        "version": VERSION,
        "data_dir": str(config.DATA_DIR),
        "platform": {"win32": "windows", "darwin": "mac"}.get(sys.platform, "other"),
        "installed": FROZEN,
        "autostart": autostart_enabled(),
    }


def autostart_enabled() -> bool:
    if not FROZEN:
        return False
    if sys.platform == "win32":
        return (_win_startup_dir() / "MiniPOS.lnk").exists()
    if sys.platform == "darwin":
        return MAC_AGENT.exists()
    return False


def set_autostart(enabled: bool) -> bool:
    if not FROZEN:
        raise RuntimeError("Автозапуск работает только в установленной программе (MiniPOS.exe / MiniPOS.app)")
    if sys.platform == "win32":
        lnk = _win_startup_dir() / "MiniPOS.lnk"
        if enabled:
            _win_shortcut("[Environment]::GetFolderPath('Startup')")
        elif lnk.exists():
            lnk.unlink()
    elif sys.platform == "darwin":
        if enabled:
            MAC_AGENT.parent.mkdir(parents=True, exist_ok=True)
            MAC_AGENT.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>kz.mizonyalyan.minipos</string>
  <key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-a</string><string>{_mac_app_path()}</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
""", encoding="utf-8")
        elif MAC_AGENT.exists():
            MAC_AGENT.unlink()
    else:
        raise RuntimeError("Не поддерживается на этой системе")
    return autostart_enabled()


def create_desktop_shortcut() -> str:
    if not FROZEN:
        raise RuntimeError("Ярлык создаётся только для установленной программы (MiniPOS.exe)")
    if sys.platform == "win32":
        _win_shortcut("[Environment]::GetFolderPath('Desktop')")
        return "Ярлык MiniPOS создан на рабочем столе"
    if sys.platform == "darwin":
        raise RuntimeError("На Mac перетащите MiniPOS из «Программ» в Dock — так быстрее всего")
    raise RuntimeError("Не поддерживается на этой системе")

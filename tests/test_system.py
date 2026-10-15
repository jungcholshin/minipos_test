"""Автозапуск (Mac-вариант проверяется на любой системе через подмену)."""
import sys

import pytest

from app import system


def test_not_installed_mode():
    assert system.info()["installed"] is False
    with pytest.raises(RuntimeError):
        system.set_autostart(True)


def test_mac_autostart(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "FROZEN", True)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(system, "MAC_AGENT", tmp_path / "agent.plist")
    monkeypatch.setattr(sys, "executable", "/Applications/MiniPOS.app/Contents/MacOS/MiniPOS")
    assert system.set_autostart(True) is True
    assert "/Applications/MiniPOS.app" in (tmp_path / "agent.plist").read_text()
    assert system.set_autostart(False) is False


def test_windows_autostart_off(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "FROZEN", True)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    lnk = tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "MiniPOS.lnk"
    lnk.parent.mkdir(parents=True)
    lnk.write_text("x")
    assert system.autostart_enabled() is True
    assert system.set_autostart(False) is False and not lnk.exists()

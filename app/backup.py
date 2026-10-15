"""Ежедневная резервная копия базы SQLite.

Раз в день (и сразу при запуске, если сегодня копии ещё нет) база копируется в BACKUP_DIR
как minipos-ГГГГ-ММ-ДД.db. Старые копии старше BACKUP_KEEP_DAYS удаляются.
Совет: укажите в settings.env папку iCloud Drive или Google Диска — копии попадут в облако бесплатно.
"""
import logging
import sqlite3
import threading
import time
from datetime import date, timedelta

from . import config

log = logging.getLogger("minipos.backup")
_started = False


def db_file():
    url = config.DATABASE_URL
    return url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else None


def make_backup() -> str | None:
    src = db_file()
    if not src:
        return None  # для PostgreSQL используйте pg_dump
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = config.BACKUP_DIR / f"minipos-{date.today().isoformat()}.db"
    if dst.exists():
        return str(dst)
    # безопасная копия работающей базы через backup API SQLite
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        s.backup(d)
    cutoff = date.today() - timedelta(days=config.BACKUP_KEEP_DAYS)
    for f in config.BACKUP_DIR.glob("minipos-*.db"):
        try:
            if date.fromisoformat(f.stem.replace("minipos-", "")) < cutoff:
                f.unlink()
        except ValueError:
            pass
    log.info("Резервная копия: %s", dst)
    return str(dst)


def _loop():
    while True:
        try:
            make_backup()
        except Exception as e:  # копия не должна ронять программу
            log.warning("Не удалось сделать копию: %s", e)
        time.sleep(3600)  # проверка раз в час, копия создаётся раз в день


def start():
    global _started
    if not _started and db_file():
        _started = True
        threading.Thread(target=_loop, daemon=True, name="backup").start()

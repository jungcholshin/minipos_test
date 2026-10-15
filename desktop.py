"""Запуск MiniPOS как обычной программы (окно без браузера и терминала).

Из этого файла собирается MiniPOS.exe (см. .github/workflows/build-windows.yml).
1. Запускает сервер программы на http://127.0.0.1:8000 (в фоне).
2. Открывает окно программы (pywebview). Если окно открыть нельзя — открывает браузер.
3. При закрытии окна программа завершается.
Если MiniPOS уже запущен — просто открывает ещё одно окно к нему.
"""
import logging
import os
import socket
import sys
import threading
import time
import webbrowser

HOST, PORT = "127.0.0.1", 8000
URL = f"http://{HOST}:{PORT}"


def setup_logging():
    from app.config import DATA_DIR
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(DATA_DIR / "minipos.log", encoding="utf-8")]
        + ([logging.StreamHandler()] if sys.stdout else []),
    )


def port_busy() -> bool:
    with socket.socket() as s:
        return s.connect_ex((HOST, PORT)) == 0


def run_server():
    import uvicorn
    from app.main import app
    uvicorn.run(app, host=HOST, port=PORT, log_config=None, log_level="info")


def wait_until_up(timeout=30) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if port_busy():
            return True
        time.sleep(0.2)
    return False


def create_windows_shortcut(log):
    """При первом запуске MiniPOS.exe создаёт ярлык на рабочем столе Windows."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    from app.config import DATA_DIR
    marker = DATA_DIR / ".shortcut_created"
    if marker.exists():
        return
    try:
        from app.system import create_desktop_shortcut
        create_desktop_shortcut()
        marker.write_text("ok")
        log.info("Ярлык MiniPOS создан на рабочем столе")
    except Exception as e:
        log.warning("Не удалось создать ярлык: %s", e)


def main():
    setup_logging()
    log = logging.getLogger("minipos")
    create_windows_shortcut(log)
    if not port_busy():
        threading.Thread(target=run_server, daemon=True).start()
        if not wait_until_up():
            log.error("Сервер не запустился, см. minipos.log")
            sys.exit(1)
    try:
        import webview
        webview.create_window("MiniPOS — mizon yalyan", URL, width=1280, height=820, min_size=(900, 600))
        webview.start()
        os._exit(0)  # окно закрыто → выходим
    except Exception as e:
        log.warning("Окно не открылось (%s), открываю браузер", e)
        webbrowser.open(URL)
        while True:  # сервер работает, пока процесс жив
            time.sleep(3600)


if __name__ == "__main__":
    main()

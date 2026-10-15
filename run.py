"""Запуск для разработки: python run.py  → откройте http://127.0.0.1:8000
(Для обычного использования — MiniPOS.exe или python desktop.py)"""
import logging

import uvicorn

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # reload выключен, чтобы Telegram-бот не запускался дважды
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000)

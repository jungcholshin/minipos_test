"""Telegram-бот: тот же ИИ-помощник, но с телефона.

Работает на том же компьютере, что и программа. Сервер в интернете не нужен:
бот сам забирает сообщения из Telegram (long polling).

Безопасность: бот отвечает только тем, чьи Telegram ID указаны в TELEGRAM_ALLOWED_IDS.
Любое изменение данных — только после нажатия кнопки «Подтвердить».
"""
import logging
import threading
import time

import httpx

from . import ai, config, reports, services
from .db import SessionLocal

log = logging.getLogger("minipos.telegram")
API = "https://api.telegram.org/bot{token}/{method}"

_stop = threading.Event()
_thread: threading.Thread | None = None
_histories: dict[int, list] = {}  # chat_id -> последние сообщения

HELP = (
    "Я помощник магазина mizon yalyan. Пишите обычным языком, например:\n"
    "• Сколько осталось Beauty of Joseon?\n"
    "• Что заканчивается?\n"
    "• Выручка за неделю\n"
    "• Пришло 10 KERASYS Damage Clinic\n"
    "• Списать 1 BERGAMO, брак\n\n"
    "Команды: /low — заканчивается, /today — продажи за сегодня, /reset — очистить диалог"
)


def tg(method: str, **params):
    r = httpx.post(API.format(token=config.TELEGRAM_BOT_TOKEN, method=method), json=params, timeout=40)
    return r.json()


def send(chat_id: int, text: str, actions: list | None = None):
    params = {"chat_id": chat_id, "text": text[:4000]}
    tg("sendMessage", **params)
    for a in actions or []:
        tg("sendMessage", chat_id=chat_id, text="⚠️ " + a["summary"],
           reply_markup={"inline_keyboard": [[
               {"text": "✅ Подтвердить", "callback_data": f"ok:{a['id']}"},
               {"text": "✖️ Отмена", "callback_data": f"no:{a['id']}"},
           ]]})


def money(n) -> str:
    return f"{n:,}".replace(",", " ") + " ₸"


def handle_message(chat_id: int, user_id: int, text: str):
    if user_id not in config.TELEGRAM_ALLOWED_IDS:
        send(chat_id, f"Доступ закрыт.\nВаш Telegram ID: {user_id}\n"
                      "Добавьте его в settings.env → TELEGRAM_ALLOWED_IDS и перезапустите MiniPOS.")
        return
    text = (text or "").strip()
    if text in ("/start", "/help"):
        return send(chat_id, HELP)
    if text == "/reset":
        _histories.pop(chat_id, None)
        return send(chat_id, "Диалог очищен.")
    with SessionLocal() as db:
        if text == "/low":
            low = reports.low_stock(db)
            return send(chat_id, "Заканчивается:\n" + "\n".join(
                f"• {x['name']} — {x['stock']} (мин. {x['minimum']})" for x in low) if low else "Всё в наличии 👍")
        if text == "/today":
            from datetime import date
            r = reports.sales_summary(db, date.today(), date.today())
            return send(chat_id, f"Сегодня: {r['sales_count']} продаж\nВыручка: {money(r['revenue'])}\n"
                                 f"Прибыль: {money(r['profit'])} ({r['margin']}%)")
        hist = _histories.setdefault(chat_id, [])
        hist.append({"role": "user", "content": text})
        tg("sendChatAction", chat_id=chat_id, action="typing")
        try:
            res = ai.chat(db, hist, source="telegram")
        except ai.AIError as e:
            hist.pop()
            return send(chat_id, "⚠️ " + str(e))
        hist.append({"role": "assistant", "content": res["reply"]})
        del hist[:-ai.HISTORY_LIMIT]
        send(chat_id, res["reply"], res["actions"])


def handle_callback(cb: dict):
    user_id = cb["from"]["id"]
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    if user_id not in config.TELEGRAM_ALLOWED_IDS:
        return tg("answerCallbackQuery", callback_query_id=cb["id"], text="Нет доступа")
    kind, _, action_id = cb.get("data", "").partition(":")
    with SessionLocal() as db:
        try:
            result = ai.confirm_action(db, int(action_id)) if kind == "ok" else ai.cancel_action(db, int(action_id))
            mark = "✅" if kind == "ok" else "✖️"
        except services.BusinessError as e:
            result, mark = str(e), "⚠️"
    tg("answerCallbackQuery", callback_query_id=cb["id"], text=result[:190])
    tg("editMessageText", chat_id=chat_id, message_id=msg.get("message_id"),
       text=f"{msg.get('text', '')}\n\n{mark} {result}")


def _loop():
    offset = 0
    me = tg("getMe")
    log.info("Telegram-бот запущен: @%s", me.get("result", {}).get("username"))
    while not _stop.is_set():
        try:
            data = tg("getUpdates", offset=offset, timeout=30, allowed_updates=["message", "callback_query"])
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    if "message" in upd and "text" in upd["message"]:
                        m = upd["message"]
                        handle_message(m["chat"]["id"], m["from"]["id"], m["text"])
                    elif "callback_query" in upd:
                        handle_callback(upd["callback_query"])
                except Exception as e:
                    log.exception("Ошибка обработки сообщения: %s", e)
        except Exception as e:
            log.warning("Нет связи с Telegram: %s. Повтор через 10 с", e)
            time.sleep(10)


def start():
    global _thread
    if not config.TELEGRAM_BOT_TOKEN or (_thread and _thread.is_alive()):
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="telegram")
    _thread.start()


def stop():
    _stop.set()

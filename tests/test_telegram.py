"""Тест логики Telegram-бота без настоящего Telegram."""
import pytest

from app import ai, config, telegram_bot


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(telegram_bot, "tg", lambda method, **p: out.append((method, p)) or {"ok": True})
    return out


def test_unknown_user_gets_id(sent, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_IDS", {111})
    telegram_bot.handle_message(5, 999, "привет")
    assert "999" in sent[0][1]["text"] and "Доступ закрыт" in sent[0][1]["text"]


def test_allowed_user_gets_ai_reply_with_buttons(sent, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_IDS", {111})
    monkeypatch.setattr(ai, "chat", lambda db, hist, source: {
        "reply": "Подтвердите", "actions": [{"id": 7, "summary": "Поступление +3"}]})
    telegram_bot.handle_message(5, 111, "пришло 3")
    texts = [p.get("text") for m, p in sent if m == "sendMessage"]
    assert texts == ["Подтвердите", "⚠️ Поступление +3"]
    buttons = [p for m, p in sent if "reply_markup" in p][0]["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["ok:7", "no:7"]

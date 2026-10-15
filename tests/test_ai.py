"""Тесты ИИ-помощника без настоящей модели: модель подменяется заглушкой."""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import ai, services
from app.db import Base
from app.models import Category, PendingAction, Product


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    c = Category(name="Для волос")
    s.add(c)
    s.flush()
    s.add_all([
        Product(name="KERASYS Hair Clinic Damage Clinic", category_id=c.id, purchase_price=2100,
                selling_price=3500, stock_quantity=5, minimum_stock=3, barcode="2000000000007"),
        Product(name="KERASYS Hair Clinic Scalp Clinic", category_id=c.id, purchase_price=2100,
                selling_price=3500, stock_quantity=2, minimum_stock=3),
    ])
    s.commit()
    yield s
    s.close()


def test_resolve_unique_and_ambiguous(db):
    assert ai.resolve_product(db, "damage").id == 1
    assert ai.resolve_product(db, "2000000000007").id == 1
    with pytest.raises(services.BusinessError, match="несколько"):
        ai.resolve_product(db, "kerasys")
    with pytest.raises(services.BusinessError, match="не найден"):
        ai.resolve_product(db, "missha")


def test_write_tool_only_proposes_then_confirm(db):
    res = ai.p_receive_stock(db, "web", product="damage", quantity=10)
    assert db.get(Product, 1).stock_quantity == 5          # ничего не изменилось
    assert db.get(PendingAction, res["action_id"]).status == "PENDING"
    ai.confirm_action(db, res["action_id"])
    assert db.get(Product, 1).stock_quantity == 15
    with pytest.raises(services.BusinessError):
        ai.confirm_action(db, res["action_id"])            # повторно нельзя


def test_cancel_and_hide(db):
    res = ai.p_hide_product(db, "web", product="scalp")
    ai.cancel_action(db, res["action_id"])
    assert db.get(Product, 2).is_active
    res = ai.p_hide_product(db, "web", product="scalp")
    ai.confirm_action(db, res["action_id"])
    assert not db.get(Product, 2).is_active


def test_write_off_more_than_stock_rejected(db):
    with pytest.raises(services.BusinessError):
        ai.p_write_off(db, "web", product="scalp", quantity=5)


def test_chat_loop_with_fake_model(db, monkeypatch):
    replies = iter([
        {"content": "", "tool_calls": [{"id": "1", "type": "function", "function": {
            "name": "receive_stock", "arguments": json.dumps({"product": "damage", "quantity": 3})}}]},
        {"content": "Подтвердите поступление 3 шт."},
    ])
    monkeypatch.setattr(ai, "call_model", lambda messages: next(replies))
    out = ai.chat(db, [{"role": "user", "content": "пришло 3 damage clinic"}])
    assert out["reply"].startswith("Подтвердите")
    assert len(out["actions"]) == 1 and "+3" in out["actions"][0]["summary"]


def test_low_stock_tool(db):
    names = [x["name"] for x in ai.t_low_stock(db)["low_stock"]]
    assert names == ["KERASYS Hair Clinic Scalp Clinic"]

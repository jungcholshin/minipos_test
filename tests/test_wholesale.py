"""Опт/розница и условия для оптовых клиентов."""
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config, services
from app.db import Base
from app.models import Category, Customer, Product, Sale


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(config, "RETAIL_MARKUP", 1.5)
    monkeypatch.setattr(config, "RETAIL_ROUND", 100)
    monkeypatch.setattr(config, "WHOLESALE_FIRST_MIN", 100_000)
    monkeypatch.setattr(config, "WHOLESALE_MONTHLY_MIN", 30_000)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    c = Category(name="Тест")
    s.add(c)
    s.flush()
    s.add(Product(name="Крем", category_id=c.id, purchase_price=3000, wholesale_price=5000,
                  selling_price=7500, stock_quantity=100))
    s.add(Customer(name="ИП Ким"))
    s.commit()
    yield s
    s.close()


def test_retail_from_wholesale():
    assert services.retail_from_wholesale(5000) == 7500
    assert services.retail_from_wholesale(4330) == 6500   # 6495 → округление до 100


def test_wholesale_sale_uses_wholesale_price(db):
    sale = services.create_sale(db, [{"product_id": 1, "quantity": 20}], "TRANSFER", "WHOLESALE", 1)
    assert sale.total_amount == 100_000 and sale.price_type == "WHOLESALE"
    retail = services.create_sale(db, [{"product_id": 1, "quantity": 1}], "CASH")
    assert retail.total_amount == 7500 and retail.price_type == "RETAIL"


def test_wholesale_requires_client(db):
    with pytest.raises(services.BusinessError, match="клиента"):
        services.create_sale(db, [{"product_id": 1, "quantity": 30}], "CASH", "WHOLESALE")


def test_first_purchase_minimum(db):
    with pytest.raises(services.BusinessError, match="Первый закуп"):
        services.create_sale(db, [{"product_id": 1, "quantity": 2}], "CASH", "WHOLESALE", 1)
    assert db.get(Product, 1).stock_quantity == 100      # откат
    services.create_sale(db, [{"product_id": 1, "quantity": 20}], "CASH", "WHOLESALE", 1)
    services.create_sale(db, [{"product_id": 1, "quantity": 2}], "CASH", "WHOLESALE", 1)  # дальше — любые суммы


def _sale(db, when, amount):
    db.add(Sale(total_amount=amount, payment_method="CASH", price_type="WHOLESALE", customer_id=1, created_at=when))
    db.commit()


def test_status_new(db):
    st = services.customer_stats(db, db.get(Customer, 1), date(2026, 10, 15))
    assert st["status"] == "NEW"


def test_status_violation_previous_month(db):
    _sale(db, datetime(2026, 8, 5), 120_000)   # первый закуп
    _sale(db, datetime(2026, 9, 3), 10_000)    # сентябрь < 30 000
    st = services.customer_stats(db, db.get(Customer, 1), date(2026, 10, 15))
    assert st["status"] == "VIOLATION" and st["missed_months"] == ["2026-09"]


def test_status_ok_and_in_progress(db):
    _sale(db, datetime(2026, 8, 5), 120_000)
    _sale(db, datetime(2026, 9, 3), 40_000)
    st = services.customer_stats(db, db.get(Customer, 1), date(2026, 10, 10))
    assert st["status"] == "IN_PROGRESS" and st["month_left"] == 30_000
    st = services.customer_stats(db, db.get(Customer, 1), date(2026, 10, 27))
    assert st["status"] == "AT_RISK"
    _sale(db, datetime(2026, 10, 12), 30_000)
    st = services.customer_stats(db, db.get(Customer, 1), date(2026, 10, 27))
    assert st["status"] == "OK"


def test_first_month_counts_as_done(db):
    _sale(db, datetime(2026, 10, 2), 100_000)
    st = services.customer_stats(db, db.get(Customer, 1), date(2026, 10, 28))
    assert st["status"] == "OK"


def test_returned_sales_do_not_count(db):
    _sale(db, datetime(2026, 8, 5), 120_000)
    db.add(Sale(total_amount=50_000, payment_method="CASH", price_type="WHOLESALE", customer_id=1,
                created_at=datetime(2026, 9, 3), status="RETURNED"))
    db.commit()
    st = services.customer_stats(db, db.get(Customer, 1), date(2026, 10, 15))
    assert st["missed_months"] == ["2026-09"]


def test_discount_percent_spread_over_items(db):
    db.add(Product(name="Тоник", category_id=1, purchase_price=2000, wholesale_price=3000,
                   selling_price=4500, stock_quantity=10))
    db.commit()
    sale = services.create_sale(db, [{"product_id": 1, "quantity": 1}, {"product_id": 2, "quantity": 1}],
                                "CASH", discount_percent=10)
    assert sale.discount_amount == 1200 and sale.total_amount == 10_800
    assert sum(i.subtotal for i in sale.items) == 10_800 and sum(i.discount for i in sale.items) == 1200


def test_discount_limit(db):
    with pytest.raises(services.BusinessError, match="30%"):
        services.create_sale(db, [{"product_id": 1, "quantity": 1}], "CASH", discount_percent=50)
    with pytest.raises(services.BusinessError):
        services.create_sale(db, [{"product_id": 1, "quantity": 1}], "CASH", discount_amount=5000)


def test_gift_is_free_but_costs(db):
    sale = services.create_sale(db, [{"product_id": 1, "quantity": 2}, {"product_id": 1, "quantity": 1, "gift": True}],
                                "CASH")
    assert sale.total_amount == 15_000
    gift = [i for i in sale.items if i.is_gift][0]
    assert gift.subtotal == 0 and gift.purchase_price == 3000
    assert db.get(Product, 1).stock_quantity == 97      # подарок списан со склада
    from app.reports import sales_summary
    r = sales_summary(db)
    assert r["gifts_qty"] == 1 and r["gifts_cost"] == 3000 and r["profit"] == 15_000 - 9000


def test_gift_counts_stock_together(db):
    db.get(Product, 1).stock_quantity = 2
    db.commit()
    with pytest.raises(services.BusinessError, match="Недостаточно"):
        services.create_sale(db, [{"product_id": 1, "quantity": 2}, {"product_id": 1, "quantity": 1, "gift": True}],
                             "CASH")

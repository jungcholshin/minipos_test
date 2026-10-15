"""Тесты из раздела 38 ТЗ. Каждый тест работает на отдельной временной БД."""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import services
from app.db import Base
from app.models import Category, Product, SaleItem, StockMovement


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    cat = Category(name="Тест")
    session.add(cat)
    session.flush()
    session.add_all([
        Product(name="Крем", category_id=cat.id, purchase_price=700, selling_price=1200, stock_quantity=10),
        Product(name="Тоник", category_id=cat.id, purchase_price=500, selling_price=900, stock_quantity=0),
    ])
    session.commit()
    yield session
    session.close()


# ---------- unit ----------
def test_calculate_total():
    assert services.calculate_total([(1200, 2), (1800, 1), (700, 2)]) == 5600


def test_profit_and_margin():
    revenue, cost = 12_000, 7_000
    assert services.calculate_profit(revenue, cost) == 5_000
    assert services.calculate_margin(revenue, cost) == 41.67
    assert services.calculate_margin(0, 0) == 0.0


# ---------- integration ----------
def test_sale_decreases_stock_and_creates_records(db):
    sale = services.create_sale(db, [{"product_id": 1, "quantity": 3}], "CARD")
    assert sale.total_amount == 3600
    assert db.get(Product, 1).stock_quantity == 7
    item = db.execute(select(SaleItem)).scalar_one()
    assert (item.unit_price, item.purchase_price) == (1200, 700)  # цена на момент продажи
    mv = db.execute(select(StockMovement).where(StockMovement.movement_type == "SALE")).scalar_one()
    assert mv.quantity == -3


def test_price_change_does_not_affect_old_sale(db):
    services.create_sale(db, [{"product_id": 1, "quantity": 1}], "CASH")
    db.get(Product, 1).selling_price = 9999
    db.commit()
    assert db.execute(select(SaleItem)).scalar_one().unit_price == 1200


# ---------- edge cases ----------
def test_sale_with_zero_stock_fails(db):
    with pytest.raises(services.BusinessError):
        services.create_sale(db, [{"product_id": 2, "quantity": 1}], "CASH")


def test_quantity_more_than_stock_fails_and_rolls_back(db):
    # первая позиция в порядке, вторая — нет: ничего не должно списаться
    with pytest.raises(services.BusinessError):
        services.create_sale(db, [{"product_id": 1, "quantity": 2}, {"product_id": 1, "quantity": 20}], "CASH")
    assert db.get(Product, 1).stock_quantity == 10
    assert db.execute(select(SaleItem)).first() is None


def test_return_increases_stock(db):
    sale = services.create_sale(db, [{"product_id": 1, "quantity": 4}], "CASH")
    services.return_sale(db, sale.id)
    assert db.get(Product, 1).stock_quantity == 10
    with pytest.raises(services.BusinessError):
        services.return_sale(db, sale.id)  # повторный возврат запрещён


def test_receive_and_adjust(db):
    services.receive_stock(db, 2, 5)
    assert db.get(Product, 2).stock_quantity == 5
    services.adjust_stock(db, 2, 3)
    assert db.get(Product, 2).stock_quantity == 3
    with pytest.raises(services.BusinessError):
        services.adjust_stock(db, 2, -1)

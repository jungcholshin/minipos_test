"""Тестовые данные: часть товаров из WhatsApp-каталога mizon yalyan.

Продажные цены взяты из каталога. Закупочные цены, остатки и штрихкоды —
ТЕСТОВЫЕ (придуманы для проверки работы), их нужно заменить реальными.
Штрихкоды в диапазоне 20xxxxxxxxxxx — это диапазон для внутреннего использования магазином.
"""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, services
from .models import Category, Customer, Product, StockMovement

# категория -> [(название, цена продажи, остаток, минимум)]
CATALOG = {
    "Отбеливающие средства": [
        ("By Wishtrend Mandelic Acid Dark Spot Correcting Serum, 30 мл", 7000, 12, 3),
        ("Torriden CELLMAZING Brightening Ampoule, 30 мл", 7500, 8, 3),
        ("PRETTYSKIN The Pure Jeju Tangerine Vita C Toner", 4800, 15, 4),
        ("BERGAMO Vita Shine Dark Spot Cream", 4800, 2, 3),
        ("Celimax Pore + Dark Spot Brightening Serum", 6500, 10, 3),
        ("Heimish RX Multi Vitamin Dark Spot Cream", 9000, 5, 2),
    ],
    "Для волос и кожи головы": [
        ("KERASYS Hair Clinic Damage Clinic", 3500, 20, 5),
        ("KERASYS Hair Clinic Scalp Clinic", 3500, 18, 5),
        ("Dr. Smart All in One K-Ginseng Shampoo", 6700, 6, 2),
        ("HAARAZ Kelp Shampoo", 9500, 4, 2),
        ("PYEONAN 5 Minute Speed Hair Color — Dark Brown", 4000, 9, 3),
    ],
    "Для сухой, чувствительной кожи": [
        ("SKIN1004 Madagascar Centella Ampoule", 5200, 14, 4),
        ("SKIN1004 Madagascar Centella Cream", 7500, 7, 3),
        ("DR.ALTHEA 147 Barrier Cream", 7500, 3, 3),
        ("Celimax The Real Cica Soothing Cream", 5800, 11, 3),
        ("ROUND LAB 1025 Dokdo Toner", 6500, 16, 4),
        ("ROUND LAB 1025 Dokdo Bubble Foam", 4600, 1, 3),
    ],
    "Маски для лица, патчи": [
        ("PEKAH Collagen Modeling Mask", 7300, 10, 3),
        ("Torriden DIVE IN Modeling Pack", 2400, 25, 6),
        ("Heimish Bulgarian Rose Water Hydrogel Eye Patch", 5400, 8, 3),
    ],
    "Антивозрастная косметика": [
        ("Celimax The Vita-A Retinol Shot Tightening Serum", 6500, 9, 3),
        ("ROUND LAB Camellia Deep Collagen Lifting Gua Sha Cream", 6700, 5, 2),
        ("C·K·D Retino Collagen Guasha Lifting Serum", 8500, 4, 2),
    ],
    "SPF, тональные крема, кушоны": [
        ("Beauty of Joseon Relief Sun SPF50+ PA++++", 6500, 22, 5),
        ("SKIN1004 Madagascar Centella Poremizing Velvet Finish Sunscreen SPF50+", 6300, 12, 4),
        ("MISSHA Glow B.B Boomer", 3600, 7, 3),
        ("PRETTYSKIN Cheongdam Collagen BB Cushion SPF50+ PA++++", 9000, 3, 2),
    ],
}


def test_purchase_price(selling: int) -> int:
    """Тестовая закупочная цена ≈ 55% от розничной, округлённая до 100 ₸."""
    return round(selling * 0.55 / 100) * 100


def wholesale_from_retail(retail: int) -> int:
    """Оптовая цена из розничной: розница / RETAIL_MARKUP, округлённая до 10 ₸."""
    return int(round(retail / config.RETAIL_MARKUP / 10) * 10)


def fill_wholesale_prices(db: Session, all_products: bool = False) -> None:
    """Проставляет оптовую цену товарам, у которых её нет (или всем — при переходе со старой версии)."""
    stmt = select(Product) if all_products else select(Product).where(Product.wholesale_price.is_(None))
    for p in db.execute(stmt).scalars():
        p.wholesale_price = wholesale_from_retail(p.selling_price)
    db.commit()


# Тестовые оптовые клиенты: (название, город, сколько месяцев назад первый закуп, закупы по месяцам)
DEMO_CLIENTS = [
    ("Косметолог Айгерим", "Алматы", [(2, 120_000), (1, 45_000), (0, 35_000)]),   # всё выполняет
    ("Салон «Beauty Room»", "Астана", [(2, 150_000), (1, 10_000)]),             # нарушил прошлый месяц
    ("ИП Ким", "Алматы", [(1, 105_000)]),                                      # в этом месяце ещё не закупал
]


def seed_demo_clients(db: Session) -> None:
    """Создаёт тестовых оптовиков с закупами в прошлых месяцах (только для новой базы)."""
    products = db.execute(select(Product).order_by(Product.selling_price.desc())).scalars().all()
    now = datetime.now()
    for name, city, purchases in DEMO_CLIENTS:
        c = Customer(name=name, city=city, phone="+7 700 000 00 00", note="Тестовый клиент")
        db.add(c)
        db.commit()
        for months_ago, amount in purchases:
            items, total = [], 0
            for p in products:                     # набираем товары на нужную сумму
                if total >= amount:
                    break
                qty = max(1, min(3, (amount - total) // p.wholesale_price))
                if p.stock_quantity < qty + 2:
                    continue
                items.append({"product_id": p.id, "quantity": int(qty)})
                total += p.wholesale_price * qty
            sale = services.create_sale(db, items, "TRANSFER", "WHOLESALE", c.id)
            month = now.month - months_ago
            year = now.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            sale.created_at = now.replace(year=year, month=month, day=min(now.day, 10), hour=12) \
                if months_ago else now - timedelta(hours=2)
            db.commit()


def seed(db: Session) -> None:
    if not config.DEMO_DATA:
        return  # пустая база: товары загружаются из Excel или вводятся вручную
    if db.execute(select(Product.id).limit(1)).first():
        return  # уже заполнено
    n = 0
    for cat_name, items in CATALOG.items():
        cat = Category(name=cat_name)
        db.add(cat)
        db.flush()
        for name, price, stock, minimum in items:
            n += 1
            stock = stock * 3 if stock > minimum else stock   # запас побольше для тестовых оптовых продаж
            p = Product(
                name=name, category_id=cat.id,
                barcode=f"20000000{n:05d}", sku=f"MZ-{n:04d}",
                selling_price=price, purchase_price=test_purchase_price(price),
                wholesale_price=wholesale_from_retail(price),
                stock_quantity=stock, minimum_stock=minimum, unit="шт.",
            )
            db.add(p)
            db.flush()
            db.add(StockMovement(product_id=p.id, movement_type="PURCHASE", quantity=stock,
                                 note="Начальный остаток (тест)"))
    db.commit()
    seed_demo_clients(db)

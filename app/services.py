"""Бизнес-логика: продажа, возврат, поступление, расчёты (разделы 10, 12, 15, 24 ТЗ)."""
import calendar
import math
from datetime import date
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import config
from .models import Customer, Product, Sale, SaleItem, StockMovement

PAYMENT_METHODS = {"CASH", "CARD", "TRANSFER", "OTHER"}
PRICE_TYPES = {"RETAIL", "WHOLESALE"}


def money(n: int) -> str:
    """12345 → «12 345 ₸»"""
    return f"{n:,}".replace(",", " ") + " ₸"


class BusinessError(Exception):
    """Ошибка бизнес-правила (нет товара, не хватает остатка и т.п.)."""


# ---------- расчёты ----------
def calculate_total(items: list[tuple[int, int]]) -> int:
    """items: [(цена, количество), ...]"""
    return sum(price * qty for price, qty in items)


def calculate_profit(revenue: int, cost: int) -> int:
    return revenue - cost


def calculate_margin(revenue: int, cost: int) -> float:
    if revenue == 0:
        return 0.0
    return round((revenue - cost) / revenue * 100, 2)


# ---------- цены ----------
def retail_from_wholesale(wholesale: int) -> int:
    """Розничная цена = оптовая × коэффициент (RETAIL_MARKUP), округлённая до RETAIL_ROUND ₸."""
    step = max(1, config.RETAIL_ROUND)
    return int(math.floor(wholesale * config.RETAIL_MARKUP / step + 0.5) * step)


def unit_price(product: Product, price_type: str) -> int:
    if price_type == "WHOLESALE":
        if not product.wholesale_price:
            raise BusinessError(f"У товара «{product.name}» не указана оптовая цена")
        return product.wholesale_price
    return product.selling_price


# ---------- продажа ----------
def create_sale(db: Session, items: list[dict], payment_method: str, price_type: str = "RETAIL",
                customer_id: Optional[int] = None, discount_percent: int = 0, discount_amount: int = 0) -> Sale:
    """Создаёт продажу одной транзакцией: sale + sale_items + списание + stock_movements.
    items: [{"product_id", "quantity", "gift": bool}] — gift=True: товар в подарок (0 ₸).
    price_type: RETAIL (розница) или WHOLESALE (опт — нужен оптовый клиент, цены по оптовой цене).
    Скидка на чек: discount_percent (%) ИЛИ discount_amount (₸); распределяется по позициям,
    чтобы выручка и прибыль по товарам считались честно.
    При любой ошибке — ROLLBACK, остатки не меняются."""
    if payment_method not in PAYMENT_METHODS:
        raise BusinessError(f"Неизвестный способ оплаты: {payment_method}")
    if price_type not in PRICE_TYPES:
        raise BusinessError(f"Неизвестный тип продажи: {price_type}")
    if not items:
        raise BusinessError("Корзина пуста")
    discount_percent, discount_amount = int(discount_percent or 0), int(discount_amount or 0)
    if discount_percent < 0 or discount_amount < 0:
        raise BusinessError("Скидка не может быть отрицательной")
    if discount_percent > config.MAX_DISCOUNT_PERCENT:
        raise BusinessError(f"Скидка больше {config.MAX_DISCOUNT_PERCENT}% — проверьте (лимит в settings.env)")
    customer = None
    if price_type == "WHOLESALE":
        if not customer_id:
            raise BusinessError("Для оптовой продажи выберите клиента")
        customer = db.get(Customer, customer_id)
        if customer is None or not customer.is_active:
            raise BusinessError("Оптовый клиент не найден")

    # объединяем одинаковые товары (подарок и обычная позиция — отдельно)
    merged: dict[tuple[int, bool], int] = {}
    for it in items:
        qty = int(it["quantity"])
        if qty <= 0:
            raise BusinessError("Количество должно быть больше 0")
        key = (int(it["product_id"]), bool(it.get("gift")))
        merged[key] = merged.get(key, 0) + qty

    try:
        sale = Sale(total_amount=0, payment_method=payment_method, price_type=price_type,
                    customer_id=customer.id if customer else None)
        db.add(sale)
        db.flush()  # получаем sale.id

        need: dict[int, int] = {}
        for (pid, _), qty in merged.items():
            need[pid] = need.get(pid, 0) + qty
        lines = []
        for (product_id, gift), qty in merged.items():
            # блокировка строки (в PostgreSQL) — защита при одновременных продажах
            product = db.execute(
                select(Product).where(Product.id == product_id).with_for_update()
            ).scalar_one_or_none()
            if product is None or not product.is_active:
                raise BusinessError(f"Товар #{product_id} не найден")
            if product.stock_quantity < need[product_id]:
                raise BusinessError(
                    f"Недостаточно товара «{product.name}»: в наличии {product.stock_quantity}, "
                    f"нужно {need[product_id]}")
            price = unit_price(product, price_type)
            item = SaleItem(sale_id=sale.id, product_id=product.id, quantity=qty, unit_price=price,
                            purchase_price=product.purchase_price, is_gift=gift,
                            discount=price * qty if gift else 0, subtotal=0 if gift else price * qty)
            db.add(item)
            lines.append(item)
            product.stock_quantity -= qty
            db.add(StockMovement(product_id=product.id, movement_type="SALE", quantity=-qty,
                                 reference_type="sale", reference_id=sale.id,
                                 note="Подарок" if gift else None))

        # скидка на чек — распределяем по платным позициям пропорционально сумме
        paid = [i for i in lines if not i.is_gift]
        base = sum(i.subtotal for i in paid)
        disc = round(base * discount_percent / 100) if discount_percent else discount_amount
        if disc > base:
            raise BusinessError("Скидка больше суммы чека")
        if disc and config.MAX_DISCOUNT_PERCENT and base and disc * 100 > base * config.MAX_DISCOUNT_PERCENT:
            raise BusinessError(f"Скидка больше {config.MAX_DISCOUNT_PERCENT}% от суммы — проверьте")
        left = disc
        for n, i in enumerate(paid):
            part = left if n == len(paid) - 1 else (disc * i.subtotal // base if base else 0)
            part = min(part, i.subtotal, left)
            i.discount, i.subtotal, left = part, i.subtotal - part, left - part
        disc -= left  # на случай округления — фактически применённая скидка
        total = sum(i.subtotal for i in lines)

        # условие первого закупа оптовика
        if customer and config.WHOLESALE_FIRST_MIN and total < config.WHOLESALE_FIRST_MIN:
            had_before = db.execute(select(func.count(Sale.id)).where(
                Sale.customer_id == customer.id, Sale.status == "COMPLETED", Sale.id != sale.id)).scalar()
            if not had_before:
                raise BusinessError(
                    f"Первый закуп клиента «{customer.name}» должен быть от {money(config.WHOLESALE_FIRST_MIN)}, "
                    f"сейчас {money(total)}")

        sale.total_amount = total
        sale.discount_amount = disc
        sale.discount_percent = discount_percent
        db.commit()
        db.refresh(sale)
        return sale
    except Exception:
        db.rollback()
        raise


# ---------- оптовые клиенты: выполнение условий ----------
def _month_key(d) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _next_month(y: int, m: int):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def customer_stats(db: Session, customer: Customer, today: Optional[date] = None) -> dict:
    """Статус оптовика по условиям: первый закуп ≥ WHOLESALE_FIRST_MIN,
    затем каждый календарный месяц закупы на сумму ≥ WHOLESALE_MONTHLY_MIN."""
    today = today or date.today()
    rows = db.execute(select(Sale.created_at, Sale.total_amount).where(
        Sale.customer_id == customer.id, Sale.status == "COMPLETED").order_by(Sale.created_at)).all()
    by_month: dict[str, int] = {}
    for dt, amount in rows:
        by_month[_month_key(dt)] = by_month.get(_month_key(dt), 0) + amount
    total = sum(a for _, a in rows)
    cur_key = _month_key(today)
    month_sum = by_month.get(cur_key, 0)
    monthly_min = config.WHOLESALE_MONTHLY_MIN
    last_day = calendar.monthrange(today.year, today.month)[1]
    days_left = last_day - today.day

    missed: list[str] = []
    if rows and monthly_min:
        first = rows[0][0]
        y, m = _next_month(first.year, first.month)       # месяц первого закупа засчитан
        while (y, m) < (today.year, today.month):         # проверяем только завершённые месяцы
            k = f"{y:04d}-{m:02d}"
            if by_month.get(k, 0) < monthly_min:
                missed.append(k)
            y, m = _next_month(y, m)

    first_month = _month_key(rows[0][0]) if rows else None
    prev_y, prev_m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    prev_key = f"{prev_y:04d}-{prev_m:02d}"

    if not rows:
        status, text = "NEW", f"Нет закупов. Первый закуп — от {money(config.WHOLESALE_FIRST_MIN)}"
    elif prev_key in missed:
        status, text = "VIOLATION", f"Не выполнил условие в прошлом месяце ({prev_key})"
    elif first_month == cur_key or not monthly_min or month_sum >= monthly_min:
        status, text = "OK", "Условие этого месяца выполнено"
    elif days_left <= 7:
        status, text = "AT_RISK", f"До конца месяца {days_left} дн., осталось докупить {money(monthly_min - month_sum)}"
    else:
        status, text = "IN_PROGRESS", f"Осталось докупить {money(monthly_min - month_sum)} в этом месяце"

    return {
        "id": customer.id, "name": customer.name, "phone": customer.phone, "bin_iin": customer.bin_iin,
        "city": customer.city, "note": customer.note,
        "status": status, "status_text": text,
        "purchases": len(rows), "total_spent": total,
        "first_purchase": rows[0][0].strftime("%d.%m.%Y") if rows else None,
        "last_purchase": rows[-1][0].strftime("%d.%m.%Y %H:%M") if rows else None,
        "days_since_last": (today - rows[-1][0].date()).days if rows else None,
        "month_sum": month_sum, "month_min": monthly_min,
        "month_left": max(0, monthly_min - month_sum) if rows and first_month != cur_key else 0,
        "days_left_in_month": days_left,
        "missed_months": missed,
    }


# ---------- возврат ----------
def return_sale(db: Session, sale_id: int) -> Sale:
    """Полный возврат продажи. Продажа не удаляется, меняется статус, остаток растёт."""
    try:
        sale = db.get(Sale, sale_id)
        if sale is None:
            raise BusinessError(f"Продажа #{sale_id} не найдена")
        if sale.status == "RETURNED":
            raise BusinessError(f"Продажа #{sale_id} уже возвращена")
        for item in sale.items:
            item.product.stock_quantity += item.quantity
            db.add(StockMovement(product_id=item.product_id, movement_type="RETURN",
                                 quantity=item.quantity, reference_type="sale", reference_id=sale.id))
        sale.status = "RETURNED"
        db.commit()
        return sale
    except Exception:
        db.rollback()
        raise


# ---------- поступление / корректировка ----------
def receive_stock(db: Session, product_id: int, quantity: int, purchase_price: int | None = None,
                  note: str | None = None) -> Product:
    if quantity <= 0:
        raise BusinessError("Количество поступления должно быть больше 0")
    try:
        product = db.get(Product, product_id)
        if product is None:
            raise BusinessError("Товар не найден")
        product.stock_quantity += quantity
        if purchase_price:
            product.purchase_price = purchase_price
        db.add(StockMovement(product_id=product.id, movement_type="PURCHASE", quantity=quantity, note=note))
        db.commit()
        return product
    except Exception:
        db.rollback()
        raise


def adjust_stock(db: Session, product_id: int, new_quantity: int, note: str | None = None) -> Product:
    if new_quantity < 0:
        raise BusinessError("Остаток не может быть отрицательным")
    try:
        product = db.get(Product, product_id)
        if product is None:
            raise BusinessError("Товар не найден")
        diff = new_quantity - product.stock_quantity
        if diff:
            product.stock_quantity = new_quantity
            db.add(StockMovement(product_id=product.id, movement_type="ADJUSTMENT", quantity=diff, note=note))
        db.commit()
        return product
    except Exception:
        db.rollback()
        raise


# ---------- изменение карточки товара ----------
def set_selling_price(db: Session, product_id: int, price: int) -> Product:
    """Меняет розничную цену."""
    if price < 0:
        raise BusinessError("Цена не может быть отрицательной")
    product = db.get(Product, product_id)
    if product is None:
        raise BusinessError("Товар не найден")
    product.selling_price = price
    db.commit()
    return product


def set_wholesale_price(db: Session, product_id: int, price: int, update_retail: bool = True) -> Product:
    """Меняет оптовую цену; по умолчанию розница пересчитывается (опт × коэффициент)."""
    if price < 0:
        raise BusinessError("Цена не может быть отрицательной")
    product = db.get(Product, product_id)
    if product is None:
        raise BusinessError("Товар не найден")
    product.wholesale_price = price
    if update_retail:
        product.selling_price = retail_from_wholesale(price)
    db.commit()
    return product


def recalc_all_retail(db: Session) -> int:
    """Пересчитать розницу всех товаров с оптовой ценой. Возвращает число изменённых товаров."""
    n = 0
    for p in db.execute(select(Product).where(Product.wholesale_price.is_not(None))).scalars():
        new = retail_from_wholesale(p.wholesale_price)
        if p.selling_price != new:
            p.selling_price, n = new, n + 1
    db.commit()
    return n


def deactivate_product(db: Session, product_id: int) -> Product:
    """Товар не удаляется из базы (история продаж сохраняется), а скрывается из продажи."""
    product = db.get(Product, product_id)
    if product is None:
        raise BusinessError("Товар не найден")
    product.is_active = False
    db.commit()
    return product


def create_product(db: Session, name: str, category_id: int, purchase_price: int, wholesale_price: int,
                   selling_price: Optional[int] = None, quantity: int = 0, minimum_stock: int = 0) -> Product:
    """selling_price (розница) не указана → считается как оптовая × RETAIL_MARKUP."""
    if selling_price is None:
        selling_price = retail_from_wholesale(wholesale_price)
    if min(purchase_price, wholesale_price, selling_price, quantity) < 0:
        raise BusinessError("Цены и количество не могут быть отрицательными")
    try:
        product = Product(name=name, category_id=category_id, purchase_price=purchase_price,
                          wholesale_price=wholesale_price, selling_price=selling_price,
                          stock_quantity=quantity, minimum_stock=minimum_stock)
        db.add(product)
        db.flush()
        if quantity:
            db.add(StockMovement(product_id=product.id, movement_type="PURCHASE", quantity=quantity,
                                 note="Начальный остаток"))
        db.commit()
        return product
    except Exception:
        db.rollback()
        raise

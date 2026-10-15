"""Отчёты (разделы 24–26 ТЗ). Используются и в Dashboard, и ИИ-помощником."""
from datetime import date, datetime, time, timedelta
from typing import Optional

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from . import services
from .models import Product, Sale, SaleItem


def period_bounds(date_from: Optional[date], date_to: Optional[date]):
    start = datetime.combine(date_from or date.today() - timedelta(days=30), time.min)
    end = datetime.combine(date_to or date.today(), time.max)
    return start, end


def sales_summary(db: Session, date_from: Optional[date] = None, date_to: Optional[date] = None,
                  top_limit: int = 5) -> dict:
    start, end = period_bounds(date_from, date_to)
    done = (Sale.status == "COMPLETED", Sale.created_at.between(start, end))
    revenue, cost, units = db.execute(
        select(func.coalesce(func.sum(SaleItem.subtotal), 0),
               func.coalesce(func.sum(SaleItem.purchase_price * SaleItem.quantity), 0),
               func.coalesce(func.sum(SaleItem.quantity), 0)).join(Sale).where(*done)
    ).one()
    sales_count = db.execute(select(func.count(Sale.id)).where(*done)).scalar()
    top = db.execute(
        select(Product.name, func.sum(SaleItem.quantity), func.sum(SaleItem.subtotal),
               func.sum(SaleItem.subtotal - SaleItem.purchase_price * SaleItem.quantity))
        .join(SaleItem, SaleItem.product_id == Product.id).join(Sale).where(*done)
        .group_by(Product.id).order_by(func.sum(SaleItem.subtotal).desc()).limit(top_limit)
    ).all()
    gift = SaleItem.is_gift.is_(True)
    discounts, gift_qty, gift_cost = db.execute(
        select(func.coalesce(func.sum(case((gift, 0), else_=SaleItem.discount)), 0),
               func.coalesce(func.sum(case((gift, SaleItem.quantity), else_=0)), 0),
               func.coalesce(func.sum(case((gift, SaleItem.quantity * SaleItem.purchase_price), else_=0)), 0))
        .join(Sale).where(*done)).one()
    by_type = dict(db.execute(select(Sale.price_type, func.sum(Sale.total_amount)).where(*done)
                              .group_by(Sale.price_type)).all())
    by_day = db.execute(
        select(func.date(Sale.created_at), func.sum(Sale.total_amount)).where(*done)
        .group_by(func.date(Sale.created_at)).order_by(func.date(Sale.created_at))
    ).all()
    return {
        "period": [start.date().isoformat(), end.date().isoformat()],
        "revenue": revenue, "cost": cost,
        "profit": services.calculate_profit(revenue, cost),
        "margin": services.calculate_margin(revenue, cost),
        "sales_count": sales_count, "units_sold": units,
        "average_check": round(revenue / sales_count) if sales_count else 0,
        "inventory_value": inventory_value(db),
        "top_products": [{"name": n, "quantity": q, "revenue": r, "profit": p} for n, q, r, p in top],
        "discounts": discounts, "gifts_qty": gift_qty, "gifts_cost": gift_cost,
        "revenue_retail": by_type.get("RETAIL", 0) or 0,
        "revenue_wholesale": by_type.get("WHOLESALE", 0) or 0,
        "revenue_by_day": [{"date": str(d), "revenue": r} for d, r in by_day],
        "low_stock": low_stock(db),
    }


def low_stock(db: Session) -> list[dict]:
    rows = db.execute(select(Product).where(Product.is_active.is_(True),
                                            Product.stock_quantity <= Product.minimum_stock)
                      .order_by(Product.stock_quantity)).scalars()
    return [{"id": p.id, "name": p.name, "stock": p.stock_quantity, "minimum": p.minimum_stock} for p in rows]


def inventory_value(db: Session) -> int:
    return db.execute(select(func.coalesce(func.sum(Product.stock_quantity * Product.purchase_price), 0))
                      .where(Product.is_active.is_(True))).scalar()


def product_sales(db: Session, date_from: Optional[date] = None, date_to: Optional[date] = None,
                  order: str = "asc", limit: int = 20) -> list[dict]:
    """Продажи КАЖДОГО активного товара за период, включая товары без продаж.
    order="asc" — сначала худшие (что покупали меньше всего), "desc" — лучшие."""
    start, end = period_bounds(date_from, date_to)
    sold = {pid: (q, r, pr) for pid, q, r, pr in db.execute(
        select(SaleItem.product_id, func.sum(SaleItem.quantity), func.sum(SaleItem.subtotal),
               func.sum(SaleItem.subtotal - SaleItem.purchase_price * SaleItem.quantity))
        .join(Sale).where(Sale.status == "COMPLETED", Sale.created_at.between(start, end))
        .group_by(SaleItem.product_id)).all()}
    rows = []
    for p in db.execute(select(Product).where(Product.is_active.is_(True))).scalars():
        q, r, pr = sold.get(p.id, (0, 0, 0))
        rows.append({"name": p.name, "category": p.category.name, "sold_qty": q, "revenue": r, "profit": pr,
                     "stock": p.stock_quantity, "frozen_money": p.stock_quantity * p.purchase_price})
    rows.sort(key=lambda x: (x["sold_qty"], x["revenue"]), reverse=(order == "desc"))
    return rows[:max(1, min(int(limit), 200))]

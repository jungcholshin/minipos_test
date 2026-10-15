"""Модели БД (раздел 19–23 ТЗ). Деньги хранятся в целых тенге."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    products: Mapped[list["Product"]] = relationship(back_populates="category")


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    barcode: Mapped[str | None] = mapped_column(String(64), unique=True)
    sku: Mapped[str | None] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    purchase_price: Mapped[int] = mapped_column(Integer)
    selling_price: Mapped[int] = mapped_column(Integer)
    wholesale_price: Mapped[int | None] = mapped_column(Integer)
    stock_quantity: Mapped[int] = mapped_column(Integer, default=0)
    minimum_stock: Mapped[int] = mapped_column(Integer, default=0)
    unit: Mapped[str] = mapped_column(String(20), default="шт.")
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
    category: Mapped[Category] = relationship(back_populates="products")


class Customer(Base):
    """Оптовый клиент."""
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(40))
    bin_iin: Mapped[str | None] = mapped_column(String(20))   # БИН / ИИН
    city: Mapped[str | None] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    sales: Mapped[list["Sale"]] = relationship(back_populates="customer")


class Sale(Base):
    __tablename__ = "sales"
    id: Mapped[int] = mapped_column(primary_key=True)
    price_type: Mapped[str] = mapped_column(String(20), default="RETAIL")  # RETAIL / WHOLESALE
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    total_amount: Mapped[int] = mapped_column(Integer)                # к оплате (после скидки)
    discount_amount: Mapped[int] = mapped_column(Integer, default=0)  # скидка на чек, ₸ (без подарков)
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    payment_method: Mapped[str] = mapped_column(String(20))  # CASH / CARD / TRANSFER / OTHER
    status: Mapped[str] = mapped_column(String(20), default="COMPLETED")  # COMPLETED / RETURNED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    items: Mapped[list["SaleItem"]] = relationship(back_populates="sale", cascade="all, delete-orphan")
    customer: Mapped[Customer | None] = relationship(back_populates="sales")


class SaleItem(Base):
    __tablename__ = "sale_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[int] = mapped_column(Integer)       # цена продажи на момент продажи
    purchase_price: Mapped[int] = mapped_column(Integer)   # себестоимость на момент продажи
    discount: Mapped[int] = mapped_column(Integer, default=0)       # скидка на позицию, ₸
    is_gift: Mapped[bool] = mapped_column(Boolean, default=False)   # подарок (цена 0, себестоимость учитывается)
    subtotal: Mapped[int] = mapped_column(Integer)                  # unit_price × qty − discount
    sale: Mapped[Sale] = relationship(back_populates="items")
    product: Mapped[Product] = relationship()


class StockMovement(Base):
    __tablename__ = "stock_movements"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    movement_type: Mapped[str] = mapped_column(String(20))  # PURCHASE / SALE / RETURN / ADJUSTMENT
    quantity: Mapped[int] = mapped_column(Integer)          # +приход / -расход
    reference_type: Mapped[str | None] = mapped_column(String(20))
    reference_id: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    product: Mapped[Product] = relationship()


class PendingAction(Base):
    """Действие, которое предложил ИИ. Выполняется только после подтверждения человеком."""
    __tablename__ = "ai_pending_actions"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    params: Mapped[str] = mapped_column(Text)      # JSON
    summary: Mapped[str] = mapped_column(Text)     # понятное человеку описание
    status: Mapped[str] = mapped_column(String(20), default="PENDING")  # PENDING / DONE / CANCELLED / FAILED
    source: Mapped[str] = mapped_column(String(20), default="web")      # web / telegram
    result: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

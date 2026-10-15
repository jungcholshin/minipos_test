"""Подключение к базе данных.

По умолчанию — SQLite (файл minipos.db рядом с программой), без установки сервера БД.
Для PostgreSQL укажите в settings.env:
    DATABASE_URL=postgresql+psycopg2://user:password@localhost:5432/minipos
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Новые колонки, которых не было в старых версиях базы: (таблица, колонка, SQL-тип с умолчанием)
_NEW_COLUMNS = [
    ("sales", "price_type", "VARCHAR(20) NOT NULL DEFAULT 'RETAIL'"),
    ("sales", "customer_id", "INTEGER REFERENCES customers(id)"),
    ("sales", "discount_amount", "INTEGER NOT NULL DEFAULT 0"),
    ("sales", "discount_percent", "INTEGER NOT NULL DEFAULT 0"),
    ("sale_items", "discount", "INTEGER NOT NULL DEFAULT 0"),
    ("sale_items", "is_gift", "BOOLEAN NOT NULL DEFAULT 0"),
]


def migrate() -> bool:
    """Создаёт новые таблицы и добавляет новые колонки в базу от старой версии. Данные не трогает.
    Возвращает True, если база была от старой версии (колонки добавлены)."""
    from sqlalchemy import inspect, text

    from . import models  # noqa: F401 — регистрирует таблицы

    Base.metadata.create_all(engine)
    insp = inspect(engine)
    added = False
    with engine.begin() as conn:
        for table, column, ddl in _NEW_COLUMNS:
            if column not in {c["name"] for c in insp.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                added = True
    return added

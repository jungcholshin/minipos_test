"""Загрузка каталога товаров из Excel (.xlsx) или CSV.

Колонки распознаются по названию (регистр не важен), порядок любой:
  Название*, Категория, Штрихкод, Артикул, Закупка, Опт, Розница, Остаток, Минимум, Единица
Нужна хотя бы одна цена: Опт или Розница (вторая посчитается через RETAIL_MARKUP).

Товар ищется по штрихкоду, затем по точному названию:
  найден   → обновляются заполненные в файле поля;
  не найден → создаётся новый.
Пустая ячейка = «не менять». Сначала показывается предпросмотр, потом — загрузка.
"""
import csv
import io
import re

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import config, services
from .models import Category, Product, StockMovement

MAX_ROWS = 5000

# поле → варианты заголовков
ALIASES = {
    "name": ["название", "наименование", "товар", "наименование товара", "name", "product"],
    "category": ["категория", "группа", "раздел", "category"],
    "barcode": ["штрихкод", "штрих-код", "штрих код", "barcode", "ean"],
    "sku": ["артикул", "код", "sku"],
    "purchase_price": ["закупка", "закупочная цена", "закуп", "себестоимость", "цена закупки"],
    "wholesale_price": ["опт", "оптовая цена", "цена опт", "оптовая"],
    "selling_price": ["розница", "розничная цена", "цена розница", "цена", "розничная"],
    "stock": ["остаток", "количество", "кол-во", "кол во", "склад"],
    "minimum_stock": ["минимум", "мин. остаток", "минимальный остаток", "мин остаток"],
    "unit": ["единица", "ед.", "ед", "ед. изм.", "единица измерения"],
}
HEADERS = ["Название", "Категория", "Штрихкод", "Артикул", "Закупка", "Опт", "Розница", "Остаток", "Минимум"]
NUMERIC = ("purchase_price", "wholesale_price", "selling_price", "stock", "minimum_stock")


def _norm(s) -> str:
    return re.sub(r"[\s,]+", " ", str(s or "").strip().lower().replace("₸", "").replace("*", "")).strip(" .:")


def _field_for(header) -> str | None:
    h = _norm(header)
    h = re.sub(r"\s*\(.*\)$", "", h)  # «Закупка (₸)» → «закупка»
    for field, names in ALIASES.items():
        if h in names:
            return field
    return None


def _to_int(v):
    """«7 000», «7000,00», «7 000 ₸», 7000.0 → 7000. Пусто → None."""
    if v is None or str(v).strip() == "":
        return None
    if isinstance(v, (int, float)):
        return int(round(v))
    s = re.sub(r"[^\d,.\-]", "", str(v)).replace(",", ".")
    if s.count(".") > 1:          # «1.234.567»
        s = s.replace(".", "")
    try:
        return int(round(float(s)))
    except ValueError:
        raise ValueError(f"не число: «{v}»")


def _text(v):
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)                # штрихкод из Excel часто приходит как 8.8e12
    s = str(v).strip()
    return s or None


def read_tables(content: bytes, filename: str) -> list[list[list]]:
    """Все листы файла (для CSV — один)."""
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        try:
            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception:
            raise services.BusinessError("Не удалось открыть файл Excel — проверьте, что это .xlsx")
        return [[list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets]
    if name.endswith((".csv", ".txt")):
        text = content.decode("utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(text[:2000], delimiters=";,\t")
        except csv.Error:
            dialect = csv.excel
        return [[row for row in csv.reader(io.StringIO(text), dialect)]]
    raise services.BusinessError("Поддерживаются файлы .xlsx и .csv (в Excel: «Сохранить как» → Книга Excel)")


def parse(content: bytes, filename: str) -> list[dict]:
    """Возвращает строки файла как словари полей (+ номер строки в файле)."""
    # лист и строка заголовков, где распознано больше всего колонок (и есть «Название»)
    header_idx, mapping, table = None, {}, []
    for t in read_tables(content, filename):
        for i, row in enumerate(t[:15]):
            m = {j: _field_for(c) for j, c in enumerate(row) if _field_for(c)}
            if "name" in m.values() and len(set(m.values())) > len(set(mapping.values())):
                header_idx, mapping, table = i, m, t
    if header_idx is None:
        raise services.BusinessError("Не найдена колонка «Название». Используйте шаблон (кнопка «Скачать шаблон»).")
    rows = []
    for i, row in enumerate(table[header_idx + 1:], start=header_idx + 2):
        rec = {"_row": i}
        for j, field in mapping.items():
            rec[field] = row[j] if j < len(row) else None
        if not any(_text(rec.get(f)) for f in mapping.values()):
            continue                                # пустая строка
        rows.append(rec)
    if len(rows) > MAX_ROWS:
        raise services.BusinessError(f"Слишком много строк ({len(rows)}). Максимум {MAX_ROWS} за раз.")
    return rows


def plan(db: Session, rows: list[dict]) -> list[dict]:
    """Проверяет строки и определяет, что будет сделано. Базу не меняет."""
    result, seen_barcodes, seen_names = [], {}, {}
    by_barcode = {p.barcode: p for p in db.execute(select(Product).where(Product.barcode.is_not(None))).scalars()}
    by_name = {p.name.strip().lower(): p for p in db.execute(select(Product)).scalars()}
    for r in rows:
        item = {"row": r["_row"], "action": "error", "errors": [], "warnings": []}
        try:
            vals = {f: _to_int(r.get(f)) for f in NUMERIC if f in r}
        except ValueError as e:
            item["errors"].append(str(e))
            vals = {}
        name = _text(r.get("name"))
        barcode, sku = _text(r.get("barcode")), _text(r.get("sku"))
        category = _text(r.get("category"))
        item.update(name=name, barcode=barcode, sku=sku, category=category, unit=_text(r.get("unit")), **vals)
        if not name:
            item["errors"].append("нет названия")
        if any(v is not None and v < 0 for v in vals.values()):
            item["errors"].append("отрицательное число")
        if barcode:
            if barcode in seen_barcodes:
                item["errors"].append(f"штрихкод повторяется (строка {seen_barcodes[barcode]})")
            seen_barcodes[barcode] = r["_row"]
        if name:
            key = name.lower()
            if key in seen_names and not barcode:
                item["errors"].append(f"название повторяется (строка {seen_names[key]})")
            seen_names[key] = r["_row"]

        existing = by_barcode.get(barcode) if barcode else None
        existing = existing or (by_name.get(name.lower()) if name else None)
        item["product_id"] = existing.id if existing else None

        w, rp = vals.get("wholesale_price"), vals.get("selling_price")
        if existing is None:
            if w is None and rp is None:
                item["errors"].append("нужна цена: Опт или Розница")
            elif w is None:
                item["wholesale_price"] = int(round(rp / config.RETAIL_MARKUP / 10) * 10)
                item["warnings"].append(f"опт посчитан: {item['wholesale_price']} ₸")
            elif rp is None:
                item["selling_price"] = services.retail_from_wholesale(w)
            if vals.get("purchase_price") is None:
                item["warnings"].append("нет закупочной цены — прибыль будет неточной")
            if not category:
                item["category"] = "Без категории"
        elif w is not None and rp is None and "selling_price" in r:
            item["selling_price"] = services.retail_from_wholesale(w)   # колонка есть, но пустая

        if not item["errors"]:
            item["action"] = "update" if existing else "create"
            if existing:
                ch = []
                for f, label in (("purchase_price", "закупка"), ("wholesale_price", "опт"),
                                 ("selling_price", "розница"), ("stock", "остаток"), ("minimum_stock", "минимум")):
                    new = item.get(f)
                    old = existing.stock_quantity if f == "stock" else getattr(existing, f)
                    if new is not None and new != old:
                        ch.append(f"{label} {old} → {new}")
                if category and category != existing.category.name:
                    ch.append(f"категория → {category}")
                if name and name != existing.name:
                    ch.append(f"название → {name}")
                item["changes"] = ch
                if not ch:
                    item["action"] = "same"
        result.append(item)
    return result


def summary(items: list[dict]) -> dict:
    count = lambda a: sum(1 for i in items if i["action"] == a)  # noqa: E731
    return {"create": count("create"), "update": count("update"), "same": count("same"), "error": count("error")}


def apply(db: Session, items: list[dict]) -> dict:
    """Применяет план одной транзакцией (строки с ошибками пропускаются)."""
    cats = {c.name.lower(): c for c in db.execute(select(Category)).scalars()}

    def category_id(name):
        if not name:
            return None
        c = cats.get(name.lower())
        if c is None:
            c = Category(name=name)
            db.add(c)
            db.flush()
            cats[name.lower()] = c
        return c.id

    try:
        for it in items:
            if it["action"] == "create":
                p = Product(name=it["name"], category_id=category_id(it["category"]), barcode=it["barcode"],
                            sku=it["sku"], purchase_price=it.get("purchase_price") or 0,
                            wholesale_price=it["wholesale_price"], selling_price=it["selling_price"],
                            stock_quantity=it.get("stock") or 0, minimum_stock=it.get("minimum_stock") or 0,
                            unit=it.get("unit") or "шт.")
                db.add(p)
                db.flush()
                if p.stock_quantity:
                    db.add(StockMovement(product_id=p.id, movement_type="PURCHASE", quantity=p.stock_quantity,
                                         note="Импорт из Excel"))
            elif it["action"] == "update":
                p = db.get(Product, it["product_id"])
                p.name = it["name"] or p.name
                for f in ("purchase_price", "wholesale_price", "selling_price", "minimum_stock"):
                    if it.get(f) is not None:
                        setattr(p, f, it[f])
                for f in ("barcode", "sku", "unit"):
                    if it.get(f):
                        setattr(p, f, it[f])
                if it.get("category"):
                    p.category_id = category_id(it["category"])
                if it.get("stock") is not None and it["stock"] != p.stock_quantity:
                    db.add(StockMovement(product_id=p.id, movement_type="ADJUSTMENT",
                                         quantity=it["stock"] - p.stock_quantity, note="Импорт из Excel"))
                    p.stock_quantity = it["stock"]
                p.is_active = True
        db.commit()
    except Exception:
        db.rollback()
        raise
    return summary(items)


def template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Товары"
    ws.append(HEADERS)
    ws.append(["Beauty of Joseon Relief Sun SPF50+", "SPF", "8809738316139", "", 3600, 4330, 6500, 20, 5])
    ws.append(["KERASYS Hair Clinic Damage Clinic", "Для волос", "", "", 2000, 2330, "", 15, 3])
    head = PatternFill("solid", fgColor="2F6F4F")
    for c in ws[1]:
        c.font, c.fill, c.alignment = Font(bold=True, color="FFFFFF"), head, Alignment(horizontal="center")
    for col, w in zip("ABCDEFGHI", (45, 22, 17, 12, 11, 11, 11, 10, 10)):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=2, max_row=500, min_col=3, max_col=3):
        for c in row:
            c.number_format = "@"   # штрихкод — как текст, чтобы Excel не превратил его в 8,81E+12
    ws.freeze_panes = "A2"

    info = wb.create_sheet("Инструкция")
    for line in [
        "Как заполнить",
        "",
        "1. Одна строка = один товар. Удалите две строки-примера.",
        "2. Обязательно: Название и хотя бы одна цена — Опт или Розница.",
        f"3. Если указан только Опт — Розница посчитается: опт × {config.RETAIL_MARKUP}, "
        f"с округлением до {config.RETAIL_ROUND} ₸.",
        "4. Категории создаются сами по названию.",
        "5. Остаток — сколько штук сейчас на складе. Минимум — при каком остатке товар отметится LOW STOCK.",
        "6. Если товар уже есть в программе (тот же штрихкод или то же название) — он обновится,",
        "   пустые ячейки при этом ничего не меняют.",
        "7. Цены — целые тенге, без ₸ (можно и с пробелами: 7 000).",
        "",
        "Колонки можно расставить в любом порядке; лишние колонки игнорируются.",
        "Подойдёт и файл из «Экспорт в Excel» (лист Products) — можно выгрузить, поправить и загрузить обратно.",
    ]:
        info.append([line])
    info["A1"].font = Font(bold=True, size=14)
    info.column_dimensions["A"].width = 110
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def import_products(db: Session, content: bytes, filename: str, do_apply: bool) -> dict:
    items = plan(db, parse(content, filename))
    if do_apply:
        return {"applied": True, "summary": apply(db, items)}
    keep = ("row", "action", "name", "category", "barcode", "purchase_price", "wholesale_price",
            "selling_price", "stock", "errors", "warnings", "changes")
    return {"applied": False, "summary": summary(items),
            "items": [{k: i.get(k) for k in keep} for i in items]}


def product_count(db: Session) -> int:
    return db.execute(select(func.count(Product.id))).scalar()

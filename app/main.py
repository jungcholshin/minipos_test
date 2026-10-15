"""MiniPOS — тестовая версия. REST API на FastAPI + веб-интерфейс + ИИ-помощник."""
import io
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import VERSION, ai, backup, config, importer, reports, services, system, telegram_bot
from .db import SessionLocal, get_db, migrate
from .models import Category, Customer, Product, Sale, SaleItem, StockMovement
from .seed import fill_wholesale_prices, seed

STATIC = config.STATIC_DIR


@asynccontextmanager
async def lifespan(app: FastAPI):
    # при запуске: создать таблицы, заполнить тестовыми товарами, запустить фоновые задачи
    old_db = migrate()
    with SessionLocal() as db:
        seed(db)
        fill_wholesale_prices(db, all_products=old_db)
    backup.start()
    telegram_bot.start()
    yield
    telegram_bot.stop()


app = FastAPI(title="MiniPOS", version=VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


# ---------------- схемы ----------------
class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    category_id: int
    purchase_price: int = Field(ge=0)
    wholesale_price: int = Field(ge=0)
    selling_price: int | None = Field(default=None, ge=0)   # пусто = опт × коэффициент
    unit: str = "шт."
    barcode: str | None = None
    sku: str | None = None
    minimum_stock: int = Field(default=0, ge=0)
    description: str | None = None


class CartItem(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)
    gift: bool = False                  # товар в подарок


class SaleIn(BaseModel):
    items: list[CartItem]
    payment_method: str = "CASH"
    price_type: str = "RETAIL"          # RETAIL / WHOLESALE
    customer_id: int | None = None      # обязателен для WHOLESALE
    discount_percent: int = Field(default=0, ge=0, le=100)
    discount_amount: int = Field(default=0, ge=0)


class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone: str | None = None
    bin_iin: str | None = Field(default=None, max_length=20)
    city: str | None = None
    note: str | None = None


class ReceiveIn(BaseModel):
    quantity: int = Field(gt=0)
    purchase_price: int | None = Field(default=None, ge=0)
    note: str | None = None


class AdjustIn(BaseModel):
    new_quantity: int = Field(ge=0)
    note: str | None = None


def product_dict(p: Product) -> dict:
    return {
        "id": p.id, "name": p.name, "barcode": p.barcode, "sku": p.sku,
        "category_id": p.category_id, "category": p.category.name if p.category else None,
        "purchase_price": p.purchase_price, "selling_price": p.selling_price,
        "wholesale_price": p.wholesale_price, "stock_quantity": p.stock_quantity,
        "minimum_stock": p.minimum_stock, "unit": p.unit,
        "low_stock": p.stock_quantity <= p.minimum_stock,
        "stock_value": p.stock_quantity * p.purchase_price,
    }


def sale_dict(s: Sale) -> dict:
    return {
        "id": s.id, "total_amount": s.total_amount, "payment_method": s.payment_method,
        "price_type": s.price_type or "RETAIL",
        "customer_id": s.customer_id, "customer": s.customer.name if s.customer else None,
        "discount_amount": s.discount_amount or 0, "discount_percent": s.discount_percent or 0,
        "gross_amount": sum(i.unit_price * i.quantity for i in s.items if not i.is_gift),
        "status": s.status, "created_at": s.created_at.strftime("%d.%m.%Y %H:%M"),
        "items": [{"product_id": i.product_id, "name": i.product.name, "quantity": i.quantity,
                   "unit_price": i.unit_price, "discount": i.discount or 0, "gift": bool(i.is_gift),
                   "subtotal": i.subtotal} for i in s.items],
    }


def biz(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except services.BusinessError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------- категории ----------------
@app.get("/categories")
def list_categories(db: Session = Depends(get_db)):
    rows = db.execute(
        select(Category, func.count(Product.id)).outerjoin(Product).group_by(Category.id).order_by(Category.name)
    ).all()
    return [{"id": c.id, "name": c.name, "products": n} for c, n in rows]


@app.post("/categories", status_code=201)
def create_category(data: CategoryIn, db: Session = Depends(get_db)):
    if db.execute(select(Category).where(Category.name == data.name)).scalar():
        raise HTTPException(400, "Такая категория уже есть")
    c = Category(name=data.name)
    db.add(c)
    db.commit()
    return {"id": c.id, "name": c.name}


# ---------------- товары ----------------
@app.get("/products")
def list_products(q: str | None = None, category_id: int | None = None, db: Session = Depends(get_db)):
    """Поиск по названию, штрихкоду или артикулу (раздел 6 ТЗ)."""
    stmt = select(Product).where(Product.is_active.is_(True))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Product.name.ilike(like), Product.barcode == q.strip(), Product.sku.ilike(like)))
    if category_id:
        stmt = stmt.where(Product.category_id == category_id)
    return [product_dict(p) for p in db.execute(stmt.order_by(Product.name)).scalars()]


@app.get("/products/barcode/{barcode}")
def product_by_barcode(barcode: str, db: Session = Depends(get_db)):
    p = db.execute(select(Product).where(Product.barcode == barcode)).scalar()
    if not p:
        raise HTTPException(404, "Товар с таким штрихкодом не найден")
    return product_dict(p)


@app.post("/products", status_code=201)
def create_product(data: ProductIn, db: Session = Depends(get_db)):
    if not db.get(Category, data.category_id):
        raise HTTPException(400, "Категория не найдена")
    for field in ("barcode", "sku"):
        val = getattr(data, field)
        if val and db.execute(select(Product).where(getattr(Product, field) == val)).scalar():
            raise HTTPException(400, f"Товар с таким {field} уже существует")
    fields = data.model_dump()
    if fields["selling_price"] is None:
        fields["selling_price"] = services.retail_from_wholesale(data.wholesale_price)
    p = Product(**fields, stock_quantity=0)
    db.add(p)
    db.commit()
    db.refresh(p)
    return product_dict(p)


@app.put("/products/{product_id}")
def update_product(product_id: int, data: ProductIn, db: Session = Depends(get_db)):
    p = db.get(Product, product_id)
    if not p:
        raise HTTPException(404, "Товар не найден")
    fields = data.model_dump()
    if fields["selling_price"] is None:
        fields["selling_price"] = services.retail_from_wholesale(data.wholesale_price)
    for k, v in fields.items():
        setattr(p, k, v)
    db.commit()
    return product_dict(p)


@app.get("/pricing")
def pricing():
    """Настройки цен и условий для оптовиков (для интерфейса)."""
    return {"retail_markup": config.RETAIL_MARKUP, "retail_round": config.RETAIL_ROUND,
            "wholesale_first_min": config.WHOLESALE_FIRST_MIN, "wholesale_monthly_min": config.WHOLESALE_MONTHLY_MIN,
            "max_discount_percent": config.MAX_DISCOUNT_PERCENT}


@app.post("/products/recalc-retail")
def recalc_retail(db: Session = Depends(get_db)):
    """Пересчитать розничные цены всех товаров: опт × коэффициент."""
    return {"changed": services.recalc_all_retail(db)}


# ---------------- настройки программы ----------------
class AutostartIn(BaseModel):
    enabled: bool


@app.get("/system/info")
def system_info():
    return system.info()


@app.post("/system/autostart")
def system_autostart(data: AutostartIn):
    try:
        return {"autostart": system.set_autostart(data.enabled)}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/system/desktop-shortcut")
def system_desktop_shortcut():
    try:
        return {"result": system.create_desktop_shortcut()}
    except Exception as e:
        raise HTTPException(400, str(e))


# ---------------- импорт каталога из Excel ----------------
class ImportIn(BaseModel):
    filename: str
    content_base64: str
    apply: bool = False     # False — только предпросмотр


@app.get("/import/template")
def import_template():
    return StreamingResponse(io.BytesIO(importer.template()),
                             media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": "attachment; filename=minipos_catalog_template.xlsx"})


@app.post("/import/products")
def import_products(data: ImportIn, db: Session = Depends(get_db)):
    import base64
    try:
        content = base64.b64decode(data.content_base64)
    except Exception:
        raise HTTPException(400, "Файл повреждён")
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "Файл больше 10 МБ")
    return biz(importer.import_products, db, content, data.filename, data.apply)


# ---------------- склад ----------------
@app.post("/products/{product_id}/receive")
def receive(product_id: int, data: ReceiveIn, db: Session = Depends(get_db)):
    return product_dict(biz(services.receive_stock, db, product_id, data.quantity, data.purchase_price, data.note))


@app.post("/products/{product_id}/adjust")
def adjust(product_id: int, data: AdjustIn, db: Session = Depends(get_db)):
    return product_dict(biz(services.adjust_stock, db, product_id, data.new_quantity, data.note))


@app.get("/stock/movements")
def movements(product_id: int | None = None, limit: int = 100, db: Session = Depends(get_db)):
    stmt = select(StockMovement).order_by(StockMovement.id.desc()).limit(limit)
    if product_id:
        stmt = stmt.where(StockMovement.product_id == product_id)
    return [{"id": m.id, "product": m.product.name, "type": m.movement_type, "quantity": m.quantity,
             "reference": f"{m.reference_type} #{m.reference_id}" if m.reference_id else (m.note or ""),
             "created_at": m.created_at.strftime("%d.%m.%Y %H:%M")} for m in db.execute(stmt).scalars()]


# ---------------- продажи ----------------
@app.post("/sales", status_code=201)
def create_sale(data: SaleIn, db: Session = Depends(get_db)):
    sale = biz(services.create_sale, db, [i.model_dump() for i in data.items], data.payment_method,
               data.price_type, data.customer_id, data.discount_percent, data.discount_amount)
    return sale_dict(sale)


@app.get("/sales")
def list_sales(limit: int = 200, price_type: str | None = None, customer_id: int | None = None,
               db: Session = Depends(get_db)):
    stmt = select(Sale).order_by(Sale.id.desc()).limit(limit)
    if price_type:
        stmt = stmt.where(Sale.price_type == price_type)
    if customer_id:
        stmt = stmt.where(Sale.customer_id == customer_id)
    return [sale_dict(s) for s in db.execute(stmt).scalars()]


@app.get("/sales/{sale_id}")
def get_sale(sale_id: int, db: Session = Depends(get_db)):
    s = db.get(Sale, sale_id)
    if not s:
        raise HTTPException(404, "Продажа не найдена")
    return sale_dict(s)


@app.post("/sales/{sale_id}/return")
def return_sale(sale_id: int, db: Session = Depends(get_db)):
    return sale_dict(biz(services.return_sale, db, sale_id))


# ---------------- оптовые клиенты ----------------
@app.get("/customers")
def list_customers(db: Session = Depends(get_db)):
    """Оптовые клиенты со статусом выполнения условий (первый закуп и ежемесячный минимум)."""
    order = {"VIOLATION": 0, "AT_RISK": 1, "IN_PROGRESS": 2, "NEW": 3, "OK": 4}
    rows = [services.customer_stats(db, c) for c in
            db.execute(select(Customer).where(Customer.is_active.is_(True)).order_by(Customer.name)).scalars()]
    return sorted(rows, key=lambda r: order.get(r["status"], 9))


@app.post("/customers", status_code=201)
def create_customer(data: CustomerIn, db: Session = Depends(get_db)):
    c = Customer(**data.model_dump())
    db.add(c)
    db.commit()
    return services.customer_stats(db, c)


@app.put("/customers/{customer_id}")
def update_customer(customer_id: int, data: CustomerIn, db: Session = Depends(get_db)):
    c = db.get(Customer, customer_id)
    if not c:
        raise HTTPException(404, "Клиент не найден")
    for k, v in data.model_dump().items():
        setattr(c, k, v)
    db.commit()
    return services.customer_stats(db, c)


@app.delete("/customers/{customer_id}")
def hide_customer(customer_id: int, db: Session = Depends(get_db)):
    """Клиент не удаляется (история продаж сохраняется), а скрывается из списка."""
    c = db.get(Customer, customer_id)
    if not c:
        raise HTTPException(404, "Клиент не найден")
    c.is_active = False
    db.commit()
    return {"ok": True}


# ---------------- отчёты / dashboard ----------------
@app.get("/reports/summary")
def summary(date_from: date | None = Query(None), date_to: date | None = Query(None),
            db: Session = Depends(get_db)):
    return reports.sales_summary(db, date_from, date_to)


# ---------------- ИИ-помощник ----------------
class ChatIn(BaseModel):
    messages: list[dict]


@app.get("/ai/status")
def ai_status():
    return ai.status()


@app.post("/ai/chat")
def ai_chat(data: ChatIn, db: Session = Depends(get_db)):
    try:
        return ai.chat(db, data.messages, source="web")
    except ai.AIError as e:
        raise HTTPException(503, str(e))


@app.post("/ai/actions/{action_id}/confirm")
def ai_confirm(action_id: int, db: Session = Depends(get_db)):
    return {"result": biz(ai.confirm_action, db, action_id)}


@app.post("/ai/actions/{action_id}/cancel")
def ai_cancel(action_id: int, db: Session = Depends(get_db)):
    return {"result": biz(ai.cancel_action, db, action_id)}


@app.get("/reports/export")
def export_excel(db: Session = Depends(get_db)):
    """Экспорт в Excel: листы Sales, Sale Items, Products (раздел 28 ТЗ)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["ID", "Дата", "Тип", "Клиент", "Скидка, ₸", "Скидка, %", "Сумма", "Оплата", "Статус"])
    for s in db.execute(select(Sale).order_by(Sale.id)).scalars():
        ws.append([s.id, s.created_at.strftime("%d.%m.%Y %H:%M"),
                   "Опт" if s.price_type == "WHOLESALE" else "Розница",
                   s.customer.name if s.customer else "", s.discount_amount or 0, s.discount_percent or 0,
                   s.total_amount, s.payment_method, s.status])

    ws2 = wb.create_sheet("Sale Items")
    ws2.append(["Продажа", "Товар", "Кол-во", "Цена", "Скидка", "Подарок", "Закупка", "Сумма", "Прибыль"])
    for i in db.execute(select(SaleItem).order_by(SaleItem.id)).scalars():
        ws2.append([i.sale_id, i.product.name, i.quantity, i.unit_price, i.discount or 0,
                    "да" if i.is_gift else "", i.purchase_price, i.subtotal,
                    i.subtotal - i.purchase_price * i.quantity])

    ws3 = wb.create_sheet("Products")
    ws3.append(["ID", "Штрихкод", "Название", "Категория", "Закупка", "Опт", "Розница", "Остаток", "Минимум",
                "Стоимость остатка"])
    for p in db.execute(select(Product).order_by(Product.id)).scalars():
        ws3.append([p.id, p.barcode, p.name, p.category.name, p.purchase_price, p.wholesale_price, p.selling_price,
                    p.stock_quantity, p.minimum_stock, p.stock_quantity * p.purchase_price])

    ws4 = wb.create_sheet("Clients")
    ws4.append(["Клиент", "Телефон", "БИН/ИИН", "Город", "Статус", "Закупов", "Всего, ₸", "Этот месяц, ₸",
                "Первый закуп", "Последний закуп", "Не выполнены месяцы"])
    for c in db.execute(select(Customer).order_by(Customer.name)).scalars():
        st = services.customer_stats(db, c)
        ws4.append([c.name, c.phone, c.bin_iin, c.city, st["status_text"], st["purchases"], st["total_spent"],
                    st["month_sum"], st["first_purchase"], st["last_purchase"], ", ".join(st["missed_months"])])

    for sheet in wb.worksheets:
        for c in sheet[1]:
            c.font = Font(bold=True)
        for col in sheet.columns:
            sheet.column_dimensions[col[0].column_letter].width = min(
                60, max(10, max(len(str(c.value or "")) for c in col) + 2))

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": "attachment; filename=sales_report.xlsx"})

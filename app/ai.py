"""ИИ-помощник (разделы 30–31 ТЗ).

Как это работает:
  пользователь → модель (Ollama / OpenAI) → инструмент (функция программы) → база → ответ.
Модель НЕ выполняет SQL и не меняет данные напрямую:
  * инструменты чтения (поиск, отчёты, остатки) выполняются сразу;
  * инструменты изменения (поступление, цена, скрыть товар…) только создают
    «ожидающее действие», которое человек подтверждает кнопкой.
"""
import json
from datetime import date

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import config, reports, services
from .models import Category, Customer, PendingAction, Product, Sale

MAX_TOOL_ROUNDS = 6
HISTORY_LIMIT = 12


class AIError(Exception):
    pass


# ======================================================================
#  Поиск товара по тексту (id, штрихкод, артикул или часть названия)
# ======================================================================
def find_products(db: Session, query: str, limit: int = 10) -> list[Product]:
    q = str(query).strip()
    stmt = select(Product).where(Product.is_active.is_(True))
    if q.isdigit():
        exact = db.execute(stmt.where(or_(Product.id == int(q), Product.barcode == q))).scalars().all()
        if exact:
            return exact
    words = [w for w in q.split() if w]
    for w in words:
        stmt = stmt.where(Product.name.ilike(f"%{w}%") | Product.sku.ilike(f"%{w}%"))
    return db.execute(stmt.order_by(Product.name).limit(limit)).scalars().all()


def resolve_product(db: Session, ref) -> Product:
    """Находит ровно один товар. Если найдено 0 или несколько — ошибка с подсказкой для модели."""
    found = find_products(db, ref)
    if len(found) == 1:
        return found[0]
    if not found:
        raise services.BusinessError(f"Товар «{ref}» не найден. Уточните название у пользователя.")
    exact = [p for p in found if p.name.lower() == str(ref).strip().lower()]
    if len(exact) == 1:
        return exact[0]
    names = "; ".join(f"id {p.id}: {p.name}" for p in found[:8])
    raise services.BusinessError(f"Под «{ref}» подходит несколько товаров: {names}. Спросите пользователя, какой именно.")


def product_brief(p: Product) -> dict:
    return {"id": p.id, "name": p.name, "category": p.category.name, "barcode": p.barcode,
            "retail_price": p.selling_price, "wholesale_price": p.wholesale_price,
            "purchase_price": p.purchase_price,
            "stock": p.stock_quantity, "minimum": p.minimum_stock}


def _parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise services.BusinessError(f"Неверная дата «{value}», нужен формат ГГГГ-ММ-ДД")


# ======================================================================
#  Инструменты ЧТЕНИЯ — выполняются сразу
# ======================================================================
def t_search_products(db, query: str = ""):
    items = find_products(db, query, limit=15) if query else \
        db.execute(select(Product).where(Product.is_active.is_(True)).order_by(Product.name).limit(40)).scalars().all()
    return {"found": len(items), "products": [product_brief(p) for p in items]}


def t_low_stock(db):
    return {"low_stock": reports.low_stock(db)}


def t_sales_report(db, date_from: str = None, date_to: str = None):
    r = reports.sales_summary(db, _parse_date(date_from), _parse_date(date_to), top_limit=10)
    r.pop("low_stock", None)
    return r


def t_product_sales(db, date_from: str = None, date_to: str = None, order: str = "asc", limit: int = 15):
    return {"products": reports.product_sales(db, _parse_date(date_from), _parse_date(date_to), order, limit),
            "note": "order=asc: сначала товары, которые покупали меньше всего (включая 0 продаж)"}


def t_wholesale_clients(db, only_problems: bool = False):
    rows = [services.customer_stats(db, c) for c in
            db.execute(select(Customer).where(Customer.is_active.is_(True))).scalars()]
    if only_problems:
        rows = [r for r in rows if r["status"] in ("VIOLATION", "AT_RISK", "IN_PROGRESS")]
    keep = ("name", "phone", "city", "status_text", "total_spent", "month_sum", "month_left",
            "last_purchase", "days_since_last", "missed_months")
    return {"conditions": f"первый закуп от {config.WHOLESALE_FIRST_MIN} ₸, затем каждый месяц от "
                          f"{config.WHOLESALE_MONTHLY_MIN} ₸",
            "clients": [{k: r[k] for k in keep} for r in rows]}


def resolve_customer(db, ref) -> Customer:
    q = str(ref).strip()
    stmt = select(Customer).where(Customer.is_active.is_(True))
    found = db.execute(stmt.where(Customer.id == int(q))).scalars().all() if q.isdigit() else []
    if not found:
        for w in q.split():
            stmt = stmt.where(Customer.name.ilike(f"%{w}%"))
        found = db.execute(stmt).scalars().all()
    if len(found) == 1:
        return found[0]
    if not found:
        raise services.BusinessError(f"Оптовый клиент «{ref}» не найден")
    raise services.BusinessError("Подходит несколько клиентов: " + "; ".join(c.name for c in found[:8]))


def t_recent_sales(db, limit: int = 10):
    sales = db.execute(select(Sale).order_by(Sale.id.desc()).limit(min(int(limit), 30))).scalars()
    return [{"sale_id": s.id, "date": s.created_at.strftime("%Y-%m-%d %H:%M"), "total": s.total_amount,
             "type": "опт" if s.price_type == "WHOLESALE" else "розница",
             "client": s.customer.name if s.customer else None,
             "payment": s.payment_method, "status": s.status,
             "items": [f"{i.product.name} × {i.quantity}" for i in s.items]} for s in sales]


def t_list_categories(db):
    return [{"id": c.id, "name": c.name} for c in db.execute(select(Category).order_by(Category.name)).scalars()]


# ======================================================================
#  Инструменты ИЗМЕНЕНИЯ — только предлагают действие
# ======================================================================
def _propose(db, kind: str, params: dict, summary: str, source: str):
    a = PendingAction(kind=kind, params=json.dumps(params, ensure_ascii=False), summary=summary, source=source)
    db.add(a)
    db.commit()
    return {"status": "ждёт подтверждения пользователя", "action_id": a.id, "summary": summary,
            "note": "Действие НЕ выполнено. Пользователь увидит кнопку «Подтвердить»."}


def p_receive_stock(db, source, product: str, quantity: int, purchase_price: int = None):
    p = resolve_product(db, product)
    qty = int(quantity)
    if qty <= 0:
        raise services.BusinessError("Количество должно быть больше 0")
    s = f"Поступление: «{p.name}» +{qty} шт. (было {p.stock_quantity}, станет {p.stock_quantity + qty})"
    if purchase_price:
        s += f", новая закупочная цена {int(purchase_price)} ₸"
    return _propose(db, "receive_stock", {"product_id": p.id, "quantity": qty,
                                          "purchase_price": int(purchase_price) if purchase_price else None}, s, source)


def p_write_off(db, source, product: str, quantity: int, reason: str = ""):
    p = resolve_product(db, product)
    qty = int(quantity)
    if qty <= 0:
        raise services.BusinessError("Количество должно быть больше 0")
    if qty > p.stock_quantity:
        raise services.BusinessError(f"Нельзя списать {qty}: на складе «{p.name}» только {p.stock_quantity}")
    s = f"Списание: «{p.name}» −{qty} шт. (было {p.stock_quantity}, станет {p.stock_quantity - qty})"
    if reason:
        s += f". Причина: {reason}"
    return _propose(db, "adjust_stock", {"product_id": p.id, "new_quantity": p.stock_quantity - qty,
                                         "note": reason or "Списание через ИИ"}, s, source)


def p_set_stock(db, source, product: str, new_quantity: int):
    p = resolve_product(db, product)
    n = int(new_quantity)
    if n < 0:
        raise services.BusinessError("Остаток не может быть отрицательным")
    s = f"Корректировка остатка: «{p.name}» {p.stock_quantity} → {n} шт."
    return _propose(db, "adjust_stock", {"product_id": p.id, "new_quantity": n, "note": "Пересчёт через ИИ"}, s, source)


def p_set_price(db, source, product: str, price: int, price_type: str = "RETAIL"):
    p = resolve_product(db, product)
    if str(price_type).upper() == "WHOLESALE":
        new_retail = services.retail_from_wholesale(int(price))
        s = (f"Новая оптовая цена: «{p.name}» {p.wholesale_price} → {int(price)} ₸; "
             f"розница пересчитается: {p.selling_price} → {new_retail} ₸")
        return _propose(db, "set_wholesale_price", {"product_id": p.id, "price": int(price)}, s, source)
    s = f"Новая розничная цена: «{p.name}» {p.selling_price} → {int(price)} ₸"
    return _propose(db, "set_price", {"product_id": p.id, "price": int(price)}, s, source)


def p_hide_product(db, source, product: str):
    p = resolve_product(db, product)
    s = f"Убрать из продажи: «{p.name}» (история продаж сохранится)"
    return _propose(db, "deactivate_product", {"product_id": p.id}, s, source)


def p_create_product(db, source, name: str, category: str, purchase_price: int, wholesale_price: int,
                     retail_price: int = None, quantity: int = 0, minimum_stock: int = 0):
    cats = db.execute(select(Category).where(Category.name.ilike(f"%{category}%"))).scalars().all()
    if len(cats) != 1:
        all_names = ", ".join(c.name for c in db.execute(select(Category)).scalars())
        raise services.BusinessError(f"Категория «{category}» не найдена однозначно. Есть: {all_names}")
    retail = int(retail_price) if retail_price else services.retail_from_wholesale(int(wholesale_price))
    s = (f"Новый товар: «{name}» в категории «{cats[0].name}», закупка {int(purchase_price)} ₸, "
         f"опт {int(wholesale_price)} ₸, розница {retail} ₸, остаток {int(quantity)} шт.")
    return _propose(db, "create_product", {"name": name, "category_id": cats[0].id,
                                           "purchase_price": int(purchase_price),
                                           "wholesale_price": int(wholesale_price), "selling_price": retail,
                                           "quantity": int(quantity), "minimum_stock": int(minimum_stock)}, s, source)


def p_create_sale(db, source, items: list, payment_method: str = "CASH", client: str = None,
                  discount_percent: int = 0):
    """client указан → оптовая продажа по оптовым ценам. items[].gift=true → в подарок."""
    customer = resolve_customer(db, client) if client else None
    price_type = "WHOLESALE" if customer else "RETAIL"
    lines, parsed, total = [], [], 0
    for it in items or []:
        p = resolve_product(db, it.get("product"))
        qty = int(it.get("quantity", 1))
        if qty > p.stock_quantity:
            raise services.BusinessError(f"На складе «{p.name}» только {p.stock_quantity} шт.")
        price = services.unit_price(p, price_type)
        gift = bool(it.get("gift"))
        parsed.append({"product_id": p.id, "quantity": qty, "gift": gift})
        lines.append(f"{p.name} × {qty} — ПОДАРОК" if gift else f"{p.name} × {qty} = {price * qty} ₸")
        total += 0 if gift else price * qty
    if not parsed:
        raise services.BusinessError("Не указаны товары для продажи")
    pm = (payment_method or "CASH").upper()
    who = f"ОПТ, клиент «{customer.name}»" if customer else "розница"
    pct = int(discount_percent or 0)
    if pct:
        s = (f"Продажа ({who}, {pm}): " + "; ".join(lines) +
             f". Сумма {total} ₸, скидка {pct}% → к оплате {total - round(total * pct / 100)} ₸")
    else:
        s = f"Продажа ({who}, {pm}): " + "; ".join(lines) + f". Итого {total} ₸"
    return _propose(db, "create_sale", {"items": parsed, "payment_method": pm, "price_type": price_type,
                                        "customer_id": customer.id if customer else None,
                                        "discount_percent": pct}, s, source)


def p_return_sale(db, source, sale_id: int):
    sale = db.get(Sale, int(sale_id))
    if not sale:
        raise services.BusinessError(f"Продажа #{sale_id} не найдена")
    if sale.status == "RETURNED":
        raise services.BusinessError(f"Продажа #{sale_id} уже возвращена")
    s = f"Возврат продажи #{sale.id} на {sale.total_amount} ₸ (товар вернётся на склад)"
    return _propose(db, "return_sale", {"sale_id": sale.id}, s, source)


READ_TOOLS = {
    "search_products": t_search_products,
    "low_stock": t_low_stock,
    "sales_report": t_sales_report,
    "recent_sales": t_recent_sales,
    "list_categories": t_list_categories,
    "product_sales": t_product_sales,
    "wholesale_clients": t_wholesale_clients,
}
WRITE_TOOLS = {
    "receive_stock": p_receive_stock,
    "write_off": p_write_off,
    "set_stock": p_set_stock,
    "set_price": p_set_price,
    "hide_product": p_hide_product,
    "create_product": p_create_product,
    "create_sale": p_create_sale,
    "return_sale": p_return_sale,
}


def _fn(name, desc, props=None, required=None):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {}, "required": required or []}}}


PRODUCT = {"type": "string", "description": "Название товара (можно часть), штрихкод или id"}
TOOLS = [
    _fn("search_products", "Найти товары и узнать цену и остаток. Пустой query — список товаров.",
        {"query": {"type": "string", "description": "Часть названия, бренд, штрихкод или id"}}),
    _fn("low_stock", "Товары, которые заканчиваются (остаток ≤ минимума)."),
    _fn("sales_report", "Выручка, прибыль, маржа, число продаж, средний чек, топ товаров за период.",
        {"date_from": {"type": "string", "description": "ГГГГ-ММ-ДД"},
         "date_to": {"type": "string", "description": "ГГГГ-ММ-ДД"}}),
    _fn("recent_sales", "Последние продажи с номерами чеков.", {"limit": {"type": "integer"}}),
    _fn("product_sales", "Продажи по КАЖДОМУ товару за период, включая товары без продаж. "
        "Для вопросов «что покупали меньше всего / не продаётся / лучше всего».",
        {"date_from": {"type": "string", "description": "ГГГГ-ММ-ДД"},
         "date_to": {"type": "string", "description": "ГГГГ-ММ-ДД"},
         "order": {"type": "string", "enum": ["asc", "desc"], "description": "asc = худшие сначала"},
         "limit": {"type": "integer"}}),
    _fn("wholesale_clients", "Оптовые клиенты и выполнение условий закупа (первый закуп, ежемесячный минимум).",
        {"only_problems": {"type": "boolean", "description": "true = только те, кто не выполнил или может не выполнить"}}),
    _fn("list_categories", "Список категорий товаров."),
    _fn("receive_stock", "Оформить поступление товара на склад (приход).",
        {"product": PRODUCT, "quantity": {"type": "integer"},
         "purchase_price": {"type": "integer", "description": "Новая закупочная цена, если изменилась"}},
        ["product", "quantity"]),
    _fn("write_off", "Списать/убрать N штук со склада (брак, подарок, потеря).",
        {"product": PRODUCT, "quantity": {"type": "integer"}, "reason": {"type": "string"}}, ["product", "quantity"]),
    _fn("set_stock", "Установить точный остаток после пересчёта.",
        {"product": PRODUCT, "new_quantity": {"type": "integer"}}, ["product", "new_quantity"]),
    _fn("set_price", "Изменить цену товара. WHOLESALE — оптовая (розница пересчитается автоматически).",
        {"product": PRODUCT, "price": {"type": "integer"},
         "price_type": {"type": "string", "enum": ["RETAIL", "WHOLESALE"]}}, ["product", "price"]),
    _fn("hide_product", "Убрать товар из продажи (удалить позицию из каталога).", {"product": PRODUCT}, ["product"]),
    _fn("create_product", "Добавить новый товар в каталог.",
        {"name": {"type": "string"}, "category": {"type": "string"},
         "purchase_price": {"type": "integer"}, "wholesale_price": {"type": "integer"},
         "retail_price": {"type": "integer", "description": "Не указывать — посчитается как опт × коэффициент"},
         "quantity": {"type": "integer", "description": "Сколько штук сразу на склад"},
         "minimum_stock": {"type": "integer"}},
        ["name", "category", "purchase_price", "wholesale_price"]),
    _fn("create_sale", "Оформить продажу. Если указан client — оптовая продажа по оптовым ценам.",
        {"items": {"type": "array", "items": {"type": "object", "properties": {
            "product": PRODUCT, "quantity": {"type": "integer"},
            "gift": {"type": "boolean", "description": "true — этот товар в подарок (0 ₸)"}},
            "required": ["product", "quantity"]}},
         "payment_method": {"type": "string", "enum": ["CASH", "CARD", "TRANSFER", "OTHER"]},
         "discount_percent": {"type": "integer", "description": "Скидка на чек в процентах"},
         "client": {"type": "string", "description": "Имя оптового клиента (только для опта)"}}, ["items"]),
    _fn("return_sale", "Оформить возврат продажи по номеру чека.", {"sale_id": {"type": "integer"}}, ["sale_id"]),
]


def system_prompt() -> str:
    return (
        "Ты — помощник магазина корейской косметики mizon yalyan в программе MiniPOS. "
        f"Сегодня {date.today().isoformat()}. Валюта — тенге (₸). Отвечай по-русски, коротко и по делу.\n"
        "Правила:\n"
        "1. Цифры (цены, остатки, выручку) бери ТОЛЬКО из инструментов. Ничего не выдумывай.\n"
        "2. Чтобы что-то изменить (приход, списание, цена, новый товар, продажа, возврат, убрать товар) — "
        "вызови соответствующий инструмент. Он НЕ выполняет действие, а показывает пользователю кнопку "
        "«Подтвердить». Поэтому никогда не пиши «готово/сделано» — пиши «подтвердите действие».\n"
        "3. Если инструмент вернул ошибку, что товаров несколько или не найден — спроси пользователя.\n"
        "4. «Удалить позицию» = hide_product. «Прибавить/пришло» = receive_stock. «Убавить/списать» = write_off.\n"
        "5. Для периодов («за неделю», «в сентябре») сам вычисли даты ГГГГ-ММ-ДД.\n"
        f"6. Цены: есть оптовая и розничная (розница = опт × {config.RETAIL_MARKUP}). Продажа оптовому клиенту — "
        "по оптовой цене, обычному покупателю — по рознице.\n"
        "7. «Что покупали меньше всего / не продаётся» = product_sales с order=asc. "
        "«Кто из оптовиков не закупился» = wholesale_clients с only_problems=true."
    )


# ======================================================================
#  Вызов модели
# ======================================================================
def call_model(messages: list) -> dict:
    try:
        r = httpx.post(f"{config.AI_BASE_URL}/chat/completions",
                       headers={"Authorization": f"Bearer {config.AI_API_KEY}"},
                       json={"model": config.AI_MODEL, "messages": messages, "tools": TOOLS, "temperature": 0.2},
                       timeout=180)
    except httpx.ConnectError:
        raise AIError("ИИ не запущен. Установите Ollama (ollama.com), выполните «ollama pull "
                      f"{config.AI_MODEL}» и попробуйте снова. Адрес: {config.AI_BASE_URL}")
    except httpx.TimeoutException:
        raise AIError("ИИ отвечает слишком долго. Попробуйте модель поменьше (например qwen2.5:3b).")
    if r.status_code == 404 and "model" in r.text:
        raise AIError(f"Модель {config.AI_MODEL} не скачана. Выполните в терминале: ollama pull {config.AI_MODEL}")
    if r.status_code >= 400:
        raise AIError(f"Ошибка ИИ ({r.status_code}): {r.text[:300]}")
    return r.json()["choices"][0]["message"]


def run_tool(db: Session, name: str, args: dict, source: str, actions: list):
    try:
        if name in READ_TOOLS:
            return READ_TOOLS[name](db, **args)
        if name in WRITE_TOOLS:
            res = WRITE_TOOLS[name](db, source, **args)
            actions.append({"id": res["action_id"], "summary": res["summary"]})
            return res
        return {"error": f"Нет такого инструмента: {name}"}
    except services.BusinessError as e:
        return {"error": str(e)}
    except TypeError as e:
        return {"error": f"Неверные параметры: {e}"}


def chat(db: Session, history: list[dict], source: str = "web") -> dict:
    """history: [{"role": "user"|"assistant", "content": "..."}]. Возвращает ответ и предложенные действия."""
    messages = [{"role": "system", "content": system_prompt()}]
    messages += [{"role": m["role"], "content": str(m["content"])} for m in history[-HISTORY_LIMIT:]
                 if m.get("role") in ("user", "assistant") and m.get("content")]
    actions: list = []
    for _ in range(MAX_TOOL_ROUNDS):
        msg = call_model(messages)
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"reply": (msg.get("content") or "").strip() or "Готово, подтвердите действие ниже.",
                    "actions": actions}
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for c in calls:
            fn = c.get("function", {})
            try:
                args = fn.get("arguments") or {}
                args = json.loads(args) if isinstance(args, str) else args
            except json.JSONDecodeError:
                args = {}
            result = run_tool(db, fn.get("name"), args or {}, source, actions)
            messages.append({"role": "tool", "tool_call_id": c.get("id", fn.get("name")),
                             "name": fn.get("name"), "content": json.dumps(result, ensure_ascii=False, default=str)})
    return {"reply": "Не получилось завершить запрос. Попробуйте сформулировать проще.", "actions": actions}


# ======================================================================
#  Подтверждение / отмена действий
# ======================================================================
def confirm_action(db: Session, action_id: int) -> str:
    a = db.get(PendingAction, action_id)
    if not a:
        raise services.BusinessError("Действие не найдено")
    if a.status != "PENDING":
        raise services.BusinessError(f"Действие уже обработано ({a.status})")
    p = json.loads(a.params)
    try:
        if a.kind == "receive_stock":
            pr = services.receive_stock(db, p["product_id"], p["quantity"], p.get("purchase_price"), "Через ИИ")
            result = f"Принято. Остаток «{pr.name}»: {pr.stock_quantity} шт."
        elif a.kind == "adjust_stock":
            pr = services.adjust_stock(db, p["product_id"], p["new_quantity"], p.get("note"))
            result = f"Готово. Остаток «{pr.name}»: {pr.stock_quantity} шт."
        elif a.kind == "set_price":
            pr = services.set_selling_price(db, p["product_id"], p["price"])
            result = f"Розничная цена «{pr.name}» теперь {pr.selling_price} ₸"
        elif a.kind == "set_wholesale_price":
            pr = services.set_wholesale_price(db, p["product_id"], p["price"])
            result = f"«{pr.name}»: опт {pr.wholesale_price} ₸, розница {pr.selling_price} ₸"
        elif a.kind == "deactivate_product":
            pr = services.deactivate_product(db, p["product_id"])
            result = f"«{pr.name}» убран из продажи"
        elif a.kind == "create_product":
            pr = services.create_product(db, **p)
            result = f"Товар «{pr.name}» добавлен (id {pr.id})"
        elif a.kind == "create_sale":
            s = services.create_sale(db, p["items"], p["payment_method"], p.get("price_type", "RETAIL"),
                                     p.get("customer_id"), p.get("discount_percent", 0))
            result = f"Продажа #{s.id} на {s.total_amount} ₸ оформлена"
        elif a.kind == "return_sale":
            s = services.return_sale(db, p["sale_id"])
            result = f"Возврат продажи #{s.id} оформлен"
        else:
            raise services.BusinessError(f"Неизвестное действие {a.kind}")
    except services.BusinessError as e:
        a = db.get(PendingAction, action_id)
        a.status, a.result = "FAILED", str(e)
        db.commit()
        raise
    a = db.get(PendingAction, action_id)
    a.status, a.result = "DONE", result
    db.commit()
    return result


def cancel_action(db: Session, action_id: int) -> str:
    a = db.get(PendingAction, action_id)
    if not a or a.status != "PENDING":
        raise services.BusinessError("Действие не найдено или уже обработано")
    a.status = "CANCELLED"
    db.commit()
    return "Отменено"


def status() -> dict:
    """Проверка, доступен ли ИИ."""
    try:
        r = httpx.get(f"{config.AI_BASE_URL}/models", headers={"Authorization": f"Bearer {config.AI_API_KEY}"},
                      timeout=3)
        models = [m.get("id") for m in r.json().get("data", [])] if r.status_code == 200 else []
        ok = r.status_code == 200 and (config.AI_MODEL in models or not models
                                       or any(m and m.split(":")[0] == config.AI_MODEL.split(":")[0] for m in models))
        hint = "" if ok else f"Модель {config.AI_MODEL} не найдена. Выполните: ollama pull {config.AI_MODEL}"
        return {"ok": ok, "model": config.AI_MODEL, "url": config.AI_BASE_URL, "hint": hint}
    except Exception:
        return {"ok": False, "model": config.AI_MODEL, "url": config.AI_BASE_URL,
                "hint": "ИИ не запущен. Установите Ollama с ollama.com и выполните: ollama pull " + config.AI_MODEL}

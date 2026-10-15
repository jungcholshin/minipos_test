// MiniPOS — фронтенд (чистый JavaScript)
const $ = s => document.querySelector(s);
const money = n => (n || 0).toLocaleString('ru-RU') + ' ₸';
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const PAY = { CASH: 'Наличные', CARD: 'Карта', TRANSFER: 'Перевод', OTHER: 'Другое' };

async function api(url, opts = {}) {
  const res = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    throw new Error(Array.isArray(d) ? d.map(x => x.msg).join('; ') : d || 'Ошибка ' + res.status);
  }
  return data;
}

function toast(msg, err = false) {
  const t = $('#toast');
  t.textContent = msg; t.className = 'show' + (err ? ' err' : '');
  clearTimeout(t._t); t._t = setTimeout(() => t.className = '', 2800);
}

// ---------- вкладки ----------
const loaders = { pos: () => searchPos(), products: loadProducts, stock: loadStock, sales: loadSales, clients: loadClients, dashboard: loadDashboard, chat: loadChat, settings: loadSettings };
document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button, .tab').forEach(x => x.classList.remove('active'));
  b.classList.add('active'); $('#' + b.dataset.tab).classList.add('active');
  loaders[b.dataset.tab]();
});

// ---------- НАСТРОЙКИ ЦЕН ----------
let pricing = { retail_markup: 1.5, retail_round: 100, wholesale_first_min: 100000, wholesale_monthly_min: 30000 };
const retailFrom = w => Math.floor(w * pricing.retail_markup / pricing.retail_round + 0.5) * pricing.retail_round;
const STATUS = {
  VIOLATION: ['Нарушил условие', 'low'], AT_RISK: ['Под угрозой', 'risk'], IN_PROGRESS: ['В процессе', ''],
  OK: ['Выполняет', 'ok'], NEW: ['Нет закупов', 'new'],
};

// ---------- КАССА ----------
let cart = []; // {product, quantity, gift}
let discPct = 0;    // скидка на чек, %
let discAmt = 0;    // или скидка, ₸
let posProducts = [];
let mode = 'RETAIL';          // RETAIL / WHOLESALE
let clients = [];
const price = p => mode === 'WHOLESALE' ? (p.wholesale_price || 0) : p.selling_price;

document.querySelectorAll('.mode button').forEach(b => b.onclick = () => setMode(b.dataset.mode));

async function setMode(m) {
  mode = m;
  document.querySelectorAll('.mode button').forEach(x => x.classList.toggle('active', x.dataset.mode === m));
  $('.receipt').classList.toggle('wholesale', m === 'WHOLESALE');
  $('#wholesaleBox').hidden = m !== 'WHOLESALE';
  $('#receiptTitle').textContent = m === 'WHOLESALE' ? 'Чек · опт' : 'Чек · розница';
  if (m === 'WHOLESALE') await loadClientSelect();
  renderCart(); searchPos();
}

async function loadClientSelect() {
  clients = await api('/customers');
  const cur = $('#posClient').value;
  $('#posClient').innerHTML = '<option value="">— выберите оптового клиента —</option>' +
    clients.map(c => `<option value="${c.id}">${esc(c.name)}${c.city ? ' · ' + esc(c.city) : ''}</option>`).join('');
  $('#posClient').value = cur;
  showClientInfo();
}

function showClientInfo() {
  const c = clients.find(x => x.id == $('#posClient').value);
  if (!c) { $('#clientInfo').innerHTML = clients.length ? '' : 'Нет клиентов — добавьте на вкладке «Оптовики»'; return; }
  const [label] = STATUS[c.status] || [c.status];
  $('#clientInfo').innerHTML = `<b>${label}</b> · ${esc(c.status_text)}` +
    (c.purchases ? `<br>В этом месяце: <b>${money(c.month_sum)}</b> из ${money(c.month_min)} · всего ${money(c.total_spent)}` : '');
}
$('#posClient').onchange = () => { showClientInfo(); renderCart(); };

async function searchPos() {
  const q = $('#posSearch').value.trim();
  posProducts = await api('/products' + (q ? '?q=' + encodeURIComponent(q) : ''));
  $('#posResults').innerHTML = posProducts.map(p => `
    <div class="result ${p.stock_quantity <= 0 ? 'out' : ''}" data-id="${p.id}">
      <div>${esc(p.name)}<small>${esc(p.category)} · ${esc(p.barcode)} · в наличии: ${p.stock_quantity}</small></div>
      <div class="price">${money(price(p))}<small>${mode === 'WHOLESALE' ? 'розн. ' + money(p.selling_price) : 'опт ' + money(p.wholesale_price)}</small></div>
    </div>`).join('') || '<p class="muted">Ничего не найдено</p>';
  return posProducts;
}

$('#posResults').onclick = e => {
  const row = e.target.closest('.result'); if (!row) return;
  addToCart(posProducts.find(p => p.id == row.dataset.id));
};

let searchTimer;
$('#posSearch').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(searchPos, 200); };
$('#posSearch').onkeydown = async e => {
  if (e.key !== 'Enter') return;
  const q = e.target.value.trim(); if (!q) return;
  // Сканер штрихкода вводит цифры и жмёт Enter — пробуем точное совпадение
  if (/^\d{6,}$/.test(q)) {
    try { addToCart(await api('/products/barcode/' + q)); e.target.value = ''; searchPos(); return; }
    catch (err) { toast(err.message, true); return; }
  }
  const list = await searchPos();
  if (list.length === 1) { addToCart(list[0]); e.target.value = ''; searchPos(); }
};

const inCart = id => cart.filter(l => l.product.id === id).reduce((s, l) => s + l.quantity, 0);

function addToCart(p, gift = false) {
  if (!p) return;
  if (inCart(p.id) + 1 > p.stock_quantity) return toast(`Недостаточно на складе: ${p.name} (в наличии ${p.stock_quantity})`, true);
  const line = cart.find(l => l.product.id === p.id && l.gift === gift);
  line ? line.quantity++ : cart.push({ product: p, quantity: 1, gift });
  renderCart();
}

// суммы чека — так же, как считает сервер
function cartTotals() {
  const gross = cart.filter(l => !l.gift).reduce((s, l) => s + price(l.product) * l.quantity, 0);
  const disc = discPct ? Math.round(gross * discPct / 100) : Math.min(discAmt, gross);
  return { gross, disc, total: gross - disc };
}

document.querySelectorAll('.discount button').forEach(b => b.onclick = () => {
  discPct = Number(b.dataset.pct); discAmt = 0; $('#discAmount').value = '';
  document.querySelectorAll('.discount button').forEach(x => x.classList.toggle('active', x === b));
  renderCart();
});
$('#discAmount').oninput = e => {
  discAmt = Math.max(0, Number(e.target.value) || 0); discPct = 0;
  document.querySelectorAll('.discount button').forEach(x => x.classList.toggle('active', !discAmt && x.dataset.pct === '0'));
  renderCart();
};
function resetDiscount() { discPct = 0; discAmt = 0; $('#discAmount').value = ''; document.querySelector('.discount button[data-pct="0"]').click(); }

function renderCart() {
  $('#cart tbody').innerHTML = cart.map((l, i) => `
    <tr class="${l.gift ? 'gift' : ''}">
      <td>${l.gift ? '🎁 ' : ''}${esc(l.product.name)}<br><small class="muted">${l.gift ? 'подарок · себест. ' + money(l.product.purchase_price) : money(price(l.product))}</small></td>
      <td class="num"><span class="qty">
        <button data-i="${i}" data-d="-1">−</button><b>${l.quantity}</b><button data-i="${i}" data-d="1">+</button>
        <button class="gift-btn" data-gift="${i}" title="${l.gift ? 'Вернуть в оплату (1 шт.)' : 'Отдать 1 шт. в подарок'}">${l.gift ? '↩' : '🎁'}</button>
      </span></td>
      <td class="num">${l.gift ? '0 ₸' : money(price(l.product) * l.quantity)}</td>
    </tr>`).join('') || '<tr><td class="muted">Чек пуст — найдите товар слева</td></tr>';
  const { gross, disc, total } = cartTotals();
  const gifts = cart.filter(l => l.gift).reduce((s, l) => s + l.quantity, 0);
  $('#cartSums').innerHTML = (disc ? `Сумма: ${money(gross)}<br>Скидка${discPct ? ' ' + discPct + '%' : ''}: <span class="minus">−${money(disc)}</span>` : '') +
    (gifts ? `${disc ? '<br>' : ''}Подарков: ${gifts} шт.` : '');
  $('#cartTotal').textContent = money(total);
  // подсказка про минимальный первый закуп оптовика
  const c = clients.find(x => x.id == $('#posClient').value);
  if (mode === 'WHOLESALE' && c && c.status === 'NEW' && pricing.wholesale_first_min && cart.length) {
    const left = pricing.wholesale_first_min - total;
    $('#cartTotal').innerHTML += left > 0 ? `<br><small class="minus">до минимального первого закупа: ${money(left)}</small>` : '';
  }
}

$('#cart').onclick = e => {
  const g = e.target.closest('button[data-gift]');
  if (g) {   // перенести 1 шт. между «оплата» и «подарок»
    const l = cart[g.dataset.gift];
    l.quantity--;
    const other = cart.find(x => x.product.id === l.product.id && x.gift === !l.gift);
    other ? other.quantity++ : cart.push({ product: l.product, quantity: 1, gift: !l.gift });
    cart = cart.filter(x => x.quantity > 0);
    return renderCart();
  }
  const b = e.target.closest('button[data-i]'); if (!b) return;
  const l = cart[b.dataset.i];
  const d = Number(b.dataset.d);
  if (d > 0 && inCart(l.product.id) + 1 > l.product.stock_quantity) return toast('Больше нет на складе', true);
  l.quantity += d;
  if (l.quantity <= 0) cart.splice(b.dataset.i, 1);
  renderCart();
};
$('#clearCart').onclick = () => { cart = []; resetDiscount(); renderCart(); };

$('#checkout').onclick = async () => {
  if (!cart.length) return toast('Чек пуст', true);
  try {
    if (mode === 'WHOLESALE' && !$('#posClient').value) return toast('Выберите оптового клиента', true);
    const sale = await api('/sales', { method: 'POST', body: {
      payment_method: $('#payMethod').value, price_type: mode,
      customer_id: mode === 'WHOLESALE' ? Number($('#posClient').value) : null,
      discount_percent: discPct, discount_amount: discPct ? 0 : discAmt,
      items: cart.map(l => ({ product_id: l.product.id, quantity: l.quantity, gift: l.gift })) } });
    resetDiscount();
    if (mode === 'WHOLESALE') loadClientSelect();
    cart = []; renderCart(); searchPos();
    $('#lastReceipt').hidden = false;
    $('#lastReceipt').textContent = receiptText(sale);
    toast(`Продажа #${sale.id} — ${money(sale.total_amount)}`);
  } catch (err) { toast(err.message, true); }
};

function receiptText(s) {
  const lines = ['MIZON YALYAN · MINIPOS',
    s.price_type === 'WHOLESALE' ? 'ОПТ · ' + (s.customer || '') : 'РОЗНИЦА', ''];
  s.items.forEach(i => lines.push(i.name, i.gift ? `   ${i.quantity} шт.`.padEnd(22) + 'ПОДАРОК'.padStart(10)
    : `   ${i.quantity} × ${i.unit_price}`.padEnd(22) + String(i.unit_price * i.quantity).padStart(10)));
  lines.push('-'.repeat(32));
  if (s.discount_amount) lines.push('Сумма:'.padEnd(20) + money(s.gross_amount).padStart(12),
    `Скидка${s.discount_percent ? ' ' + s.discount_percent + '%' : ''}:`.padEnd(20) + ('−' + money(s.discount_amount)).padStart(12));
  lines.push('ИТОГО:'.padEnd(20) + money(s.total_amount).padStart(12),
    'Оплата: ' + PAY[s.payment_method], '', s.created_at, 'Продажа #' + s.id);
  return lines.join('\n');
}

// ---------- ТОВАРЫ ----------
let categories = [];
async function loadCategories() {
  categories = await api('/categories');
  const opts = categories.map(c => `<option value="${c.id}">${esc(c.name)} (${c.products})</option>`).join('');
  $('#productForm [name=category_id]').innerHTML = opts;
  $('#prodCategory').innerHTML = '<option value="">Все категории</option>' + opts;
}

async function loadProducts() {
  if (!categories.length) await loadCategories();
  const q = $('#prodSearch').value.trim(), c = $('#prodCategory').value;
  const params = new URLSearchParams(); if (q) params.set('q', q); if (c) params.set('category_id', c);
  const list = await api('/products?' + params);
  const pct = (a, b) => b ? Math.round((b - a) / b * 100) + '%' : '—';
  $('#pricingNote').textContent = `Розница = опт × ${pricing.retail_markup}, округление до ${pricing.retail_round} ₸ (меняется в settings.env).`;
  $('#productsTable').innerHTML = `<tr><th>Штрихкод</th><th>Название</th><th>Категория</th>
    <th class="num">Закупка</th><th class="num">Опт</th><th class="num">Розница</th>
    <th class="num">Маржа опт</th><th class="num">Маржа розн.</th><th class="num">Остаток</th></tr>` +
    list.map(p => `<tr><td>${esc(p.barcode)}</td><td>${esc(p.name)}</td><td>${esc(p.category)}</td>
      <td class="num">${money(p.purchase_price)}</td><td class="num">${money(p.wholesale_price)}</td>
      <td class="num">${money(p.selling_price)}${p.wholesale_price && retailFrom(p.wholesale_price) !== p.selling_price ? ' <span class="badge risk" title="Отличается от опт × коэффициент: ' + money(retailFrom(p.wholesale_price)) + '">≠</span>' : ''}</td>
      <td class="num">${pct(p.purchase_price, p.wholesale_price)}</td>
      <td class="num">${pct(p.purchase_price, p.selling_price)}</td>
      <td class="num">${p.stock_quantity}</td></tr>`).join('');
}
$('#productForm [name=wholesale_price]').oninput = e => {
  const w = Number(e.target.value);
  $('#productForm [name=selling_price]').placeholder = w ? `Розница: ${money(retailFrom(w))} (авто)` : 'Розница — посчитается';
};
$('#recalcRetail').onclick = async () => {
  if (!confirm(`Пересчитать розничные цены ВСЕХ товаров: опт × ${pricing.retail_markup}?`)) return;
  const r = await api('/products/recalc-retail', { method: 'POST' });
  toast(`Изменено цен: ${r.changed}`); loadProducts();
};
$('#prodSearch').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadProducts, 200); };
$('#prodCategory').onchange = loadProducts;

$('#productForm').onsubmit = async e => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  for (const k of ['category_id', 'purchase_price', 'selling_price', 'wholesale_price', 'minimum_stock'])
    f[k] = f[k] === '' ? null : Number(f[k]);
  for (const k of ['barcode', 'sku']) if (!f[k]) f[k] = null;
  if (f.minimum_stock == null) f.minimum_stock = 0;
  try {
    const p = await api('/products', { method: 'POST', body: f });
    e.target.reset(); toast(`Добавлен: ${p.name}. Оформите поступление на вкладке «Склад».`);
    await loadCategories(); loadProducts();
  } catch (err) { toast(err.message, true); }
};

$('#categoryForm').onsubmit = async e => {
  e.preventDefault();
  try {
    await api('/categories', { method: 'POST', body: { name: e.target.name.value } });
    e.target.reset(); toast('Категория добавлена'); loadCategories();
  } catch (err) { toast(err.message, true); }
};

// ---------- ИМПОРТ КАТАЛОГА ----------
let importData = null;   // {filename, content_base64}
const ACT = { create: ['Новый', 'ok'], update: ['Обновить', 'risk'], same: ['Без изменений', 'new'], error: ['Ошибка', 'low'] };

$('#importFile').onchange = async e => {
  const f = e.target.files[0]; if (!f) return;
  $('#importName').textContent = f.name;
  const buf = new Uint8Array(await f.arrayBuffer());
  let bin = ''; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  importData = { filename: f.name, content_base64: btoa(bin) };
  e.target.value = '';
  try {
    const r = await api('/import/products', { method: 'POST', body: { ...importData, apply: false } });
    const s = r.summary;
    $('#importSummary').innerHTML = `<span>Новых: <b>${s.create}</b></span><span>Обновится: <b>${s.update}</b></span>` +
      `<span>Без изменений: <b>${s.same}</b></span>${s.error ? `<span class="minus">С ошибками (пропустятся): <b>${s.error}</b></span>` : ''}`;
    $('#importTable').innerHTML = `<tr><th>Стр.</th><th>Действие</th><th>Название</th><th>Категория</th><th class="num">Закупка</th>
      <th class="num">Опт</th><th class="num">Розница</th><th class="num">Остаток</th><th>Примечание</th></tr>` +
      r.items.slice(0, 500).map(i => {
        const [label, cls] = ACT[i.action];
        const note = [...i.errors.map(x => `<small class="minus">${esc(x)}</small>`),
          ...(i.changes || []).map(x => `<small>${esc(x)}</small>`), ...i.warnings.map(x => `<small class="muted">${esc(x)}</small>`)].join('');
        const m = v => v == null ? '' : money(v);
        return `<tr class="${i.action === 'error' ? 'err' : ''}"><td>${i.row}</td><td><span class="badge ${cls}">${label}</span></td>
          <td>${esc(i.name)}</td><td>${esc(i.category)}</td><td class="num">${m(i.purchase_price)}</td><td class="num">${m(i.wholesale_price)}</td>
          <td class="num">${m(i.selling_price)}</td><td class="num">${i.stock ?? ''}</td><td>${note}</td></tr>`; }).join('');
    $('#importApply').disabled = !(s.create + s.update);
    $('#importApply').textContent = `Загрузить (${s.create + s.update})`;
    $('#importPreview').hidden = false;
  } catch (err) { toast(err.message, true); $('#importPreview').hidden = true; }
};
$('#importCancel').onclick = () => { importData = null; $('#importPreview').hidden = true; $('#importName').textContent = ''; };
$('#importApply').onclick = async () => {
  if (!importData) return;
  $('#importApply').disabled = true;
  try {
    const r = await api('/import/products', { method: 'POST', body: { ...importData, apply: true } });
    toast(`Готово: добавлено ${r.summary.create}, обновлено ${r.summary.update}`);
    $('#importCancel').click();
    categories = []; loadProducts();
  } catch (err) { toast(err.message, true); $('#importApply').disabled = false; }
};

// ---------- СКЛАД ----------
async function loadStock() {
  const list = await api('/products');
  $('#invValue').textContent = money(list.reduce((s, p) => s + p.stock_value, 0));
  list.sort((a, b) => (b.low_stock - a.low_stock) || a.name.localeCompare(b.name));
  $('#stockTable').innerHTML = `<tr><th>Товар</th><th class="num">Остаток</th><th class="num">Минимум</th>
    <th class="num">Закупка</th><th class="num">Стоимость</th><th>Статус</th><th>Действия</th></tr>` +
    list.map(p => `<tr><td>${esc(p.name)}</td><td class="num">${p.stock_quantity}</td><td class="num">${p.minimum_stock}</td>
      <td class="num">${money(p.purchase_price)}</td><td class="num">${money(p.stock_value)}</td>
      <td>${p.low_stock ? '<span class="badge low">LOW STOCK</span>' : '<span class="badge">OK</span>'}</td>
      <td><button data-recv="${p.id}">+ Поступление</button> <button data-adj="${p.id}" data-q="${p.stock_quantity}">Корректировка</button></td></tr>`).join('');
  const moves = await api('/stock/movements?limit=50');
  $('#movesTable').innerHTML = `<tr><th>Дата</th><th>Товар</th><th>Тип</th><th class="num">Кол-во</th><th>Основание</th></tr>` +
    moves.map(m => `<tr><td>${m.created_at}</td><td>${esc(m.product)}</td><td>${m.type}</td>
      <td class="num ${m.quantity > 0 ? 'plus' : 'minus'}">${m.quantity > 0 ? '+' : ''}${m.quantity}</td><td>${esc(m.reference)}</td></tr>`).join('');
}

$('#stockTable').onclick = async e => {
  const b = e.target.closest('button'); if (!b) return;
  try {
    if (b.dataset.recv) {
      const q = prompt('Сколько штук поступило?'); if (!q) return;
      await api(`/products/${b.dataset.recv}/receive`, { method: 'POST', body: { quantity: Number(q) } });
      toast('Поступление оформлено');
    } else if (b.dataset.adj) {
      const q = prompt('Фактический остаток после пересчёта:', b.dataset.q); if (q === null) return;
      await api(`/products/${b.dataset.adj}/adjust`, { method: 'POST', body: { new_quantity: Number(q), note: 'Пересчёт' } });
      toast('Остаток скорректирован');
    }
    loadStock();
  } catch (err) { toast(err.message, true); }
};

// ---------- ПРОДАЖИ ----------
async function loadSales() {
  if ($('#salesClient').options.length <= 1) {
    const cl = await api('/customers');
    $('#salesClient').innerHTML += cl.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
  }
  const p = new URLSearchParams();
  if ($('#salesType').value) p.set('price_type', $('#salesType').value);
  if ($('#salesClient').value) p.set('customer_id', $('#salesClient').value);
  const list = await api('/sales?' + p);
  $('#salesTable').innerHTML = `<tr><th>№</th><th>Дата и время</th><th>Тип</th><th>Товары</th><th>Оплата</th><th class="num">Сумма</th><th>Статус</th><th></th></tr>` +
    (list.map(s => `<tr><td>#${s.id}</td><td>${s.created_at}</td>
      <td>${s.price_type === 'WHOLESALE' ? `<span class="badge type-w">ОПТ</span><br><small>${esc(s.customer)}</small>` : 'Розница'}</td>
      <td>${s.items.map(i => `${i.gift ? '🎁 ' : ''}${esc(i.name)} × ${i.quantity}${i.gift ? ' <small class="muted">(подарок)</small>' : ''}`).join('<br>')}
        ${s.discount_amount ? `<br><small class="minus">скидка${s.discount_percent ? ' ' + s.discount_percent + '%' : ''}: −${money(s.discount_amount)}</small>` : ''}</td>
      <td>${PAY[s.payment_method]}</td><td class="num">${money(s.total_amount)}</td>
      <td>${s.status === 'RETURNED' ? '<span class="badge ret">ВОЗВРАТ</span>' : '<span class="badge">Продано</span>'}</td>
      <td>${s.status === 'COMPLETED' ? `<button data-ret="${s.id}">Возврат</button>` : ''}</td></tr>`).join('')
      || '<tr><td colspan="8" class="muted">Продаж нет</td></tr>');
}
$('#salesType').onchange = loadSales;
$('#salesClient').onchange = loadSales;
$('#salesTable').onclick = async e => {
  const b = e.target.closest('button[data-ret]'); if (!b) return;
  if (!confirm(`Оформить возврат продажи #${b.dataset.ret}? Товар вернётся на склад.`)) return;
  try { await api(`/sales/${b.dataset.ret}/return`, { method: 'POST' }); toast('Возврат оформлен'); loadSales(); }
  catch (err) { toast(err.message, true); }
};

// ---------- ОПТОВЫЕ КЛИЕНТЫ ----------
async function loadClients() {
  $('#conditionsNote').textContent = `Условия: первый закуп от ${money(pricing.wholesale_first_min)}, ` +
    `затем каждый календарный месяц от ${money(pricing.wholesale_monthly_min)} (меняется в settings.env).`;
  let list = await api('/customers');
  const f = $('#clientFilter').value;
  if (f === 'problems') list = list.filter(c => ['VIOLATION', 'AT_RISK', 'IN_PROGRESS'].includes(c.status));
  else if (f) list = list.filter(c => c.status === f);
  $('#clientsTable').innerHTML = `<tr><th>Клиент</th><th>Статус</th><th>Этот месяц</th><th class="num">Всего</th>
    <th>Последний закуп</th><th>Не выполнены</th><th></th></tr>` +
    (list.map(c => {
      const [label, cls] = STATUS[c.status] || [c.status, ''];
      const pctDone = c.month_min ? Math.min(100, Math.round(c.month_sum / c.month_min * 100)) : 100;
      return `<tr class="${c.status === 'VIOLATION' ? 'row-bad' : ''}">
        <td><b>${esc(c.name)}</b><br><small class="muted">${[c.phone, c.city, c.bin_iin && 'БИН/ИИН ' + c.bin_iin].filter(Boolean).map(esc).join(' · ')}</small></td>
        <td><span class="badge ${cls}">${label}</span><br><small class="muted">${esc(c.status_text)}</small></td>
        <td>${c.status === 'NEW' ? '<span class="muted">—</span>' : `${money(c.month_sum)} / ${money(c.month_min)}<div class="progress"><div style="width:${pctDone}%"></div></div>`}</td>
        <td class="num">${money(c.total_spent)}<br><small class="muted">${c.purchases} закуп.</small></td>
        <td>${c.last_purchase || '—'}${c.days_since_last != null ? `<br><small class="muted">${c.days_since_last} дн. назад</small>` : ''}</td>
        <td>${c.missed_months.length ? `<span class="minus">${c.missed_months.join(', ')}</span>` : '—'}</td>
        <td><button data-sales="${c.id}">Продажи</button> <button data-hide="${c.id}" title="Скрыть из списка">✕</button></td>
      </tr>`; }).join('') || '<tr><td colspan="7" class="muted">Нет клиентов</td></tr>');
}
$('#clientFilter').onchange = loadClients;
$('#clientsTable').onclick = async e => {
  const b = e.target.closest('button'); if (!b) return;
  if (b.dataset.sales) {
    if ($('#salesClient').options.length <= 1) await loadSales();
    $('#salesClient').value = b.dataset.sales; $('#salesType').value = '';
    document.querySelector('nav button[data-tab=sales]').click();
  } else if (b.dataset.hide) {
    if (!confirm('Скрыть клиента из списка? История продаж сохранится.')) return;
    await api('/customers/' + b.dataset.hide, { method: 'DELETE' }); loadClients();
  }
};
$('#clientForm').onsubmit = async e => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  for (const k in f) if (f[k] === '') f[k] = null;
  try {
    const c = await api('/customers', { method: 'POST', body: f });
    e.target.reset(); toast('Клиент добавлен: ' + c.name);
    $('#salesClient').innerHTML = '<option value="">Все клиенты</option>';
    loadClients();
  } catch (err) { toast(err.message, true); }
};

// ---------- DASHBOARD ----------
async function loadDashboard() {
  const p = new URLSearchParams();
  if ($('#dFrom').value) p.set('date_from', $('#dFrom').value);
  if ($('#dTo').value) p.set('date_to', $('#dTo').value);
  const r = await api('/reports/summary?' + p);
  if (!$('#dFrom').value) { $('#dFrom').value = r.period[0]; $('#dTo').value = r.period[1]; }
  const k = [['Выручка', money(r.revenue)], ['Розница', money(r.revenue_retail)], ['Опт', money(r.revenue_wholesale)],
    ['Прибыль', money(r.profit)], ['Маржа', r.margin + '%'],
    ['Скидки', money(r.discounts)], ['Подарки', `${r.gifts_qty} шт. · ${money(r.gifts_cost)}`],
    ['Продаж', r.sales_count], ['Средний чек', money(r.average_check)], ['Продано шт.', r.units_sold],
    ['Запасы (закупка)', money(r.inventory_value)]];
  $('#kpis').innerHTML = k.map(([a, b]) => `<div class="kpi"><div>${a}</div><b>${b}</b></div>`).join('');
  const max = Math.max(1, ...r.revenue_by_day.map(d => d.revenue));
  $('#chart').innerHTML = r.revenue_by_day.map(d =>
    `<div class="bar" style="height:${d.revenue / max * 100}%" title="${d.date}: ${money(d.revenue)}"><span>${d.date.slice(5)}</span></div>`).join('')
    || '<p class="muted">Нет продаж за период</p>';
  $('#topTable').innerHTML = `<tr><th>Товар</th><th class="num">Шт.</th><th class="num">Выручка</th><th class="num">Прибыль</th></tr>` +
    r.top_products.map(t => `<tr><td>${esc(t.name)}</td><td class="num">${t.quantity}</td><td class="num">${money(t.revenue)}</td><td class="num">${money(t.profit)}</td></tr>`).join('');
  $('#lowTable').innerHTML = `<tr><th>Товар</th><th class="num">Остаток</th><th class="num">Минимум</th></tr>` +
    r.low_stock.map(l => `<tr><td>${esc(l.name)}</td><td class="num">${l.stock}</td><td class="num">${l.minimum}</td></tr>`).join('');
}
$('#dApply').onclick = loadDashboard;

// ---------- НАСТРОЙКИ ----------
async function loadSettings() {
  const s = await api('/system/info');
  $('#autostart').checked = s.autostart;
  $('#autostart').disabled = !s.installed;
  $('#desktopShortcut').hidden = s.platform !== 'windows' || !s.installed;
  $('#sysNote').hidden = s.installed;
  $('#sysNote').textContent = 'Программа запущена из кода (режим разработки) — автозапуск и ярлык доступны в установленной программе.';
  $('#sysInfo').innerHTML = `<tr><td>Версия</td><td>${esc(s.version)}</td></tr>
    <tr><td>Папка с данными</td><td><code>${esc(s.data_dir)}</code></td></tr>`;
}
$('#autostart').onchange = async e => {
  try {
    const r = await api('/system/autostart', { method: 'POST', body: { enabled: e.target.checked } });
    e.target.checked = r.autostart;
    toast(r.autostart ? 'MiniPOS будет запускаться при включении компьютера' : 'Автозапуск выключен');
  } catch (err) { toast(err.message, true); e.target.checked = !e.target.checked; }
};
$('#desktopShortcut').onclick = async () => {
  try { toast((await api('/system/desktop-shortcut', { method: 'POST' })).result); } catch (err) { toast(err.message, true); }
};

// ---------- ИИ-ЧАТ ----------
let chatHistory = [];
let chatBusy = false;

async function loadChat() {
  $('#chatInput').focus();
  try {
    const s = await api('/ai/status');
    $('#aiStatus').textContent = s.ok ? 'ИИ: ' + s.model : 'ИИ не подключён';
    $('#aiStatus').className = 'badge ' + (s.ok ? 'ok' : 'low');
    $('#aiHint').hidden = s.ok; $('#aiHint').textContent = s.hint || '';
  } catch { $('#aiStatus').textContent = 'ошибка'; }
}

// простое форматирование ответа ИИ: **жирный**, заголовки ### и маркеры списков
const fmt = t => esc(t).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/^#{1,4}\s*(.+)$/gm, '<b>$1</b>')
  .replace(/^\s*[-*]\s+/gm, '• ');

function addMsg(cls, text) {
  const d = document.createElement('div');
  d.className = 'msg ' + cls;
  if (cls === 'bot') d.innerHTML = fmt(text); else d.textContent = text;
  $('#chatLog').appendChild(d); $('#chatLog').scrollTop = 1e9;
  return d;
}

function addAction(a) {
  const d = document.createElement('div');
  d.className = 'action';
  d.innerHTML = `<div>⚠️ ${esc(a.summary)}</div><div class="btns">
    <button class="primary" data-ok>Подтвердить</button><button data-no>Отмена</button></div>`;
  d.querySelector('[data-ok]').onclick = () => decide(d, a.id, 'confirm');
  d.querySelector('[data-no]').onclick = () => decide(d, a.id, 'cancel');
  $('#chatLog').appendChild(d); $('#chatLog').scrollTop = 1e9;
}

async function decide(el, id, what) {
  el.querySelectorAll('button').forEach(b => b.disabled = true);
  try {
    const r = await api(`/ai/actions/${id}/${what}`, { method: 'POST' });
    el.classList.add(what === 'confirm' ? 'done' : 'cancelled');
    el.querySelector('.btns').outerHTML = `<div class="res">${what === 'confirm' ? '✅' : '✖️'} ${esc(r.result)}</div>`;
    chatHistory.push({ role: 'assistant', content: (what === 'confirm' ? 'Пользователь подтвердил: ' : 'Пользователь отменил: ') + r.result });
  } catch (err) {
    el.querySelector('.btns').outerHTML = `<div class="res">⚠️ ${esc(err.message)}</div>`;
  }
}

async function sendChat(text) {
  if (chatBusy || !text.trim()) return;
  chatBusy = true;
  addMsg('user', text);
  chatHistory.push({ role: 'user', content: text });
  const typing = addMsg('bot typing', 'Думаю…');
  try {
    const r = await api('/ai/chat', { method: 'POST', body: { messages: chatHistory } });
    typing.remove();
    addMsg('bot', r.reply);
    chatHistory.push({ role: 'assistant', content: r.reply });
    r.actions.forEach(addAction);
  } catch (err) {
    typing.remove(); chatHistory.pop();
    addMsg('err', err.message);
  }
  chatBusy = false;
}

$('#chatForm').onsubmit = e => { e.preventDefault(); const t = $('#chatInput').value; $('#chatInput').value = ''; sendChat(t); };
document.querySelectorAll('.chips button').forEach(b => b.onclick = () => sendChat(b.dataset.q));
$('#chatReset').onclick = () => { chatHistory = []; $('#chatLog').querySelectorAll('.msg:not(:first-child), .action').forEach(x => x.remove()); };

// старт
api('/system/info').then(s => { $('#appVersion').textContent = 'v' + s.version; }).catch(() => {});
api('/pricing').then(p => { pricing = p; }).catch(() => {}).finally(() => { renderCart(); searchPos(); });

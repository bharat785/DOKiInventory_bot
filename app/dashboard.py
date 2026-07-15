"""Password-protected web dashboard: stock, spend, variance, items & BOM."""
import datetime as dt
import hmac

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func

from . import config, logic
from .models import (BomLine, InventoryTxn, Item, Payment, Product,
                     SessionLocal, StockCount, current_stock)

router = APIRouter()

STYLE = """
<style>
:root{--bg:#f6f4ef;--card:#fff;--ink:#26221c;--mut:#8a8377;--acc:#c96f2e;--ok:#3d7a4a;--warn:#c0392b}
*{box-sizing:border-box}body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--ink);margin:0}
nav{background:var(--ink);padding:10px 16px;display:flex;gap:18px;flex-wrap:wrap}
nav a{color:#eee;text-decoration:none;font-weight:600}nav a.active{color:var(--acc)}
main{max-width:960px;margin:20px auto;padding:0 14px}
.card{background:var(--card);border-radius:10px;padding:16px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #eee;font-size:14px}
th{color:var(--mut);font-size:12px;text-transform:uppercase}
.low{color:var(--warn);font-weight:700}.ok{color:var(--ok)}
.kpi{display:flex;gap:14px;flex-wrap:wrap}.kpi div{background:var(--card);border-radius:10px;padding:12px 18px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.kpi b{font-size:22px;display:block}
input,select,button{padding:8px;border:1px solid #ccc;border-radius:6px;font-size:14px}
button{background:var(--acc);color:#fff;border:0;cursor:pointer;font-weight:600}
form.inline{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
h1{font-size:20px}h2{font-size:16px}
.badge{font-size:11px;background:#eee;border-radius:10px;padding:2px 8px;color:var(--mut)}
</style>"""


def page(title, active, body):
    tabs = [("Stock", "/"), ("Count", "/count"), ("Spend", "/spend"),
            ("Variance", "/variance"), ("Items & Recipes", "/items")]
    nav = "".join(f'<a href="{u}" class="{"active" if u == active else ""}">{t}</a>'
                  for t, u in tabs)
    return HTMLResponse(f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>DOKi — {title}</title>{STYLE}
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script></head>
<body><nav><b style="color:#fff">🏭 DOKi</b>{nav}
<a href="/logout" style="margin-left:auto">Logout</a></nav><main>{body}</main></body></html>""")


# ---------------------------------------------------------------- auth
def authed(request: Request) -> bool:
    return hmac.compare_digest(request.cookies.get("doki_auth", ""),
                               config.DASHBOARD_PASSWORD)


def guard(request: Request):
    if not authed(request):
        return RedirectResponse("/login", status_code=302)
    return None


@router.get("/login", response_class=HTMLResponse)
def login_form():
    return HTMLResponse(f"""<!doctype html><html><head>{STYLE}</head><body>
<main style="max-width:360px;margin:80px auto"><div class=card>
<h1>🏭 DOKi Dashboard</h1><form method=post action=/login>
<input type=password name=password placeholder="Password" style="width:100%;margin-bottom:10px">
<button style="width:100%">Enter</button></form></div></main></body></html>""")


@router.post("/login")
def login(password: str = Form(...)):
    if hmac.compare_digest(password, config.DASHBOARD_PASSWORD):
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("doki_auth", password, httponly=True, max_age=60 * 60 * 24 * 30)
        return resp
    return RedirectResponse("/login", status_code=302)


@router.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("doki_auth")
    return resp


# ---------------------------------------------------------------- stock
@router.get("/", response_class=HTMLResponse)
def stock_page(request: Request):
    if (r := guard(request)):
        return r
    session = SessionLocal()
    try:
        snap = logic.stock_snapshot(session)
        low_count = sum(1 for i, s in snap
                        if i.reorder_threshold and s < i.reorder_threshold)
        month_start = dt.date.today().replace(day=1)
        spend = (session.query(func.coalesce(func.sum(Payment.amount), 0))
                 .filter(Payment.entry_date >= month_start).scalar())
        rows = ""
        for item, stock in snap:
            low = item.reorder_threshold and stock < item.reorder_threshold
            lp = logic.last_purchase(session, item.id)
            rows += f"""<tr><td>{item.name} <span class=badge>{item.category.replace('_', ' ')}</span></td>
<td class="{'low' if low else 'ok'}">{stock:g} {item.unit}</td>
<td>{(f"{item.reorder_threshold:g}" if item.reorder_threshold else "—")}</td>
<td>{f"{lp.entry_date} · {config.CURRENCY}{lp.unit_cost:g}/{item.unit}" if lp and lp.unit_cost else (str(lp.entry_date) if lp else "—")}</td></tr>"""
        body = f"""
<div class=kpi><div><b>{len(snap)}</b>items tracked</div>
<div><b class="{'low' if low_count else 'ok'}">{low_count}</b>below threshold</div>
<div><b>{config.CURRENCY}{spend:,.0f}</b>spend this month</div></div>
<div class=card><h2>Current stock</h2><table>
<tr><th>Item</th><th>Stock</th><th>Alert below</th><th>Last purchase</th></tr>{rows}</table></div>"""
        return page("Stock", "/", body)
    finally:
        session.close()


# ---------------------------------------------------------------- spend
@router.get("/spend", response_class=HTMLResponse)
def spend_page(request: Request, month: str = None):
    if (r := guard(request)):
        return r
    session = SessionLocal()
    try:
        # which month? default = current. month param format: YYYY-MM
        today = dt.date.today()
        try:
            y, m = (int(x) for x in (month or "").split("-"))
            month_start = dt.date(y, m, 1)
        except (ValueError, TypeError):
            month_start = today.replace(day=1)
        next_month = (month_start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        label = month_start.strftime("%B %Y")

        by_cat = (session.query(Payment.category, func.sum(Payment.amount))
                  .filter(Payment.entry_date >= month_start,
                          Payment.entry_date < next_month)
                  .group_by(Payment.category).all())
        labels = [c.replace("_", " ") for c, _ in by_cat]
        values = [round(v, 2) for _, v in by_cat]
        month_total = sum(values)

        payments = (session.query(Payment)
                    .filter(Payment.entry_date >= month_start,
                            Payment.entry_date < next_month)
                    .order_by(Payment.entry_date.desc()).limit(200).all())
        rows = "".join(
            f"<tr><td>{p.entry_date}</td><td>{config.CURRENCY}{p.amount:,.0f}</td>"
            f"<td>{p.category.replace('_', ' ')}</td><td>{p.vendor or '—'}</td>"
            f"<td>{(p.description or '')[:60]}</td><td>{(p.created_by or '').split(' (')[0]}</td>"
            f"<td><form method=post action=/payments/delete/{p.id} "
            f"onsubmit=\"return confirm('Delete this payment (and any stock entries "
            f"from the same invoice)? This cannot be undone.')\">"
            f"<button style='background:var(--warn);padding:2px 8px'>✕</button></form></td></tr>"
            for p in payments)

        # month selector: last 12 months with totals
        opts, hist_rows = "", ""
        cursor = today.replace(day=1)
        for _ in range(12):
            key = cursor.strftime("%Y-%m")
            nxt = (cursor.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
            total = (session.query(func.coalesce(func.sum(Payment.amount), 0))
                     .filter(Payment.entry_date >= cursor,
                             Payment.entry_date < nxt).scalar())
            sel = " selected" if cursor == month_start else ""
            opts += f'<option value="{key}"{sel}>{cursor.strftime("%b %Y")}</option>'
            if total:
                hist_rows += (f'<tr><td><a href="/spend?month={key}">'
                              f'{cursor.strftime("%B %Y")}</a></td>'
                              f'<td>{config.CURRENCY}{total:,.0f}</td></tr>')
            cursor = (cursor - dt.timedelta(days=1)).replace(day=1)

        body = f"""
<div class=card><form class=inline method=get action=/spend>
<label>Month:&nbsp;</label><select name=month onchange="this.form.submit()">{opts}</select>
</form></div>
<div class=kpi><div><b>{config.CURRENCY}{month_total:,.0f}</b>total — {label}</div></div>
<div class=card><h2>Spend by category — {label}</h2>
<canvas id=c height=110></canvas>
<script>new Chart(document.getElementById('c'),{{type:'doughnut',
data:{{labels:{labels},datasets:[{{data:{values}}}]}},
options:{{plugins:{{legend:{{position:'right'}}}}}}}});</script></div>
<div class=card><h2>Payments — {label}</h2><table>
<tr><th>Date</th><th>Amount</th><th>Category</th><th>Vendor</th><th>Note</th><th>By</th><th></th></tr>
{rows or '<tr><td colspan=7>No payments this month.</td></tr>'}</table></div>
<div class=card><h2>Last 12 months</h2><table>
<tr><th>Month</th><th>Total spend</th></tr>{hist_rows or '<tr><td colspan=2>No history yet.</td></tr>'}</table></div>"""
        return page("Spend", "/spend", body)
    finally:
        session.close()


# ------------------------------------------------------------- count page
@router.get("/count", response_class=HTMLResponse)
def count_page(request: Request):
    if (r := guard(request)):
        return r
    session = SessionLocal()
    try:
        snap = logic.stock_snapshot(session)
        today = dt.date.today()
        last = (session.query(func.max(StockCount.count_date)).scalar())
        rows = "".join(f"""<tr><td>{item.name}</td>
<td>{stock:g} {item.unit}</td>
<td><input name="qty_{item.id}" inputmode=decimal placeholder="counted {item.unit}" size=10></td></tr>"""
                       for item, stock in snap)
        body = f"""<div class=card><h2>Physical stock count — {today.strftime('%A %d %b %Y')}</h2>
<p style="color:var(--mut)">Count every item in the store and enter the actual quantity.
Leave blank to skip an item. Book stock will be adjusted to your counted values and the
variance saved to history. Last count: <b>{last or 'never'}</b>.</p>
<form method=post action=/count>
<table><tr><th>Item</th><th>Expected (book)</th><th>Counted (actual)</th></tr>{rows}</table>
<button style="margin-top:12px">Submit count</button></form></div>"""
        return page("Count", "/count", body)
    finally:
        session.close()


@router.post("/count", response_class=HTMLResponse)
async def count_submit(request: Request):
    if (r := guard(request)):
        return r
    form = await request.form()
    counts = {}
    for key, val in form.items():
        if key.startswith("qty_") and str(val).strip():
            try:
                counts[int(key[4:])] = float(val)
            except ValueError:
                pass
    session = SessionLocal()
    try:
        if not counts:
            return RedirectResponse("/count", status_code=302)
        results = logic.record_stock_count_by_ids(session, counts,
                                                  created_by="dashboard")
        rows = ""
        for item, expected, counted, variance in results:
            cls = "ok" if abs(variance) < 1e-9 else "low"
            pct = (variance / expected * 100) if expected else 0
            rows += (f"<tr><td>{item.name}</td><td>{expected:g}</td>"
                     f"<td>{counted:g}</td>"
                     f"<td class={cls}>{variance:+g} ({pct:+.1f}%)</td></tr>")
        body = f"""<div class=card><h2>✅ Count recorded — {len(results)} items</h2>
<table><tr><th>Item</th><th>Expected</th><th>Counted</th><th>Variance</th></tr>{rows}</table>
<p><a href="/variance">View variance history →</a></p></div>"""
        return page("Count", "/count", body)
    finally:
        session.close()


# -------------------------------------------------------------- variance
@router.get("/variance", response_class=HTMLResponse)
def variance_page(request: Request):
    if (r := guard(request)):
        return r
    session = SessionLocal()
    try:
        counts = (session.query(StockCount).order_by(StockCount.count_date.desc())
                  .limit(100).all())
        rows = ""
        for c in counts:
            pct = (c.variance / c.expected_qty * 100) if c.expected_qty else 0
            cls = "ok" if abs(c.variance) < 1e-9 else "low"
            rows += (f"<tr><td>{c.count_date}</td><td>{c.item.name}</td>"
                     f"<td>{c.expected_qty:g}</td><td>{c.counted_qty:g}</td>"
                     f"<td class={cls}>{c.variance:+g} ({pct:+.1f}%)</td>"
                     f"<td>{(c.created_by or '').split(' (')[0]}</td></tr>")
        body = f"""<div class=card><h2>Weekly count history</h2>
<p style="color:var(--mut)">Negative variance = missing stock (wastage, spillage, unlogged use).
Consistent negatives on one item usually mean the recipe under-states real usage.</p>
<table><tr><th>Date</th><th>Item</th><th>Expected</th><th>Counted</th><th>Variance</th><th>By</th></tr>
{rows or '<tr><td colspan=6>No counts yet — the bot asks every Sunday.</td></tr>'}</table></div>"""
        return page("Variance", "/variance", body)
    finally:
        session.close()


# ------------------------------------------------------- items & recipes
@router.get("/items", response_class=HTMLResponse)
def items_page(request: Request):
    if (r := guard(request)):
        return r
    session = SessionLocal()
    try:
        items = session.query(Item).filter(Item.active.is_(True)).order_by(Item.name).all()
        products = (session.query(Product).filter(Product.active.is_(True))
                    .order_by(Product.name).all())
        item_rows = "".join(f"""<tr><td>{i.name}</td><td>{i.unit}</td>
<td><form class=inline method=post action=/items/threshold>
<input type=hidden name=item_id value={i.id}>
<input name=threshold value="{i.reorder_threshold:g}" size=6>
<button>Save</button></form></td></tr>""" for i in items)
        opt_items = "".join(f"<option value={i.id}>{i.name} ({i.unit})</option>" for i in items)
        prod_blocks = ""
        for p in products:
            bom = "".join(f"<li>{bl.qty_per_unit:g} {bl.item.unit} {bl.item.name} "
                          f"<a href='/bom/delete/{bl.id}'>✕</a></li>" for bl in p.bom_lines)
            prod_blocks += f"""<div class=card><h2>{p.name} <span class=badge>per {p.unit}</span></h2>
<ul>{bom or '<li>No recipe lines yet</li>'}</ul>
<form class=inline method=post action=/bom/add>
<input type=hidden name=product_id value={p.id}>
<select name=item_id>{opt_items}</select>
<input name=qty placeholder="qty per unit" size=8><button>Add line</button></form></div>"""
        danger = f"""<div class=card style="border:1px solid var(--warn)">
<h2 style="color:var(--warn)">⚠️ Danger zone — wipe all data</h2>
<p style="color:var(--mut)">Deletes ALL items, stock, payments, recipes, and count history.
Use this once to clear test data before going live. Cannot be undone.</p>
<form class=inline method=post action=/admin/wipe
 onsubmit="return confirm('Really delete ALL data? This cannot be undone.')">
<input type=password name=password placeholder="Dashboard password">
<input name=confirm_text placeholder='Type: DELETE EVERYTHING'>
<button style="background:var(--warn)">Wipe all data</button></form></div>"""
        body = f"""
<div class=card><h2>Items & alert thresholds</h2>
<table><tr><th>Item</th><th>Unit</th><th>Alert when below</th></tr>{item_rows}</table>
<form class=inline method=post action=/items/add style="margin-top:12px">
<input name=name placeholder="New item name">
<select name=unit><option>kg</option><option>g</option><option>L</option><option>ml</option>
<option>pcs</option><option>box</option><option>bag</option></select>
<select name=category><option value=raw_material>raw material</option>
<option value=packaging>packaging</option><option value=other>other</option></select>
<button>Add item</button></form></div>
<div class=card><h2>Add product</h2>
<form class=inline method=post action=/products/add>
<input name=name placeholder="Product name">
<input name=unit placeholder="unit (pcs/pack/box)" value=pcs size=10>
<button>Add product</button></form></div>
{prod_blocks}
{danger}"""
        return page("Items & Recipes", "/items", body)
    finally:
        session.close()


@router.post("/payments/delete/{payment_id}")
def delete_payment(request: Request, payment_id: int):
    if (r := guard(request)):
        return r
    session = SessionLocal()
    try:
        logic.void_entry(session, [payment_id], [])
        return RedirectResponse("/spend", status_code=302)
    finally:
        session.close()


@router.post("/txns/delete/{txn_id}")
def delete_txn(request: Request, txn_id: int):
    if (r := guard(request)):
        return r
    session = SessionLocal()
    try:
        logic.void_entry(session, [], [txn_id])
        return RedirectResponse("/", status_code=302)
    finally:
        session.close()


@router.post("/admin/wipe")
def admin_wipe(request: Request, password: str = Form(...),
               confirm_text: str = Form("")):
    if (r := guard(request)):
        return r
    if not hmac.compare_digest(password, config.DASHBOARD_PASSWORD) or \
            confirm_text.strip().upper() != "DELETE EVERYTHING":
        return RedirectResponse("/items", status_code=302)
    session = SessionLocal()
    try:
        logic.wipe_all_data(session)
        return RedirectResponse("/", status_code=302)
    finally:
        session.close()


@router.post("/items/threshold")
def set_threshold(request: Request, item_id: int = Form(...), threshold: float = Form(...)):
    if (r := guard(request)):
        return r
    session = SessionLocal()
    try:
        item = session.get(Item, item_id)
        if item:
            item.reorder_threshold = threshold
            item.alert_sent = False
            session.commit()
        return RedirectResponse("/items", status_code=302)
    finally:
        session.close()


@router.post("/items/add")
def add_item(request: Request, name: str = Form(...), unit: str = Form("kg"),
             category: str = Form("raw_material")):
    if (r := guard(request)):
        return r
    session = SessionLocal()
    try:
        logic.get_or_create_item(session, name, unit, category)
        session.commit()
        return RedirectResponse("/items", status_code=302)
    finally:
        session.close()


@router.post("/products/add")
def add_product(request: Request, name: str = Form(...), unit: str = Form("pcs")):
    if (r := guard(request)):
        return r
    session = SessionLocal()
    try:
        if not logic.find_product(session, name):
            session.add(Product(name=name.strip().title(), unit=unit))
            session.commit()
        return RedirectResponse("/items", status_code=302)
    finally:
        session.close()


@router.post("/bom/add")
def add_bom(request: Request, product_id: int = Form(...), item_id: int = Form(...),
            qty: float = Form(...)):
    if (r := guard(request)):
        return r
    session = SessionLocal()
    try:
        session.add(BomLine(product_id=product_id, item_id=item_id, qty_per_unit=qty))
        session.commit()
        return RedirectResponse("/items", status_code=302)
    finally:
        session.close()


@router.get("/bom/delete/{line_id}")
def del_bom(request: Request, line_id: int):
    if (r := guard(request)):
        return r
    session = SessionLocal()
    try:
        bl = session.get(BomLine, line_id)
        if bl:
            session.delete(bl)
            session.commit()
        return RedirectResponse("/items", status_code=302)
    finally:
        session.close()

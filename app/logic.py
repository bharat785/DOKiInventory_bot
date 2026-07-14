"""Business logic: committing entries, stock maths, alerts, duplicates, variance."""
import datetime as dt
import logging

from sqlalchemy import func

from . import config
from .models import (BomLine, InventoryTxn, Item, Payment, PendingEntry,
                     Product, StockCount, current_stock)

log = logging.getLogger(__name__)

EXPENSE_CATEGORIES = ["raw_material", "packaging", "utilities", "repairs",
                      "transport", "water", "petty_cash", "other"]


# ------------------------------------------------------------------ items
def find_item(session, name: str):
    """Match by name or alias, case-insensitive."""
    if not name:
        return None
    n = name.strip().lower()
    for item in session.query(Item).filter(Item.active.is_(True)).all():
        if item.name.lower() == n:
            return item
        aliases = [a.strip().lower() for a in (item.aliases or "").split(",") if a.strip()]
        if n in aliases:
            return item
    return None


def get_or_create_item(session, name: str, unit: str = "kg", category="raw_material"):
    item = find_item(session, name)
    if item:
        return item, False
    item = Item(name=name.strip().title(), unit=unit or "kg", category=category)
    session.add(item)
    session.flush()
    return item, True


def find_product(session, name: str):
    if not name:
        return None
    n = name.strip().lower()
    for p in session.query(Product).filter(Product.active.is_(True)).all():
        if p.name.lower() == n:
            return p
    return None


# ------------------------------------------------------------- duplicates
def possible_duplicate(session, vendor, amount):
    """Same vendor + same amount within DUP_WINDOW_DAYS → probable duplicate."""
    if not vendor or not amount:
        return None
    cutoff = dt.date.today() - dt.timedelta(days=config.DUP_WINDOW_DAYS)
    return (session.query(Payment)
            .filter(func.lower(Payment.vendor) == vendor.strip().lower(),
                    Payment.amount == float(amount),
                    Payment.entry_date >= cutoff)
            .first())


# ----------------------------------------------------------------- commit
def commit_entry(session, payload: dict, created_by: str):
    """Write a confirmed parsed entry to the ledgers.

    Returns dict: {summary_lines: [...], low_stock: [Item...], new_items: [...]}
    """
    kind = payload.get("kind")
    entry_date = _parse_date(payload.get("date"))
    result = {"summary_lines": [], "low_stock": [], "new_items": []}

    if kind == "purchase":
        payment = None
        total = payload.get("total_amount")
        if total:
            payment = Payment(
                amount=float(total),
                category=payload.get("expense_category") or "raw_material",
                description=payload.get("description"),
                vendor=payload.get("vendor"), entry_date=entry_date,
                created_by=created_by)
            session.add(payment)
            session.flush()
        for line in payload.get("lines", []):
            item, created = get_or_create_item(session, line["item"],
                                               line.get("unit", "kg"))
            if created:
                result["new_items"].append(item.name)
            qty = float(line["qty"])
            txn = InventoryTxn(
                item_id=item.id, qty=qty, txn_type="purchase",
                unit_cost=line.get("unit_cost"), total_cost=line.get("line_total"),
                vendor=payload.get("vendor"), entry_date=entry_date,
                created_by=created_by,
                payment_id=payment.id if payment else None)
            session.add(txn)
            session.flush()
            stock = current_stock(session, item.id)
            result["summary_lines"].append(
                f"{item.name} +{qty:g}{item.unit} → stock {stock:g}{item.unit}")
            _reset_alert_if_recovered(item, stock)
        if total:
            result["summary_lines"].append(
                f"Spend logged: {config.CURRENCY}{float(total):,.0f} "
                f"({(payload.get('expense_category') or 'raw_material').replace('_', ' ')})")

    elif kind == "expense":
        amount = float(payload.get("total_amount") or 0)
        payment = Payment(
            amount=amount, category=payload.get("expense_category") or "other",
            description=payload.get("description"), vendor=payload.get("vendor"),
            entry_date=entry_date, created_by=created_by)
        session.add(payment)
        result["summary_lines"].append(
            f"Expense logged: {config.CURRENCY}{amount:,.0f} "
            f"({(payload.get('expense_category') or 'other').replace('_', ' ')})")

    elif kind == "production":
        product = find_product(session, payload.get("product"))
        qty = float(payload.get("product_qty") or 0)
        if not product:
            raise ValueError(f"Unknown product '{payload.get('product')}'. "
                             "Add it and its recipe on the dashboard first.")
        if not product.bom_lines:
            raise ValueError(f"No recipe (BOM) defined for {product.name} yet — "
                             "add it on the dashboard.")
        result["summary_lines"].append(f"Production: {qty:g} {product.unit} {product.name}")
        for bl in product.bom_lines:
            used = round(bl.qty_per_unit * qty, 3)
            txn = InventoryTxn(item_id=bl.item_id, qty=-used, txn_type="production_out",
                               note=f"{qty:g}x {product.name}", entry_date=entry_date,
                               created_by=created_by)
            session.add(txn)
            session.flush()
            stock = current_stock(session, bl.item_id)
            result["summary_lines"].append(
                f"{bl.item.name} -{used:g}{bl.item.unit} → stock {stock:g}{bl.item.unit}")
            if _breached(bl.item, stock):
                result["low_stock"].append((bl.item, stock))

    elif kind == "stock_out":
        for line in payload.get("lines", []):
            item = find_item(session, line["item"])
            if not item:
                raise ValueError(f"Unknown item '{line['item']}'")
            qty = float(line["qty"])
            session.add(InventoryTxn(item_id=item.id, qty=-qty, txn_type="manual_out",
                                     note=payload.get("description"),
                                     entry_date=entry_date, created_by=created_by))
            session.flush()
            stock = current_stock(session, item.id)
            result["summary_lines"].append(
                f"{item.name} -{qty:g}{item.unit} → stock {stock:g}{item.unit}")
            if _breached(item, stock):
                result["low_stock"].append((item, stock))
    else:
        raise ValueError("Could not classify this entry.")

    session.commit()
    return result


def _parse_date(s):
    if not s:
        return dt.date.today()
    try:
        return dt.date.fromisoformat(s)
    except (ValueError, TypeError):
        return dt.date.today()


def _breached(item, stock):
    """Low-stock alert due? Fires once per crossing."""
    if item.reorder_threshold and stock < item.reorder_threshold and not item.alert_sent:
        item.alert_sent = True
        return True
    return False


def _reset_alert_if_recovered(item, stock):
    if item.alert_sent and (not item.reorder_threshold or stock >= item.reorder_threshold):
        item.alert_sent = False


def last_purchase(session, item_id):
    return (session.query(InventoryTxn)
            .filter(InventoryTxn.item_id == item_id,
                    InventoryTxn.txn_type == "purchase")
            .order_by(InventoryTxn.created_at.desc()).first())


# ------------------------------------------------------------ stock count
def record_stock_count(session, counts: dict, created_by: str):
    """counts: {item_name: counted_qty}. Posts adjustment txns + count rows.
    Returns list of (item, expected, counted, variance)."""
    results = []
    for name, counted in counts.items():
        item = find_item(session, name)
        if not item:
            continue
        results.extend(_post_count(session, item, counted, created_by))
    session.commit()
    return results


def record_stock_count_by_ids(session, counts: dict, created_by: str):
    """Dashboard variant — counts: {item_id: counted_qty}."""
    from .models import Item
    results = []
    for item_id, counted in counts.items():
        item = session.get(Item, int(item_id))
        if not item:
            continue
        results.extend(_post_count(session, item, counted, created_by))
    session.commit()
    return results


def _post_count(session, item, counted, created_by):
    expected = current_stock(session, item.id)
    variance = round(float(counted) - expected, 3)
    session.add(StockCount(item_id=item.id, counted_qty=float(counted),
                           expected_qty=expected, variance=variance,
                           created_by=created_by))
    if abs(variance) > 1e-9:
        session.add(InventoryTxn(item_id=item.id, qty=variance,
                                 txn_type="adjustment",
                                 note="weekly stock count",
                                 created_by=created_by))
    return [(item, expected, float(counted), variance)]


# ---------------------------------------------------------------- digests
def daily_digest(session, day=None):
    day = day or dt.date.today()
    txns = (session.query(InventoryTxn)
            .filter(InventoryTxn.entry_date == day).all())
    pays = session.query(Payment).filter(Payment.entry_date == day).all()
    low = []
    for item in session.query(Item).filter(Item.active.is_(True)).all():
        stock = current_stock(session, item.id)
        if item.reorder_threshold and stock < item.reorder_threshold:
            low.append((item, stock))
    return {"txns": txns, "payments": pays, "low": low,
            "total_spend": sum(p.amount for p in pays)}


def stock_snapshot(session):
    out = []
    for item in (session.query(Item).filter(Item.active.is_(True))
                 .order_by(Item.name).all()):
        out.append((item, current_stock(session, item.id)))
    return out

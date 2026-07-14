"""Read-only analytics API.

Lets Claude (or any tool) pull the ledgers for deeper analysis, e.g.:
    GET {PUBLIC_URL}/api/summary?token=YOUR_ANALYTICS_TOKEN
    GET {PUBLIC_URL}/api/counts?days=180&token=...

Disabled entirely if ANALYTICS_TOKEN is not set. Read-only by design —
nothing here can modify the ledgers.
"""
import datetime as dt
import hmac

from fastapi import APIRouter, HTTPException, Query, Request

from . import config, logic
from .models import (InventoryTxn, Item, Payment, SessionLocal, StockCount,
                     current_stock)

router = APIRouter(prefix="/api")


def _check(request: Request, token: str = None):
    supplied = token or request.headers.get("x-api-key", "")
    if not config.ANALYTICS_TOKEN or not hmac.compare_digest(
            supplied, config.ANALYTICS_TOKEN):
        raise HTTPException(status_code=403, detail="Bad or missing token")


def _since(days: int) -> dt.date:
    return dt.date.today() - dt.timedelta(days=days)


@router.get("/summary")
def summary(request: Request, token: str = Query(None)):
    _check(request, token)
    session = SessionLocal()
    try:
        month_start = dt.date.today().replace(day=1)
        stock = [{"item": i.name, "unit": i.unit, "category": i.category,
                  "stock": s, "reorder_threshold": i.reorder_threshold,
                  "below_threshold": bool(i.reorder_threshold
                                          and s < i.reorder_threshold)}
                 for i, s in logic.stock_snapshot(session)]
        spend = {}
        for p in (session.query(Payment)
                  .filter(Payment.entry_date >= month_start).all()):
            spend[p.category] = round(spend.get(p.category, 0) + p.amount, 2)
        return {"as_of": dt.datetime.now().isoformat(timespec="seconds"),
                "stock": stock,
                "spend_this_month": spend,
                "spend_total_this_month": round(sum(spend.values()), 2)}
    finally:
        session.close()


@router.get("/stock")
def stock(request: Request, token: str = Query(None)):
    _check(request, token)
    session = SessionLocal()
    try:
        return [{"item": i.name, "unit": i.unit, "category": i.category,
                 "stock": s, "reorder_threshold": i.reorder_threshold}
                for i, s in logic.stock_snapshot(session)]
    finally:
        session.close()


@router.get("/transactions")
def transactions(request: Request, token: str = Query(None),
                 days: int = Query(90, le=730)):
    _check(request, token)
    session = SessionLocal()
    try:
        rows = (session.query(InventoryTxn)
                .filter(InventoryTxn.entry_date >= _since(days))
                .order_by(InventoryTxn.entry_date).all())
        return [{"date": str(t.entry_date), "item": t.item.name,
                 "qty": t.qty, "unit": t.item.unit, "type": t.txn_type,
                 "unit_cost": t.unit_cost, "total_cost": t.total_cost,
                 "vendor": t.vendor, "note": t.note,
                 "by": t.created_by} for t in rows]
    finally:
        session.close()


@router.get("/payments")
def payments(request: Request, token: str = Query(None),
             days: int = Query(90, le=730)):
    _check(request, token)
    session = SessionLocal()
    try:
        rows = (session.query(Payment)
                .filter(Payment.entry_date >= _since(days))
                .order_by(Payment.entry_date).all())
        return [{"date": str(p.entry_date), "amount": p.amount,
                 "category": p.category, "vendor": p.vendor,
                 "description": p.description, "by": p.created_by}
                for p in rows]
    finally:
        session.close()


@router.get("/counts")
def counts(request: Request, token: str = Query(None),
           days: int = Query(365, le=1095)):
    _check(request, token)
    session = SessionLocal()
    try:
        rows = (session.query(StockCount)
                .filter(StockCount.count_date >= _since(days))
                .order_by(StockCount.count_date).all())
        return [{"date": str(c.count_date), "item": c.item.name,
                 "unit": c.item.unit, "expected": c.expected_qty,
                 "counted": c.counted_qty, "variance": c.variance,
                 "variance_pct": round(c.variance / c.expected_qty * 100, 2)
                 if c.expected_qty else None,
                 "by": c.created_by} for c in rows]
    finally:
        session.close()

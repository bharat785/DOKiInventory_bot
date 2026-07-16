"""Database schema. Single source of truth for stock and spend.

Stock is NEVER stored directly — it is always the sum of transactions,
so the ledger stays auditable and adjustments are explicit.
"""
import datetime as dt

from sqlalchemy import (
    JSON, Boolean, Column, Date, DateTime, Float, ForeignKey, Integer,
    String, Text, create_engine, func,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from . import config

Base = declarative_base()


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


class Item(Base):
    """Raw material / consumable master."""
    __tablename__ = "items"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    aliases = Column(Text, default="")          # comma-separated alternate names
    unit = Column(String(20), default="kg")     # kg, g, L, pcs, box...
    # grams per piece for packaged items (e.g. 300 for a 300g cup); null for loose
    pack_size_g = Column(Float)
    category = Column(String(50), default="raw_material")
    reorder_threshold = Column(Float, default=0)
    # True when a low-stock alert has fired and stock hasn't recovered yet
    alert_sent = Column(Boolean, default=False)
    active = Column(Boolean, default=True)
    transactions = relationship("InventoryTxn", back_populates="item")


class InventoryTxn(Base):
    """Every stock movement. qty > 0 = in, qty < 0 = out."""
    __tablename__ = "inventory_txns"
    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    qty = Column(Float, nullable=False)
    # purchase | production_out | adjustment | manual_out
    txn_type = Column(String(30), nullable=False)
    unit_cost = Column(Float)                   # for purchases
    total_cost = Column(Float)
    vendor = Column(String(120))
    note = Column(Text)
    entry_date = Column(Date, default=dt.date.today)
    created_by = Column(String(120))            # telegram name (id)
    created_at = Column(DateTime(timezone=True), default=now_utc)
    payment_id = Column(Integer, ForeignKey("payments.id"))
    item = relationship("Item", back_populates="transactions")


class Payment(Base):
    """Every rupee out — inventory-linked or expense-only."""
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    amount = Column(Float, nullable=False)
    # raw_material | packaging | utilities | repairs | transport | petty_cash | other
    category = Column(String(50), default="other")
    description = Column(Text)
    vendor = Column(String(120))
    entry_date = Column(Date, default=dt.date.today)
    created_by = Column(String(120))
    created_at = Column(DateTime(timezone=True), default=now_utc)


class Product(Base):
    """Finished goods, each with a BOM (recipe)."""
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    unit = Column(String(20), default="pcs")
    active = Column(Boolean, default=True)
    bom_lines = relationship("BomLine", back_populates="product",
                             cascade="all, delete-orphan")


class BomLine(Base):
    """Raw material consumed per ONE unit of product."""
    __tablename__ = "bom_lines"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    qty_per_unit = Column(Float, nullable=False)
    product = relationship("Product", back_populates="bom_lines")
    item = relationship("Item")


class StockCount(Base):
    """Weekly physical count results + computed variance."""
    __tablename__ = "stock_counts"
    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    counted_qty = Column(Float, nullable=False)
    expected_qty = Column(Float, nullable=False)
    variance = Column(Float, nullable=False)    # counted - expected
    count_date = Column(Date, default=dt.date.today)
    created_by = Column(String(120))
    created_at = Column(DateTime(timezone=True), default=now_utc)
    item = relationship("Item")


class PendingEntry(Base):
    """A parsed message waiting for the ✅ Confirm tap."""
    __tablename__ = "pending_entries"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String(40), nullable=False)
    message_id = Column(String(40))
    payload = Column(JSON, nullable=False)      # parser output
    status = Column(String(20), default="pending")  # pending|committed|discarded
    created_by = Column(String(120))
    created_at = Column(DateTime(timezone=True), default=now_utc)


engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db():
    from sqlalchemy import text
    Base.metadata.create_all(engine)
    # lightweight migration for columns added after first deploy
    try:
        with engine.begin() as conn:
            if engine.dialect.name == "postgresql":
                conn.execute(text(
                    "ALTER TABLE items ADD COLUMN IF NOT EXISTS pack_size_g DOUBLE PRECISION"))
            else:
                conn.execute(text("ALTER TABLE items ADD COLUMN pack_size_g FLOAT"))
    except Exception:
        pass  # column already exists


def current_stock(session, item_id: int) -> float:
    total = session.query(func.coalesce(func.sum(InventoryTxn.qty), 0.0)).filter(
        InventoryTxn.item_id == item_id).scalar()
    return round(total or 0.0, 3)

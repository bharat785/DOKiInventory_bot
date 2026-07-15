"""Telegram bot: ingestion, confirmation flow, receipts, alerts, commands."""
import datetime as dt
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from . import config, logic, parser
from .models import Item, PendingEntry, Product, SessionLocal, current_stock

log = logging.getLogger(__name__)

application: Application = None  # set by build_application()


# ------------------------------------------------------------------ utils
def _user(update: Update) -> str:
    u = update.effective_user
    return f"{u.full_name} ({u.id})" if u else "unknown"


def _known_names(session):
    items = [i.name for i in session.query(Item).filter(Item.active.is_(True)).all()]
    products = [p.name for p in session.query(Product).filter(Product.active.is_(True)).all()]
    return items, products


def _preview_text(payload: dict, dup) -> str:
    kind = payload.get("kind", "unknown")
    icon = {"purchase": "📦", "expense": "🧾", "expense_batch": "🧾",
            "production": "🏭", "stock_out": "📤"}.get(kind, "❓")
    lines = [f"{icon} <b>{kind.replace('_', ' ').title()}</b>"]
    for e in payload.get("expenses") or []:
        d = f"{e['date']} — " if e.get("date") else ""
        lines.append(f"• {d}{e.get('description')} — "
                     f"{config.CURRENCY}{float(e.get('amount') or 0):,.0f}")
    if payload.get("vendor"):
        lines.append(f"Vendor: {payload['vendor']}")
    if payload.get("date"):
        lines.append(f"Date: {payload['date']}")
    for ln in payload.get("lines", []):
        pack = f" ({ln['pack_size']})" if ln.get("pack_size") else ""
        part = f"• {ln.get('item')}{pack} — {ln.get('qty')}{ln.get('unit', '')}"
        if ln.get("unit_cost"):
            part += f" @ {config.CURRENCY}{ln['unit_cost']:g}"
        if ln.get("line_total"):
            part += f" = {config.CURRENCY}{ln['line_total']:,.0f}"
        lines.append(part)
    if kind == "production":
        lines.append(f"• {payload.get('product_qty')} x {payload.get('product')}")
    if payload.get("total_amount"):
        lines.append(f"<b>Total: {config.CURRENCY}{payload['total_amount']:,.0f}</b>")
    if payload.get("expense_category"):
        lines.append(f"Category: {payload['expense_category'].replace('_', ' ')}")
    if payload.get("description"):
        lines.append(f"<i>{payload['description']}</i>")
    conf = payload.get("confidence", 0)
    if conf < 0.7:
        lines.append("⚠️ <i>Low confidence — please check carefully.</i>")
    for issue in payload.get("issues", []):
        lines.append(f"⚠️ {issue}")
    if dup:
        lines.append(f"🔁 <b>Possible duplicate</b> of entry on {dup.entry_date} "
                     f"(same vendor & amount). Confirm only if this is a separate bill.")
    lines.append("\nConfirm to add, or discard.")
    return "\n".join(lines)


def _confirm_kb(pending_id: int):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=f"ok:{pending_id}"),
        InlineKeyboardButton("❌ Discard", callback_data=f"no:{pending_id}"),
    ]])


async def send_low_stock_alerts(bot, session, breached):
    if not config.GROUP_CHAT_ID:
        log.warning("GROUP_CHAT_ID not set — low-stock alert not sent")
        return
    for item, stock in breached:
        lp = logic.last_purchase(session, item.id)
        extra = ""
        if lp:
            extra = (f"\nLast purchase: {lp.qty:g}{item.unit}"
                     + (f" @ {config.CURRENCY}{lp.unit_cost:g}" if lp.unit_cost else "")
                     + (f" from {lp.vendor}" if lp.vendor else "")
                     + f" on {lp.entry_date}")
        await bot.send_message(
            chat_id=config.GROUP_CHAT_ID,
            text=(f"⚠️ <b>Low stock: {item.name}</b>\n"
                  f"Current: {stock:g}{item.unit} "
                  f"(threshold {item.reorder_threshold:g}{item.unit}){extra}"),
            parse_mode=ParseMode.HTML)
    session.commit()


# ------------------------------------------------------------- ingestion
def _pdf_to_jpeg(pdf_bytes: bytes) -> bytes:
    """Render first 1-2 pages of a PDF to one JPEG for the parser."""
    import io
    import pypdfium2 as pdfium
    from PIL import Image
    pdf = pdfium.PdfDocument(pdf_bytes)
    pages = [pdf[i].render(scale=2.0).to_pil() for i in range(min(2, len(pdf)))]
    if len(pages) == 1:
        img = pages[0]
    else:  # stack two pages vertically
        w = max(p.width for p in pages)
        img = Image.new("RGB", (w, sum(p.height for p in pages)), "white")
        y = 0
        for p in pages:
            img.paste(p, (0, y))
            y += p.height
    img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    await msg.reply_chat_action("typing")
    try:
        photo = msg.photo[-1] if msg.photo else None
        doc = msg.document
        doc_mime = (doc.mime_type or "") if doc else ""
        if photo:
            f = await photo.get_file()
            mime = "image/jpeg"
        elif doc and (doc_mime.startswith("image/") or doc_mime == "application/pdf"):
            f = await doc.get_file()
            mime = doc_mime
        elif doc:
            await msg.reply_text(
                "❌ I can read photos and PDF invoices, but not this file type "
                f"({doc_mime or 'unknown'}). Send a photo or PDF instead.")
            return
        else:
            return
        image_bytes = bytes(await f.download_as_bytearray())
        if mime == "application/pdf":
            image_bytes = _pdf_to_jpeg(image_bytes)
            mime = "image/jpeg"
        await _parse_and_preview(update, text=msg.caption, image_bytes=image_bytes, mime=mime)
    except Exception:
        log.exception("photo handling failed")
        await msg.reply_text(
            "❌ Sorry, I couldn't read that photo. Please retake it (good light, flat, "
            "whole invoice in frame) or type the entry, e.g.\n"
            "\"bought 50kg sugar 2100rs from Sri Ram Traders\"")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = msg.text or ""
    if not text or text.startswith("/"):
        return
    # Stock count replies: "count: sugar 40, flour 22.5"
    if re.match(r"^\s*count\s*[:\-]", text, re.I):
        await _handle_count_reply(update, text)
        return
    # Threshold setting: "set sugar alert to 30"
    m = re.match(r"^\s*set\s+(.+?)\s+alert\s+(?:to\s+)?([\d.]+)\s*$", text, re.I)
    if m:
        await _set_threshold(update, m.group(1), float(m.group(2)))
        return
    await msg.reply_chat_action("typing")
    await _parse_and_preview(update, text=text)


async def _parse_and_preview(update: Update, text=None, image_bytes=None, mime="image/jpeg"):
    msg = update.effective_message
    session = SessionLocal()
    try:
        items, products = _known_names(session)
        try:
            payload = parser.parse_entry(text=text, image_bytes=image_bytes, mime=mime,
                                         known_items=items, known_products=products)
        except Exception:
            log.exception("parse failed")
            await msg.reply_text(
                "❌ I couldn't parse that. Try a clearer photo, or type it like:\n"
                "\"bought 50kg sugar 2100rs\" / \"paid 400 for drinking water\" / "
                "\"produced 300 packs of chikki\"")
            return
        if payload.get("kind") == "unknown" or (
                payload.get("kind") in ("purchase", "stock_out") and not payload.get("lines")) or (
                payload.get("kind") == "expense_batch" and not payload.get("expenses")):
            await msg.reply_text(
                "🤔 I couldn't tell what this is. Please rephrase — examples:\n"
                "• bought 50kg sugar 2100rs from Sri Ram Traders\n"
                "• paid 500 plumber repair (petty cash)\n"
                "• produced 300 packs of chikki\n"
                "• used 20kg flour for testing")
            return
        dup = logic.possible_duplicate(session, payload.get("vendor"),
                                       payload.get("total_amount"))
        pe = PendingEntry(chat_id=str(msg.chat_id), message_id=str(msg.message_id),
                          payload=payload, created_by=_user(update))
        session.add(pe)
        session.commit()
        await msg.reply_text(_preview_text(payload, dup),
                             parse_mode=ParseMode.HTML,
                             reply_markup=_confirm_kb(pe.id))
    finally:
        session.close()


# ------------------------------------------------------------ confirm tap
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, pid = q.data.split(":")
    session = SessionLocal()
    try:
        pe = session.get(PendingEntry, int(pid))
        if action == "undo":
            if pe and pe.status == "committed":
                await _handle_undo(q, session, pe)
            else:
                await q.edit_message_text("Nothing to undo here.")
            return
        if not pe or pe.status != "pending":
            await q.edit_message_text("This entry was already handled.")
            return
        if action == "no":
            pe.status = "discarded"
            session.commit()
            await q.edit_message_text("🗑️ Discarded. Nothing was added.")
            return
        try:
            result = logic.commit_entry(session, pe.payload, created_by=_user(update))
        except ValueError as e:
            await q.edit_message_text(f"❌ Couldn't add: {e}")
            return
        pe.status = "committed"
        pe.payload = {**pe.payload,
                      "committed_ids": {"payments": result["payment_ids"],
                                        "txns": result["txn_ids"]}}
        session.commit()
        lines = ["✔️ <b>Added.</b>"] + result["summary_lines"]
        for name in result["new_items"]:
            lines.append(f"🆕 New item created: {name} — set its alert threshold with "
                         f"\"set {name.lower()} alert to &lt;qty&gt;\"")
        undo_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ Undo", callback_data=f"undo:{pe.id}")]])
        await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                  reply_markup=undo_kb)
        if result["low_stock"]:
            await send_low_stock_alerts(context.bot, session, result["low_stock"])
    finally:
        session.close()


async def _handle_undo(q, session, pe):
    ids = (pe.payload or {}).get("committed_ids") or {}
    removed = logic.void_entry(session, ids.get("payments"), ids.get("txns"))
    pe.status = "undone"
    session.commit()
    await q.edit_message_text(
        f"↩️ Entry removed ({removed} records deleted). "
        "Stock and spend have been restored.")


# ------------------------------------------------------------- stock count
async def _handle_count_reply(update: Update, text: str):
    msg = update.effective_message
    body = re.sub(r"^\s*count\s*[:\-]\s*", "", text, flags=re.I)
    counts = {}
    for part in re.split(r"[,\n]+", body):
        m = re.match(r"\s*(.+?)\s+([\d.]+)\s*(?:kg|g|l|ml|pcs|box|bags?)?\s*$",
                     part.strip(), re.I)
        if m:
            counts[m.group(1).strip()] = float(m.group(2))
    if not counts:
        await msg.reply_text("Couldn't read any counts. Format:\n"
                             "count: sugar 40, flour 22.5, oil 15")
        return
    session = SessionLocal()
    try:
        results = logic.record_stock_count(session, counts, created_by=_user(update))
        unknown = [n for n in counts if not logic.find_item(session, n)]
        lines = ["📋 <b>Stock count recorded</b>"]
        for item, expected, counted, variance in results:
            flag = "✅" if abs(variance) < 1e-9 else ("🔺" if variance > 0 else "🔻")
            lines.append(f"{flag} {item.name}: counted {counted:g}{item.unit}, "
                         f"expected {expected:g}{item.unit} "
                         f"(variance {variance:+g})")
        if unknown:
            lines.append("❓ Unknown items skipped: " + ", ".join(unknown))
        lines.append("\nBook stock has been adjusted to the counted values.")
        await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    finally:
        session.close()


async def _set_threshold(update: Update, name: str, qty: float):
    msg = update.effective_message
    session = SessionLocal()
    try:
        item = logic.find_item(session, name)
        if not item:
            await msg.reply_text(f"❓ No item called '{name}'.")
            return
        item.reorder_threshold = qty
        item.alert_sent = False
        session.commit()
        await msg.reply_text(f"🔔 {item.name} alert threshold set to {qty:g}{item.unit}.")
    finally:
        session.close()


# --------------------------------------------------------------- commands
async def cmd_start(update: Update, context):
    await update.effective_message.reply_text(
        "👋 I'm the DOKi inventory bot.\n\n"
        "Send me:\n"
        "📷 a photo of an invoice or portal screenshot\n"
        "💬 a line like \"bought 50kg sugar 2100rs\"\n"
        "🏭 \"produced 300 packs of chikki\"\n"
        "🧾 \"paid 400 drinking water\" (expenses & petty cash)\n"
        "📋 \"count: sugar 40, flour 22.5\" (weekly stock count)\n\n"
        "Commands: /stock /spend /low /help\n"
        f"Group chat id (for setup): {update.effective_chat.id}")


async def cmd_stock(update: Update, context):
    session = SessionLocal()
    try:
        snap = logic.stock_snapshot(session)
        if not snap:
            await update.effective_message.reply_text("No items yet — send your first invoice!")
            return
        lines = ["📦 <b>Current stock</b>"]
        for item, stock in snap:
            warn = " ⚠️" if item.reorder_threshold and stock < item.reorder_threshold else ""
            lines.append(f"• {item.name}: {logic.fmt_qty(item, stock)}{warn}")
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    finally:
        session.close()


async def cmd_low(update: Update, context):
    session = SessionLocal()
    try:
        lows = [(i, s) for i, s in logic.stock_snapshot(session)
                if i.reorder_threshold and s < i.reorder_threshold]
        if not lows:
            await update.effective_message.reply_text("✅ Nothing below threshold.")
            return
        lines = ["⚠️ <b>Below threshold</b>"]
        for item, stock in lows:
            lines.append(f"• {item.name}: {stock:g}{item.unit} "
                         f"(threshold {item.reorder_threshold:g})")
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    finally:
        session.close()


async def cmd_spend(update: Update, context):
    import datetime as dt
    from sqlalchemy import func as f
    from .models import Payment
    session = SessionLocal()
    try:
        start = dt.date.today().replace(day=1)
        rows = (session.query(Payment.category, f.sum(Payment.amount))
                .filter(Payment.entry_date >= start)
                .group_by(Payment.category).all())
        total = sum(r[1] for r in rows)
        lines = [f"💰 <b>Spend this month</b> (from {start.strftime('%d %b')})"]
        for cat, amt in sorted(rows, key=lambda r: -r[1]):
            lines.append(f"• {cat.replace('_', ' ')}: {config.CURRENCY}{amt:,.0f}")
        lines.append(f"<b>Total: {config.CURRENCY}{total:,.0f}</b>")
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    finally:
        session.close()


async def cmd_help(update: Update, context):
    await cmd_start(update, context)


# ------------------------------------------------------------------ build
def build_application() -> Application:
    global application
    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("stock", cmd_stock))
    application.add_handler(CommandHandler("low", cmd_low))
    application.add_handler(CommandHandler("spend", cmd_spend))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(
        filters.PHOTO | filters.Document.IMAGE | filters.Document.ALL, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return application

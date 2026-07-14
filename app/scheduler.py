"""Scheduled jobs: 8pm daily digest + Sunday stock-count reminder (Asia/Kolkata)."""
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.constants import ParseMode

from . import config, logic
from .models import SessionLocal

log = logging.getLogger(__name__)


async def send_daily_digest(bot):
    if not config.GROUP_CHAT_ID:
        return
    session = SessionLocal()
    try:
        d = logic.daily_digest(session)
        lines = ["🌙 <b>Daily summary</b>"]
        if d["txns"]:
            lines.append(f"\n<b>Inventory entries: {len(d['txns'])}</b>")
            for t in d["txns"][:15]:
                sign = "+" if t.qty > 0 else ""
                lines.append(f"• {t.item.name} {sign}{t.qty:g}{t.item.unit} ({t.txn_type})")
            if len(d["txns"]) > 15:
                lines.append(f"…and {len(d['txns']) - 15} more")
        else:
            lines.append("\nNo inventory entries today.")
        if d["payments"]:
            lines.append(f"\n<b>Spend today: {config.CURRENCY}{d['total_spend']:,.0f}</b>")
            for p in d["payments"][:10]:
                lines.append(f"• {config.CURRENCY}{p.amount:,.0f} — "
                             f"{p.category.replace('_', ' ')}"
                             + (f" ({p.vendor})" if p.vendor else ""))
        else:
            lines.append("\nNo payments logged today.")
        if d["low"]:
            lines.append("\n⚠️ <b>Below threshold:</b>")
            for item, stock in d["low"]:
                lines.append(f"• {item.name}: {stock:g}{item.unit} "
                             f"(threshold {item.reorder_threshold:g})")
        await bot.send_message(chat_id=config.GROUP_CHAT_ID, text="\n".join(lines),
                               parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("digest failed")
    finally:
        session.close()


async def send_count_reminder(bot):
    if not config.GROUP_CHAT_ID:
        return
    session = SessionLocal()
    try:
        snap = logic.stock_snapshot(session)
        lines = ["📋 <b>Weekly stock count time!</b>",
                 "Count the store and reply in ONE message like:",
                 "<code>count: sugar 40, flour 22.5, oil 15</code>",
                 "\nExpected (book) stock right now:"]
        for item, stock in snap:
            lines.append(f"• {item.name}: {stock:g}{item.unit}")
        await bot.send_message(chat_id=config.GROUP_CHAT_ID, text="\n".join(lines),
                               parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("count reminder failed")
    finally:
        session.close()


def start_scheduler(bot) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=config.TIMEZONE)
    sched.add_job(send_daily_digest, CronTrigger(hour=config.DIGEST_HOUR, minute=0),
                  args=[bot], id="daily_digest")
    sched.add_job(send_count_reminder,
                  CronTrigger(day_of_week=config.COUNT_REMINDER_DOW,
                              hour=config.COUNT_REMINDER_HOUR, minute=0),
                  args=[bot], id="count_reminder")
    sched.start()
    return sched

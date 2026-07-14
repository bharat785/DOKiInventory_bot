"""Entry point: FastAPI app serving the Telegram webhook AND the dashboard."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from telegram import Update

from . import api
from . import bot as bot_module
from . import config, dashboard, scheduler
from .models import init_db

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("doki")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    tg = bot_module.build_application()
    await tg.initialize()
    await tg.start()
    if config.PUBLIC_URL:
        url = f"{config.PUBLIC_URL}/telegram/{config.WEBHOOK_SECRET}"
        await tg.bot.set_webhook(url, allowed_updates=Update.ALL_TYPES)
        log.info("Webhook set to %s", url)
    else:
        log.warning("PUBLIC_URL not set — webhook not registered")
    sched = scheduler.start_scheduler(tg.bot)
    app.state.tg = tg
    yield
    sched.shutdown(wait=False)
    await tg.stop()
    await tg.shutdown()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(dashboard.router)
app.include_router(api.router)


@app.post("/telegram/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != config.WEBHOOK_SECRET:
        return Response(status_code=403)
    tg = app.state.tg
    update = Update.de_json(await request.json(), tg.bot)
    await tg.process_update(update)
    return Response(status_code=200)


@app.get("/health")
def health():
    return {"ok": True}

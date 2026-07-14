# DOKi Inventory & Payments Bot

Telegram bot + live dashboard for DOKi Foods. The team sends invoice photos,
portal screenshots, or one-line messages to a Telegram group; the bot parses
them (Claude by default), asks for confirmation, and writes to a Postgres
ledger. The dashboard shows live stock, spend, and weekly-count variance.

## What the team can send

| Message | What happens |
|---|---|
| 📷 photo of an invoice / portal screenshot | Parsed → preview → ✅ Confirm → stock + spend updated |
| `bought 50kg sugar 2100rs from Sri Ram Traders` | Same as above, from text |
| `paid 400 drinking water` / `paid 500 plumber petty cash` | Expense logged (no stock impact) |
| `produced 300 packs of chikki` | Raw materials deducted per the product's recipe (BOM) |
| `used 20kg flour for testing` | Manual stock-out |
| `count: sugar 40, flour 22.5, oil 15` | Weekly physical count → variance recorded, book stock adjusted |
| `set sugar alert to 30` | Low-stock threshold changed |
| `/stock` `/low` `/spend` | Quick reports in chat |

Automatic: low-stock alerts to the group (once per crossing, with last-purchase
info), 8pm daily digest, Monday-10am stock-count reminder (Asia/Kolkata;
day configurable via `COUNT_REMINDER_DOW` — if Monday is a holiday, just do
the count Tuesday, the Count page works any day).

## Weekly physical count — two ways

- **Telegram**: reply `count: sugar 40, flour 22.5` to the Monday reminder.
- **Dashboard**: the **Count** tab lists every item with its expected (book)
  quantity next to an input box. Enter actuals, leave blanks to skip, submit —
  variances are shown immediately, saved to history, and book stock realigns.

## Querying from Claude (deeper analysis)

Set `ANALYTICS_TOKEN` to a long random string. The app then serves read-only
JSON at:

```
GET {PUBLIC_URL}/api/summary?token=...        current stock + month spend
GET {PUBLIC_URL}/api/stock?token=...          stock levels
GET {PUBLIC_URL}/api/transactions?days=90&token=...
GET {PUBLIC_URL}/api/payments?days=90&token=...
GET {PUBLIC_URL}/api/counts?days=365&token=...   count history w/ variance %
```

In a Claude/Cowork chat, just say e.g. *"fetch
https://your-app.up.railway.app/api/counts?days=180&token=XXX and analyse
which items keep showing negative variance"* — Claude fetches the JSON and can
chart, correlate with purchases, flag drift, etc. The token can also be sent
as an `X-API-Key` header. Endpoints are read-only; nothing can be modified
through them.

## Deploy on Railway (~15 minutes)

1. **Create the bot**: message [@BotFather](https://t.me/BotFather) → `/newbot` →
   copy the token. In BotFather, run `/setprivacy` → **Disable** so the bot can
   read group messages.
2. **Make the group**: create your ops Telegram group, add the bot to it.
3. **Railway**: New Project → *Deploy from GitHub repo* (push this folder to a
   private repo first) → also add **Postgres** to the project
   (New → Database → PostgreSQL). Railway auto-injects `DATABASE_URL` — add a
   variable reference to it on the app service if it isn't automatic.
4. **Variables**: on the app service, set everything in `.env.example`
   (token, `ANTHROPIC_API_KEY`, `DASHBOARD_PASSWORD`, secrets). Leave
   `GROUP_CHAT_ID` empty for now.
5. **Domain**: Settings → Networking → Generate Domain. Set `PUBLIC_URL` to it
   (e.g. `https://doki-bot.up.railway.app`). Redeploy.
6. **Get the group id**: in the group, send `/start` — the bot replies with the
   chat id (a negative number). Set it as `GROUP_CHAT_ID` and redeploy.
7. **Dashboard**: open `PUBLIC_URL` in a browser, log in, add your items with
   alert thresholds, and your products with recipes.

Done. Send a test invoice photo to the group.

## Swapping the AI model (no code changes)

```
PARSER_PROVIDER=anthropic   PARSER_MODEL=claude-haiku-4-5     (default)
PARSER_PROVIDER=gemini      PARSER_MODEL=gemini-3-flash       (+ GEMINI_API_KEY)
PARSER_PROVIDER=openai      PARSER_MODEL=gpt-5-mini           (+ OPENAI_API_KEY)
```
Change the variables on Railway and redeploy — that's it.

## Design notes

- **Stock is never stored** — it's always the sum of the transaction ledger, so
  every number on the dashboard can be traced to who sent what, when.
- **Nothing commits without a human ✅.** The parse preview + confirm tap is the
  quality-control layer; train the team to glance before tapping.
- **Duplicate guard**: same vendor + amount within 3 days is flagged in the
  preview.
- **Weekly counts post adjustment transactions**, so book stock realigns to
  reality and the variance history shows where material leaks.

## Local dev

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in, or leave DATABASE_URL unset to use sqlite
uvicorn app.main:app --reload
```

## Structure

```
app/
  main.py       FastAPI app: telegram webhook + dashboard + scheduler startup
  bot.py        Telegram handlers, confirm flow, receipts, alerts, commands
  parser.py     Model-agnostic AI parsing (anthropic/openai/gemini)
  logic.py      Ledger commits, stock maths, duplicates, variance, digests
  dashboard.py  Password-protected web UI (stock / spend / variance / items+BOM)
  scheduler.py  Daily digest + Sunday count reminder
  models.py     SQLAlchemy schema
  config.py     All env vars
```

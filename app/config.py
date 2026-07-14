"""Central configuration — everything comes from environment variables."""
import os

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# Chat ID of the main ops group (bot must be a member). Alerts + digests go here.
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID", "")
# Public URL of this app (Railway gives you one), used to register the webhook.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "doki-hook")

# --- AI parser (swappable) ---
# provider: "anthropic" | "openai" | "gemini"
PARSER_PROVIDER = os.environ.get("PARSER_PROVIDER", "anthropic")
PARSER_MODEL = os.environ.get("PARSER_MODEL", "claude-haiku-4-5")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# --- Database ---
# Railway injects DATABASE_URL for its Postgres plugin. Falls back to local sqlite for dev.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///doki_dev.db")
# SQLAlchemy needs postgresql:// not postgres://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# --- Dashboard ---
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "doki123")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "change-me-please")

# --- Behaviour ---
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Kolkata")
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "20"))        # 8 pm daily digest
COUNT_REMINDER_DOW = os.environ.get("COUNT_REMINDER_DOW", "mon")  # weekly stock count day
COUNT_REMINDER_HOUR = int(os.environ.get("COUNT_REMINDER_HOUR", "10"))
CURRENCY = os.environ.get("CURRENCY", "₹")
# Days within which same vendor+amount is flagged as possible duplicate
DUP_WINDOW_DAYS = int(os.environ.get("DUP_WINDOW_DAYS", "3"))

# --- Analytics API (read-only JSON, for querying from Claude etc.) ---
# Leave empty to disable the /api/* endpoints entirely.
ANALYTICS_TOKEN = os.environ.get("ANALYTICS_TOKEN", "")

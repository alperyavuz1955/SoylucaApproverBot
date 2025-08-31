from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ChatJoinRequestHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
WELCOME_MESSAGE = os.getenv(
    "WELCOME_MESSAGE",
    "{mention} hoş geldin! Grup kurallarını /kurallar komutuyla görebilirsin.",
)

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN env değişkeni zorunludur.")

# Bekleyen istekler: {user_id: (chat_id, user)}
pending_requests = {}

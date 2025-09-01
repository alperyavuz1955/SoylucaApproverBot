"""
Soyluca Onaylayıcı Bot
---------------------------------
• Katılım isteklerini yönetir (onayla / reddet).
• Eski istekleri de listeler ve komutlarla toplu onay yapılabilir.
• Komutlar:
    /start   -> Botu başlatır
    /id      -> Kendi user id gösterir
    /istek   -> Bekleyen istekleri listeler
    /sec     -> Grup/kanal seçmek için
    /onayla  -> Seçilen gruptaki istekleri onaylar
    /iptal   -> İşlemi iptal eder
"""

import asyncio
import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ChatJoinRequestHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# Log
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Ortam değişkenleri
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN env zorunlu")

# Bekleyen istekler
pending_requests = {}  # {chat_id: [user_id1, user_id2]}
selected_chat = {}     # {admin_id: chat_id}


# Komutlar
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Merhaba! Ben onay botuyum.\n/istek ile bekleyenleri görebilirsin.")


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_html(
        f"🆔 <code>{user.id}</code>\n👤 {user.full_name}\n@{user.username or '-'}"
    )


async def list_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tüm gruplardaki bekleyen istekleri listeler"""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return await update.message.reply_text("⛔ Yetkin yok")

    text = "📋 Bekleyen istekler:\n"
    if not pending_requests:
        text += "Hiç bekleyen istek yok."
    else:
        for chat_id, users in pending_requests.items():
            text += f"\n<b>{chat_id}</b> → {len(users)} istek"
    await update.message.reply_html(text)


async def sec_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kanal/grup seç"""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return await update.message.reply_text("⛔ Yetkin yok")

    if not pending_requests:
        return await update.message.reply_text("Hiç bekleyen istek yok.")

    keyboard = []
    for chat_id, users in pending_requests.items():
        keyboard.append([InlineKeyboardButton(f"{chat_id} ({len(users)} istek)", callback_data=f"sec:{chat_id}")])

    await update.message.reply_text("Bir grup seç:", reply_markup=InlineKeyboardMarkup(keyboard))


async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Seçilen gruptaki istekleri onayla"""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return await update.message.reply_text("⛔ Yetkin yok")

    chat_id = selected_chat.get(user_id)
    if not chat_id:
        return await update.message.reply_text("⚠️ Önce /sec ile bir grup seç.")

    users = pending_requests.get(chat_id, [])
    if not users:
        return await update.message.reply_text("✅ Bekleyen istek yok.")

    # Hız parametresi (varsayılan: orta)
    speed = "orta"
    if context.args:
        speed = context.args[0].lower()

    delay = 0.5
    if speed == "hızlı":
        delay = 0.1
    elif speed == "yavaş":
        delay = 1.5

    count = 0
    for uid in list(users):
        try:
            await context.bot.approve_chat_join_request(chat_id, uid)
            users.remove(uid)
            count += 1
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error("Onaylama hatası: %s", e)

    await update.message.reply_text(f"✅ {count} istek onaylandı. ({speed})")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Seçimi iptal et"""
    user_id = update.effective_user.id
    selected_chat.pop(user_id, None)
    await update.message.reply_text("❌ Seçim iptal edildi.")


# Join request yakala
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    user = req.from_user
    chat = req.chat

    pending_requests.setdefault(chat.id, []).append(user.id)

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"📩 Yeni istek: {user.full_name} ({user.id}) → {chat.title} ({chat.id})",
            )
        except:
            pass


# Callback (seçim için)
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data.startswith("sec:"):
        chat_id = int(query.data.split(":")[1])
        selected_chat[user_id] = chat_id
        await query.edit_message_text(f"✅ {chat_id} seçildi.")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("istek", list_requests))
    app.add_handler(CommandHandler("sec", sec_cmd))
    app.add_handler(CommandHandler("onayla", approve_cmd))
    app.add_handler(CommandHandler("iptal", cancel_cmd))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Bot başlıyor…")
    app.run_polling()


if __name__ == "__main__":
    main()

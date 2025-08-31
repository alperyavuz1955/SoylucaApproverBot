"""
Telegram İstek Onaylayıcı Bot (Butonlu Onay)
---------------------------------
• Amaç: Grup/kanal “İstekle katılım” açıkken gelen üyelik isteklerini admin butonlarıyla onaylamak/reddetmek.
• Kütüphane: python-telegram-bot==21.6
• Çalıştırma: BOT_TOKEN=xxxxx ADMIN_IDS=111,222 python app.py

Nasıl çalışır?
- Katılma isteği geldiğinde bot, adminlere özel mesaj gönderir.
- Mesajda Onayla / Reddet butonları çıkar.
- Admin tıkladığında işlem yapılır ve kullanıcıya sonuç uygulanır.

Notlar:
- Bot, grupta admin ve “Üyelik isteklerini yönet” iznine sahip olmalıdır.
- ADMIN_IDS env değişkenine admin Telegram user_id’lerini yaz (virgülle ayır).
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Merhaba! Ben butonlu onay botuyum. /id yazarak user_id'ni öğrenebilirsin.")


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = (
        f"🆔 User ID: <code>{user.id}</code>\n"
        f"👤 Ad: {user.full_name}\n"
        f"@ Kullanıcı adı: @{user.username or '-'}"
    )
    await update.message.reply_html(text)


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    req = update.chat_join_request
    user = req.from_user
    chat = req.chat

    pending_requests[user.id] = (chat.id, user)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Onayla", callback_data=f"approve:{chat.id}:{user.id}"),
                InlineKeyboardButton("❌ Reddet", callback_data=f"decline:{chat.id}:{user.id}"),
            ]
        ]
    )

    text = (
        f"📩 Yeni istek: <a href='tg://user?id={user.id}'>{user.full_name}</a> "
        f"(@{user.username or '-'} / <code>{user.id}</code>)\n"
        f"Chat: {chat.title} ({chat.id})"
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning("Admin bildirimi başarısız: %s", e)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if update.effective_user.id not in ADMIN_IDS:
        await query.edit_message_text("⛔ Bu işlem için yetkin yok.")
        return

    try:
        action, chat_id_str, user_id_str = query.data.split(":")
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)
    except ValueError:
        await query.edit_message_text("Hatalı veri.")
        return

    if user_id not in pending_requests:
        await query.edit_message_text("İstek zaten işlenmiş.")
        return

    chat_id_stored, user = pending_requests.pop(user_id)
    if chat_id != chat_id_stored:
        await query.edit_message_text("Veri uyuşmuyor.")
        return

    if action == "approve":
        await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        mention = f"<a href='tg://user?id={user.id}'>{user.first_name}</a>"
        welcome = WELCOME_MESSAGE.format(mention=mention)
        await context.bot.send_message(chat_id=chat_id, text=welcome, parse_mode=ParseMode.HTML)
        await query.edit_message_text(f"✅ {user.full_name} onaylandı.")
    elif action == "decline":
        await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
        await query.edit_message_text(f"❌ {user.full_name} reddedildi.")


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["id", "kimim"], my_id))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Bot başlıyor…")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.sleep(0))
    except RuntimeError:
        pass
    main()

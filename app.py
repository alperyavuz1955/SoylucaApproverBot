"""
Telegram İstek Onaylayıcı Bot (PTB 21.7)
- Grup/kanal "İstekle katılım" açıkken gelen join isteklerini yöneticilere bildirir.
- Tek tek butonla onayla/ret veya komutla toplu onay/ret yapar.
- Oran sınırlama + bekleme: RetryAfter yakalanır, otomatik beklenir.

ENV:
  BOT_TOKEN        -> Telegram bot token (zorunlu)
  ADMIN_IDS        -> Virgülle ayrılmış admin user_id listesi. Örn: "111,222"
  WELCOME_MESSAGE  -> (opsiyonel) Onay sonrası gönderilecek mesaj. {mention} değişkenini destekler.
  MAX_RATE_PER_SEC -> (opsiyonel) saniyedeki en fazla işlem. Varsayılan 10
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, Tuple, List, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, Forbidden, BadRequest
from telegram.ext import (
    Application,
    ChatJoinRequestHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ---------- Ayarlar ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS: List[int] = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
WELCOME_MESSAGE = os.getenv(
    "WELCOME_MESSAGE",
    "🎉 {mention} hoş geldin! Kuralları /kurallar ile görebilirsin.",
)
MAX_RATE_PER_SEC = float(os.getenv("MAX_RATE_PER_SEC", "10"))  # saniyede maksimum işlem

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN env değişkeni zorunludur.")
# ------------------------------

# Bekleyen istek havuzu: user_id -> (chat_id, user)
pending_requests: Dict[int, Tuple[int, "telegram.User"]] = {}

# Basit oran sınırlama (token bucket benzeri)
_last_ops: List[float] = []  # son 1 saniyede yapılan işlem anları


def _record_op():
    """Oran sınırlama için zaman damgası kaydı."""
    import time

    now = time.time()
    _last_ops.append(now)
    # yalnız son 1 saniyeyi tut
    while _last_ops and now - _last_ops[0] > 1.0:
        _last_ops.pop(0)


async def _rate_limit():
    """Saniyede MAX_RATE_PER_SEC'i aşmamak için bekle."""
    import time

    while True:
        now = time.time()
        # 1 saniye penceresinde kaç işlem var
        _last_ops[:] = [t for t in _last_ops if now - t <= 1.0]
        if len(_last_ops) < MAX_RATE_PER_SEC:
            return
        # pencere dolu ise biraz bekle
        await asyncio.sleep(0.02)


# -------------- Komutlar --------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Merhaba! Ben butonlu onay botuyum.\n"
        "/id ile kendi user_id'ni öğrenebilirsin.\n"
        "/status ile bekleyen istek sayısını görebilirsin.\n"
        "/approveall [n] → bekleyenlerden n tanesini (boşsa hepsini) onayla.\n"
        "/declineall [n] → bekleyenlerden n tanesini reddet.\n"
        f"(Rate limit: {MAX_RATE_PER_SEC}/sn)"
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    text = (
        f"🆔 <b>{u.id}</b>\n"
        f"👤 {u.full_name}\n"
        f"@{u.username or '-'}"
    )
    await update.message.reply_html(text)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Bekleyen istek: {len(pending_requests)}")


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# -------------- Join isteği geldiğinde --------------
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    req = update.chat_join_request
    user = req.from_user
    chat = req.chat

    pending_requests[user.id] = (chat.id, user)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Onayla", callback_data=f"approve:{chat.id}:{user.id}"
                ),
                InlineKeyboardButton(
                    "❌ Reddet", callback_data=f"decline:{chat.id}:{user.id}"
                ),
            ]
        ]
    )

    text = (
        f"📩 Yeni istek: <a href='tg://user?id={user.id}'>{user.full_name}</a> "
        f"(@{user.username or '-'} / <code>{user.id}</code>)\n"
        f"Chat: <code>{chat.title}</code> (<code>{chat.id}</code>)"
    )

    # tüm adminlere bildir
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id, text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning("Admin bildirimi hatası: %s", e)


# -------------- Buton işlemcisi --------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not _is_admin(update.effective_user.id):
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

    stored_chat_id, user = pending_requests.pop(user_id, (None, None))
    if stored_chat_id != chat_id:
        await query.edit_message_text("Veri uyuşmuyor.")
        return

    try:
        if action == "approve":
            await _approve_one(context, chat_id, user)
            await query.edit_message_text(f"✅ {user.full_name} onaylandı.")
        else:
            await _decline_one(context, chat_id, user.id, user.full_name)
            await query.edit_message_text(f"❌ {user.full_name} reddedildi.")
    except Exception as e:
        await query.edit_message_text(f"⚠️ İşlem hatası: {e}")


# -------------- Tek işlem yardımcıları --------------
async def _approve_one(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user: "telegram.User"
) -> None:
    """Tek kişiyi onayla (rate limit + retry)."""
    await _rate_limit()
    try:
        await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user.id)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 0.1)
        await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user.id)

    # hoş geldin
    mention = f"<a href='tg://user?id={user.id}'>{user.first_name}</a>"
    msg = WELCOME_MESSAGE.format(mention=mention)
    try:
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
    except (Forbidden, BadRequest):
        pass  # mesaj yetkisi yoksa sessiz geç


async def _decline_one(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, fullname: str
) -> None:
    await _rate_limit()
    try:
        await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 0.1)
        await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)


# -------------- Toplu komutlar --------------
async def approve_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /approveall [n]  → bekleyenlerden n adede kadar onayla (boşsa hepsi) """
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Yetkisiz.")
        return

    limit: Optional[int] = None
    if context.args:
        try:
            limit = max(0, int(context.args[0]))
        except ValueError:
            return await update.message.reply_text("Kullanım: /approveall [adet]")

    items = list(pending_requests.items())
    if not items:
        return await update.message.reply_text("Bekleyen istek yok.")

    if limit is not None:
        items = items[:limit]

    await update.message.reply_text(f"🟡 {len(items)} istek onaylanıyor…")
    done = 0
    for user_id, (chat_id, user) in items:
        # tekrar tetiklenirse yarış durumunu engelle
        if user_id not in pending_requests:
            continue
        pending_requests.pop(user_id, None)
        try:
            await _approve_one(context, chat_id, user)
            done += 1
        except Exception as e:
            logger.warning("approve error for %s: %s", user_id, e)

    await update.message.reply_text(f"✅ Tamamlandı. Onaylanan: {done}")


async def decline_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /declineall [n]  → bekleyenlerden n adede kadar reddet """
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Yetkisiz.")
        return

    limit: Optional[int] = None
    if context.args:
        try:
            limit = max(0, int(context.args[0]))
        except ValueError:
            return await update.message.reply_text("Kullanım: /declineall [adet]")

    items = list(pending_requests.items())
    if not items:
        return await update.message.reply_text("Bekleyen istek yok.")

    if limit is not None:
        items = items[:limit]

    await update.message.reply_text(f"🟡 {len(items)} istek reddediliyor…")
    done = 0
    for user_id, (chat_id, user) in items:
        if user_id not in pending_requests:
            continue
        pending_requests.pop(user_id, None)
        try:
            await _decline_one(context, chat_id, user.id, user.full_name)
            done += 1
        except Exception as e:
            logger.warning("decline error for %s: %s", user_id, e)

    await update.message.reply_text(f"✅ Tamamlandı. Reddedilen: {done}")


# -------------- Uygulama --------------
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["id", "kimim"], my_id))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("approveall", approve_all))
    app.add_handler(CommandHandler("declineall", decline_all))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info(
        "Bot başlıyor… (admins=%s, rate=%s/sn)", ",".join(map(str, ADMIN_IDS)), MAX_RATE_PER_SEC
    )
    app.run_polling()


if __name__ == "__main__":
    main()

"""
Telegram İstek Onaylayıcı (DM’den toplu onay)
- Kütüphane: python-telegram-bot==21.7
- Çalıştırma:
  BOT_TOKEN=xxxxx ADMIN_IDS=111,222 WELCOME_MESSAGE="..." python app.py
"""

import asyncio
import logging
import os
from typing import Dict, Set, List, Tuple, Optional

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, User
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, Forbidden
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ChatJoinRequestHandler,
    ContextTypes
)

# ---------- Ayarlar / Log ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS: Set[int] = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x}
WELCOME_MESSAGE = os.getenv(
    "WELCOME_MESSAGE",
    "{mention} hoş geldin! Grup kurallarını /kurallar komutuyla görebilirsin.",
)

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN env değişkeni zorunludur.")

# ---------- Çalışma durumları ----------
# Bot çalışırken gördüğü bekleyen istekler:
# pending[(chat_id)][user_id] = User
pending: Dict[int, Dict[int, User]] = {}

# Botun çalışırken istek aldığı sohbetleri (başlıkla) da tutalım:
known_chats: Dict[int, str] = {}

# Şu an seçim yapılmış sohbet (admin bazlı saklıyoruz)
selected_chat_by_admin: Dict[int, int] = {}

# ---------- Yardımcılar ----------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def rate_to_delay(rate_name: str) -> float:
    name = (rate_name or "").lower()
    if name in ("hizli", "fast"):
        return 0.045   # ~22/sn
    if name in ("orta", "medium"):
        return 0.08    # ~12/sn
    # varsayılan + yavaş
    return 0.2        # ~5/sn

def mention_html(u: User) -> str:
    return f"<a href='tg://user?id={u.id}'>{u.first_name}</a>"

# ---------- Komutlar (DM) ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Merhaba! İstek onay botu.\n"
        "/istek → gördüğüm kanallar\n"
        "/sec <numara> → kanalı seç\n"
        "/onayla all [hiz] → tümünü onayla\n"
        "/onayla <adet> [hiz] → belirtilen sayıda onayla\n"
        "Hızlar: yavas | orta | hizli"
    )

async def chatid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    text = (
        f"🆔 Chat ID: <code>{chat.id}</code>\n"
        f"📌 Başlık: {chat.title or '-'}\n"
        f"👥 Tür: {chat.type}"
    )
    await update.message.reply_html(text)

async def istek_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        return await update.message.reply_text("⛔ Yetkin yok.")

    if not known_chats:
        return await update.message.reply_text("Şu ana kadar istek aldığım bir sohbet görmedim.")

    lines = []
    for i, (cid, title) in enumerate(known_chats.items(), start=1):
        count = len(pending.get(cid, {}))
        lines.append(f"{i}. {title} ({cid}) – bekleyen: {count}")

    await update.message.reply_text(
        "Gördüğüm sohbetler (çalıştığım süre içinde):\n" + "\n".join(lines) +
        "\n\n/seç <numara> ile birini seç."
    )

# Türkçe alias: /sec ve /seç ikisini de tutalım
async def sec_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        return await update.message.reply_text("⛔ Yetkin yok.")

    if not context.args:
        return await update.message.reply_text("Kullanım: /sec <numara>")

    try:
        idx = int(context.args[0]) - 1
    except ValueError:
        return await update.message.reply_text("Numara bekleniyordu: /sec 1")

    items = list(known_chats.items())
    if not (0 <= idx < len(items)):
        return await update.message.reply_text("Geçersiz numara.")

    chat_id, title = items[idx]
    selected_chat_by_admin[user.id] = chat_id
    await update.message.reply_text(f"Seçildi: {title} ({chat_id}).\n"
                                    f"Bekleyen: {len(pending.get(chat_id, {}))}\n"
                                    f"`/onayla all hizli` veya `/onayla 500 orta` gibi.", parse_mode=ParseMode.MARKDOWN)

async def onayla_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        return await update.message.reply_text("⛔ Yetkin yok.")

    chat_id = selected_chat_by_admin.get(user.id)
    if not chat_id:
        return await update.message.reply_text("Önce bir sohbet seç: /istek → /sec <numara>")

    # Argümanlar: all [hiz]  |  <adet> [hiz]
    args = context.args or []
    if not args:
        return await update.message.reply_text("Kullanım: /onayla all [hiz]  veya  /onayla <adet> [hiz]")

    count: Optional[int] = None
    speed = "orta"

    if args[0].lower() == "all":
        count = None
        if len(args) >= 2:
            speed = args[1]
    else:
        try:
            count = int(args[0])
        except ValueError:
            return await update.message.reply_text("Sayı veya 'all' bekleniyordu.")
        if len(args) >= 2:
            speed = args[1]

    delay = rate_to_delay(speed)
    users_map = pending.get(chat_id, {})
    if not users_map:
        return await update.message.reply_text("Bekleyen istek yok (bot çalışırken hiç gelmemiş olabilir).")

    to_process: List[Tuple[int, User]] = list(users_map.items())
    if count is not None:
        to_process = to_process[: max(0, count)]

    approved = 0
    msg = await update.message.reply_text(f"Onay başlıyor… hedef: {len(to_process)} | hız: {speed}")

    for uid, u in to_process:
        try:
            await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=uid)
            # Hoş geldin mesajı
            try:
                welcome = WELCOME_MESSAGE.format(mention=mention_html(u))
                await context.bot.send_message(chat_id=chat_id, text=welcome, parse_mode=ParseMode.HTML)
            except Forbidden:
                pass
            approved += 1
            users_map.pop(uid, None)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        except Exception as e:
            logger.warning("Onay hatası: %s", e)
        # hız kontrolü
        await asyncio.sleep(delay)

        # ara durum güncellemesi (seyrek)
        if approved % 200 == 0:
            try:
                await msg.edit_text(f"Onaylanıyor… {approved}/{len(to_process)}")
            except Exception:
                pass

    await msg.edit_text(f"✅ Bitti. Onaylanan: {approved}")

# ---------- Join request yakalayıcı ----------
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    req = update.chat_join_request
    user: User = req.from_user
    chat = req.chat

    # listelerde tut
    known_chats.setdefault(chat.id, chat.title or f"{chat.type}:{chat.id}")
    pending.setdefault(chat.id, {})[user.id] = user

    # Adminlere DM ile haber ver (isteğe bağlı, burada logluyoruz)
    logger.info("JoinRequest: chat=%s user=%s", chat.id, user.id)

# ---------- Callback (kullanılmıyor ama ileride butonlar için) ----------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()

# ---------- main ----------
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    # DM komutları
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chatid_cmd))
    app.add_handler(CommandHandler("istek", istek_cmd))
    app.add_handler(CommandHandler(["sec", "seç"], sec_cmd))
    app.add_handler(CommandHandler("onayla", onayla_cmd))

    # Join request
    app.add_handler(ChatJoinRequestHandler(on_join_request))

    # (İleride buton kullanırsak)
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Bot başlıyor…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    try:
        asyncio.run(asyncio.sleep(0))
    except RuntimeError:
        pass
    main()

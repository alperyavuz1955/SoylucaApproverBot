"""
Telegram İstek Onaylayıcı Bot (PTB 21.7) — DM'den yönetim

Özellikler:
- Katılma isteği geldiğinde adminlere özelden buton gönderir (✅/❌).
- DM'den toplu onay/ret komutları: /approveall, /declineall
- Eski bekleyenleri DM’den /syncrequests ile içeri alabilirsin (opsiyonel).
- Rate limit'e saygı (RetryAfter yakalanır), otomatik bekler ve devam eder.

ENV:
  BOT_TOKEN        -> Telegram bot token (zorunlu)
  ADMIN_IDS        -> Virgülle ayrılmış admin user_id listesi. Örn: "111,222"
  WELCOME_MESSAGE  -> (opsiyonel) Tek tek onaydan sonra gruba atılır. {mention} destekler.
  MAX_RATE_PER_SEC -> (opsiyonel) saniyede en fazla işlem (vars: 10)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, Tuple, List, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import RetryAfter, Forbidden, BadRequest, TimedOut, NetworkError
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
MAX_RATE_PER_SEC = float(os.getenv("MAX_RATE_PER_SEC", "10"))

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN env değişkeni zorunludur.")

# Bekleyen istek havuzu: user_id -> (chat_id, user)
pending_requests: Dict[int, Tuple[int, "telegram.User"]] = {}

# Basit oran sınırlama
_last_ops: List[float] = []
def _record_op():
    now = time.time()
    _last_ops.append(now)
    while _last_ops and now - _last_ops[0] > 1.0:
        _last_ops.pop(0)

async def _rate_limit():
    while True:
        now = time.time()
        _last_ops[:] = [t for t in _last_ops if now - t <= 1.0]
        if len(_last_ops) < MAX_RATE_PER_SEC:
            _record_op()
            return
        await asyncio.sleep(0.02)

def _is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def _resolve_chat_id(update: Update, args: List[str]) -> Optional[int]:
    """DM'de çalışıyorsan args'tan, grupta çalışıyorsan otomatik chat_id alır."""
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup", "channel"):
        return chat.id
    # DM ise argümandan bekle
    for tok in reversed(args):
        try:
            return int(tok)
        except ValueError:
            continue
    return None

# ---------- Yardımcı işlemler ----------
async def safe_approve(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    retries = 0
    while True:
        try:
            await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
            return True
        except RetryAfter as e:
            await asyncio.sleep(int(getattr(e, "retry_after", 3)) or 3)
        except (TimedOut, NetworkError):
            await asyncio.sleep(2)
        except Forbidden:
            logger.error("Forbidden: Botun yetkisi yok (Üyelik isteklerini yönet).")
            return False
        except BadRequest as e:
            logger.warning("BadRequest approve: %s", e)
            return False
        except Exception as e:
            logger.exception("approve err: %s", e)
            return False
        retries += 1
        if retries > 8:
            return False

async def safe_decline(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
        return True
    except RetryAfter as e:
        await asyncio.sleep(int(getattr(e, "retry_after", 3)) or 3)
        try:
            await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
            return True
        except Exception:
            return False
    except Forbidden:
        return False
    except Exception:
        return False

# ---------- Temel komutlar ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Merhaba! Ben onay botuyum.\n"
        "• /id → kendi user_id'in\n"
        "• /status <chat_id> → bekleyen sayısı\n"
        "• /syncrequests <chat_id> → bekleyenleri içeri yükle\n"
        "• /approveall [adet] <chat_id> → toplu onay\n"
        "• /declineall [adet] <chat_id> → toplu ret\n"
        "(Tüm toplu komutları DM'den verebilirsin.)"
    )

async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    await update.message.reply_html(
        f"🆔 <b>{u.id}</b>\n👤 {u.full_name}\n@{u.username or '-'}"
    )

# ---------- Join request geldiğinde (gerçek zamanlı) ----------
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    req = update.chat_join_request
    user = req.from_user
    chat = req.chat

    pending_requests[user.id] = (chat.id, user)

    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Onayla", callback_data=f"approve:{chat.id}:{user.id}"),
            InlineKeyboardButton("❌ Reddet",  callback_data=f"decline:{chat.id}:{user.id}"),
        ]]
    )
    text = (
        f"📩 Yeni istek: <a href='tg://user?id={user.id}'>{user.full_name}</a> "
        f"(@{user.username or '-'} / <code>{user.id}</code>)\n"
        f"Chat: <code>{chat.title}</code> (<code>{chat.id}</code>)"
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning("Admin DM hatası: %s", e)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _is_admin(update.effective_user.id):
        return await q.edit_message_text("⛔ Yetkisiz.")
    try:
        action, chat_id_s, user_id_s = q.data.split(":")
        chat_id = int(chat_id_s); user_id = int(user_id_s)
    except ValueError:
        return await q.edit_message_text("Hatalı veri.")
    tup = pending_requests.pop(user_id, None)
    if tup and tup[0] != chat_id:
        return await q.edit_message_text("Veri uyuşmuyor.")
    if action == "approve":
        await _rate_limit()
        ok = await safe_approve(context, chat_id, user_id)
        if ok and tup:
            mention = f"<a href='tg://user?id={user_id}'>{tup[1].first_name}</a>"
            msg = WELCOME_MESSAGE.format(mention=mention)
            try:
                await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML)
            except Exception:
                pass
        await q.edit_message_text("✅ Onaylandı." if ok else "⚠️ Onay hatası.")
    else:
        await _rate_limit()
        ok = await safe_decline(context, chat_id, user_id)
        await q.edit_message_text("❌ Reddedildi." if ok else "⚠️ Ret hatası.")

# ---------- DM’den toplu senkron & sayım ----------
async def syncrequests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ Yetkisiz.")
    chat_id = _resolve_chat_id(update, context.args)
    if chat_id is None:
        return await update.message.reply_text("Kullanım: /syncrequests <chat_id>")
    try:
        reqs = await context.bot.get_chat_join_requests(chat_id=chat_id, limit=200)
    except Exception as e:
        return await update.message.reply_text(f"Hata: {e}")
    added = 0
    for r in reqs:
        if r.user.id not in pending_requests:
            pending_requests[r.user.id] = (chat_id, r.user)
            added += 1
    await update.message.reply_text(f"🔄 Eklendi: {added} | Toplam bekleyen (bellek): {len(pending_requests)}")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _resolve_chat_id(update, context.args)
    if chat_id is None:
        return await update.message.reply_text("Kullanım: /status <chat_id>")
    try:
        reqs = await context.bot.get_chat_join_requests(chat_id=chat_id, limit=1)
        # Bot API toplam sayıyı direkt döndürmüyor; kaba tahmin: ilk sayfayı saymak istersen burada 200'e kadar çekip len() alınabilir.
        approx = len(reqs)
        await update.message.reply_text(f"⚙️ İlk sayfada bekleyen ~{approx} (toplam daha fazla olabilir).")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

# ---------- DM’den toplu onay/ret (doğrudan API ile) ----------
async def _bulk_core(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     chat_id: int, limit: Optional[int], approve: bool) -> None:
    done = 0
    page = 0
    await update.message.reply_text(
        f"{'Onay' if approve else 'Ret'} başlıyor… "
        f"{'Limit: ' + str(limit) if limit else 'Limit yok (tümü)'}"
    )
    while True:
        try:
            reqs = await context.bot.get_chat_join_requests(chat_id=chat_id, limit=200)
        except RetryAfter as e:
            await asyncio.sleep(int(getattr(e, "retry_after", 3)) or 3)
            continue
        except Forbidden:
            return await update.message.reply_text("Botun yetkisi yok (Üyelik isteklerini yönet).")
        except Exception as e:
            logger.exception("get_chat_join_requests: %s", e)
            return await update.message.reply_text("İstek listesi alınamadı.")

        if not reqs:
            break

        for r in reqs:
            if limit and done >= limit:
                break
            await _rate_limit()
            ok = await (safe_approve(context, chat_id, r.user.id) if approve
                        else safe_decline(context, chat_id, r.user.id))
            if ok:
                done += 1

        if limit and done >= limit:
            break

        page += 1
        await asyncio.sleep(0.5)

    await update.message.reply_text(f"✅ Bitti. {'Onaylanan' if approve else 'Reddedilen'}: {done}")

async def approveall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ Yetkisiz.")
    # /approveall [adet] <chat_id>
    args = context.args[:]
    count = None
    if args and args[0].isdigit():
        count = int(args.pop(0))
        if count <= 0:
            return await update.message.reply_text("Pozitif bir sayı veriniz.")
    chat_id = _resolve_chat_id(update, args)
    if chat_id is None:
        return await update.message.reply_text("Kullanım: /approveall [adet] <chat_id>")
    await _bulk_core(update, context, chat_id, limit=count, approve=True)

async def declineall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ Yetkisiz.")
    # /declineall [adet] <chat_id>
    args = context.args[:]
    count = None
    if args and args[0].isdigit():
        count = int(args.pop(0))
        if count <= 0:
            return await update.message.reply_text("Pozitif bir sayı veriniz.")
    chat_id = _resolve_chat_id(update, args)
    if chat_id is None:
        return await update.message.reply_text("Kullanım: /declineall [adet] <chat_id>")
    await _bulk_core(update, context, chat_id, limit=count, approve=False)

# ---------- App ----------
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    # Temel
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["id", "kimim"], my_id))

    # DM yönetim komutları
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("syncrequests", syncrequests))
    app.add_handler(CommandHandler("approveall", approveall))
    app.add_handler(CommandHandler("declineall", declineall))

    # Gerçek zamanlı istek + buton
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Bot başlıyor… admins=%s rate=%s/s", ",".join(map(str, ADMIN_IDS)), MAX_RATE_PER_SEC)
    app.run_polling()

if __name__ == "__main__":
    main()

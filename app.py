"""
Telegram İstek Onaylayıcı Bot (Butonlu Onay + Toplu Onay)

Gereken paket: python-telegram-bot==21.6

ENV değişkenleri:
- BOT_TOKEN         : Telegram bot token
- ADMIN_IDS         : Virgülle ayrılmış admin user_id listesi. Örn: "111,222"
- WELCOME_MESSAGE   : (opsiyonel) Onay sonrası gruba atılacak mesaj (tekil onayda)
- BULK_RPS          : (ops., varsayılan 18) Toplu onayda hedef istek/saniye
- BULK_CONCURRENCY  : (ops., varsayılan 25) Toplu onay eşzamanlı işçi sayısı

Komutlar:
- /start                  : Selam mesajı
- /id                     : Kullanıcının id bilgisini gösterir
- /approve_all            : Grupta tüm bekleyenleri onaylar (özelden: /approve_all <chat_id>)
- /approve <adet>         : Grupta belirtilen sayıda onaylar (özelden: /approve <adet> <chat_id>)
"""

import asyncio
import logging
import os
from time import monotonic
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ChatJoinRequestHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from telegram.error import RetryAfter, Forbidden, BadRequest, TimedOut, NetworkError


# -------------------- Ayarlar --------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN env değişkeni zorunludur.")

ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
WELCOME_MESSAGE = os.getenv(
    "WELCOME_MESSAGE",
    "{mention} hoş geldin! Grup kurallarını /kurallar komutuyla görebilirsin.",
)

BULK_RPS = float(os.getenv("BULK_RPS", "18"))
BULK_CONCURRENCY = int(os.getenv("BULK_CONCURRENCY", "25"))

# Bekleyen istekler: {user_id: (chat_id, user_obj)}
pending_requests: dict[int, tuple[int, "telegram.User"]] = {}


# -------------------- Yardımcılar --------------------
class RateLimiter:
    """Basit zaman aralıklı oran sınırlayıcı (rps ~ requests per second)."""
    def __init__(self, rps: float):
        self.min_interval = 1.0 / max(1.0, rps)
        self._next = monotonic()

    async def wait(self):
        now = monotonic()
        if now < self._next:
            await asyncio.sleep(self._next - now)
        self._next = max(self._next + self.min_interval, monotonic())


async def safe_approve(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """
    Bir üyelik isteğini güvenli şekilde onaylar.
    RetryAfter, geçici ağ hataları vb. durumlarda otomatik tekrar dener.
    """
    retries = 0
    while True:
        try:
            await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
            return True

        except RetryAfter as e:
            # Telegram rate limit — bekle ve tekrar dene
            wait = int(getattr(e, "retry_after", 3)) or 3
            await asyncio.sleep(wait)
            retries += 1
            if retries > 8:
                logger.warning("RetryAfter çok fazla (user_id=%s).", user_id)
                return False

        except (TimedOut, NetworkError):
            # Ağ/timeout — kısa bekle ve dene
            await asyncio.sleep(2)
            retries += 1
            if retries > 6:
                return False

        except Forbidden:
            # Yetki yoksa boşuna deneme
            logger.error("Forbidden: Botun yetkisi yok (Üyelik isteklerini yönet).")
            return False

        except BadRequest as e:
            # Geçersiz istek vs. — tekrar denemeyelim
            logger.warning("BadRequest onayda (user_id=%s): %s", user_id, e)
            return False

        except Exception as e:
            logger.exception("Bilinmeyen hata onayda (user_id=%s): %s", user_id, e)
            return False


def _resolve_chat_id(update: Update, args: list[str]) -> Optional[int]:
    """Grupta çalışıyorsa chat_id otomatik; özelden ise argümandan beklenir."""
    if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
        return update.effective_chat.id
    if args:
        try:
            return int(args[-1])
        except ValueError:
            return None
    return None


# -------------------- Komutlar (temel) --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Merhaba! Ben butonlu onay botuyum.\n"
        "• /id ile user id’ni öğrenebilirsin.\n"
        "• /approve_all veya /approve <adet> ile toplu onay yapabilirsin."
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = (
        f"🆔 User ID: <code>{user.id}</code>\n"
        f"👤 Ad: {user.full_name}\n"
        f"@ Kullanıcı adı: @{user.username or '-'}"
    )
    await update.message.reply_html(text)


# -------------------- Katılma isteği + butonlu akış --------------------
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
        ok = await safe_approve(context, chat_id, user_id)
        if ok:
            mention = f"<a href='tg://user?id={user.id}'>{user.first_name}</a>"
            welcome = WELCOME_MESSAGE.format(mention=mention)
            try:
                await context.bot.send_message(chat_id=chat_id, text=welcome, parse_mode=ParseMode.HTML)
            except Exception:
                pass
            await query.edit_message_text(f"✅ {user.full_name} onaylandı.")
        else:
            await query.edit_message_text("Onaylanamadı (loglara bakınız).")

    elif action == "decline":
        try:
            await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
            await query.edit_message_text(f"❌ {user.full_name} reddedildi.")
        except Exception as e:
            await query.edit_message_text(f"Reddetme hatası: {e!s}")


# -------------------- Toplu onay çekirdeği --------------------
async def _approve_worker(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                          queue: asyncio.Queue, limiter: RateLimiter,
                          counter: dict):
    while True:
        user_id = await queue.get()
        if user_id is None:
            queue.task_done()
            break
        await limiter.wait()
        ok = await safe_approve(context, chat_id, user_id)
        if ok:
            counter["ok"] += 1
        queue.task_done()


async def bulk_approve_core(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            chat_id: int, limit: Optional[int]) -> None:
    """
    limit=None -> tüm bekleyenler
    limit= sayı -> o kadarını onayla
    """
    page_size = 200
    limiter = RateLimiter(BULK_RPS)
    approved_total = 0

    await update.message.reply_text(
        f"Toplu onay başlıyor… hedef ≈ {BULK_RPS}/sn, işçi={BULK_CONCURRENCY}. "
        f"{'Limit: ' + str(limit) if limit else 'Limit yok (tümü).'}"
    )

    while True:
        try:
            reqs = await context.bot.get_chat_join_requests(chat_id=chat_id, limit=page_size)
        except RetryAfter as e:
            await asyncio.sleep(int(getattr(e, "retry_after", 3)) or 3)
            continue
        except Forbidden:
            await update.message.reply_text("Botun yetkisi yok (Üyelik isteklerini yönet).")
            return
        except Exception as e:
            logger.exception("get_chat_join_requests hatası: %s", e)
            await update.message.reply_text("İstek listesi alınamadı.")
            return

        if not reqs:
            break

        queue: asyncio.Queue[int] = asyncio.Queue()
        for r in reqs:
            if limit and approved_total >= limit:
                break
            queue.put_nowait(r.user.id)

        if queue.qsize() == 0:
            break

        counter = {"ok": 0}
        workers = [
            asyncio.create_task(_approve_worker(context, chat_id, queue, limiter, counter))
            for _ in range(BULK_CONCURRENCY)
        ]

        await queue.join()
        for _ in workers:
            queue.put_nowait(None)
        await asyncio.gather(*workers, return_exceptions=True)

        approved_total += counter["ok"]

        if limit and approved_total >= limit:
            break

        await asyncio.sleep(0.5)  # sayfalar arası minik ara

    await update.message.reply_text(f"✅ Bitti. Onaylanan: {approved_total}")


# -------------------- Komutlar (toplu) --------------------
async def cmd_approve_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Bu komutu yalnızca adminler kullanabilir.")
        return
    chat_id = _resolve_chat_id(update, context.args)
    if chat_id is None:
        await update.message.reply_text("Kullanım: grupta /approve_all veya özelden /approve_all <chat_id>")
        return
    await bulk_approve_core(update, context, chat_id, limit=None)


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Bu komutu yalnızca adminler kullanabilir.")
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /approve <adet>  (grupta)  veya  /approve <adet> <chat_id> (özel)")
        return
    try:
        count = int(context.args[0])
        if count <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Lütfen pozitif bir sayı verin: /approve 500")
        return
    chat_id = _resolve_chat_id(update, context.args[1:])
    if chat_id is None:
        await update.message.reply_text("Chat bulunamadı. Grupta çalıştırın ya da chat_id verin.")
        return
    await bulk_approve_core(update, context, chat_id, limit=count)


# -------------------- Uygulama --------------------
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    # Temel komutlar
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["id", "kimim"], my_id))

    # Katılma isteği + buton
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Toplu onay komutları
    app.add_handler(CommandHandler("approve_all", cmd_approve_all))
    app.add_handler(CommandHandler("approve", cmd_approve))

    logger.info("Bot başlıyor…")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    # Render'ın bazı ortamlarda event loop hatasını atmaması için küçük hack
    try:
        asyncio.run(asyncio.sleep(0))
    except RuntimeError:
        pass
    main()

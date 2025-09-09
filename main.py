# Requirements (requirements.txt)
# python-telegram-bot==21.6
# asyncpg==0.29.0
# httpx==0.27.2
# python-dotenv==1.0.1  # optional

import os, logging, threading, asyncio, re
from dataclasses import dataclass, field
from typing import List, Optional, Dict
import asyncpg
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, TypeHandler
)
from telegram.request import HTTPXRequest

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("broadcast-bot")

# ---------------- Env ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
DATABASE_URL = os.getenv("DATABASE_URL", "")
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN kosong")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL kosong")
if not OWNER_ID:
    raise RuntimeError("OWNER_ID kosong")

# ---------------- DB Layer ----------------
async def init_db(pool):
    async with pool.acquire() as con:
        await con.execute(
            """CREATE TABLE IF NOT EXISTS users(
                user_id BIGINT PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                last_seen TIMESTAMPTZ NOT NULL
            )"""
        )
        await con.execute(
            """CREATE TABLE IF NOT EXISTS admins(
                user_id BIGINT PRIMARY KEY
            )"""
        )
        await con.execute("INSERT INTO admins(user_id) VALUES($1) ON CONFLICT DO NOTHING", OWNER_ID)

async def upsert_user(pool, uid, first_name, username):
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO users(user_id, first_name, username, last_seen)
                   VALUES($1,$2,$3,NOW())
                   ON CONFLICT (user_id) DO UPDATE SET
                     first_name=EXCLUDED.first_name,
                     username=EXCLUDED.username,
                     last_seen=NOW()""",
            uid, first_name, username,
        )

async def is_admin(pool, uid:int) -> bool:
    if uid == OWNER_ID:
        return True
    async with pool.acquire() as con:
        return await con.fetchval("SELECT 1 FROM admins WHERE user_id=$1", uid) is not None

async def add_admin(pool, uid:int) -> bool:
    try:
        async with pool.acquire() as con:
            await con.execute("INSERT INTO admins(user_id) VALUES($1)", uid)
        return True
    except Exception as e:
        log.warning("add_admin fail %s: %s", uid, e)
        return False

async def del_admin(pool, uid:int) -> bool:
    async with pool.acquire() as con:
        res = await con.execute("DELETE FROM admins WHERE user_id=$1", uid)
        return res.endswith("1")

async def get_admins(pool) -> List[int]:
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT user_id FROM admins ORDER BY user_id")
        ids = [r[0] for r in rows]
        if OWNER_ID not in ids:
            ids.insert(0, OWNER_ID)
        return ids

async def count_users(pool) -> int:
    async with pool.acquire() as con:
        return int(await con.fetchval("SELECT COUNT(*) FROM users"))

async def get_all_user_ids(pool) -> List[int]:
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT user_id FROM users")
        return [r[0] for r in rows]

# ---------------- State ----------------
class Step:
    ASK_TEXT = "ASK_TEXT"
    ASK_MEDIA = "ASK_MEDIA"
    ASK_ADD_BUTTON = "ASK_ADD_BUTTON"
    ASK_BUTTON_TEXT = "ASK_BUTTON_TEXT"
    ASK_BUTTON_URL = "ASK_BUTTON_URL"
    PREVIEW = "PREVIEW"
    IDLE = "IDLE"

@dataclass
class ButtonDef:
    text: str
    url: str

@dataclass
class BroadcastDraft:
    text: str = ""
    photo_file_id: Optional[str] = None
    video_file_id: Optional[str] = None
    animation_file_id: Optional[str] = None
    buttons: List[ButtonDef] = field(default_factory=list)

@dataclass
class Session:
    step: str = Step.IDLE
    draft: BroadcastDraft = field(default_factory=BroadcastDraft)
    temp_button_text: Optional[str] = None

sessions: Dict[int, Session] = {}

def ensure_session(uid:int) -> Session:
    if uid not in sessions:
        sessions[uid] = Session()
    return sessions[uid]

def yesno_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Ya", callback_data="btn_yes"), InlineKeyboardButton("Tidak", callback_data="btn_no")]
    ])

# ---------------- Health ----------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        else:
            self.send_response(404); self.end_headers()

def start_health_server():
    srv = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    log.info("Health server :%s", PORT)
    srv.serve_forever()

# ---------------- Debug & Tracking ----------------
async def debug_all(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.message:
        log.info("UPDATE message chat=%s text=%r", update.message.chat_id, update.message.text)
    elif update.callback_query:
        step = sessions.get(update.callback_query.from_user.id, Session()).step
        log.info("UPDATE callback from=%s data=%r (step=%s)", update.callback_query.from_user.id, update.callback_query.data, step)
    elif update.my_chat_member:
        log.info("UPDATE my_chat_member chat=%s status=%s", update.my_chat_member.chat.id, update.my_chat_member.new_chat_member.status)
    else:
        log.info("UPDATE other: %s", update.to_dict())

async def track(update:Update, context:ContextTypes.DEFAULT_TYPE):
    pool = context.application.bot_data.get("pool")
    u = update.effective_user
    if pool and u:
        try:
            await upsert_user(pool, u.id, u.first_name, u.username)
        except Exception as e:
            log.warning("track fail: %s", e)

# ---------------- Commands ----------------
async def start_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Selamat datang di Nagabola! Temukan informasi dan promo menarik di bot Nagabola."
    )

async def ping_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def id_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; c = update.effective_chat
    await update.message.reply_text(f"user_id: {u.id}\nchat_id: {c.id}")

async def stats_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    pool = context.application.bot_data["pool"]
    total = await count_users(pool)
    await update.message.reply_text(f"👥 Total pengguna terdaftar: {total}")

async def admins_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    pool = context.application.bot_data["pool"]
    uid = update.effective_user.id
    if not await is_admin(pool, uid):
        return await update.message.reply_text("Khusus admin.")
    ids = await get_admins(pool)
    await update.message.reply_text(
        "Daftar admin:\n" + "\n".join(f"- {i}" + (" (OWNER)" if i == OWNER_ID else "") for i in ids)
    )

# -------- Admin helpers --------
_def_num = re.compile(r"(-?\d{5,20})")

def _parse_first_int(text:str) -> Optional[int]:
    if not text:
        return None
    m = _def_num.search(text)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None

def extract_target_id(update:Update, context:ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    # reply target
    if update.message and update.message.reply_to_message and update.message.reply_to_message.from_user:
        return update.message.reply_to_message.from_user.id
    # args numeric
    if context.args:
        tid = _parse_first_int(" ".join(context.args))
        if tid:
            return tid
    # fallback: text
    if update.message and update.message.text:
        tid = _parse_first_int(update.message.text)
        if tid:
            return tid
    return None

async def _ensure_owner(update:Update) -> bool:
    uid = update.effective_user.id
    if uid != OWNER_ID:
        await update.message.reply_text("Hanya OWNER.")
        return False
    return True

# -------- Admin commands --------
async def admin_add_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    log.info("admin_add invoked by %s text=%r args=%r", update.effective_user.id, update.message.text, context.args)
    if not await _ensure_owner(update):
        return
    pool = context.application.bot_data["pool"]
    target_id = extract_target_id(update, context)
    if not target_id:
        return await update.message.reply_text("Gunakan /admin_add <user_id> atau reply pesan user.")
    ok = await add_admin(pool, target_id)
    await update.message.reply_text(("Berhasil" if ok else "Gagal/duplikat") + f" menambah admin: {target_id}")

async def admin_del_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    log.info("admin_del invoked by %s text=%r args=%r", update.effective_user.id, update.message.text, context.args)
    if not await _ensure_owner(update):
        return
    pool = context.application.bot_data["pool"]
    target_id = extract_target_id(update, context)
    if not target_id:
        return await update.message.reply_text("Gunakan /admin_del <user_id> atau reply pesan user.")
    if target_id == OWNER_ID:
        return await update.message.reply_text("Tidak dapat menghapus OWNER.")
    ok = await del_admin(pool, target_id)
    await update.message.reply_text(("Admin dihapus" if ok else "User bukan admin / gagal menghapus") + f": {target_id}")

async def admin_regex_router(update:Update, context:ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").lower()
    if "admin_add" in txt:
        return await admin_add_cmd(update, context)
    if "admin_del" in txt:
        return await admin_del_cmd(update, context)

# ---------------- Broadcast helpers ----------------
async def send_preview_to_chat(context:ContextTypes.DEFAULT_TYPE, chat_id:int, draft:BroadcastDraft):
    rows = [[InlineKeyboardButton(b.text, url=b.url)] for b in draft.buttons]
    kb = InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])
    caption = draft.text or ""
    if draft.photo_file_id:
        await context.bot.send_photo(chat_id, draft.photo_file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
    elif draft.video_file_id:
        await context.bot.send_video(chat_id, draft.video_file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
    elif draft.animation_file_id:
        await context.bot.send_animation(chat_id, draft.animation_file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await context.bot.send_message(chat_id, caption, parse_mode=ParseMode.HTML, reply_markup=kb)
    await context.bot.send_message(
        chat_id,
        "Preview di atas. Lanjutkan?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Kirim", callback_data="preview_send")],
            [InlineKeyboardButton("🔁 Ulangi", callback_data="preview_restart")],
            [InlineKeyboardButton("❌ Batal", callback_data="preview_cancel")],
        ]),
    )

# ---------------- Broadcast flow ----------------
async def broadcast_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    pool = context.application.bot_data["pool"]
    uid = update.effective_user.id
    log.info("Handling /broadcast from %s", uid)
    if not await is_admin(pool, uid):
        return await update.message.reply_text("Maaf, fitur ini hanya untuk admin.")
    s = ensure_session(uid)
    s.step = Step.ASK_TEXT
    s.draft = BroadcastDraft()
    await update.message.reply_text("Kirimkan *teks* untuk broadcast.", parse_mode=ParseMode.MARKDOWN)

async def handle_message(update:Update, context:ContextTypes.DEFAULT_TYPE):
    pool = context.application.bot_data["pool"]
    uid = update.effective_user.id
    s = ensure_session(uid)
    # hanya proses jika admin dan berada pada flow (bukan IDLE)
    if (not await is_admin(pool, uid)) or s.step == Step.IDLE:
        return

    msg = update.effective_message

    if s.step == Step.ASK_TEXT:
        if not msg.text or msg.text.strip().lower() == "/broadcast":
            return await msg.reply_text("Silakan kirim teks isi broadcast.")
        s.draft.text = msg.text_html
        s.step = Step.ASK_MEDIA
        return await msg.reply_text("Kirimkan *foto/GIF/video* (opsional) atau ketik *skip*.", parse_mode=ParseMode.MARKDOWN)

    if s.step == Step.ASK_MEDIA:
        if msg.text and msg.text.strip().lower() == "skip":
            s.step = Step.ASK_ADD_BUTTON
            return await msg.reply_text("Tambah *button*?", reply_markup=yesno_keyboard(), parse_mode=ParseMode.MARKDOWN)
        if msg.photo:
            s.draft.photo_file_id = msg.photo[-1].file_id
            s.draft.video_file_id = None
            s.draft.animation_file_id = None
        elif msg.video:
            s.draft.video_file_id = msg.video.file_id
            s.draft.photo_file_id = None
            s.draft.animation_file_id = None
        elif msg.animation:
            s.draft.animation_file_id = msg.animation.file_id
            s.draft.photo_file_id = None
            s.draft.video_file_id = None
        else:
            return await msg.reply_text("Format tidak dikenali. Kirim foto/GIF/video atau *skip*.", parse_mode=ParseMode.MARKDOWN)
        s.step = Step.ASK_ADD_BUTTON
        return await msg.reply_text("Tambah *button*?", reply_markup=yesno_keyboard(), parse_mode=ParseMode.MARKDOWN)

    if s.step == Step.ASK_ADD_BUTTON:
        if msg.text:
            txt = msg.text.strip().lower()
            if txt in ("ya", "yes", "y"):
                s.step = Step.ASK_BUTTON_TEXT
                return await msg.reply_text("Kirim *teks button* (contoh: Kunjungi Situs)", parse_mode=ParseMode.MARKDOWN)
            if txt in ("tidak", "no", "n"):
                s.step = Step.PREVIEW
                return await send_preview_to_chat(context, update.effective_chat.id, s.draft)
        # jika bukan teks, tunggu interaksi callback button

    if s.step == Step.ASK_BUTTON_TEXT:
        if not msg.text:
            return await msg.reply_text("Kirim teks button (misal: Kunjungi Situs).")
        s.temp_button_text = msg.text.strip()
        s.step = Step.ASK_BUTTON_URL
        return await msg.reply_text("Kirim URL button (harus diawali http/https).")

    if s.step == Step.ASK_BUTTON_URL:
        if not msg.text or not msg.text.strip().lower().startswith(("http://", "https://")):
            return await msg.reply_text("URL tidak valid. Contoh: https://example.com")
        s.draft.buttons.append(ButtonDef(text=s.temp_button_text, url=msg.text.strip()))
        s.temp_button_text = None
        s.step = Step.ASK_ADD_BUTTON
        return await msg.reply_text("Tambah button lagi?", reply_markup=yesno_keyboard())

async def cb_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    s = ensure_session(uid)
    data = query.data
    log.info("Callback data=%s step=%s uid=%s", data, s.step, uid)

    if s.step == Step.ASK_ADD_BUTTON:
        if data == "btn_yes":
            s.step = Step.ASK_BUTTON_TEXT
            return await query.edit_message_text("Kirim *teks button* (contoh: Kunjungi Situs)", parse_mode=ParseMode.MARKDOWN)
        elif data == "btn_no":
            s.step = Step.PREVIEW
            return await send_preview_to_chat(context, query.message.chat_id, s.draft)

    if s.step == Step.PREVIEW:
        if data == "preview_send":
            await query.edit_message_text("Mulai broadcast…")
            await do_broadcast(context, s.draft, query)
            s.step = Step.IDLE
            s.draft = BroadcastDraft()
            return
        elif data == "preview_restart":
            s.step = Step.ASK_TEXT
            s.draft = BroadcastDraft()
            return await query.edit_message_text("Ulangi. Kirim *teks* untuk broadcast.", parse_mode=ParseMode.MARKDOWN)
        elif data == "preview_cancel":
            s.step = Step.IDLE
            s.draft = BroadcastDraft()
            return await query.edit_message_text("Broadcast dibatalkan.")

async def do_broadcast(context:ContextTypes.DEFAULT_TYPE, draft:BroadcastDraft, query):
    pool = context.application.bot_data["pool"]
    targets = await get_all_user_ids(pool)
    sent = 0
    failed = 0
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(b.text, url=b.url)] for b in draft.buttons]) if draft.buttons else InlineKeyboardMarkup([])

    for chat_id in targets:
        try:
            if draft.photo_file_id:
                await context.bot.send_photo(chat_id, draft.photo_file_id, caption=draft.text, parse_mode=ParseMode.HTML, reply_markup=kb)
            elif draft.video_file_id:
                await context.bot.send_video(chat_id, draft.video_file_id, caption=draft.text, parse_mode=ParseMode.HTML, reply_markup=kb)
            elif draft.animation_file_id:
                await context.bot.send_animation(chat_id, draft.animation_file_id, caption=draft.text, parse_mode=ParseMode.HTML, reply_markup=kb)
            else:
                await context.bot.send_message(chat_id, draft.text, parse_mode=ParseMode.HTML, reply_markup=kb)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            failed += 1
            log.warning("Broadcast fail %s: %s", chat_id, e)
            await asyncio.sleep(0.3)

    await query.message.reply_text(f"Selesai. Terkirim: {sent}, Gagal: {failed}")

# ---------------- Lifecycle ----------------
async def post_init(app):
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    await init_db(pool)
    app.bot_data["pool"] = pool
    # reset webhook dan JANGAN drop update
    await app.bot.delete_webhook(drop_pending_updates=False)
    me = await app.bot.get_me()
    log.info("Authorized as @%s (%s)", me.username, me.id)

async def post_shutdown(app):
    pool = app.bot_data.get("pool")
    if pool:
        await pool.close()
        log.info("DB pool closed.")

# ---------------- Build App ----------------
def build_app():
    req = HTTPXRequest(connect_timeout=15.0, read_timeout=45.0, write_timeout=15.0, pool_timeout=15.0)
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(req)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Order: debug (-2), track (-1), commands (0), callbacks (0), message flow (1)
    app.add_handler(TypeHandler(Update, debug_all, block=False), group=-2)
    app.add_handler(TypeHandler(Update, track, block=False), group=-1)

    # Commands
    app.add_handler(CommandHandler("start", start_cmd), group=0)
    app.add_handler(CommandHandler("ping", ping_cmd), group=0)
    app.add_handler(CommandHandler("id", id_cmd), group=0)
    app.add_handler(CommandHandler("stats", stats_cmd), group=0)
    app.add_handler(CommandHandler("admins", admins_cmd), group=0)

    # Admin commands + regex fallback (/admin_add@bot, /admin_del@bot)
    app.add_handler(CommandHandler("admin_add", admin_add_cmd), group=0)
    app.add_handler(CommandHandler("admin_del", admin_del_cmd), group=0)
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^/(admin_add|admin_del)(@[A-Za-z0-9_]+)?(\s|$)"), admin_regex_router), group=0)

    # Broadcast
    app.add_handler(CommandHandler("broadcast", broadcast_cmd), group=0)
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^/broadcast(@[A-Za-z0-9_]+)?(\s|$)"), broadcast_cmd), group=0)

    # Callback + flow
    app.add_handler(CallbackQueryHandler(cb_handler), group=0)
    app.add_handler(MessageHandler(filters.ALL, handle_message), group=1)

    return app

# ---------------- Main ----------------
def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    app = build_app()
    app.run_polling(drop_pending_updates=False)

if __name__ == "__main__":
    main()

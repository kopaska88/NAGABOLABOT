"""
Telegram Broadcast Bot — Railway + Postgres (python-telegram-bot v21)
---------------------------------------------------------------
Fitur:
- /broadcast (flow: teks → media opsional → tombol opsional → preview → kirim)
- Simpan user yang berinteraksi (users table) & hitung jumlah pengguna (/stats)
- Manajemen admin: /admins (lihat), /admin_add, /admin_del (khusus OWNER)
- Broadcast ke semua user terdaftar (atau ganti query sesuai kebutuhan)
- Siap deploy di Railway + Database PostgreSQL

ENV yang dibutuhkan:
- BOT_TOKEN       : token Bot Telegram
- OWNER_ID        : ID pemilik (angka) — hanya ini yang bisa tambah/hapus admin
- DATABASE_URL    : URL Postgres, contoh: postgres://user:pass@host:5432/dbname

Requirements (requirements.txt):
--------------------------------
python-telegram-bot==21.6
asyncpg==0.29.0
python-dotenv==1.0.1

Procfile (opsional, Railway autodetect Python):
-----------------------------------------------
web: python main.py

"""

import os
import asyncio
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from datetime import datetime

import asyncpg
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from dotenv import load_dotenv

# ----------------- ENV -----------------
load_dotenv()  # aman jika lokal; di Railway ENV tetap dari panel
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL", "")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN kosong. Set ENV BOT_TOKEN.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL kosong. Set ENV DATABASE_URL.")
if not OWNER_ID:
    raise RuntimeError("OWNER_ID kosong. Set ENV OWNER_ID (integer Telegram user_id).")

# -------------- DB UTILS --------------
async def init_db(pool: asyncpg.Pool):
    async with pool.acquire() as con:
        # Tabel users: simpan interaksi
        await con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                last_seen TIMESTAMPTZ NOT NULL
            );
            """
        )
        # Tabel admins: list admin
        await con.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY
            );
            """
        )
        # Pastikan OWNER adalah admin minimal (opsional)
        await con.execute(
            "INSERT INTO admins(user_id) VALUES($1) ON CONFLICT (user_id) DO NOTHING;",
            OWNER_ID,
        )

async def upsert_user(pool: asyncpg.Pool, uid: int, first_name: Optional[str], username: Optional[str]):
    async with pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO users(user_id, first_name, username, last_seen)
            VALUES($1, $2, $3, NOW())
            ON CONFLICT (user_id)
            DO UPDATE SET first_name=EXCLUDED.first_name,
                          username=EXCLUDED.username,
                          last_seen=NOW();
            """,
            uid, first_name, username
        )

async def is_admin(pool: asyncpg.Pool, uid: int) -> bool:
    if uid == OWNER_ID:
        return True
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT 1 FROM admins WHERE user_id=$1", uid)
        return row is not None

async def add_admin(pool: asyncpg.Pool, uid: int) -> bool:
    async with pool.acquire() as con:
        try:
            await con.execute("INSERT INTO admins(user_id) VALUES($1)", uid)
            return True
        except Exception:
            return False

async def del_admin(pool: asyncpg.Pool, uid: int) -> bool:
    async with pool.acquire() as con:
        res = await con.execute("DELETE FROM admins WHERE user_id=$1", uid)
        return res.endswith("1")

async def get_admins(pool: asyncpg.Pool) -> List[int]:
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT user_id FROM admins ORDER BY user_id")
        ids = [r[0] for r in rows]
        if OWNER_ID not in ids:
            ids.insert(0, OWNER_ID)
        return ids

async def count_users(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT COUNT(*) FROM users")
        return int(row[0])

async def get_all_user_ids(pool: asyncpg.Pool) -> List[int]:
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT user_id FROM users")
        return [r[0] for r in rows]

# -------------- BROADCAST STATE --------------
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

# -------------- HELPERS --------------
def ensure_session(user_id: int) -> Session:
    if user_id not in sessions:
        sessions[user_id] = Session()
    return sessions[user_id]

def draft_keyboard(draft: BroadcastDraft) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(b.text, url=b.url)] for b in draft.buttons]
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])

def preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Kirim", callback_data="preview_send")],
        [InlineKeyboardButton("🔁 Ulangi", callback_data="preview_restart")],
        [InlineKeyboardButton("❌ Batal", callback_data="preview_cancel")],
    ])

def yesno_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Ya", callback_data="btn_yes"), InlineKeyboardButton("Tidak", callback_data="btn_no")]
    ])

# -------------- HANDLERS --------------
async def track_user_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Call in every handler via application.add_handler(MessageHandler(filters.ALL, track_user_interaction), group=0)
    pool: asyncpg.Pool = context.application.bot_data["pool"]
    u = update.effective_user
    if u:
        await upsert_user(pool, u.id, u.first_name, u.username)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Halo! Bot siap. Gunakan /broadcast (admin) atau /stats untuk melihat total pengguna.")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pool: asyncpg.Pool = context.application.bot_data["pool"]
    total = await count_users(pool)
    await update.message.reply_text(f"👥 Total pengguna terdaftar: {total}")

async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pool: asyncpg.Pool = context.application.bot_data["pool"]
    uid = update.effective_user.id
    if not await is_admin(pool, uid):
        return await update.message.reply_text("Khusus admin.")
    ids = await get_admins(pool)
    lines = [f"- {i}" + (" (OWNER)" if i == OWNER_ID else "") for i in ids]
    await update.message.reply_text("Daftar admin:\n" + "\n".join(lines))

async def cmd_admin_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pool: asyncpg.Pool = context.application.bot_data["pool"]
    uid = update.effective_user.id
    if uid != OWNER_ID:
        return await update.message.reply_text("Hanya OWNER yang dapat menambah admin.")

    target_id: Optional[int] = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])

    if not target_id:
        return await update.message.reply_text("Gunakan /admin_add <user_id> atau reply pesan user.")

    ok = await add_admin(pool, target_id)
    if ok:
        await update.message.reply_text(f"Berhasil menambah admin: {target_id}")
    else:
        await update.message.reply_text("Gagal/duplikat. User mungkin sudah admin.")

async def cmd_admin_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pool: asyncpg.Pool = context.application.bot_data["pool"]
    uid = update.effective_user.id
    if uid != OWNER_ID:
        return await update.message.reply_text("Hanya OWNER yang dapat menghapus admin.")

    target_id: Optional[int] = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])

    if not target_id:
        return await update.message.reply_text("Gunakan /admin_del <user_id> atau reply pesan user.")
    if target_id == OWNER_ID:
        return await update.message.reply_text("Tidak dapat menghapus OWNER.")

    ok = await del_admin(pool, target_id)
    if ok:
        await update.message.reply_text(f"Admin {target_id} dihapus.")
    else:
        await update.message.reply_text("User bukan admin atau gagal menghapus.")

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pool: asyncpg.Pool = context.application.bot_data["pool"]
    if not await is_admin(pool, update.effective_user.id):
        return await update.message.reply_text("Maaf, fitur ini hanya untuk admin.")
    s = ensure_session(update.effective_user.id)
    s.step = Step.ASK_TEXT
    s.draft = BroadcastDraft()
    await update.message.reply_text("Kirimkan *teks* untuk broadcast.", parse_mode=ParseMode.MARKDOWN)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pool: asyncpg.Pool = context.application.bot_data["pool"]
    uid = update.effective_user.id
    if not await is_admin(pool, uid):
        return  # abaikan non-admin dalam flow broadcast

    s = ensure_session(uid)
    msg = update.effective_message

    if s.step == Step.ASK_TEXT:
        if not msg.text or msg.text.strip().lower() in ("/broadcast",):
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

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pool: asyncpg.Pool = context.application.bot_data["pool"]
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    s = ensure_session(uid)
    data = query.data

    if s.step == Step.ASK_ADD_BUTTON:
        if data == "btn_yes":
            s.step = Step.ASK_BUTTON_TEXT
            return await query.edit_message_text("Kirim *teks button* (contoh: Kunjungi Situs)", parse_mode=ParseMode.MARKDOWN)
        elif data == "btn_no":
            s.step = Step.PREVIEW
            await show_preview(query, s.draft)
            return

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

async def show_preview(query, draft: BroadcastDraft):
    caption_html = draft.text or ""
    kb = draft_keyboard(draft)
    chat_id = query.message.chat_id
    # kirim preview sesuai jenis
    if draft.photo_file_id:
        await query.message.bot.send_photo(chat_id=chat_id, photo=draft.photo_file_id,
                                           caption=caption_html, parse_mode=ParseMode.HTML,
                                           reply_markup=kb)
    elif draft.video_file_id:
        await query.message.bot.send_video(chat_id=chat_id, video=draft.video_file_id,
                                           caption=caption_html, parse_mode=ParseMode.HTML,
                                           reply_markup=kb)
    elif draft.animation_file_id:
        await query.message.bot.send_animation(chat_id=chat_id, animation=draft.animation_file_id,
                                               caption=caption_html, parse_mode=ParseMode.HTML,
                                               reply_markup=kb)
    else:
        await query.message.bot.send_message(chat_id=chat_id, text=caption_html,
                                             parse_mode=ParseMode.HTML, reply_markup=kb)

    await query.message.bot.send_message(
        chat_id=chat_id,
        text="Preview di atas. Lanjutkan?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Kirim", callback_data="preview_send")],
            [InlineKeyboardButton("🔁 Ulangi", callback_data="preview_restart")],
            [InlineKeyboardButton("❌ Batal", callback_data="preview_cancel")],
        ])
    )

async def do_broadcast(context: ContextTypes.DEFAULT_TYPE, draft: BroadcastDraft, query):
    pool: asyncpg.Pool = context.application.bot_data["pool"]
    targets = await get_all_user_ids(pool)
    sent = 0
    failed = 0
    kb = draft_keyboard(draft)

    for chat_id in targets:
        try:
            if draft.photo_file_id:
                await context.bot.send_photo(chat_id=chat_id, photo=draft.photo_file_id,
                                             caption=draft.text, parse_mode=ParseMode.HTML,
                                             reply_markup=kb)
            elif draft.video_file_id:
                await context.bot.send_video(chat_id=chat_id, video=draft.video_file_id,
                                             caption=draft.text, parse_mode=ParseMode.HTML,
                                             reply_markup=kb)
            elif draft.animation_file_id:
                await context.bot.send_animation(chat_id=chat_id, animation=draft.animation_file_id,
                                                 caption=draft.text, parse_mode=ParseMode.HTML,
                                                 reply_markup=kb)
            else:
                await context.bot.send_message(chat_id=chat_id, text=draft.text,
                                               parse_mode=ParseMode.HTML, reply_markup=kb)
            sent += 1
            await asyncio.sleep(0.05)  # rate limit friendly
        except Exception:
            failed += 1
            await asyncio.sleep(0.3)

    await query.message.reply_text(f"Selesai. Terkirim: {sent}, Gagal: {failed}")

# -------------- MAIN -----------------
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram.request import HTTPXRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("broadcast-bot")

PORT = int(os.getenv("PORT", "8000"))  # Railway sometimes sends PORT

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

async def post_init_del_webhook(app):
    # Pastikan tidak ada webhook yang aktif (kalau sebelumnya pernah pakai webhook)
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        me = await app.bot.get_me()
        logger.info("Bot authorized as @%s (%s)", me.username, me.id)
    except Exception as e:
        logger.exception("POST_INIT error: %s", e)

async def build_app(pool: asyncpg.Pool):
    # Atur request client dengan timeout yang lebih longgar (Railway kadang lambat warm-up)
    request = HTTPXRequest(
        connect_timeout=15.0,
        read_timeout=45.0,
        write_timeout=15.0,
        pool_timeout=15.0,
    )

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request)
        .post_init(post_init_del_webhook)
        .build()
    )

    app.bot_data["pool"] = pool

    # Group 0: tracker interaksi (jalan duluan untuk semua update)
    app.add_handler(MessageHandler(filters.ALL, track_user_interaction), group=0)

    # Commands umum
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))

    # Admin management
    app.add_handler(CommandHandler("admins", cmd_admins))
    app.add_handler(CommandHandler("admin_add", cmd_admin_add))
    app.add_handler(CommandHandler("admin_del", cmd_admin_del))

    # Broadcast
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.ALL, handle_message))

    return app

async def serve_healthz():
    # Jalankan HTTP server sederhana agar Railway menganggap service "up"
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logger.info("Health server on :%s", PORT)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, server.serve_forever)

async def main_async():
    # Pool Postgres
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    await init_db(pool)

    app = await build_app(pool)

    # --- Manual polling sequence (tanpa run_polling agar tidak bentrok event loop) ---
    await app.initialize()
    # Pastikan webhook mati & test koneksi di post_init
    await app.start()

    # Mulai polling
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

    # Health server jalan paralel
    health_task = asyncio.create_task(serve_healthz())

    # Tunggu sampai updater berhenti (SIGTERM dari Railway akan menghentikan proses)
    try:
        await app.updater.wait_until_closed()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        health_task.cancel()
        try:
            await health_task
        except asyncio.CancelledError:
            pass
        await pool.close()

if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        pass

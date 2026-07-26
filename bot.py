import os
import sys
import json
import logging
import asyncio
import re
import tempfile
import threading
import shutil
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import date
from pathlib import Path
from urllib.request import urlopen
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import yt_dlp
from dotenv import load_dotenv

load_dotenv()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.getenv("PORT", "8000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


def self_ping():
    port = os.getenv("PORT", "")
    if not port:
        return
    import time
    import urllib.request
    while True:
        time.sleep(600)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10)
        except Exception:
            pass


logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    force=True,
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
COOKIES_FILE = os.getenv("COOKIES_FILE", "")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE_MB", "2000")) * 1024 * 1024
DAILY_LIMIT_FREE = int(os.getenv("DAILY_LIMIT_FREE", "10"))
DB_PATH = "bot_database.json"
DB_LOCK = threading.Lock()
BOT_NAME = "Omar"

Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)


def load_db():
    with DB_LOCK:
        try:
            with open(DB_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"users": {}, "total_downloads": 0}


def save_db(data):
    with DB_LOCK:
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def _get_user_entry(db, uid):
    today = str(date.today())
    u = db["users"].get(uid, {"d": 0, "dt": today})
    if u.get("dt") != today:
        u = {"d": 0, "dt": today}
    return u


def get_remaining(user_id):
    db = load_db()
    u = _get_user_entry(db, str(user_id))
    return DAILY_LIMIT_FREE - u["d"]


def inc_downloads(user_id):
    db = load_db()
    uid = str(user_id)
    u = _get_user_entry(db, uid)
    u["d"] += 1
    u["dt"] = str(date.today())
    db["users"][uid] = u
    db["total_downloads"] = db.get("total_downloads", 0) + 1
    save_db(db)


def get_stats():
    db = load_db()
    total = db.get("total_downloads", 0)
    active_today = sum(
        1 for u in db["users"].values()
        if u.get("dt") == str(date.today())
    )
    return total, active_today, len(db["users"])


def fmt_duration(secs):
    if not secs:
        return "Live"
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fmt_size(b):
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def progress_bar(pct):
    filled = int(pct / 10)
    return f"{'█' * filled}{'░' * (10 - filled)} {pct:.1f}%"


def extract_domain(url):
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else url


def _find_ffmpeg():
    path = os.getenv("FFMPEG_PATH") or ""
    if path and shutil.which(path):
        return str(Path(path).parent) if Path(path).is_file() else path
    found = shutil.which("ffmpeg")
    if found:
        return str(Path(found).parent)
    return None


HAS_FFMPEG = _find_ffmpeg() is not None
logger.info("ffmpeg available: %s", HAS_FFMPEG)


def build_ydl_opts(extra=None, is_audio=False):
    opts = {
        "outtmpl": f"{DOWNLOAD_DIR}/%(title).80s.%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "nocheckcertificate": True,
        "extractor_retries": 3,
        "ignoreerrors": False,
        "noprogress": True,
    }
    ffmpeg_dir = _find_ffmpeg()
    if ffmpeg_dir:
        opts["ffmpeg_location"] = ffmpeg_dir
    if is_audio and HAS_FFMPEG:
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        }]
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        opts["cookiefile"] = COOKIES_FILE
    if extra:
        opts.update(extra)
    return opts


async def edit_callback_message(query, text, reply_markup=None):
    try:
        if query.message.text is not None:
            return await query.edit_message_text(text, reply_markup=reply_markup)
        else:
            try:
                await query.message.delete()
            except Exception:
                pass
            return await query.message.reply_text(text, reply_markup=reply_markup)
    except Exception:
        try:
            return await query.message.reply_text(text, reply_markup=reply_markup)
        except Exception:
            return None


class DownloadTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self.pct = 0.0
        self.speed = ""
        self.eta = ""
        self.status = "starting"
        self.cancelled = False

    def hook(self, d):
        if self.cancelled:
            raise yt_dlp.utils.DownloadError("Download cancelled by user")
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            with self._lock:
                self.pct = (downloaded / total * 100) if total else 0
                self.speed = d.get("_speed_str", "").strip()
                self.eta = d.get("_eta_str", "").strip()
        elif d["status"] == "finished":
            with self._lock:
                self.status = "finished"
                self.pct = 100

    def get_progress(self):
        with self._lock:
            return self.pct, self.speed, self.eta


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    total, active, users = get_stats()

    if user.id == ADMIN_ID:
        text = (
            f"Hey Omar! Welcome back.\n\n"
            f"Bot Stats:\n"
            f"  Total Downloads: {total}\n"
            f"  Active Users Today: {active}\n"
            f"  Total Users: {users}\n\n"
            f"Send a video link to start downloading.\n"
            f"You are the owner - no daily limits."
        )
    else:
        remaining = get_remaining(user.id)
        text = (
            f"Hey! Welcome to {BOT_NAME}'s Download Bot!\n\n"
            f"Send a video link and I will download it for you.\n"
            f"Supported: YouTube, TikTok, Instagram, Twitter, Facebook and more.\n\n"
            f"Downloads left today: {remaining}\n"
            f"Contact the owner for unlimited access."
        )
    await update.message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Available Commands:\n\n"
        "/start - Show welcome message\n"
        "/help - This help text\n\n"
        "Just send a video link to start downloading."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tracker = context.user_data.get("tracker")
    if tracker:
        tracker.cancelled = True
    await edit_callback_message(query, "Download cancelled.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    url = update.message.text.strip()

    if not url.startswith("http"):
        await update.message.reply_text("Please send a valid link starting with http")
        return

    if user.id != ADMIN_ID:
        remaining = get_remaining(user.id)
        if remaining <= 0:
            await update.message.reply_text(
                "You have reached your daily download limit.\n"
                "Contact the owner for unlimited access."
            )
            return

    status_msg = await update.message.reply_text("Checking link...")

    try:
        with yt_dlp.YoutubeDL(build_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        logger.exception("Info extraction failed")
        error_text = str(e)[:200]
        await status_msg.edit_text(
            f"Could not check the link.\n"
            f"Error: {error_text}\n\n"
            "The link may be invalid, deleted, or from an unsupported site."
        )
        return

    title = info.get("title", "No title")
    duration = fmt_duration(info.get("duration"))
    uploader = info.get("uploader") or info.get("channel") or info.get("creator") or "Unknown"
    thumbnail_url = info.get("thumbnail")
    domain = extract_domain(url)

    formats = info.get("formats", [])
    height_map = {}
    for f in formats:
        h = f.get("height")
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        if vcodec != "none" and h:
            height_map[h] = True

    available_q = set()
    for h in height_map:
        if h <= 360:
            available_q.add("360")
        if h <= 480:
            available_q.add("480")
        if h <= 720:
            available_q.add("720")
        if h <= 1080:
            available_q.add("1080")
        if h > 1080:
            available_q.add("2160")

    keyboard = []
    row = []
    if "1080" in available_q:
        row.append(InlineKeyboardButton("1080p", callback_data="dl_1080"))
    if "720" in available_q:
        row.append(InlineKeyboardButton("720p", callback_data="dl_720"))
    if row:
        keyboard.append(row)
    row2 = []
    if "480" in available_q:
        row2.append(InlineKeyboardButton("480p", callback_data="dl_480"))
    if "360" in available_q:
        row2.append(InlineKeyboardButton("360p", callback_data="dl_360"))
    row2.append(InlineKeyboardButton("MP3", callback_data="dl_mp3"))
    keyboard.append(row2)
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel")])

    context.user_data["current_url"] = url
    context.user_data["current_info"] = info

    caption = (
        f"{title}\n\n"
        f"By: {uploader}\n"
        f"Duration: {duration}\n"
        f"Source: {domain}"
    )

    thumb_path = None
    if thumbnail_url:
        try:
            thumb_data = urlopen(thumbnail_url, timeout=10).read()
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp.write(thumb_data)
                thumb_path = tmp.name
        except Exception:
            thumb_path = None

    await status_msg.delete()

    if thumb_path:
        try:
            with open(thumb_path, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption=caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
        finally:
            try:
                os.unlink(thumb_path)
            except Exception:
                pass
    else:
        await update.message.reply_text(
            caption,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    choice = query.data

    if choice == "cancel":
        tracker = context.user_data.get("tracker")
        if tracker:
            tracker.cancelled = True
        await edit_callback_message(query, "Download cancelled.")
        return

    if not choice.startswith("dl_"):
        return

    if user.id != ADMIN_ID:
        remaining = get_remaining(user.id)
        if remaining <= 0:
            await edit_callback_message(query, "Daily limit reached. Contact the owner.")
            return

    url = context.user_data.get("current_url")
    if not url:
        await edit_callback_message(query, "Link expired. Please send a new link.")
        return

    quality_map = {
        "dl_1080": ("1080p", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best", "mp4"),
        "dl_720":  ("720p",  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best", "mp4"),
        "dl_480":  ("480p",  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/best", "mp4"),
        "dl_360":  ("360p",  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[height<=360]/best", "mp4"),
        "dl_mp3":  ("MP3",   "bestaudio/best", None),
    }

    if not HAS_FFMPEG:
        quality_map = {
            "dl_1080": ("1080p", "best[height<=1080][ext=mp4]/best[height<=1080]/best", "mp4"),
            "dl_720":  ("720p",  "best[height<=720][ext=mp4]/best[height<=720]/best", "mp4"),
            "dl_480":  ("480p",  "best[height<=480][ext=mp4]/best[height<=480]/best", "mp4"),
            "dl_360":  ("360p",  "best[height<=360][ext=mp4]/best[height<=360]/best", "mp4"),
            "dl_mp3":  ("MP3",   "bestaudio[ext=m4a]/best[ext=m4a]/bestaudio/best", None),
        }

    if choice not in quality_map:
        return

    label, fmt, merge_fmt = quality_map[choice]
    is_audio = choice == "dl_mp3"

    ydl_opts = build_ydl_opts(
        {"format": fmt},
        is_audio=is_audio,
    )
    if merge_fmt:
        ydl_opts["merge_output_format"] = merge_fmt

    tracker = DownloadTracker()
    context.user_data["tracker"] = tracker
    ydl_opts["progress_hooks"] = [tracker.hook]

    status_msg = await edit_callback_message(
        query,
        f"Starting download: {label}...",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Cancel", callback_data="cancel")
        ]]),
    )

    def download():
        last_exc = None
        format_attempts = [
            fmt,
            "best[ext=mp4]/best",
            "best",
        ] if not is_audio else [
            "bestaudio[ext=m4a]/bestaudio/best",
            "best",
        ]
        for attempt_fmt in format_attempts:
            attempt_opts = dict(ydl_opts)
            attempt_opts["format"] = attempt_fmt
            try:
                with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    fn = ydl.prepare_filename(info)
                    if is_audio and HAS_FFMPEG:
                        fn = str(Path(fn).with_suffix(".mp3"))
                    elif merge_fmt and HAS_FFMPEG:
                        fn = str(Path(fn).with_suffix(f".{merge_fmt}"))
                    return info, fn
            except Exception as e:
                last_exc = e
                logger.warning("Format '%s' failed: %s", attempt_fmt, e)
                continue
        raise last_exc or yt_dlp.utils.DownloadError("All format attempts failed")

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, download)
    filename = None

    try:
        while not future.done():
            if tracker.cancelled:
                await edit_callback_message(
                    status_msg if status_msg and hasattr(status_msg, 'edit_text') else query.message,
                    "Download cancelled."
                )
                return

            await asyncio.sleep(1.5)
            pct, speed, eta = tracker.get_progress()
            bar = progress_bar(pct)
            parts = [f"Downloading: {label}", f"{bar}"]
            if speed:
                parts.append(f"Speed: {speed}")
            if eta:
                parts.append(f"ETA: {eta}")
            try:
                if status_msg and hasattr(status_msg, 'edit_text'):
                    await status_msg.edit_text(
                        "\n".join(parts),
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("Cancel", callback_data="cancel")
                        ]]),
                    )
            except Exception:
                pass

        info, filename = await future

        if not filename or not os.path.exists(filename):
            try:
                if status_msg and hasattr(status_msg, 'edit_text'):
                    await status_msg.edit_text("Download failed: file was not created.")
            except Exception:
                pass
            return

        file_size = os.path.getsize(filename)
        if file_size > MAX_FILE_SIZE:
            os.remove(filename)
            filename = None
            try:
                if status_msg and hasattr(status_msg, 'edit_text'):
                    await status_msg.edit_text(
                        f"File too large ({fmt_size(file_size)}).\n"
                        f"Max allowed: {MAX_FILE_SIZE // 1024 // 1024}MB.\n"
                        "Try a lower quality or MP3."
                    )
            except Exception:
                pass
            return

        try:
            if status_msg and hasattr(status_msg, 'edit_text'):
                await status_msg.edit_text("Uploading to Telegram...")
        except Exception:
            pass

        title = info.get("title", "")
        caption = (
            f"Downloaded!\n"
            f"{title}\n"
            f"{label} | {fmt_size(file_size)}"
        )

        with open(filename, "rb") as f:
            if is_audio:
                await query.message.reply_audio(audio=f, caption=caption)
            else:
                if file_size > 50 * 1024 * 1024:
                    await query.message.reply_document(document=f, caption=caption)
                else:
                    await query.message.reply_video(video=f, caption=caption)

        if user.id != ADMIN_ID:
            inc_downloads(user.id)
            remaining = get_remaining(user.id)
            await query.message.reply_text(
                f"Downloads left today: {remaining}\n"
                "Contact the owner for unlimited access."
            )

    except asyncio.CancelledError:
        try:
            if status_msg and hasattr(status_msg, 'edit_text'):
                await status_msg.edit_text("Download cancelled.")
        except Exception:
            pass
    except Exception as e:
        logger.exception("Download failed")
        error_detail = str(e)[:300]
        try:
            if status_msg and hasattr(status_msg, 'edit_text'):
                await status_msg.edit_text(
                    f"Download failed.\n\n"
                    f"Error: {error_detail}\n\n"
                    "Try a different quality or check if the link is public."
                )
        except Exception:
            pass
    finally:
        if filename and os.path.exists(filename):
            try:
                os.remove(filename)
            except Exception:
                pass
        context.user_data.pop("tracker", None)


def main():
    if not TOKEN or ADMIN_ID == 0:
        logger.error("Missing BOT_TOKEN or ADMIN_ID in environment variables")
        sys.exit(1)

    logger.info("Bot starting... Token: %s... Admin: %s", TOKEN[:10], ADMIN_ID)

    if os.getenv("PORT"):
        t = threading.Thread(target=start_health_server, daemon=True)
        t.start()
        t2 = threading.Thread(target=self_ping, daemon=True)
        t2.start()
        logger.info("Health check server started")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_click))

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Exception while handling an update: %s", context.error)

    app.add_error_handler(error_handler)

    logger.info("Bot is running!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

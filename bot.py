import os, sys, json, logging, asyncio, time, re, tempfile, threading
from datetime import date
from pathlib import Path
from urllib.request import urlopen
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import yt_dlp
from dotenv import load_dotenv

load_dotenv()

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
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE_MB", "50")) * 1024 * 1024
DAILY_LIMIT_FREE = int(os.getenv("DAILY_LIMIT_FREE", "2"))
DB_PATH = "bot_database.json"
DB_LOCK = threading.Lock()

Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_duration(secs):
    if not secs:
        return "مباشر"
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

def build_ydl_opts(extra=None, is_audio=False):
    opts = {
        "outtmpl": f"{DOWNLOAD_DIR}/%(title)s.%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 10,
        "ffmpeg_location": os.getenv("FFMPEG_PATH") or None,
    }
    if is_audio:
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

# ---------------------------------------------------------------------------
# Helper to edit callback message (handles both text and photo messages)
# ---------------------------------------------------------------------------

async def edit_callback_message(query, text, reply_markup=None):
    """Edit the callback query message. If it's a photo (no text), delete and send new."""
    if query.message.text is not None:
        return await query.edit_message_text(text, reply_markup=reply_markup)
    else:
        try:
            await query.message.delete()
        except Exception:
            pass
        return await query.message.reply_text(text, reply_markup=reply_markup)

# ---------------------------------------------------------------------------
# Download tracker (thread-safe)
# ---------------------------------------------------------------------------

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
            raise yt_dlp.utils.DownloadError("تم إلغاء التحميل من قبل المستخدم")
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

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    total, active, users = get_stats()

    if user.id == ADMIN_ID:
        text = (
            f"⚡ أهلاً بك يا مالك البوت!\n\n"
            f"📊 إحصائيات البوت:\n"
            f"• إجمالي التحميلات: {total}\n"
            f"• المستخدمون النشطون اليوم: {active}\n"
            f"• إجمالي المستخدمين: {users}\n\n"
            f"📥 أرسل رابط فيديو لبدء التحميل.\n"
            f"👑 أنت المالك — بدون حدود يومية."
        )
    else:
        remaining = get_remaining(user.id)
        text = (
            f"🎥 مرحباً بك في بوت التحميل!\n\n"
            f"📥 أرسل رابط فيديو وسأقوم بتحميله لك.\n"
            f"يدعم: YouTube, TikTok, Instagram, Twitter, Facebook والمزيد...\n\n"
            f"⚡ تبقى لك اليوم: {remaining} تحميلات\n"
            f"💎 للاشتراك غير المحدود تواصل مع المطور."
        )
    await update.message.reply_text(text)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚙️ الأوامر المتاحة:\n\n"
        "/start - عرض الترحيب\n"
        "/help - هذه التعليمات\n\n"
        "📥 فقط أرسل رابط الفيديو لبدء التحميل."
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tracker = context.user_data.get("tracker")
    if tracker:
        tracker.cancelled = True
    await edit_callback_message(query, "❌ تم إلغاء التحميل.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    url = update.message.text.strip()

    if not url.startswith("http"):
        await update.message.reply_text("⚠️ أرسل رابطاً صحيحاً يبدأ بـ http")
        return

    if user.id != ADMIN_ID:
        remaining = get_remaining(user.id)
        if remaining <= 0:
            keyboard = [[InlineKeyboardButton("💎 تواصل مع المالك", url="https://t.me/botfather")]]
            await update.message.reply_text(
                "❌ استنفذت حدود التحميل اليومية.\n"
                "اشترك للتحميل غير المحدود.",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

    status_msg = await update.message.reply_text("🔍 جاري فحص الرابط...")

    try:
        with yt_dlp.YoutubeDL(build_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        logger.exception("Info extraction failed")
        await status_msg.edit_text(
            "❌ تعذر فحص الرابط. الأسباب المحتملة:\n"
            "• الرابط غير صالح أو محذوف\n"
            "• الموقع غير مدعوم أو يحتاج تسجيل دخول\n"
            "• الفيديو خاص (private) أو مقيد بحظر عمري"
        )
        return

    title = info.get("title", "بدون عنوان")
    duration = fmt_duration(info.get("duration"))
    uploader = info.get("uploader") or info.get("channel") or info.get("creator") or "غير معروف"
    thumbnail_url = info.get("thumbnail")
    duration_secs = info.get("duration", 0)
    domain = extract_domain(url)

    # Detect available qualities
    available_q = {"audio": True}
    for f in info.get("formats", []):
        h = f.get("height")
        if h:
            if h <= 480:
                available_q["480"] = True
            elif h <= 720:
                available_q["720"] = True
            elif h <= 1080:
                available_q["1080"] = True
            elif h > 1080:
                available_q["2160"] = True

    keyboard = []
    row = []
    if "1080" in available_q:
        row.append(InlineKeyboardButton("🎬 1080p", callback_data="mp4_1080"))
    if "720" in available_q:
        row.append(InlineKeyboardButton("🎬 720p", callback_data="mp4_720"))
    if row:
        keyboard.append(row)
    row2 = []
    if "480" in available_q:
        row2.append(InlineKeyboardButton("📱 480p", callback_data="mp4_480"))
    if "audio" in available_q:
        row2.append(InlineKeyboardButton("🎵 MP3", callback_data="mp3"))
    if row2:
        keyboard.append(row2)
    keyboard.append([InlineKeyboardButton("❌ إلغاء", callback_data="cancel")])

    context.user_data["current_url"] = url
    context.user_data["current_info"] = info

    caption = (
        f"🎬 {title}\n\n"
        f"👤 {uploader}\n"
        f"⏱ {duration}\n"
        f"🌐 {domain}"
    )

    # Send thumbnail preview if available
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
            os.unlink(thumb_path)
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
        await edit_callback_message(query, "❌ تم الإلغاء.")
        return

    if user.id != ADMIN_ID:
        remaining = get_remaining(user.id)
        if remaining <= 0:
            await edit_callback_message(query, "❌ انتهت حصتك اليومية. تواصل مع المالك.")
            return

    url = context.user_data.get("current_url")
    if not url:
        await edit_callback_message(query, "❌ الرابط غير صالح، أرسل رابطاً جديداً.")
        return

    quality_labels = {
        "mp4_1080": ("1080p Full HD", "bestvideo[height<=1080]+bestaudio/best[height<=1080]", "mp4"),
        "mp4_720": ("720p HD", "bestvideo[height<=720]+bestaudio/best[height<=720]", "mp4"),
        "mp4_480": ("480p SD", "bestvideo[height<=480]+bestaudio/best[height<=480]", "mp4"),
        "mp3": ("MP3 320kbps", "bestaudio/best", None),
    }

    if choice not in quality_labels:
        await edit_callback_message(query, "❌ اختيار غير صالح.")
        return

    label, fmt, merge_fmt = quality_labels[choice]
    is_audio = choice == "mp3"

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
        f"⏳ بدء التحميل بصيغة {label}...",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ إلغاء", callback_data="cancel")
        ]]),
    )

    def download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            fn = ydl.prepare_filename(info)
            if is_audio:
                fn = str(Path(fn).with_suffix(".mp3"))
            elif merge_fmt:
                fn = str(Path(fn).with_suffix(f".{merge_fmt}"))
            return info, fn

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, download)
    filename = None

    try:
        while not future.done():
            if tracker.cancelled:
                await status_msg.edit_text("❌ تم إلغاء التحميل.")
                return

            await asyncio.sleep(1)
            pct, speed, eta = tracker.get_progress()
            bar = progress_bar(pct)
            parts = [f"📥 {label}", f"`{bar}`"]
            if speed:
                parts.append(f"⚡ {speed}")
            if eta:
                parts.append(f"⏱ {eta}")
            try:
                await status_msg.edit_text(
                    "\n".join(parts),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("❌ إلغاء", callback_data="cancel")
                    ]]),
                    parse_mode="Markdown",
                )
            except Exception:
                pass

        info, filename = await future

        if not os.path.exists(filename):
            await status_msg.edit_text("❌ فشل التحميل: الملف لم يتم إنشاؤه.")
            return

        file_size = os.path.getsize(filename)
        if file_size > MAX_FILE_SIZE:
            os.remove(filename)
            filename = None
            await status_msg.edit_text(
                f"❌ الملف كبير جداً ({fmt_size(file_size)}).\n"
                f"الحد الأقصى: {MAX_FILE_SIZE // 1024 // 1024}MB.\n"
                "جرب جودة أقل أو صيغة MP3."
            )
            return

        await status_msg.edit_text("🚀 جاري رفع الملف إلى تيليجرام...")

        caption = (
            f"✅ تم التحميل بنجاح!\n"
            f"🎬 {info.get('title', '')}\n"
            f"⚙️ {label} | {fmt_size(file_size)}"
        )

        with open(filename, "rb") as f:
            if is_audio:
                await query.message.reply_audio(audio=f, caption=caption)
            else:
                await query.message.reply_video(video=f, caption=caption)

        if user.id != ADMIN_ID:
            inc_downloads(user.id)
            remaining = get_remaining(user.id)
            await query.message.reply_text(
                f"⚡ تبقى لك اليوم: {remaining} تحميلات\n"
                "💎 للاشتراك غير المحدود تواصل مع المالك."
            )

    except asyncio.CancelledError:
        await status_msg.edit_text("❌ تم إلغاء التحميل.")
    except Exception:
        logger.exception("Download failed")
        await status_msg.edit_text(
            "❌ فشل التحميل. الأسباب المحتملة:\n"
            "• الرابط خاص أو محذوف\n"
            "• الموقع يتطلب تسجيل دخول\n"
            "• الفيديو طويل جداً (جرب MP3 أو جودة أقل)"
        )
    finally:
        if filename and os.path.exists(filename):
            try:
                os.remove(filename)
            except Exception:
                pass
        context.user_data.pop("tracker", None)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not TOKEN or ADMIN_ID == 0:
        logger.error("تأكد من وجود BOT_TOKEN و ADMIN_ID في ملف .env")
        sys.exit(1)

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_click))

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Exception while handling an update: %s", context.error)

    app.add_error_handler(error_handler)

    logger.info("✅ البوت يعمل...")
    app.run_polling()

if __name__ == "__main__":
    main()

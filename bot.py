"""
بوت تلجرام يرسل 5 صفحات من القرآن مع كل أذان صلاة
لكل مستخدم حسب مدينته الخاصة.
"""

import json
import logging
import os
from datetime import datetime, time as dtime

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from zoneinfo import ZoneInfo

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# إعدادات عامة
# ---------------------------------------------------------------------------
DATA_FILE = "users.json"
TOTAL_PAGES = 604
PAGES_PER_PRAYER = 5
PRAYER_NAMES = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]
PRAYER_NAMES_AR = {
    "Fajr": "الفجر",
    "Dhuhr": "الظهر",
    "Asr": "العصر",
    "Maghrib": "المغرب",
    "Isha": "العشاء",
}

# مجلد صور صفحات المصحف. الملفات لازم تكون مسماة برقم الصفحة: 1.jpg, 2.jpg ... 604.jpg
PAGES_DIR = "pages"
IMAGE_EXTENSIONS = ["jpg", "jpeg", "png"]

ALADHAN_URL = "https://api.aladhan.com/v1/timingsByCity"
QURAN_PAGE_URL = "https://api.alquran.cloud/v1/page/{page}/quran-uthmani"
QURAN_SURAH_LIST_URL = "https://api.alquran.cloud/v1/surah"
QURAN_SURAH_URL = "https://api.alquran.cloud/v1/surah/{number}/quran-uthmani"

SURAHS_PER_PAGE = 8  # عدد السور اللي تظهر بكل صفحة أزرار
MAX_MESSAGE_CHARS = 3500  # هامش أمان قبل حد تلجرام (4096)

_surah_list_cache: list[dict] | None = None  # كاش بالذاكرة لقائمة السور

# قائمة المدن الجاهزة اللي تظهر كأزرار (عدّل أو زِد عليها كما تحب)
# الصيغة: (الاسم اللي يظهر للمستخدم بالعربي، اسم المدينة بالإنجليزي، اسم الدولة بالإنجليزي)
POPULAR_CITIES = [
    ("الرياض", "Riyadh", "Saudi Arabia"),
    ("جدة", "Jeddah", "Saudi Arabia"),
    ("مكة المكرمة", "Makkah", "Saudi Arabia"),
    ("المدينة المنورة", "Madinah", "Saudi Arabia"),
    ("الدمام", "Dammam", "Saudi Arabia"),
    ("دبي", "Dubai", "United Arab Emirates"),
    ("أبوظبي", "Abu Dhabi", "United Arab Emirates"),
    ("الدوحة", "Doha", "Qatar"),
    ("الكويت", "Kuwait City", "Kuwait"),
    ("المنامة", "Manama", "Bahrain"),
    ("مسقط", "Muscat", "Oman"),
    ("القاهرة", "Cairo", "Egypt"),
    ("عمّان", "Amman", "Jordan"),
    ("بيروت", "Beirut", "Lebanon"),
    ("بغداد", "Baghdad", "Iraq"),
]


# ---------------------------------------------------------------------------
# تخزين بيانات المستخدمين (ملف JSON بسيط)
# ---------------------------------------------------------------------------
def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# استدعاء واجهات برمجية خارجية
# ---------------------------------------------------------------------------
def fetch_timings(city: str, country: str) -> dict:
    """يرجع أوقات الصلاة الخمسة + المنطقة الزمنية لمدينة معينة."""
    resp = requests.get(
        ALADHAN_URL,
        params={"city": city, "country": country, "method": 4},
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()["data"]
    timings = {p: payload["timings"][p].split(" ")[0] for p in PRAYER_NAMES}
    timezone = payload["meta"]["timezone"]
    return {"timings": timings, "timezone": timezone}


def fetch_pages(start_page: int, count: int = PAGES_PER_PRAYER) -> list[str]:
    """يرجع نصوص الصفحات المطلوبة من المصحف (نص عثماني)."""
    messages = []
    for offset in range(count):
        page_num = ((start_page - 1 + offset) % TOTAL_PAGES) + 1
        resp = requests.get(QURAN_PAGE_URL.format(page=page_num), timeout=15)
        resp.raise_for_status()
        ayahs = resp.json()["data"]["ayahs"]

        lines = []
        current_surah = None
        for ayah in ayahs:
            surah_name = ayah["surah"]["name"]
            if surah_name != current_surah:
                lines.append(f"\n📖 سورة {surah_name}")
                current_surah = surah_name
            lines.append(f"({ayah['numberInSurah']}) {ayah['text']}")

        text = f"📄 صفحة {page_num}\n" + "\n".join(lines)
        messages.append(text)
    return messages


def find_page_image(page_num: int) -> str | None:
    """يدور على ملف صورة الصفحة بأي امتداد مدعوم، يرجع المسار أو None."""
    for ext in IMAGE_EXTENSIONS:
        path = os.path.join(PAGES_DIR, f"{page_num}.{ext}")
        if os.path.exists(path):
            return path
    return None


def get_surah_list() -> list[dict]:
    """يرجع قائمة السور الـ114 (يجيبها مرة وحدة ويحفظها بالذاكرة)."""
    global _surah_list_cache
    if _surah_list_cache is None:
        resp = requests.get(QURAN_SURAH_LIST_URL, timeout=15)
        resp.raise_for_status()
        _surah_list_cache = resp.json()["data"]
    return _surah_list_cache


def fetch_surah_messages(number: int) -> list[str]:
    """يرجع نص سورة كاملة، مقسّم لعدة رسائل إذا كانت طويلة."""
    resp = requests.get(QURAN_SURAH_URL.format(number=number), timeout=15)
    resp.raise_for_status()
    surah = resp.json()["data"]

    revelation_ar = "مكية" if surah["revelationType"] == "Meccan" else "مدنية"
    header = (
        f"📖 سورة {surah['name']}\n"
        f"({surah['englishName']}) — {revelation_ar} — {surah['numberOfAyahs']} آية\n"
    )

    bismillah = ""
    if number != 1 and number != 9:
        bismillah = "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ\n\n"

    messages = []
    current = header + "\n" + bismillah
    for ayah in surah["ayahs"]:
        line = f"({ayah['numberInSurah']}) {ayah['text']} "
        if len(current) + len(line) > MAX_MESSAGE_CHARS:
            messages.append(current.strip())
            current = ""
        current += line
    if current.strip():
        messages.append(current.strip())
    return messages


# ---------------------------------------------------------------------------
# جدولة المهام
# ---------------------------------------------------------------------------
def schedule_today_jobs(app: Application, chat_id: str, user: dict) -> None:
    """يجدول إرسال الصفحات عند كل أذان متبقٍ اليوم، ويحدّث أوقات الغد عند منتصف الليل."""
    tz = ZoneInfo(user["timezone"])
    now = datetime.now(tz)

    try:
        result = fetch_timings(user["city"], user["country"])
    except Exception as e:  # noqa: BLE001
        logger.error("فشل جلب أوقات الصلاة لـ %s: %s", chat_id, e)
        return

    user["timezone"] = result["timezone"]
    save_data_safe(chat_id, user)

    for prayer in PRAYER_NAMES:
        hour, minute = map(int, result["timings"][prayer].split(":"))
        prayer_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if prayer_dt <= now:
            continue  # وقت الصلاة فات اليوم، بينتظر الجدول القادم
        delay = (prayer_dt - now).total_seconds()
        app.job_queue.run_once(
            send_pages_job,
            when=delay,
            data={"chat_id": chat_id, "prayer": prayer},
            name=f"{chat_id}-{prayer}-{prayer_dt.date()}",
        )

    # جدولة مهمة يومية الساعة 00:05 بتوقيت المستخدم لتحديث أوقات الغد
    app.job_queue.run_daily(
        daily_refresh_job,
        time=dtime(hour=0, minute=5, tzinfo=tz),
        data={"chat_id": chat_id},
        name=f"{chat_id}-daily-refresh",
    )


def save_data_safe(chat_id: str, user: dict) -> None:
    data = load_data()
    data[chat_id] = user
    save_data(data)


async def daily_refresh_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.data["chat_id"]
    data = load_data()
    user = data.get(chat_id)
    if user:
        schedule_today_jobs(context.application, chat_id, user)


async def send_pages_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.data["chat_id"]
    prayer = context.job.data["prayer"]
    data = load_data()
    user = data.get(chat_id)
    if not user:
        return

    start_page = user.get("current_page", 1)
    page_numbers = [
        ((start_page - 1 + offset) % TOTAL_PAGES) + 1
        for offset in range(PAGES_PER_PRAYER)
    ]

    prayer_ar = PRAYER_NAMES_AR.get(prayer, prayer)
    await context.bot.send_message(
        chat_id=int(chat_id), text=f"🕌 حان وقت صلاة {prayer_ar}\nوردك اليوم:"
    )

    for page_num in page_numbers:
        image_path = find_page_image(page_num)
        if image_path:
            with open(image_path, "rb") as img:
                await context.bot.send_photo(
                    chat_id=int(chat_id), photo=img, caption=f"📄 صفحة {page_num}"
                )
        else:
            logger.error("ما لقيت صورة الصفحة %d بمجلد %s", page_num, PAGES_DIR)
            await context.bot.send_message(
                chat_id=int(chat_id),
                text=f"⚠️ صورة صفحة {page_num} غير متوفرة حالياً.",
            )

    new_page = start_page + PAGES_PER_PRAYER
    if new_page > TOTAL_PAGES:
        new_page = (new_page % TOTAL_PAGES) or 1
    user["current_page"] = new_page
    save_data_safe(chat_id, user)


# ---------------------------------------------------------------------------
# أوامر البوت
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "أهلاً بك في بوت القرآن 📖\n\n"
        "مع كل أذان صلاة بترسل لك 5 صفحات من القرآن تلقائياً، "
        "وبتكمل من حيث وقفت في كل مرة.\n\n"
        "أول خطوة، اختر مدينتك بالأمر /city"
    )


def build_city_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for i, (label, _city, _country) in enumerate(POPULAR_CITIES):
        row.append(InlineKeyboardButton(label, callback_data=f"city_{i}"))
        if len(row) == 2:  # زرين بكل صف
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


async def apply_city(
    context: ContextTypes.DEFAULT_TYPE, chat_id: str, city: str, country: str
) -> str:
    """يحفظ المدينة ويجدول المهام، ويرجع نص رسالة التأكيد (أو الخطأ)."""
    try:
        result = fetch_timings(city, country)
    except Exception:  # noqa: BLE001
        return (
            "ما قدرت ألقى أوقات الصلاة لهذه المدينة. "
            "تأكد من كتابة الاسم بالإنجليزية وحاول مرة ثانية."
        )

    data = load_data()
    existing = data.get(chat_id, {})
    user = {
        "city": city,
        "country": country,
        "timezone": result["timezone"],
        "current_page": existing.get("current_page", 1),
    }
    data[chat_id] = user
    save_data(data)

    # احذف أي مهام قديمة لهذا المستخدم قبل ما نجدول من جديد
    for job in context.application.job_queue.jobs():
        if job.data and job.data.get("chat_id") == chat_id:
            job.schedule_removal()

    schedule_today_jobs(context.application, chat_id, user)

    return (
        f"تم ✅ مدينتك الآن: {city}, {country}\n"
        f"وردك بيبدأ من صفحة {user['current_page']}.\n"
        "بترسل لك 5 صفحات تلقائياً مع كل أذان."
    )


async def set_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.partition(" ")[2].strip()
    chat_id = str(update.effective_chat.id)

    # لو ما كتب شي بعد الأمر، نعرض له قائمة الأزرار
    if not text:
        await update.message.reply_text(
            "اختر مدينتك من القائمة:\n\n"
            "(ما لقيت مدينتك؟ اكتب /city المدينة, الدولة يدوياً، مثال:\n"
            "/city Riyadh, Saudi Arabia)",
            reply_markup=build_city_keyboard(),
        )
        return

    if "," not in text:
        await update.message.reply_text(
            "الصيغة الصحيحة:\n/city المدينة, الدولة\nمثال: /city Riyadh, Saudi Arabia"
        )
        return

    city, country = (p.strip() for p in text.split(",", 1))
    message = await apply_city(context, chat_id, city, country)
    await update.message.reply_text(message)


async def city_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    index = int(query.data.split("_", 1)[1])
    label, city, country = POPULAR_CITIES[index]
    chat_id = str(update.effective_chat.id)

    message = await apply_city(context, chat_id, city, country)
    await query.edit_message_text(message)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    data = load_data()
    user = data.get(chat_id)
    if not user:
        await update.message.reply_text("ما حددت مدينتك بعد. استخدم /city المدينة, الدولة")
        return
    await update.message.reply_text(
        f"📍 مدينتك: {user['city']}, {user['country']}\n"
        f"📄 ستبدأ القراءة القادمة من صفحة: {user['current_page']}"
    )


# ---------------------------------------------------------------------------
# إعادة جدولة كل المستخدمين عند تشغيل البوت من جديد
# ---------------------------------------------------------------------------
async def on_startup(app: Application) -> None:
    data = load_data()
    for chat_id, user in data.items():
        schedule_today_jobs(app, chat_id, user)
    logger.info("تمت إعادة جدولة %d مستخدم", len(data))


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("لازم تحط متغير البيئة BOT_TOKEN (التوكن من BotFather)")

    app = Application.builder().token(token).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("city", set_city))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(city_button_callback, pattern=r"^city_\d+$"))

    app.run_polling()


if __name__ == "__main__":
    main()

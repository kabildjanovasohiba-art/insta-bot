"""
Instagram Downloader Telegram boti
===================================

Ushbu bot foydalanuvchi Instagram'dan (post, reel, carousel, stories) havola
yuborganda, yt-dlp kutubxonasi orqali kontentni serverga tezda yuklab oladi
va uni to'g'ridan-to'g'ri Telegram'ga video/fayl ko'rinishida jo'natadi.

Asosiy xususiyatlar:
    * To'liq asinxron ish (aiogram v3), yt-dlp esa alohida potokda (thread)
      ishga tushiriladi (asyncio.to_thread), shuning uchun bot qotib qolmaydi.
    * Maxfiy sozlamalar (.env) python-dotenv orqali o'qiladi.
    * "Video yuklab olinmoqda, iltimos kuting..." degan status xabar
      jarayon oxirida o'chirib yuboriladi.
    * Yuklab olinan barcha vaqtinchalik fayllar yuborilgandan so'ng
      darhol os.remove() bilan o'chiriladi.
    * Barcha xatoliklar ushlanib, foydalanuvchiga tushunarli xabar chiqariladi
      (bot hech qachon qulab tushmaydi).
    * Agar GEMINI_API_KEY sozlangan bo'lsa — Google Gemini video tahlili
      asosida qisqacha, qiziqarli tavsif (caption) generatsiya qilib,
      uni video bilan birga yuboradi. Gemini ishlamasa — bot baribir
      ishlayveradi (zaxira caption bilan).

Ishga tushirish:
    pip install -r requirements.txt
    .env faylini yarating (ko'ring: .env.example)
    python bot.py
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

# --- Telegram (aiogram v3) kutubxonalari ---
from aiogram import Bot, Dispatcher
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.types import (
    FSInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

# --- Google Gemini (google-genai) kutubxonasi ---
from google import genai
from google.genai import types as genai_types

# --- Yuklab olish (yt-dlp) va sozlamalar ---
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import YoutubeDLError

# =============================================================================
# 1. Konfiguratsiya — barcha maxfiy sozlamalar .env fayldan o'qiladi
# =============================================================================

load_dotenv()  # .env faylini o'qib, atrof muhit o'zgaruvchilariga yuklaydi

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    # Token bo'lmasa botni ishga tushirib bo'lmaydi — aniq xabar beramiz
    raise SystemExit(
        "[XATO] BOT_TOKEN topilmadi! .env fayliga BOT_TOKEN=... qo'shing.\n"
        "Tokenni @BotFather'dan olish mumkin."
    )

# Telegram Bot API orqali yuboriladigan maksimal fayl hajmi (MB).
# Standart Bot API chegarasi — 50 MB (o'z serveringiz bo'lsa, kattaroq qo'ying).
MAX_FILESIZE_MB: int = int(os.getenv("MAX_FILESIZE_MB", "50") or "50")
MAX_FILESIZE_BYTES: int = MAX_FILESIZE_MB * 1024 * 1024

# Bitta video uchun maksimal kutish vaqti (soniya)
DOWNLOAD_TIMEOUT: int = int(os.getenv("DOWNLOAD_TIMEOUT", "300") or "300")

# Instagram kontenti uchun cookies fayli (shaxsiy yoki cheklangan postlar uchun).
# Ixtiyoriy: bo'sh qoldirilsa — ishlatilmaydi.
COOKIES_FILE: Optional[str] = os.getenv("COOKIES_FILE", "").strip() or None

# Admin Telegram ID — bot ishga tushganda va kutilmagan xatolik
# yuz berganda shu foydalanuvchiga xabar yuboriladi (ixtiyoriy).
_admin_raw: str = os.getenv("ADMIN_ID", "").strip()
try:
    ADMIN_ID: Optional[int] = int(_admin_raw) if _admin_raw else None
except ValueError:
    ADMIN_ID = None
    logging.warning("ADMIN_ID noto'g'ri raqam — admin xabarlari o'chirilgan")

# Google Gemini API kaliti — tavsif (caption) generatsiya qilish uchun.
# Bo'sh qoldirilsa — bot Gemini'siz, oddiy tavsif bilan ishlayveradi.
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()

# Ishlatiladigan Gemini modeli (tez va arzon variant — flash)
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "").strip() or "gemini-2.5-flash"

# Gemini javobi uchun maksimal kutish vaqti (soniya)
GEMINI_TIMEOUT: int = int(os.getenv("GEMINI_TIMEOUT", "120") or "120")

# Log darajasi (DEBUG, INFO, WARNING, ERROR)
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("instagram-bot")

# Gemini client (yaratishda xatolik bo'lsa — bot baribir ishlayveradi)
genai_client = None
if GEMINI_API_KEY:
    try:
        genai_client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("Gemini client tayyor (model: %s)", GEMINI_MODEL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Gemini client yaratilmadi: %s", exc)
        genai_client = None
else:
    logger.info("GEMINI_API_KEY kiritilmagan — tavsif Gemini'siz yoziladi")

# Dispatcher (handlerlar ro'yxatini saqlovchi markaz)
dp = Dispatcher()

# =============================================================================
# 2. Yordamchi funksiyalar: havolalarni aniqlash va tekshirish
# =============================================================================

# Matndagi istalgan http(s) havolasini topish uchun
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# Instagram'ga tegishli havola ekanini tekshirish (post/reel/stories/share...)
INSTAGRAM_HOST_RE = re.compile(
    r"^https?://(?:www\.|vm\.|vt\.|m\.)?(?:instagram\.com|instagr\.am)/",
    re.IGNORECASE,
)

# Rasm kengaytmalari — ularni video emas, photo sifatida yuboramiz
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# yt-dlp tomonidan yaratilishi mumkin bo'lgan "keraksiz" fayllar
SKIP_EXTENSIONS = {".part", ".ytdl", ".json", ".desc", ".description"}

# Media guruh (sendMedia_group) uchun Telegram chegarasi — 1 ta postda 10 ta fayl
MEDIA_GROUP_LIMIT = 10


def extract_url(text: Optional[str]) -> Optional[str]:
    """Matndagi birinchi http(s) havolasini qaytaradi (chegaralarni tozalaydi)."""
    if not text:
        return None
    match = URL_RE.search(text)
    if not match:
        return None
    # Xabardan olingan havola oxiridati:), ., ' kabi belgilarni olib tashlaymiz
    return match.group(0).rstrip(").,;!?\"'")


def is_instagram_url(url: Optional[str]) -> bool:
    """Havola Instagram'ga tegishli ekanini tekshiradi."""
    return bool(url) and bool(INSTAGRAM_HOST_RE.match(url or ""))


class IsInstagramLink(BaseFilter):
    """Filtr: xabar matnida Instagram havolasi borligini tekshiradi."""

    async def __call__(self, message: Message) -> bool:
        return is_instagram_url(extract_url(message.text))


class IsWebLink(BaseFilter):
    """Filtr: xabar matnida istalgan internet havolasi borligini tekshiradi."""

    async def __call__(self, message: Message) -> bool:
        return extract_url(message.text) is not None


# =============================================================================
# 3. Xatoliklarni foydalanuvchi tili (o'zbekcha) ga "tarjima" qilish
# =============================================================================


def friendly_error(raw_error: str) -> str:
    """
    yt-dlp beran texnik xatolik matnini chiroyli va tushunarli
    o'zbekcha xabarga aylantiradi.
    """
    text = (raw_error or "").lower()

    if "max-filesize" in text or "larger than" in text:
        return (
            f"⚠️ Video juda katta! Telegram cheklovi — {MAX_FILESIZE_MB} MB.\n"
            "Qisqaroq video yuboring yoki limitni .env faylida oshiring."
        )
    if "unsupported url" in text or "no video could be downloaded" in text:
        return (
            "⚠️ Bu havola qo'llab-quvvatlanmaydi.\n"
            "Iltimos, to'g'ridan-to'g'ri Instagram ilovasidan "
            "\"Havolani nusxalash\" orqali olingan havolani yuboring."
        )
    if any(
        key in text
        for key in ("private", "login", "sign in", "signin", "cookies", "auth")
    ):
        return (
            "🔒 Bu kontent shaxsiy (private) yoki tizimga kirishni talab qiladi.\n"
            "Bunday videolarni yuklab bo'lmaydi — ochiq (public) postlarni yuboring."
        )
    if any(
        key in text
        for key in ("video unavailable", "not available", "404", "has been removed")
    ):
        return "🕵️ Video topilmadi yoki o'chirilgan bo'lishi mumkin. Havolani tekshiring."
    if any(key in text for key in ("429", "rate-limit", "rate limit", "too many requests")):
        return (
            "⏳ Instagram tomonidan vaqtinchalik cheklov qo'yildi.\n"
            "Birozdan so'ng qayta urinib ko'ring."
        )
    if "403" in text or "forbidden" in text:
        return (
            "🚫 Instagram kirishni rad etdi. Bir necha daqiqadan keyin "
            "qayta urinib ko'ring yoki COOKIES_FILE sozlamasini yoqing."
        )
    if "geo" in text or "not available in your country" in text:
        return "🌍 Bu video Sizning hududingizda mavjud emas."

    # Noma'lum xatolik — qisqa texnik tushuntirish bilan
    short = raw_error.strip().replace("ERROR:", "").strip()
    if len(short) > 200:
        short = short[:200] + "..."
    base = "❌ Yuklab bo'lmadi. Havolani tekshirib, qayta urinib ko'ring."
    return f"{base}\n\nTexnik sabab: {short}" if short else base


# =============================================================================
# 4. Yordamchi funksiyalar: status xabari, "yuborilmoqda" indikatori, fayllar
# =============================================================================


async def _safe_edit(message: Message, text: str) -> None:
    """Status xabarini xavfsiz yangilaydi (xatolarni e'tiborsiz qoldiradi)."""
    try:
        await message.edit_text(text)
    except TelegramAPIError:
        pass  # xabar o'zgarmagan bo'lsa yoki allaqachon o'chirilgan bo'lsa — muhim emas


async def _safe_delete(message: Message) -> None:
    """Xabarni xavfsiz o'chiradi."""
    try:
        await message.delete()
    except TelegramAPIError:
        pass


async def _notify_admin(bot: Bot, text: str) -> None:
    """
    Adminga xabar yuboradi (boshlanish va kutilmagan xatolik haqida).
    Xatolik bo'lsa — faqat logga yoziladi, bot ishlashda davom etadi.
    """
    if not ADMIN_ID:
        return  # admin sozlanmagan
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text)
    except TelegramAPIError as exc:
        logger.warning("Adminga xabar yuborilmadi: %s", exc)


async def _keep_chat_action(bot: Bot, chat_id: int) -> None:
    """
    Yuklab olish va yuborish davomida foydalanuvchida
    "yuborilmoqda..." indikatori (chayqaluvchi qalamchasi) yoqib turadi.
    """
    while True:
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
        except TelegramAPIError:
            pass
        await asyncio.sleep(4)  # har 4 soniyada bir marta yangilaymiz


def _build_ydl_opts(
    tmpdir: str, status: Message, loop: asyncio.AbstractEventLoop
) -> dict:
    """yt-dlp uchun sozlamalar to'plamini tuzadi (progress callback bilan)."""

    # Progress holatini saqlaymiz: xabar chastotasi Telegram chegarasiga mos bo'lishi
    state = {"last_percent": -1, "last_update": 0.0}

    def progress_hook(data: dict) -> None:
        """Yuklab olish foizini status xabarida ko'rsatadi (thread ichida ishlaydi)."""
        if data.get("status") != "downloading":
            return

        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        done = data.get("downloaded_bytes") or 0
        if not total:
            return

        percent = min(100, int(done * 100 / total))
        now = time.monotonic()

        # Telegram ~1 sekundda 1 ta tahrirlashga ruxsat beradi — cheklaymiz
        if percent == state["last_percent"] or (now - state["last_update"]) < 2.0:
            return
        state["last_percent"] = percent
        state["last_update"] = now

        text = f"⏳ Video yuklab olinmoqda... {percent}%\nIltimos, kuting..."
        # Hook oddiy (bloklovchi) potokda ishlaydi, shuning uchun tayyor koroutinani
        # asosiy event-loop'ga uzatamiz (run_coroutine_threadsafe).
        asyncio.run_coroutine_threadsafe(_safe_edit(status, text), loop)

    opts: dict = {
        # Fayllar vaqtinchalik papkaga, ixcham nom bilan yoziladi
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        # Eng sifatli MP4: video+audio birlashtiriladi, bo'lmasa tayyor MP4
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        # Instagram karusellari (bir necha faylli post) ham yuklab olinadi
        "noplaylist": False,
        # Katta hajmli fayllarni oldindan tekshirish (tejash uchun)
        "max_filesize": MAX_FILESIZE_BYTES if MAX_FILESIZE_BYTES > 0 else None,
        # Barqarorlik: qayta urinishlar va parallel segmentlar
        "retries": 3,
        "fragment_retries": 3,
        "file_access_retries": 3,
        "concurrent_fragment_downloads": 4,
        # Chiqishni tozalash — faqat xatoliklar logging'ga yoziladi
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [progress_hook],
        # Chiqindi fayllarni yozmaslik
        "writethumbnail": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
    }

    # max_filesize=None bo'lsa, yt-dlp buni xato deb qabul qiladi — o'chiramiz
    if not opts["max_filesize"]:
        opts.pop("max_filesize")

    # Instagram cookies fayli ko'rsatilgan bo'lsa — ulamiz
    if COOKIES_FILE:
        if os.path.isfile(COOKIES_FILE):
            opts["cookiefile"] = COOKIES_FILE
            logger.info("Cookies fayli ulandi: %s", COOKIES_FILE)
        else:
            logger.warning("COOKIES_FILE topilmadi: %s", COOKIES_FILE)

    return opts


def _download_sync(
    url: str, tmpdir: str, status: Message, loop: asyncio.AbstractEventLoop
) -> None:
    """
    yt-dlp orqali haqiqiy yuklab olish jarayoni.
    BU FUNKSIYA BLOKLAYDI — shuning uchun uni albatta
    asyncio.to_thread() orqali alohida potokda chaqiramiz.
    """
    opts = _build_ydl_opts(tmpdir, status, loop)
    with YoutubeDL(opts) as ydl:
        # extract_info(..., download=True) — ma'lumotni olib, faylni yuklab oladi.
        # Qaytgan info dict ichida sarlavha, muallif, davomiyligi va h.k. bor —
        # ularni Gemini tavsifini tuzishda ishlatamiz.
        info = ydl.extract_info(url, download=True)
    return info if isinstance(info, dict) else {}


def _collect_files(tmpdir: str) -> List[Path]:
    """Yuklab olingan (yaratilgan) barcha fayllar ro'yxatini qaytaradi."""
    files: List[Path] = []
    for entry in sorted(Path(tmpdir).iterdir()):
        if not entry.is_file():
            continue
        if entry.suffix.lower() in SKIP_EXTENSIONS:  # .part, .ytdl va hokazo
            continue
        if entry.stat().st_size == 0:  # bo'sh fayl — yaramaydi
            continue
        files.append(entry)
    return files


def _split_by_size(files: Sequence[Path]) -> Tuple[List[Path], List[Path]]:
    """
    Fayllarni Telegram limitiga mos (sendable) va juda katta (oversized)
    guruhlarga ajratadi.
    """
    sendable: List[Path] = []
    oversized: List[Path] = []
    for path in files:
        size = path.stat().st_size
        if MAX_FILESIZE_BYTES and size > MAX_FILESIZE_BYTES:
            oversized.append(path)
        else:
            sendable.append(path)
    return sendable, oversized


def _is_image(path: Path) -> bool:
    """Fayl rasm ekanini aniqlaydi (rasm bo'lsa photo sifatida yuboriladi)."""
    return path.suffix.lower() in IMAGE_EXTENSIONS


def _chunked(seq: Sequence[Path], size: int) -> Iterator[List[Path]]:
    """Ro'yxatni `size` li bo'laklarga bo'ladi (Telegram media group uchun)."""
    for index in range(0, len(seq), size):
        yield list(seq[index : index + size])


def _remove_files(paths: Sequence[Path]) -> None:
    """Barcha vaqtinchalik fayllarni os.remove() bilan o'chiradi (xatosiz)."""
    for path in paths:
        try:
            os.remove(path)
            logger.debug("O'chirildi: %s", path)
        except FileNotFoundError:
            pass  # allaqachon o'chirilgan
        except OSError as exc:
            # Windows'da fayl hali band bo'lsa — xatolikni bosamiz
            logger.warning("Faylni o'chirib bo'lmadi %s: %s", path, exc)


# =============================================================================
# 4.5 Google Gemini: video haqida qisqacha qiziqarli tavsif (caption)
# =============================================================================

# Gemini uchun o'zbekcha prompt — video tahlili uchun
VIDEO_CAPTION_PROMPT = (
    "Quyida Instagram'dan yuklab olingan video kelyapti.\n"
    "Videoni diqqat bilan ko'rib chiq va uning kontenti haqida\n"
    "qisqacha, qiziqarli Telegram post (caption) yoz.\n\n"
    "Talablar:\n"
    "1) Til — o'zbek tili.\n"
    "2) 2-3 ta qisqa gap, tabiiy va jozibali bo'lsin.\n"
    "3) 2-4 ta mos emoji qo'sh.\n"
    "4) \"Caption:\", \"Tavsif:\" kabi sarlavha yozma.\n"
    "5) Havola yoki boshqa link yozma.\n"
    "6) 900 belgidan oshmasin."
)

# Gemini uchun zaxira prompt — faqat metama'lumot asosida (videosiz)
TEXT_CAPTION_PROMPT_TEMPLATE = (
    "Quyida Instagram videosi haqida metama'lumot berilgan.\n"
    "Shu ma'lumot asosida qisqacha, qiziqarli Telegram post (caption) yoz.\n\n"
    "Talablar:\n"
    "1) Til — o'zbek tili.\n"
    "2) 2-3 ta qisqa gap, tabiiy va jozibali.\n"
    "3) 2-4 ta mos emoji qo'sh.\n"
    "4) \"Caption:\" kabi sarlavha yozma, link yozma.\n"
    "5) 900 belgidan oshmasin.\n\n"
    "Ma'lumot:\n{meta}"
)


def _extract_metadata(info: dict) -> dict:
    """
    yt-dlp bergan info obyektidan metama'lumot ajratib oladi.
    Playlist (karusel) bo'lsa — birinchi element ma'lumotini oladi.
    """
    data = info if isinstance(info, dict) else {}
    entries = data.get("entries")
    if isinstance(entries, (list, tuple)):
        for item in entries:
            if isinstance(item, dict):
                data = item
                break

    duration = data.get("duration")
    try:
        duration_sec = int(duration) if duration else None
    except (TypeError, ValueError):
        duration_sec = None

    return {
        "title": str(data.get("title") or "").strip(),
        "uploader": str(data.get("uploader") or data.get("channel") or "").strip(),
        "duration": duration_sec,
        "description": str(data.get("description") or "").strip()[:500],
    }


def _format_metadata(meta: dict) -> str:
    """Metama'lumotni Gemini uchun chiroyli matnga aylantiradi."""
    lines = []
    if meta.get("title"):
        lines.append(f"Sarlavha: {meta['title']}")
    if meta.get("uploader"):
        lines.append(f"Muallif: {meta['uploader']}")
    if meta.get("duration"):
        lines.append(f"Davomiyligi: {meta['duration']} soniya")
    if meta.get("description"):
        lines.append(f"Tavsif: {meta['description']}")
    return "\n".join(lines) if lines else "Ma'lumot yo'q."


def _sanitize_caption(text: str) -> Optional[str]:
    """Gemini javobini Telegram caption qilib tayyorlaydi (tozalaydi)."""
    if not text:
        return None
    cleaned = text.strip()
    # Bir xil qatorlardan iborat bo'sh qatorlarni olib tashlaymiz
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    cleaned = "\n".join(lines)
    if not cleaned:
        return None
    # Telegram chegarasidan xavfsiz qoldiramiz (caption — 1024 belgi)
    if len(cleaned) > 950:
        cleaned = cleaned[:950].rstrip() + "..."
    return cleaned


def _default_caption(meta: dict, url: str) -> str:
    """Gemini ishlamasa ishlatiladigan oddiy, lekin foydali caption."""
    title = meta.get("title") or "Instagram videosi"
    return f"🎬 {title}\n\n🔗 Manba: {url}"


async def _delete_gemini_file(file_obj) -> None:
    """Gemini serveriga yuklangan faylni o'chiradi (serverni ham tozalaymiz)."""
    if genai_client is None or file_obj is None:
        return
    name = getattr(file_obj, "name", None)
    if not name:
        return
    try:
        await genai_client.aio.files.delete(name=name)
        logger.debug("Gemini fayli o'chirildi: %s", name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Gemini faylini o'chirib bo'lmadi: %s", exc)


async def _gemini_video_caption(video_path: Path) -> Optional[str]:
    """
    Videoni Gemini'ga yuklab (Files API), modelning o'zi uni tahlil qilib,
    qisqacha qiziqarli matn yozadi. Muvaffaqiyatsiz bo'lsa — None qaytaradi.
    """
    if genai_client is None:
        return None

    uploaded = None
    try:
        # 1) Videoni Gemini serveriga yuklaymiz (to'liq asinxron)
        uploaded = await genai_client.aio.files.upload(file=str(video_path))

        # 2) Fayl qayta ishlanishini kutamiz (video uchun biroz vaqt kerak)
        for _ in range(30):  # ~60 soniya gacha kutamiz
            state = getattr(uploaded, "state", None)
            state_name = str(getattr(state, "name", state) or "").upper()
            if state_name == "FAILED":
                logger.warning("Gemini faylni qayta ishlay olmadi")
                return None
            if state_name != "PROCESSING":
                break
            await asyncio.sleep(2)
            uploaded = await genai_client.aio.files.get(name=uploaded.name)

        # 3) Videdan tavsif so'raymiz
        response = await genai_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=[uploaded, VIDEO_CAPTION_PROMPT],
            config=genai_types.GenerateContentConfig(
                temperature=0.9,  # ijodiyroq matn uchun
                max_output_tokens=400,
            ),
        )
        return _sanitize_caption(getattr(response, "text", None) or "")
    except Exception as exc:  # noqa: BLE001 — Gemini xatolari botni to'xtatmasin
        logger.warning("Gemini video tahlili amalga oshmadi: %s", exc)
        return None
    finally:
        # 4) Serverdagi vaqtinchalik faylni o'chiramiz
        await _delete_gemini_file(uploaded)


async def _gemini_text_caption(meta: dict) -> Optional[str]:
    """
    Zaxira yo'l: video yuklab bo'lmasa (yoki juda katta bo'lsa),
    faqat metama'lumot (sarlavha, muallif, izoh) asosida tavsif yozadi.
    """
    if genai_client is None:
        return None
    try:
        prompt = TEXT_CAPTION_PROMPT_TEMPLATE.format(meta=_format_metadata(meta))
        response = await genai_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.9,
                max_output_tokens=400,
            ),
        )
        return _sanitize_caption(getattr(response, "text", None) or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Gemini matn tahlili amalga oshmadi: %s", exc)
        return None


async def generate_caption(video_path: Path, info: dict, url: str) -> str:
    """
    Telegram uchun caption tayyorlaydi. Navbat:
        1) Gemini videoning o'zini tahlil qilib yozadi;
        2) bo'lmasa — Gemini metama'lumot asosida yozadi;
        3) bo'lmasa — oddiy tayyor caption.
    Hech qachon xatolik uzatmaydi (bot qulab tushmaydi).
    """
    meta = _extract_metadata(info)
    caption: Optional[str] = None

    if genai_client is not None:
        try:
            caption = await asyncio.wait_for(
                _gemini_video_caption(video_path), timeout=GEMINI_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning("Gemini video tahlili vaqti tugadi (%ss)", GEMINI_TIMEOUT)

        if not caption:
            try:
                caption = await asyncio.wait_for(
                    _gemini_text_caption(meta), timeout=GEMINI_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning("Gemini matn javobi vaqti tugadi (%ss)", GEMINI_TIMEOUT)

    if not caption:
        return _default_caption(meta, url)
    return caption


async def _send_files(bot: Bot, chat_id: int, files: Sequence[Path], caption: str) -> None:
    """
    Yuklab olingan fayllarni Telegram'ga yuboradi:
        * 1 ta fayl → to'g'ridan-to'g'ri video yoki photo;
        * bir nechta fayl → media group (karusel) ko'rinishida.
    `caption` — Gemini yozgan (yoki zaxira) tavsif, u video bilan birga ketadi.
    """
    if not caption:
        caption = "🎬 Instagram videosi"
    if len(caption) > 1024:  # Telegram sarlavhasi chegarasi
        caption = caption[:1021] + "..."

    # --- 1 ta fayl ---
    if len(files) == 1:
        path = files[0]
        if _is_image(path):
            await bot.send_photo(chat_id=chat_id, photo=FSInputFile(path), caption=caption)
        else:
            await bot.send_video(
                chat_id=chat_id,
                video=FSInputFile(path),
                caption=caption,
                supports_streaming=True,
            )
        return

    # --- Bir nechta fayl: media group (10 tadan bo'laklarda) ---
    first_group = True
    for chunk in _chunked(files, MEDIA_GROUP_LIMIT):
        media = []
        for item in chunk:
            item_caption = caption if first_group else None
            if _is_image(item):
                media.append(
                    InputMediaPhoto(media=FSInputFile(item), caption=item_caption)
                )
            else:
                media.append(
                    InputMediaVideo(
                        media=FSInputFile(item),
                        caption=item_caption,
                        supports_streaming=True,
                    )
                )
        first_group = False

        try:
            await bot.send_media_group(chat_id=chat_id, media=media)
        except TelegramAPIError:
            # Media group yuborilmadi (masalan, aralash format) —
            # birma-bir yuborishga o'tamiz
            logger.exception("Media group yuborishda xatolik, yagona yuborishga o'tildi")
            for single in chunk:
                if _is_image(single):
                    await bot.send_photo(
                        chat_id=chat_id, photo=FSInputFile(single), caption=caption
                    )
                else:
                    await bot.send_video(
                        chat_id=chat_id,
                        video=FSInputFile(single),
                        caption=caption,
                        supports_streaming=True,
                    )


# =============================================================================
# 5. Bot handlerlari (aiogram v3 sintaksisi)
# =============================================================================


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """/start — bilish vaqti."""
    user_name = message.from_user.full_name if message.from_user else "do'st"
    await message.answer(
        f"👋 Salom, {user_name}!\n\n"
        "Men Instagram'dan video (post, reel, stories) yuklab olaman.\n\n"
        "📌 Qanday ishlaydi:\n"
        "1. Instagram'da video yoki postni oching.\n"
        '2. "Ulashish" → "Havolani nusxalash" tugmasini bosing.\n'
        "3. Havolani menga yuboring — men uni yuklab olib,\n"
        "   video ko'rinishida qaytaraman.\n\n"
        "⚡ Tezkor va bepul.\n"
        "ℹ️ Batafsil ma'lumot: /help"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """/help — qo'llanma."""
    await message.answer(
        "ℹ️ Yordam\n\n"
        "• Instagram post, reel, karusel (bir nechta rasm/video)\n"
        "  va stories havolalarini qo'llab-quvvatlayman.\n"
        "• Faqat ochiq (public) kontentni yuklab olish mumkin.\n"
        f"• Maksimal fayl hajmi: {MAX_FILESIZE_MB} MB.\n"
        "• Yuklab olingan fayllar serverda saqlab qolinmaydi —\n"
        "  Telegram'ga yuborilgandan so'ng darhol o'chiriladi.\n\n"
        "⚠️ Xatolik yuzaga kelsa: havolani yangilab, qayta yuboring."
    )


@dp.message(IsInstagramLink())
async def handle_instagram_link(message: Message, bot: Bot) -> None:
    """
    ASOSIY ishlov beruvchi: Instagram havolasi kelganda
    yuklab olish va Telegram'ga yuborish jarayoni.
    """
    url = extract_url(message.text) or ""
    logger.info("Havola olindi: %s (chat_id=%s)", url, message.chat.id)

    # 1) Foydalanuvchiga "kuting" statusini ko'rsatamiz
    status: Message = await message.reply(
        "⏳ Video yuklab olinmoqda, iltimos kuting..."
    )

    # 2) "Yuborilmoqda" indikatorini fon rejimida yoqamiz
    typing_task = asyncio.create_task(_keep_chat_action(bot, message.chat.id))

    loop = asyncio.get_running_loop()
    downloaded: List[Path] = []  # yuborilgandan keyin o'chiriladigan fayllar
    info: dict = {}  # yt-dlp ma'lumotlari (sarlavha, muallif va h.k.)

    # Vaqtinchalik papka — yakuniy holatda baribir o'chiriladi
    tmpdir = tempfile.mkdtemp(prefix="igbot_")

    try:
        try:
            # 3) yt-dlp BLOKLOVCHI funksiya — uni thread'ga chiqaramiz
            #    va maksimal kutish vaqti bilan cheklaymiz
            info = await asyncio.wait_for(
                asyncio.to_thread(_download_sync, url, tmpdir, status, loop),
                timeout=DOWNLOAD_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await message.reply(
                f"⌛ Kutish vaqti tugadi ({DOWNLOAD_TIMEOUT} soniya).\n"
                "Internet aloqasi yoki Instagram tomonidan kechikish bo'lishi mumkin. "
                "Qayta urinib ko'ring."
            )
            return
        except YoutubeDLError as exc:
            # yt-dlp xatolari: yaroqsiz havola, shaxsiy post, cheklov...
            logger.warning("yt-dlp xatoligi: %s", exc)
            await message.reply(friendly_error(str(exc)))
            return
        except OSError as exc:
            # Serverda fayl yozish/o'qishda muammo
            logger.exception("Fayl tizimi xatoligi")
            await message.reply(f"❌ Serverda faylni saqlashda xatolik: {exc}")
            return

        # 4) Yuklab olingan fayllarni topamiz
        downloaded = _collect_files(tmpdir)
        if not downloaded:
            await message.reply(
                "❌ Hech qanday fayl yuklab bo'lmadi.\n"
                "Havola noto'g'ri yoki kontent mavjud emas bo'lishi mumkin."
            )
            return

        # 5) Telegram limitiga moslikni tekshiramiz
        sendable, oversized = _split_by_size(downloaded)
        if not sendable:
            biggest = max(oversized, key=lambda p: p.stat().st_size)
            size_mb = biggest.stat().st_size / (1024 * 1024)
            await message.reply(
                f"⚠️ Video juda katta — {size_mb:.1f} MB.\n"
                f"Telegram cheklovi: {MAX_FILESIZE_MB} MB. Bunday katta faylni "
                "yuborib bo'lmaydi."
            )
            return

        # 6) Gemini yordamida caption tayyorlash (agar imkoni bo'lsa)
        #    Bu ham to'liq asinxron — boshqa foydalanuvchilar kutmaydi.
        await _safe_edit(status, "🤖 Gemini video haqida tavsif yozmoqda...")
        main_file = next((f for f in sendable if not _is_image(f)), sendable[0])
        caption = await generate_caption(main_file, info, url)

        # 7) Telegram'ga yuborish (video + Gemini tavsifi caption sifatida)
        try:
            await _send_files(bot, message.chat.id, sendable, caption)
        except TelegramAPIError as exc:
            logger.exception("Telegram'ga yuborishda xatolik")
            await message.reply(
                "❌ Faylni Telegram'ga yuborib bo'lmadi.\n"
                "Iltimos, bir necha daqiqadan keyin qayta urinib ko'ring."
            )
            logger.debug("Telegram API xatoligi: %s", exc)
            return

        # 8) Agar bir qismi limitdan katta bo'lsa — foydalanuvchiga aytiladi
        if oversized:
            await message.reply(
                f"ℹ️ {len(oversized)} ta fayl {MAX_FILESIZE_MB} MB dan katta "
                "bo'lgani uchun yuborilmadi."
            )

    except Exception as exc:  # noqa: BLE001 — bot hech qachon qulab tushmasligi kerak
        logger.exception("Kutilmagan xatolik yuz berdi")
        await message.reply(
            "❌ Kutilmagan xatolik yuz berdi. Iltimos, keyinroq qayta urinib ko'ring."
        )
        # Adminni ogohlantiramiz (chat_id, havola va texnik sabab bilan)
        await _notify_admin(
            bot,
            "🚨 Botda kutilmagan xatolik\n"
            f"Chat: {message.chat.id}\n"
            f"Havola: {url}\n"
            f"Sabab: {type(exc).__name__}: {exc}",
        )
    finally:
        # 8) TOZALASH: yuklab olingan fayllarni darhol o'chiramiz
        _remove_files(downloaded)
        shutil.rmtree(tmpdir, ignore_errors=True)  # papkaning o'zi ham ketadi

        # "Yuborilmoqda" indikatorini o'chiramiz
        typing_task.cancel()

        # 9) "Video yuklab olinmoqda..." status xabarini o'chiramiz
        await _safe_delete(status)

        logger.info("Jarayon yakunlandi: %s", url)


@dp.message(IsWebLink())
async def handle_other_link(message: Message) -> None:
    """Boshqa sayt havolasi kelganda — muloqotli xabar."""
    await message.reply(
        "⚠️ Kechirasiz, men faqat Instagram havolalarini qo'llab-quvvatlayman.\n\n"
        "Masalan:\n"
        "• https://www.instagram.com/reel/XXXXXXX\n"
        "• https://www.instagram.com/p/XXXXXXX\n"
        "• https://www.instagram.com/stories/username/XXXXXXX"
    )


# Barcha matnli xabarlar uchun oxirgi handler (buyruqlar va havoladan keyin) —
# aiogram handlerlarni tartibda tekshiradi, shuning uchun bu eng oxirida turadi.
@dp.message()
async def handle_any_text(message: Message) -> None:
    """Oddiy matn yoki noma'lum kontent kelganda — yo'naltiruvchi javob."""
    text = (message.text or "").strip()
    if text.startswith("/"):
        await message.reply(
            "🤔 Noma'lum buyruq.\nMavjud buyruqlar: /start va /help"
        )
        return
    await message.reply(
        "🤔 Instagram video havolasini yuboring.\n\n"
        "Ulashish → \"Havolani nusxalash\" tugmasini bosing va "
        "havolani shu yerga yuboring.\n\n"
        "ℹ️ Yordam: /help"
    )


# =============================================================================
# 6. Botni ishga tushirish
# =============================================================================


async def main() -> None:
    """Botni ishga tushiradi va doimiy so'rov (polling) rejimida ushlab turadi."""
    bot = Bot(token=BOT_TOKEN)
    logger.info("Bot ishga tushdi. Polling boshlandi...")

    # Adminga ishga tushgani haqida xabar beramiz (agar ADMIN_ID sozlangan bo'lsa)
    await _notify_admin(
        bot,
        "✅ Instagram Downloader boti ishga tushdi!\n"
        f"Fayl limiti: {MAX_FILESIZE_MB} MB\n"
        f"Kutish vaqti: {DOWNLOAD_TIMEOUT} soniya",
    )

    try:
        # allowed_updates — faqat ishlatiladigan update turlari so'raladi
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        # Gemini async client yopiladi (resurslarni erkinlashtirish uchun)
        if genai_client is not None:
            try:
                await genai_client.aio.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Gemini client yopilmadi: %s", exc)
        await bot.session.close()
        logger.info("Bot to'xtatildi.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Foydalanuvchi tomonidan to'xtatildi.")

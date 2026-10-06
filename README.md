# 📲 Instagram Downloader Telegram Boti

Foydalanuvchi Instagram'dan (post, reel, carousel, stories) havola yuboradi —
bot **yt-dlp** orqali kontentni tezda yuklab olib, Telegram'ga **video fayl
ko'rinishida** to'g'ridan-to'g'ri jo'natadi. Yuborilgandan so'ng fayllar
**darhol o'chiriladi** (serverda saqlanmaydi).

## 🧰 Texnologiyalar

| Kutubxona | Vazifasi |
|---|---|
| **aiogram 3** | Telegram boti, to'liq asinxron (asyncio) |
| **yt-dlp** | Instagram/youtube va boshqa saytlardan video yuklab olish |
| **google-genai** | Google Gemini API — video uchun qiziqarli tavsif yozish |
| **python-dotenv** | `.env` fayldan maxfiy sozlamalarni o'qish |

## 📁 Loyiha tuzilishi

```
.
├── bot.py           # Botning to'liq kodi
├── requirements.txt # Kutubxonalar ro'yxati
├── .env.example     # Namunaviy konfiguratsiya (nusxalab .env qiling)
└── README.md        # Ushbu fayl
```

## 🚀 Ishga tushirish qadamlari

### 1. Talablar
- **Python 3.10+** (https://www.python.org/downloads/)
- Telegram bot tokeni — [@BotFather](https://t.me/BotFather) dan ` /newbot `
  buyrug'i bilan olinadi.

### 2. Virtual muhit yaratish (tavsiya etiladi)

Windows:
```bash
python -m venv venv
venv\Scripts\activate
```

Linux / macOS:
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Kutubxonalarni o'rnatish

```bash
pip install -r requirements.txt
```

> 💡 Instagram'dagi ayrim videolarni eng sifatli ko'rinishida birlashtirish uchun
> **ffmpeg** ham o'rnatilgan bo'lishi maqsadga muvofiq (ixtiyoriy):
> Windows — `winget install ffmpeg`, Ubuntu — `sudo apt install ffmpeg`.

### 4. Konfiguratsiya

```bash
cp .env.example .env    # Windows'da: copy .env.example .env
```

Keyin `.env` faylini ochib, kamida **BOT_TOKEN** qiymatini kiriting:

```env
BOT_TOKEN=123456789:AAFxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
MAX_FILESIZE_MB=50
DOWNLOAD_TIMEOUT=300
COOKIES_FILE=
LOG_LEVEL=INFO
```

| O'zgaruvchi | Tavsifi |
|---|---|
| `BOT_TOKEN` | Majburiy. @BotFather'dan olinadigan token |
| `ADMIN_ID` | Ixtiyoriy. Admin ID — ishga tushish va xatolik haqida xabar oladi |
| `GEMINI_API_KEY` | Ixtiyoriy. Gemini kaliti — AI tavsif (caption) uchun |
| `GEMINI_MODEL` | Gemini modeli (standart: `gemini-2.5-flash`) |
| `GEMINI_TIMEOUT` | Gemini javobi uchun kutish chegarasi (soniya) |
| `MAX_FILESIZE_MB` | Telegram'ga yuboriladigan maksimal fayl hajmi (MB) |
| `DOWNLOAD_TIMEOUT` | Bitta video uchun kutish chegarasi (soniya) |
| `COOKIES_FILE` | Ixtiyoriy. Shaxsiy cheklangan kontent uchun cookies.txt |
| `LOG_LEVEL` | Log darajasi: `INFO`, `DEBUG`, ... |

### 5. Botni ishga tushirish

```bash
python bot.py
```

Endi Telegram'da botingizga `/start` yozing va Instagram havolasini yuboring.

## ⚙️ Qanday ishlaydi

1. Foydalanuvchi havola yuboradi → bot uni Instagram havolasi ekanini tekshiradi.
2. Foydalanuvchiga `⏳ Video yuklab olinmoqda, iltimos kuting...` xabari chiqadi.
3. yt-dlp **alohida potokda** (`asyncio.to_thread`) ishga tushiriladi —
   bot boshqa foydalanuvchilarga javob bera oladi, status esa foiz ko'rinishida
   yangilanadi.
4. Agar `GEMINI_API_KEY` sozlangan bo'lsa — **Google Gemini** video o'zini
   tahlil qilib, o'zbekcha qisqacha qiziqarli tavsif yozadi (bo'lmasa —
   metama'lumot asosida, yana bo'lmasa — oddiy tavsif).
5. Fayl Telegram limitiga (50 MB) mos tekshiriladi va video **Gemini tavsifi
   caption sifatida** yuboriladi. Karusel postlar media group ko'rinishida ketadi.
6. `finally` blokida: status xabari o'chiriladi, fayllar `os.remove()` bilan
   serverdan **darhol** olib tashlanadi (Gemini serveridagi fayl ham o'chiriladi).

## 🛡️ Xatoliklarni boshqarish

| Holat | Natija |
|---|---|
| Yaroqsiz/noto'g'ri havola | Tushunarli xabar, bot ishlashda davom etadi |
| Shaxsiy (private) post | "Kontent shaxsiy" degan xabar |
| Juda katta video | Hajm chegarasi haqida xabar |
| Instagram cheklovi (429/403) | "Birozdan keyin qayta urinib ko'ring" |
| Kutish vaqti tugadi | Timeout xabari |
| Gemini API xatoligi/kalit yo'q | Oddiy tavsif bilan yuboriladi, bot ishlayveradi |
| Kutilmagan xatolik | Umumiy xabar + logga yoziladi |

## 🔒 Xavfsizlik

- Token va boshqa maxfiy sozlamalar faqat `.env` faylda (u `.gitignore`da).
- Vaqtinchalik fayllar har bir so'rovdan keyin o'chiriladi.

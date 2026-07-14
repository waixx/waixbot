import logging
import os
import json
import sys
import asyncio
import re
from datetime import datetime
import aiohttp
import requests
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
)

load_dotenv()

# --- КОНФИГ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
APISERPENT_API_KEY = os.getenv("APISERPENT_API_KEY")

try:
    ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
except ValueError:
    ADMIN_USER_ID = 0

MAX_HISTORY = int(os.getenv("MAX_HISTORY", "30"))
KEEP_RECENT = int(os.getenv("KEEP_RECENT", "10"))
MODEL_DEFAULT = os.getenv("MODEL_DEFAULT", "deepseek-v4-flash")
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")

# --- ТЕКУЩАЯ ДАТА ---
NOW = datetime.now()
CURRENT_DATE = NOW.strftime("%d.%m.%Y")
CURRENT_TIME = NOW.strftime("%H:%M")
CURRENT_YEAR = NOW.year
CURRENT_MONTH = NOW.month

# --- ПРОВЕРКА ПЕРЕМЕННЫХ ---
if not TELEGRAM_TOKEN or not DEEPSEEK_API_KEY:
    print("❌ TELEGRAM_TOKEN или DEEPSEEK_API_KEY не заданы")
    sys.exit(1)

print("\n" + "=" * 50)
print("🚀 БОТ ЗАПУЩЕН")
print("=" * 50)
print(f"  🤖 TELEGRAM_TOKEN: {'✅' if TELEGRAM_TOKEN else '❌'}")
print(f"  🔑 DEEPSEEK_API_KEY: {'✅' if DEEPSEEK_API_KEY else '❌'}")
print(f"  🔍 APISERPENT_API_KEY: {'✅' if APISERPENT_API_KEY else '❌'}")
print(f"  👤 ADMIN_USER_ID: {ADMIN_USER_ID}")
print(f"  📅 Текущая дата: {CURRENT_DATE} {CURRENT_TIME}")
print("=" * 50 + "\n")

# --- ПАМЯТЬ ---
os.makedirs("data", exist_ok=True)
MEMORY_FILE = "data/memory.json"

def compress_history(history):
    if len(history) <= MAX_HISTORY:
        return history
    recent = history[-KEEP_RECENT:]
    old = history[:-KEEP_RECENT]
    summary = []
    for msg in old:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            summary.append(f"Пользователь: {content[:150]}")
        elif role == "assistant":
            summary.append(f"Ассистент: {content[:150]}")
    if summary:
        return [{"role": "system", "content": "Краткая выжимка диалога:\n" + "\n".join(summary[-5:])}] + recent
    return recent

def load_memory(user_id):
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return compress_history(data.get(str(user_id), []))
    except Exception as e:
        print(f"⚠️ Ошибка загрузки памяти: {e}")
    return []

def save_memory(user_id, history):
    try:
        data = {}
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        data[str(user_id)] = compress_history(history)
        with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"✅ Память сохранена ({len(history)} сообщений)")
    except Exception as e:
        print(f"❌ Ошибка сохранения памяти: {e}")

# --- ПОИСК В ИНТЕРНЕТЕ ---
def search_apiserpent(query):
    if not APISERPENT_API_KEY:
        return []
    try:
        response = requests.get(
            "https://apiserpent.com/api/search",
            params={"q": query, "engine": "google", "num": 5},
            headers={"X-API-Key": APISERPENT_API_KEY},
            timeout=15
        )
        if response.status_code != 200:
            return []
        data = response.json()
        results = []
        if "results" in data and isinstance(data["results"], dict):
            results = data["results"].get("organic", [])
        elif "organic_results" in data:
            results = data["organic_results"]
        elif isinstance(data.get("results"), list):
            results = data["results"]
        formatted = []
        for r in results[:5]:
            if isinstance(r, dict):
                formatted.append({
                    "title": str(r.get("title", "Без названия"))[:200],
                    "snippet": str(r.get("snippet", r.get("description", "Нет описания")))[:300],
                    "link": str(r.get("url", r.get("link", "#")))[:200]
                })
        return formatted
    except Exception as e:
        print(f"❌ Ошибка поиска: {e}")
        return []

# --- АНАЛИЗ: МОЖЕТ ЛИ ОТВЕТ УСТАРЕТЬ ---
def is_time_sensitive(query):
    """
    Определяет, может ли информация по запросу устареть.
    Если ДА — бот пойдёт в интернет.
    """
    q = query.lower()

    # Статичные темы (НЕ устаревают)
    static_patterns = [
        r'\bматематик\b', r'\bуравнени[ея]\b', r'\bформул[аы]\b',
        r'\bфизик\b', r'\bхими\w*\b', r'\bгравитаци\w*\b',
        r'\bзакон\b', r'\bтеорем\w*\b',
        r'\bклассик\w*\b', r'\bантичн\w*\b', r'\bдревн\w*\b',
        r'\bисторическ\w*\b', r'\bсредневеков\w*\b',
        r'\bкто такой\b', r'\bкто такая\b',
        r'\bбиографи\w*\b', r'\bродилс\w*\b', r'\bумер\w*\b',
        r'\bпроизведени\w*\b', r'\bкниг\w*\b', r'\bроман\b',
        r'\bстих\w*\b', r'\bпоэм\w*\b',
        r'\bперевод\w*\b', r'\bсмысл\b', r'\bопределени\w*\b',
        r'\bчто такое\b', r'\bчто значит\b',
        r'\bкак работает\b', r'\bпринцип\b',
    ]
    for pattern in static_patterns:
        if re.search(pattern, q):
            return False

    # Темы, которые МОГУТ устареть
    dynamic_keywords = [
        'погод', 'температур', 'дожд', 'снег', 'ветер', 'градус',
        'врем', 'час', 'минут', 'дата', 'сегодня', 'завтра', 'вчера', 'сейчас',
        'новост', 'событи', 'происшеств', 'авар', 'выбор', 'кризис', 'войн',
        'курс', 'доллар', 'евро', 'юань', 'биткоин', 'криптовалют',
        'матч', 'счет', 'побед', 'спорт', 'футбол', 'хоккей', 'баскетбол',
        'акции', 'биржа', 'котировки', 'индекс',
        'президент', 'правительств', 'реформа', 'закон',
        'релиз', 'обновлени', 'анонс', 'презентаци',
        'концерт', 'премьер', 'фестиваль',
        'завтра', 'на этой неделе', 'в следующем',
        'сегодня вечером', 'завтра утром',
    ]
    for kw in dynamic_keywords:
        if kw in q:
            return True

    # Годы
    years = re.findall(r'\b(19[0-9]{2}|20[0-9]{2})\b', q)
    for y in years:
        if int(y) >= CURRENT_YEAR - 1:
            return True

    # Дни недели
    weekdays = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье']
    today = NOW.strftime('%A').lower()
    ru_weekdays = {
        'monday': 'понедельник', 'tuesday': 'вторник', 'wednesday': 'среда',
        'thursday': 'четверг', 'friday': 'пятница', 'saturday': 'суббота', 'sunday': 'воскресенье'
    }
    today_ru = ru_weekdays.get(today, today)
    for day in weekdays:
        if day in q and day != today_ru:
            return True

    # Если вопрос начинается с "когда", "во сколько" — скорее всего актуально
    if re.search(r'\bкогда\b|\bво сколько\b', q):
        return True

    return False

# --- HTTP СЕССИЯ ---
def create_session():
    return aiohttp.TCPConnector(
        limit=100,
        limit_per_host=30,
        keepalive_timeout=30,
        enable_cleanup_closed=True
    ), aiohttp.ClientTimeout(total=60, connect=10, sock_read=30)

async def ask_deepseek(messages, retries=3):
    connector, timeout = create_session()
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.post(
                    f"{DEEPSEEK_API_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                    json={"model": MODEL_DEFAULT, "messages": messages},
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"], None
                    if resp.status == 429:
                        await asyncio.sleep(min(2 ** attempt, 30))
                        continue
                    if resp.status == 401:
                        return None, "❌ Ошибка авторизации API. Проверьте DEEPSEEK_API_KEY."
                    if resp.status == 500:
                        return None, "⚠️ Внутренняя ошибка сервера DeepSeek. Попробуйте позже."
                    return None, f"❌ Ошибка API ({resp.status}): {await resp.text()}"
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            return None, "❌ Ошибка соединения с API DeepSeek."
        except Exception as e:
            return None, f"❌ Неизвестная ошибка: {str(e)}"
    return None, "❌ Превышено количество попыток."

# --- КОМАНДЫ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    await update.message.reply_text(
        f"🤖 Привет! Я бот на DeepSeek-V4 Flash.\n\n"
        f"📅 Сегодня: {CURRENT_DATE} {CURRENT_TIME}\n\n"
        "🧠 **Я проверяю актуальность своих знаний:**\n"
        "• Если вопрос статичный (математика, факты, классика) — отвечаю сразу.\n"
        "• Если информация может быть устаревшей — ищу в интернете.\n\n"
        "🌐 Когда ищу — пишу: «Актуализирую информацию в интернете...»\n"
        "✅ Затем даю проверенный ответ.\n\n"
        "📋 Команды:\n"
        "  /model — модель\n"
        "  /clear — очистить историю\n"
        "  /stats — статистика\n"
        "  /date — показать дату\n\n"
        "💡 Просто задавай вопросы!"
    )

async def date_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    await update.message.reply_text(
        f"📅 Текущая дата и время:\n\n"
        f"📆 {NOW.strftime('%d.%m.%Y')}\n"
        f"🕐 {NOW.strftime('%H:%M:%S')}\n"
        f"📅 {NOW.strftime('%A')}\n"
        f"📅 {NOW.strftime('%B')} {NOW.year}"
    )

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    keyboard = [[InlineKeyboardButton("⚡ Flash", callback_data=MODEL_DEFAULT)]]
    await update.message.reply_text(
        f"✅ Модель: **{MODEL_DEFAULT}**",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if str(user_id) in data:
            del data[str(user_id)]
            with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            await update.message.reply_text("🧹 История очищена.")
        else:
            await update.message.reply_text("📭 История пуста.")
    else:
        await update.message.reply_text("📭 История пуста.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    history = load_memory(user_id)
    await update.message.reply_text(
        f"📊 Статистика:\n"
        f"📝 Сообщений: {len(history)}\n"
        f"💾 Максимум: {MAX_HISTORY}\n"
        f"📌 Сохраняется: {KEEP_RECENT}\n"
        f"📅 Текущая дата: {CURRENT_DATE}"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == MODEL_DEFAULT:
        await query.edit_message_text(f"✅ Модель: **{MODEL_DEFAULT}**")

# --- ОСНОВНОЙ ОБРАБОТЧИК ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return

    user_message = update.message.text
    await update.message.chat.send_action(action=ChatAction.TYPING)

    history = load_memory(user_id)

    # Определяем, нужно ли искать в интернете
    need_search = is_time_sensitive(user_message)

    # Если пользователь явно написал "бро" — принудительный поиск
    if user_message.lower().startswith("бро "):
        need_search = True
        user_message = user_message[4:].strip()
        if not user_message:
            await update.message.reply_text("❌ Напиши, что искать после 'бро'.")
            return

    # --- ЕСЛИ НУЖЕН ПОИСК В ИНТЕРНЕТЕ ---
    if need_search and user_message:
        # Первое сообщение — уведомление о поиске
        status_msg = await update.message.reply_text("🌐 Актуализирую информацию в интернете...")

        results = search_apiserpent(user_message)

        if not results:
            await status_msg.edit_text("⚠️ Не удалось найти актуальную информацию. Отвечаю из базы знаний.")
            # Отвечаем из базы, но с учётом даты
            system = {"role": "system", "content": f"Сегодня: {CURRENT_DATE} {CURRENT_TIME}. Отвечай на русском языке."}
            history.append({"role": "user", "content": user_message})
            answer, error = await ask_deepseek([system] + history)
            if error:
                await update.message.reply_text(error)
                return
            history.append({"role": "assistant", "content": answer})
            save_memory(user_id, history)
            await update.message.reply_text(answer)
            return

        # Формируем ответ на основе поиска
        search_text = f"📅 Сегодня: {CURRENT_DATE} {CURRENT_TIME}\n\n"
        search_text += f"🔍 Результаты по запросу: '{user_message}'\n\n"
        for i, r in enumerate(results[:5], 1):
            search_text += f"{i}. **{r['title']}**\n   {r['snippet']}\n   🔗 {r['link']}\n\n"

        system_prompt = {
            "role": "system",
            "content": f"""Ты — полезный ассистент. Сегодня: {CURRENT_DATE} {CURRENT_TIME}.

Пользователь спросил: "{user_message}"

Я проверил актуальность в интернете и нашёл свежие данные:
{search_text}

ПРАВИЛА:
1. Отвечай ТОЛЬКО на основе найденных данных.
2. Если информация совпадает с твоими знаниями — подтверди.
3. Если данные обновились — дай новую информацию.
4. Всегда указывай источники (ссылки).
5. Отвечай на русском языке.

Твой ответ должен быть проверенным и актуальным на {CURRENT_DATE}."""
        }

        history.append({"role": "user", "content": user_message})
        messages = [system_prompt] + history

        answer, error = await ask_deepseek(messages)
        await status_msg.delete()  # Удаляем сообщение о поиске

        if error:
            await update.message.reply_text(error)
            return

        history.append({"role": "assistant", "content": answer})
        save_memory(user_id, history)
        await update.message.reply_text(answer)
        return

    # --- ОБЫЧНЫЙ ОТВЕТ (БЕЗ ПОИСКА) ---
    system = {"role": "system", "content": f"Сегодня: {CURRENT_DATE} {CURRENT_TIME}. Отвечай на русском языке."}
    history.append({"role": "user", "content": user_message})
    answer, error = await ask_deepseek([system] + history)

    if error:
        await update.message.reply_text(error)
        return

    history.append({"role": "assistant", "content": answer})
    save_memory(user_id, history)
    await update.message.reply_text(answer)

# --- ОБРАБОТЧИК ОШИБОК ---
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raise context.error
    except Exception as e:
        print(f"⚠️ Ошибка: {e}")
        import traceback
        traceback.print_exc()
        if update and update.effective_message:
            await update.effective_message.reply_text("⚠️ Произошла ошибка. Попробуйте позже.")

# --- ЗАПУСК ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("date", date_command))
    app.add_handler(CommandHandler("model", model_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    print(f"✅ Бот запущен. Текущая дата: {CURRENT_DATE}")
    app.run_polling()

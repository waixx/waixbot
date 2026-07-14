import logging
import os
import json
import sys
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

# ============================================================
# 1. КОНФИГУРАЦИЯ
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
APISERPENT_API_KEY = os.getenv("APISERPENT_API_KEY")

try:
    ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
except ValueError:
    ADMIN_USER_ID = 0

# --- НАСТРОЙКИ ---
MAX_HISTORY = 40
KEEP_RECENT = 15
MODEL_DEFAULT = "deepseek-v4-flash"
DEEPSEEK_API_BASE = "https://api.deepseek.com/v1"

# --- ТЕКУЩАЯ ДАТА ---
NOW = datetime.now()
CURRENT_DATE = NOW.strftime("%d.%m.%Y")
CURRENT_TIME = NOW.strftime("%H:%M")
CURRENT_YEAR = NOW.year

# --- ПРОВЕРКА ---
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
print("=" * 50 + "\n")

# ============================================================
# 2. ПАМЯТЬ (ПРОСТАЯ И НАДЁЖНАЯ)
# ============================================================

os.makedirs("data", exist_ok=True)
MEMORY_FILE = "data/memory.json"
PROFILE_FILE = "data/user_profile.json"

# --- ПРОСТОЙ ПРОФИЛЬ ---
def load_profile(user_id):
    """Загружает профиль пользователя"""
    try:
        if os.path.exists(PROFILE_FILE):
            with open(PROFILE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get(str(user_id), {})
    except Exception as e:
        print(f"⚠️ Ошибка загрузки профиля: {e}")
    return {}

def save_profile(user_id, profile):
    """Сохраняет профиль пользователя"""
    try:
        data = {}
        if os.path.exists(PROFILE_FILE):
            with open(PROFILE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        
        profile["updated"] = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        data[str(user_id)] = profile
        
        with open(PROFILE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"❌ Ошибка сохранения профиля: {e}")
        return False

def get_profile_value(user_id, key, default=None):
    """Получает значение из профиля"""
    profile = load_profile(user_id)
    return profile.get(key, default)

def set_profile_value(user_id, key, value):
    """Устанавливает значение в профиле"""
    profile = load_profile(user_id)
    profile[key] = value
    save_profile(user_id, profile)
    return profile

# --- ИСТОРИЯ ДИАЛОГОВ ---
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
            summary.append(f"U: {content[:80]}")
        elif role == "assistant":
            summary.append(f"A: {content[:80]}")
    if summary:
        return [{"role": "system", "content": "Сжатая история:\n" + "\n".join(summary[-4:])}] + recent
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
    except Exception as e:
        print(f"❌ Ошибка сохранения памяти: {e}")

# ============================================================
# 3. ПОИСК В ИНТЕРНЕТЕ
# ============================================================

def search_apiserpent(query):
    if not APISERPENT_API_KEY:
        return []
    try:
        response = requests.get(
            "https://apiserpent.com/api/search",
            params={"q": query, "engine": "google", "num": 5},
            headers={"X-API-Key": APISERPENT_API_KEY},
            timeout=10
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
                    "title": str(r.get("title", "Без названия"))[:150],
                    "snippet": str(r.get("snippet", r.get("description", "Нет описания")))[:250],
                    "link": str(r.get("url", r.get("link", "#")))[:150]
                })
        return formatted
    except Exception as e:
        print(f"❌ Ошибка поиска: {e}")
        return []

def is_time_sensitive(query):
    q = query.lower()
    static = ['математик', 'уравнени', 'физик', 'хими', 'гравитаци', 'закон', 'теорем',
              'классик', 'античн', 'древн', 'историческ', 'средневеков',
              'кто такой', 'кто такая', 'биографи', 'родилс', 'умер',
              'произведени', 'книг', 'роман', 'стих', 'поэм',
              'что такое', 'что значит', 'как работает']
    for word in static:
        if word in q:
            return False
    
    dynamic = ['погод', 'температур', 'дожд', 'снег', 'ветер', 'градус',
               'врем', 'час', 'минут', 'дата', 'сегодня', 'завтра', 'вчера', 'сейчас',
               'новост', 'событи', 'происшеств', 'авар', 'выбор', 'кризис', 'войн',
               'курс', 'доллар', 'евро', 'юань', 'биткоин',
               'матч', 'счет', 'спорт', 'футбол', 'хоккей',
               'акции', 'биржа', 'котировки', 'индекс',
               'президент', 'правительств', 'реформа', 'закон',
               'релиз', 'обновлени', 'анонс']
    for word in dynamic:
        if word in q:
            return True
    
    years = re.findall(r'\b(19[0-9]{2}|20[0-9]{2})\b', q)
    for y in years:
        if int(y) >= CURRENT_YEAR - 1:
            return True
    return False

# ============================================================
# 4. ЗАПРОС К DEEPSEEK
# ============================================================

async def ask_deepseek(messages, retries=3):
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{DEEPSEEK_API_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                    json={"model": MODEL_DEFAULT, "messages": messages},
                    timeout=30
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"], None
                    if resp.status == 429:
                        import asyncio
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return None, f"❌ Ошибка API ({resp.status})"
        except Exception as e:
            if attempt < retries - 1:
                continue
            return None, f"❌ Ошибка: {str(e)}"
    return None, "❌ Превышено количество попыток."

# ============================================================
# 5. КОМАНДЫ БОТА
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    name = get_profile_value(user_id, "name", "друг")
    await update.message.reply_text(
        f"👋 Привет, {name}!\n\n"
        f"📅 Сегодня: {CURRENT_DATE} {CURRENT_TIME}\n\n"
        "🧠 **Я запоминаю:**\n"
        "• `запомни имя: Алексей`\n"
        "• `запомни город: Москва`\n"
        "• `запомни люблю кофе`\n\n"
        "📋 Команды:\n"
        "• `/profile` — что я помню\n"
        "• `/forget` — забыть всё\n"
        "• `/stats` — статистика\n\n"
        "🔍 Поиск: просто спроси или напиши `бро погода`"
    )

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    profile = load_profile(user_id)
    
    if not profile:
        await update.message.reply_text("📭 Я пока ничего не знаю о тебе. Расскажи что-нибудь!")
        return
    
    lines = ["🧠 **Что я помню о тебе:**\n"]
    
    # Показываем все ключи
    for key, value in profile.items():
        if key == "updated":
            continue
        if isinstance(value, list):
            lines.append(f"• **{key}:** {', '.join(value)}")
        else:
            lines.append(f"• **{key}:** {value}")
    
    lines.append(f"\n🔄 **Обновлено:** {profile.get('updated', 'неизвестно')}")
    
    await update.message.reply_text("\n".join(lines))

async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    save_profile(user_id, {})
    save_memory(user_id, [])
    await update.message.reply_text("🧹 **Я забыл всё, что знал о тебе!**")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    history = load_memory(user_id)
    profile = load_profile(user_id)
    
    await update.message.reply_text(
        f"📊 **Статистика:**\n\n"
        f"💬 Сообщений в истории: {len(history)}\n"
        f"📋 Фактов в профиле: {len(profile)}\n"
        f"🔄 Обновлён: {profile.get('updated', 'неизвестно')}"
    )

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    save_memory(user_id, [])
    await update.message.reply_text("🧹 История диалогов очищена.")

# ============================================================
# 6. ГЛАВНЫЙ ОБРАБОТЧИК
# ============================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    user_message = update.message.text
    
    # --- ОБРАБОТКА "ЗАПОМНИ" ---
    if user_message.lower().startswith("запомни "):
        text = user_message[8:].strip()
        
        if ":" in text:
            key, value = text.split(":", 1)
            key = key.strip()
            value = value.strip()
            set_profile_value(user_id, key, value)
            await update.message.reply_text(f"✅ **Запомнил:** {key} = {value}")
        else:
            # Сохраняем как простой факт
            profile = load_profile(user_id)
            if "факты" not in profile:
                profile["факты"] = []
            profile["факты"].append(text)
            save_profile(user_id, profile)
            await update.message.reply_text(f"✅ **Запомнил факт:** {text}")
        return
    
    # --- ОБЫЧНЫЙ ОТВЕТ ---
    history = load_memory(user_id)
    
    # Проверяем, нужен ли поиск
    need_search = is_time_sensitive(user_message)
    if user_message.lower().startswith("бро "):
        need_search = True
        user_message = user_message[4:].strip()
        if not user_message:
            await update.message.reply_text("❌ Напиши, что искать после 'бро'.")
            return
    
    # --- СИСТЕМНЫЙ ПРОМПТ ---
    profile = load_profile(user_id)
    system_parts = [f"Сегодня: {CURRENT_DATE} {CURRENT_TIME}"]
    
    for key, value in profile.items():
        if key == "updated":
            continue
        if isinstance(value, list):
            system_parts.append(f"{key}: {', '.join(value)}")
        else:
            system_parts.append(f"{key}: {value}")
    
    system_prompt = ". ".join(system_parts)
    system_msg = {"role": "system", "content": system_prompt}
    
    # --- ПОИСК ---
    if need_search and user_message:
        status_msg = await update.message.reply_text("🌐 **Ищу в интернете...**")
        
        results = search_apiserpent(user_message)
        
        if not results:
            await status_msg.edit_text("⚠️ Не нашёл в интернете. Отвечаю сам.")
            history.append({"role": "user", "content": user_message})
            answer, error = await ask_deepseek([system_msg] + history)
            if error:
                await update.message.reply_text(error)
                return
            history.append({"role": "assistant", "content": answer})
            save_memory(user_id, history)
            await update.message.reply_text(answer)
            return
        
        # Формируем ответ на основе поиска
        search_text = f"Результаты поиска:\n\n"
        for i, r in enumerate(results[:5], 1):
            search_text += f"{i}. {r['title']}\n   {r['snippet'][:200]}\n   {r['link']}\n\n"
        
        search_prompt = {
            "role": "system",
            "content": f"""Сегодня: {CURRENT_DATE}.

Вопрос: "{user_message}"

{search_text}

ОТВЕЧАЙ ТОЛЬКО НА ОСНОВЕ НАЙДЕННЫХ ДАННЫХ."""
        }
        
        history.append({"role": "user", "content": user_message})
        messages = [system_msg, search_prompt] + history
        
        answer, error = await ask_deepseek(messages)
        await status_msg.delete()
        
        if error:
            await update.message.reply_text(error)
            return
        
        history.append({"role": "assistant", "content": answer})
        save_memory(user_id, history)
        await update.message.reply_text(answer)
        return
    
    # --- ОБЫЧНЫЙ ОТВЕТ ---
    history.append({"role": "user", "content": user_message})
    messages = [system_msg] + history
    
    answer, error = await ask_deepseek(messages)
    
    if error:
        await update.message.reply_text(error)
        return
    
    history.append({"role": "assistant", "content": answer})
    save_memory(user_id, history)
    await update.message.reply_text(answer)

# ============================================================
# 7. ЗАПУСК
# ============================================================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raise context.error
    except Exception as e:
        print(f"⚠️ Ошибка: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    
    print("✅ Бот запущен!")
    app.run_polling()

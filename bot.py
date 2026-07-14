import logging
import os
import json
import sys
import re
import hashlib
from datetime import datetime, timedelta
import asyncio
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

ALLOWED_USERS_STR = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS_LIST = []
if ALLOWED_USERS_STR:
    try:
        ALLOWED_USERS_LIST = [int(x.strip()) for x in ALLOWED_USERS_STR.split(",") if x.strip()]
    except ValueError:
        print("⚠️ Ошибка в ALLOWED_USERS")

if ADMIN_USER_ID != 0 and ADMIN_USER_ID not in ALLOWED_USERS_LIST:
    ALLOWED_USERS_LIST.append(ADMIN_USER_ID)

# --- НАСТРОЙКИ ПАМЯТИ (КАСКАДНОЕ СЖАТИЕ) ---
MAX_HISTORY = 80
KEEP_RECENT = 20
COMPRESS_INTERVAL = 40
MAX_LONG_TERM_ITEMS = 20

# Уровень 2: Среднесрочная память (архив 1000)
MEDIUM_MAX_ITEMS = 1000
MEDIUM_COMPRESSED = 50

# Уровень 3: Долгосрочная память (архив 10000)
LONG_MAX_ITEMS = 10000
LONG_COMPRESSED = 100

# --- АДАПТИВНЫЙ TTL ---
DEFAULT_TTL = {
    'critical': 60,
    'instructional': 720,
    'important': 720,
    'static': 1440,
    'personal': 999999,
    'default': 1440
}

MODEL_DEFAULT = "deepseek-v4-flash"
DEEPSEEK_API_BASE = "https://api.deepseek.com/v1"

NOW = datetime.now()
CURRENT_DATE = NOW.strftime("%d.%m.%Y")
CURRENT_TIME = NOW.strftime("%H:%M")
CURRENT_YEAR = NOW.year

# --- ПРОВЕРКА ---
if not TELEGRAM_TOKEN or not DEEPSEEK_API_KEY:
    print("❌ TELEGRAM_TOKEN или DEEPSEEK_API_KEY не заданы")
    sys.exit(1)

print("\n" + "=" * 50)
print("🚀 БОТ ЗАПУЩЕН (МАКСИМАЛЬНАЯ ПАМЯТЬ + АВТО-ПОИСК)")
print("=" * 50)
print(f"  🤖 TELEGRAM_TOKEN: {'✅' if TELEGRAM_TOKEN else '❌'}")
print(f"  🔑 DEEPSEEK_API_KEY: {'✅' if DEEPSEEK_API_KEY else '❌'}")
print(f"  🔍 APISERPENT_API_KEY: {'✅' if APISERPENT_API_KEY else '❌'}")
print(f"  👤 ADMIN_USER_ID: {ADMIN_USER_ID}")
print(f"  👥 Разрешённых пользователей: {len(ALLOWED_USERS_LIST)}")
print(f"  📊 Память: 80 → 1000 → 10000 сообщений")
print(f"  🧠 Авто-поиск по истории: ВКЛЮЧЁН")
print("=" * 50 + "\n")

# ============================================================
# 2. ПАМЯТЬ (МНОГОУРОВНЕВАЯ)
# ============================================================

os.makedirs("data", exist_ok=True)
MEMORY_FILE = "data/memory.json"
PROFILE_FILE = "data/user_profile.json"

def is_allowed(user_id):
    if not ALLOWED_USERS_LIST:
        return True
    return user_id in ALLOWED_USERS_LIST

def load_profile(user_id):
    try:
        if os.path.exists(PROFILE_FILE):
            with open(PROFILE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get(str(user_id), {})
    except Exception as e:
        print(f"⚠️ Ошибка загрузки профиля: {e}")
    return {}

def save_profile(user_id, profile):
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
    profile = load_profile(user_id)
    return profile.get(key, default)

def set_profile_value(user_id, key, value):
    profile = load_profile(user_id)
    profile[key] = value
    save_profile(user_id, profile)
    return profile

# ============================================================
# 3. КЛЮЧЕВЫЕ СЛОВА ДЛЯ ПОИСКА В ПАМЯТИ
# ============================================================

MEMORY_SEARCH_TRIGGERS = [
    'помнишь', 'помните', 'помнишь ли', 'ты помнишь',
    'когда я', 'раньше я', 'в прошлом', 'давно',
    'что я говорил', 'что я писал', 'мои вопросы',
    'что ты знаешь', 'что ты помнишь', 'вспомни',
    'напомни', 'что было', 'что мы обсуждали',
    'я спрашивал', 'я просил', 'я говорил',
    'недавно', 'на днях', 'вчера', 'сегодня утром'
]

def should_search_memory(query):
    q = query.lower()
    for trigger in MEMORY_SEARCH_TRIGGERS:
        if trigger in q:
            return True
    return False

# ============================================================
# 4. КАСКАДНОЕ СЖАТИЕ
# ============================================================

def extract_key_points(text, max_len=30):
    if len(text) <= max_len:
        return text
    
    stop_words = ['это', 'так', 'вот', 'ну', 'просто', 'очень', 'такой', 'какой-то']
    words = text.split()
    
    important = []
    for word in words:
        if word.lower() not in stop_words and len(word) > 2:
            important.append(word)
    
    result = ' '.join(important[:10])
    return result[:max_len] + "..."

def extract_keywords_aggressive(text, max_len=15):
    if len(text) <= max_len:
        return text
    
    important_words = []
    for word in text.split():
        if len(word) > 3 and word.lower() not in ['это', 'так', 'вот', 'ну']:
            important_words.append(word[:8])
    
    result = ' '.join(important_words[:5])
    return result[:max_len] + "..."

def compress_history(history):
    if len(history) <= MAX_HISTORY:
        return history
    
    recent = history[-KEEP_RECENT:]
    old = history[:-KEEP_RECENT]
    
    summary = []
    for msg in old[-10:]:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            summary.append(f"Q: {extract_key_points(content, 50)}")
        elif role == "assistant":
            summary.append(f"A: {extract_key_points(content, 50)}")
    
    if summary:
        return [{"role": "system", "content": "📚 Сжатая история:\n" + "\n".join(summary[-5:])}] + recent
    
    return recent

def update_medium_memory(user_id, messages):
    profile = load_profile(user_id)
    
    if "medium_memory" not in profile:
        profile["medium_memory"] = []
    
    compressed = []
    for msg in messages[-50:]:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            compressed.append(f"Q: {extract_key_points(content, 30)}")
        elif role == "assistant":
            compressed.append(f"A: {extract_key_points(content, 30)}")
    
    timestamp = datetime.now().strftime("%d.%m")
    for item in compressed:
        profile["medium_memory"].append(f"[{timestamp}] {item}")
    
    if len(profile["medium_memory"]) > MEDIUM_COMPRESSED:
        profile["medium_memory"] = profile["medium_memory"][-MEDIUM_COMPRESSED:]
    
    save_profile(user_id, profile)

def update_long_memory(user_id, messages):
    profile = load_profile(user_id)
    
    if "long_memory" not in profile:
        profile["long_memory"] = []
    
    compressed = []
    for msg in messages[-100:]:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            compressed.append(f"Q: {extract_keywords_aggressive(content, 20)}")
        elif role == "assistant":
            compressed.append(f"A: {extract_keywords_aggressive(content, 20)}")
    
    timestamp = datetime.now().strftime("%m.%d")
    for item in compressed:
        profile["long_memory"].append(f"[{timestamp}] {item}")
    
    if len(profile["long_memory"]) > LONG_COMPRESSED:
        profile["long_memory"] = profile["long_memory"][-LONG_COMPRESSED:]
    
    save_profile(user_id, profile)

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
        
        # Обновляем каскадную память
        if len(history) > MAX_HISTORY:
            old_messages = history[:-KEEP_RECENT]
            if old_messages:
                update_medium_memory(user_id, old_messages)
                profile = load_profile(user_id)
                if len(profile.get("medium_memory", [])) >= MEDIUM_COMPRESSED:
                    update_long_memory(user_id, old_messages)
        
        data[str(user_id)] = compress_history(history)
        with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ Ошибка сохранения памяти: {e}")

# ============================================================
# 5. ПОИСК ПО СТАРОЙ ПАМЯТИ (АВТОМАТИЧЕСКИЙ)
# ============================================================

def search_in_all_memory(user_id, query):
    profile = load_profile(user_id)
    results = []
    q = query.lower()
    
    # Проверяем long-term память
    long_term = profile.get("long_term_memory", [])
    for item in long_term:
        if q in item.lower():
            results.append(f"📚 {item}")
    
    # Проверяем medium память
    medium = profile.get("medium_memory", [])
    for item in medium:
        if q in item.lower():
            results.append(f"📖 {item}")
    
    # Проверяем long память
    long_mem = profile.get("long_memory", [])
    for item in long_mem:
        if q in item.lower():
            results.append(f"📕 {item}")
    
    # Проверяем историю
    history = load_memory(user_id)
    for msg in history[-20:]:
        content = msg.get("content", "")
        if q in content.lower():
            role = "👤" if msg.get("role") == "user" else "🤖"
            results.append(f"{role} {extract_key_points(content, 80)}")
    
    return results[:10]

# ============================================================
# 6. КЛАССИФИКАЦИЯ ВОПРОСОВ
# ============================================================

def classify_question(query):
    q = query.lower()
    
    critical = {
        'погода': ['погод', 'температур', 'дожд', 'снег', 'ветер', 'градус'],
        'курс': ['курс', 'доллар', 'евро', 'юань', 'биткоин', 'акции', 'биржа'],
        'новости': ['новост', 'событи', 'происшеств', 'выбор', 'кризис'],
        'время': ['врем', 'час', 'минут', 'дата', 'сейчас', 'который час'],
    }
    for category, keywords in critical.items():
        for keyword in keywords:
            if keyword in q:
                return 'critical', category
    
    instructional = ['как ', 'как сделать', 'как настроить', 'как установить', 
                     'как деплоить', 'инструкция', 'руководство']
    for word in instructional:
        if word in q:
            return 'instructional', 'инструкция'
    
    personal = ['имя', 'город', 'работа', 'возраст', 'интерес', 'люби', 'хобби']
    for word in personal:
        if word in q:
            return 'personal', 'личное'
    
    return 'static', 'общее'

# ============================================================
# 7. ПРОВЕРКА АКТУАЛЬНОСТИ
# ============================================================

def is_data_fresh_for_category(user_id, category_type):
    profile = load_profile(user_id)
    if category_type == 'personal':
        return True
    
    last_check_key = f"last_check_{category_type}"
    last_check = profile.get(last_check_key, "")
    
    if not last_check:
        return False
    
    try:
        last_check_time = datetime.strptime(last_check, "%d.%m.%Y %H:%M:%S")
        age_minutes = (datetime.now() - last_check_time).total_seconds() / 60
        ttl = DEFAULT_TTL.get(category_type, DEFAULT_TTL['default'])
        return age_minutes < ttl
    except:
        return False

def update_last_check(user_id, category_type):
    profile = load_profile(user_id)
    profile[f"last_check_{category_type}"] = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    save_profile(user_id, profile)

def has_relevant_data_in_memory(user_id, query):
    profile = load_profile(user_id)
    history = load_memory(user_id)
    q = query.lower()
    
    if profile:
        for key, value in profile.items():
            if key.startswith("last_check_") or key.startswith("update_history_"):
                continue
            if key in ["medium_memory", "long_memory", "long_term_memory", "answer_cache"]:
                continue
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and (item.lower() in q or q in item.lower()):
                        return True, f"из профиля ({key})"
            elif isinstance(value, str):
                if value.lower() in q or q in value.lower():
                    return True, f"из профиля ({key})"
    
    for msg in history[-10:]:
        if msg.get("role") == "assistant":
            content = msg.get("content", "").lower()
            if len(content) > 20 and (q in content or any(word in content for word in q.split()[:3])):
                return True, "из истории"
        if msg.get("role") == "user":
            content = msg.get("content", "").lower()
            if q in content or content in q:
                return True, "из истории"
    
    return False, None

def check_relevance_and_freshness(user_id, query):
    if query.lower().startswith("бро "):
        return True, "принудительный поиск", False, False
    
    category_type, category_name = classify_question(query)
    
    print(f"📊 Классификация: {category_type} ({category_name})")
    
    need_memory_search = should_search_memory(query)
    
    if need_memory_search:
        print(f"🔍 Триггер поиска по памяти: ДА")
        memory_results = search_in_all_memory(user_id, query)
        if memory_results:
            print(f"📂 Найдено в памяти: {len(memory_results)} результатов")
            return False, "найдено в старой памяти", True, memory_results
    
    has_data, data_source = has_relevant_data_in_memory(user_id, query)
    
    if has_data:
        print(f"📂 Данные найдены: {data_source}")
    else:
        print(f"📂 Данных в локальной базе нет")
    
    if category_type == 'personal':
        return False, f"личный вопрос", has_data, None
    
    is_fresh = is_data_fresh_for_category(user_id, category_type)
    
    if category_type == 'critical':
        if has_data and is_fresh:
            return False, f"критические данные свежие", True, None
        else:
            return True, f"критические данные {'устарели' if has_data else 'отсутствуют'}", has_data, None
    
    if category_type == 'instructional':
        if has_data and is_fresh:
            return False, f"инструкция свежая", True, None
        else:
            return True, f"инструкция {'устарела' if has_data else 'отсутствует'}", has_data, None
    
    if has_data and is_fresh:
        return False, f"данные есть и свежие", True, None
    else:
        return True, f"данных {'нет' if not has_data else 'устарели'}", has_data, None

# ============================================================
# 8. ПОИСК В ИНТЕРНЕТЕ
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

# ============================================================
# 9. ЗАПРОС К DEEPSEEK
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
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return None, f"❌ Ошибка API ({resp.status})"
        except Exception as e:
            if attempt < retries - 1:
                continue
            return None, f"❌ Ошибка: {str(e)}"
    return None, "❌ Превышено количество попыток."

# ============================================================
# 10. КОМАНДЫ БОТА
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    name = load_profile(user_id).get("name", "друг")
    await update.message.reply_text(
        f"👋 Привет, {name}!\n\n"
        f"📅 Сегодня: {CURRENT_DATE} {CURRENT_TIME}\n\n"
        "🧠 **Я помню ВСЁ:**\n"
        "• 📝 80 последних сообщений (полностью)\n"
        "• 📚 1000 сообщений (сжато)\n"
        "• 📖 10000+ сообщений (суть)\n\n"
        "🔍 **Я сам ищу в памяти:**\n"
        "Когда ты спрашиваешь: 'помнишь', 'напомни', 'что я говорил'\n\n"
        "📝 **Запоминай меня:**\n"
        "• `запомни имя: Алексей`\n"
        "• `запомни город: Москва`\n\n"
        "📋 **Команды:**\n"
        "• `/profile` — что я помню\n"
        "• `/stats` — статистика\n\n"
        "🔍 **Принудительный поиск:** `бро погода`"
    )

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    profile = load_profile(user_id)
    if not profile:
        await update.message.reply_text("📭 Я пока ничего не знаю о тебе.")
        return
    
    lines = ["🧠 **Что я помню о тебе:**\n"]
    
    for key, value in profile.items():
        if key.startswith("last_check_") or key.startswith("update_history_"):
            continue
        if key in ["medium_memory", "long_memory", "long_term_memory", "answer_cache"]:
            if key == "medium_memory" and value:
                lines.append(f"• 📚 1000 сообщений: {len(value)} пунктов")
            elif key == "long_memory" and value:
                lines.append(f"• 📖 10000+ сообщений: {len(value)} пунктов")
            continue
        if isinstance(value, list):
            if value:
                lines.append(f"• **{key}:** {', '.join(str(v)[:50] for v in value[:5])}")
        else:
            lines.append(f"• **{key}:** {str(value)[:50]}")
    
    lines.append(f"\n🔄 **Обновлено:** {profile.get('updated', 'неизвестно')}")
    await update.message.reply_text("\n".join(lines))

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    profile = load_profile(user_id)
    history = load_memory(user_id)
    
    medium = len(profile.get("medium_memory", []))
    long_mem = len(profile.get("long_memory", []))
    
    await update.message.reply_text(
        f"📊 **Статистика памяти:**\n\n"
        f"💬 Сообщений в истории: {len(history)}\n"
        f"📚 1000 сообщений (архив): {medium} пунктов\n"
        f"📖 10000+ сообщений (архив): {long_mem} пунктов\n"
        f"🔄 Обновлён: {profile.get('updated', 'неизвестно')}"
    )

# ============================================================
# 11. ГЛАВНЫЙ ОБРАБОТЧИК (АВТО-ПОИСК)
# ============================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not is_allowed(user_id):
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
            profile = load_profile(user_id)
            profile[key] = value
            save_profile(user_id, profile)
            await update.message.reply_text(f"✅ **Запомнил:** {key} = {value}")
        else:
            profile = load_profile(user_id)
            if "факты" not in profile:
                profile["факты"] = []
            profile["факты"].append(text)
            save_profile(user_id, profile)
            await update.message.reply_text(f"✅ **Запомнил факт:** {text}")
        return
    
    # --- ОСНОВНАЯ ЛОГИКА ---
    history = load_memory(user_id)
    
    need_search, reason, has_data, memory_results = check_relevance_and_freshness(user_id, user_message)
    
    print(f"🔍 Анализ: '{user_message[:50]}...'")
    print(f"   → Интернет: {'ДА' if need_search else 'НЕТ'} ({reason})")
    
    # --- СИСТЕМНЫЙ ПРОМПТ ---
    profile = load_profile(user_id)
    system_parts = [f"Сегодня: {CURRENT_DATE} {CURRENT_TIME}"]
    
    for key, value in profile.items():
        if key.startswith("last_check_") or key.startswith("update_history_"):
            continue
        if key in ["medium_memory", "long_memory", "long_term_memory", "answer_cache"]:
            continue
        if isinstance(value, list):
            if value:
                system_parts.append(f"{key}: {', '.join(str(v)[:50] for v in value[:3])}")
        else:
            system_parts.append(f"{key}: {str(value)[:50]}")
    
    system_prompt = ". ".join(system_parts)
    system_msg = {"role": "system", "content": system_prompt}
    
    # --- ЕСЛИ НАШЛИ В СТАРОЙ ПАМЯТИ ---
    if memory_results:
        memory_text = "\n".join(memory_results)
        memory_prompt = {
            "role": "system",
            "content": f"Нашёл в истории:\n{memory_text}\n\nОтветь на вопрос пользователя на основе этой информации."
        }
        history.append({"role": "user", "content": user_message})
        messages = [system_msg, memory_prompt] + history
        answer, error = await ask_deepseek(messages)
        if error:
            await update.message.reply_text(error)
            return
        history.append({"role": "assistant", "content": answer})
        save_memory(user_id, history)
        await update.message.reply_text(answer)
        return
    
    # --- ЕСЛИ НУЖЕН ПОИСК В ИНТЕРНЕТЕ ---
    if need_search:
        status_msg = await update.message.reply_text("🌐 **Ищу актуальную информацию...**")
        
        search_query = user_message
        if user_message.lower().startswith("бро "):
            search_query = user_message[4:].strip()
            if not search_query:
                await status_msg.edit_text("❌ Напиши, что искать после 'бро'.")
                return
        
        results = search_apiserpent(search_query)
        
        if not results:
            await status_msg.edit_text("⚠️ Не нашёл в интернете. Отвечаю из памяти.")
            history.append({"role": "user", "content": user_message})
            answer, error = await ask_deepseek([system_msg] + history)
            if error:
                await update.message.reply_text(error)
                return
            history.append({"role": "assistant", "content": answer})
            save_memory(user_id, history)
            await update.message.reply_text(answer)
            return
        
        search_text = f"🔍 Результаты поиска:\n\n"
        for i, r in enumerate(results[:5], 1):
            search_text += f"{i}. **{r['title']}**\n   {r['snippet'][:200]}\n   🔗 {r['link']}\n\n"
        
        category_type, _ = classify_question(search_query)
        
        search_prompt = {
            "role": "system",
            "content": f"""Сегодня: {CURRENT_DATE}.

Вопрос пользователя: "{search_query}"

{search_text}

ОТВЕЧАЙ ТОЛЬКО НА ОСНОВЕ НАЙДЕННЫХ ДАННЫХ.
Указывай источники. Отвечай на русском языке."""
        }
        
        history.append({"role": "user", "content": user_message})
        messages = [system_msg, search_prompt] + history
        
        answer, error = await ask_deepseek(messages)
        await status_msg.delete()
        
        if error:
            await update.message.reply_text(error)
            return
        
        update_last_check(user_id, category_type)
        
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
# 12. ЗАПУСК
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
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    
    print("✅ Бот запущен!")
    print(f"📊 Память: 80 → 1000 → 10000+ сообщений")
    print(f"🔍 Авто-поиск по памяти: ВКЛЮЧЁН")
    app.run_polling()

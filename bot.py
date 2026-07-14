import logging
import os
import json
import sys
import re
import hashlib
import asyncio
import aiohttp
import requests
from datetime import datetime, timedelta
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
# 1. КОНФИГУРАЦИЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
APISERPENT_API_KEY = os.getenv("APISERPENT_API_KEY")

try:
    ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
except ValueError:
    ADMIN_USER_ID = 0

# --- СПИСОК РАЗРЕШЁННЫХ ПОЛЬЗОВАТЕЛЕЙ ---
ALLOWED_USERS_STR = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS_LIST = []
if ALLOWED_USERS_STR:
    try:
        ALLOWED_USERS_LIST = [int(x.strip()) for x in ALLOWED_USERS_STR.split(",") if x.strip()]
    except ValueError:
        print("⚠️ Ошибка в ALLOWED_USERS")

if ADMIN_USER_ID != 0 and ADMIN_USER_ID not in ALLOWED_USERS_LIST:
    ALLOWED_USERS_LIST.append(ADMIN_USER_ID)

# ============================================================
# 2. ПИРАМИДАЛЬНАЯ ПАМЯТЬ (1 000 000+ СООБЩЕНИЙ)
# ============================================================

# УРОВЕНЬ 1: Быстрая память (всегда в промпте)
LEVEL_1 = {
    'max_history': 80,
    'keep_recent': 20,
    'compress_to': 20
}

# УРОВЕНЬ 2: Среднесрочная память (1000 сообщений)
LEVEL_2 = {
    'max_items': 1000,
    'compress_interval': 40,
    'compress_to': 50
}

# УРОВЕНЬ 3: Долгосрочная память (10000 сообщений)
LEVEL_3 = {
    'max_items': 10000,
    'compress_interval': 200,
    'compress_to': 100
}

# УРОВЕНЬ 4: Архивная память (100000 сообщений)
LEVEL_4 = {
    'max_items': 100000,
    'compress_interval': 1000,
    'compress_to': 200
}

# УРОВЕНЬ 5: Вечная память (1 000 000+ сообщений)
LEVEL_5 = {
    'max_items': 1000000,
    'compress_interval': 10000,
    'compress_to': 500
}

# --- АДАПТИВНЫЙ TTL ---
DEFAULT_TTL = {
    'critical': 60,
    'instructional': 720,
    'important': 720,
    'static': 1440,
    'personal': 999999,
    'default': 1440
}

MODEL_DEFAULT = os.getenv("MODEL_DEFAULT", "deepseek-v4-flash")
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")

NOW = datetime.now()
CURRENT_DATE = NOW.strftime("%d.%m.%Y")
CURRENT_TIME = NOW.strftime("%H:%M")
CURRENT_YEAR = NOW.year

# --- ПРОВЕРКА ОБЯЗАТЕЛЬНЫХ ПЕРЕМЕННЫХ ---
if not TELEGRAM_TOKEN or not DEEPSEEK_API_KEY:
    print("❌ TELEGRAM_TOKEN или DEEPSEEK_API_KEY не заданы")
    sys.exit(1)

print("\n" + "=" * 50)
print("🚀 БОТ ЗАПУЩЕН (ПИРАМИДАЛЬНАЯ ПАМЯТЬ: 1 000 000+)")
print("=" * 50)
print(f"  🤖 TELEGRAM_TOKEN: {'✅' if TELEGRAM_TOKEN else '❌'}")
print(f"  🔑 DEEPSEEK_API_KEY: {'✅' if DEEPSEEK_API_KEY else '❌'}")
print(f"  🔍 APISERPENT_API_KEY: {'✅' if APISERPENT_API_KEY else '❌'}")
print(f"  👤 ADMIN_USER_ID: {ADMIN_USER_ID}")
print(f"  👥 Разрешённых пользователей: {len(ALLOWED_USERS_LIST)}")
print(f"  📊 Пирамидальная память: 80 → 1000 → 10000 → 100000 → 1 000 000+")
print(f"  📅 Текущая дата: {CURRENT_DATE} {CURRENT_TIME}")
print("=" * 50 + "\n")

# ============================================================
# 3. ФАЙЛЫ ПАМЯТИ
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

# ============================================================
# 4. ПИРАМИДАЛЬНОЕ СЖАТИЕ
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

def extract_keywords_aggressive(text, max_len=20):
    if len(text) <= max_len:
        return text
    important_words = []
    for word in text.split():
        if len(word) > 3 and word.lower() not in ['это', 'так', 'вот', 'ну']:
            important_words.append(word[:8])
    result = ' '.join(important_words[:5])
    return result[:max_len] + "..."

def extract_keywords_ultra(text, max_len=12):
    if len(text) <= max_len:
        return text
    important_words = []
    for word in text.split():
        if len(word) > 3 and word.lower() not in ['это', 'так', 'вот', 'ну']:
            important_words.append(word[:5])
    result = ' '.join(important_words[:3])
    return result[:max_len] + "..."

# --- УРОВЕНЬ 1: Сжатие истории (80 сообщений) ---
def compress_history(history):
    if len(history) <= LEVEL_1['max_history']:
        return history
    
    recent = history[-LEVEL_1['keep_recent']:]
    old = history[:-LEVEL_1['keep_recent']]
    
    summary = []
    for msg in old[-10:]:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            summary.append(f"Q: {extract_key_points(content, 50)}")
        elif role == "assistant":
            summary.append(f"A: {extract_key_points(content, 50)}")
    
    if summary:
        return [{"role": "system", "content": "📚 История:\n" + "\n".join(summary[-5:])}] + recent
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
        
        # Каскадное сжатие
        if len(history) > LEVEL_1['max_history']:
            old_messages = history[:-LEVEL_1['keep_recent']]
            if old_messages:
                update_level_2(user_id, old_messages)
                
                profile = load_profile(user_id)
                if len(profile.get("level_2", [])) >= LEVEL_2['compress_to']:
                    update_level_3(user_id, old_messages)
                if len(profile.get("level_3", [])) >= LEVEL_3['compress_to']:
                    update_level_4(user_id, old_messages)
                if len(profile.get("level_4", [])) >= LEVEL_4['compress_to']:
                    update_level_5(user_id, old_messages)
        
        data[str(user_id)] = compress_history(history)
        with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ Ошибка сохранения памяти: {e}")

# --- УРОВЕНЬ 2: 1000 сообщений → 50 пунктов ---
def update_level_2(user_id, messages):
    profile = load_profile(user_id)
    if "level_2" not in profile:
        profile["level_2"] = []
    
    batch = messages[-LEVEL_2['compress_interval']:]
    compressed = []
    for msg in batch:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            compressed.append(f"Q: {extract_key_points(content, 30)}")
        elif role == "assistant":
            compressed.append(f"A: {extract_key_points(content, 30)}")
    
    timestamp = datetime.now().strftime("%d.%m")
    for item in compressed:
        profile["level_2"].append(f"[{timestamp}] {item}")
    
    if len(profile["level_2"]) > LEVEL_2['compress_to']:
        profile["level_2"] = profile["level_2"][-LEVEL_2['compress_to']:]
    
    save_profile(user_id, profile)

# --- УРОВЕНЬ 3: 10000 сообщений → 100 пунктов ---
def update_level_3(user_id, messages):
    profile = load_profile(user_id)
    if "level_3" not in profile:
        profile["level_3"] = []
    
    batch = messages[-LEVEL_3['compress_interval']:]
    compressed = []
    for msg in batch:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            compressed.append(f"Q: {extract_keywords_aggressive(content, 25)}")
        elif role == "assistant":
            compressed.append(f"A: {extract_keywords_aggressive(content, 25)}")
    
    timestamp = datetime.now().strftime("%m.%d")
    for item in compressed:
        profile["level_3"].append(f"[{timestamp}] {item}")
    
    if len(profile["level_3"]) > LEVEL_3['compress_to']:
        profile["level_3"] = profile["level_3"][-LEVEL_3['compress_to']:]
    
    save_profile(user_id, profile)

# --- УРОВЕНЬ 4: 100000 сообщений → 200 пунктов ---
def update_level_4(user_id, messages):
    profile = load_profile(user_id)
    if "level_4" not in profile:
        profile["level_4"] = []
    
    batch = messages[-LEVEL_4['compress_interval']:]
    compressed = []
    for msg in batch:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            compressed.append(f"Q: {extract_keywords_aggressive(content, 20)}")
        elif role == "assistant":
            compressed.append(f"A: {extract_keywords_aggressive(content, 20)}")
    
    timestamp = datetime.now().strftime("%m.%d")
    for item in compressed:
        profile["level_4"].append(f"[{timestamp}] {item}")
    
    if len(profile["level_4"]) > LEVEL_4['compress_to']:
        profile["level_4"] = profile["level_4"][-LEVEL_4['compress_to']:]
    
    save_profile(user_id, profile)

# --- УРОВЕНЬ 5: 1 000 000+ сообщений → 500 пунктов ---
def update_level_5(user_id, messages):
    profile = load_profile(user_id)
    if "level_5" not in profile:
        profile["level_5"] = []
    
    batch = messages[-LEVEL_5['compress_interval']:]
    compressed = []
    for msg in batch:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            compressed.append(f"Q: {extract_keywords_ultra(content, 15)}")
        elif role == "assistant":
            compressed.append(f"A: {extract_keywords_ultra(content, 15)}")
    
    timestamp = datetime.now().strftime("%y.%m")
    for item in compressed:
        profile["level_5"].append(f"[{timestamp}] {item}")
    
    if len(profile["level_5"]) > LEVEL_5['compress_to']:
        profile["level_5"] = profile["level_5"][-LEVEL_5['compress_to']:]
    
    save_profile(user_id, profile)

# ============================================================
# 5. ПОИСК ПО ВСЕЙ ПИРАМИДЕ ПАМЯТИ
# ============================================================

def search_in_pyramid(user_id, query):
    """Поиск по всем уровням пирамиды памяти"""
    profile = load_profile(user_id)
    results = []
    q = query.lower()
    
    # Уровень 1: История
    history = load_memory(user_id)
    for msg in history[-20:]:
        content = msg.get("content", "")
        if q in content.lower():
            role = "👤" if msg.get("role") == "user" else "🤖"
            results.append(f"{role} {extract_key_points(content, 80)}")
    
    # Уровень 2: 1000 сообщений
    for item in profile.get("level_2", []):
        if q in item.lower():
            results.append(f"📚 {item}")
    
    # Уровень 3: 10000 сообщений
    for item in profile.get("level_3", []):
        if q in item.lower():
            results.append(f"📖 {item}")
    
    # Уровень 4: 100000 сообщений
    for item in profile.get("level_4", []):
        if q in item.lower():
            results.append(f"📕 {item}")
    
    # Уровень 5: 1 000 000+ сообщений
    for item in profile.get("level_5", []):
        if q in item.lower():
            results.append(f"📗 {item}")
    
    return results[:15]

# ============================================================
# 6. УМНЫЙ АНАЛИЗАТОР СООБЩЕНИЙ
# ============================================================

async def analyze_message(user_id, user_message):
    """Анализирует сообщение и определяет тип"""
    q = user_message.lower().strip()
    
    # Приветствия (мгновенный ответ)
    simple_greetings = ['привет', 'здравствуй', 'здрасте', 'приветствую', 'салют', 'hello', 'hi', 'пока', 'до свидания']
    if q in simple_greetings or q in [g + '!' for g in simple_greetings] or q in [g + '.' for g in simple_greetings]:
        return {"type": "greeting", "action": "greeting", "needs_search": False, "needs_memory": False}
    
    # Личные вопросы (из профиля)
    personal_triggers = ['имя', 'город', 'работа', 'возраст', 'интерес', 'хобби', 'меня зовут', 'как меня зовут', 'где я живу']
    for trigger in personal_triggers:
        if trigger in q:
            return {"type": "personal", "action": "memory", "needs_search": False, "needs_memory": True}
    
    # Поиск в старой памяти
    memory_triggers = ['помнишь', 'ты помнишь', 'напомни', 'что я говорил', 'что я писал', 'вспомни', 'что ты знаешь обо мне']
    for trigger in memory_triggers:
        if trigger in q:
            return {"type": "memory_query", "action": "memory_search", "needs_search": False, "needs_memory": True}
    
    # Динамичные темы (интернет)
    dynamic_triggers = ['погод', 'температур', 'дожд', 'снег', 'ветер', 'курс', 'доллар', 'евро', 'новост', 'событи']
    for trigger in dynamic_triggers:
        if trigger in q:
            return {"type": "dynamic", "action": "internet", "needs_search": True, "needs_memory": False}
    
    # Инструкции (интернет)
    instructional_triggers = ['как ', 'как сделать', 'как настроить', 'как установить', 'инструкция', 'руководство', 'как задеплоить']
    for trigger in instructional_triggers:
        if trigger in q:
            return {"type": "instructional", "action": "internet", "needs_search": True, "needs_memory": False}
    
    # Если не определили → DeepSeek анализ
    analysis_prompt = {
        "role": "system",
        "content": """Определи тип сообщения. Ответь ТОЛЬКО JSON.

Типы: static, dynamic, personal, memory_query, greeting
Действия: internet, memory, memory_search, greeting

{"type": "...", "action": "..."}"""
    }
    
    messages = [analysis_prompt, {"role": "user", "content": user_message}]
    
    try:
        answer, error = await ask_deepseek(messages, retries=2, max_tokens=50)
        if error:
            return {"type": "static", "action": "memory", "needs_search": False, "needs_memory": True}
        
        json_match = re.search(r'\{.*\}', answer, re.DOTALL)
        if json_match:
            import json
            result = json.loads(json_match.group())
            action = result.get("action", "memory")
            return {
                "type": result.get("type", "static"),
                "action": action,
                "needs_search": action in ["internet"],
                "needs_memory": action in ["memory", "memory_search"]
            }
    except Exception as e:
        print(f"❌ Ошибка анализа: {e}")
    
    return {"type": "static", "action": "memory", "needs_search": False, "needs_memory": True}

# ============================================================
# 7. ПОИСК В ИНТЕРНЕТЕ (APISERPENT)
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
# 8. ЗАПРОС К DEEPSEEK API
# ============================================================

async def ask_deepseek(messages, retries=3, max_tokens=None):
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": MODEL_DEFAULT,
                    "messages": messages,
                    "temperature": 0.3
                }
                if max_tokens:
                    payload["max_tokens"] = max_tokens
                
                async with session.post(
                    f"{DEEPSEEK_API_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                    json=payload,
                    timeout=30
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"], None
                    if resp.status == 429:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    if resp.status == 401:
                        return None, "❌ Ошибка авторизации API. Проверьте DEEPSEEK_API_KEY."
                    if resp.status == 500:
                        return None, "⚠️ Внутренняя ошибка сервера DeepSeek. Попробуйте позже."
                    return None, f"❌ Ошибка API ({resp.status})"
        except aiohttp.ClientConnectionError:
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            return None, "❌ Ошибка соединения с API DeepSeek."
        except asyncio.TimeoutError:
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            return None, "❌ Превышен таймаут ожидания ответа от DeepSeek."
        except Exception as e:
            if attempt < retries - 1:
                continue
            return None, f"❌ Неизвестная ошибка: {str(e)}"
    return None, "❌ Превышено количество попыток."

# ============================================================
# 9. ГЕНЕРАЦИЯ ОТВЕТА
# ============================================================

async def generate_response(user_id, user_message, analysis_result, history, profile):
    """Генерирует финальный ответ на основе анализа"""
    
    # --- СБОР СИСТЕМНОГО ПРОМПТА ---
    system_parts = [f"Сегодня: {CURRENT_DATE} {CURRENT_TIME}"]
    
    # Ядро профиля
    for key, value in profile.items():
        if key.startswith("last_check_") or key.startswith("update_history_"):
            continue
        if key in ["level_2", "level_3", "level_4", "level_5", "answer_cache"]:
            continue
        if isinstance(value, list):
            if value:
                system_parts.append(f"{key}: {', '.join(str(v)[:50] for v in value[:3])}")
        else:
            system_parts.append(f"{key}: {str(value)[:50]}")
    
    # Добавляем сжатые уровни (компактно)
    if profile.get("level_2"):
        system_parts.append(f"📚 1000: {', '.join(profile['level_2'][-10:])}")
    if profile.get("level_3"):
        system_parts.append(f"📖 10000: {', '.join(profile['level_3'][-5:])}")
    
    system_prompt = ". ".join(system_parts)
    if len(system_prompt) > 800:
        system_prompt = system_prompt[:800] + "..."
    
    system_msg = {"role": "system", "content": system_prompt}
    
    action = analysis_result.get("action", "memory")
    
    # --- ПРИВЕТСТВИЕ (мгновенно) ---
    if action == "greeting":
        greetings = {
            'привет': '👋 Привет! Как дела?',
            'здравствуй': '👋 Здравствуйте! Чем могу помочь?',
            'пока': '👋 Пока! Было приятно пообщаться!',
            'спасибо': 'Пожалуйста! Всегда рад помочь! 🤗',
            'благодарю': 'Пожалуйста! Обращайся! 🤗'
        }
        for key, value in greetings.items():
            if key in user_message.lower():
                return value, False
        return "👋 Привет! Чем могу помочь?", False
    
    # --- ПОИСК В СТАРОЙ ПАМЯТИ ---
    if action == "memory_search":
        memory_results = search_in_pyramid(user_id, user_message)
        if memory_results:
            memory_text = "\n".join(memory_results[:10])
            prompt = {
                "role": "system",
                "content": f"Нашёл в истории:\n{memory_text}\n\nОтветь на вопрос пользователя на основе этой информации."
            }
            history.append({"role": "user", "content": user_message})
            messages = [system_msg, prompt] + history
            answer, error = await ask_deepseek(messages)
            if error:
                return f"⚠️ {error}", False
            return answer, True
        else:
            history.append({"role": "user", "content": user_message})
            messages = [system_msg] + history
            answer, error = await ask_deepseek(messages)
            if error:
                return f"⚠️ {error}", False
            return answer, True
    
    # --- ПОИСК В ИНТЕРНЕТЕ ---
    if action == "internet":
        results = search_apiserpent(user_message)
        if not results:
            history.append({"role": "user", "content": user_message})
            messages = [system_msg] + history
            answer, error = await ask_deepseek(messages)
            if error:
                return f"⚠️ {error}", False
            return answer, True
        
        search_text = f"🔍 Результаты поиска:\n\n"
        for i, r in enumerate(results[:5], 1):
            search_text += f"{i}. **{r['title']}**\n   {r['snippet'][:200]}\n   🔗 {r['link']}\n\n"
        
        search_prompt = {
            "role": "system",
            "content": f"""Сегодня: {CURRENT_DATE}.

Вопрос пользователя: "{user_message}"

{search_text}

ОТВЕЧАЙ ТОЛЬКО НА ОСНОВЕ НАЙДЕННЫХ ДАННЫХ.
Указывай источники. Отвечай на русском языке."""
        }
        
        history.append({"role": "user", "content": user_message})
        messages = [system_msg, search_prompt] + history
        answer, error = await ask_deepseek(messages)
        if error:
            return f"⚠️ {error}", False
        return answer, True
    
    # --- ОБЫЧНЫЙ ОТВЕТ (из памяти/базы) ---
    history.append({"role": "user", "content": user_message})
    messages = [system_msg] + history
    answer, error = await ask_deepseek(messages)
    if error:
        return f"⚠️ {error}", False
    return answer, True

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
        "🧠 **Пирамидальная память (1 000 000+ сообщений):**\n"
        "• 📝 80 последних (полностью)\n"
        "• 📚 1000 сообщений (сжато)\n"
        "• 📖 10000 сообщений (сжато)\n"
        "• 📕 100000 сообщений (сжато)\n"
        "• 📗 1 000 000+ сообщений (суть)\n\n"
        "📋 **Команды:**\n"
        "• `/profile` — что я помню\n"
        "• `/stats` — статистика\n"
        "• `/memory [текст]` — поиск в памяти\n"
        "• `/forget` — забыть всё\n\n"
        "🔍 **Принудительный поиск:** `бро погода`"
    )

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    profile = load_profile(user_id)
    if not profile:
        await update.message.reply_text("📭 Я пока ничего не знаю о тебе. Расскажи что-нибудь!")
        return
    
    lines = ["🧠 **Пирамидальная память:**\n"]
    
    # Уровни памяти
    level_labels = {
        'level_2': '📚 1000 сообщений',
        'level_3': '📖 10000 сообщений',
        'level_4': '📕 100000 сообщений',
        'level_5': '📗 1 000 000+ сообщений'
    }
    
    for key, label in level_labels.items():
        value = profile.get(key, [])
        lines.append(f"• {label}: {len(value)} пунктов")
    
    history = load_memory(user_id)
    lines.append(f"• 📝 80 последних: {len(history)} сообщений")
    
    # Личная информация
    lines.append(f"\n👤 **Личная информация:**")
    personal_keys = ['name', 'город', 'city', 'работа', 'job', 'возраст', 'age', 'интересы', 'interests']
    found = False
    for key in personal_keys:
        if key in profile:
            lines.append(f"• {key}: {profile[key]}")
            found = True
    if not found:
        lines.append("• Пока ничего не запомнил")
    
    lines.append(f"\n🔄 **Обновлено:** {profile.get('updated', 'неизвестно')}")
    await update.message.reply_text("\n".join(lines))

async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Поиск по всей пирамиде памяти"""
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "🔍 **Поиск в памяти:**\n"
            "Напиши: `/memory что искать`\n"
            "Например: `/memory погода`"
        )
        return
    
    query = ' '.join(context.args)
    results = search_in_pyramid(user_id, query)
    
    if not results:
        await update.message.reply_text(f"📭 Ничего не найдено по запросу: '{query}'")
        return
    
    lines = [f"🔍 **Результаты поиска:** '{query}'\n"]
    for i, result in enumerate(results[:10], 1):
        lines.append(f"{i}. {result}")
    
    if len(results) > 10:
        lines.append(f"\n... и ещё {len(results) - 10} результатов")
    
    await update.message.reply_text("\n".join(lines))

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    profile = load_profile(user_id)
    history = load_memory(user_id)
    
    level_labels = {
        'level_2': '📚 1000 сообщений',
        'level_3': '📖 10000 сообщений',
        'level_4': '📕 100000 сообщений',
        'level_5': '📗 1 000 000+ сообщений'
    }
    
    lines = ["📊 **Статистика памяти:**\n"]
    lines.append(f"• 📝 80 последних: {len(history)} сообщений")
    
    total_punkts = len(history)
    for key, label in level_labels.items():
        value = profile.get(key, [])
        lines.append(f"• {label}: {len(value)} пунктов")
        total_punkts += len(value)
    
    total_messages = total_punkts * 50  # Примерное количество
    lines.append(f"\n📊 **Всего в памяти:** ~{total_messages:,} сообщений")
    lines.append(f"🔄 **Обновлён:** {profile.get('updated', 'неизвестно')}")
    
    await update.message.reply_text("\n".join(lines))

async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Полная очистка памяти о пользователе"""
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    # Очищаем все уровни памяти
    save_profile(user_id, {})
    save_memory(user_id, [])
    await update.message.reply_text("🧹 **Я забыл всё, что знал о тебе!** Начинаем с чистого листа.")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очистка истории диалогов (без профиля)"""
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    save_memory(user_id, [])
    await update.message.reply_text("🧹 **История диалогов очищена.** (Профиль сохранён)")

# ============================================================
# 11. ГЛАВНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ
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
    
    # --- УМНЫЙ АНАЛИЗ ---
    analysis_result = await analyze_message(user_id, user_message)
    print(f"📊 Анализ: {analysis_result}")
    
    # --- ЗАГРУЗКА ДАННЫХ ---
    history = load_memory(user_id)
    profile = load_profile(user_id)
    
    # --- ГЕНЕРАЦИЯ ОТВЕТА ---
    answer, should_save = await generate_response(user_id, user_message, analysis_result, history, profile)
    
    # --- СОХРАНЕНИЕ ---
    if should_save:
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": answer})
        save_memory(user_id, history)
    
    await update.message.reply_text(answer)

# ============================================================
# 12. ОБРАБОТЧИК ОШИБОК
# ============================================================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raise context.error
    except Exception as e:
        print(f"⚠️ Глобальная ошибка: {e}")
        import traceback
        traceback.print_exc()
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ **Произошла ошибка.** Пожалуйста, попробуйте позже."
            )

# ============================================================
# 13. ЗАПУСК БОТА
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Регистрируем команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    
    print("=" * 50)
    print("✅ БОТ УСПЕШНО ЗАПУЩЕН!")
    print(f"📊 Пирамидальная память: 80 → 1000 → 10000 → 100000 → 1 000 000+")
    print(f"👥 Разрешённых пользователей: {len(ALLOWED_USERS_LIST)}")
    print(f"📅 Текущая дата: {CURRENT_DATE} {CURRENT_TIME}")
    print("=" * 50)
    
    app.run_polling()

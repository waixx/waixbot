import logging
import os
import json
import sys
import asyncio
import re
from datetime import datetime, timedelta
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

# --- ОПТИМАЛЬНЫЕ НАСТРОЙКИ ПАМЯТИ ---
MAX_HISTORY = 40          # Максимум сообщений в истории
KEEP_RECENT = 15          # Сколько последних сообщений сохранять полностью
MODEL_DEFAULT = "deepseek-v4-flash"
DEEPSEEK_API_BASE = "https://api.deepseek.com/v1"

# --- ТЕКУЩАЯ ДАТА ---
NOW = datetime.now()
CURRENT_DATE = NOW.strftime("%d.%m.%Y")
CURRENT_TIME = NOW.strftime("%H:%M")
CURRENT_YEAR = NOW.year

# --- ПРОВЕРКА ПЕРЕМЕННЫХ ---
if not TELEGRAM_TOKEN or not DEEPSEEK_API_KEY:
    print("❌ TELEGRAM_TOKEN или DEEPSEEK_API_KEY не заданы")
    sys.exit(1)

print("\n" + "=" * 50)
print("🚀 БОТ ЗАПУЩЕН (СУПЕР-ПАМЯТЬ + ОПТИМИЗАЦИЯ)")
print("=" * 50)
print(f"  🤖 TELEGRAM_TOKEN: {'✅' if TELEGRAM_TOKEN else '❌'}")
print(f"  🔑 DEEPSEEK_API_KEY: {'✅' if DEEPSEEK_API_KEY else '❌'}")
print(f"  🔍 APISERPENT_API_KEY: {'✅' if APISERPENT_API_KEY else '❌'}")
print(f"  👤 ADMIN_USER_ID: {ADMIN_USER_ID}")
print(f"  📅 Текущая дата: {CURRENT_DATE} {CURRENT_TIME}")
print(f"  💾 MAX_HISTORY: {MAX_HISTORY} сообщений")
print(f"  📌 KEEP_RECENT: {KEEP_RECENT} последних")
print("=" * 50 + "\n")

# ============================================================
# 2. ПАМЯТЬ (ФАЙЛЫ)
# ============================================================

os.makedirs("data", exist_ok=True)
MEMORY_FILE = "data/memory.json"
PROFILE_FILE = "data/user_profile.json"

# ============================================================
# 3. ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ (ПОСТОЯННАЯ ПАМЯТЬ)
# ============================================================

DEFAULT_PROFILE = {
    "persona": {
        "name": "",
        "age": "",
        "gender": "",
        "city": "",
        "job": "",
        "education": "",
        "relationship": ""
    },
    "preferences": {
        "style": "сбалансированный",
        "tone": "дружелюбный",
        "language": "ru",
        "formality": "неформальный"
    },
    "interests": [],
    "facts": {},
    "events": {},
    "context": {
        "last_topics": [],
        "last_mood": "",
        "last_activity": "",
        "favorite_topics": []
    },
    "stats": {
        "messages_count": 0,
        "first_seen": "",
        "last_seen": ""
    },
    "updated": ""
}

def load_profile(user_id):
    """Загружает профиль пользователя"""
    try:
        if os.path.exists(PROFILE_FILE):
            with open(PROFILE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                profile = data.get(str(user_id), {})
                if not profile or "persona" not in profile:
                    return DEFAULT_PROFILE.copy()
                return profile
    except Exception as e:
        print(f"⚠️ Ошибка загрузки профиля: {e}")
    return DEFAULT_PROFILE.copy()

def save_profile(user_id, profile):
    """Сохраняет профиль пользователя"""
    try:
        data = {}
        if os.path.exists(PROFILE_FILE):
            with open(PROFILE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        
        profile["updated"] = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        if not profile["stats"].get("first_seen"):
            profile["stats"]["first_seen"] = profile["updated"]
        profile["stats"]["last_seen"] = profile["updated"]
        
        data[str(user_id)] = profile
        
        with open(PROFILE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"❌ Ошибка сохранения профиля: {e}")
        return False

# ============================================================
# 4. ИНТЕЛЛЕКТУАЛЬНЫЙ АНАЛИЗ СООБЩЕНИЙ
# ============================================================

def analyze_and_update_profile(user_id, message):
    """Анализирует сообщение и обновляет профиль"""
    profile = load_profile(user_id)
    changed = False
    msg_lower = message.lower()
    
    # --- 4.1. ЯВНАЯ КОМАНДА "ЗАПОМНИ" ---
    if msg_lower.startswith("запомни "):
        text = message[8:].strip()
        if ":" in text:
            key, value = text.split(":", 1)
            key = key.strip().lower()
            value = value.strip()
            
            if key in ["имя", "возраст", "пол", "город", "работа", "должность", "образование", "статус"]:
                profile["persona"][key] = value
                changed = True
            elif key in ["стиль", "тон", "язык", "формальность"]:
                profile["preferences"][key] = value
                changed = True
            elif key in ["день рождения", "дата рождения", "годовщина", "праздник"]:
                profile["events"][key] = value
                changed = True
            else:
                profile["facts"][key] = value
                changed = True
            print(f"📝 Запомнил: {key} = {value}")
        else:
            # Просто факт
            key = f"факт_{len(profile['facts']) + 1}"
            profile["facts"][key] = text[:100]
            changed = True
            print(f"📝 Запомнил факт: {text[:50]}...")
    
    # --- 4.2. ИЗВЛЕЧЕНИЕ ИМЕНИ ---
    if not profile["persona"].get("name"):
        name_patterns = [
            r'(?:меня зовут|я|зовут|называй)\s+([А-Яа-яA-Za-z\s\-]{2,30})',
            r'(?:я|меня)\s+зовут\s+([А-Яа-яA-Za-z\s\-]{2,30})',
        ]
        for pattern in name_patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                name = match.group(1).strip()[:30]
                if len(name) > 1:
                    profile["persona"]["name"] = name
                    changed = True
                    print(f"📝 Извлёк имя: {name}")
                    break
    
    # --- 4.3. ИЗВЛЕЧЕНИЕ ВОЗРАСТА ---
    if not profile["persona"].get("age"):
        age_patterns = [
            r'мне\s+(\d{1,3})\s+(?:год|лет)',
            r'(\d{1,3})\s+лет',
            r'возраст\s+(\d{1,3})',
        ]
        for pattern in age_patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                profile["persona"]["age"] = match.group(1)
                changed = True
                print(f"📝 Извлёк возраст: {match.group(1)}")
                break
    
    # --- 4.4. ИЗВЛЕЧЕНИЕ ГОРОДА ---
    if not profile["persona"].get("city"):
        cities = ['москв', 'питер', 'спб', 'санкт-петербург', 'казан', 'новосиб', 'екатерин',
                  'нижн', 'самар', 'омск', 'челяб', 'ростов', 'уф', 'краснодар', 'воронеж',
                  'перм', 'волгоград', 'сочи', 'иркутск', 'тюмен', 'барнаул', 'владивосток']
        
        for city in cities:
            if city in msg_lower:
                match = re.search(r'(?:в|из|живу в|проживаю в)\s+([А-Яа-яA-Za-z\s\-]{2,30})(?:\s|,|\.|$)', message, re.IGNORECASE)
                if match:
                    city_name = match.group(1).strip().capitalize()
                    if len(city_name) > 1:
                        profile["persona"]["city"] = city_name
                        changed = True
                        print(f"📝 Извлёк город: {city_name}")
                        break
    
    # --- 4.5. ИЗВЛЕЧЕНИЕ ПРОФЕССИИ ---
    if not profile["persona"].get("job"):
        job_keywords = ['программист', 'разработчик', 'дизайнер', 'менеджер', 'маркетолог', 
                        'аналитик', 'юрист', 'врач', 'учитель', 'инженер', 'архитектор', 
                        'журналист', 'переводчик', 'психолог', 'предприниматель', 'фрилансер',
                        'студент', 'пенсионер', 'безработный']
        
        for job in job_keywords:
            if job in msg_lower:
                profile["persona"]["job"] = job
                changed = True
                print(f"📝 Извлёк профессию: {job}")
                break
    
    # --- 4.6. ИЗВЛЕЧЕНИЕ ИНТЕРЕСОВ (до 10) ---
    hobby_indicators = ['люблю', 'нравится', 'увлекаюсь', 'хобби', 'интересуюсь', 
                        'занимаюсь', 'в свободное время', 'обожаю', 'фанат']
    
    for word in hobby_indicators:
        if word in msg_lower:
            parts = re.split(rf'{word}\s+', msg_lower, maxsplit=1)
            if len(parts) > 1:
                hobby_text = parts[1].strip()
                hobby_text = re.sub(r'[,.;!?].*$', '', hobby_text)
                hobby_text = hobby_text[:50]
                
                if len(hobby_text) > 2 and hobby_text not in profile["interests"]:
                    profile["interests"].append(hobby_text)
                    if len(profile["interests"]) > 10:
                        profile["interests"] = profile["interests"][-10:]
                    changed = True
                    print(f"📝 Извлёк интерес: {hobby_text}")
                break
    
    # --- 4.7. НАСТРОЙКИ СТИЛЯ ---
    style_map = {
        'кратко': 'краткий',
        'подробно': 'подробный',
        'коротко': 'краткий',
        'развернуто': 'подробный',
        'лаконично': 'краткий',
        'сжато': 'краткий',
        'детально': 'подробный',
        'сухо': 'сухой',
        'эмоционально': 'эмоциональный',
        'нейтрально': 'нейтральный',
        'сбалансированно': 'сбалансированный'
    }
    
    for word, style in style_map.items():
        if f"отвечай {word}" in msg_lower or f"пиши {word}" in msg_lower:
            profile["preferences"]["style"] = style
            changed = True
            print(f"📝 Установил стиль: {style}")
            break
    
    # --- 4.8. ВАЖНЫЕ ДАТЫ ---
    date_patterns = [
        (r'день рождения\s+(\d{1,2})\.(\d{1,2})\.(\d{4})', 'день рождения'),
        (r'др\s+(\d{1,2})\.(\d{1,2})\.(\d{4})', 'день рождения'),
        (r'годовщина\s+(\d{1,2})\.(\d{1,2})\.(\d{4})', 'годовщина'),
    ]
    
    for pattern, event_type in date_patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            try:
                day, month, year = match.groups()
                date_str = f"{day}.{month}.{year}"
                profile["events"][event_type] = date_str
                changed = True
                print(f"📝 Запомнил дату: {event_type} = {date_str}")
            except:
                pass
    
    # --- 4.9. КОНТЕКСТ РАЗГОВОРА ---
    topics = {
        'погода': ['погод', 'дожд', 'снег', 'ветер', 'температур', 'градус'],
        'работа': ['работ', 'должн', 'проект', 'задач', 'коллег', 'босс', 'офис', 'зарплат'],
        'новости': ['новост', 'событи', 'происшеств', 'выбор', 'кризис', 'войн'],
        'технологии': ['технолог', 'гаджет', 'программ', 'обновлени', 'релиз', 'айфон', 'компьютер'],
        'спорт': ['спорт', 'футбол', 'хоккей', 'матч', 'команд', 'побед', 'счет'],
        'еда': ['ед', 'блюд', 'рецепт', 'кафе', 'ресторан', 'вкусн'],
        'путешествия': ['путешеств', 'поездк', 'отдых', 'город', 'страна', 'виза'],
        'семья': ['семь', 'родств', 'дет', 'родител', 'брат', 'сестр'],
        'здоровье': ['здоров', 'бол', 'лечени', 'врач', 'аптек', 'таблетк'],
        'образование': ['уч', 'школ', 'универ', 'курс', 'обучени', 'лекци', 'экзамен'],
        'фильмы': ['фильм', 'кино', 'сериал', 'актер', 'режиссер', 'сценарий'],
        'книги': ['книг', 'роман', 'фантастик', 'детектив', 'автор', 'чита'],
        'музыка': ['музык', 'песн', 'исполнител', 'концерт', 'альбом'],
    }
    
    for topic, keywords in topics.items():
        for keyword in keywords:
            if keyword in msg_lower:
                if "last_topics" not in profile["context"]:
                    profile["context"]["last_topics"] = []
                if topic not in profile["context"]["last_topics"]:
                    profile["context"]["last_topics"].append(topic)
                    if len(profile["context"]["last_topics"]) > 5:
                        profile["context"]["last_topics"] = profile["context"]["last_topics"][-5:]
                    changed = True
                break
        if changed:
            break
    
    # --- 4.10. НАСТРОЕНИЕ ---
    mood_words = {
        'отлично': 'отличное', 'хорошо': 'хорошее', 'нормально': 'нейтральное',
        'плохо': 'плохое', 'грустно': 'грустное', 'весело': 'весёлое',
        'устал': 'уставшее', 'воодушевл': 'воодушевлённое', 'раздраж': 'раздражённое',
        'спокойно': 'спокойное', 'рад': 'радостное', 'счастлив': 'счастливое'
    }
    
    for word, mood in mood_words.items():
        if word in msg_lower:
            profile["context"]["last_mood"] = mood
            changed = True
            break
    
    # --- 4.11. СТАТИСТИКА ---
    profile["stats"]["messages_count"] = profile["stats"].get("messages_count", 0) + 1
    
    if changed:
        save_profile(user_id, profile)
        return True, profile
    return False, profile

# ============================================================
# 5. КОМПАКТНЫЙ СИСТЕМНЫЙ ПРОМПТ
# ============================================================

def build_system_prompt(user_id):
    """Собирает компактный системный промпт из профиля"""
    profile = load_profile(user_id)
    parts = []
    
    # --- 5.1. Дата и время ---
    parts.append(f"[Сегодня: {CURRENT_DATE} {CURRENT_TIME}]")
    
    # --- 5.2. Личность ---
    p = profile["persona"]
    if p.get("name"):
        persona_parts = [f"Пользователь: {p['name']}"]
        if p.get("age"):
            persona_parts.append(f"{p['age']} лет")
        if p.get("city"):
            persona_parts.append(f"из {p['city']}")
        if p.get("job"):
            persona_parts.append(f"работа: {p['job']}")
        parts.append(" | ".join(persona_parts))
    
    # --- 5.3. Интересы (до 5) ---
    interests = profile["interests"][:5]
    if interests:
        parts.append(f"Интересы: {', '.join(interests)}")
    
    # --- 5.4. Важные факты (до 3) ---
    facts = list(profile["facts"].items())[:3]
    if facts:
        fact_str = ", ".join([f"{k}: {v}" for k, v in facts])
        parts.append(f"Факты: {fact_str}")
    
    # --- 5.5. Важные даты (до 2) ---
    events = list(profile["events"].items())[:2]
    if events:
        event_str = ", ".join([f"{k}: {v}" for k, v in events])
        parts.append(f"Даты: {event_str}")
    
    # --- 5.6. Настройки ---
    prefs = profile["preferences"]
    if prefs.get("style") or prefs.get("tone"):
        style_parts = []
        if prefs.get("style"):
            style_parts.append(prefs["style"])
        if prefs.get("tone"):
            style_parts.append(prefs["tone"])
        parts.append(f"Стиль: {', '.join(style_parts)}")
    
    # --- 5.7. Контекст ---
    ctx = profile["context"]
    if ctx.get("last_topics"):
        topics = ctx["last_topics"][-3:]
        parts.append(f"Темы: {', '.join(topics)}")
    if ctx.get("last_mood"):
        parts.append(f"Настроение: {ctx['last_mood']}")
    
    # --- 5.8. Собираем ---
    prompt = ". ".join(parts)
    if len(prompt) > 600:
        prompt = prompt[:600] + "..."
    
    return prompt

# ============================================================
# 6. ИСТОРИЯ ДИАЛОГА (ВРЕМЕННАЯ ПАМЯТЬ)
# ============================================================

def compress_history(history):
    """Сжимает старые сообщения, оставляя суть"""
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
        compressed = {
            "role": "system",
            "content": "Сжатая история:\n" + "\n".join(summary[-4:])
        }
        return [compressed] + recent
    
    return recent

def load_memory(user_id):
    """Загружает историю диалога"""
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return compress_history(data.get(str(user_id), []))
    except Exception as e:
        print(f"⚠️ Ошибка загрузки памяти: {e}")
    return []

def save_memory(user_id, history):
    """Сохраняет историю диалога"""
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
# 7. ПОИСК В ИНТЕРНЕТЕ
# ============================================================

def search_apiserpent(query):
    """Быстрый поиск через APISerpent"""
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
# 8. ОПРЕДЕЛЕНИЕ НУЖНОСТИ ПОИСКА
# ============================================================

def is_time_sensitive(query):
    """Определяет, нужен ли поиск в интернете"""
    q = query.lower()
    
    # Статичные темы (НЕ ищем)
    static = ['математик', 'уравнени', 'физик', 'хими', 'гравитаци', 'закон', 'теорем',
              'классик', 'античн', 'древн', 'историческ', 'средневеков',
              'кто такой', 'кто такая', 'биографи', 'родилс', 'умер',
              'произведени', 'книг', 'роман', 'стих', 'поэм',
              'что такое', 'что значит', 'как работает']
    for word in static:
        if word in q:
            return False
    
    # Динамичные темы (Ищем)
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
    
    # Годы
    years = re.findall(r'\b(19[0-9]{2}|20[0-9]{2})\b', q)
    for y in years:
        if int(y) >= CURRENT_YEAR - 1:
            return True
    
    return False

# ============================================================
# 9. HTTP СЕССИЯ ДЛЯ API
# ============================================================

def create_session():
    """Создаёт оптимизированную HTTP сессию"""
    connector = aiohttp.TCPConnector(
        limit=50,
        limit_per_host=20,
        keepalive_timeout=30,
        enable_cleanup_closed=True
    )
    timeout = aiohttp.ClientTimeout(
        total=60,
        connect=10,
        sock_read=30
    )
    return connector, timeout

# ============================================================
# 10. ЗАПРОС К DEEPSEEK API
# ============================================================

async def ask_deepseek(messages, retries=3):
    """Отправляет запрос к DeepSeek API с повторными попытками"""
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
                        wait_time = min(2 ** attempt, 30)
                        print(f"⚠️ Превышен лимит, ждём {wait_time}с...")
                        await asyncio.sleep(wait_time)
                        continue
                    
                    if resp.status == 401:
                        return None, "❌ Ошибка авторизации API. Проверьте DEEPSEEK_API_KEY."
                    
                    if resp.status == 500:
                        return None, "⚠️ Внутренняя ошибка сервера. Попробуйте позже."
                    
                    error_text = await resp.text()
                    return None, f"❌ Ошибка API ({resp.status}): {error_text[:100]}"
                    
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
            if attempt < retries - 1:
                print(f"⚠️ Ошибка соединения, попытка {attempt + 1} из {retries}...")
                await asyncio.sleep(2 ** attempt)
                continue
            return None, "❌ Ошибка соединения с API DeepSeek."
            
        except Exception as e:
            return None, f"❌ Неизвестная ошибка: {str(e)}"
    
    return None, "❌ Превышено количество попыток."

# ============================================================
# 11. КОМАНДЫ БОТА
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    profile = load_profile(user_id)
    name = profile.get("persona", {}).get("name", "друг")
    
    await update.message.reply_text(
        f"👋 Привет, {name}!\n\n"
        f"📅 Сегодня: {CURRENT_DATE} {CURRENT_TIME}\n\n"
        "🧠 **Я запоминаю о тебе:**\n"
        "• Имя, возраст, город, работа\n"
        "• Интересы и хобби (до 10)\n"
        "• Важные даты (день рождения и др.)\n"
        "• Стиль и тон общения\n"
        "• Контекст разговора\n\n"
        "📝 **Команды памяти:**\n"
        "• `запомни имя: Алексей` — запомнить факт\n"
        "• `отвечай кратко` — изменить стиль\n"
        "• `/profile` — показать, что я помню\n"
        "• `/forget` — очистить память обо мне\n"
        "• `/stats` — статистика памяти\n\n"
        "🔍 **Поиск:** `бро погода` или просто спроси\n"
        "⚡ Я сам пойму, когда нужен интернет!"
    )

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает, что бот помнит о пользователе"""
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    profile = load_profile(user_id)
    lines = ["🧠 **Что я помню о тебе:**\n"]
    
    # Личность
    p = profile.get("persona", {})
    if p.get("name") or p.get("age") or p.get("city") or p.get("job"):
        p_str = []
        if p.get("name"): p_str.append(f"👤 {p['name']}")
        if p.get("age"): p_str.append(f"📅 {p['age']} лет")
        if p.get("city"): p_str.append(f"📍 {p['city']}")
        if p.get("job"): p_str.append(f"💼 {p['job']}")
        lines.append(" **Личность:** " + " | ".join(p_str))
    
    # Интересы
    interests = profile.get("interests", [])
    if interests:
        lines.append(f"❤️ **Интересы:** " + ", ".join(interests[:7]))
        if len(interests) > 7:
            lines.append(f"   и ещё {len(interests) - 7}...")
    
    # Факты
    facts = profile.get("facts", {})
    if facts:
        facts_str = []
        for k, v in list(facts.items())[:5]:
            facts_str.append(f"{k}: {v}")
        if facts_str:
            lines.append(f"📌 **Факты:** " + ", ".join(facts_str))
    
    # Даты
    events = profile.get("events", {})
    if events:
        events_str = []
        for k, v in list(events.items())[:5]:
            events_str.append(f"{k}: {v}")
        if events_str:
            lines.append(f"📅 **Даты:** " + ", ".join(events_str))
    
    # Настройки
    prefs = profile.get("preferences", {})
    if prefs.get("style") or prefs.get("tone"):
        pref_str = []
        if prefs.get("style"): pref_str.append(f"стиль: {prefs['style']}")
        if prefs.get("tone"): pref_str.append(f"тон: {prefs['tone']}")
        if pref_str:
            lines.append(f"⚙️ **Настройки:** " + ", ".join(pref_str))
    
    # Контекст
    ctx = profile.get("context", {})
    if ctx.get("last_topics"):
        lines.append(f"💬 **Темы:** " + ", ".join(ctx["last_topics"][-3:]))
    if ctx.get("last_mood"):
        lines.append(f"😊 **Настроение:** {ctx['last_mood']}")
    
    # Статистика
    stats = profile.get("stats", {})
    if stats.get("messages_count"):
        lines.append(f"📊 **Сообщений:** {stats['messages_count']}")
    if stats.get("first_seen"):
        lines.append(f"🕐 **Впервые:** {stats['first_seen']}")
    
    if len(lines) == 1:
        lines.append("Пока ничего не запомнил. Расскажи о себе!")
    
    lines.append(f"\n🔄 **Обновлено:** {profile.get('updated', 'неизвестно')}")
    
    await update.message.reply_text("\n".join(lines))

async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очищает память о пользователе"""
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    await update.message.reply_text(
        "⚠️ **Ты уверен, что хочешь, чтобы я забыл всё, что знаю о тебе?**\n"
        "Напиши **ДА** для подтверждения."
    )
    context.user_data['forget_confirm'] = user_id

async def confirm_forget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение очистки памяти"""
    user_id = update.effective_user.id
    if context.user_data.get('forget_confirm') != user_id:
        return
    
    if update.message.text.upper() == "ДА":
        # Очищаем профиль
        save_profile(user_id, DEFAULT_PROFILE.copy())
        # Очищаем историю
        save_memory(user_id, [])
        await update.message.reply_text(
            "🧹 **Я забыл всё, что знал о тебе.**\n"
            "Начинаем с чистого листа! Напиши /start чтобы познакомиться заново."
        )
    else:
        await update.message.reply_text("✅ Отмена. Я продолжаю помнить!")
    
    context.user_data.pop('forget_confirm', None)

async def date_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает текущую дату"""
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    now = datetime.now()
    await update.message.reply_text(
        f"📅 **Текущая дата и время:**\n\n"
        f"📆 Дата: {now.strftime('%d.%m.%Y')}\n"
        f"🕐 Время: {now.strftime('%H:%M:%S')}\n"
        f"📅 День недели: {now.strftime('%A')}\n"
        f"📅 Месяц: {now.strftime('%B')}\n"
        f"📅 Год: {now.year}"
    )

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает текущую модель"""
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    keyboard = [[InlineKeyboardButton("⚡ Flash", callback_data=MODEL_DEFAULT)]]
    await update.message.reply_text(
        f"✅ **Модель:** `{MODEL_DEFAULT}`\n\n"
        "⚡ Flash — быстрая, дешёвая и точная.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очищает историю диалогов"""
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    save_memory(user_id, [])
    await update.message.reply_text("🧹 **История диалогов очищена.**")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику памяти"""
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    history = load_memory(user_id)
    profile = load_profile(user_id)
    
    await update.message.reply_text(
        f"📊 **Статистика памяти:**\n\n"
        f"💬 Сообщений в истории: **{len(history)}**\n"
        f"📝 Максимум истории: **{MAX_HISTORY}**\n"
        f"📌 Сохраняется последних: **{KEEP_RECENT}**\n"
        f"📋 Фактов в профиле: **{len(profile.get('facts', {}))}**\n"
        f"❤️ Интересов: **{len(profile.get('interests', []))}**\n"
        f"📅 Важных дат: **{len(profile.get('events', {}))}**\n"
        f"💬 Всего сообщений: **{profile.get('stats', {}).get('messages_count', 0)}**\n"
        f"🔄 Обновлён: **{profile.get('updated', 'неизвестно')}**"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок"""
    query = update.callback_query
    await query.answer()
    if query.data == MODEL_DEFAULT:
        await query.edit_message_text(f"✅ **Модель:** `{MODEL_DEFAULT}`")

# ============================================================
# 12. ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ
# ============================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает все текстовые сообщения"""
    user_id = update.effective_user.id
    
    # Проверка доступа
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    user_message = update.message.text
    
    # --- ОБРАБОТКА КОМАНДЫ "ЗАПОМНИ" ---
    if user_message.lower().startswith("запомни "):
        updated, profile = analyze_and_update_profile(user_id, user_message)
        if updated:
            await update.message.reply_text("✅ **Запомнил!** Я буду помнить это.")
        else:
            await update.message.reply_text("📝 Я запомнил, но не совсем понял. Попробуй уточнить.")
        return
    
    # --- ПОДТВЕРЖДЕНИЕ ОЧИСТКИ ---
    if context.user_data.get('forget_confirm') == user_id:
        await confirm_forget(update, context)
        return
    
    # --- ОБНОВЛЕНИЕ ПРОФИЛЯ ---
    analyze_and_update_profile(user_id, user_message)
    
    # --- ЗАГРУЗКА ИСТОРИИ ---
    history = load_memory(user_id)
    
    # --- ПРОВЕРКА НУЖНОСТИ ПОИСКА ---
    need_search = is_time_sensitive(user_message)
    
    # Явный поиск "бро"
    if user_message.lower().startswith("бро "):
        need_search = True
        user_message = user_message[4:].strip()
        if not user_message:
            await update.message.reply_text("❌ Напиши, что искать после 'бро'.")
            return
    
    # --- СИСТЕМНЫЙ ПРОМПТ ---
    system_prompt = build_system_prompt(user_id)
    system_msg = {"role": "system", "content": system_prompt}
    
    # --- ПОИСК В ИНТЕРНЕТЕ ---
    if need_search and user_message:
        status_msg = await update.message.reply_text("🌐 **Актуализирую информацию в интернете...**")
        
        results = search_apiserpent(user_message)
        
        if not results:
            await status_msg.edit_text("⚠️ **Не удалось найти информацию.** Отвечаю из базы знаний.")
            history.append({"role": "user", "content": user_message})
            answer, error = await ask_deepseek([system_msg] + history)
            if error:
                await update.message.reply_text(error)
                return
            history.append({"role": "assistant", "content": answer})
            save_memory(user_id, history)
            await update.message.reply_text(answer)
            return
        
        # Формируем результаты поиска
        search_text = f"📅 **Сегодня:** {CURRENT_DATE} {CURRENT_TIME}\n\n"
        search_text += f"🔍 **Запрос:** '{user_message}'\n\n"
        for i, r in enumerate(results[:5], 1):
            search_text += f"{i}. **{r['title']}**\n"
            search_text += f"   {r['snippet'][:200]}\n"
            search_text += f"   🔗 {r['link']}\n\n"
        
        search_prompt = {
            "role": "system",
            "content": f"""Сегодня: {CURRENT_DATE} {CURRENT_TIME}.

Вопрос пользователя: "{user_message}"

Результаты поиска (используй ТОЛЬКО их):
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
# 13. ОБРАБОТЧИК ОШИБОК
# ============================================================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик ошибок"""
    try:
        raise context.error
    except Exception as e:
        print(f"⚠️ Ошибка: {e}")
        import traceback
        traceback.print_exc()
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ **Произошла ошибка.** Пожалуйста, попробуйте позже."
            )

# ============================================================
# 14. ЗАПУСК БОТА
# ============================================================

if __name__ == "__main__":
    # Настройка логирования
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Создаём приложение
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Регистрируем команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(CommandHandler("date", date_command))
    app.add_handler(CommandHandler("model", model_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    
    print(f"✅ Бот запущен. Текущая дата: {CURRENT_DATE}")
    print(f"🧠 Супер-память активна: MAX_HISTORY={MAX_HISTORY}, KEEP_RECENT={KEEP_RECENT}")
    print("=" * 50)
    
    app.run_polling()

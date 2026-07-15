import logging
import os
import json
import sys
import re
import asyncio
import aiohttp
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Optional, List, Dict, Any, Tuple
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

# ========== ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ==========
TELEGRAM_TOKEN: Optional[str] = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY: Optional[str] = os.getenv("DEEPSEEK_API_KEY")
APISERPENT_API_KEY: Optional[str] = os.getenv("APISERPENT_API_KEY")

try:
    ADMIN_USER_ID: int = int(os.getenv("ADMIN_USER_ID", "0"))
except ValueError:
    ADMIN_USER_ID = 0

ALLOWED_USERS_STR: str = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS_LIST: List[int] = []
if ALLOWED_USERS_STR:
    try:
        ALLOWED_USERS_LIST = [int(x.strip()) for x in ALLOWED_USERS_STR.split(",") if x.strip()]
    except ValueError:
        logger.warning("Ошибка в ALLOWED_USERS")

if ADMIN_USER_ID != 0 and ADMIN_USER_ID not in ALLOWED_USERS_LIST:
    ALLOWED_USERS_LIST.append(ADMIN_USER_ID)

# ========== ЧАСОВОЙ ПОЯС (с проверкой) ==========
TIMEZONE_STR: str = os.getenv("TIMEZONE", "Europe/Moscow")
try:
    TZ = ZoneInfo(TIMEZONE_STR)
except ZoneInfoNotFoundError:
    logger.warning(f"Часовой пояс '{TIMEZONE_STR}' не найден, используется UTC")
    TZ = ZoneInfo("UTC")
except Exception as e:
    logger.warning(f"Ошибка при установке часового пояса: {e}, используется UTC")
    TZ = ZoneInfo("UTC")

def now() -> datetime:
    """Возвращает текущее локальное время с учётом часового пояса."""
    return datetime.now(TZ)

def get_current_date() -> str:
    return now().strftime("%d.%m.%Y")

def get_current_time() -> str:
    return now().strftime("%H:%M")

def get_current_weekday() -> str:
    return now().strftime("%A")

# ========== КОНСТАНТЫ ==========
LEVEL_1 = {'max_history': 80, 'keep_recent': 20, 'compress_to': 20}
LEVEL_2 = {'max_items': 1000, 'compress_interval': 40, 'compress_to': 50}
LEVEL_3 = {'max_items': 10000, 'compress_interval': 200, 'compress_to': 100}
LEVEL_4 = {'max_items': 100000, 'compress_interval': 1000, 'compress_to': 200}
LEVEL_5 = {'max_items': 1000000, 'compress_interval': 10000, 'compress_to': 500}

MODEL_DEFAULT: str = os.getenv("MODEL_DEFAULT", "deepseek-v4-flash")
DEEPSEEK_API_BASE: str = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
SEARCH_ENGINE: str = os.getenv("SEARCH_ENGINE", "google")
MODEL_TEMPERATURE: float = float(os.getenv("MODEL_TEMPERATURE", "0.1"))
CORE_SYSTEM_RULE: str = (
    "Ты — честный ассистент. КРИТИЧЕСКИЕ ПРАВИЛА:\n"
    "1. НИКОГДА не выдумывай факты. Если не знаешь — прямо скажи «Я не знаю» или «У меня нет точных данных».\n"
    "2. Не придумывай числа, даты, курсы, имена. Лучше признать незнание, чем соврать.\n"
    "3. Если отвечаешь по результатам интернет-поиска — опирайся ТОЛЬКО на них.\n"
    "4. Если данные могли устареть — предупреди об этом."
)

PEAK_HOURS: List[Tuple[int, int]] = [(9, 12), (14, 18)]
RATE_LIMIT: int = int(os.getenv("RATE_LIMIT", "3"))
RATE_WINDOW: int = int(os.getenv("RATE_WINDOW", "5"))
MAX_MSG_LEN: int = 3500
BACKUP_INTERVAL: int = 10  # каждые 10 сообщений
INACTIVITY_TIMEOUT: int = 600  # 10 минут
CLEANUP_INTERVAL: int = 3600  # 1 час

if not TELEGRAM_TOKEN or not DEEPSEEK_API_KEY:
    logger.error("TELEGRAM_TOKEN или DEEPSEEK_API_KEY не заданы")
    sys.exit(1)

if not APISERPENT_API_KEY:
    logger.warning("APISERPENT_API_KEY не задан — интернет-поиск будет недоступен.")

# ========== ЛОГИ ПРИ СТАРТЕ ==========
logger.info("=" * 50)
logger.info("🚀 БОТ ЗАПУЩЕН")
logger.info("=" * 50)
logger.info(f"  🤖 TELEGRAM_TOKEN: {'✅' if TELEGRAM_TOKEN else '❌'}")
logger.info(f"  🔑 DEEPSEEK_API_KEY: {'✅' if DEEPSEEK_API_KEY else '❌'}")
logger.info(f"  🔍 APISERPENT_API_KEY: {'✅' if APISERPENT_API_KEY else '❌'}")
logger.info(f"  🧠 Модель: {MODEL_DEFAULT} (temperature={MODEL_TEMPERATURE})")
logger.info(f"  🕐 Часовой пояс: {TZ.key}")
logger.info(f"  👤 ADMIN_USER_ID: {ADMIN_USER_ID}")
logger.info(f"  👥 Разрешённых пользователей: {len(ALLOWED_USERS_LIST)}")
logger.info(f"  📊 Память: 80 → 1000 → 10000 → 100000 → 1 000 000+")
logger.info(f"  💾 Данные — по отдельному файлу на пользователя")
logger.info(f"  💾 Авто-бэкап: каждые {BACKUP_INTERVAL} сообщений")
logger.info(f"  🕐 Время: динамическое, с учётом часового пояса")
logger.info(f"  🔍 Интернет-поиск: авто (погода/курс/новости) + 'бро'")
logger.info(f"  🛡 Принцип: не врать, признавать незнание")
logger.info("=" * 50)

# ========== ПУТИ ==========
os.makedirs("data", exist_ok=True)
os.makedirs("data/backups", exist_ok=True)
DATA_DIR = "data"
BACKUP_DIR = "data/backups"

def memory_path(user_id: int) -> str:
    return os.path.join(DATA_DIR, f"memory_{user_id}.json")

def profile_path(user_id: int) -> str:
    return os.path.join(DATA_DIR, f"profile_{user_id}.json")

def counter_path(user_id: int) -> str:
    return os.path.join(DATA_DIR, f"counter_{user_id}.json")

# ========== СЕССИИ И БЛОКИРОВКИ ==========
_http_session: Optional[aiohttp.ClientSession] = None
user_locks: Dict[int, asyncio.Lock] = {}
rate_lock = asyncio.Lock()
request_count: Dict[int, List[float]] = {}

def get_user_lock(user_id: int) -> asyncio.Lock:
    return user_locks.setdefault(user_id, asyncio.Lock())

async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        connector = aiohttp.TCPConnector(
            limit=50, limit_per_host=20,
            keepalive_timeout=30, enable_cleanup_closed=True
        )
        timeout = aiohttp.ClientTimeout(total=60, connect=10, sock_read=30)
        _http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return _http_session

# ========== АНАЛИЗ ОШИБОК ==========
def analyze_error(error_text: str) -> str:
    e = error_text.lower()
    if "timeout" in e or "timed out" in e:
        return "⏰ Превышено время ожидания ответа от сервера. Попробуйте позже."
    if "connection" in e or "network" in e:
        return "🌐 Проблемы с интернет-соединением. Проверьте связь."
    if "429" in error_text or "too many requests" in e:
        return "📊 Слишком много запросов. Подождите минуту и повторите."
    if "401" in error_text or "unauthorized" in e:
        return "🔑 Ошибка авторизации API. Проверьте ключи доступа."
    if "500" in error_text or "internal server" in e or "server_error" in e:
        return "⚠️ Внутренняя ошибка сервера API. Повторите позже."
    if "not found" in e or "404" in error_text:
        return "🔍 Ресурс не найден. Возможно, изменился адрес API или имя модели."
    if "400" in error_text or "bad request" in e:
        return "⚠️ Некорректный запрос. Возможно, неверное имя модели (проверьте MODEL_DEFAULT)."
    if "empty" in e:
        return "📭 Получен пустой ответ от сервера. Попробуйте переформулировать вопрос."
    if "invalid_response" in e:
        return "⚠️ Некорректный ответ от сервера. Возможно, изменился формат API."
    if "max_retries" in e:
        return "⚠️ Не удалось получить ответ после нескольких попыток. Проверьте соединение."
    if "server_disconnected" in e:
        return "⚠️ Сервер разорвал соединение. Попробуйте позже."
    return f"⚠️ Ошибка: {error_text[:150]}"

# ========== АТОМАРНЫЕ ОПЕРАЦИИ ==========
def atomic_write(filename: str, data: Any, as_json: bool = True) -> bool:
    temp_file = filename + ".tmp"
    try:
        with open(temp_file, 'w', encoding='utf-8') as f:
            if as_json:
                json.dump(data, f, ensure_ascii=False, indent=2)
            else:
                f.write(data)
            f.flush()
            os.fsync(f.fileno())
        shutil.move(temp_file, filename)
        return True
    except Exception as e:
        logger.error(f"Ошибка атомарной записи {filename}: {e}")
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass
        return False

def atomic_read(filename: str, default: Any = None, as_json: bool = True) -> Any:
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f) if as_json else f.read()
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        if not isinstance(e, FileNotFoundError):
            logger.warning(f"Ошибка чтения {filename}: {e}")
        return default

# ========== ПРОФИЛЬ / СЧЁТЧИК ==========
def load_profile(user_id: int) -> Dict:
    return atomic_read(profile_path(user_id), default={})

def save_profile(user_id: int, profile: Dict, backup: bool = True) -> bool:
    """Должна вызываться только при захваченном замке пользователя."""
    profile["updated"] = now().strftime("%d.%m.%Y %H:%M:%S")
    if not atomic_write(profile_path(user_id), profile):
        return False
    if backup:
        create_backup(user_id, "profile")
    return True

def load_counter(user_id: int) -> int:
    data = atomic_read(counter_path(user_id), default={"count": 0})
    return data.get("count", 0)

def save_counter(user_id: int, count: int) -> None:
    atomic_write(counter_path(user_id), {"count": count})

# ========== БЭКАПЫ ==========
def create_backup(user_id: int, data_type: str) -> bool:
    try:
        timestamp = now().strftime("%Y%m%d_%H%M%S")
        filename = f"{BACKUP_DIR}/{data_type}_{user_id}_{timestamp}.json"
        if data_type == "profile":
            atomic_write(filename, load_profile(user_id))
        elif data_type == "memory":
            atomic_write(filename, load_memory_raw(user_id))
        backups = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith(f"{data_type}_{user_id}_"))
        if len(backups) > 10:
            for old_file in backups[:-10]:
                try:
                    os.remove(os.path.join(BACKUP_DIR, old_file))
                except OSError:
                    pass
        return True
    except Exception as e:
        logger.error(f"Ошибка создания бэкапа: {e}")
        return False

async def restore_backup(user_id: int, data_type: str) -> bool:
    lock = get_user_lock(user_id)
    async with lock:
        try:
            backups = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith(f"{data_type}_{user_id}_"))
            if not backups:
                return False
            latest = backups[-1]
            with open(os.path.join(BACKUP_DIR, latest), 'r', encoding='utf-8') as f:
                data = json.load(f)
            logger.info(f"🔄 Восстановлен {data_type} пользователя {user_id} из {latest}")
            if data_type == "profile":
                save_profile(user_id, data, backup=False)
            elif data_type == "memory":
                await save_memory(user_id, data, backup=False, lock_held=True)
            return True
        except Exception as e:
            logger.error(f"Ошибка восстановления {data_type}: {e}")
            return False

# ========== СЖАТИЕ ==========
STOP_WORDS = {'это', 'так', 'вот', 'ну', 'просто', 'очень'}

def extract_key_points(text: str, max_len: int = 30) -> str:
    if len(text) <= max_len:
        return text
    important = [w for w in text.split() if w.lower() not in STOP_WORDS and len(w) > 2]
    result = ' '.join(important[:10])
    return result[:max_len] + "..."

def extract_keywords_aggressive(text: str, max_len: int = 20) -> str:
    if len(text) <= max_len:
        return text
    important = [w[:8] for w in text.split() if len(w) > 3 and w.lower() not in STOP_WORDS]
    result = ' '.join(important[:5])
    return result[:max_len] + "..."

def extract_keywords_ultra(text: str, max_len: int = 12) -> str:
    if len(text) <= max_len:
        return text
    important = [w[:5] for w in text.split() if len(w) > 3 and w.lower() not in STOP_WORDS]
    result = ' '.join(important[:3])
    return result[:max_len] + "..."

def compress_ultra_old(items: List[str], target_count: int = 50) -> List[str]:
    if len(items) <= target_count:
        return items
    old_items = items[:200]
    compressed = []
    for i in range(0, len(old_items), 4):
        batch = old_items[i:i+4]
        compressed.append(f"[архив] {' | '.join(item[:20] for item in batch)}")
    result = compressed + items[-target_count:]
    if len(result) > target_count + 10:
        result = result[-target_count:]
    return result

def compress_history(history: List[Dict]) -> List[Dict]:
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
        return [{"role": "system", "content": "📚 История (сжато):\n" + "\n".join(summary[-5:])}] + recent
    return recent

def load_memory_raw(user_id: int) -> List[Dict]:
    return atomic_read(memory_path(user_id), default=[])

def load_memory(user_id: int) -> List[Dict]:
    return compress_history(load_memory_raw(user_id))

# ========== СЖАТЫЕ УРОВНИ ==========
def _update_level(user_id: int, messages: List[Dict], level_key: str,
                  level_cfg: Dict, extractor, ext_len: int, ts_fmt: str) -> None:
    """Должна вызываться только при захваченном замке пользователя."""
    profile = load_profile(user_id)
    profile.setdefault(level_key, [])
    batch = messages[-level_cfg['compress_interval']:]
    timestamp = now().strftime(ts_fmt)
    for msg in batch:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            profile[level_key].append(f"[{timestamp}] Q: {extractor(content, ext_len)}")
        elif role == "assistant":
            profile[level_key].append(f"[{timestamp}] A: {extractor(content, ext_len)}")
    if level_key == "level_5" and len(profile["level_5"]) > level_cfg['compress_to'] + 100:
        profile["level_5"] = compress_ultra_old(profile["level_5"][:200], 50) + profile["level_5"][200:]
    if len(profile[level_key]) > level_cfg['compress_to']:
        profile[level_key] = profile[level_key][-level_cfg['compress_to']:]
    save_profile(user_id, profile, backup=False)

async def _save_memory_impl(user_id: int, history: List[Dict], backup: bool) -> bool:
    try:
        if len(history) > LEVEL_1['max_history']:
            old_messages = history[:-LEVEL_1['keep_recent']]
            if old_messages:
                _update_level(user_id, old_messages, "level_2", LEVEL_2, extract_key_points, 30, "%d.%m")
                profile = load_profile(user_id)
                if len(profile.get("level_2", [])) >= LEVEL_2['compress_to']:
                    _update_level(user_id, old_messages, "level_3", LEVEL_3, extract_keywords_aggressive, 25, "%m.%d")
                if len(profile.get("level_3", [])) >= LEVEL_3['compress_to']:
                    _update_level(user_id, old_messages, "level_4", LEVEL_4, extract_keywords_aggressive, 20, "%m.%d")
                if len(profile.get("level_4", [])) >= LEVEL_4['compress_to']:
                    _update_level(user_id, old_messages, "level_5", LEVEL_5, extract_keywords_ultra, 15, "%y.%m")
        if not atomic_write(memory_path(user_id), compress_history(history)):
            logger.error(f"Не удалось сохранить историю для {user_id}")
            return False
        if backup:
            create_backup(user_id, "memory")
        count = load_counter(user_id) + 1
        save_counter(user_id, count)
        if count % BACKUP_INTERVAL == 0:
            create_backup(user_id, "profile")
        return True
    except Exception as e:
        logger.error(f"Критическая ошибка при сохранении памяти {user_id}: {e}")
        return False

async def save_memory(user_id: int, history: List[Dict], backup: bool = True, lock_held: bool = False) -> bool:
    if lock_held:
        return await _save_memory_impl(user_id, history, backup)
    lock = get_user_lock(user_id)
    async with lock:
        return await _save_memory_impl(user_id, history, backup)

# ========== ПОИСК ==========
def parse_time_query(time_query: str) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    try:
        parts = time_query.split(":")
        if len(parts) == 2:
            return int(parts[0]), int(parts[1]), None
        if len(parts) == 3:
            return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        pass
    return None, None, None

def search_by_time(user_id: int, time_query: str) -> List[Dict]:
    history = load_memory_raw(user_id)
    results = []
    qh, qm, _ = parse_time_query(time_query)
    if qh is None:
        return results
    for msg in history:
        ts = msg.get("timestamp", "")
        if not ts:
            continue
        try:
            mt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            if mt.hour == qh and mt.minute == qm:
                results.append(msg)
        except ValueError:
            if time_query in ts:
                results.append(msg)
    return results

def parse_date_query(query: str) -> Optional[str]:
    q = query.lower().strip()
    n = now()
    if q == "сегодня":
        return n.strftime("%Y-%m-%d")
    if q == "вчера":
        return (n - timedelta(days=1)).strftime("%Y-%m-%d")
    if q == "завтра":
        return (n + timedelta(days=1)).strftime("%Y-%m-%d")
    for pattern in [r'(\d{2})\.(\d{2})\.(\d{4})', r'(\d{2})\.(\d{2})', r'(\d{4})-(\d{2})-(\d{2})']:
        m = re.search(pattern, query)
        if m:
            g = m.groups()
            if len(g) == 3:
                if '.' in query:
                    d, mo, y = g
                else:
                    y, mo, d = g
                return f"{y}-{mo}-{d}"
            if len(g) == 2:
                d, mo = g
                return f"{n.year}-{mo}-{d}"
    return None

def search_by_date(user_id: int, date_str: str) -> List[Dict]:
    return [m for m in load_memory_raw(user_id) if m.get("timestamp", "").startswith(date_str)]

def search_in_pyramid(user_id: int, query: str) -> List[str]:
    profile = load_profile(user_id)
    q = query.lower()
    results = []
    for msg in load_memory_raw(user_id)[-40:]:
        content = msg.get("content", "")
        if q in content.lower():
            role = "👤" if msg.get("role") == "user" else "🤖"
            ts = msg.get("timestamp", "")
            time_str = f" [{ts}]" if ts else ""
            results.append(f"{role}{time_str} {extract_key_points(content, 80)}")
    for lvl, emoji in [("level_2", "📚"), ("level_3", "📖"), ("level_4", "📕"), ("level_5", "📗")]:
        for item in profile.get(lvl, []):
            if q in item.lower():
                results.append(f"{emoji} {item}")
    return results[:15]

# ========== АНАЛИЗ СООБЩЕНИЯ ==========
async def analyze_message(user_id: int, user_message: str) -> Dict[str, Any]:
    q = user_message.lower().strip()

    short_confirm = {'да', 'нет', 'ок', 'хорошо', 'понял', 'поняла', 'ага', 'угу', 'ясно', 'ладно', 'окей'}
    if q in short_confirm or q.rstrip('.!') in short_confirm:
        return {"action": "confirm"}

    simple_greetings = {'привет', 'здравствуй', 'здрасте', 'приветствую', 'салют', 'hello', 'hi'}
    if q in simple_greetings or q.rstrip('!') in simple_greetings:
        return {"action": "greeting"}

    personal_triggers = {'имя', 'город', 'работа', 'возраст', 'интерес', 'хобби', 'меня зовут'}
    if any(t in q for t in personal_triggers):
        return {"action": "memory"}

    memory_triggers = {'помнишь', 'напомни', 'что я говорил', 'что я писал', 'вспомни'}
    if any(t in q for t in memory_triggers):
        return {"action": "memory"}

    # Объединённый список дат/времён
    date_time_keywords = {
        'дата', 'время', 'число', 'который час', 'сколько времени',
        'какая дата', 'какое сегодня число', 'какой сегодня день', 'текущее время'
    }
    if any(k in q for k in date_time_keywords):
        return {"action": "date_time"}

    dynamic_triggers = {
        'погод', 'температур', 'прогноз', 'осадк',
        'курс валют', 'курс доллар', 'курс евро', 'курс юан', 'биткоин', 'котировк',
        'последние новости', 'свежие новости', 'что произошло сегодня'
    }
    if any(t in q for t in dynamic_triggers):
        return {"action": "internet"}

    return {"action": "memory"}

# ========== APISERPENT ==========
async def search_apiserpent_async(query: str) -> List[Dict[str, str]]:
    if not APISERPENT_API_KEY:
        logger.error("APISERPENT_API_KEY не задан")
        return []
    session = await get_http_session()
    try:
        logger.info(f"🔍 Поиск (движок {SEARCH_ENGINE}): {query}")
        async with session.get(
            "https://apiserpent.com/api/search",
            params={"q": query, "engine": SEARCH_ENGINE, "num": 5},
            headers={"X-API-Key": APISERPENT_API_KEY},
            timeout=30
        ) as response:
            response.raise_for_status()
            data = await response.json()
            results = []
            if isinstance(data.get("results"), dict):
                results = data["results"].get("organic", [])
            elif "organic_results" in data:
                results = data["organic_results"]
            elif isinstance(data.get("results"), list):
                results = data["results"]
            elif "organic" in data:
                results = data["organic"]
            elif "items" in data:
                results = data["items"]
            if not results and isinstance(data, dict):
                for key in data:
                    if isinstance(data[key], list) and data[key] and isinstance(data[key][0], dict):
                        results = data[key]
                        break
            formatted = []
            for r in results[:5]:
                if isinstance(r, dict):
                    formatted.append({
                        "title": str(r.get("title", r.get("name", "Без названия")))[:150],
                        "snippet": str(r.get("snippet", r.get("description", r.get("text", "Нет описания"))))[:250],
                        "link": str(r.get("url", r.get("link", r.get("href", "#"))))[:150],
                    })
            if formatted:
                logger.info(f"✅ Найдено {len(formatted)} результатов")
            else:
                logger.warning(f"APISerpent пустой результат. Сырой ответ: {str(data)[:300]}")
            return formatted
    except asyncio.TimeoutError:
        logger.error("⏰ Таймаут APISerpent")
        return []
    except aiohttp.ClientResponseError as e:
        logger.error(f"Ошибка APISerpent: {e.status} - {str(e)}")
        return []
    except Exception as e:
        logger.error(f"Ошибка APISerpent: {e}")
        return []

# ========== DEEPSEEK ==========
async def ask_deepseek(messages: List[Dict], retries: int = 3, max_tokens: Optional[int] = None) -> Tuple[Optional[str], Optional[str]]:
    session = await get_http_session()
    for attempt in range(retries):
        try:
            payload = {"model": MODEL_DEFAULT, "messages": messages, "temperature": MODEL_TEMPERATURE}
            if max_tokens:
                payload["max_tokens"] = max_tokens
            async with session.post(
                f"{DEEPSEEK_API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                json=payload
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                if data.get("choices"):
                    content = data["choices"][0].get("message", {}).get("content")
                    return (content, None) if content else (None, "empty")
                return None, "invalid_response"
        except aiohttp.ClientResponseError as e:
            logger.error(f"Ошибка от DeepSeek: {e.status} - {str(e)}")
            if e.status in (429, 500):
                await asyncio.sleep(min(2 ** attempt, 30))
                continue
            return None, f"http_{e.status}"
        except asyncio.TimeoutError:
            logger.error("⏰ Таймаут DeepSeek")
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            return None, "timeout"
        except Exception as e:
            logger.error(f"Неизвестная ошибка DeepSeek: {e}")
            if attempt < retries - 1:
                continue
            return None, "unknown"
    return None, "max_retries"

# ========== ГЕНЕРАЦИЯ ОТВЕТА ==========
def build_profile_context(profile: Dict) -> str:
    parts = []
    for key, value in profile.items():
        if key in ("updated", "level_2", "level_3", "level_4", "level_5") or key.startswith(("last_check_", "update_history_")):
            continue
        if isinstance(value, list):
            if value:
                parts.append(f"{key}: {', '.join(str(v)[:50] for v in value[:3])}")
        else:
            parts.append(f"{key}: {str(value)[:50]}")
    if profile.get("level_2"):
        parts.append(f"📚: {', '.join(profile['level_2'][-10:])}")
    if profile.get("level_3"):
        parts.append(f"📖: {', '.join(profile['level_3'][-5:])}")
    context = ". ".join(parts)
    return context[:800] + "..." if len(context) > 800 else context

async def generate_response(user_id: int, user_message: str, analysis_result: Dict,
                            history: List[Dict], profile: Dict) -> Tuple[str, bool, Optional[str]]:
    action = analysis_result.get("action", "memory")

    if action == "confirm":
        return "✅ Понял! Продолжаем.", False, None

    if action == "greeting":
        greetings = {
            'привет': '👋 Привет! Как дела?',
            'здравствуй': '👋 Здравствуйте! Чем могу помочь?',
            'пока': '👋 Пока!',
            'спасибо': 'Пожалуйста! 🤗'
        }
        for key, val in greetings.items():
            if key in user_message.lower():
                return val, False, None
        return "👋 Привет! Чем могу помочь?", False, None

    if action == "date_time":
        weekday_ru = {
            'Monday': 'Понедельник', 'Tuesday': 'Вторник', 'Wednesday': 'Среда',
            'Thursday': 'Четверг', 'Friday': 'Пятница', 'Saturday': 'Суббота', 'Sunday': 'Воскресенье'
        }.get(get_current_weekday(), get_current_weekday())
        answer = f"📅 Сегодня: {get_current_date()} ({weekday_ru})\n🕐 Текущее время: {get_current_time()}"
        return answer, False, "📂 локально"

    if action == "internet":
        logger.info(f"🔍 Поисковый запрос: {user_message}")
        results = await search_apiserpent_async(user_message)
        profile_ctx = build_profile_context(profile)

        if not results:
            system_msg = {"role": "system", "content":
                f"{CORE_SYSTEM_RULE}\nСегодня: {get_current_date()} {get_current_time()}. {profile_ctx}\n"
                f"ВАЖНО: интернет-поиск не дал результатов. Если это вопрос о текущих данных "
                f"(погода, курс, новости) — честно скажи, что не можешь проверить сейчас, и НЕ выдумывай."}
            history.append({"role": "user", "content": user_message})
            answer, err = await ask_deepseek([system_msg] + history)
            if err:
                return f"⚠️ {analyze_error(err)}", False, None
            full = (f"🔍 **Искал в интернете:** `{user_message}`\n\n"
                    f"❌ **Ничего не найдено.** Уточните запрос (например: 'бро погода в Москве').\n\n"
                    f"🧠 **Ответ из знаний модели (может быть неактуален):**\n{answer}")
            return full, True, "🧠 из модели (поиск пуст)"

        search_text = f"🔍 **Искал в интернете:** `{user_message}`\n\n📊 **Найдено {len(results)} результатов:**\n\n"
        for i, r in enumerate(results, 1):
            search_text += f"{i}. **{r['title']}**\n   {r['snippet'][:200]}\n   🔗 {r['link']}\n\n"

        search_prompt = {"role": "system", "content":
            f"{CORE_SYSTEM_RULE}\nСегодня: {get_current_date()} {get_current_time()}.\n"
            f'Вопрос: "{user_message}"\n\n{search_text}\n'
            f"ОТВЕЧАЙ ТОЛЬКО НА ОСНОВЕ НАЙДЕННЫХ ДАННЫХ. Если данных недостаточно — так и скажи."}
        history.append({"role": "user", "content": user_message})
        answer, err = await ask_deepseek([search_prompt] + history)
        if err:
            return f"⚠️ {analyze_error(err)}", False, None
        return f"🔍 **Искал в интернете:** `{user_message}`\n\n{answer}", True, "🌐 из интернета"

    # Поиск по дате
    date_match = re.search(r'\b(сегодня|вчера|завтра|\d{2}\.\d{2}(\.\d{4})?|\d{4}-\d{2}-\d{2})\b', user_message, re.IGNORECASE)
    if date_match:
        date_str = parse_date_query(date_match.group(1))
        if date_str:
            res = search_by_date(user_id, date_str)
            if res:
                txt = "\n".join(f"{m.get('timestamp','')} {m.get('role','')}: {m.get('content','')[:100]}" for m in res[:10])
                answer = f"📅 Сообщения за {date_match.group(1)}:\n{txt}"
                if len(res) > 10:
                    answer += f"\n... и ещё {len(res)-10}"
                return answer, False, "📂 из памяти (по дате)"

    # Поиск по времени
    time_match = re.search(r'(\d{1,2}:\d{2}(:\d{2})?)', user_message)
    if time_match:
        res = search_by_time(user_id, time_match.group(1))
        if res:
            txt = "\n".join(f"{m.get('timestamp','')} {m.get('role','')}: {m.get('content','')[:100]}" for m in res[:5])
            answer = f"🕐 Найдено по времени {time_match.group(1)}:\n{txt}"
            if len(res) > 5:
                answer += f"\n... и ещё {len(res)-5}"
            return answer, False, "📂 из памяти (по времени)"

    # Обычный ответ
    system_msg = {"role": "system", "content":
        f"{CORE_SYSTEM_RULE}\nСегодня: {get_current_date()} {get_current_time()}. {build_profile_context(profile)}"}
    history.append({"role": "user", "content": user_message})
    answer, err = await ask_deepseek([system_msg] + history)
    if err:
        return f"⚠️ {analyze_error(err)}", False, None
    return answer, True, "🧠 из модели"

# ========== КОМАНДЫ ==========
def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS_LIST or user_id in ALLOWED_USERS_LIST

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    name = load_profile(user_id).get("name", "друг")
    await update.message.reply_text(
        f"👋 Привет, {name}!\n\n"
        f"📅 Сегодня: {get_current_date()} {get_current_time()}\n\n"
        f"{get_peak_status()}\n\n"
        "🛡 **Мой принцип: никогда не врать. Если не знаю — скажу честно.**\n\n"
        "🧠 **Пирамидальная память (1 000 000+ сообщений):**\n"
        "• 📝 80 последних (полностью)\n"
        "• 📚📖📕📗 старые — сжато\n\n"
        "🕐 Дату и время отвечаю точно (динамически).\n"
        "📂 В каждом ответе указываю источник (📂 память / 🌐 интернет / 🧠 модель).\n\n"
        "🔍 Поиск сам включается для погоды/курсов/новостей.\n"
        "🔍 Принудительный поиск: `бро <запрос>`\n\n"
        "📋 Команды: /profile /stats /memory /forget /restore"
    )

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    profile = load_profile(user_id)
    if not profile:
        await update.message.reply_text("📭 Я пока ничего не знаю о тебе.")
        return
    lines = ["🧠 **Пирамидальная память:**\n"]
    level_labels = {'level_2': '📚 уровень 2', 'level_3': '📖 уровень 3',
                    'level_4': '📕 уровень 4', 'level_5': '📗 уровень 5'}
    for key, label in level_labels.items():
        lines.append(f"• {label}: {len(profile.get(key, []))} пунктов")
    lines.append(f"• 📝 активная история: {len(load_memory_raw(user_id))} сообщений")
    lines.append("\n👤 **Личная информация:**")
    found = False
    personal_keys = ['name', 'город', 'city', 'работа', 'job', 'возраст', 'age', 'факты']
    for key in personal_keys:
        if key in profile:
            lines.append(f"• {key}: {profile[key]}")
            found = True
    if not found:
        lines.append("• Пока ничего не запомнил")
    lines.append(f"\n⏰ {get_peak_status()}")
    lines.append(f"🔄 Обновлено: {profile.get('updated', 'неизвестно')}")
    await update.message.reply_text("\n".join(lines))

async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    if not context.args:
        await update.message.reply_text(
            "🔍 Поиск в памяти:\n`/memory что искать`\n"
            "Например: `/memory погода`, `/memory 13:44`, `/memory 14.07.2026`"
        )
        return
    query = ' '.join(context.args)
    date_str = parse_date_query(query)
    if date_str:
        res = search_by_date(user_id, date_str)
        if res:
            lines = [f"📅 Сообщения за {query}:\n"]
            lines += [f"{m.get('timestamp','')} {m.get('role','')}: {m.get('content','')[:100]}" for m in res[:10]]
            if len(res) > 10:
                lines.append(f"\n... и ещё {len(res)-10}")
            await update.message.reply_text("\n".join(lines))
            return
    time_match = re.search(r'(\d{1,2}:\d{2}(:\d{2})?)', query)
    if time_match:
        res = search_by_time(user_id, time_match.group(1))
        if res:
            lines = [f"🕐 Найдено по времени {time_match.group(1)}:\n"]
            lines += [f"{m.get('timestamp','')} {m.get('role','')}: {m.get('content','')[:100]}" for m in res[:5]]
            if len(res) > 5:
                lines.append(f"\n... и ещё {len(res)-5}")
            await update.message.reply_text("\n".join(lines))
            return
    results = search_in_pyramid(user_id, query)
    if not results:
        await update.message.reply_text(f"📭 Ничего не найдено по запросу: '{query}'")
        return
    lines = [f"🔍 Результаты поиска: '{query}'\n"]
    lines += [f"{i}. {r}" for i, r in enumerate(results[:10], 1)]
    if len(results) > 10:
        lines.append(f"\n... и ещё {len(results)-10}")
    await update.message.reply_text("\n".join(lines))

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    profile = load_profile(user_id)
    raw_history = load_memory_raw(user_id)
    lines = ["📊 **Статистика памяти (точные данные):**\n"]
    counter = load_counter(user_id)
    lines.append(f"• 📊 Обработано сообщений (реально): {counter}")
    lines.append(f"• 📝 В активной истории: {len(raw_history)} сообщений")
    total_punkts = 0
    level_labels = {'level_2': '📚 уровень 2', 'level_3': '📖 уровень 3',
                    'level_4': '📕 уровень 4', 'level_5': '📗 уровень 5'}
    for key, label in level_labels.items():
        cnt = len(profile.get(key, []))
        total_punkts += cnt
        lines.append(f"• {label}: {cnt} сжатых пунктов")
    lines.append(f"\n📦 Всего сжатых пунктов: {total_punkts}")
    lines.append("ℹ️ Каждый пункт — это сжатая выжимка, точное число исходных сообщений не хранится.")
    backup_count = len([f for f in os.listdir(BACKUP_DIR) if f.startswith(f"profile_{user_id}_")])
    lines.append(f"💾 Бэкапов профиля: {backup_count}")
    lines.append(f"⏰ {get_peak_status()}")
    lines.append(f"🔄 Обновлён: {profile.get('updated', 'неизвестно')}")
    await update.message.reply_text("\n".join(lines))

async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    lock = get_user_lock(user_id)
    async with lock:
        save_profile(user_id, {})
        await save_memory(user_id, [], backup=True, lock_held=True)
        save_counter(user_id, 0)
    await update.message.reply_text("🧹 Я забыл всё, что знал о тебе!")

async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    p = await restore_backup(user_id, "profile")
    m = await restore_backup(user_id, "memory")
    if p or m:
        await update.message.reply_text(
            "✅ Восстановлено из бэкапа!\n"
            f"{'📋 Профиль восстановлен' if p else ''}\n{'💬 История восстановлена' if m else ''}"
        )
    else:
        await update.message.reply_text("❌ Нет бэкапов для восстановления.")

# ========== RATE LIMIT ==========
async def check_rate_limit(user_id: int) -> bool:
    async with rate_lock:
        now_ts = datetime.now().timestamp()
        request_count[user_id] = [t for t in request_count.get(user_id, []) if now_ts - t < RATE_WINDOW]
        if len(request_count[user_id]) >= RATE_LIMIT:
            return False
        request_count[user_id].append(now_ts)
        # Удаляем пустые списки
        for uid in list(request_count.keys()):
            if not request_count[uid]:
                del request_count[uid]
        return True

# ========== ФОНОВЫЕ ЗАДАЧИ ==========
async def clean_request_count() -> None:
    """Удаляет записи пользователей, неактивных более INACTIVITY_TIMEOUT секунд."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        async with rate_lock:
            now_ts = datetime.now().timestamp()
            to_delete = [uid for uid, timestamps in request_count.items()
                         if not timestamps or now_ts - timestamps[-1] > INACTIVITY_TIMEOUT]
            for uid in to_delete:
                del request_count[uid]
            if to_delete:
                logger.debug(f"Очищено {len(to_delete)} неактивных записей в rate_limit")

async def clean_user_locks() -> None:
    """Удаляет замки для пользователей, неактивных более INACTIVITY_TIMEOUT секунд."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        # Получаем список активных пользователей из request_count
        async with rate_lock:
            active_users = set(request_count.keys())
        # Удаляем замки для неактивных
        for uid in list(user_locks.keys()):
            if uid not in active_users:
                del user_locks[uid]
                logger.debug(f"Очищен замок для пользователя {uid}")

# ========== ОТПРАВКА ДЛИННЫХ СООБЩЕНИЙ ==========
async def send_long_message(update: Update, text: str) -> None:
    for attempt in range(3):
        try:
            if len(text) > 4096:
                for i in range(0, len(text), 4096):
                    await update.message.reply_text(text[i:i+4096])
            else:
                await update.message.reply_text(text)
            return
        except Exception as e:
            if attempt == 2:
                await update.message.reply_text(analyze_error(str(e)))
            else:
                await asyncio.sleep(1)

# ========== ОСНОВНОЙ ОБРАБОТЧИК ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    if not await check_rate_limit(user_id):
        await update.message.reply_text("⏳ Слишком много запросов. Подождите 5 секунд.")
        return

    user_message = update.message.text
    if len(user_message) > MAX_MSG_LEN:
        user_message = user_message[:MAX_MSG_LEN] + "... (сообщение обрезано)"

    if user_message.lower().startswith("запомни "):
        text = user_message[8:].strip()
        lock = get_user_lock(user_id)
        async with lock:
            profile = load_profile(user_id)
            if ":" in text:
                key, value = text.split(":", 1)
                profile[key.strip()] = value.strip()
                save_profile(user_id, profile)
                await update.message.reply_text(f"✅ Запомнил: {key.strip()} = {value.strip()}")
            else:
                profile.setdefault("факты", []).append(text)
                save_profile(user_id, profile)
                await update.message.reply_text(f"✅ Запомнил факт: {text}")
        return

    force_internet = False
    status_msg = None
    if user_message.lower().startswith("бро "):
        search_query = user_message[4:].strip()
        if not search_query:
            await update.message.reply_text("❌ Напиши, что искать после 'бро'.")
            return
        user_message = search_query
        force_internet = True

    analysis_result = await analyze_message(user_id, user_message)
    if force_internet and analysis_result.get("action") != "date_time":
        analysis_result["action"] = "internet"

    logger.info(f"📊 Анализ user={user_id}: {analysis_result}")
    history = load_memory(user_id)
    profile = load_profile(user_id)

    if analysis_result.get("action") == "internet":
        status_msg = await update.message.reply_text("🌐 Ищу информацию в интернете...")

    answer, should_save, source = await generate_response(user_id, user_message, analysis_result, history, profile)

    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    if source and not answer.startswith(("⚠️", "✅")):
        answer = f"{source}\n\n{answer}"
    if is_peak_hour() and not answer.startswith("⚠️"):
        answer = f"⏰ Внимание: сейчас пиковые часы DeepSeek. Стоимость API удвоена.\n\n{answer}"

    if should_save:
        now_str = now().strftime("%Y-%m-%d %H:%M:%S")
        history.append({"role": "user", "content": user_message, "timestamp": now_str})
        history.append({"role": "assistant", "content": answer, "timestamp": now_str})
        await save_memory(user_id, history)

    await send_long_message(update, answer)

# ========== ОБРАБОТЧИК ОШИБОК ==========
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Глобальная ошибка: {context.error}")
    import traceback
    traceback.print_exc()
    if update and update.effective_message:
        await update.effective_message.reply_text(analyze_error(str(context.error)))

# ========== ЗАКРЫТИЕ СЕССИИ ==========
async def shutdown_session() -> None:
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
        logger.info("🔒 HTTP-сессия закрыта")

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(CommandHandler("restore", restore_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    # Запускаем фоновые задачи
    cleanup_task = asyncio.create_task(clean_request_count())
    lock_cleanup_task = asyncio.create_task(clean_user_locks())

    logger.info("✅ БОТ ЗАПУЩЕН. Готов к работе.")
    try:
        app.run_polling()
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен пользователем")
    finally:
        # Отменяем фоновые задачи
        cleanup_task.cancel()
        lock_cleanup_task.cancel()
        if _http_session and not _http_session.closed:
            try:
                asyncio.run(shutdown_session())
            except Exception as e:
                logger.error(f"Ошибка при закрытии сессии: {e}")

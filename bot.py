# ============================================================
#  BroWaix Bot — максимально защищённая версия
#  Принципы: не врать, точные ответы, не терять память
#  + Автоматическое восстановление из бэкапов при старте
# ============================================================
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
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters,
)

# ---------- ЛОГИ ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
APISERPENT_API_KEY = os.getenv("APISERPENT_API_KEY")

try:
    ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
except ValueError:
    ADMIN_USER_ID = 0

ALLOWED_USERS_LIST = []
_raw_allowed = os.getenv("ALLOWED_USERS", "")
if _raw_allowed:
    try:
        ALLOWED_USERS_LIST = [int(x.strip()) for x in _raw_allowed.split(",") if x.strip()]
    except ValueError:
        logger.warning("Ошибка в ALLOWED_USERS — проверьте формат (числа через запятую)")
if ADMIN_USER_ID != 0 and ADMIN_USER_ID not in ALLOWED_USERS_LIST:
    ALLOWED_USERS_LIST.append(ADMIN_USER_ID)

# ========== ЧАСОВОЙ ПОЯС ==========
TIMEZONE_STR = os.getenv("TIMEZONE", "Europe/Moscow")
try:
    TZ = ZoneInfo(TIMEZONE_STR)
except ZoneInfoNotFoundError:
    logger.warning(f"Часовой пояс '{TIMEZONE_STR}' не найден, используется UTC")
    TZ = ZoneInfo("UTC")
except Exception as e:
    logger.warning(f"Ошибка установки часового пояса: {e}, используется UTC")
    TZ = ZoneInfo("UTC")

def now():
    return datetime.now(TZ)

def get_current_date():
    return now().strftime("%d.%m.%Y")

def get_current_time():
    return now().strftime("%H:%M")

def get_current_weekday():
    return now().strftime("%A")

# ---------- МОДЕЛЬ ----------
MODEL_DEFAULT = os.getenv("MODEL_DEFAULT", "deepseek-v4-flash")
MODEL_FALLBACK = os.getenv("MODEL_FALLBACK", "deepseek-v4-pro")
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
SEARCH_ENGINE = os.getenv("SEARCH_ENGINE", "google")
MODEL_TEMPERATURE = float(os.getenv("MODEL_TEMPERATURE", "0.1"))

CORE_SYSTEM_RULE = (
    "Ты — честный ассистент. КРИТИЧЕСКИЕ ПРАВИЛА:\n"
    "1. НИКОГДА не выдумывай факты. Не знаешь — прямо скажи «Я не знаю».\n"
    "2. Не придумывай числа, даты, курсы, имена. Лучше признать незнание, чем соврать.\n"
    "3. По результатам интернет-поиска опирайся ТОЛЬКО на них.\n"
    "4. Если данные могли устареть — предупреди."
)

# ---------- УРОВНИ ПАМЯТИ ----------
LEVEL_1 = {'max_history': 80, 'keep_recent': 20}
LEVEL_2 = {'compress_interval': 40, 'compress_to': 50}
LEVEL_3 = {'compress_interval': 200, 'compress_to': 100}
LEVEL_4 = {'compress_interval': 1000, 'compress_to': 200}
LEVEL_5 = {'compress_interval': 10000, 'compress_to': 500}

PEAK_HOURS = [(9, 12), (14, 18)]

def is_peak_hour():
    hour = now().hour
    return any(s <= hour < e for s, e in PEAK_HOURS)

def get_peak_status():
    if is_peak_hour():
        return "⚠️ Сейчас пиковые часы DeepSeek (9:00–12:00, 14:00–18:00) — стоимость API удвоена."
    return "✅ Сейчас непиковые часы DeepSeek — стандартная стоимость."

if not TELEGRAM_TOKEN or not DEEPSEEK_API_KEY:
    logger.error("TELEGRAM_TOKEN или DEEPSEEK_API_KEY не заданы")
    sys.exit(1)
if not APISERPENT_API_KEY:
    logger.warning("APISERPENT_API_KEY не задан — интернет-поиск недоступен.")

logger.info("=" * 50)
logger.info("🚀 БОТ ЗАПУЩЕН")
logger.info(f"  🧠 Модель: {MODEL_DEFAULT} (fallback: {MODEL_FALLBACK}, temp={MODEL_TEMPERATURE})")
logger.info(f"  🕐 Часовой пояс: {TZ.key}")
logger.info(f"  👤 ADMIN: {ADMIN_USER_ID} | 👥 разрешено: {len(ALLOWED_USERS_LIST)}")
logger.info(f"  💾 Данные — отдельный файл на пользователя (изоляция аккаунтов)")
logger.info(f"  🛡 Принцип: не врать, точные ответы, атомарная запись + бэкапы")
logger.info("=" * 50)

# ---------- ПУТИ ----------
DATA_DIR = "data"
BACKUP_DIR = "data/backups"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

def memory_path(uid): return os.path.join(DATA_DIR, f"memory_{uid}.json")
def profile_path(uid): return os.path.join(DATA_DIR, f"profile_{uid}.json")
def counter_path(uid): return os.path.join(DATA_DIR, f"counter_{uid}.json")

# ---------- ГЛОБАЛЬНЫЕ ----------
_http_session = None
user_locks = {}
rate_lock = asyncio.Lock()
request_count = {}

def get_user_lock(uid):
    return user_locks.setdefault(uid, asyncio.Lock())

async def get_http_session():
    global _http_session
    if _http_session is None or _http_session.closed:
        connector = aiohttp.TCPConnector(limit=50, limit_per_host=20,
                                         keepalive_timeout=30, enable_cleanup_closed=True)
        timeout = aiohttp.ClientTimeout(total=60, connect=10, sock_read=30)
        _http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return _http_session

# ---------- ОБРАБОТКА ОШИБОК ----------
def analyze_error(error_text):
    e = str(error_text).lower()
    if "timeout" in e or "timed out" in e:
        return "⏰ Превышено время ожидания ответа от сервера. Попробуйте позже."
    if "connection" in e or "network" in e or "disconnected" in e:
        return "🌐 Проблемы с соединением. Проверьте интернет и повторите."
    if "429" in error_text or "too many requests" in e:
        return "📊 Слишком много запросов. Подождите минуту."
    if "401" in error_text or "unauthorized" in e:
        return "🔑 Ошибка авторизации API. Проверьте DEEPSEEK_API_KEY."
    if "400" in error_text or "bad request" in e:
        return ("⚠️ Некорректный запрос. Возможно, устарело имя модели.\n"
                "Актуальные: deepseek-v4-flash / deepseek-v4-pro.")
    if "404" in error_text or "not found" in e:
        return ("🔍 Модель/ресурс не найдены. deepseek-chat и deepseek-reasoner "
                "выводятся из эксплуатации. Используйте deepseek-v4-flash / deepseek-v4-pro.")
    if "500" in error_text or "server_error" in e or "internal server" in e:
        return "⚠️ Внутренняя ошибка сервера API. Повторите позже."
    if "empty" in e:
        return "📭 Пустой ответ от сервера. Переформулируйте вопрос."
    if "max_retries" in e:
        return "⚠️ Не удалось получить ответ после нескольких попыток."
    return f"⚠️ Ошибка: {str(error_text)[:150]}"

# ---------- АТОМАРНЫЕ ФАЙЛЫ ----------
def atomic_write(filename, data, as_json=True):
    tmp = filename + ".tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            if as_json:
                json.dump(data, f, ensure_ascii=False, indent=2)
            else:
                f.write(data)
            f.flush()
            os.fsync(f.fileno())
        shutil.move(tmp, filename)
        return True
    except Exception as ex:
        logger.error(f"Ошибка записи {filename}: {ex}")
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass
        return False

def atomic_read(filename, default=None, as_json=True):
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f) if as_json else f.read()
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as ex:
        logger.warning(f"Ошибка чтения {filename}: {ex} — пробую восстановить из бэкапа")
        return default

# ---------- ПРОФИЛЬ / СЧЁТЧИК ----------
def load_profile(uid):
    return atomic_read(profile_path(uid), default={})

def save_profile(uid, profile, backup=True):
    profile["updated"] = now().strftime("%d.%m.%Y %H:%M:%S")
    if not atomic_write(profile_path(uid), profile):
        return False
    if backup:
        create_backup(uid, "profile")
    return True

def load_counter(uid):
    return atomic_read(counter_path(uid), default={"count": 0}).get("count", 0)

def save_counter(uid, count):
    atomic_write(counter_path(uid), {"count": count})

def load_memory_raw(uid):
    return atomic_read(memory_path(uid), default=[])

# ---------- БЭКАПЫ ----------
def create_backup(uid, data_type):
    try:
        ts = now().strftime("%Y%m%d_%H%M%S")
        fname = f"{BACKUP_DIR}/{data_type}_{uid}_{ts}.json"
        if data_type == "profile":
            atomic_write(fname, load_profile(uid))
        elif data_type == "memory":
            atomic_write(fname, load_memory_raw(uid))
        backups = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith(f"{data_type}_{uid}_"))
        for old in backups[:-10]:
            try: os.remove(os.path.join(BACKUP_DIR, old))
            except OSError: pass
        return True
    except Exception as ex:
        logger.error(f"Ошибка бэкапа: {ex}")
        return False

async def restore_backup(uid, data_type):
    async with get_user_lock(uid):
        try:
            backups = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith(f"{data_type}_{uid}_"))
            if not backups:
                return False
            with open(os.path.join(BACKUP_DIR, backups[-1]), 'r', encoding='utf-8') as f:
                data = json.load(f)
            if data_type == "profile":
                save_profile(uid, data, backup=False)
            elif data_type == "memory":
                await save_memory(uid, data, backup=False, lock_held=True)
            logger.info(f"🔄 Восстановлен {data_type} {uid} из {backups[-1]}")
            return True
        except Exception as ex:
            logger.error(f"Ошибка восстановления {data_type}: {ex}")
            return False

# ---------- СЖАТИЕ ----------
STOP_WORDS = {'это', 'так', 'вот', 'ну', 'просто', 'очень'}

def extract_key_points(text, max_len=30):
    if len(text) <= max_len: return text
    imp = [w for w in text.split() if w.lower() not in STOP_WORDS and len(w) > 2]
    return ' '.join(imp[:10])[:max_len] + "..."

def extract_aggressive(text, max_len=20):
    if len(text) <= max_len: return text
    imp = [w[:8] for w in text.split() if len(w) > 3 and w.lower() not in STOP_WORDS]
    return ' '.join(imp[:5])[:max_len] + "..."

def extract_ultra(text, max_len=12):
    if len(text) <= max_len: return text
    imp = [w[:5] for w in text.split() if len(w) > 3 and w.lower() not in STOP_WORDS]
    return ' '.join(imp[:3])[:max_len] + "..."

def compress_ultra_old(items, target=50):
    if len(items) <= target: return items
    old = items[:200]
    out = []
    for i in range(0, len(old), 4):
        out.append("[архив] " + " | ".join(x[:20] for x in old[i:i+4]))
    res = out + items[-target:]
    return res[-target:] if len(res) > target + 10 else res

def compress_history(history):
    if len(history) <= LEVEL_1['max_history']:
        return history
    recent = history[-LEVEL_1['keep_recent']:]
    old = history[:-LEVEL_1['keep_recent']]
    summary = []
    for m in old[-10:]:
        r, c = m.get("role", ""), m.get("content", "")
        if r == "user": summary.append(f"Q: {extract_key_points(c, 50)}")
        elif r == "assistant": summary.append(f"A: {extract_key_points(c, 50)}")
    if summary:
        return [{"role": "system", "content": "📚 История (сжато):\n" + "\n".join(summary[-5:])}] + recent
    return recent

def load_memory(uid):
    return compress_history(load_memory_raw(uid))

def _update_level(uid, messages, key, cfg, extractor, ext_len, ts_fmt):
    profile = load_profile(uid)
    profile.setdefault(key, [])
    ts = now().strftime(ts_fmt)
    for m in messages[-cfg['compress_interval']:]:
        r, c = m.get("role", ""), m.get("content", "")
        if r == "user": profile[key].append(f"[{ts}] Q: {extractor(c, ext_len)}")
        elif r == "assistant": profile[key].append(f"[{ts}] A: {extractor(c, ext_len)}")
    if key == "level_5" and len(profile["level_5"]) > cfg['compress_to'] + 100:
        profile["level_5"] = compress_ultra_old(profile["level_5"][:200], 50) + profile["level_5"][200:]
    if len(profile[key]) > cfg['compress_to']:
        profile[key] = profile[key][-cfg['compress_to']:]
    save_profile(uid, profile, backup=False)

async def _save_memory_impl(uid, history, backup):
    try:
        try:
            if len(history) > LEVEL_1['max_history']:
                old = history[:-LEVEL_1['keep_recent']]
                if old:
                    _update_level(uid, old, "level_2", LEVEL_2, extract_key_points, 30, "%d.%m")
                    p = load_profile(uid)
                    if len(p.get("level_2", [])) >= LEVEL_2['compress_to']:
                        _update_level(uid, old, "level_3", LEVEL_3, extract_aggressive, 25, "%m.%d")
                    if len(p.get("level_3", [])) >= LEVEL_3['compress_to']:
                        _update_level(uid, old, "level_4", LEVEL_4, extract_aggressive, 20, "%m.%d")
                    if len(p.get("level_4", [])) >= LEVEL_4['compress_to']:
                        _update_level(uid, old, "level_5", LEVEL_5, extract_ultra, 15, "%y.%m")
        except Exception as ex:
            logger.error(f"Ошибка уровней {uid}: {ex}")
        if not atomic_write(memory_path(uid), compress_history(history)):
            logger.error(f"Не удалось сохранить историю {uid}")
            return False
        if backup:
            create_backup(uid, "memory")
        cnt = load_counter(uid) + 1
        save_counter(uid, cnt)
        if cnt % 10 == 0:
            create_backup(uid, "profile")
        return True
    except Exception as ex:
        logger.error(f"Критическая ошибка сохранения {uid}: {ex}")
        return False

async def save_memory(uid, history, backup=True, lock_held=False):
    if lock_held:
        return await _save_memory_impl(uid, history, backup)
    async with get_user_lock(uid):
        return await _save_memory_impl(uid, history, backup)

# ---------- ПОИСК ПО ДАТЕ/ВРЕМЕНИ ----------
def parse_time_query(tq):
    try:
        parts = tq.split(":")
        if len(parts) == 2: return int(parts[0]), int(parts[1])
        if len(parts) == 3: return int(parts[0]), int(parts[1])
    except ValueError:
        pass
    return None, None

def search_by_time(uid, tq):
    res, (qh, qm) = [], parse_time_query(tq)
    if qh is None: return res
    for m in load_memory_raw(uid):
        ts = m.get("timestamp", "")
        if not ts: continue
        try:
            mt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            if mt.hour == qh and mt.minute == qm: res.append(m)
        except ValueError:
            if tq in ts: res.append(m)
    return res

def parse_date_query(query):
    q = query.lower().strip()
    n = now()
    if q == "сегодня": return n.strftime("%Y-%m-%d")
    if q == "вчера": return (n - timedelta(days=1)).strftime("%Y-%m-%d")
    if q == "завтра": return (n + timedelta(days=1)).strftime("%Y-%m-%d")
    for pat in [r'(\d{2})\.(\d{2})\.(\d{4})', r'(\d{2})\.(\d{2})', r'(\d{4})-(\d{2})-(\d{2})']:
        m = re.search(pat, query)
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

def search_by_date(uid, date_str):
    return [m for m in load_memory_raw(uid) if m.get("timestamp", "").startswith(date_str)]

def search_in_pyramid(uid, query):
    profile, q, res = load_profile(uid), query.lower(), []
    for m in load_memory_raw(uid)[-40:]:
        c = m.get("content", "")
        if q in c.lower():
            role = "👤" if m.get("role") == "user" else "🤖"
            ts = m.get("timestamp", "")
            res.append(f"{role}{(' ['+ts+']') if ts else ''} {extract_key_points(c, 80)}")
    for lvl, em in [("level_2", "📚"), ("level_3", "📖"), ("level_4", "📕"), ("level_5", "📗")]:
        for item in profile.get(lvl, []):
            if q in item.lower(): res.append(f"{em} {item}")
    return res[:15]

# ---------- АНАЛИЗ СООБЩЕНИЯ ----------
async def analyze_message(user_message):
    q = user_message.lower().strip()
    confirm = ['да','нет','ок','хорошо','понял','поняла','ага','угу','ясно','ладно','окей']
    if q in confirm or q.rstrip('.!') in confirm: return {"action": "confirm"}
    greet = ['привет','здравствуй','здрасте','приветствую','салют','hello','hi']
    if q in greet or q.rstrip('!') in greet: return {"action": "greeting"}
    if any(t in q for t in ['имя','город','работа','возраст','интерес','хобби','меня зовут']):
        return {"action": "memory"}
    if any(t in q for t in ['помнишь','напомни','что я говорил','что я писал','вспомни']):
        return {"action": "memory"}

    # Проверка на вопрос о дате/времени (локальный ответ)
    dt_kw = ['дата','время','число','который час','сколько времени','какая дата',
             'какое сегодня число','какой сегодня день','текущее время']
    if any(k in q for k in dt_kw):
        return {"action": "date_time"}

    # ===== НОВЫЙ БЛОК: автоматический интернет-поиск, если есть указание на сегодня/завтра/вчера/дату =====
    date_indicators = ['сегодня', 'завтра', 'вчера']
    has_date = any(ind in q for ind in date_indicators) or re.search(r'\d{2}\.\d{2}(\.\d{4})?', q)
    if has_date:
        return {"action": "internet"}

    # Остальные динамические триггеры (погода, курс, новости)
    dyn = ['погод','температур','прогноз','осадк','курс валют','курс доллар','курс евро',
           'курс юан','биткоин','котировк','последние новости','свежие новости','что произошло сегодня']
    if any(t in q for t in dyn):
        return {"action": "internet"}

    return {"action": "memory"}

# ---------- APISERPENT ----------
async def search_apiserpent_async(query):
    if not APISERPENT_API_KEY:
        return []
    session = await get_http_session()
    try:
        logger.info(f"🔍 Поиск ({SEARCH_ENGINE}): {query}")
        async with session.get("https://apiserpent.com/api/search",
                               params={"q": query, "engine": SEARCH_ENGINE, "num": 5},
                               headers={"X-API-Key": APISERPENT_API_KEY}, timeout=30) as r:
            if r.status != 200:
                logger.error(f"APISerpent HTTP {r.status}")
                return []
            data = await r.json()
            results = []
            if isinstance(data.get("results"), dict): results = data["results"].get("organic", [])
            elif "organic_results" in data: results = data["organic_results"]
            elif isinstance(data.get("results"), list): results = data["results"]
            elif "organic" in data: results = data["organic"]
            elif "items" in data: results = data["items"]
            if not results and isinstance(data, dict):
                for k in data:
                    if isinstance(data[k], list) and data[k] and isinstance(data[k][0], dict):
                        results = data[k]; break
            out = []
            for x in results[:5]:
                if isinstance(x, dict):
                    out.append({
                        "title": str(x.get("title", x.get("name", "Без названия")))[:150],
                        "snippet": str(x.get("snippet", x.get("description", x.get("text", "Нет описания"))))[:250],
                        "link": str(x.get("url", x.get("link", x.get("href", "#"))))[:150],
                    })
            if not out:
                logger.warning(f"APISerpent пусто. Сырой ответ: {str(data)[:300]}")
            return out
    except asyncio.TimeoutError:
        logger.error("⏰ Таймаут APISerpent"); return []
    except Exception as ex:
        logger.error(f"Ошибка APISerpent: {ex}"); return []

# ---------- DEEPSEEK ----------
async def ask_deepseek(messages, retries=3, max_tokens=None, model=None):
    session = await get_http_session()
    use_model = model or MODEL_DEFAULT
    for attempt in range(retries):
        try:
            payload = {"model": use_model, "messages": messages, "temperature": MODEL_TEMPERATURE}
            if max_tokens: payload["max_tokens"] = max_tokens
            async with session.post(f"{DEEPSEEK_API_BASE}/chat/completions",
                                    headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                                    json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("choices"):
                        c = data["choices"][0].get("message", {}).get("content")
                        return (c, None) if c else (None, "empty")
                    return None, "invalid_response"
                if resp.status == 429:
                    await asyncio.sleep(min(2 ** attempt, 30)); continue
                if resp.status in (400, 404) and use_model != MODEL_FALLBACK:
                    logger.warning(f"Модель '{use_model}' недоступна (HTTP {resp.status}). Пробую '{MODEL_FALLBACK}'.")
                    use_model = MODEL_FALLBACK; continue
                return None, f"http_{resp.status}"
        except (aiohttp.ClientResponseError, aiohttp.ServerDisconnectedError,
                aiohttp.ClientConnectionError, asyncio.TimeoutError) as ex:
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt); continue
            return None, type(ex).__name__.lower()
        except Exception as ex:
            if attempt < retries - 1: continue
            return None, f"unknown: {ex}"
    return None, "max_retries"

def build_profile_context(profile):
    parts = []
    for k, v in profile.items():
        if k in ("updated","level_2","level_3","level_4","level_5") or k.startswith(("last_check_","update_history_")):
            continue
        if isinstance(v, list):
            if v: parts.append(f"{k}: {', '.join(str(x)[:50] for x in v[:3])}")
        else:
            parts.append(f"{k}: {str(v)[:50]}")
    if profile.get("level_2"): parts.append("📚: " + ", ".join(profile['level_2'][-10:]))
    if profile.get("level_3"): parts.append("📖: " + ", ".join(profile['level_3'][-5:]))
    ctx = ". ".join(parts)
    return ctx[:800] + "..." if len(ctx) > 800 else ctx

# ---------- ГЕНЕРАЦИЯ ОТВЕТА ----------
async def generate_response(uid, user_message, analysis, history, profile):
    action = analysis.get("action", "memory")

    if action == "confirm":
        return "✅ Понял! Продолжаем.", False, None

    if action == "greeting":
        for k, v in {'привет':'👋 Привет! Как дела?','здравствуй':'👋 Здравствуйте!',
                     'пока':'👋 Пока!','спасибо':'Пожалуйста! 🤗'}.items():
            if k in user_message.lower():
                return v, False, None
        return "👋 Привет! Чем могу помочь?", False, None

    if action == "date_time":
        wd = {'Monday':'Понедельник','Tuesday':'Вторник','Wednesday':'Среда','Thursday':'Четверг',
              'Friday':'Пятница','Saturday':'Суббота','Sunday':'Воскресенье'}.get(get_current_weekday(), "")
        return f"📅 Сегодня: {get_current_date()} ({wd})\n🕐 Время: {get_current_time()}", False, "📂 локально"

    if action == "internet":
        results = await search_apiserpent_async(user_message)
        ctx = build_profile_context(profile)
        if not results:
            sysmsg = {"role": "system", "content":
                f"{CORE_SYSTEM_RULE}\nСегодня: {get_current_date()} {get_current_time()}. {ctx}\n"
                f"ВАЖНО: интернет-поиск не дал результатов. Если вопрос о текущих данных — "
                f"честно скажи, что не можешь проверить, и НЕ выдумывай."}
            history.append({"role": "user", "content": user_message})
            ans, err = await ask_deepseek([sysmsg] + history)
            if err: return f"⚠️ {analyze_error(err)}", False, None
            return (f"🔍 **Искал в интернете:** `{user_message}`\n\n"
                    f"❌ Ничего не найдено. Уточните запрос (например: 'бро погода в Москве').\n\n"
                    f"🧠 Ответ из знаний модели (может быть неактуален):\n{ans}"), True, "🧠 из модели (поиск пуст)"
        stext = f"🔍 **Искал:** `{user_message}`\n\n📊 Найдено {len(results)}:\n\n"
        for i, r in enumerate(results, 1):
            stext += f"{i}. **{r['title']}**\n   {r['snippet'][:200]}\n   🔗 {r['link']}\n\n"
        sp = {"role": "system", "content":
            f"{CORE_SYSTEM_RULE}\nСегодня: {get_current_date()} {get_current_time()}.\n"
            f'Вопрос: "{user_message}"\n\n{stext}\nОТВЕЧАЙ ТОЛЬКО ПО НАЙДЕННЫМ ДАННЫМ. Мало данных — скажи прямо.'}
        history.append({"role": "user", "content": user_message})
        ans, err = await ask_deepseek([sp] + history)
        if err: return f"⚠️ {analyze_error(err)}", False, None
        return f"🔍 **Искал в интернете:** `{user_message}`\n\n{ans}", True, "🌐 из интернета"

    # поиск по дате
    dm = re.search(r'\b(сегодня|вчера|завтра|\d{2}\.\d{2}(\.\d{4})?|\d{4}-\d{2}-\d{2})\b', user_message, re.I)
    if dm:
        ds = parse_date_query(dm.group(1))
        if ds:
            res = search_by_date(uid, ds)
            if res:
                txt = "\n".join(f"{m.get('timestamp','')} {m.get('role','')}: {m.get('content','')[:100]}" for m in res[:10])
                a = f"📅 Сообщения за {dm.group(1)}:\n{txt}"
                if len(res) > 10: a += f"\n... и ещё {len(res)-10}"
                return a, False, "📂 из памяти (по дате)"

    # поиск по времени
    tm = re.search(r'(\d{1,2}:\d{2}(:\d{2})?)', user_message)
    if tm:
        res = search_by_time(uid, tm.group(1))
        if res:
            txt = "\n".join(f"{m.get('timestamp','')} {m.get('role','')}: {m.get('content','')[:100]}" for m in res[:5])
            a = f"🕐 По времени {tm.group(1)}:\n{txt}"
            if len(res) > 5: a += f"\n... и ещё {len(res)-5}"
            return a, False, "📂 из памяти (по времени)"

    sysmsg = {"role": "system", "content":
        f"{CORE_SYSTEM_RULE}\nСегодня: {get_current_date()} {get_current_time()}. {build_profile_context(profile)}"}
    history.append({"role": "user", "content": user_message})
    ans, err = await ask_deepseek([sysmsg] + history)
    if err: return f"⚠️ {analyze_error(err)}", False, None
    return ans, True, "🧠 из модели"

# ---------- БЕЗОПАСНЫЙ ОТВЕТ ----------
async def safe_reply(update: Update, text: str):
    msg = update.effective_message
    if msg is None:
        logger.warning("safe_reply: нет effective_message"); return
    for attempt in range(3):
        try:
            if len(text) > 4096:
                for i in range(0, len(text), 4096):
                    await msg.reply_text(text[i:i+4096])
            else:
                await msg.reply_text(text)
            return
        except Exception as ex:
            if attempt == 2:
                logger.error(f"safe_reply не смог отправить: {ex}")
            else:
                await asyncio.sleep(1)

def is_allowed(uid):
    return not ALLOWED_USERS_LIST or uid in ALLOWED_USERS_LIST

# ---------- КОМАНДЫ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None: return
    uid = update.effective_user.id
    if not is_allowed(uid):
        await safe_reply(update, "❌ Доступ запрещён."); return
    name = load_profile(uid).get("name", "друг")
    await safe_reply(update,
        f"👋 Привет, {name}!\n\n📅 Сегодня: {get_current_date()} {get_current_time()}\n\n"
        f"{get_peak_status()}\n\n"
        "🛡 Мой принцип: никогда не врать. Не знаю — скажу честно.\n\n"
        "🧠 Пирамидальная память (1 000 000+): 80 последних полностью + сжатые уровни.\n"
        "🕐 Дату/время отвечаю точно.\n"
        "📂 Указываю источник: 📂 память / 🌐 интернет / 🧠 модель.\n\n"
        "🔍 Поиск сам включается для погоды/курсов/новостей.\n"
        "🔍 Принудительно: `бро <запрос>`\n\n"
        "📋 Команды: /profile /stats /memory /forget /restore")

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None: return
    uid = update.effective_user.id
    if not is_allowed(uid):
        await safe_reply(update, "❌ Доступ запрещён."); return
    p = load_profile(uid)
    if not p:
        await safe_reply(update, "📭 Я пока ничего не знаю о тебе."); return
    lines = ["🧠 **Память:**"]
    for k, lab in {'level_2':'📚 ур.2','level_3':'📖 ур.3','level_4':'📕 ур.4','level_5':'📗 ур.5'}.items():
        lines.append(f"• {lab}: {len(p.get(k, []))} пунктов")
    lines.append(f"• 📝 активная история: {len(load_memory_raw(uid))} сообщений")
    lines.append("\n👤 **Личное:**")
    exclude = {'updated', 'level_2', 'level_3', 'level_4', 'level_5'}
    personal_keys = [k for k in p.keys() if k not in exclude]
    if personal_keys:
        for k in personal_keys:
            lines.append(f"• {k}: {p[k]}")
    else:
        lines.append("• Пока ничего не запомнил")
    lines.append(f"\n⏰ {get_peak_status()}\n🔄 Обновлено: {p.get('updated','неизвестно')}")
    await safe_reply(update, "\n".join(lines))

async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None: return
    uid = update.effective_user.id
    if not is_allowed(uid):
        await safe_reply(update, "❌ Доступ запрещён."); return
    if not context.args:
        await safe_reply(update, "🔍 Поиск: `/memory что искать`\nПример: `/memory погода`, `/memory 13:44`, `/memory 14.07.2026`"); return
    query = ' '.join(context.args)
    ds = parse_date_query(query)
    if ds:
        res = search_by_date(uid, ds)
        if res:
            lines = [f"📅 За {query}:"] + [f"{m.get('timestamp','')} {m.get('role','')}: {m.get('content','')[:100]}" for m in res[:10]]
            if len(res) > 10: lines.append(f"... и ещё {len(res)-10}")
            await safe_reply(update, "\n".join(lines)); return
    tm = re.search(r'(\d{1,2}:\d{2}(:\d{2})?)', query)
    if tm:
        res = search_by_time(uid, tm.group(1))
        if res:
            lines = [f"🕐 По времени {tm.group(1)}:"] + [f"{m.get('timestamp','')} {m.get('role','')}: {m.get('content','')[:100]}" for m in res[:5]]
            if len(res) > 5: lines.append(f"... и ещё {len(res)-5}")
            await safe_reply(update, "\n".join(lines)); return
    res = search_in_pyramid(uid, query)
    if not res:
        await safe_reply(update, f"📭 Ничего не найдено: '{query}'"); return
    lines = [f"🔍 Результаты '{query}':"] + [f"{i}. {r}" for i, r in enumerate(res[:10], 1)]
    if len(res) > 10: lines.append(f"... и ещё {len(res)-10}")
    await safe_reply(update, "\n".join(lines))

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None: return
    uid = update.effective_user.id
    if not is_allowed(uid):
        await safe_reply(update, "❌ Доступ запрещён."); return
    p = load_profile(uid)
    raw = load_memory_raw(uid)
    lines = ["📊 **Статистика (точные данные):**"]
    lines.append(f"• Обработано сообщений (реально): {load_counter(uid)}")
    lines.append(f"• В активной истории: {len(raw)}")
    total = 0
    for k, lab in {'level_2':'📚 ур.2','level_3':'📖 ур.3','level_4':'📕 ур.4','level_5':'📗 ур.5'}.items():
        c = len(p.get(k, [])); total += c
        lines.append(f"• {lab}: {c} сжатых пунктов")
    lines.append(f"\n📦 Всего сжатых пунктов: {total}")
    lines.append("ℹ️ Пункт — это выжимка; точное число исходных сообщений не хранится.")
    bc = len([f for f in os.listdir(BACKUP_DIR) if f.startswith(f"profile_{uid}_")])
    lines.append(f"💾 Бэкапов профиля: {bc}\n⏰ {get_peak_status()}\n🔄 {p.get('updated','неизвестно')}")
    await safe_reply(update, "\n".join(lines))

async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None: return
    uid = update.effective_user.id
    if not is_allowed(uid):
        await safe_reply(update, "❌ Доступ запрещён."); return
    async with get_user_lock(uid):
        save_profile(uid, {})
        await save_memory(uid, [], backup=True, lock_held=True)
        save_counter(uid, 0)
    await safe_reply(update, "🧹 Я забыл всё, что знал о тебе!")

async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None: return
    uid = update.effective_user.id
    if not is_allowed(uid):
        await safe_reply(update, "❌ Доступ запрещён."); return
    pr = await restore_backup(uid, "profile")
    mr = await restore_backup(uid, "memory")
    if pr or mr:
        await safe_reply(update, "✅ Восстановлено!\n" +
                         ("📋 Профиль\n" if pr else "") + ("💬 История" if mr else ""))
    else:
        await safe_reply(update, "❌ Нет бэкапов для восстановления.")

# ---------- RATE LIMIT ----------
RATE_LIMIT, RATE_WINDOW = 3, 5
async def check_rate_limit(uid):
    async with rate_lock:
        now_ts = datetime.now().timestamp()
        request_count[uid] = [t for t in request_count.get(uid, []) if now_ts - t < RATE_WINDOW]
        if len(request_count[uid]) >= RATE_LIMIT:
            return False
        request_count[uid].append(now_ts)
        for u in list(request_count.keys()):
            if not request_count[u]: del request_count[u]
        return True

# ---------- АВТОМАТИЧЕСКОЕ ВОССТАНОВЛЕНИЕ ПРИ СТАРТЕ ----------
async def auto_restore_all_users():
    """При запуске восстанавливает профиль и память из последних бэкапов для всех пользователей,
    если основные файлы отсутствуют или пусты."""
    logger.info("🔄 Проверка данных при старте...")
    backup_files = os.listdir(BACKUP_DIR)
    user_ids = set()
    for fname in backup_files:
        parts = fname.split('_')
        if len(parts) >= 2 and parts[0] in ('profile', 'memory'):
            try:
                uid = int(parts[1])
                user_ids.add(uid)
            except ValueError:
                continue

    if not user_ids:
        logger.info("✅ Нет пользователей для восстановления.")
        return

    for uid in user_ids:
        mem_path = memory_path(uid)
        prof_path = profile_path(uid)
        need_restore = False

        mem_data = atomic_read(mem_path, default=None)
        if mem_data is None or (isinstance(mem_data, list) and len(mem_data) == 0):
            need_restore = True

        prof_data = atomic_read(prof_path, default=None)
        if prof_data is None or (isinstance(prof_data, dict) and len(prof_data) == 0):
            need_restore = True

        if need_restore:
            logger.info(f"🔄 Восстанавливаю данные для пользователя {uid} из бэкапов...")
            profile_restored = await restore_backup(uid, "profile")
            memory_restored = await restore_backup(uid, "memory")
            if profile_restored or memory_restored:
                logger.info(f"✅ Пользователь {uid} восстановлен (профиль: {profile_restored}, память: {memory_restored})")
            else:
                logger.warning(f"⚠️ Для пользователя {uid} бэкапов не найдено.")

# ---------- ГЛАВНЫЙ ОБРАБОТЧИК ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.effective_message is None:
        return
    if not update.effective_message.text:
        return
    uid = update.effective_user.id
    if not is_allowed(uid):
        await safe_reply(update, "❌ Доступ запрещён.")
        return
    if not await check_rate_limit(uid):
        await safe_reply(update, "⏳ Слишком много запросов. Подождите 5 секунд.")
        return

    user_message = update.effective_message.text
    if len(user_message) > 3500:
        user_message = user_message[:3500] + "... (обрезано)"

    # ---------- ОБРАБОТКА ПОДТВЕРЖДЕНИЯ ----------
    if context.user_data.get("awaiting_internet_confirm"):
        if user_message.lower() in ("да", "нет", "д", "н", "yes", "no"):
            if user_message.lower() in ("да", "д", "yes"):
                context.user_data["awaiting_internet_confirm"] = False
                query = context.user_data.get("pending_query")
                analysis = context.user_data.get("pending_analysis")
                if query and analysis:
                    await safe_reply(update, "🌐 Ищу информацию в интернете...")
                    history = load_memory(uid)
                    profile = load_profile(uid)
                    answer, should_save, source = await generate_response(uid, query, analysis, history, profile)
                    if source and not answer.startswith(("⚠️", "✅")):
                        answer = f"{source}\n\n{answer}"
                    if is_peak_hour() and not answer.startswith("⚠️"):
                        answer = f"⏰ Внимание: пиковые часы DeepSeek. Стоимость API удвоена.\n\n{answer}"
                    if should_save:
                        now_str = now().strftime("%Y-%m-%d %H:%M:%S")
                        history.append({"role": "user", "content": query, "timestamp": now_str})
                        history.append({"role": "assistant", "content": answer, "timestamp": now_str})
                        await save_memory(uid, history)
                    await safe_reply(update, answer)
                else:
                    await safe_reply(update, "❌ Ошибка: запрос потерян. Попробуйте заново.")
                context.user_data.pop("pending_query", None)
                context.user_data.pop("pending_analysis", None)
                return
            else:
                context.user_data["awaiting_internet_confirm"] = False
                query = context.user_data.get("pending_query")
                analysis = context.user_data.get("pending_analysis")
                if query and analysis:
                    analysis["action"] = "memory"
                    history = load_memory(uid)
                    profile = load_profile(uid)
                    answer, should_save, source = await generate_response(uid, query, analysis, history, profile)
                    if source and not answer.startswith(("⚠️", "✅")):
                        answer = f"{source}\n\n{answer}"
                    if is_peak_hour() and not answer.startswith("⚠️"):
                        answer = f"⏰ Внимание: пиковые часы DeepSeek. Стоимость API удвоена.\n\n{answer}"
                    if should_save:
                        now_str = now().strftime("%Y-%m-%d %H:%M:%S")
                        history.append({"role": "user", "content": query, "timestamp": now_str})
                        history.append({"role": "assistant", "content": answer, "timestamp": now_str})
                        await save_memory(uid, history)
                    await safe_reply(update, answer)
                else:
                    await safe_reply(update, "❌ Ошибка: запрос потерян. Попробуйте заново.")
                context.user_data.pop("pending_query", None)
                context.user_data.pop("pending_analysis", None)
                return
        else:
            await safe_reply(update, "❓ Напишите «да» или «нет» — я продолжу.")
            return

    # ---------- ЗАПОМИНАНИЕ ----------
    if user_message.lower().startswith("запомни "):
        text = user_message[8:].strip()
        async with get_user_lock(uid):
            p = load_profile(uid)
            if ":" in text:
                k, v = text.split(":", 1)
                k, v = k.strip(), v.strip()
                p[k] = v
                if save_profile(uid, p):
                    check_p = load_profile(uid)
                    if k in check_p and check_p[k] == v:
                        await safe_reply(update, f"✅ Запомнил: {k} = {v}")
                    else:
                        await safe_reply(update, "❌ Ошибка: запись не сохранилась. Попробуйте позже.")
                else:
                    await safe_reply(update, "❌ Не удалось сохранить. Проверьте права на запись в папку data/.")
            else:
                p.setdefault("факты", []).append(text)
                if save_profile(uid, p):
                    await safe_reply(update, f"✅ Запомнил факт: {text}")
                else:
                    await safe_reply(update, "❌ Не удалось сохранить факт. Проверьте права на запись.")
        return

    # ---------- ПРИНУДИТЕЛЬНЫЙ ПОИСК (бро) БЕЗ ПОДТВЕРЖДЕНИЯ ----------
    force_internet = False
    if user_message.lower().startswith("бро "):
        sq = user_message[4:].strip()
        if not sq:
            await safe_reply(update, "❌ Напиши, что искать после 'бро'.")
            return
        user_message = sq
        force_internet = True

    analysis = await analyze_message(user_message)

    if force_internet:
        analysis["action"] = "internet"
        status_msg = await update.effective_message.reply_text("🌐 Ищу информацию в интернете...")
        history = load_memory(uid)
        profile = load_profile(uid)
        answer, should_save, source = await generate_response(uid, user_message, analysis, history, profile)
        if status_msg:
            try: await status_msg.delete()
            except Exception: pass
        if source and not answer.startswith(("⚠️", "✅")):
            answer = f"{source}\n\n{answer}"
        if is_peak_hour() and not answer.startswith("⚠️"):
            answer = f"⏰ Внимание: пиковые часы DeepSeek. Стоимость API удвоена.\n\n{answer}"
        if should_save:
            now_str = now().strftime("%Y-%m-%d %H:%M:%S")
            history.append({"role": "user", "content": user_message, "timestamp": now_str})
            history.append({"role": "assistant", "content": answer, "timestamp": now_str})
            await save_memory(uid, history)
        await safe_reply(update, answer)
        return

    if analysis.get("action") == "internet":
        context.user_data["awaiting_internet_confirm"] = True
        context.user_data["pending_query"] = user_message
        context.user_data["pending_analysis"] = analysis
        await safe_reply(update, "🔍 Я могу поискать в интернете по вашему запросу. Напишите «да» или «нет».")
        return

    history = load_memory(uid)
    profile = load_profile(uid)
    answer, should_save, source = await generate_response(uid, user_message, analysis, history, profile)

    if source and not answer.startswith(("⚠️", "✅")):
        answer = f"{source}\n\n{answer}"
    if is_peak_hour() and not answer.startswith("⚠️"):
        answer = f"⏰ Внимание: пиковые часы DeepSeek. Стоимость API удвоена.\n\n{answer}"

    if should_save:
        now_str = now().strftime("%Y-%m-%d %H:%M:%S")
        history.append({"role": "user", "content": user_message, "timestamp": now_str})
        history.append({"role": "assistant", "content": answer, "timestamp": now_str})
        await save_memory(uid, history)

    await safe_reply(update, answer)

# ---------- ОБРАБОТЧИК ОШИБОК ----------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Глобальная ошибка: {context.error}")
    import traceback; traceback.print_exc()
    if isinstance(update, Update):
        await safe_reply(update, analyze_error(str(context.error)))

async def shutdown_session():
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
        logger.info("🔒 HTTP-сессия закрыта")

# ---------- ЗАПУСК ----------
if __name__ == "__main__":
    # 1. Автоматическое восстановление при старте
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(auto_restore_all_users())
    loop.close()

    # 2. Запуск бота
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(CommandHandler("restore", restore_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    logger.info("✅ БОТ ГОТОВ К РАБОТЕ.")
    try:
        app.run_polling()
    except KeyboardInterrupt:
        logger.info("👋 Остановлен пользователем")
    finally:
        if _http_session and not _http_session.closed:
            try:
                asyncio.run(shutdown_session())
            except Exception as ex:
                logger.error(f"Ошибка закрытия сессии: {ex}")

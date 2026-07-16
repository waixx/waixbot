# ============================================================
#  BroWaix Bot — УНИВЕРСАЛЬНАЯ ФИНАЛЬНАЯ ВЕРСИЯ 2026
#  (БЕЗ ХАРДКОДА, БЕЗ ЗАУЖЕНИЙ, РАБОТАЕТ С ЛЮБЫМИ ЗАПРОСАМИ)
#  Бюджет $7–8/мес, 150 запросов/день
# ============================================================
import logging, os, json, sys, re, asyncio, aiohttp, shutil, weakref, hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from logging.handlers import RotatingFileHandler

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = RotatingFileHandler("bot.log", maxBytes=10*1024*1024, backupCount=3)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
console = logging.StreamHandler()
console.setFormatter(formatter)
logger.addHandler(console)

load_dotenv()

# ---------- ПЕРЕМЕННЫЕ ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
APISERPENT_API_KEY = os.getenv("APISERPENT_API_KEY")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0") or 0)
ALLOWED_USERS_LIST = [int(x.strip()) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()]
if ADMIN_USER_ID and ADMIN_USER_ID not in ALLOWED_USERS_LIST:
    ALLOWED_USERS_LIST.append(ADMIN_USER_ID)

TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow") or "UTC")
def now(): return datetime.now(TZ)
def get_current_date(): return now().strftime("%d.%m.%Y")
def get_current_time(): return now().strftime("%H:%M")

MODEL_DEFAULT = os.getenv("MODEL_DEFAULT", "deepseek-v4-flash")
MODEL_FALLBACK = os.getenv("MODEL_FALLBACK", "deepseek-v4-pro")
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
SEARCH_ENGINE = os.getenv("SEARCH_ENGINE", "google")

# --- ПАРАМЕТРЫ ---
SEARCH_RESULTS_NUM = 10
SEARCH_VARIANTS_COUNT = 1
MODEL_TEMPERATURE = 0.1
MAX_RETRY_ATTEMPTS = 1
CACHE_TTL = 604800
MAX_TOKENS_ANSWER = 2048
MAX_TOKENS_DEEP = 4096
TOP_RESULTS_SHOW = 6

# ---------- ПАМЯТЬ ----------
LEVEL_1 = {'max_history': 40, 'keep_recent': 10}
LEVEL_2 = {'compress_interval': 20, 'compress_to': 30}

if not TELEGRAM_TOKEN or not DEEPSEEK_API_KEY:
    logger.error("Токены не заданы"); sys.exit(1)

DATA_DIR, BACKUP_DIR = "data", "data/backups"
os.makedirs(DATA_DIR, exist_ok=True); os.makedirs(BACKUP_DIR, exist_ok=True)

def memory_path(uid): return os.path.join(DATA_DIR, f"memory_{uid}.json")
def profile_path(uid): return os.path.join(DATA_DIR, f"profile_{uid}.json")
def counter_path(uid): return os.path.join(DATA_DIR, f"counter_{uid}.json")

_http_session = None
_session_lock = asyncio.Lock()
user_locks = weakref.WeakValueDictionary()
rate_lock = asyncio.Lock()
request_count = {}
search_cache = {}
query_hash_cache = {}

def get_user_lock(uid): return user_locks.setdefault(uid, asyncio.Lock())

async def get_http_session():
    global _http_session
    async with _session_lock:
        if _http_session is None or _http_session.closed:
            _http_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=20, limit_per_host=10),
                timeout=aiohttp.ClientTimeout(total=90, connect=10, sock_read=60)
            )
        return _http_session

# ---------- ФАЙЛЫ ----------
def atomic_write(filename, data, as_json=True):
    tmp = filename + ".tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            if as_json: json.dump(data, f, ensure_ascii=False, indent=2)
            else: f.write(data)
            f.flush(); os.fsync(f.fileno())
        shutil.move(tmp, filename)
        return True
    except Exception:
        if os.path.exists(tmp): os.remove(tmp)
        return False

def atomic_read(filename, default=None, as_json=True):
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f) if as_json else f.read()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default

def load_profile(uid): return atomic_read(profile_path(uid), default={})
def save_profile(uid, profile, backup=True):
    profile["updated"] = now().strftime("%d.%m.%Y %H:%M:%S")
    if not atomic_write(profile_path(uid), profile): return False
    if backup: create_backup(uid, "profile")
    return True
def load_counter(uid): return atomic_read(counter_path(uid), default={"count":0}).get("count",0)
def save_counter(uid, count): atomic_write(counter_path(uid), {"count":count})
def load_memory_raw(uid): return atomic_read(memory_path(uid), default=[])

def create_backup(uid, data_type):
    try:
        ts = now().strftime("%Y%m%d_%H%M%S")
        fname = f"{BACKUP_DIR}/{data_type}_{uid}_{ts}.json"
        if data_type == "profile": atomic_write(fname, load_profile(uid))
        elif data_type == "memory": atomic_write(fname, load_memory_raw(uid))
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith(f"{data_type}_{uid}_")])
        for old in backups[:-5]: os.remove(os.path.join(BACKUP_DIR, old))
        return True
    except Exception:
        return False

async def restore_backup(uid, data_type):
    async with get_user_lock(uid):
        try:
            backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith(f"{data_type}_{uid}_")])
            if not backups: return False
            with open(os.path.join(BACKUP_DIR, backups[-1]), 'r', encoding='utf-8') as f:
                data = json.load(f)
            if data_type == "profile": save_profile(uid, data, backup=False)
            elif data_type == "memory": await save_memory(uid, data, backup=False, lock_held=True)
            return True
        except Exception:
            return False

# ---------- СЖАТИЕ ----------
STOP_WORDS_GLOBAL = {'это','так','вот','ну','просто','очень','что','как','где','когда','для','без','по'}
def extract_key_points(text, max_len=40):
    if len(text) <= max_len: return text
    imp = [w for w in text.split() if w.lower() not in STOP_WORDS_GLOBAL and len(w) > 2]
    return ' '.join(imp[:8])[:max_len] + "..."

def compress_history(history):
    if len(history) <= LEVEL_1['max_history']: return history
    recent = history[-LEVEL_1['keep_recent']:]
    old = history[:-LEVEL_1['keep_recent']]
    summary = []
    for m in old[-8:]:
        r, c = m.get("role",""), m.get("content","")
        if r == "user": summary.append(f"Q: {extract_key_points(c,50)}")
        elif r == "assistant": summary.append(f"A: {extract_key_points(c,50)}")
    if summary:
        return [{"role":"system","content":"📚 История:\n" + "\n".join(summary[-5:])}] + recent
    return recent

def load_memory(uid): return compress_history(load_memory_raw(uid))

def _update_level(uid, messages, key, cfg, extractor, ext_len, ts_fmt):
    try:
        profile = load_profile(uid); profile.setdefault(key, [])
        ts = now().strftime(ts_fmt)
        for m in messages[-cfg['compress_interval']:]:
            r, c = m.get("role",""), m.get("content","")
            if r == "user": profile[key].append(f"[{ts}] Q: {extractor(c, ext_len)}")
            elif r == "assistant": profile[key].append(f"[{ts}] A: {extractor(c, ext_len)}")
        if len(profile[key]) > cfg['compress_to']:
            profile[key] = profile[key][-cfg['compress_to']:]
        save_profile(uid, profile, backup=False)
    except Exception as ex:
        logger.error(f"Ошибка сжатия: {ex}")

async def _save_memory_impl(uid, history, backup):
    try:
        if len(history) > LEVEL_1['max_history']:
            old = history[:-LEVEL_1['keep_recent']]
            if old:
                _update_level(uid, old, "level_2", LEVEL_2, extract_key_points, 40, "%d.%m")
        if not atomic_write(memory_path(uid), compress_history(history)):
            return False
        if backup: create_backup(uid, "memory")
        cnt = load_counter(uid) + 1
        save_counter(uid, cnt)
        return True
    except Exception as ex:
        logger.error(f"Ошибка сохранения памяти: {ex}")
        return False

async def save_memory(uid, history, backup=True, lock_held=False):
    if lock_held: return await _save_memory_impl(uid, history, backup)
    async with get_user_lock(uid): return await _save_memory_impl(uid, history, backup)

# ---------- УНИВЕРСАЛЬНЫЕ ИНСТРУМЕНТЫ (БЕЗ ХАРДКОДА) ----------
def extract_year_from_text(text):
    match = re.search(r'\b(20[2-9][0-9])\b', text)
    if match and match.group(1).isdigit():
        return int(match.group(1))
    return None

def extract_price_from_text(text):
    match = re.search(r'([\d\s]+)\s*(?:руб|₽|р\.|рублей|RUB|\$|€|USD|EUR)', text, re.I)
    if match:
        price_str = re.sub(r'\s', '', match.group(1))
        if price_str.isdigit():
            return int(price_str)
    return None

def extract_relevant_entities(text, query):
    """
    УНИВЕРСАЛЬНОЕ извлечение сущностей.
    БЕЗ списков игр/фильмов/техники.
    Работает на ПАТТЕРНАХ.
    """
    patterns = [
        r'\b([А-ЯA-Z][а-яa-zА-ЯA-Z0-9]+(?:[\s-][А-ЯA-Zа-яa-z0-9]+)*)\b',
        r'\b([А-ЯA-Zа-яa-z0-9]+[\s-]+[А-ЯA-Zа-яa-z0-9]+[\s-]+[А-ЯA-Zа-яa-z0-9]+)\b',
        r'\b([А-ЯA-Zа-яa-z0-9]+[\s-]*\d+[\s-]*[А-ЯA-Zа-яa-z0-9]*)\b',
        r'"([^"]+)"',
        r'\b([А-ЯA-Z][а-яa-zА-ЯA-Z0-9]{2,})\b',
    ]
    
    entities = []
    for pattern in patterns:
        matches = re.findall(pattern, text)
        entities.extend([m.strip() for m in matches if len(m.strip()) > 2])
    
    stop_words = {
        'это', 'так', 'вот', 'ну', 'просто', 'очень', 'что', 'как', 'где',
        'когда', 'для', 'без', 'по', 'про', 'уже', 'ещё', 'только', 'самый',
        'лучший', 'топ', 'рейтинг', 'обзор', 'сравнение', 'цена', 'отзыв',
        'характеристика', 'купить', 'выбор', 'какой', 'какая', 'какое', 'какие',
        'новый', 'старый', 'лучшая', 'лучшее', 'лучшие', 'следующий', 'предыдущий'
    }
    
    filtered = []
    for e in entities:
        e_lower = e.lower()
        if len(e) < 3:
            continue
        if e_lower in stop_words:
            continue
        if any(word in e_lower for word in stop_words):
            continue
        if e.isdigit():
            continue
        filtered.append(e)
    
    seen = set()
    unique = []
    for e in filtered:
        if e not in seen:
            seen.add(e)
            unique.append(e)
    
    freq = {}
    for e in unique:
        freq[e] = text.count(e)
    
    sorted_entities = sorted(unique, key=lambda x: freq.get(x, 0), reverse=True)
    return sorted_entities[:10]

def extract_pros_cons(text):
    pros = re.findall(r'(лучш(ий|ая|ее)|преимущество|плюс|отлично|хорош|удобн|мощн|быстр|тих|легк|долг)', text, re.I)
    cons = re.findall(r'(хуже|недостаток|минус|дорогой|проблема|тяжел|греет|шумн|мало|не хватает|устарел)', text, re.I)
    return len(pros), len(cons)

def extract_budget_from_query(query):
    match = re.search(r'(?:до|не более|не дороже|max|максимум)\s*([\d\s]+)\s*(?:тыс|руб|₽|р\.|\$|€)', query, re.I)
    if match:
        price_str = re.sub(r'\s', '', match.group(1))
        if price_str.isdigit():
            return int(price_str)
    return None

def is_official_link(link):
    return any(dom in link.lower() for dom in [
        'wikipedia.org','4pda','habr','ixbt','youtube','review','top','blog',
        'notebookcheck','techradar','gsmarena','dxomark','laptopmag','cnet',
        'metacritic','opencritic','ign','gamespot'
    ])

def assess_relevance(results, query):
    if not results:
        return []
    
    query_year = None
    year_match = re.search(r'\b(20[2-9][0-9])\b', query)
    if year_match:
        query_year = int(year_match.group(1))
    
    stop_words = {'найди','пожалуйста','помоги','мне','лучшие','скажи','расскажи','покажи','найти','бро'}
    keywords = [w.lower() for w in re.sub(r'[^\w\s]', '', query).split() 
                if w.lower() not in stop_words and len(w) > 3]
    
    scored = []
    for res in results:
        text = (res.get('title', '') + ' ' + res.get('snippet', '')).lower()
        link = res.get('link', '').lower()
        
        keyword_score = sum(2 for kw in keywords if kw in text)
        
        year = extract_year_from_text(text)
        year_score = 0
        if year:
            if query_year and year == query_year:
                year_score = 10
            elif year >= 2024:
                year_score = 8
            elif year >= 2023:
                year_score = 5
            elif year >= 2022:
                year_score = 2
            else:
                year_score = -5
        
        domain_score = 0
        trusted_domains = ['4pda', 'habr', 'ixbt', 'youtube', 'review', 'top', 'blog', 'notebookcheck', 'techradar', 'gsmarena', 'dxomark', 'laptopmag', 'cnet', 'metacritic', 'opencritic', 'ign', 'gamespot']
        for domain in trusted_domains:
            if domain in link:
                domain_score += 3
                break
        
        if any(x in link for x in ['ozon', 'wildberries', 'aliexpress', 'sbermegamarket']):
            domain_score -= 5
        
        pros, cons = extract_pros_cons(text)
        balance_score = pros - cons
        
        total_score = keyword_score + year_score + domain_score + balance_score
        scored.append({**res, 'score': total_score, 'year': year, 'price': extract_price_from_text(text)})
    
    scored.sort(key=lambda x: x['score'], reverse=True)
    return [r for r in scored if r['score'] > 0][:TOP_RESULTS_SHOW]

def normalize_query(query):
    normalized = re.sub(r'[^\w\s]', '', query.lower())
    normalized = ' '.join([w for w in normalized.split() if w not in STOP_WORDS_GLOBAL and len(w)>2])
    return normalized[:100]

def get_cached(query):
    if any(word in query.lower() for word in ['погода', 'курс']):
        return None
    norm_key = normalize_query(query)
    if norm_key in search_cache and (datetime.now() - search_cache[norm_key]['time']).total_seconds() < CACHE_TTL:
        logger.info("✅ Cache HIT")
        return search_cache[norm_key]['data']
    return None

def set_cache(query, data):
    if any(word in query.lower() for word in ['погода', 'курс']):
        return
    norm_key = normalize_query(query)
    search_cache[norm_key] = {'data': data, 'time': datetime.now()}
    if len(search_cache) > 100:
        oldest = min(search_cache.keys(), key=lambda k: search_cache[k]['time'])
        del search_cache[oldest]

def remove_unverified_claims(ans, raw_snippets):
    if not raw_snippets:
        return ans
    entities = extract_relevant_entities(ans, "")
    modified = False
    for entity in entities:
        if len(entity) > 3 and entity.lower() not in raw_snippets.lower():
            ans = re.sub(rf'\b{re.escape(entity)}\b', '', ans, flags=re.I)
            modified = True
            logger.info(f"🔍 Удалена неподтверждённая сущность: {entity}")
    if modified:
        ans = re.sub(r'\s+', ' ', ans)
        ans = re.sub(r'\s*\.\s*', '. ', ans)
        ans = re.sub(r'\s*,', ',', ans)
        ans = re.sub(r'\.\s*\.', '.', ans)
    return ans

def generate_manual_answer(results, user_message, max_items=5):
    """УНИВЕРСАЛЬНАЯ ручная сборка ответа — БЕЗ ХАРДКОДА"""
    if not results:
        return "❌ В интернете ничего не найдено по вашему запросу."
    
    relevant = [r for r in results if r.get('score', 0) > 0][:max_items]
    if not relevant:
        return "⚠️ По вашему запросу найдены общие статьи, но конкретных ответов в них нет. Попробуйте уточнить запрос."
    
    all_entities = []
    for r in relevant:
        text = r.get('title', '') + ' ' + r.get('snippet', '')
        entities = extract_relevant_entities(text, user_message)
        price = r.get('price')
        year = r.get('year')
        for ent in entities:
            all_entities.append({
                'name': ent,
                'price': price,
                'year': year,
                'snippet': r.get('snippet', '')[:200],
                'link': r.get('link', '#')
            })
    
    seen = set()
    unique_entities = []
    for e in all_entities:
        if e['name'] not in seen:
            seen.add(e['name'])
            unique_entities.append(e)
    
    answer = "📌 **Краткий вывод**\n\n"
    answer += f"Я проанализировал {len(relevant)} источников. Вот ключевая информация:\n\n"
    
    if unique_entities:
        answer += "### 🏆 Найденные объекты\n\n"
        for i, ent in enumerate(unique_entities[:5], 1):
            price_str = f" — {ent['price']} руб." if ent['price'] else ""
            year_str = f" ({ent['year']})" if ent['year'] else ""
            answer += f"{i}. **{ent['name']}**{year_str}{price_str}\n"
            if ent.get('snippet'):
                answer += f"   {ent['snippet'][:150]}\n"
            answer += "\n"
    
    answer += "### ✅ Рекомендация\n\n"
    if unique_entities:
        answer += f"На основе анализа чаще всего упоминается **{unique_entities[0]['name']}**.\n"
    answer += "Для полной информации рекомендую изучить источники.\n\n"
    
    answer += f"📅 Дата: {get_current_date()}\n"
    answer += "Уверенность: 70% (на основе найденных данных)"
    
    return answer

# ===== УНИВЕРСАЛЬНЫЙ ПРОМПТ (БЕЗ ЗАУЖЕНИЙ) =====
CORE_SYSTEM_RULE = (
    "Ты — экспертный аналитик. Твоя задача — дать ПОЛНЫЙ, СТРУКТУРИРОВАННЫЙ ответ на основе найденных данных.\n\n"
    
    "=== ГЛАВНЫЙ ПРИНЦИП ===\n"
    "Если в найденных данных есть ответы — используй их. Если ответы неполные или косвенные — сделай логический вывод. "
    "Если ответов нет — дай ПРЕДПОЛОЖЕНИЕ на основе знаний, но ЧЁТКО пометь это.\n\n"
    
    "=== ТВОЙ АЛГОРИТМ ===\n"
    "1. Проанализируй найденные данные: что есть?\n"
    "2. Если источники противоречат друг другу — выдели это отдельно.\n"
    "3. В любом случае структурируй ответ:\n"
    "   а) выдели ключевую информацию (названия, цифры, факты)\n"
    "   б) сделай вывод на основе того, что есть\n"
    "   в) если данных не хватает — дополни логикой и знаниями\n"
    "4. Чётко раздели:\n"
    "   ✅ Факты из интернета (со ссылками)\n"
    "   📊 Сравнение источников\n"
    "   ⚠️ Логические выводы\n"
    "   🧠 Предположения — не выше 25%\n\n"
    
    "=== ЖЁСТКИЙ ЗАПРЕТ ===\n"
    "ЗАПРЕЩЕНО давать ответ в виде просто списка ссылок.\n"
    "ЗАПРЕЩЕНО говорить 'нет данных' — всегда есть что-то, даже если это косвенная информация.\n"
    "Если данных мало — сделай вывод из того, что есть.\n"
    "Ссылки — только в конце.\n\n"
    
    "=== СТРУКТУРА ОТВЕТА ===\n"
    "📌 **Краткий вывод** (что удалось выяснить)\n\n"
    "### 🔍 Что найдено в источниках\n"
    "Ключевая информация из интернета\n\n"
    "### 🎯 Логический вывод\n"
    "Что можно сказать на основе найденного\n\n"
    "### 🧠 Предположение (если нужно)\n"
    "Только если данных не хватает, с пометкой 'Предположительно'\n\n"
    "### ⚠️ На что обратить внимание\n"
    "Предостережения\n\n"
    "### 🔗 Источники\n"
    "Ссылки\n\n"
    "📅 Дата\n"
    "Уверенность: XX%"
)

# ---------- ПОИСК ----------
async def search_apiserpent_async(query, num=SEARCH_RESULTS_NUM):
    if not APISERPENT_API_KEY: return []
    session = await get_http_session()
    try:
        logger.info(f"🔍 APISerpent: {query[:50]}...")
        async with session.get(
            "https://apiserpent.com/api/search",
            params={"q": query, "engine": SEARCH_ENGINE, "num": num},
            headers={"X-API-Key": APISERPENT_API_KEY},
            timeout=20
        ) as r:
            if r.status != 200:
                logger.warning(f"APISerpent статус: {r.status}")
                return []
            data = await r.json()
            results = []
            if isinstance(data.get("results"), dict):
                results = data["results"].get("organic", [])
            elif "organic_results" in data:
                results = data["organic_results"]
            out = []
            for x in results[:num]:
                if isinstance(x, dict):
                    out.append({
                        "title": str(x.get("title", ""))[:120],
                        "snippet": str(x.get("snippet", ""))[:400],
                        "link": str(x.get("url", x.get("link", "#")))[:120],
                        "source": str(x.get("source", x.get("domain", "неизвестно")))[:50]
                    })
            return out
    except Exception as e:
        logger.warning(f"APISerpent ошибка: {e}")
        return []

async def search_duckduckgo_async(query):
    session = await get_http_session()
    try:
        logger.info(f"🦆 DuckDuckGo: {query[:50]}...")
        async with session.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=8
        ) as r:
            if r.status != 200: return []
            data = await r.json()
            results = []
            if data.get('AbstractText'):
                results.append({
                    "title": "DuckDuckGo (факт)",
                    "snippet": data['AbstractText'][:500],
                    "link": data.get('AbstractURL', '#'),
                    "source": "duckduckgo"
                })
            for topic in data.get('RelatedTopics', []):
                if 'Text' in topic:
                    results.append({
                        "title": "DuckDuckGo",
                        "snippet": topic['Text'][:300],
                        "link": topic.get('FirstURL', '#'),
                        "source": "duckduckgo"
                    })
            return results
    except Exception:
        return []

async def search_primary(query):
    results = await search_apiserpent_async(query)
    if results:
        return results
    logger.info("🔄 APISerpent пуст, пробуем DuckDuckGo")
    return await search_duckduckgo_async(query)

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
def calculate_confidence(scored, user_message):
    if not scored:
        return 20
    
    base = 70
    has_prices = False
    has_entities = False
    has_dates = False
    has_scores = False
    source_count = min(len(scored), 5)
    
    for r in scored[:5]:
        text = r.get('title', '') + ' ' + r.get('snippet', '')
        if r.get('price') or re.search(r'\d{3,5}\s*(руб|₽)', text):
            has_prices = True
        if extract_relevant_entities(text, user_message):
            has_entities = True
        if re.search(r'20[2-9][0-9]', text):
            has_dates = True
        if re.search(r'\d+\.\d+|\d+/\d+|\d+%\s*$', text):
            has_scores = True
    
    confidence = base
    if has_prices: confidence += 10
    if has_entities: confidence += 10
    if has_dates: confidence += 5
    if has_scores: confidence += 5
    if source_count >= 3: confidence += 5
    
    return min(confidence, 95)

def has_direct_answer(scored, user_message):
    if not scored:
        return False
    
    keywords = [w.lower() for w in re.sub(r'[^\w\s]', '', user_message).split() 
                if len(w) > 3 and w.lower() not in STOP_WORDS_GLOBAL]
    
    for r in scored[:5]:
        text = (r.get('title', '') + ' ' + r.get('snippet', '')).lower()
        matches = sum(1 for kw in keywords if kw in text)
        if matches >= len(keywords) * 0.5:
            return True
    
    for r in scored[:5]:
        text = r.get('snippet', '') + ' ' + r.get('title', '')
        if re.search(r'\d{3,5}\s*(руб|₽)', text):
            return True
        if extract_relevant_entities(text, user_message):
            return True
    
    return False

def infer_entities_from_data(scored, budget_limit, query_year):
    if not scored:
        return []
    
    all_entities = []
    for r in scored:
        text = r.get('title', '') + ' ' + r.get('snippet', '')
        entities = extract_relevant_entities(text, "")
        all_entities.extend(entities)
    
    if not all_entities:
        return None
    
    candidates = []
    for r in scored:
        entities = extract_relevant_entities(r.get('title', '') + ' ' + r.get('snippet', ''), "")
        price = r.get('price')
        for entity in entities[:2]:
            if price and price <= budget_limit:
                candidates.append({
                    'name': entity,
                    'price': price,
                    'snippet': r.get('snippet', '')[:200],
                    'year': r.get('year', 0),
                    'link': r.get('link', ''),
                    'confidence': 60
                })
    
    if candidates:
        return candidates
    return None

# ---------- ГЕНЕРАЦИЯ ОТВЕТА ----------
async def generate_response(uid, user_message, history, profile, is_deep=False):
    try:
        return await asyncio.wait_for(
            _generate_response_internal(uid, user_message, history, profile, is_deep),
            timeout=120
        )
    except asyncio.TimeoutError:
        return "⏰ Превышено время ожидания. Попробуйте позже.", False

async def _generate_response_internal(uid, user_message, history, profile, is_deep):
    ctx = build_profile_context(profile)
    
    budget = extract_budget_from_query(user_message)
    budget_note = f" (бюджет до {budget} руб.)" if budget else ""
    
    cached = get_cached(user_message)
    if cached:
        all_results = cached
    else:
        variants = await generate_search_query(user_message)
        logger.info(f"🔍 Поисковый запрос: {variants[0]}")
        all_results = await search_primary(variants[0])
        logger.info(f"📊 Найдено результатов: {len(all_results)}")
        if all_results:
            set_cache(user_message, all_results)
    
    if not all_results:
        return await generate_local_answer(uid, user_message, history, profile, 
            reason="Поиск не дал прямых результатов")
    
    scored = assess_relevance(all_results, user_message)
    if not scored:
        return await generate_local_answer(uid, user_message, history, profile,
            reason="Найденные данные нерелевантны запросу")
    
    if not has_direct_answer(scored, user_message):
        if budget:
            inferred = infer_entities_from_data(scored, budget, now().year)
            if inferred:
                return await generate_inferred_answer(uid, user_message, history, profile, 
                    scored, inferred, budget, is_deep)
        return await generate_inferred_answer(uid, user_message, history, profile,
            scored, None, None, is_deep)
    
    if re.search(r'\b(этот год|202[4-9])\b', user_message.lower()):
        has_fresh = any(r.get('year', 0) >= 2024 for r in scored[:5])
        if not has_fresh:
            return await generate_local_answer(uid, user_message, history, profile,
                reason="Нет актуальных данных за этот год")
    
    stext = ""
    for i, r in enumerate(scored[:TOP_RESULTS_SHOW], 1):
        year_note = f" ({r.get('year')} г.)" if r.get('year') else ""
        price_note = f" [{r.get('price')} руб.]" if r.get('price') else ""
        source = r.get('source', 'неизвестно')
        link_html = f"🔗 <a href=\"{r.get('link')}\">Источник</a>" if r.get('link') and r['link'] != '#' else ""
        stext += f"{i}. **{r.get('title', 'Без названия')}**{year_note}{price_note}\n"
        stext += f"   {r.get('snippet', 'Нет описания')[:350]}\n"
        stext += f"   Источник: {source} | {link_html}\n\n"
    
    max_tokens = MAX_TOKENS_DEEP if is_deep else MAX_TOKENS_ANSWER
    
    sp = {
        "role": "system",
        "content": (
            f"{CORE_SYSTEM_RULE}\n"
            f"Сегодня: {get_current_date()}.\n"
            f"Контекст: {ctx}\n\n"
            f"НАЙДЕННЫЕ ДАННЫЕ (используй их как основу){budget_note}:\n"
            f"{stext}\n\n"
            "ИНСТРУКЦИЯ:\n"
            "1. Используй найденные данные как основу.\n"
            "2. Если источники противоречат друг другу — выдели это.\n"
            "3. Сделай логический вывод на основе того, что есть.\n"
            "4. Дай ПОЛНЫЙ ответ, а не ссылки.\n"
            "5. Если данных не хватает — дополни логикой и знаниями.\n"
            "6. НЕ оставляй пользователя без ответа."
        )
    }
    
    ans, err = await ask_deepseek([sp] + history, max_tokens=max_tokens)
    if err:
        logger.warning(f"DeepSeek ошибка: {err}, пробуем PRO...")
        ans, err2 = await ask_deepseek([sp] + history, max_tokens=max_tokens, model=MODEL_FALLBACK)
        if err2:
            logger.warning("⚠️ PRO тоже не ответил, генерируем вручную")
            manual_answer = generate_manual_answer(scored, user_message)
            return manual_answer, True
    
    final_ans = remove_unverified_claims(ans, stext)
    
    if len(final_ans) < 50:
        manual_answer = generate_manual_answer(scored, user_message)
        return manual_answer, True
    
    confidence = calculate_confidence(scored, user_message)
    if '📅 Дата:' not in final_ans:
        final_ans += f"\n\n📅 Дата: {get_current_date()}"
    if 'Уверенность' not in final_ans:
        final_ans += f"\nУверенность: {confidence}% (на основе найденных данных)"
    
    return f"🌐 из интернета + логика\n\n{final_ans}", True

async def generate_inferred_answer(uid, user_message, history, profile, scored, inferred, budget, is_deep):
    ctx = build_profile_context(profile)
    
    stext = ""
    for i, r in enumerate(scored[:TOP_RESULTS_SHOW], 1):
        year_note = f" ({r.get('year')} г.)" if r.get('year') else ""
        source = r.get('source', 'неизвестно')
        link_html = f"🔗 <a href=\"{r.get('link')}\">Источник</a>" if r.get('link') and r['link'] != '#' else ""
        stext += f"{i}. **{r.get('title', 'Без названия')}**{year_note}\n"
        stext += f"   {r.get('snippet', 'Нет описания')[:300]}\n"
        stext += f"   Источник: {source} | {link_html}\n\n"
    
    inferred_text = ""
    if inferred:
        inferred_text = f"**Логический вывод на основе найденных данных (до {budget} руб.):**\n\n"
        for item in inferred[:5]:
            price = item.get('price', 'не указана')
            name = item.get('name', 'Не указано')
            inferred_text += f"• **{name}** — примерно {price} руб.\n"
        inferred_text += "\n⚠️ Это ЛОГИЧЕСКИЙ ВЫВОД на основе найденных данных.\n"
    else:
        inferred_text = "**На основе найденных данных можно сделать следующий вывод:**\n\n"
        for r in scored[:5]:
            title = r.get('title', 'Без названия')
            inferred_text += f"• {title}\n"
        inferred_text += "\n⚠️ Это обобщение на основе найденных источников.\n"
    
    max_tokens = MAX_TOKENS_DEEP if is_deep else MAX_TOKENS_ANSWER
    
    sp = {
        "role": "system",
        "content": (
            f"{CORE_SYSTEM_RULE}\n"
            f"Сегодня: {get_current_date()}.\n"
            f"Контекст: {ctx}\n\n"
            f"НАЙДЕННЫЕ ДАННЫЕ (используй их как основу):\n"
            f"{stext}\n\n"
            f"{inferred_text}\n\n"
            "ИНСТРУКЦИЯ:\n"
            "1. Используй найденные данные как основу.\n"
            "2. Используй логический вывод, чтобы дополнить ответ.\n"
            "3. Дай ПОЛНЫЙ ответ.\n"
            "4. НЕ оставляй пользователя без ответа."
        )
    }
    
    ans, err = await ask_deepseek([sp] + history, max_tokens=max_tokens)
    if err:
        logger.warning(f"DeepSeek ошибка в inferred: {err}, пробуем PRO...")
        ans, err2 = await ask_deepseek([sp] + history, max_tokens=max_tokens, model=MODEL_FALLBACK)
        if err2:
            return generate_manual_answer(scored, user_message), True
    
    final_ans = remove_unverified_claims(ans, stext)
    
    if '📅 Дата:' not in final_ans:
        final_ans += f"\n\n📅 Дата: {get_current_date()}"
    if 'Уверенность' not in final_ans:
        final_ans += "\nУверенность: 70% (частично на основе интернет-данных + логический вывод)"
    
    return f"🌐 из интернета + логика\n\n{final_ans}", True

async def generate_local_answer(uid, user_message, history, profile, reason):
    ctx = build_profile_context(profile)
    
    sp = {
        "role": "system",
        "content": (
            f"{CORE_SYSTEM_RULE}\n"
            f"Сегодня: {get_current_date()}.\n"
            f"Контекст: {ctx}\n\n"
            f"⚠️ ВНИМАНИЕ: {reason}.\n"
            "Используй свои знания, НО:\n"
            "1. Начинай с '🧠 На основе моих знаний'\n"
            "2. Каждое утверждение — 'Предположительно'\n"
            "3. Уверенность не выше 25%\n"
            "4. НЕ придумывай факты"
        )
    }
    
    messages = [sp] + history
    ans, err = await ask_deepseek(messages, max_tokens=MAX_TOKENS_ANSWER)
    if err:
        return f"⚠️ Ошибка. {reason}", False
    
    if 'Уверенность:' not in ans:
        ans += f"\n\n📅 Дата: {get_current_date()}"
        ans += "\nУверенность: 20% (на основе знаний модели)"
    
    return f"🧠 из базы\n\n{ans}", True

async def generate_search_query(query):
    stop = {'найди','пожалуйста','помоги','мне','лучшие','скажи','расскажи','покажи','найти','бро','что','как','без','для','по','про'}
    words = [w for w in re.sub(r'[^\w\s]', '', query).split() 
             if w.lower() not in stop and len(w) > 2]
    if not words:
        return [query]
    base = " ".join(words[:5])
    if not re.search(r'\b20[2-9][0-9]\b', base):
        base += f" {now().year}"
    return [base]

def build_profile_context(profile):
    parts = []
    for k, v in profile.items():
        if k in ("updated","level_2"): continue
        if isinstance(v, str):
            parts.append(f"{k}: {v[:40]}")
    return ". ".join(parts)[:150]

async def ask_deepseek(messages, retries=2, max_tokens=None, model=MODEL_DEFAULT):
    session = await get_http_session()
    for attempt in range(retries):
        try:
            payload = {"model": model, "messages": messages, "temperature": MODEL_TEMPERATURE}
            if max_tokens: payload["max_tokens"] = max_tokens
            async with session.post(
                f"{DEEPSEEK_API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                json=payload,
                timeout=60
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("choices"):
                        c = data["choices"][0].get("message", {}).get("content")
                        if c and len(c.strip()) > 10:
                            return (c, None)
                        else:
                            logger.warning(f"⚠️ DeepSeek вернул пустой ответ: {c}")
                            return (None, "empty_response")
                if resp.status == 429:
                    await asyncio.sleep(2)
                    continue
                if resp.status in (400, 404) and model != MODEL_FALLBACK:
                    logger.warning(f"Модель {model} недоступна, пробуем {MODEL_FALLBACK}")
                    model = MODEL_FALLBACK
                    continue
                return None, f"http_{resp.status}"
        except asyncio.TimeoutError:
            logger.warning(f"⏰ Таймаут DeepSeek (попытка {attempt+1})")
            await asyncio.sleep(2)
            continue
        except Exception as ex:
            logger.warning(f"Ошибка DeepSeek: {ex}")
            await asyncio.sleep(1)
    return None, "max_retries"

# ---------- КОМАНДЫ ----------
async def start(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid): return
    await safe_reply(update, "👋 Привет! Я — экспертный ассистент с доступом в интернет.\n🛡 Принципы: даю структурированные ответы, сравниваю источники, указываю уверенность.\n📋 Команды: /profile, /memory, /stats, /forget, /restore, /deep [запрос]")

async def profile_command(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid): return
    p = load_profile(uid)
    if not p:
        await safe_reply(update, "📭 Я пока ничего не знаю о тебе.")
        return
    lines = ["🧠 **Память:**"]
    lines.append(f"• 📝 активная история: {len(load_memory_raw(uid))} сообщений")
    lines.append("\n👤 **Личное:**")
    exclude = {'updated','level_2'}
    personal = {k:v for k,v in p.items() if k not in exclude}
    if personal:
        for k,v in personal.items():
            lines.append(f"• {k}: {v}")
    else:
        lines.append("• Пока ничего не запомнил")
    lines.append(f"\n🔄 Обновлено: {p.get('updated','неизвестно')}")
    await safe_reply(update, "\n".join(lines))

async def memory_command(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid): return
    if not context.args:
        await safe_reply(update, "🔍 Поиск: `/memory что искать`")
        return
    query = ' '.join(context.args)
    res = search_in_pyramid(uid, query)
    if not res:
        await safe_reply(update, f"📭 Ничего не найдено: '{query}'")
        return
    lines = [f"🔍 Результаты '{query}':"] + [f"{i}. {r}" for i,r in enumerate(res[:10],1)]
    if len(res) > 10: lines.append(f"... и ещё {len(res)-10}")
    await safe_reply(update, "\n".join(lines))

def search_in_pyramid(uid, query):
    profile, q, res = load_profile(uid), query.lower(), []
    for m in load_memory_raw(uid)[-30:]:
        c = m.get("content","")
        if q in c.lower():
            role = "👤" if m.get("role")=="user" else "🤖"
            ts = m.get("timestamp","")
            res.append(f"{role}{(' ['+ts+']') if ts else ''} {extract_key_points(c,80)}")
    for item in profile.get("level_2", []):
        if q in item.lower(): res.append(f"📚 {item}")
    return res[:15]

async def stats_command(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid): return
    p = load_profile(uid)
    raw = load_memory_raw(uid)
    lines = ["📊 **Статистика:**"]
    lines.append(f"• Обработано сообщений: {load_counter(uid)}")
    lines.append(f"• В активной истории: {len(raw)}")
    lines.append(f"• Сжатых пунктов: {len(p.get('level_2', []))}")
    bc = len([f for f in os.listdir(BACKUP_DIR) if f.startswith(f"profile_{uid}_")])
    lines.append(f"💾 Бэкапов профиля: {bc}")
    await safe_reply(update, "\n".join(lines))

async def forget_command(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid): return
    async with get_user_lock(uid):
        save_profile(uid, {})
        await save_memory(uid, [], backup=True, lock_held=True)
        save_counter(uid, 0)
    await safe_reply(update, "🧹 Я забыл всё, что знал о тебе!")

async def restore_command(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid): return
    pr = await restore_backup(uid, "profile")
    mr = await restore_backup(uid, "memory")
    if pr or mr:
        await safe_reply(update, "✅ Восстановлено!\n" + ("📋 Профиль\n" if pr else "") + ("💬 История" if mr else ""))
    else:
        await safe_reply(update, "❌ Нет бэкапов.")

# ---------- RATE LIMIT ----------
RATE_LIMIT, RATE_WINDOW = 5, 10
async def check_rate_limit(uid):
    async with rate_lock:
        now_ts = datetime.now().timestamp()
        request_count[uid] = [t for t in request_count.get(uid, []) if now_ts - t < RATE_WINDOW]
        if len(request_count[uid]) >= RATE_LIMIT: return False
        request_count[uid].append(now_ts)
        return True

# ---------- ОТПРАВКА ----------
async def safe_reply(update: Update, text: str, reply_markup=None):
    msg = update.effective_message
    if msg is None: return
    def markdown_to_html(t):
        t = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', t)
        t = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', t)
        t = re.sub(r'^### (.*?)$', r'<b>\1</b>', t, flags=re.M)
        return t.strip()
    if len(text) > 20 and not text.startswith(('/', '❌', '✅')):
        text = markdown_to_html(text)
    try:
        if len(text) > 4096:
            for i in range(0, len(text), 4096):
                await msg.reply_text(text[i:i+4096], parse_mode='HTML', reply_markup=reply_markup, disable_web_page_preview=True)
        else:
            await msg.reply_text(text, parse_mode='HTML', reply_markup=reply_markup, disable_web_page_preview=True)
    except Exception:
        try: await msg.reply_text(text, reply_markup=reply_markup)
        except: pass

def is_allowed(uid):
    return not ALLOWED_USERS_LIST or uid in ALLOWED_USERS_LIST

# ---------- ОБРАБОТЧИК СООБЩЕНИЙ ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.effective_message or not update.effective_message.text: return
    uid = update.effective_user.id
    if not is_allowed(uid): return
    if not await check_rate_limit(uid):
        await safe_reply(update, "⏳ Не пишите так часто.")
        return
    
    user_message = update.effective_message.text[:1000]
    is_deep = False
    
    if user_message.lower().startswith("/deep "):
        is_deep = True
        user_message = user_message[6:].strip()
        if not user_message:
            await safe_reply(update, "📝 Напишите запрос после /deep")
            return

    if user_message.lower().startswith("запомни "):
        text = user_message[8:].strip()
        async with get_user_lock(uid):
            p = load_profile(uid)
            if ":" in text:
                k, v = text.split(":", 1)
                k, v = k.strip(), v.strip()
                p[k] = v
                if save_profile(uid, p):
                    await safe_reply(update, f"✅ Запомнил: {k} = {v}")
                else:
                    await safe_reply(update, "❌ Не удалось сохранить.")
            else:
                p.setdefault("факты", []).append(text)
                if save_profile(uid, p):
                    await safe_reply(update, f"✅ Запомнил факт: {text}")
                else:
                    await safe_reply(update, "❌ Не удалось сохранить факт.")
        return

    history = load_memory(uid)
    profile = load_profile(uid)
    user_msg_obj = {"role": "user", "content": user_message, "timestamp": now().strftime("%Y-%m-%d %H:%M:%S")}
    history.append(user_msg_obj)

    answer, should_save = await generate_response(uid, user_message, history, profile, is_deep)
    
    if should_save and isinstance(answer, str) and len(answer) > 10:
        clean_answer = re.sub(r'<[^>]+>', '', answer)
        history.append({"role": "assistant", "content": clean_answer, "timestamp": now().strftime("%Y-%m-%d %H:%M:%S")})
        await save_memory(uid, history)
    
    await safe_reply(update, answer)

# ---------- ЗАПУСК ----------
async def auto_restore_all_users():
    logger.info("🔄 Проверка данных при старте...")
    backup_files = os.listdir(BACKUP_DIR)
    user_ids = set()
    for fname in backup_files:
        parts = fname.split('_')
        if len(parts) >= 2 and parts[0] in ('profile','memory'):
            try:
                user_ids.add(int(parts[1]))
            except: pass
    for uid in user_ids:
        mem_path = memory_path(uid)
        prof_path = profile_path(uid)
        mem_data = atomic_read(mem_path, default=None)
        prof_data = atomic_read(prof_path, default=None)
        need_restore = False
        if mem_data is None or (isinstance(mem_data, list) and len(mem_data)==0):
            need_restore = True
        if prof_data is None or (isinstance(prof_data, dict) and len(prof_data)==0):
            need_restore = True
        if need_restore:
            pr = await restore_backup(uid, "profile")
            mr = await restore_backup(uid, "memory")
            if pr or mr:
                logger.info(f"✅ Пользователь {uid} восстановлен")

async def error_handler(update, context):
    logger.error(f"Ошибка: {context.error}")

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(auto_restore_all_users())
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(CommandHandler("restore", restore_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    logger.info("✅ БОТ ЗАПУЩЕН (универсальный, без хардкода)")
    app.run_polling()
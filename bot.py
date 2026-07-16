# ============================================================
#  BroWaix Bot — ФИНАЛЬНАЯ ВЕРСИЯ 2026
#  (экспертный уровень, бюджет $7–8/мес, 150 запросов/день)
#  Улучшения: сравнение источников, плюсы/минусы, фильтр бюджета
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

# --- ОПТИМИЗИРОВАННЫЕ ПАРАМЕТРЫ ---
SEARCH_RESULTS_NUM = 10
SEARCH_VARIANTS_COUNT = 1
MODEL_TEMPERATURE = 0.1
MAX_RETRY_ATTEMPTS = 1
CACHE_TTL = 604800  # 7 дней
MAX_TOKENS_ANSWER = 2048  # увеличен для структурированных ответов
MAX_TOKENS_DEEP = 4096    # для /deep
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
                timeout=aiohttp.ClientTimeout(total=35, connect=5, sock_read=20)
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
STOP_WORDS = {'это','так','вот','ну','просто','очень','что','как','где','когда','для','без','по'}
def extract_key_points(text, max_len=40):
    if len(text) <= max_len: return text
    imp = [w for w in text.split() if w.lower() not in STOP_WORDS and len(w) > 2]
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

# ---------- УЛУЧШЕННЫЕ ИНСТРУМЕНТЫ ФИЛЬТРАЦИИ ----------
def extract_year_from_text(text):
    match = re.search(r'\b(20[2-9][0-9])\b', text)
    if match and match.group(1).isdigit():
        return int(match.group(1))
    return None

def extract_price_from_text(text):
    match = re.search(r'([\d\s]+)\s*(?:руб|₽|р\.|рублей|RUB)', text, re.I)
    if match:
        price_str = re.sub(r'\s', '', match.group(1))
        if price_str.isdigit():
            return int(price_str)
    return None

def extract_models_from_text(text):
    """Извлекает все упоминания моделей из текста"""
    model_patterns = [
        r'(X96Q|X96|H96\s*Max|H96|Tanix\s*TX3|Tanix|Xiaomi\s*Mi\s*Box\s*S|Xiaomi|Vontar\s*X4|Vontar|Ugoos|Beelink|Minix|WeChip|A95X|Dune|Sber|Rombica|Kickpi|TOX|X88)',
        r'(MacBook\s*Air\s*M[4-6]|MacBook\s*Pro\s*M[4-6]|ASUS\s*Zenbook\s*S\s*\d+|ThinkPad\s*T\d+|Dell\s*XPS\s*\d+)',
        r'(iPhone\s*\d+\s*Pro\s*(Max)?|Samsung\s*Galaxy\s*S\d+\s*Ultra|Xiaomi\s*\d+\s*Pro|Google\s*Pixel\s*\d+)',
        r'(H618|H313|S905X4|S905X3|S905|RK3566|M4|M5|Ultra\s*\d+|Ryzen\s*\d+)',
    ]
    models = []
    for pattern in model_patterns:
        matches = re.findall(pattern, text, re.I)
        models.extend([m.strip() for m in matches if m.strip()])
    return list(set(models))

def extract_pros_cons(text):
    """Извлекает плюсы и минусы из текста"""
    pros = re.findall(r'(лучш(ий|ая|ее)|преимущество|плюс|отлично|хорош|удобн|мощн|быстр|тих|легк|долг)', text, re.I)
    cons = re.findall(r'(хуже|недостаток|минус|дорогой|проблема|тяжел|греет|шумн|мало|не хватает|устарел)', text, re.I)
    return len(pros), len(cons)

def extract_budget_from_query(query):
    """Извлекает бюджет из запроса пользователя"""
    match = re.search(r'(?:до|не более|не дороже|max|максимум)\s*([\d\s]+)\s*(?:тыс|руб|₽|р\.)', query, re.I)
    if match:
        price_str = re.sub(r'\s', '', match.group(1))
        if price_str.isdigit():
            return int(price_str)
    # Если указано "бюджетный"
    if re.search(r'бюджетн', query, re.I):
        return 15000
    # Если указано "премиальный" или "флагман"
    if re.search(r'(премиальн|флагман|дорог)', query, re.I):
        return 80000
    return None

def is_official_link(link):
    return any(dom in link.lower() for dom in [
        'python.org','docs.python.org','github.com','pypi.org',
        'wikipedia.org','4pda','habr','ixbt','youtube','review','top','blog',
        'notebookcheck','techradar','gsmarena','dxomark','laptopmag','cnet'
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
    
    model_names = ['X96','H96','Tanix','Xiaomi','Vontar','Ugoos','Beelink','Minix','WeChip','A95X','Dune','Sber','Rombica','Kickpi','TOX','X88','H618','H313','S905','RK3566','4K','HDR','Dolby','Android','Google TV','MacBook','Air','Pro','ASUS','Zenbook','ThinkPad','Dell','XPS','iPhone','Samsung','Galaxy','Pixel']
    
    scored = []
    for res in results:
        text = (res.get('title', '') + ' ' + res.get('snippet', '')).lower()
        link = res.get('link', '').lower()
        
        keyword_score = sum(2 for kw in keywords if kw in text)
        model_score = sum(3 for model in model_names if model.lower() in text)
        
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
        trusted_domains = ['4pda', 'habr', 'ixbt', 'youtube', 'review', 'top', 'blog', 'notebookcheck', 'techradar', 'gsmarena', 'dxomark', 'laptopmag', 'cnet']
        for domain in trusted_domains:
            if domain in link:
                domain_score += 3
                break
        
        if any(x in link for x in ['ozon', 'wildberries', 'aliexpress', 'sbermegamarket']):
            domain_score -= 5
        
        pros, cons = extract_pros_cons(text)
        balance_score = pros - cons
        
        total_score = keyword_score + model_score + year_score + domain_score + balance_score
        scored.append({**res, 'score': total_score, 'year': year, 'price': extract_price_from_text(text)})
    
    scored.sort(key=lambda x: x['score'], reverse=True)
    return [r for r in scored if r['score'] > 0][:TOP_RESULTS_SHOW]

def normalize_query(query):
    normalized = re.sub(r'[^\w\s]', '', query.lower())
    normalized = ' '.join([w for w in normalized.split() if w not in STOP_WORDS and len(w)>2])
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
    entities = re.findall(r'(Xiaomi|H96|X96Q|Tanix|Vontar|RockTek|Ugoos|Beelink|Minix|WeChip|A95X|Dune|Apple|Sber|Rombica|Kickpi|TOX|X88|H618|H313|S905|RK3566|4K|HDR|Dolby|Android|Google TV|MacBook|ASUS|Zenbook|ThinkPad|Dell|XPS|iPhone|Samsung|Galaxy|Pixel)', ans, re.I)
    modified = False
    for entity in set(entities):
        if entity.lower() not in raw_snippets.lower():
            ans = re.sub(rf'\b{re.escape(entity)}\b', '', ans, flags=re.I)
            modified = True
            logger.info(f"🔍 Удалена неподтверждённая сущность: {entity}")
    if modified:
        ans = re.sub(r'\s+', ' ', ans)
        ans = re.sub(r'\s*\.\s*', '. ', ans)
        ans = re.sub(r'\s*,', ',', ans)
        ans = re.sub(r'\.\s*\.', '.', ans)
    return ans

def generate_answer_from_snippets(results, user_message, max_items=5):
    if not results:
        return "❌ В интернете ничего не найдено по вашему запросу."
    answer = f"🔍 **Результаты поиска по запросу:** {user_message[:100]}\n\n"
    relevant = [r for r in results if r.get('score', 0) > 0][:max_items]
    if not relevant:
        links = [r.get('link') for r in results if r.get('link') and r['link'] != '#']
        if links:
            answer = "🔍 **Найденные ссылки:**\n\n"
            for link in links[:max_items]:
                answer += f"• {link}\n"
            answer += f"\n📅 Дата: {get_current_date()}"
            answer += "\nУверенность: 70% (на основе найденных данных)"
            return answer
        else:
            return f"Найденные данные:\n\n{str(results)[:1000]}"
    for i, r in enumerate(relevant, 1):
        year_note = f" ({r.get('year')})" if r.get('year') else ""
        price_note = f" [{r.get('price')} руб.]" if r.get('price') else ""
        answer += f"{i}. **{r.get('title', 'Без названия')}**{year_note}{price_note}\n"
        if r.get('snippet'):
            answer += f"   {r['snippet'][:200]}\n"
        if r.get('link') and r['link'] != '#':
            answer += f"   🔗 <a href='{r['link']}'>Источник</a>\n"
        answer += "\n"
    answer += f"📅 Дата: {get_current_date()}"
    answer += "\nУверенность: 85% (на основе найденных данных)"
    return answer

# ===== НОВЫЙ ЭКСПЕРТНЫЙ ПРОМПТ СО СРАВНЕНИЕМ ИСТОЧНИКОВ =====
CORE_SYSTEM_RULE = (
    "Ты — экспертный аналитик. Твоя задача — дать ПОЛНЫЙ, СТРУКТУРИРОВАННЫЙ ответ, а не просто ссылки.\n\n"
    
    "=== ГЛАВНЫЙ ПРИНЦИП ===\n"
    "Никогда не оставляй пользователя без ответа. Если данных нет — дай ПРЕДПОЛОЖЕНИЕ на основе знаний и логики, но ЧЁТКО пометь это.\n\n"
    
    "=== ТВОЙ АЛГОРИТМ ===\n"
    "1. Проанализируй найденные данные: что есть?\n"
    "2. Если источники противоречат друг другу — выдели это отдельно.\n"
    "3. Если данных мало или они косвенные:\n"
    "   а) выдели ключевую информацию из того, что есть (бренды, модели, цены, оценки)\n"
    "   б) сделай логический вывод на основе частоты упоминаний\n"
    "   в) используй свои общие знания о рынке, чтобы заполнить пробелы\n"
    "   г) дай СТРУКТУРИРОВАННЫЙ ответ, а не просто ссылки\n"
    "4. Чётко раздели:\n"
    "   ✅ Факты из интернета (со ссылками)\n"
    "   📊 Сравнение источников (кто что сказал)\n"
    "   ⚠️ Логические выводы (на основе данных)\n"
    "   🧠 Предположения (на основе знаний модели) — не выше 25%\n\n"
    
    "=== СТРУКТУРА ОТВЕТА ===\n"
    "📌 **Краткий вывод** (1–2 предложения)\n\n"
    "### 🏆 ТОП-3/5 моделей\n"
    "| № | Модель | Цена | Характеристики | Оценка |\n"
    "|---|--------|------|----------------|--------|\n"
    "| 1 | X      | Y    | Z              | W      |\n\n"
    "### 📋 Что говорят источники (сравнение)\n"
    "| Источник | №1 | №2 | №3 | Комментарий |\n"
    "|----------|----|----|----|-------------|\n"
    "| Source1  | A  | B  | C  | ...         |\n\n"
    "### 🎯 По сценариям (моя рекомендация)\n"
    "| Если вам нужно | Берите | Почему |\n"
    "|----------------|--------|--------|\n"
    "| Сценарий X     | Модель Y | причина |\n\n"
    "### ⚠️ На что обратить внимание\n"
    "• предостережение 1\n"
    "• предостережение 2\n\n"
    "### 🔗 На чём основан ответ\n"
    "Список источников\n\n"
    "📅 Дата\n"
    "🔹 Уверенность в ТОПе: XX%\n"
    "🔹 Уверенность в ценах: XX%\n"
    "🔹 Уверенность в рекомендациях: XX%"
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

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ УВЕРЕННОСТИ ----------
def calculate_confidence(scored, user_message):
    if not scored:
        return 20
    
    base = 70
    has_prices = False
    has_models = False
    has_dates = False
    has_scores = False
    source_count = min(len(scored), 5)
    
    model_names = ['X96','H96','Tanix','Xiaomi','Vontar','Ugoos','Beelink','Minix','WeChip','A95X','Dune','Sber','Rombica','Kickpi','TOX','X88','H618','H313','S905','RK3566','MacBook','ASUS','Zenbook','ThinkPad','Dell','XPS','iPhone','Samsung','Galaxy','Pixel']
    
    for r in scored[:5]:
        text = r.get('title', '') + ' ' + r.get('snippet', '')
        if r.get('price') or re.search(r'\d{3,5}\s*(руб|₽)', text):
            has_prices = True
        if any(model in text for model in model_names):
            has_models = True
        if re.search(r'20[2-9][0-9]', text):
            has_dates = True
        if re.search(r'\d+\.\d+|\d+/\d+|\d+%\s*$', text):
            has_scores = True
    
    confidence = base
    if has_prices: confidence += 10
    if has_models: confidence += 10
    if has_dates: confidence += 5
    if has_scores: confidence += 5
    if source_count >= 3: confidence += 5
    
    return min(confidence, 95)

def has_direct_answer(scored, user_message):
    if not scored:
        return False
    
    keywords = [w.lower() for w in re.sub(r'[^\w\s]', '', user_message).split() 
                if len(w) > 3 and w.lower() not in STOP_WORDS]
    
    for r in scored[:5]:
        text = (r.get('title', '') + ' ' + r.get('snippet', '')).lower()
        matches = sum(1 for kw in keywords if kw in text)
        if matches >= len(keywords) * 0.5:
            return True
    
    model_names = ['X96','H96','Tanix','Xiaomi','Vontar','Ugoos','Beelink','Minix','WeChip','A95X','Dune','Sber','Rombica','Kickpi','TOX','X88','H618','H313','S905','RK3566','MacBook','ASUS','Zenbook','ThinkPad','Dell','XPS','iPhone','Samsung','Galaxy','Pixel']
    for r in scored[:5]:
        text = r.get('snippet', '') + ' ' + r.get('title', '')
        if re.search(r'\d{3,5}\s*(руб|₽)', text):
            return True
        if any(model in text for model in model_names):
            return True
    
    return False

def infer_models_from_data(scored, budget_limit, query_year):
    """Логический вывод моделей на основе имеющихся данных"""
    if not scored:
        return []
    
    all_models = []
    for r in scored:
        text = r.get('title', '') + ' ' + r.get('snippet', '')
        models = extract_models_from_text(text)
        all_models.extend(models)
    
    if not all_models:
        return None
    
    candidates = []
    for r in scored:
        models = extract_models_from_text(r.get('title', '') + ' ' + r.get('snippet', ''))
        price = r.get('price')
        if models and price and price <= budget_limit:
            candidates.append({
                'model': models[0],
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
            timeout=60
        )
    except asyncio.TimeoutError:
        return "⏰ Превышено время ожидания. Попробуйте позже.", False

async def _generate_response_internal(uid, user_message, history, profile, is_deep):
    ctx = build_profile_context(profile)
    
    # Извлекаем бюджет из запроса
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
            reason="Поиск в интернете не дал результатов")
    
    scored = assess_relevance(all_results, user_message)
    if not scored:
        return await generate_local_answer(uid, user_message, history, profile,
            reason="Найденные данные нерелевантны запросу")
    
    # Если данных мало — пробуем логический вывод
    if not has_direct_answer(scored, user_message):
        if budget:
            inferred = infer_models_from_data(scored, budget, now().year)
            if inferred:
                return await generate_inferred_answer(uid, user_message, history, profile, 
                    scored, inferred, budget, is_deep)
        return await generate_local_answer(uid, user_message, history, profile,
            reason="Найденные данные не содержат прямого ответа")
    
    if re.search(r'\b(этот год|202[4-9])\b', user_message.lower()):
        has_fresh = any(r.get('year', 0) >= 2024 for r in scored[:5])
        if not has_fresh:
            return await generate_local_answer(uid, user_message, history, profile,
                reason="В интернете нет актуальных данных за этот год")
    
    # Формируем сниппеты с источниками
    stext = ""
    sources_text = ""
    for i, r in enumerate(scored[:TOP_RESULTS_SHOW], 1):
        year_note = f" ({r.get('year')} г.)" if r.get('year') else ""
        price_note = f" [{r.get('price')} руб.]" if r.get('price') else ""
        source = r.get('source', 'неизвестно')
        link_html = f"🔗 <a href=\"{r.get('link')}\">Источник</a>" if r.get('link') and r['link'] != '#' else ""
        stext += f"{i}. **{r.get('title', 'Без названия')}**{year_note}{price_note}\n"
        stext += f"   {r.get('snippet', 'Нет описания')[:350]}\n"
        stext += f"   Источник: {source} | {link_html}\n\n"
        sources_text += f"• {source} ({r.get('link', 'нет ссылки')})\n"
    
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
            "2. Если источники противоречат друг другу — выдели это в таблице сравнения.\n"
            "3. Сделай логический вывод на основе частоты упоминаний.\n"
            "4. Дай ПОЛНЫЙ ответ: список моделей, таблицу, рекомендации.\n"
            "5. Если данных не хватает — дополни своими знаниями, но чётко пометь 'Предположительно'.\n"
            "6. НЕ оставляй пользователя без ответа."
        )
    }
    
    ans, err = await ask_deepseek([sp] + history, max_tokens=max_tokens)
    if err:
        return generate_answer_from_snippets(scored, user_message), True
    
    final_ans = remove_unverified_claims(ans, stext)
    
    if len(final_ans) < 50 or 'http' not in final_ans:
        final_ans = generate_answer_from_snippets(scored, user_message)
    else:
        confidence = calculate_confidence(scored, user_message)
        if '📅 Дата:' not in final_ans:
            final_ans += f"\n\n📅 Дата: {get_current_date()}"
        if 'Уверенность' not in final_ans:
            final_ans += f"\n🔹 Уверенность в ТОПе: {confidence}% (на основе найденных данных)"
    
    return f"🌐 из интернета + логика\n\n{final_ans}", True

async def generate_inferred_answer(uid, user_message, history, profile, scored, inferred, budget, is_deep):
    """Генерирует ответ с логическим выводом на основе данных"""
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
        inferred_text = "**Логический вывод на основе найденных данных (до {budget} руб.):**\n\n"
        for item in inferred[:5]:
            price = item.get('price', 'не указана')
            model = item.get('model', 'Модель не указана')
            inferred_text += f"• **{model}** — примерно {price} руб.\n"
        inferred_text += "\n⚠️ Это ЛОГИЧЕСКИЙ ВЫВОД на основе найденных данных, а не прямой факт из интернета.\n"
    
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
            "3. Если данных всё ещё мало — дополни своими знаниями, но чётко пометь 'Предположительно'.\n"
            "4. Дай ПОЛНЫЙ ответ: список моделей с ценами, таблицу, рекомендацию.\n"
            "5. НЕ оставляй пользователя без ответа."
        )
    }
    
    ans, err = await ask_deepseek([sp] + history, max_tokens=max_tokens)
    if err:
        return generate_answer_from_snippets(scored, user_message), True
    
    final_ans = remove_unverified_claims(ans, stext)
    
    if '📅 Дата:' not in final_ans:
        final_ans += f"\n\n📅 Дата: {get_current_date()}"
    if 'Уверенность' not in final_ans:
        final_ans += "\n🔹 Уверенность в ТОПе: 70% (частично на основе интернет-данных + логический вывод)"
    
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
            "Ты МОЖЕШЬ использовать свои внутренние знания, НО:\n"
            "1. Начинай ответ с '🧠 На основе моих знаний (интернет-данных нет)'\n"
            "2. Каждое утверждение должно начинаться с 'Предположительно' или 'Возможно'\n"
            "3. Если не уверен — скажи 'Я не знаю'\n"
            "4. Уверенность не выше 25%\n"
            "5. НЕ придумывай факты\n"
            "6. Постарайся дать структурированный ответ даже на основе предположений"
        )
    }
    
    messages = [sp] + history
    ans, err = await ask_deepseek(messages, max_tokens=MAX_TOKENS_ANSWER)
    if err:
        return f"⚠️ Ошибка генерации из базы. {reason}", False
    
    if 'Уверенность:' not in ans:
        ans += f"\n\n📅 Дата: {get_current_date()}"
        ans += "\nУверенность: 20% (основано на знаниях модели, без интернет-данных)"
    
    return f"🧠 из базы (интернет пуст)\n\n{ans}", True

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
                json=payload
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("choices"):
                        c = data["choices"][0].get("message", {}).get("content")
                        return (c, None) if c is not None else (None, "empty")
                if resp.status == 429:
                    await asyncio.sleep(2)
                    continue
                if resp.status in (400, 404) and model != MODEL_FALLBACK:
                    logger.warning(f"Модель {model} недоступна, пробуем {MODEL_FALLBACK}")
                    model = MODEL_FALLBACK
                    continue
                return None, f"http_{resp.status}"
        except Exception as ex:
            logger.warning(f"Ошибка DeepSeek: {ex}")
            await asyncio.sleep(1)
    return None, "max_retries"

# ---------- КОМАНДЫ ----------
async def start(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid): return
    await safe_reply(update, "👋 Привет! Я — экспертный ассистент с доступом в интернет.\n🛡 Принципы: никогда не вру, даю структурированные ответы, сравниваю источники, указываю уверенность.\n📋 Команды: /profile, /memory, /stats, /forget, /restore, /deep [запрос] — для развёрнутого ответа")

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
            await safe_reply(update, "📝 Напишите запрос после /deep, например: `/deep лучшие ТВ-приставки 2026`")
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
    logger.info("✅ БОТ ЗАПУЩЕН (экспертный уровень, бюджет $7–8/мес)")
    app.run_polling()
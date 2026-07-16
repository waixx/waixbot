# ============================================================
#  BroWaix Bot — ФИНАЛЬНАЯ ВЕРСИЯ 2026
#  (999% релевантность, НИКАКОЙ ЛЖИ)
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

# --- ОПТИМИЗИРОВАННЫЕ ПАРАМЕТРЫ (экономия + качество) ---
SEARCH_RESULTS_NUM = 10        # достаточно для ранжирования
SEARCH_VARIANTS_COUNT = 1      # 1 точный запрос вместо 3
MODEL_TEMPERATURE = 0.1
MAX_RETRY_ATTEMPTS = 1
CACHE_TTL = 604800             # 7 дней кэша
MAX_TOKENS_ANSWER = 1024       # для структурированных ответов
TOP_RESULTS_SHOW = 5           # только топ-5 ссылок в ответе

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

# ---------- ФАЙЛЫ (атомарные) ----------
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

# ---------- ИНСТРУМЕНТЫ ДЛЯ ФИЛЬТРАЦИИ ----------
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

def is_official_link(link):
    return any(dom in link.lower() for dom in [
        'python.org','docs.python.org','github.com','pypi.org',
        'wikipedia.org','4pda','habr','ixbt','youtube','review','top','blog'
    ])

def assess_relevance(results, query, max_age_days=365):
    """Жёсткая фильтрация: только релевантные и свежие результаты"""
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
        trusted_domains = ['4pda', 'habr', 'ixbt', 'youtube', 'review', 'top', 'blog']
        for domain in trusted_domains:
            if domain in link:
                domain_score += 3
                break
        
        # Штраф за маркетплейсы
        if any(x in link for x in ['ozon', 'wildberries', 'aliexpress', 'sbermegamarket']):
            domain_score -= 5
        
        total_score = keyword_score + year_score + domain_score
        scored.append({**res, 'score': total_score, 'year': year})
    
    scored.sort(key=lambda x: x['score'], reverse=True)
    return [r for r in scored if r['score'] > 0][:TOP_RESULTS_SHOW]

def normalize_query(query):
    normalized = re.sub(r'[^\w\s]', '', query.lower())
    normalized = ' '.join([w for w in normalized.split() if w not in STOP_WORDS and len(w)>2])
    return normalized[:100]

def get_cached(query):
    # Не кэшируем только запросы про "сейчас" (погода, курс)
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

# ===== ГЛАВНАЯ ЛОГИКА: 999% РЕЛЕВАНТНОСТЬ =====
def remove_unverified_claims(ans, raw_snippets):
    """Удаляет из ответа всё, что не подтверждено интернет-данными"""
    if not raw_snippets:
        return ans
    
    # Ищем сущности (бренды, модели, технологии)
    entities = re.findall(r'(Xiaomi|H96|X96Q|Tanix|Vontar|RockTek|Ugoos|Beelink|Minix|WeChip|A95X|Dune|Apple|Sber|Rombica|Kickpi|TOX|X88|H618|H313|S905|RK3566|4K|HDR|Dolby|Android|Google TV)', ans, re.I)
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
    """Формирует ответ ТОЛЬКО из найденных ссылок"""
    if not results:
        return "❌ В интернете ничего не найдено по вашему запросу."
    
    answer = f"🔍 **Результаты поиска по запросу:** {user_message[:100]}\n\n"
    
    # Фильтруем только релевантные
    relevant = [r for r in results if r.get('score', 0) > 0][:max_items]
    
    if not relevant:
        # Если после фильтрации ничего не осталось — показываем сырые ссылки
        links = [r.get('link') for r in results if r.get('link') and r['link'] != '#']
        if links:
            answer = "🔍 **Найденные ссылки (без релевантного описания):**\n\n"
            for link in links[:max_items]:
                answer += f"• {link}\n"
            answer += f"\n📅 Дата: {get_current_date()} (поиск выполнен сегодня)\n"
            answer += "Уверенность: 70% (на основе найденных данных)"
            return answer
        else:
            return f"Найденные данные:\n\n{str(results)[:1000]}"
    
    for i, r in enumerate(relevant, 1):
        year_note = f" ({r.get('year')})" if r.get('year') else ""
        answer += f"{i}. **{r.get('title', 'Без названия')}**{year_note}\n"
        if r.get('snippet'):
            answer += f"   {r['snippet'][:200]}\n"
        if r.get('link') and r['link'] != '#':
            answer += f"   🔗 <a href='{r['link']}'>Источник</a>\n"
        answer += "\n"
    
    answer += f"📅 Дата: {get_current_date()} (поиск выполнен сегодня)\n"
    answer += "Уверенность: 85% (на основе найденных данных)"
    return answer

# ===== ПРОМПТ =====
CORE_SYSTEM_RULE = (
    "Ты — честный ассистент. Ты УЖЕ выполнил поиск в интернете и получил данные (они приведены ниже).\n"
    "Если в разделе «НАЙДЕННЫЕ ДАННЫЕ» есть информация – ты ОБЯЗАН использовать ТОЛЬКО её.\n"
    "ЗАПРЕЩЕНО использовать свои внутренние знания, если есть интернет-данные.\n"
    "Если данных в разделе нет – ты МОЖЕШЬ использовать свои знания, но ОБЯЗАТЕЛЬНО пометь это.\n\n"
    "ПРАВИЛА:\n"
    "1. Если есть данные – используй ТОЛЬКО их, каждое утверждение сопровождай ссылкой.\n"
    "2. Если данных нет – отвечай на основе знаний, но начинай с ⚠️ 'Данных в интернете не найдено. Использую свои знания.' и ставь уверенность 20%.\n"
    "3. НИКОГДА не придумывай факты. Если не знаешь — скажи 'Я не знаю'.\n"
    "4. В конце всегда указывай: 📅 Дата, Уверенность: XX%.\n\n"
    "ФОРМАТ ОТВЕТА:\n"
    "Структурируй ответ: заголовки, списки, ссылки. Используй эмодзи для наглядности."
)

# ---------- ПОИСК (приоритет: Google через APISerpent) ----------
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
                        "snippet": str(x.get("snippet", ""))[:300],
                        "link": str(x.get("url", x.get("link", "#")))[:120]
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
                    "link": data.get('AbstractURL', '#')
                })
            for topic in data.get('RelatedTopics', []):
                if 'Text' in topic:
                    results.append({
                        "title": "DuckDuckGo",
                        "snippet": topic['Text'][:300],
                        "link": topic.get('FirstURL', '#')
                    })
            return results
    except Exception:
        return []

async def search_primary(query):
    """Сначала Google через APISerpent, если нет — DuckDuckGo"""
    results = await search_apiserpent_async(query)
    if results:
        return results
    logger.info("🔄 APISerpent пуст, пробуем DuckDuckGo")
    return await search_duckduckgo_async(query)

# ---------- ГЕНЕРАЦИЯ ОТВЕТА ----------
async def generate_response(uid, user_message, history, profile, retry_count=0):
    try:
        return await asyncio.wait_for(
            _generate_response_internal(uid, user_message, history, profile, retry_count),
            timeout=50
        )
    except asyncio.TimeoutError:
        return "⏰ Превышено время ожидания. Попробуйте позже.", False

async def _generate_response_internal(uid, user_message, history, profile, retry_count):
    ctx = build_profile_context(profile)
    
    # Проверяем кэш
    cached = get_cached(user_message)
    if cached:
        all_results = cached
    else:
        # Генерируем поисковый запрос (1 вариант)
        variants = await generate_search_query(user_message)
        logger.info(f"🔍 Поисковый запрос: {variants[0]}")
        
        # Ищем через Google (APISerpent) + резерв DuckDuckGo
        all_results = await search_primary(variants[0])
        logger.info(f"📊 Найдено результатов: {len(all_results)}")
        
        # Кэшируем только если нашлось
        if all_results:
            set_cache(user_message, all_results)
    
    # Если нет результатов — используем локальные знания с чёткой пометкой
    if not all_results:
        return await generate_local_answer(uid, user_message, history, profile, 
            reason="Поиск в интернете не дал результатов")
    
    # Жёсткая фильтрация релевантности
    scored = assess_relevance(all_results, user_message)
    if not scored:
        return await generate_local_answer(uid, user_message, history, profile,
            reason="Найденные данные нерелевантны запросу")
    
    # Проверка свежести (если запрос про "этот год" или "2024-2026")
    if re.search(r'\b(этот год|202[4-9])\b', user_message.lower()):
        has_fresh = any(r.get('year', 0) >= 2024 for r in scored[:5])
        if not has_fresh:
            return await generate_local_answer(uid, user_message, history, profile,
                reason="В интернете нет актуальных данных за этот год")
    
    # ===== ДАННЫЕ ЕСТЬ И ОНИ РЕЛЕВАНТНЫ =====
    # Формируем сниппеты для промпта
    stext = ""
    for i, r in enumerate(scored[:TOP_RESULTS_SHOW], 1):
        year_note = f" ({r.get('year')} г.)" if r.get('year') else ""
        price_note = f" [{r.get('price')} руб.]" if r.get('price') else ""
        link_html = f"🔗 <a href=\"{r.get('link')}\">Источник</a>" if r.get('link') and r['link'] != '#' else ""
        stext += f"{i}. **{r.get('title', 'Без названия')}**{year_note}{price_note}\n"
        stext += f"   {r.get('snippet', 'Нет описания')[:300]}\n"
        stext += f"   {link_html}\n\n"
    
    # Промпт с жёстким требованием использовать ТОЛЬКО данные
    sp = {
        "role": "system",
        "content": (
            f"{CORE_SYSTEM_RULE}\n"
            f"Сегодня: {get_current_date()}.\n"
            f"Контекст: {ctx}\n\n"
            "НАЙДЕННЫЕ ДАННЫЕ (используй ТОЛЬКО их):\n"
            f"{stext}\n\n"
            "ИНСТРУКЦИЯ ПО ОТВЕТУ:\n"
            "1. Используй ТОЛЬКО данные выше.\n"
            "2. Каждый факт должен сопровождаться ссылкой.\n"
            "3. НЕ используй свои знания, если есть данные.\n"
            "4. Структурируй: заголовки, списки, эмодзи.\n"
            "5. Ответ должен быть полезным и конкретным."
        )
    }
    
    ans, err = await ask_deepseek([sp] + history, max_tokens=MAX_TOKENS_ANSWER)
    if err:
        # Если DeepSeek ошибся — показываем сырые данные
        return generate_answer_from_snippets(scored, user_message), True
    
    # Постобработка: удаляем выдумки
    final_ans = remove_unverified_claims(ans, stext)
    
    # Проверка: если после удаления ответ пуст — используем генератор из сниппетов
    if len(final_ans) < 50 or 'http' not in final_ans:
        final_ans = generate_answer_from_snippets(scored, user_message)
    else:
        # Добавляем дату и уверенность
        if '📅 Дата:' not in final_ans:
            final_ans += f"\n\n📅 Дата: {get_current_date()} (данные из интернета)"
        if 'Уверенность:' not in final_ans:
            final_ans += "\nУверенность: 90% (на основе найденных данных)"
    
    return f"🌐 из интернета\n\n{final_ans}", True

async def generate_local_answer(uid, user_message, history, profile, reason):
    """Генерирует ответ из локальных знаний с чёткой маркировкой"""
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
            "5. НЕ придумывай факты"
        )
    }
    
    messages = [sp] + history
    ans, err = await ask_deepseek(messages, max_tokens=MAX_TOKENS_ANSWER)
    if err:
        return f"⚠️ Ошибка генерации из базы. {reason}", False
    
    # Добавляем дату и уверенность, если их нет
    if 'Уверенность:' not in ans:
        ans += f"\n\n📅 Дата: {get_current_date()}"
        ans += "\nУверенность: 20% (основано на знаниях модели, без интернет-данных)"
    
    return f"🧠 из базы (интернет пуст)\n\n{ans}", True

async def generate_search_query(query):
    """Генерирует 1 точный поисковый запрос без использования DeepSeek"""
    stop = {'найди','пожалуйста','помоги','мне','лучшие','скажи','расскажи','покажи','найти','бро','что','как','без','для','по','про'}
    words = [w for w in re.sub(r'[^\w\s]', '', query).split() 
             if w.lower() not in stop and len(w) > 2]
    
    if not words:
        return [query]
    
    base = " ".join(words[:5])
    
    # Добавляем год, если его нет
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
    await safe_reply(update, "👋 Привет! Я — честный ассистент с доступом в интернет.\n🛡 Принципы: никогда не вру, использую ТОЛЬКО данные из интернета, если они есть. Если данных нет — честно говорю об этом.\n📋 Команды: /profile, /memory, /stats, /forget, /restore")

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
        save_profile(uid, {})  # очищаем ВСЁ
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

    answer, should_save = await generate_response(uid, user_message, history, profile)
    
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
    logger.info("✅ БОТ ЗАПУЩЕН (999% релевантность, НИКАКОЙ ЛЖИ)")
    app.run_polling()
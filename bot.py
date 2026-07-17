# ================================================================
#  Universal Bot — ЭТАЛОННАЯ ВЕРСИЯ С ДОПОЛНИТЕЛЬНОЙ ЗАЩИТОЙ КЭША
#  Добавлено: ограничение размера html_cache в fetch_and_clean
# ================================================================
import logging, os, json, sys, re, asyncio, aiohttp, shutil, weakref, hashlib, uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)
from logging.handlers import RotatingFileHandler
from bs4 import BeautifulSoup
from html import escape

# ---------- НАСТРОЙКА ЛОГИРОВАНИЯ ----------
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

# ---------- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LLM_API_KEY = os.getenv("LLM_API_KEY")
SEARCH_API_KEY = os.getenv("SEARCH_API_KEY")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0") or 0)
ALLOWED_USERS_LIST = [int(x.strip()) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()]
if ADMIN_USER_ID and ADMIN_USER_ID not in ALLOWED_USERS_LIST:
    ALLOWED_USERS_LIST.append(ADMIN_USER_ID)

TZ = ZoneInfo(os.getenv("TIMEZONE", "UTC") or "UTC")
def now(): return datetime.now(TZ)
def get_current_date(): return now().strftime("%d.%m.%Y")
def get_current_time(): return now().strftime("%H:%M")

# ---------- ПАРАМЕТРЫ LLM И ПОИСКА ----------
LLM_API_BASE = os.getenv("LLM_API_BASE", "https://api.deepseek.com/v1")
LLM_MODEL_DEFAULT = os.getenv("LLM_MODEL_DEFAULT", "deepseek-chat")
LLM_MODEL_FALLBACK = os.getenv("LLM_MODEL_FALLBACK", "deepseek-chat")
SEARCH_ENGINE = os.getenv("SEARCH_ENGINE", "google")

# ---------- ОПТИМИЗИРОВАННЫЕ ПАРАМЕТРЫ ----------
SEARCH_RESULTS_NUM = 3
TOP_RESULTS_SHOW = 3
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.1"))
MAX_RETRY_ATTEMPTS = 1
CACHE_TTL = 172800  # 2 дня
MAX_TOKENS_ANSWER = int(os.getenv("MAX_TOKENS_ANSWER", "1024"))
MAX_HTML_LEN = 5000
LEVEL_1 = {'max_history': 20, 'keep_recent': 5}
LEVEL_2 = {'compress_interval': 20, 'compress_to': 30}
CACHE_CLEANUP_INTERVAL = 3600

if not TELEGRAM_TOKEN or not LLM_API_KEY:
    logger.error("❌ TELEGRAM_TOKEN или LLM_API_KEY не заданы")
    sys.exit(1)

DATA_DIR, BACKUP_DIR = "data", "data/backups"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

def memory_path(uid): return os.path.join(DATA_DIR, f"memory_{uid}.json")
def profile_path(uid): return os.path.join(DATA_DIR, f"profile_{uid}.json")
def counter_path(uid): return os.path.join(DATA_DIR, f"counter_{uid}.json")

# ---------- ГЛОБАЛЬНОЕ СОСТОЯНИЕ ----------
_http_session = None
_session_lock = None
user_locks = weakref.WeakValueDictionary()
_rate_lock = None
request_count = {}
search_cache = {}
answer_cache = {}
html_cache = {}  # {url: {"text": str, "expires": datetime}}

def get_user_lock(uid):
    return user_locks.setdefault(uid, asyncio.Lock())

async def get_http_session():
    global _http_session, _session_lock
    async with _session_lock:
        if _http_session is None or _http_session.closed:
            _http_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=20, limit_per_host=10),
                timeout=aiohttp.ClientTimeout(total=60, connect=10, sock_read=45)
            )
        return _http_session

async def cleanup_http_session():
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()

# ---------- ФАЙЛОВЫЕ ОПЕРАЦИИ (АТОМАРНЫЕ) ----------
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
        logger.error(f"Ошибка atomic_write({filename}): {ex}")
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except: pass
        return False

def atomic_read(filename, default=None, as_json=True):
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f) if as_json else f.read()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default

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

def create_backup(uid, data_type):
    try:
        ts = now().strftime("%Y%m%d_%H%M%S")
        fname = os.path.join(BACKUP_DIR, f"{data_type}_{uid}_{ts}.json")
        if data_type == "profile":
            atomic_write(fname, load_profile(uid))
        elif data_type == "memory":
            atomic_write(fname, load_memory_raw(uid))
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith(f"{data_type}_{uid}_")])
        for old in backups[:-5]:
            try: os.remove(os.path.join(BACKUP_DIR, old))
            except: pass
        return True
    except Exception as ex:
        logger.error(f"Ошибка create_backup: {ex}")
        return False

async def restore_backup(uid, data_type):
    async with get_user_lock(uid):
        try:
            backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith(f"{data_type}_{uid}_")])
            if not backups:
                return False
            with open(os.path.join(BACKUP_DIR, backups[-1]), 'r', encoding='utf-8') as f:
                data = json.load(f)
            if data_type == "profile":
                save_profile(uid, data, backup=False)
            elif data_type == "memory":
                await save_memory(uid, data, backup=False, lock_held=True)
            logger.info(f"✅ Восстановлен {data_type} для {uid}")
            return True
        except Exception as ex:
            logger.error(f"Ошибка restore_backup: {ex}")
            return False

# ---------- СЖАТИЕ ИСТОРИИ ----------
STOP_WORDS = {'это','так','вот','ну','просто','очень','что','как','где','когда','для','без','по'}

def extract_key_points(text, max_len=40):
    if not text or len(text) <= max_len:
        return str(text)[:max_len]
    imp = [w for w in text.split() if w.lower() not in STOP_WORDS and len(w) > 2]
    result = ' '.join(imp[:8])[:max_len]
    return result + "..." if len(result) == max_len else result

def compress_history(history):
    if not isinstance(history, list):
        return []
    if len(history) <= LEVEL_1['max_history']:
        return history
    recent = history[-LEVEL_1['keep_recent']:]
    old = history[:-LEVEL_1['keep_recent']]
    summary = []
    for m in old[-8:]:
        if not isinstance(m, dict):
            continue
        r, c = m.get("role", ""), m.get("content", "")
        if r == "user":
            summary.append(f"Q: {extract_key_points(c, 50)}")
        elif r == "assistant":
            summary.append(f"A: {extract_key_points(c, 50)}")
    if summary:
        return [{"role": "system", "content": "📚 История:\n" + "\n".join(summary[-5:])}] + recent
    return recent

def load_memory(uid):
    return compress_history(load_memory_raw(uid))

def _update_level(uid, messages, key, cfg, extractor, ext_len, ts_fmt):
    try:
        profile = load_profile(uid)
        profile.setdefault(key, [])
        ts = now().strftime(ts_fmt)
        for m in messages[-cfg['compress_interval']:]:
            if not isinstance(m, dict):
                continue
            r, c = m.get("role", ""), m.get("content", "")
            if r == "user":
                profile[key].append(f"[{ts}] Q: {extractor(c, ext_len)}")
            elif r == "assistant":
                profile[key].append(f"[{ts}] A: {extractor(c, ext_len)}")
        if len(profile[key]) > cfg['compress_to']:
            profile[key] = profile[key][-cfg['compress_to']:]
        save_profile(uid, profile, backup=False)
    except Exception as ex:
        logger.error(f"Ошибка _update_level: {ex}")

async def _save_memory_impl(uid, history, backup):
    try:
        if not isinstance(history, list):
            return False
        if len(history) > LEVEL_1['max_history']:
            old = history[:-LEVEL_1['keep_recent']]
            if old:
                _update_level(uid, old, "level_2", LEVEL_2, extract_key_points, 40, "%d.%m")
        if not atomic_write(memory_path(uid), compress_history(history)):
            return False
        if backup:
            create_backup(uid, "memory")
        cnt = load_counter(uid) + 1
        save_counter(uid, cnt)
        return True
    except Exception as ex:
        logger.error(f"Ошибка _save_memory_impl: {ex}")
        return False

async def save_memory(uid, history, backup=True, lock_held=False):
    if lock_held:
        return await _save_memory_impl(uid, history, backup)
    async with get_user_lock(uid):
        return await _save_memory_impl(uid, history, backup)

# ---------- ЛОКАЛЬНАЯ ОЧИСТКА HTML (с lxml) ----------
def clean_html_to_text(html: str, max_len: int = MAX_HTML_LEN) -> str:
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, 'lxml')
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'form', 'noscript', 'iframe', 'svg']):
            tag.decompose()
        for tag in soup.find_all():
            if not tag.get_text(strip=True):
                tag.decompose()
        text = soup.get_text(separator=' ', strip=True)
        text = re.sub(r'\s+', ' ', text)
        return text[:max_len]
    except Exception as e:
        logger.warning(f"Ошибка очистки HTML: {e}")
        return re.sub(r'<[^>]+>', ' ', html)[:max_len]

async def fetch_and_clean(url: str) -> str:
    now_time = datetime.now()
    if url in html_cache and html_cache[url]["expires"] > now_time:
        logger.info(f"✅ Cache HIT (HTML) для {url[:50]}...")
        return html_cache[url]["text"]
    
    session = await get_http_session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        async with session.get(url, headers=headers, timeout=15) as resp:
            if resp.status == 200:
                html = await resp.text()
                clean_text = clean_html_to_text(html)
                if clean_text:
                    html_cache[url] = {
                        "text": clean_text,
                        "expires": now_time + timedelta(seconds=CACHE_TTL)
                    }
                    # --- ДОБАВЛЕНО: ограничение размера кэша ---
                    if len(html_cache) > 200:
                        oldest = min(html_cache.keys(), key=lambda k: html_cache[k]["expires"])
                        del html_cache[oldest]
                    return clean_text
            logger.warning(f"Не удалось загрузить {url}, статус {resp.status}")
    except Exception as e:
        logger.warning(f"Ошибка загрузки {url}: {e}")
    return ""

# ---------- ИНСТРУМЕНТЫ УНИВЕРСАЛЬНОГО АНАЛИЗА ----------
def extract_year_from_text(text):
    if not isinstance(text, str):
        return None
    match = re.search(r'\b(20[2-9][0-9])\b', text)
    return int(match.group(1)) if match else None

def normalize_query(query):
    if not isinstance(query, str):
        return ""
    normalized = re.sub(r'[^\w\s]', '', query.lower())
    normalized = ' '.join([w for w in normalized.split() if w not in STOP_WORDS and len(w) > 2])
    return normalized[:100]

def get_cached(query):
    norm_key = normalize_query(query)
    if not norm_key:
        return None
    if norm_key in search_cache:
        age = (datetime.now() - search_cache[norm_key]['time']).total_seconds()
        if age < CACHE_TTL:
            logger.info("✅ Cache HIT (поиск)")
            return search_cache[norm_key]['data']
        else:
            del search_cache[norm_key]
    return None

def set_cache(query, data):
    norm_key = normalize_query(query)
    if not norm_key:
        return
    search_cache[norm_key] = {'data': data, 'time': datetime.now()}
    if len(search_cache) > 200:
        oldest = min(search_cache.keys(), key=lambda k: search_cache[k]['time'])
        del search_cache[oldest]

def assess_relevance(results, query):
    if not results or not isinstance(results, list):
        return []
    
    query_year = None
    year_match = re.search(r'\b(20[2-9][0-9])\b', query)
    if year_match:
        query_year = int(year_match.group(1))
    
    requires_year = any(word in query.lower() for word in ['новинк', 'последн', 'свеж', 'актуальн', 'этот год', 'сейчас', 'сегодня'])
    
    stop_words = {'найди','пожалуйста','помоги','мне','лучшие','скажи','расскажи','покажи','найти','бро', 'какая', 'какой', 'что', 'где'}
    keywords = [w.lower() for w in re.sub(r'[^\w\s]', '', query).split() 
                if w.lower() not in stop_words and len(w) > 2]
    
    scored = []
    for res in results:
        if not isinstance(res, dict):
            continue
        
        title = res.get('title', '') or ''
        snippet = res.get('snippet', '') or ''
        text_lower = (title + ' ' + snippet).lower()
        link = res.get('link', '').lower()
        
        keyword_score = sum(3 for kw in keywords if kw in text_lower)
        
        # Динамический анализ URL (Универсальный)
        domain_score = 0
        for kw in keywords:
            if len(kw) > 3 and kw in link:
                domain_score += 4  # Тематическое совпадение
                
        if any(zone in link for zone in ['.gov', '.edu', '.org', 'wikipedia.org']):
            domain_score += 5
            
        # Штрафуем маркетплейсы и мусорные магазины
        spam_domains = ['ozon', 'wildberries', 'aliexpress', 'avito', 'amazon', 'ebay', 'taobao', 'sbermegamarket', 'prom.ua', 'olx']
        if any(spam in link for spam in spam_domains):
            domain_score -= 8
            
        year = extract_year_from_text(text_lower)
        year_score = 0
        if year:
            if query_year and year == query_year:
                year_score = 10
            elif query_year and year > query_year:
                year_score = -5
            elif year >= 2025:
                year_score = 8
            elif year >= 2024:
                year_score = 5
        else:
            if requires_year:
                year_score = -2
                
        total = keyword_score + year_score + domain_score
        scored.append({**res, 'score': total, 'year': year})
        
    relevant = [r for r in scored if r['score'] > 1]
    relevant.sort(key=lambda x: x['score'], reverse=True)
    return relevant[:TOP_RESULTS_SHOW]

def remove_unverified_claims(ans, raw_text):
    """
    Универсальный динамический фильтр галлюцинаций.
    """
    if not raw_text or not ans:
        return ans
    entities = re.findall(r'\b([A-ZА-Я][a-zA-Zа-яА-Я0-9\-]+|\d+[-+°]*[CС]?)\b', ans)
    modified = False
    ignore_set = {'В','На','По','За','Из','Для','Этот','Эта','Эти','Только','Как','Что','Где','Когда'}
    
    for entity in set(entities):
        if entity in ignore_set or len(entity) < 2:
            continue
        if entity.lower() not in raw_text.lower():
            ans = re.sub(rf'\b{re.escape(entity)}\b', '', ans)
            modified = True
            logger.info(f"🔍 Универсальный фильтр удалил неподтверждённый факт: {entity}")
            
    if modified:
        ans = re.sub(r'\s+', ' ', ans)
        ans = re.sub(r'\s*\.\s*', '. ', ans)
        ans = re.sub(r'\s*,', ',', ans)
        ans = re.sub(r'\.\s*\.', '.', ans)
    return ans

def generate_answer_from_snippets(results, user_message, max_items=5):
    if not results:
        return "❌ В интернете ничего не найдено."
    relevant = [r for r in results if r.get('score', 0) > 0][:max_items]
    if not relevant:
        links = [r.get('link') for r in results if r.get('link') and r['link'] != '#']
        if links:
            answer = "🔍 **Найденные ссылки:**\n\n"
            for link in links[:max_items]:
                answer += f"• {link}\n"
            answer += f"\n📅 Дата: {get_current_date()}\nУверенность: 70%"
            return answer
        else:
            return "❌ Не удалось получить результаты."
    answer = f"🔍 Результаты поиска\n\n"
    for i, r in enumerate(relevant, 1):
        title = r.get('title', 'Без названия')
        snippet = r.get('snippet', 'Нет описания')[:200]
        link = r.get('link', '')
        year_note = f" ({r.get('year')})" if r.get('year') else ""
        answer += f"{i}. **{title}**{year_note}\n"
        answer += f"   {snippet}\n"
        if link and link != '#':
            answer += f"   🔗 <a href='{link}'>Источник</a>\n"
        answer += "\n"
    answer += f"📅 Дата: {get_current_date()}\nУверенность: 85%"
    return answer

# ---------- ПОИСК (APISerpent + DuckDuckGo) ----------
async def search_apiserpent_async(query):
    if not SEARCH_API_KEY:
        return []
    session = await get_http_session()
    try:
        logger.info(f"🔍 APISerpent: {query[:50]}...")
        params = {
            "q": query,
            "engine": SEARCH_ENGINE,
            "num": SEARCH_RESULTS_NUM,
        }
        async with session.get(
            "https://apiserpent.com/api/search",
            params=params,
            headers={"X-API-Key": SEARCH_API_KEY},
            timeout=20
        ) as r:
            if r.status != 200:
                logger.warning(f"APISerpent статус: {r.status}")
                return []
            data = await r.json()
            results = []
            organic = data.get("results", {}).get("organic", []) if isinstance(data.get("results"), dict) else data.get("organic_results", [])
            for x in organic[:SEARCH_RESULTS_NUM]:
                if isinstance(x, dict):
                    results.append({
                        "title": str(x.get("title", ""))[:120],
                        "snippet": str(x.get("snippet", ""))[:300],
                        "link": str(x.get("url", x.get("link", "#")))[:120]
                    })
            return results
    except Exception as e:
        logger.warning(f"APISerpent ошибка: {type(e).__name__}: {str(e)}")
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
            if r.status != 200:
                return []
            data = await r.json()
            results = []
            if data.get('AbstractText'):
                results.append({
                    "title": "Результат DuckDuckGo",
                    "snippet": data['AbstractText'][:500],
                    "link": data.get('AbstractURL', '#')
                })
            for topic in data.get('RelatedTopics', []):
                if 'Text' in topic:
                    results.append({
                        "title": "Результат DuckDuckGo",
                        "snippet": topic['Text'][:300],
                        "link": topic.get('FirstURL', '#')
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

# ---------- ГЕНЕРАЦИЯ ОТВЕТА ----------
async def generate_response(uid, user_message, history, profile):
    try:
        return await asyncio.wait_for(
            _generate_response_internal(uid, user_message, history, profile),
            timeout=60
        )
    except asyncio.TimeoutError:
        return "⏰ Превышено время ожидания. Попробуйте позже.", False

async def _generate_response_internal(uid, user_message, history, profile):
    cache_key = hashlib.md5(user_message.encode()).hexdigest()
    if cache_key in answer_cache:
        logger.info("✅ Cache HIT (ответ LLM)")
        return answer_cache[cache_key], True

    cached = get_cached(user_message)
    if cached:
        all_results = cached
    else:
        all_results = await search_primary(user_message)
        logger.info(f"📊 Найдено результатов: {len(all_results)}")
        if all_results:
            set_cache(user_message, all_results)

    if not all_results:
        return (
            "❌ По вашему запросу в интернете ничего не найдено.\n"
            "Попробуйте перефразировать запрос.",
            False
        )

    scored = assess_relevance(all_results, user_message)
    if not scored:
        return (
            "❌ Найденные данные нерелевантны вашему запросу.\n"
            "Пожалуйста, уточните запрос.",
            False
        )

    full_texts = []
    for r in scored[:TOP_RESULTS_SHOW]:
        clean = await fetch_and_clean(r['link'])
        if clean:
            full_texts.append(f"--- ИСТОЧНИК: {r['link']} ---\n{clean}")
        else:
            full_texts.append(
                f"--- ИСТОЧНИК (сниппет): {r['link']} ---\n"
                f"Заголовок: {r.get('title', '')}\n"
                f"Описание: {r.get('snippet', '')}"
            )

    stext = "\n\n".join(full_texts) if full_texts else "Нет доступного контента."

    system_prompt = {
        "role": "system",
        "content": (
            "Ты — честный ассистент. Ты получил данные из интернета (они ниже).\n"
            "Используй ТОЛЬКО эти данные для ответа. Не придумывай информацию.\n"
            "Каждый факт должен сопровождаться ссылкой на источник.\n"
            "Структурируй ответ: заголовки, списки, таблицы, эмодзи.\n"
            "В конце укажи: 📅 Дата, Уверенность: XX%.\n\n"
            f"КОНТЕНТ СТРАНИЦ:\n{stext}"
        )
    }

    ans, err = await ask_llm([system_prompt] + history, max_tokens=MAX_TOKENS_ANSWER)
    if err or ans is None:
        logger.warning(f"DeepSeek не ответил: err={err}, ans={ans}")
        fallback = generate_answer_from_snippets(scored, user_message)
        return fallback, True

    final_ans = remove_unverified_claims(ans, stext)
    if len(final_ans) < 50 or 'http' not in final_ans:
        final_ans = generate_answer_from_snippets(scored, user_message)
    else:
        if '📅 Дата:' not in final_ans:
            final_ans += f"\n\n📅 Дата: {get_current_date()}"
        if 'Уверенность:' not in final_ans:
            final_ans += "\nУверенность: 90%"

    result = f"🌐 Из интернета\n\n{final_ans}"
    answer_cache[cache_key] = result
    return result, True

# ---------- ЗАПРОС К LLM ----------
async def ask_llm(messages, retries=2, max_tokens=None, model=None):
    if model is None:
        model = LLM_MODEL_DEFAULT
    session = await get_http_session()
    for attempt in range(retries):
        try:
            payload = {
                "model": model,
                "messages": messages,
                "temperature": LLM_TEMPERATURE,
                "max_tokens": max_tokens
            }
            async with session.post(
                f"{LLM_API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json=payload,
                timeout=30
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("choices"):
                        c = data["choices"][0].get("message", {}).get("content")
                        return (c, None) if c is not None else (None, "empty")
                if resp.status == 429:
                    await asyncio.sleep(2)
                    continue
                if resp.status in (400, 404) and model != LLM_MODEL_FALLBACK:
                    logger.warning(f"Модель {model} недоступна, пробуем {LLM_MODEL_FALLBACK}")
                    model = LLM_MODEL_FALLBACK
                    continue
                return None, f"http_{resp.status}"
        except Exception as ex:
            logger.warning(f"Ошибка LLM: {ex}")
            await asyncio.sleep(1)
    return None, "max_retries"

# ---------- RATE LIMIT ----------
RATE_LIMIT, RATE_WINDOW = 5, 10
async def check_rate_limit(uid):
    global _rate_lock
    async with _rate_lock:
        now_ts = datetime.now().timestamp()
        request_count[uid] = [t for t in request_count.get(uid, []) if now_ts - t < RATE_WINDOW]
        if len(request_count[uid]) >= RATE_LIMIT:
            return False
        request_count[uid].append(now_ts)
        return True

# ---------- БЕЗОПАСНАЯ ОТПРАВКА ----------
async def safe_reply(update: Update, text: str, reply_markup=None):
    if not text or not isinstance(text, str):
        text = "⚠️ Пустой ответ."
    msg = update.effective_message
    if msg is None:
        return

    def markdown_to_html_safe(t):
        links = []
        def save_link(match):
            link_text = match.group(1)
            link_url = match.group(2)
            uid_placeholder = f"__LINK_{uuid.uuid4().hex}__"
            links.append((uid_placeholder, link_text, link_url))
            return uid_placeholder
            
        t = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', save_link, t)
        t = escape(t)
        for placeholder, link_text, link_url in links:
            t = t.replace(placeholder, f'<a href="{escape(link_url)}">{escape(link_text)}</a>')
        t = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', t)
        return t.strip()

    if len(text) > 20 and not text.startswith(('/', '❌', '✅')):
        text = markdown_to_html_safe(text)
    try:
        if len(text) > 4096:
            for i in range(0, len(text), 4096):
                await msg.reply_text(text[i:i+4096], parse_mode='HTML', disable_web_page_preview=True, reply_markup=reply_markup)
        else:
            await msg.reply_text(text, parse_mode='HTML', disable_web_page_preview=True, reply_markup=reply_markup)
    except Exception as ex:
        logger.error(f"Ошибка safe_reply: {ex}")
        clean = re.sub(r'<[^>]+>', '', text)
        try:
            await msg.reply_text(clean[:4096], reply_markup=reply_markup)
        except:
            pass

def is_allowed(uid):
    return not ALLOWED_USERS_LIST or uid in ALLOWED_USERS_LIST

# ---------- КОМАНДЫ ----------
async def start(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid): return
    await safe_reply(update,
        "👋 Привет! Я — честный ассистент с доступом в интернет.\n"
        "🛡 Принципы: использую ТОЛЬКО данные из интернета, ничего не выдумываю.\n"
        "📋 Команды: /profile, /memory, /stats, /forget, /restore"
    )

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
    exclude = {'updated', 'level_2'}
    personal = {k: v for k, v in p.items() if k not in exclude}
    if personal:
        for k, v in personal.items():
            lines.append(f"• {k}: {v}")
    else:
        lines.append("• Пока ничего не запомнил")
    lines.append(f"\n🔄 Обновлено: {p.get('updated', 'неизвестно')}")
    await safe_reply(update, "\n".join(lines))

async def memory_command(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid): return
    if not context.args:
        await safe_reply(update, "🔍 Использование: `/memory что_искать`")
        return
    query = ' '.join(context.args)
    res = search_in_pyramid(uid, query)
    if not res:
        await safe_reply(update, f"📭 Ничего не найдено: '{query}'")
        return
    lines = [f"🔍 Результаты '{query}':"] + [f"{i}. {r}" for i, r in enumerate(res[:10], 1)]
    if len(res) > 10:
        lines.append(f"... и ещё {len(res)-10}")
    await safe_reply(update, "\n".join(lines))

def search_in_pyramid(uid, query):
    profile = load_profile(uid)
    q = query.lower()
    res = []
    for m in load_memory_raw(uid)[-30:]:
        if not isinstance(m, dict): continue
        c = m.get("content", "")
        if q in c.lower():
            role = "👤" if m.get("role") == "user" else "🤖"
            ts = m.get("timestamp", "")
            res.append(f"{role}{(' ['+ts+']') if ts else ''} {extract_key_points(c, 80)}")
    for item in profile.get("level_2", []):
        if q in item.lower():
            res.append(f"📚 {item}")
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
        msg = "✅ Восстановлено!\n" + ("📋 Профиль\n" if pr else "") + ("💬 История" if mr else "")
        await safe_reply(update, msg)
    else:
        await safe_reply(update, "❌ Нет бэкапов.")

# ---------- ОБРАБОТЧИК СООБЩЕНИЙ ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.effective_user or not update.effective_message or not update.effective_message.text:
            return
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
        user_msg_obj = {
            "role": "user",
            "content": user_message,
            "timestamp": now().strftime("%Y-%m-%d %H:%M:%S")
        }
        history.append(user_msg_obj)

        answer, should_save = await generate_response(uid, user_message, history, profile)

        if should_save and isinstance(answer, str) and len(answer) > 10:
            clean_answer = re.sub(r'<[^>]+>', '', answer)
            history.append({
                "role": "assistant",
                "content": clean_answer,
                "timestamp": now().strftime("%Y-%m-%d %H:%M:%S")
            })
            await save_memory(uid, history)

        await safe_reply(update, answer)

    except Exception as e:
        logger.error(f"КРИТИЧЕСКАЯ ОШИБКА в handle_message: {type(e).__name__}: {e}", exc_info=True)
        await safe_reply(update, "⚠️ Произошла внутренняя ошибка. Пожалуйста, попробуйте позже.")

# ---------- ФОНОВЫЕ ЗАДАЧИ ----------
async def cleanup_caches():
    while True:
        try:
            await asyncio.sleep(CACHE_CLEANUP_INTERVAL)
            now_time = datetime.now()
            expired = [k for k, v in search_cache.items() if (now_time - v['time']).total_seconds() > CACHE_TTL]
            for k in expired:
                del search_cache[k]
            if len(answer_cache) > 500:
                to_delete = sorted(answer_cache.keys())[:250]
                for k in to_delete:
                    del answer_cache[k]
            
            # Очистка HTML кэша
            expired_html = [k for k, v in html_cache.items() if v['expires'] <= now_time]
            for k in expired_html:
                del html_cache[k]
            if len(html_cache) > 200:
                oldest = sorted(html_cache.keys(), key=lambda k: html_cache[k]['expires'])[:50]
                for k in oldest:
                    del html_cache[k]
            logger.debug(f"🧹 Кэши очищены (поиск: {len(search_cache)}, ответы: {len(answer_cache)}, HTML: {len(html_cache)})")
        except asyncio.CancelledError:
            logger.info("🧹 Задача cleanup_caches корректно завершена")
            break
        except Exception as ex:
            logger.error(f"Ошибка cleanup_caches: {ex}")

async def error_handler(update, context):
    logger.error(f"Ошибка: {context.error}")

# ---------- ИНИЦИАЛИЗАЦИЯ И ЗАПУСК ----------
async def auto_restore_all_users():
    logger.info("🔄 Проверка данных при старте...")
    try:
        if not os.path.exists(BACKUP_DIR):
            return
        for fname in os.listdir(BACKUP_DIR):
            parts = fname.split('_')
            if len(parts) >= 2 and parts[0] in ('profile', 'memory'):
                try:
                    uid = int(parts[1])
                    mem_data = atomic_read(memory_path(uid), default=None)
                    prof_data = atomic_read(profile_path(uid), default=None)
                    if (mem_data is None or (isinstance(mem_data, list) and len(mem_data)==0)) or \
                       (prof_data is None or (isinstance(prof_data, dict) and len(prof_data)==0)):
                        pr = await restore_backup(uid, "profile")
                        mr = await restore_backup(uid, "memory")
                        if pr or mr:
                            logger.info(f"✅ Пользователь {uid} восстановлен")
                except:
                    pass
    except Exception as ex:
        logger.error(f"Ошибка auto_restore: {ex}")

async def post_init_callback(application):
    """Корутина, вызываемая после инициализации приложения"""
    asyncio.create_task(cleanup_caches())

async def main():
    global _session_lock, _rate_lock
    _session_lock = asyncio.Lock()
    _rate_lock = asyncio.Lock()

    await auto_restore_all_users()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init_callback).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(CommandHandler("restore", restore_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("🚀 БОТ ЗАПУЩЕН (универсальная стабильная версия)")
    try:
        await app.run_polling()
    finally:
        await cleanup_http_session()

if __name__ == "__main__":
    asyncio.run(main())

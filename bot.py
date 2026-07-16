# ============================================================
#  BroWaix Bot — финальная версия
#  (LLM-оптимизация, дата/уверенность/ссылки по умолчанию, 3 варианта)
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
handler = RotatingFileHandler("bot.log", maxBytes=5*1024*1024, backupCount=2)
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

# --- ОПТИМАЛЬНЫЕ ПАРАМЕТРЫ (4+ месяцев) ---
SEARCH_RESULTS_NUM = 10
SEARCH_VARIANTS_COUNT = 3          # теперь 3 варианта
MODEL_TEMPERATURE = 0.1
MAX_RETRY_ATTEMPTS = 1
CACHE_TTL = 86400
MAX_TOKENS_ANSWER = 500
TOP_RESULTS_SHOW = 5

# ---------- СИСТЕМНЫЙ ПРОМПТ (УСИЛЕННЫЙ) ----------
CORE_SYSTEM_RULE = (
    "Честный ассистент. Отвечай строго по данным, если они есть. "
    "Если данных нет — скажи «Я не знаю». Если делаешь предположение — начинай с «Предположительно». "
    "Не выдумывай. Включай в ответ модели, цены, бренды из сниппетов.\n\n"
    "ОБЯЗАТЕЛЬНО в конце ответа укажи:\n"
    "- 📅 Дата публикации источников (если неизвестна, напиши «дата не указана»).\n"
    "- Уверенность: XX% (если данных нет, ставь 0% или 5%).\n"
    "- Ссылки на источники (если их нет, напиши «ссылки не найдены»).\n\n"
    "ФОРМАТИРОВАНИЕ (ОБЯЗАТЕЛЬНО):\n"
    "• Каждый результат оформляй как отдельный блок.\n"
    "• Используй эмодзи: <b>жирный</b> для названия, 💰 цена, 📟 чипсет/ОС, 💾 память, 📺 особенности.\n"
    "• Ссылки: 🔗 <a href=\"URL\">Источник</a>\n"
    "• Разделяй блоки пустой строкой. Не используй таблицы."
)

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
        for old in backups[:-3]: os.remove(os.path.join(BACKUP_DIR, old))
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

# ---------- СЖАТИЕ ПАМЯТИ ----------
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

# ---------- ВСПОМОГАТЕЛЬНЫЕ ----------
def extract_year_from_text(text):
    m = re.search(r'\b(20[2-9][0-9])\b', text)
    return int(m.group(1)) if m else None

def extract_price_from_text(text):
    match = re.search(r'([\d\s]+)\s*(?:руб|₽|р\.|рублей|RUB)', text, re.I)
    if match: return int(re.sub(r'\s', '', match.group(1)))
    return None

def is_official_link(link):
    return any(dom in link.lower() for dom in ['python.org','docs.python.org','github.com','pypi.org','wikipedia.org'])

def assess_relevance(results, keywords):
    if not results: return []
    scored = []
    for res in results:
        text = (res.get('title','') + ' ' + res.get('snippet','')).lower()
        score = sum(2 for kw in keywords if kw in text)
        if extract_price_from_text(text): score += 3
        if extract_year_from_text(text): score += 2
        if is_official_link(res.get('link','')): score += 5
        if re.search(r'\d+\.\s+', text): score += 3
        scored.append({**res, 'score': score})
    scored.sort(key=lambda x: x['score'], reverse=True)
    return scored

def normalize_query(query):
    normalized = re.sub(r'[^\w\s]', '', query.lower())
    normalized = ' '.join([w for w in normalized.split() if w not in STOP_WORDS and len(w)>2])
    return normalized[:100]

def get_cached(query):
    norm_key = normalize_query(query)
    if norm_key in search_cache and (datetime.now() - search_cache[norm_key]['time']).total_seconds() < CACHE_TTL:
        logger.info("✅ Cache HIT")
        return search_cache[norm_key]['data']
    q_hash = hashlib.md5(norm_key.encode()).hexdigest()[:8]
    if q_hash in query_hash_cache:
        cached_norm = query_hash_cache[q_hash]
        if cached_norm in search_cache and (datetime.now() - search_cache[cached_norm]['time']).total_seconds() < CACHE_TTL:
            logger.info("✅ Cache HIT (вариация)")
            return search_cache[cached_norm]['data']
    return None

def set_cache(query, data):
    norm_key = normalize_query(query)
    search_cache[norm_key] = {'data': data, 'time': datetime.now()}
    q_hash = hashlib.md5(norm_key.encode()).hexdigest()[:8]
    query_hash_cache[q_hash] = norm_key
    if len(search_cache) > 100:
        oldest = min(search_cache.keys(), key=lambda k: search_cache[k]['time'])
        del search_cache[oldest]

def highlight_contradictions(text):
    markers = ['но', 'однако', 'с другой стороны', 'в то же время', 'хотя', 'несмотря на', 'с одной стороны', 'в отличие от']
    sentences = re.split(r'(?<=[.!?])\s+', text)
    highlighted = []
    found = False
    for sent in sentences:
        if any(m in sent.lower() for m in markers):
            highlighted.append(f"**{sent}**")
            found = True
        else:
            highlighted.append(sent)
    return ' '.join(highlighted), found

# ---------- УЛУЧШЕННАЯ POST-ОБРАБОТКА (ДОБАВЛЯЕТ ДАТУ, УВЕРЕННОСТЬ, ССЫЛКИ) ----------
def finalize_answer(ans, current_date):
    """Постобработка: добавляет дату, уверенность, ссылки по умолчанию, если их нет."""
    # Проверка противоречий
    highlighted, has_contradiction = highlight_contradictions(ans)
    if has_contradiction:
        ans = highlighted
        ans = f"⚠️ В ответе есть возможные противоречия (выделены жирным).\n\n{ans}"
    
    # Если нет даты – добавляем
    if '📅 Дата:' not in ans and 'дата' not in ans.lower():
        ans += f"\n\n📅 Дата: дата не указана (проверьте актуальность на {current_date})"
    
    # Если нет уверенности – добавляем
    if 'Уверенность:' not in ans:
        # Определяем, есть ли данные вообще
        if 'Я не знаю' in ans or 'не нашел' in ans:
            ans += "\n\nУверенность: 0% (данных нет)"
        else:
            ans += "\n\nУверенность: 5% (предположительно)"
    
    # Если нет ссылок – добавляем
    if 'http' not in ans and 'ссылки' not in ans.lower():
        ans += "\n\n🔗 Ссылки: не найдены"
    
    return ans

# ---------- ОПТИМИЗАЦИЯ ЗАПРОСА ЧЕРЕЗ LLM ----------
async def optimize_query(query, profile=None):
    """Генерирует 3 коротких поисковых запроса через LLM с учётом контекста."""
    context_prompt = ""
    if profile:
        levels = []
        for level in ['level_2', 'level_3']:
            if profile.get(level):
                levels.extend(profile[level][-5:])
        if levels:
            context_prompt = "Контекст предыдущего диалога:\n" + "\n".join(levels) + "\n\n"

    prompt = (
        f"{context_prompt}"
        "Преврати следующий запрос в 3 КОРОТКИХ ПОИСКОВЫХ ЗАПРОСА (3-5 слов, только ключевые слова). "
        "Убери все лишние слова: 'найди', 'пожалуйста', 'помоги', 'мне', 'лучшие', 'скажи' и т.п. "
        "Оставь только суть: объект, характеристика, год (если он важен). "
        "Если запрос про рейтинг или сравнение – обязательно добавь слова 'рейтинг', 'обзор' или 'сравнение'.\n"
        "Раздели варианты символом '|'.\n"
        f"Запрос: {query}"
    )
    messages = [
        {"role": "system", "content": "Ты — эксперт по поисковой оптимизации."},
        {"role": "user", "content": prompt}
    ]
    try:
        result, err = await ask_deepseek(messages, max_tokens=100, model=MODEL_DEFAULT)
        if err or not result:
            return [query]
        variants = [v.strip() for v in result.split('|') if v.strip()]
        return variants[:3] if len(variants) >= 2 else [query]
    except Exception as e:
        logger.warning(f"Ошибка оптимизации запроса: {e}")
        return [query]

# ---------- ПОИСКОВЫЕ ФУНКЦИИ ----------
async def search_apiserpent_async(query, num=SEARCH_RESULTS_NUM):
    if not APISERPENT_API_KEY: return []
    session = await get_http_session()
    try:
        logger.info(f"🔍 APISerpent: [HIDDEN]")
        async with session.get(
            "https://apiserpent.com/api/search",
            params={"q": query, "engine": SEARCH_ENGINE, "num": num},
            headers={"X-API-Key": APISERPENT_API_KEY},
            timeout=20
        ) as r:
            if r.status != 200: return []
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
                        "snippet": str(x.get("snippet", ""))[:200],
                        "link": str(x.get("url", x.get("link", "#")))[:120]
                    })
            return out
    except Exception as e:
        logger.warning(f"APISerpent ошибка: {e}")
        return []

async def search_duckduckgo_async(query):
    session = await get_http_session()
    try:
        logger.info(f"🦆 DuckDuckGo: [HIDDEN]")
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

# ---------- ГЕНЕРАЦИЯ ОТВЕТА ----------
async def generate_response(uid, user_message, history, profile, retry_count=0):
    try:
        return await asyncio.wait_for(
            _generate_response_internal(uid, user_message, history, profile, retry_count),
            timeout=40
        )
    except asyncio.TimeoutError:
        return "⏰ Превышено время ожидания. Попробуйте позже.", False

async def _generate_response_internal(uid, user_message, history, profile, retry_count):
    ctx = build_profile_context(profile)
    cached = get_cached(user_message)
    if cached:
        all_results = cached
    else:
        # Генерация запросов через LLM (с учётом контекста)
        variants = await optimize_query(user_message, profile)
        logger.info(f"🔍 Варианты: {len(variants)}")
        tasks = [search_apiserpent_async(v, SEARCH_RESULTS_NUM) for v in variants]
        results_list = await asyncio.gather(*tasks)
        all_results = []
        seen = set()
        for res_list in results_list:
            if not res_list: continue
            for res in res_list:
                link = res.get('link')
                if link and link not in seen:
                    seen.add(link)
                    all_results.append(res)
        # Если APISerpent не дал результатов, пробуем DuckDuckGo
        if not all_results:
            logger.info("🔄 APISerpent пуст, пробуем DuckDuckGo")
            duck_results = await search_duckduckgo_async(variants[0] if variants else user_message)
            for res in duck_results:
                key = (res.get('title','')+res.get('snippet',''))[:100]
                if key not in seen:
                    seen.add(key)
                    all_results.append(res)
        logger.info(f"📊 Результатов: {len(all_results)}")
        set_cache(user_message, all_results)

    if not all_results:
        # Fallback: пробуем ответить из знаний модели
        sp_knowledge = {
            "role": "system",
            "content": (
                f"{CORE_SYSTEM_RULE}\n"
                f"Сегодня: {get_current_date()}.\n"
                f"Контекст: {ctx}\n\n"
                "Поиск в интернете не дал результатов. "
                "Если у тебя есть общие знания по теме, дай предположительный ответ с пометкой «Предположительно». "
                "Укажи, что это не факт, и поставь низкую уверенность (например, 20%). "
                "Если знаний нет – скажи «Я не знаю»."
            )
        }
        ans, err = await ask_deepseek([sp_knowledge] + history, max_tokens=MAX_TOKENS_ANSWER)
        if err:
            return "⚠️ Ошибка API.", False
        final_ans = finalize_answer(ans, get_current_date())
        return f"🧠 из модели (поиск пуст)\n\n{final_ans}", True

    # Ранжирование результатов
    keywords = [w for w in user_message.split() if len(w)>3 and w not in STOP_WORDS]
    scored = assess_relevance(all_results, keywords)
    for r in all_results:
        r['year'] = extract_year_from_text(r.get('title','') + ' ' + r.get('snippet','')) or 0
        r['price'] = extract_price_from_text(r.get('snippet',''))
    all_results.sort(key=lambda r: (
        (5 if is_official_link(r.get('link','')) else 0) +
        (3 if r.get('price') else 0) +
        (2 if abs(r.get('year',0) - now().year) <= 1 else 0) +
        sum(1 for kw in keywords if kw in (r.get('title','') + ' ' + r.get('snippet','')).lower())
    ), reverse=True)

    top_results = all_results[:TOP_RESULTS_SHOW]
    stext = ""
    for i, r in enumerate(top_results, 1):
        is_off = "⭐ " if is_official_link(r.get('link','')) else ""
        price_note = f" ({r['price']} руб.)" if r.get('price') else ""
        year_note = f" ({r['year']})" if r.get('year') else ""
        link_html = f"🔗 <a href=\"{r['link']}\">Источник</a>" if r.get('link') and r['link'] != '#' else ""
        stext += f"{i}. {is_off}**{r['title']}**{year_note}{price_note}\n   {r['snippet'][:180]}\n   {link_html}\n\n"

    sp = {
        "role": "system",
        "content": (
            f"{CORE_SYSTEM_RULE}\n"
            f"Сегодня: {get_current_date()}.\n"
            f"Контекст: {ctx}\n\n"
            f"Найденные данные:\n{stext}\n"
            "На основе этих данных дай ответ в виде отдельных блоков с эмодзи.\n"
            "Каждый результат оформи как:\n"
            "<b>Название модели</b>\n"
            "💰 Цена\n"
            "📟 Чипсет / ОС\n"
            "💾 Память\n"
            "📺 Особенности\n"
            "🔗 <a href=\"ссылка\">Источник</a>\n\n"
            "В конце обязательно укажи: 📅 Дата: ... и Уверенность: XX%."
        )
    }

    ans, err = await ask_deepseek([sp] + history, max_tokens=MAX_TOKENS_ANSWER)
    if err:
        return f"⚠️ Ошибка API: {err}", False

    final_ans = finalize_answer(ans, get_current_date())
    return f"🌐 из интернета\n\n{final_ans}", True

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
    await safe_reply(update, "👋 Привет! Я — честный ассистент с доступом в интернет. Задавай вопросы!\n\n🛡 Принципы: не врать, указывать источники, дату и уверенность.\n📋 Команды: /profile, /memory, /stats, /forget, /restore")

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
RATE_LIMIT, RATE_WINDOW = 3, 10
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

    # Локальная классификация (без LLM)
    simple_patterns = [r'^привет', r'^как дела', r'^спасибо', r'^ты кто', r'^что умеешь', r'^напиши код', r'^напиши стих', r'^помоги']
    needs_search = not any(re.search(pat, user_message.lower()) for pat in simple_patterns)

    if not needs_search:
        sp = {"role": "system", "content": f"{CORE_SYSTEM_RULE}\nКонтекст: {build_profile_context(profile)}"}
        ans, err = await ask_deepseek([sp] + history, max_tokens=400)
        if not err and ans:
            final_ans = finalize_answer(ans, get_current_date())
            history.append({"role": "assistant", "content": ans, "timestamp": now().strftime("%Y-%m-%d %H:%M:%S")})
            await save_memory(uid, history)
            await safe_reply(update, f"🧠 из модели\n\n{final_ans}")
        else:
            await safe_reply(update, "⚠️ Ошибка при ответе.")
    else:
        answer, should_save = await generate_response(uid, user_message, history, profile)
        if should_save == "need_retry":
            context.user_data["pending_retry_query"] = user_message
            keyboard = [[
                InlineKeyboardButton("✅ Да, переформулировать", callback_data="retry_yes"),
                InlineKeyboardButton("❌ Нет", callback_data="retry_no")
            ]]
            await safe_reply(update, answer, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            if should_save and isinstance(answer, str) and len(answer) > 10:
                clean_answer = re.sub(r'<[^>]+>', '', answer)
                history.append({"role": "assistant", "content": clean_answer, "timestamp": now().strftime("%Y-%m-%d %H:%M:%S")})
                await save_memory(uid, history)
            await safe_reply(update, answer)

# ---------- КНОПКИ ----------
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data
    if data == "retry_yes":
        original_query = context.user_data.get("pending_retry_query")
        if not original_query:
            await query.edit_message_text("❌ Запрос потерян.")
            return
        history = load_memory(user_id)
        profile = load_profile(user_id)
        new_query = await reformulate_query(original_query)
        if history and history[-1]["role"] == "user":
            history[-1]["content"] = new_query
            history[-1]["timestamp"] = now().strftime("%Y-%m-%d %H:%M:%S")
        else:
            history.append({"role": "user", "content": new_query, "timestamp": now().strftime("%Y-%m-%d %H:%M:%S")})
        answer, should_save = await generate_response(user_id, new_query, history, profile, retry_count=1)
        if should_save == "need_retry":
            keyboard = [[
                InlineKeyboardButton("✅ Попробовать ещё раз", callback_data="retry_yes"),
                InlineKeyboardButton("❌ Нет, хватит", callback_data="retry_no")
            ]]
            await query.edit_message_text(answer, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            if should_save and isinstance(answer, str) and len(answer) > 10:
                clean_answer = re.sub(r'<[^>]+>', '', answer)
                history.append({"role": "assistant", "content": clean_answer, "timestamp": now().strftime("%Y-%m-%d %H:%M:%S")})
                await save_memory(user_id, history)
            await query.edit_message_text(answer, parse_mode='HTML')
            context.user_data.pop("pending_retry_query", None)
    elif data == "retry_no":
        await query.edit_message_text("❌ Хорошо, отменяю повторный поиск.")
        context.user_data.pop("pending_retry_query", None)

async def reformulate_query(query):
    if not re.search(r'\b20[2-9][0-9]\b', query):
        query += f" {now().year}"
    return query

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
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)
    logger.info("✅ БОТ ЗАПУЩЕН (финальная версия, LLM-оптимизация, дата/уверенность/ссылки)")
    app.run_polling()

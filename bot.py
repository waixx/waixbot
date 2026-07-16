# ============================================================
#  BroWaix Bot — автоматический поиск (без кнопок выбора)
#  = сравнение с локальной памятью + 3 запроса к APISerpent
#  + 1 запрос к DuckDuckGo, оценка, датирование, честность
# ============================================================
import logging, os, json, sys, re, asyncio, aiohttp, shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()])
logger = logging.getLogger(__name__)
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
def get_current_weekday(): return now().strftime("%A")

MODEL_DEFAULT = os.getenv("MODEL_DEFAULT", "deepseek-v4-flash")
MODEL_FALLBACK = os.getenv("MODEL_FALLBACK", "deepseek-v4-pro")
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
SEARCH_ENGINE = os.getenv("SEARCH_ENGINE", "google")
SEARCH_RESULTS_NUM = int(os.getenv("SEARCH_RESULTS_NUM", "35"))
SEARCH_THRESHOLD = int(os.getenv("SEARCH_THRESHOLD", "3"))
MODEL_TEMPERATURE = float(os.getenv("MODEL_TEMPERATURE", "0.1"))
MAX_RETRY_ATTEMPTS = 3

# ---------- УСИЛЕННЫЙ СИСТЕМНЫЙ ПРОМПТ ----------
CORE_SYSTEM_RULE = (
    "Ты — честный ассистент. Отвечай строго по данным. "
    "Если данных нет — честно скажи «Я не знаю» и предложи уточнить запрос. "
    "Если делаешь предположение — обязательно начинай с «Предположительно» и укажи, что это не факт. "
    "Указывай источники (ссылки), дату информации, оценку уверенности (0–100%). "
    "Никогда не выдумывай факты, даже если они кажутся логичными."
)

# ---------- ПАМЯТЬ ----------
LEVEL_1, LEVEL_2, LEVEL_3, LEVEL_4, LEVEL_5 = (
    {'max_history':80,'keep_recent':20},
    {'compress_interval':40,'compress_to':50},
    {'compress_interval':200,'compress_to':100},
    {'compress_interval':1000,'compress_to':200},
    {'compress_interval':10000,'compress_to':500}
)
PEAK_HOURS = [(9,12),(14,18)]
def is_peak_hour(): return any(s <= now().hour < e for s,e in PEAK_HOURS)
def get_peak_status(): return "⚠️ Сейчас пиковые часы DeepSeek" if is_peak_hour() else "✅ Непиковые часы"

if not TELEGRAM_TOKEN or not DEEPSEEK_API_KEY:
    logger.error("Токены не заданы"); sys.exit(1)
if not APISERPENT_API_KEY:
    logger.warning("APISERPENT_API_KEY не задан — APISerpent недоступен")

DATA_DIR, BACKUP_DIR = "data", "data/backups"
os.makedirs(DATA_DIR, exist_ok=True); os.makedirs(BACKUP_DIR, exist_ok=True)
def memory_path(uid): return os.path.join(DATA_DIR, f"memory_{uid}.json")
def profile_path(uid): return os.path.join(DATA_DIR, f"profile_{uid}.json")
def counter_path(uid): return os.path.join(DATA_DIR, f"counter_{uid}.json")

_http_session = None
user_locks = {}
rate_lock = asyncio.Lock()
request_count = {}
search_cache = {}
CACHE_TTL = 3600

def get_user_lock(uid): return user_locks.setdefault(uid, asyncio.Lock())

async def get_http_session():
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=50, limit_per_host=20, keepalive_timeout=30),
            timeout=aiohttp.ClientTimeout(total=60, connect=10, sock_read=30)
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
    except Exception as ex:
        logger.error(f"Ошибка записи {filename}: {ex}")
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
        for old in backups[:-10]: os.remove(os.path.join(BACKUP_DIR, old))
        return True
    except Exception as ex:
        logger.error(f"Бэкап ошибка: {ex}")
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
        except Exception as ex:
            logger.error(f"Восстановление ошибка: {ex}")
            return False

# ---------- СЖАТИЕ ----------
STOP_WORDS = {'это','так','вот','ну','просто','очень'}
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
    old = items[:200]; out = []
    for i in range(0, len(old), 4):
        out.append("[архив] " + " | ".join(x[:20] for x in old[i:i+4]))
    res = out + items[-target:]
    return res[-target:] if len(res) > target + 10 else res

def compress_history(history):
    if len(history) <= LEVEL_1['max_history']: return history
    recent = history[-LEVEL_1['keep_recent']:]
    old = history[:-LEVEL_1['keep_recent']]
    summary = []
    for m in old[-10:]:
        r, c = m.get("role",""), m.get("content","")
        if r == "user": summary.append(f"Q: {extract_key_points(c,50)}")
        elif r == "assistant": summary.append(f"A: {extract_key_points(c,50)}")
    if summary:
        return [{"role":"system","content":"📚 История (сжато):\n" + "\n".join(summary[-5:])}] + recent
    return recent

def load_memory(uid): return compress_history(load_memory_raw(uid))

def _update_level(uid, messages, key, cfg, extractor, ext_len, ts_fmt):
    profile = load_profile(uid); profile.setdefault(key, [])
    ts = now().strftime(ts_fmt)
    for m in messages[-cfg['compress_interval']:]:
        r, c = m.get("role",""), m.get("content","")
        if r == "user": profile[key].append(f"[{ts}] Q: {extractor(c, ext_len)}")
        elif r == "assistant": profile[key].append(f"[{ts}] A: {extractor(c, ext_len)}")
    if key == "level_5" and len(profile["level_5"]) > cfg['compress_to'] + 100:
        profile["level_5"] = compress_ultra_old(profile["level_5"][:200], 50) + profile["level_5"][200:]
    if len(profile[key]) > cfg['compress_to']:
        profile[key] = profile[key][-cfg['compress_to']:]
    save_profile(uid, profile, backup=False)

async def _save_memory_impl(uid, history, backup):
    try:
        if len(history) > LEVEL_1['max_history']:
            old = history[:-LEVEL_1['keep_recent']]
            if old:
                _update_level(uid, old, "level_2", LEVEL_2, extract_key_points, 30, "%d.%m")
                p = load_profile(uid)
                if len(p.get("level_2",[])) >= LEVEL_2['compress_to']:
                    _update_level(uid, old, "level_3", LEVEL_3, extract_aggressive, 25, "%m.%d")
                if len(p.get("level_3",[])) >= LEVEL_3['compress_to']:
                    _update_level(uid, old, "level_4", LEVEL_4, extract_aggressive, 20, "%m.%d")
                if len(p.get("level_4",[])) >= LEVEL_4['compress_to']:
                    _update_level(uid, old, "level_5", LEVEL_5, extract_ultra, 15, "%y.%m")
        if not atomic_write(memory_path(uid), compress_history(history)):
            return False
        if backup: create_backup(uid, "memory")
        cnt = load_counter(uid) + 1
        save_counter(uid, cnt)
        if cnt % 10 == 0: create_backup(uid, "profile")
        return True
    except Exception as ex:
        logger.error(f"Сохранение ошибка {uid}: {ex}")
        return False

async def save_memory(uid, history, backup=True, lock_held=False):
    if lock_held: return await _save_memory_impl(uid, history, backup)
    async with get_user_lock(uid): return await _save_memory_impl(uid, history, backup)

# ---------- ПОИСК ПО ПАМЯТИ ----------
def parse_time_query(tq):
    try:
        parts = tq.split(":")
        if len(parts) >= 2: return int(parts[0]), int(parts[1])
    except: pass
    return None, None
def search_by_time(uid, tq):
    qh, qm = parse_time_query(tq); res = []
    if qh is None: return res
    for m in load_memory_raw(uid):
        ts = m.get("timestamp","")
        if not ts: continue
        try:
            mt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            if mt.hour == qh and mt.minute == qm: res.append(m)
        except:
            if tq in ts: res.append(m)
    return res

def parse_date_query(query):
    q = query.lower().strip(); n = now()
    if q == "сегодня": return n.strftime("%Y-%m-%d")
    if q == "вчера": return (n - timedelta(days=1)).strftime("%Y-%m-%d")
    if q == "завтра": return (n + timedelta(days=1)).strftime("%Y-%m-%d")
    for pat in [r'(\d{2})\.(\d{2})\.(\d{4})', r'(\d{2})\.(\d{2})', r'(\d{4})-(\d{2})-(\d{2})']:
        m = re.search(pat, query)
        if m:
            g = m.groups()
            if len(g) == 3:
                if '.' in query: d, mo, y = g
                else: y, mo, d = g
                return f"{y}-{mo}-{d}"
            if len(g) == 2:
                d, mo = g
                return f"{n.year}-{mo}-{d}"
    return None

def search_by_date(uid, date_str):
    return [m for m in load_memory_raw(uid) if m.get("timestamp","").startswith(date_str)]

def search_in_pyramid(uid, query):
    profile, q, res = load_profile(uid), query.lower(), []
    for m in load_memory_raw(uid)[-40:]:
        c = m.get("content","")
        if q in c.lower():
            role = "👤" if m.get("role") == "user" else "🤖"
            ts = m.get("timestamp","")
            res.append(f"{role}{(' ['+ts+']') if ts else ''} {extract_key_points(c,80)}")
    for lvl, em in [("level_2","📚"), ("level_3","📖"), ("level_4","📕"), ("level_5","📗")]:
        for item in profile.get(lvl, []):
            if q in item.lower(): res.append(f"{em} {item}")
    return res[:15]

# ---------- ВСПОМОГАТЕЛЬНЫЕ ----------
def extract_year_from_text(text):
    m = re.search(r'\b(20[2-9][0-9])\b', text)
    return int(m.group(1)) if m else None

def is_official_link(link):
    return any(dom in link.lower() for dom in ['python.org','docs.python.org','peps.python.org','github.com/python','pypi.org'])

def assess_relevance(results, keywords):
    if not results:
        return []
    scored = []
    for res in results:
        text = (res.get('title','') + ' ' + res.get('snippet','')).lower()
        score = sum(1 for kw in keywords if kw in text)
        scored.append({**res, 'score': score})
    scored.sort(key=lambda x: x['score'], reverse=True)
    return scored

def get_cached(query):
    key = query.lower().strip()
    if key in search_cache and (datetime.now() - search_cache[key]['time']).seconds < CACHE_TTL:
        logger.info(f"💾 Кэш для '{query}'")
        return search_cache[key]['data']
    return None

def set_cache(query, data):
    key = query.lower().strip()
    search_cache[key] = {'data': data, 'time': datetime.now()}
    if len(search_cache) > 100:
        oldest = min(search_cache.keys(), key=lambda k: search_cache[k]['time'])
        del search_cache[oldest]

def highlight_contradictions(text):
    """Находит предложения с маркерами противоречий и выделяет их жирным (Markdown)."""
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

def finalize_answer(ans, current_date):
    """Постобработка: проверка противоречий, уверенности, предположений, ссылок."""
    highlighted, has_contradiction = highlight_contradictions(ans)
    if has_contradiction:
        ans = highlighted
        ans = f"⚠️ Внимание: в ответе есть возможные противоречия (выделены жирным).\n\n{ans}"

    conf = re.search(r'Уверенность:\s*(\d+)%', ans)
    if conf:
        if int(conf.group(1)) < 70:
            ans = f"⚠️ Модель оценивает уверенность всего на {conf.group(1)}%.\n\n{ans}"
    else:
        ans += "\n\n⚠️ Оценка уверенности не указана."

    if 'http' not in ans and len(ans) > 100:
        ans += "\n\n⚠️ Нет ссылок на источники."

    if 'дата' not in ans.lower() and 'уверенность' not in ans.lower():
        ans += f"\n\n⚠️ Дата публикации источников не указана. Проверьте актуальность на {current_date}."

    assumption_markers = ['возможно', 'вероятно', 'похоже', 'скорее всего', 'может быть', 'по всей видимости']
    has_assumption = any(m in ans.lower() for m in assumption_markers)
    if has_assumption and 'предположительно' not in ans.lower() and 'не факт' not in ans.lower():
        ans = "⚠️ В ответе есть предположения, но они не помечены. Будьте внимательны.\n\n" + ans

    return ans

# ---------- ПОИСКОВЫЕ ФУНКЦИИ ----------
async def optimize_query(query):
    has_date = re.search(r'\b(20[2-9][0-9]|\d{1,2}\.\d{1,2}\.\d{4}|\bянв|фев|мар|апр|май|июн|июл|авг|сен|окт|ноя|дек|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)', query, re.I)
    if not has_date:
        query = f"{query} {now().strftime('%B')} {now().year}"
    prompt = (
        "Преврати запрос в 3 коротких поисковых запроса (только суть, ключевые слова). "
        "Раздели варианты символом '|'.\n"
        f"Запрос: {query}"
    )
    messages = [
        {"role": "system", "content": "Ты — эксперт по поисковой оптимизации."},
        {"role": "user", "content": prompt}
    ]
    try:
        result, err = await ask_deepseek(messages, max_tokens=100, model="deepseek-v4-flash")
        if err or not result:
            return [query]
        variants = [v.strip() for v in result.split('|') if v.strip()]
        return variants[:3] if len(variants) >= 2 else [query]
    except:
        return [query]

async def ask_deepseek(messages, retries=3, max_tokens=None, model=None):
    session = await get_http_session()
    use_model = model or MODEL_DEFAULT
    for attempt in range(retries):
        try:
            payload = {"model": use_model, "messages": messages, "temperature": MODEL_TEMPERATURE}
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
                        return (c, None) if c else (None, "empty")
                    return None, "invalid_response"
                if resp.status == 429:
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                if resp.status in (400, 404) and use_model != MODEL_FALLBACK:
                    logger.warning(f"Модель {use_model} недоступна, пробуем {MODEL_FALLBACK}")
                    use_model = MODEL_FALLBACK
                    continue
                return None, f"http_{resp.status}"
        except Exception as ex:
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            return None, type(ex).__name__.lower()
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

async def search_apiserpent_async(query, num=SEARCH_RESULTS_NUM):
    if not APISERPENT_API_KEY:
        return []
    session = await get_http_session()
    for attempt in range(2):
        try:
            logger.info(f"🔍 APISerpent попытка {attempt+1}: {query}")
            async with session.get(
                "https://apiserpent.com/api/search",
                params={"q": query, "engine": SEARCH_ENGINE, "num": num},
                headers={"X-API-Key": APISERPENT_API_KEY},
                timeout=45
            ) as r:
                response_text = await r.text()
                logger.info(f"APISerpent статус: {r.status}")
                if r.status == 401:
                    logger.error("Неверный ключ APISerpent")
                    return None
                if r.status == 402:
                    logger.error("Баланс APISerpent закончился")
                    return None
                if r.status != 200:
                    logger.error(f"APISerpent ошибка {r.status}")
                    if attempt == 1:
                        return []
                    continue
                data = await r.json()
                results = []
                if isinstance(data.get("results"), dict):
                    results = data["results"].get("organic", [])
                elif "organic_results" in data:
                    results = data["organic_results"]
                elif "organic" in data:
                    results = data["organic"]
                elif "items" in data:
                    results = data["items"]
                else:
                    for key in data:
                        if isinstance(data[key], list) and data[key] and isinstance(data[key][0], dict):
                            results = data[key]
                            break
                out = []
                for x in results[:num]:
                    if isinstance(x, dict):
                        out.append({
                            "title": str(x.get("title", x.get("name", "Без названия")))[:150],
                            "snippet": str(x.get("snippet", x.get("description", "Нет описания")))[:250],
                            "link": str(x.get("url", x.get("link", "#")))[:150],
                            "source": "apiserpent"
                        })
                logger.info(f"APISerpent вернул {len(out)} результатов")
                return out
        except asyncio.TimeoutError:
            logger.error(f"Таймаут APISerpent (попытка {attempt+1})")
            if attempt == 1:
                return []
            await asyncio.sleep(2)
        except Exception as ex:
            logger.error(f"Ошибка APISerpent: {ex}")
            if attempt == 1:
                return []
            await asyncio.sleep(2)
    return []

async def search_duckduckgo_async(query):
    session = await get_http_session()
    try:
        logger.info(f"🦆 DuckDuckGo запрос: {query}")
        async with session.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=10
        ) as r:
            if r.status != 200:
                return []
            data = await r.json()
            results = []
            if data.get('AbstractText'):
                results.append({
                    "title": "DuckDuckGo (факт)",
                    "snippet": data['AbstractText'][:500],
                    "link": data.get('AbstractURL', ''),
                    "source": "duckduckgo"
                })
            for topic in data.get('RelatedTopics', []):
                if 'Text' in topic:
                    results.append({
                        "title": "DuckDuckGo (связанное)",
                        "snippet": topic['Text'][:300],
                        "link": topic.get('FirstURL', ''),
                        "source": "duckduckgo"
                    })
            logger.info(f"🦆 DuckDuckGo вернул {len(results)} результатов")
            return results
    except:
        return []

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
    words = user_message.split()
    if len(words) <= 2 or any(ext in user_message for ext in ['.com','.ru','.org','.net','.io']):
        return ("🔍 Короткий запрос. Уточните, что именно интересует."), False

    # Проверка кэша
    cached = get_cached(user_message)
    if cached:
        all_results = cached
    else:
        variants = await optimize_query(user_message)
        logger.info(f"🔍 Варианты: {variants}")
        tasks = [search_apiserpent_async(v, num=SEARCH_RESULTS_NUM) for v in variants]
        task_duck = search_duckduckgo_async(variants[0] if variants else user_message)
        results_apiserpent = await asyncio.gather(*tasks)
        results_duck = await task_duck

        all_results = []
        seen = set()
        for res_list in results_apiserpent:
            if not res_list:
                continue
            for res in res_list:
                link = res.get('link')
                if link and link not in seen:
                    seen.add(link)
                    all_results.append(res)
        for res in results_duck:
            key = (res.get('title','') + res.get('snippet',''))[:100]
            if key not in seen:
                seen.add(key)
                all_results.append(res)
        logger.info(f"📊 Всего уникальных результатов: {len(all_results)}")
        set_cache(user_message, all_results)

    # Оценка релевантности
    keywords = [w for w in user_message.split() if len(w) > 3 and w not in ['как','для','при','на','в','и','не']]
    scored = assess_relevance(all_results, keywords)
    if len(all_results) < 3 and retry_count < MAX_RETRY_ATTEMPTS:
        return (f"🔍 **Искал:** `{user_message}`\n\n❌ Мало релевантных результатов. Хотите уточнить запрос?"), "need_retry"

    if not all_results:
        if retry_count < MAX_RETRY_ATTEMPTS:
            return (f"🔍 **Искал:** `{user_message}`\n\n❌ Поиск не дал результатов. Попробовать с другой формулировкой?"), "need_retry"
        else:
            return (f"🔍 **Искал:** `{user_message}`\n\n❌ Поиск не дал результатов после {MAX_RETRY_ATTEMPTS} попыток."), False

    # Ранжирование
    for res in all_results:
        res['year'] = extract_year_from_text(res.get('title','') + ' ' + res.get('snippet','')) or 0
    current_year = now().year
    all_results.sort(key=lambda r: (
        10 if is_official_link(r.get('link','')) else 0 +
        5 if abs(r.get('year',0) - current_year) <= 1 else 0 +
        sum(1 for kw in keywords if kw in (r.get('title','') + ' ' + r.get('snippet','')).lower())
    ), reverse=True)
    top_results = all_results[:8]

    stext = f"🔍 **Искал:** `{user_message}`\n\n📊 Найдено {len(top_results)} результатов:\n\n"
    for i, r in enumerate(top_results, 1):
        is_off = is_official_link(r.get('link',''))
        mark = " ⭐ (официальный)" if is_off else ""
        year_note = f" (год: {r['year']})" if r.get('year') else ""
        stext += f"{i}. **{r['title']}**{mark}{year_note}\n   {r['snippet'][:200]}\n   🔗 {r['link']}\n\n"

    total_snippet_len = sum(len(r.get('snippet', '')) for r in all_results)
    max_tokens_limit = 300 if total_snippet_len < 200 else None

    sp = {
        "role": "system",
        "content": (
            f"{CORE_SYSTEM_RULE}\n"
            f"Сегодня: {get_current_date()} {get_current_time()}.\n"
            f"Вопрос: \"{user_message}\"\n"
            f"Контекст: {ctx}\n\n"
            f"Найденные данные:\n{stext}\n\n"
            "ИНСТРУКЦИЯ:\n"
            "1. Отвечай ТОЛЬКО по данным.\n"
            "2. Предположения помечай.\n"
            "3. Указывай ссылки и дату.\n"
            "4. В конце пиши «Уверенность: XX%»."
        )
    }
    history.append({"role": "user", "content": user_message})
    ans, err = await ask_deepseek([sp] + history, max_tokens=max_tokens_limit)
    if err:
        return f"⚠️ {analyze_error(err)}", False

    ans = finalize_answer(ans, get_current_date())
    return f"🔍 **Искал в интернете:** `{user_message}`\n\n{ans}", True

def analyze_error(e):
    s = str(e).lower()
    if "timeout" in s: return "⏰ Таймаут."
    if "connection" in s: return "🌐 Проблемы с соединением."
    if "429" in str(e): return "📊 Слишком много запросов."
    if "401" in str(e): return "🔑 Ошибка API ключа."
    if "400" in str(e): return "⚠️ Некорректный запрос."
    if "404" in str(e): return "🔍 Модель не найдена."
    return f"⚠️ Ошибка: {str(e)[:150]}"

# ---------- ОТПРАВКА С HTML ----------
async def safe_reply(update: Update, text: str, reply_markup=None):
    msg = update.effective_message
    if msg is None: return

    def markdown_to_html(t):
        t = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', t)
        t = re.sub(r'\_\_([^_]+)\_\_', r'<i>\1</i>', t)
        t = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', t)
        t = re.sub(r'^#{1,3}\s+(.+)$', r'📌 <b>\1</b>', t, flags=re.MULTILINE)
        t = re.sub(r'^[-*]\s+(.+)', r'• \1', t, flags=re.MULTILINE)
        t = re.sub(r'^\d+\.\s+(.+)', r'• \1', t, flags=re.MULTILINE)
        lines = t.split('\n')
        new_lines = []
        in_table = False
        for line in lines:
            if '|' in line and '---' not in line:
                parts = [p.strip() for p in line.split('|') if p.strip()]
                if parts and not in_table:
                    new_lines.append('📋 <b>Список:</b>')
                    in_table = True
                new_lines.append('• ' + ' – '.join(parts))
            else:
                if in_table and line.strip() == '':
                    in_table = False
                new_lines.append(line)
        t = '\n'.join(new_lines)
        emoji_map = {
            'дата': '📅', 'релиз': '🚀', 'выход': '📅',
            'производительность': '⚡',
            'совместимость': '⚠️', 'изменения': '⚠️',
            'источники': '🔗', 'ссылки': '🔗',
            'цена': '💰', 'стоимость': '💰', 'топ': '🏆'
        }
        for word, emoji in emoji_map.items():
            t = re.sub(rf'(?i)({word}\s*:)', f'{emoji} \\1', t)
        t = re.sub(r'\n{3,}', '\n\n', t)
        return t.strip()

    if len(text) > 20 and not text.startswith(('/', '❌', '✅', '⏰', '📅')):
        text = markdown_to_html(text)

    for attempt in range(3):
        try:
            if len(text) > 4096:
                for i in range(0, len(text), 4096):
                    await msg.reply_text(text[i:i+4096], parse_mode='HTML', reply_markup=reply_markup)
            else:
                await msg.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
            return
        except Exception as ex:
            if attempt == 2:
                logger.error(f"safe_reply не смог: {ex}")
                try:
                    await msg.reply_text(text, reply_markup=reply_markup)
                except:
                    pass
            else:
                await asyncio.sleep(1)

def is_allowed(uid):
    return not ALLOWED_USERS_LIST or uid in ALLOWED_USERS_LIST

# ---------- КОМАНДЫ ----------
async def start(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await safe_reply(update, "❌ Доступ запрещён.")
        return
    name = load_profile(uid).get("name", "друг")
    await safe_reply(update,
        f"👋 Привет, {name}!\n\n📅 Сегодня: {get_current_date()} {get_current_time()}\n\n{get_peak_status()}\n\n"
        "🛡 Мой принцип: никогда не врать.\n\n"
        "📋 Команды: /profile /stats /memory /forget /restore")

async def profile_command(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await safe_reply(update, "❌ Доступ запрещён.")
        return
    p = load_profile(uid)
    if not p:
        await safe_reply(update, "📭 Я пока ничего не знаю о тебе.")
        return
    lines = ["🧠 **Память:**"]
    for k, lab in {'level_2':'📚 ур.2','level_3':'📖 ур.3','level_4':'📕 ур.4','level_5':'📗 ур.5'}.items():
        lines.append(f"• {lab}: {len(p.get(k, []))} пунктов")
    lines.append(f"• 📝 активная история: {len(load_memory_raw(uid))} сообщений")
    lines.append("\n👤 **Личное:**")
    exclude = {'updated','level_2','level_3','level_4','level_5'}
    personal_keys = [k for k in p.keys() if k not in exclude]
    if personal_keys:
        for k in personal_keys:
            lines.append(f"• {k}: {p[k]}")
    else:
        lines.append("• Пока ничего не запомнил")
    lines.append(f"\n⏰ {get_peak_status()}\n🔄 Обновлено: {p.get('updated','неизвестно')}")
    await safe_reply(update, "\n".join(lines))

async def memory_command(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await safe_reply(update, "❌ Доступ запрещён.")
        return
    if not context.args:
        await safe_reply(update, "🔍 Поиск: `/memory что искать`")
        return
    query = ' '.join(context.args)
    ds = parse_date_query(query)
    if ds:
        res = search_by_date(uid, ds)
        if res:
            lines = [f"📅 За {query}:"] + [f"{m.get('timestamp','')} {m.get('role','')}: {m.get('content','')[:100]}" for m in res[:10]]
            if len(res) > 10:
                lines.append(f"... и ещё {len(res)-10}")
            await safe_reply(update, "\n".join(lines))
            return
    tm = re.search(r'(\d{1,2}:\d{2}(:\d{2})?)', query)
    if tm:
        res = search_by_time(uid, tm.group(1))
        if res:
            lines = [f"🕐 По времени {tm.group(1)}:"] + [f"{m.get('timestamp','')} {m.get('role','')}: {m.get('content','')[:100]}" for m in res[:5]]
            if len(res) > 5:
                lines.append(f"... и ещё {len(res)-5}")
            await safe_reply(update, "\n".join(lines))
            return
    res = search_in_pyramid(uid, query)
    if not res:
        await safe_reply(update, f"📭 Ничего не найдено: '{query}'")
        return
    lines = [f"🔍 Результаты '{query}':"] + [f"{i}. {r}" for i,r in enumerate(res[:10],1)]
    if len(res) > 10:
        lines.append(f"... и ещё {len(res)-10}")
    await safe_reply(update, "\n".join(lines))

async def stats_command(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await safe_reply(update, "❌ Доступ запрещён.")
        return
    p = load_profile(uid)
    raw = load_memory_raw(uid)
    lines = ["📊 **Статистика:**"]
    lines.append(f"• Обработано сообщений: {load_counter(uid)}")
    lines.append(f"• В активной истории: {len(raw)}")
    total = 0
    for k, lab in {'level_2':'📚 ур.2','level_3':'📖 ур.3','level_4':'📕 ур.4','level_5':'📗 ур.5'}.items():
        c = len(p.get(k, []))
        total += c
        lines.append(f"• {lab}: {c} сжатых пунктов")
    lines.append(f"\n📦 Всего сжатых пунктов: {total}")
    bc = len([f for f in os.listdir(BACKUP_DIR) if f.startswith(f"profile_{uid}_")])
    lines.append(f"💾 Бэкапов профиля: {bc}\n⏰ {get_peak_status()}\n🔄 {p.get('updated','неизвестно')}")
    await safe_reply(update, "\n".join(lines))

async def forget_command(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await safe_reply(update, "❌ Доступ запрещён.")
        return
    async with get_user_lock(uid):
        save_profile(uid, {})
        await save_memory(uid, [], backup=True, lock_held=True)
        save_counter(uid, 0)
    await safe_reply(update, "🧹 Я забыл всё, что знал о тебе!")

async def restore_command(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await safe_reply(update, "❌ Доступ запрещён.")
        return
    pr = await restore_backup(uid, "profile")
    mr = await restore_backup(uid, "memory")
    if pr or mr:
        await safe_reply(update, "✅ Восстановлено!\n" + ("📋 Профиль\n" if pr else "") + ("💬 История" if mr else ""))
    else:
        await safe_reply(update, "❌ Нет бэкапов.")

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
            if not request_count[u]:
                del request_count[u]
        return True

async def clean_request_count():
    while True:
        try:
            await asyncio.sleep(21600)
            async with rate_lock:
                now_ts = datetime.now().timestamp()
                to_delete = [uid for uid, timestamps in request_count.items() if not timestamps or now_ts - timestamps[-1] > 600]
                for uid in to_delete:
                    del request_count[uid]
                if to_delete:
                    logger.debug(f"Очищено {len(to_delete)} неактивных записей")
        except Exception as e:
            logger.error(f"Ошибка в clean_request_count: {e}, перезапуск через 60 сек")
            await asyncio.sleep(60)

async def auto_restore_all_users():
    logger.info("🔄 Проверка данных при старте...")
    backup_files = os.listdir(BACKUP_DIR)
    user_ids = set()
    for fname in backup_files:
        parts = fname.split('_')
        if len(parts) >= 2 and parts[0] in ('profile','memory'):
            try:
                user_ids.add(int(parts[1]))
            except:
                pass
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
            pr = await restore_backup(uid, "profile")
            mr = await restore_backup(uid, "memory")
            if pr or mr:
                logger.info(f"✅ Пользователь {uid} восстановлен")

# ---------- ОБРАБОТЧИК КНОПОК (только для уточнения) ----------
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
        retry_count = context.user_data.get("retry_count", 0) + 1
        await query.edit_message_text(f"🔄 Переформулирую (попытка {retry_count})...")
        new_query = await optimize_query(original_query)
        if isinstance(new_query, list):
            new_query = new_query[0]
        history = load_memory(user_id)
        profile = load_profile(user_id)
        answer, should_save = await generate_response(user_id, new_query, history, profile, retry_count)
        if should_save == "need_retry":
            context.user_data["pending_retry_query"] = original_query
            context.user_data["retry_count"] = retry_count
            keyboard = [[
                InlineKeyboardButton("✅ Попробовать ещё раз", callback_data="retry_yes"),
                InlineKeyboardButton("❌ Нет, хватит", callback_data="retry_no")
            ]]
            await query.edit_message_text(answer, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.edit_message_text(answer, parse_mode='HTML')
            context.user_data.pop("pending_retry_query", None)
            context.user_data.pop("retry_count", None)
        return

    if data == "retry_no":
        await query.edit_message_text("❌ Хорошо, отменяю повторный поиск.")
        context.user_data.pop("pending_retry_query", None)
        context.user_data.pop("retry_count", None)
        return

# ---------- ГЛАВНЫЙ ОБРАБОТЧИК ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.effective_message or not update.effective_message.text:
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

    # Запомни
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

    # Принудительный поиск "бро"
    if user_message.lower().startswith("бро "):
        sq = user_message[4:].strip()
        if not sq:
            await safe_reply(update, "❌ Напиши, что искать.")
            return
        status_msg = await update.effective_message.reply_text("🌐 Ищу информацию...")
        history = load_memory(uid)
        profile = load_profile(uid)
        answer, should_save = await generate_response(uid, sq, history, profile)
        if status_msg:
            try:
                await status_msg.delete()
            except:
                pass
        if should_save == "need_retry":
            context.user_data["pending_retry_query"] = sq
            keyboard = [[
                InlineKeyboardButton("✅ Да, переформулировать", callback_data="retry_yes"),
                InlineKeyboardButton("❌ Нет", callback_data="retry_no")
            ]]
            await safe_reply(update, answer, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await safe_reply(update, answer)
        return

    # Обычный запрос – автоматический поиск (без кнопок выбора)
    history = load_memory(uid)
    profile = load_profile(uid)
    answer, should_save = await generate_response(uid, user_message, history, profile)
    if should_save == "need_retry":
        context.user_data["pending_retry_query"] = user_message
        keyboard = [[
            InlineKeyboardButton("✅ Да, переформулировать", callback_data="retry_yes"),
            InlineKeyboardButton("❌ Нет", callback_data="retry_no")
        ]]
        await safe_reply(update, answer, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await safe_reply(update, answer)

async def error_handler(update, context):
    logger.error(f"Глобальная ошибка: {context.error}")
    if isinstance(update, Update):
        await safe_reply(update, analyze_error(str(context.error)))

async def shutdown_session():
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(auto_restore_all_users())
    loop.create_task(clean_request_count())
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
    logger.info("✅ БОТ ГОТОВ К РАБОТЕ.")
    try:
        app.run_polling()
    except KeyboardInterrupt:
        logger.info("👋 Остановлен")
    finally:
        if _http_session and not _http_session.closed:
            try:
                loop.run_until_complete(shutdown_session())
            except:
                pass
        loop.close()

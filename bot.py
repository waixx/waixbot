# ============================================================
#  BroWaix Bot — ПОЛНАЯ ВЕРСИЯ
#  (память, профили, бэкапы, умный парсинг, бюджет $7–8/мес)
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
from bs4 import BeautifulSoup

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
MODEL_FALLBACK = os.getenv("MODEL_FALLBACK", "deepseek-v4-flash")
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
SEARCH_ENGINE = os.getenv("SEARCH_ENGINE", "google")

# --- ПАРАМЕТРЫ ---
SEARCH_RESULTS_NUM = 10
SEARCH_VARIANTS_COUNT = 1
MODEL_TEMPERATURE = 0.1
CACHE_TTL = 604800
MAX_TOKENS_ANSWER = 2048
MAX_TOKENS_DEEP = 4096
TOP_RESULTS_SHOW = 6
MAX_PAGES_TO_PARSE = 5
MAX_CHARS_PER_PAGE = 1200

# ---------- ПАМЯТЬ ----------
LEVEL_1 = {'max_history': 40, 'keep_recent': 10}
LEVEL_2 = {'compress_interval': 20, 'compress_to': 30}

if not TELEGRAM_TOKEN or not DEEPSEEK_API_KEY or not APISERPENT_API_KEY:
    logger.error("❌ Токены не заданы"); sys.exit(1)

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
                timeout=aiohttp.ClientTimeout(total=60, connect=10, sock_read=50)
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

# ---------- ИНСТРУМЕНТЫ ----------
def extract_year_from_text(text):
    match = re.search(r'\b(20[2-9][0-9])\b', text)
    if match and match.group(1).isdigit():
        return int(match.group(1))
    return None

def extract_price_from_text(text):
    match = re.search(r'([\d\s]+)\s*(?:руб|₽|р\.|рублей|RUB|\$|€)', text, re.I)
    if match:
        price_str = re.sub(r'\s', '', match.group(1))
        if price_str.isdigit():
            return int(price_str)
    return None

def extract_budget_from_query(query):
    match = re.search(r'(?:до|не более|не дороже|max|максимум)\s*([\d\s]+)\s*(?:тыс|руб|₽|р\.|\$|€)', query, re.I)
    if match:
        price_str = re.sub(r'\s', '', match.group(1))
        if price_str.isdigit():
            return int(price_str)
    return None

# ---------- ПОИСК (APISerpent) ----------
async def search_apiserpent(query, num=SEARCH_RESULTS_NUM):
    if not APISERPENT_API_KEY:
        return []
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
                        "link": str(x.get("url", x.get("link", "#")))[:120],
                        "source": str(x.get("source", x.get("domain", "неизвестно")))[:50]
                    })
            return out
    except Exception as e:
        logger.warning(f"APISerpent ошибка: {e}")
        return []

# ---------- УМНЫЙ ПАРСИНГ (только релевантные абзацы) ----------
async def fetch_relevant_content(url, query, max_chars=MAX_CHARS_PER_PAGE):
    """Парсит страницу, оставляет только абзацы, релевантные запросу"""
    try:
        session = await get_http_session()
        async with session.get(url, timeout=15) as response:
            if response.status != 200:
                return None
            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')
            
            for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
                tag.decompose()
            
            content = None
            for selector in ['article', 'main', '.content', '.post', '.entry', '.article', '.entry-content']:
                elem = soup.select_one(selector)
                if elem:
                    content = elem
                    break
            
            if not content:
                content = soup.body
            
            paragraphs = content.find_all(['p', 'li', 'div']) if content else []
            text_blocks = []
            for p in paragraphs:
                text = p.get_text(strip=True)
                if len(text) > 40:
                    text_blocks.append(text)
            
            query_words = query.lower().split()
            scored_blocks = []
            for block in text_blocks:
                score = sum(1 for word in query_words if word in block.lower())
                if score > 0:
                    scored_blocks.append((score, block[:350]))
            
            scored_blocks.sort(key=lambda x: x[0], reverse=True)
            top_blocks = [block for _, block in scored_blocks[:12]]
            
            result = '\n'.join(top_blocks)
            return result[:max_chars] if result else None
    except Exception as e:
        logger.warning(f"Ошибка парсинга {url}: {e}")
        return None

# ---------- КЭШ ----------
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

# ---------- ГЕНЕРАЦИЯ ОТВЕТА ----------
async def generate_response(uid, user_message, history, profile, is_deep=False):
    try:
        return await asyncio.wait_for(
            _generate_response_internal(uid, user_message, history, profile, is_deep),
            timeout=90
        )
    except asyncio.TimeoutError:
        return "⏰ Превышено время ожидания. Попробуйте позже.", False
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return "⚠️ Произошла ошибка. Попробуйте позже.", False

async def _generate_response_internal(uid, user_message, history, profile, is_deep):
    ctx = build_profile_context(profile)
    budget = extract_budget_from_query(user_message)
    budget_note = f" (бюджет до {budget} руб.)" if budget else ""
    
    # 1. Поиск
    cached = get_cached(user_message)
    if cached:
        results = cached
        logger.info("✅ Используем кэш")
    else:
        variants = await generate_search_query(user_message)
        logger.info(f"🔍 Поиск: {variants[0]}")
        results = await search_apiserpent(variants[0])
        if results:
            set_cache(user_message, results)
    
    if not results:
        return generate_local_answer(uid, user_message, history, profile, "Поиск не дал результатов"), False
    
    # 2. Парсинг
    logger.info(f"📖 Парсим статьи...")
    articles = []
    for r in results[:5]:
        content = await fetch_relevant_content(r['link'], user_message)
        if content:
            articles.append({
                'title': r['title'],
                'link': r['link'],
                'snippet': r.get('snippet', ''),
                'content': content
            })
    
    if not articles:
        # Если парсинг не удался — используем сниппеты
        logger.info("⚠️ Парсинг не удался, используем сниппеты")
        for r in results[:5]:
            articles.append({
                'title': r['title'],
                'link': r['link'],
                'snippet': r.get('snippet', ''),
                'content': r.get('snippet', '')
            })
    
    # 3. Формируем данные
    articles_text = ""
    for i, a in enumerate(articles, 1):
        articles_text += f"### Источник {i}: {a['title']}\n"
        articles_text += f"Ссылка: {a['link']}\n"
        articles_text += f"Содержание:\n{a['content']}\n\n"
    
    # 4. Промпт
    system_prompt = (
        "Ты — экспертный аналитик. Проанализируй статьи и дай ПОЛНЫЙ, СТРУКТУРИРОВАННЫЙ ответ.\n\n"
        "=== ПРАВИЛА ===\n"
        "1. Используй ТОЛЬКО информацию из статей.\n"
        "2. Структура:\n"
        "   📌 краткий вывод\n"
        "   🏆 список ключевых моментов (с фактами и цифрами)\n"
        "   📊 сравнение (если есть)\n"
        "   ✅ рекомендация\n"
        "   ⚠️ важные нюансы\n"
        "   🔗 ссылки на источники\n"
        "3. Если источники противоречат — скажи об этом.\n"
        "4. Укажи дату и уверенность."
    )
    
    sp = {
        "role": "system",
        "content": (
            f"{system_prompt}\n"
            f"Сегодня: {get_current_date()}\n"
            f"Контекст: {ctx}{budget_note}\n\n"
            f"Статьи:\n{articles_text}"
        )
    }
    
    # 5. Запрос к DeepSeek
    ans, err = await ask_deepseek([sp] + history, max_tokens=MAX_TOKENS_DEEP if is_deep else MAX_TOKENS_ANSWER)
    if err:
        logger.warning(f"DeepSeek ошибка: {err}")
        return generate_manual_answer(articles, user_message), False
    
    # 6. Обработка ответа
    if '📅 Дата:' not in ans:
        ans += f"\n\n📅 Дата: {get_current_date()}"
    if 'Уверенность:' not in ans:
        ans += "\nУверенность: 85% (на основе найденных данных)"
    
    return ans, True

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
def generate_manual_answer(articles, user_message):
    if not articles:
        return "🔍 По вашему запросу ничего не найдено."
    
    answer = "📌 **Краткий вывод**\n\n"
    answer += f"Я проанализировал {len(articles)} источников по запросу: *{user_message[:80]}*\n\n"
    answer += "### 🏆 Ключевые моменты\n\n"
    
    for i, a in enumerate(articles[:3], 1):
        answer += f"{i}. **{a['title']}**\n"
        answer += f"   {a['content'][:200]}...\n"
        answer += f"   🔗 {a['link']}\n\n"
    
    answer += f"📅 Дата: {get_current_date()}\n"
    answer += "Уверенность: 70% (на основе найденных данных)"
    return answer

def generate_local_answer(uid, user_message, history, profile, reason):
    sp = {
        "role": "system",
        "content": (
            f"⚠️ {reason}. Используй свои знания, но помечай 'Предположительно'.\n"
            f"Сегодня: {get_current_date()}\n"
            f"Уверенность не выше 25%."
        )
    }
    ans, err = asyncio.run(ask_deepseek([sp] + history, max_tokens=MAX_TOKENS_ANSWER))
    if err:
        return "⚠️ Ошибка. Попробуйте позже."
    return f"🧠 {ans}"

async def generate_search_query(query):
    stop = {'найди','пожалуйста','помоги','мне','лучшие','скажи','расскажи','покажи','найти'}
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
            payload = {
                "model": model,
                "messages": messages,
                "temperature": MODEL_TEMPERATURE,
                "user": str(uid) if 'uid' in locals() else "anonymous"
            }
            if max_tokens:
                payload["max_tokens"] = max_tokens
            
            async with session.post(
                f"{DEEPSEEK_API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                json=payload,
                timeout=50
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("choices"):
                        c = data["choices"][0].get("message", {}).get("content")
                        if c and len(c.strip()) > 10:
                            return (c, None)
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
            logger.warning(f"⏰ Таймаут (попытка {attempt+1})")
            await asyncio.sleep(2)
            continue
        except Exception as ex:
            logger.warning(f"Ошибка: {ex}")
            await asyncio.sleep(1)
    return None, "max_retries"

# ---------- КОМАНДЫ ----------
async def start(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid): return
    await safe_reply(update, "👋 Привет! Я — ассистент с доступом в интернет.\n"
                            "🔍 Ищу, анализирую и даю структурированный ответ.\n"
                            "📋 Команды: /profile, /memory, /stats, /forget, /restore, /deep [запрос]")

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
    logger.info("✅ БОТ ЗАПУЩЕН (полная версия, умный парсинг, бюджет $7–8/мес)")
    app.run_polling()
# ================================================================
#  BroWaix Bot — ФИНАЛЬНАЯ ВЕРСИЯ (Playwright устанавливается из кода)
#  - Playwright устанавливается автоматически при первом запуске
#  - 40 ссылок, фильтрация, топ-7 по контенту
#  - Статус-сообщения + таймер в Telegram
#  - Вечная память, бэкапы, честность
#  - Никаких Dockerfile — только код
# ================================================================

import logging
import os
import json
import sys
import re
import asyncio
import aiohttp
import shutil
import weakref
import hashlib
import time
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
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
SERPER_API_KEY = os.getenv("SERPER_API_KEY")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0") or 0)
ALLOWED_USERS_LIST = [int(x.strip()) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()]
if ADMIN_USER_ID and ADMIN_USER_ID not in ALLOWED_USERS_LIST:
    ALLOWED_USERS_LIST.append(ADMIN_USER_ID)

TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow") or "UTC")
def now(): return datetime.now(TZ)
def get_current_date(): return now().strftime("%d.%m.%Y")

# ---------- ПАРАМЕТРЫ ----------
MODEL_DEFAULT = os.getenv("MODEL_DEFAULT", "deepseek-v4-flash")
MODEL_FALLBACK = os.getenv("MODEL_FALLBACK", "deepseek-v4-pro")
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
SEARCH_ENGINE = os.getenv("SEARCH_ENGINE", "google")

SEARCH_RESULTS_NUM = 40
TOP_RESULTS_SHOW = 7
MODEL_TEMPERATURE = 0.1
MAX_RETRY_ATTEMPTS = 2
CACHE_TTL = 172800
MAX_TOKENS_ANSWER = 1500
MAX_HTML_LEN = 5000
CACHE_CLEANUP_INTERVAL = 3600

LEVEL_1 = {'max_history': 20, 'keep_recent': 5}
LEVEL_2 = {'compress_interval': 20, 'compress_to': 30}

if not TELEGRAM_TOKEN or not DEEPSEEK_API_KEY:
    logger.error("❌ TELEGRAM_TOKEN или DEEPSEEK_API_KEY не заданы")
    sys.exit(1)

DATA_DIR, BACKUP_DIR = "data", "data/backups"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

def memory_path(uid): return os.path.join(DATA_DIR, f"memory_{uid}.json")
def profile_path(uid): return os.path.join(DATA_DIR, f"profile_{uid}.json")
def counter_path(uid): return os.path.join(DATA_DIR, f"counter_{uid}.json")

# ---------- ГЛОБАЛЬНОЕ СОСТОЯНИЕ ----------
_http_session = None
_session_lock = asyncio.Lock()
user_locks = weakref.WeakValueDictionary()
rate_lock = asyncio.Lock()
request_count = {}
search_cache = {}
html_cache = {}

def get_user_lock(uid): return user_locks.setdefault(uid, asyncio.Lock())

async def get_http_session():
    global _http_session
    async with _session_lock:
        if _http_session is None or _http_session.closed:
            _http_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=50, limit_per_host=10),
                timeout=aiohttp.ClientTimeout(total=90, connect=15, sock_read=45)
            )
        return _http_session

async def cleanup_http_session():
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()

# ---------- АВТОУСТАНОВКА PLAYWRIGHT ----------
def ensure_playwright_installed():
    """Устанавливает Playwright и браузер Chromium при первом запуске"""
    try:
        import playwright
        logger.info("✅ Playwright уже установлен")
    except ImportError:
        logger.info("📦 Playwright не найден, устанавливаю...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright"])
            subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
            logger.info("✅ Playwright и Chromium успешно установлены")
        except Exception as e:
            logger.error(f"❌ Ошибка установки Playwright: {e}")
            logger.warning("⚠️ Бот продолжит работу без Playwright (SPA-сайты могут не читаться)")

# ---------- ФАЙЛЫ ----------
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
    except Exception:
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
    except Exception:
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
            return True
        except Exception:
            return False

# ---------- СЖАТИЕ ----------
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

# ---------- ФИЛЬТРАЦИЯ КЛЮЧЕВЫМИ СЛОВАМИ ----------
def extract_year_from_text(text):
    if not isinstance(text, str):
        return None
    match = re.search(r'\b(20[2-9][0-9])\b', text)
    return int(match.group(1)) if match else None

def assess_relevance(results, query):
    if not results or not isinstance(results, list):
        return []
    query_year = None
    year_match = re.search(r'\b(20[2-9][0-9])\b', query)
    if year_match:
        query_year = int(year_match.group(1))
    requires_year = any(word in query.lower() for word in ['новинк','последн','свеж','актуальн','этот год','сейчас','сегодня'])
    stop_words = {'найди','пожалуйста','помоги','мне','лучшие','скажи','расскажи','покажи','найти','бро','что','как','где'}
    keywords = [w.lower() for w in re.sub(r'[^\w\s]', '', query).split()
                if w.lower() not in stop_words and len(w) > 3]
    scored = []
    for res in results:
        if not isinstance(res, dict):
            continue
        text = (res.get('title', '') or '') + ' ' + (res.get('snippet', '') or '')
        text_lower = text.lower()
        link = res.get('link', '').lower()
        keyword_score = sum(3 for kw in keywords if kw in text_lower)
        domain_score = 0
        for kw in keywords:
            if len(kw) > 3 and kw in link:
                domain_score += 4
        if any(zone in link for zone in ['.gov','.edu','.org','wikipedia.org']):
            domain_score += 5
        spam = ['ozon','wildberries','aliexpress','avito','amazon','ebay','taobao','sbermegamarket']
        if any(s in link for s in spam):
            domain_score -= 8
        year = extract_year_from_text(text)
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
    relevant = [r for r in scored if r['score'] > 0]
    relevant.sort(key=lambda x: x['score'], reverse=True)
    return relevant

def normalize_query(query):
    if not isinstance(query, str):
        return ""
    normalized = re.sub(r'[^\w\s]', '', query.lower())
    normalized = ' '.join([w for w in normalized.split() if w not in STOP_WORDS and len(w)>2])
    return normalized[:100]

def get_cached(query):
    if any(w in query.lower() for w in ['погода','курс']):
        return None
    norm_key = normalize_query(query)
    if norm_key in search_cache and (datetime.now() - search_cache[norm_key]['time']).total_seconds() < CACHE_TTL:
        logger.info("✅ Cache HIT (поиск)")
        return search_cache[norm_key]['data']
    return None

def set_cache(query, data):
    if any(w in query.lower() for w in ['погода','курс']):
        return
    norm_key = normalize_query(query)
    search_cache[norm_key] = {'data': data, 'time': datetime.now()}
    if len(search_cache) > 100:
        oldest = min(search_cache.keys(), key=lambda k: search_cache[k]['time'])
        del search_cache[oldest]

# ---------- УНИВЕРСАЛЬНАЯ ЗАГРУЗКА (Playwright + резерв) ----------
async def fetch_content(url: str) -> str:
    """
    Загружает содержимое страницы: сначала через Playwright (рендерит SPA),
    если не удалось — через прямой HTTP-запрос.
    """
    now_time = datetime.now()
    if url in html_cache and html_cache[url]["expires"] > now_time:
        logger.info(f"✅ Cache HIT для {url[:50]}...")
        return html_cache[url]["text"]

    result = ""

    # --- 1. Playwright (рендеринг SPA) ---
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            try:
                await page.wait_for_selector("body", timeout=5000)
            except:
                pass
            html = await page.content()
            await browser.close()

            text = re.sub(r'<[^>]+>', ' ', html)
            text = re.sub(r'\s+', ' ', text).strip()
            if len(text) > 500:
                result = text[:MAX_HTML_LEN]
                logger.info(f"✅ Playwright спарсил {url[:50]}, {len(result)} символов")
            else:
                logger.warning(f"⚠️ Playwright не дал контента для {url[:50]}")
    except Exception as e:
        logger.warning(f"Playwright ошибка для {url}: {e}")

    # --- 2. Резерв: прямой HTTP-запрос ---
    if not result:
        logger.info(f"🔄 Пробуем прямой HTTP для {url[:50]}")
        session = await get_http_session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Referer": "https://www.google.com/",
        }
        try:
            async with session.get(url, headers=headers, timeout=20) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    text = re.sub(r'<[^>]+>', ' ', html)
                    text = re.sub(r'\s+', ' ', text).strip()
                    if len(text) > 500:
                        result = text[:MAX_HTML_LEN]
                        logger.info(f"✅ Прямой HTML для {url[:50]}, {len(result)} символов")
        except Exception as e:
            logger.warning(f"Прямой HTTP не удался для {url}: {e}")

    # Кэшируем результат, если получен
    if result:
        html_cache[url] = {
            "text": result,
            "expires": now_time + timedelta(seconds=CACHE_TTL)
        }
        if len(html_cache) > 200:
            oldest = min(html_cache.keys(), key=lambda k: html_cache[k]["expires"])
            del html_cache[oldest]
        return result

    logger.warning(f"❌ Не удалось получить контент для {url}")
    return ""

# ---------- ПАРАЛЛЕЛЬНАЯ ЗАГРУЗКА ----------
async def fetch_multiple_pages(links, max_pages=40, top_k=7) -> list:
    if not links:
        return []

    async def fetch_one(url, semaphore):
        async with semaphore:
            await asyncio.sleep(0.3)
            try:
                content = await fetch_content(url)
                if content:
                    return {"url": url, "text": content}
                return None
            except Exception as e:
                logger.warning(f"Ошибка загрузки {url}: {e}")
                return None

    semaphore = asyncio.Semaphore(5)
    tasks = [fetch_one(url, semaphore) for url in links[:max_pages]]
    results = await asyncio.gather(*tasks)

    valid = [r for r in results if r is not None]
    valid.sort(key=lambda x: len(x["text"]), reverse=True)
    top = valid[:top_k]

    logger.info(f"📊 Загружено {len(links)} ссылок, отобрано {len(top)} с контентом")
    return top

# ---------- ПОИСК ----------
async def search_apiserpent_async(query, num=SEARCH_RESULTS_NUM):
    if not APISERPENT_API_KEY:
        return []
    session = await get_http_session()
    try:
        logger.info(f"🔍 APISerpent: {query[:50]}...")
        params = {"q": query, "engine": SEARCH_ENGINE, "num": num}
        async with session.get(
            "https://apiserpent.com/api/search",
            params=params,
            headers={"X-API-Key": APISERPENT_API_KEY},
            timeout=45
        ) as r:
            if r.status != 200:
                logger.warning(f"APISerpent статус: {r.status}")
                return []
            data = await r.json()
            results = []
            organic = data.get("results", {}).get("organic", []) if isinstance(data.get("results"), dict) else data.get("organic_results", [])
            for x in organic[:num]:
                if isinstance(x, dict):
                    results.append({
                        "title": str(x.get("title", ""))[:120],
                        "snippet": str(x.get("snippet", ""))[:300],
                        "link": str(x.get("url", x.get("link", "#")))[:120]
                    })
            return results
    except asyncio.TimeoutError:
        logger.error("APISerpent таймаут (45 сек)")
        return []
    except Exception as e:
        logger.warning(f"APISerpent ошибка: {type(e).__name__}: {str(e)}")
        return []

async def search_serper_async(query):
    if not SERPER_API_KEY:
        return []
    session = await get_http_session()
    try:
        logger.info(f"🔍 Serper: {query[:50]}...")
        async with session.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": SEARCH_RESULTS_NUM},
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            timeout=15
        ) as r:
            if r.status != 200:
                logger.warning(f"Serper статус: {r.status}")
                return []
            data = await r.json()
            results = []
            for item in data.get("organic", [])[:SEARCH_RESULTS_NUM]:
                results.append({
                    "title": item.get("title", "")[:120],
                    "snippet": item.get("snippet", "")[:300],
                    "link": item.get("link", "#")[:120]
                })
            return results
    except Exception as e:
        logger.warning(f"Serper ошибка: {type(e).__name__}: {str(e)}")
        return []

async def search_primary(query):
    results = await search_apiserpent_async(query)
    if results:
        return results
    logger.info("🔄 APISerpent пуст, пробуем Serper")
    return await search_serper_async(query)

# ---------- СТАТУС-СООБЩЕНИЯ ----------
async def send_status(update: Update, text: str, start_time: float, status_msg=None):
    elapsed = int(time.time() - start_time)
    full_text = f"{text} ⏱ {elapsed} сек"
    if status_msg is None:
        return await update.effective_message.reply_text(full_text)
    else:
        try:
            await status_msg.edit_text(full_text)
        except Exception:
            pass
        return status_msg

# ---------- ГЕНЕРАЦИЯ ОТВЕТА ----------
async def generate_response(uid, user_message, history, profile, status_msg, update, start_time):
    try:
        return await asyncio.wait_for(
            _generate_response_internal(uid, user_message, history, profile, status_msg, update, start_time),
            timeout=90
        )
    except asyncio.TimeoutError:
        return "⏰ Превышено время ожидания. Попробуйте позже.", False

async def _generate_response_internal(uid, user_message, history, profile, status_msg, update, start_time):
    ctx = build_profile_context(profile)

    if len(user_message.split()) < 3:
        return "👋 Привет! Напишите конкретный вопрос, я поищу информацию в интернете.", False

    status_msg = await send_status(update, "🔍 Ищу в интернете...", start_time, status_msg)

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
        return ("🔍 По вашему запросу в интернете ничего не найдено.\n"
                "Попробуйте перефразировать запрос.", False)

    scored = assess_relevance(all_results, user_message)
    if not scored:
        all_links = [r['link'] for r in all_results[:40]]
    else:
        all_links = [r['link'] for r in scored[:40]]

    status_msg = await send_status(update, f"📥 Загружаю {len(all_links)} страниц...", start_time, status_msg)

    top_pages = await fetch_multiple_pages(all_links, max_pages=40, top_k=7)

    if top_pages:
        full_texts = [f"--- ИСТОЧНИК: {p['url']} ---\n{p['text']}" for p in top_pages]
        status_msg = await send_status(update, f"🧠 Анализирую {len(top_pages)} страниц через DeepSeek...", start_time, status_msg)
    else:
        logger.info("⚠️ Не найдено страниц с контентом, используем сниппеты")
        if scored:
            full_texts = [
                f"--- ИСТОЧНИК (сниппет): {r['link']} ---\n"
                f"Заголовок: {r.get('title','')}\n"
                f"Описание: {r.get('snippet','')}"
                for r in scored[:TOP_RESULTS_SHOW]
            ]
        else:
            full_texts = [
                f"--- ИСТОЧНИК (сниппет): {r['link']} ---\n"
                f"Заголовок: {r.get('title','')}\n"
                f"Описание: {r.get('snippet','')}"
                for r in all_results[:TOP_RESULTS_SHOW]
            ]
        status_msg = await send_status(update, "🧠 Анализирую сниппеты...", start_time, status_msg)

    stext = "\n\n".join(full_texts) if full_texts else "Нет данных"

    sp = {
        "role": "system",
        "content": (
            "Ты — честный ассистент. Ты получил содержимое веб-страниц (HTML или сниппеты).\n"
            "Твоя задача — извлечь из этих данных ТОЛЬКО фактологическую информацию.\n"
            "Правила:\n"
            "1. Если HTML — игнорируй теги, скрипты, стили, рекламу.\n"
            "2. Если сниппет — используй как есть.\n"
            "3. Сосредоточься на фактах, цифрах, моделях, ценах, характеристиках.\n"
            "4. Если нет прямого ответа — напиши: 'В предоставленных данных не обнаружено прямого ответа. Вот что удалось найти:'\n"
            "5. Структурируй ответ: краткий вывод, затем блоки с заголовками, списки, таблицы, эмодзи.\n"
            "6. Каждый факт сопровождай ссылкой на источник.\n"
            "7. НЕ придумывай, НЕ додумывай — только то, что есть в данных.\n"
            f"Запрос пользователя: {user_message}\n"
            f"Сегодня: {get_current_date()}\n"
            f"Контекст: {ctx}\n\n"
            f"ДАННЫЕ:\n{stext}\n\n"
            "В конце укажи: 📅 Дата, Уверенность: XX%."
        )
    }

    ans, err = await ask_deepseek([sp] + history, max_tokens=MAX_TOKENS_ANSWER)
    if err or ans is None:
        logger.warning(f"DeepSeek не ответил: {err}")
        return generate_answer_from_snippets(scored or all_results, user_message), True

    final_ans = ans
    if len(final_ans) < 50 or 'http' not in final_ans:
        final_ans = generate_answer_from_snippets(scored or all_results, user_message)
    else:
        if '📅 Дата:' not in final_ans:
            final_ans += f"\n\n📅 Дата: {get_current_date()}"
        if 'Уверенность:' not in final_ans:
            final_ans += "\nУверенность: 90%"

    return f"🌐 из интернета\n\n{final_ans}", True

def generate_answer_from_snippets(results, user_message):
    if not results:
        return "❌ В интернете ничего не найдено."
    relevant = [r for r in results if r.get('score',0) > 0][:TOP_RESULTS_SHOW]
    if not relevant:
        links = [r.get('link') for r in results if r.get('link') and r['link']!='#']
        if links:
            answer = "🔍 **Найденные ссылки:**\n\n"
            for link in links[:TOP_RESULTS_SHOW]:
                answer += f"• {link}\n"
            answer += f"\n📅 Дата: {get_current_date()}\nУверенность: 70%"
            return answer
        else:
            return "❌ Не удалось получить результаты."
    answer = f"🔍 Результаты поиска\n\n"
    for i, r in enumerate(relevant, 1):
        year_note = f" ({r.get('year')})" if r.get('year') else ""
        answer += f"{i}. **{r.get('title','Без названия')}**{year_note}\n"
        snippet = r.get('snippet','Нет описания')[:200]
        answer += f"   {snippet}\n"
        if r.get('link') and r['link']!='#':
            answer += f"   🔗 <a href='{r['link']}'>Источник</a>\n"
        answer += "\n"
    answer += f"📅 Дата: {get_current_date()}\nУверенность: 85%"
    return answer

# ---------- ВСПОМОГАТЕЛЬНЫЕ ----------
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
        if k in ("updated","level_2"):
            continue
        if isinstance(v, str):
            parts.append(f"{k}: {v[:40]}")
    return ". ".join(parts)[:150]

async def ask_deepseek(messages, retries=2, max_tokens=None, model=MODEL_DEFAULT):
    session = await get_http_session()
    for attempt in range(retries):
        try:
            payload = {"model": model, "messages": messages, "temperature": MODEL_TEMPERATURE}
            if max_tokens:
                payload["max_tokens"] = max_tokens
            async with session.post(
                f"{DEEPSEEK_API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
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
    await safe_reply(update, "👋 Привет! Я — честный ассистент с интернетом.\n"
        "🛡 Принципы: использую ТОЛЬКО данные из интернета, никогда не вру.\n"
        "📋 Команды: /profile, /memory, /stats, /forget, /restore")

async def profile_command(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid): return
    p = load_profile(uid)
    if not p:
        await safe_reply(update, "📭 Я пока ничего не знаю о тебе.")
        return
    lines = ["🧠 **Память:**", f"• 📝 активная история: {len(load_memory_raw(uid))} сообщений"]
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
    if len(res) > 10:
        lines.append(f"... и ещё {len(res)-10}")
    await safe_reply(update, "\n".join(lines))

def search_in_pyramid(uid, query):
    profile = load_profile(uid)
    q = query.lower()
    res = []
    for m in load_memory_raw(uid)[-30:]:
        if not isinstance(m, dict):
            continue
        c = m.get("content", "")
        if q in c.lower():
            role = "👤" if m.get("role")=="user" else "🤖"
            ts = m.get("timestamp","")
            res.append(f"{role}{(' ['+ts+']') if ts else ''} {extract_key_points(c,80)}")
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
        await safe_reply(update, "✅ Восстановлено!\n" + ("📋 Профиль\n" if pr else "") + ("💬 История" if mr else ""))
    else:
        await safe_reply(update, "❌ Нет бэкапов.")

# ---------- RATE LIMIT ----------
RATE_LIMIT, RATE_WINDOW = 5, 10
async def check_rate_limit(uid):
    async with rate_lock:
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
    def markdown_to_html(t):
        t = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', t)
        t = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', t)
        return t.strip()
    if len(text) > 20 and not text.startswith(('/', '❌', '✅')):
        text = markdown_to_html(text)
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

# ---------- ОБРАБОТЧИК ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.effective_user or not update.effective_message or not update.effective_message.text:
            return
        uid = update.effective_user.id
        if not is_allowed(uid):
            return
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

        start_time = time.time()
        status_msg = await send_status(update, "🔍 Запускаю поиск...", start_time, None)

        answer, should_save = await generate_response(uid, user_message, history, profile, status_msg, update, start_time)

        try:
            await status_msg.delete()
        except Exception:
            pass

        if should_save and isinstance(answer, str) and len(answer) > 10:
            clean_answer = re.sub(r'<[^>]+>', '', answer)
            history.append({"role": "assistant", "content": clean_answer, "timestamp": now().strftime("%Y-%m-%d %H:%M:%S")})
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
            expired_html = [k for k, v in html_cache.items() if v['expires'] <= now_time]
            for k in expired_html:
                del html_cache[k]
            if len(html_cache) > 200:
                oldest = sorted(html_cache.keys(), key=lambda k: html_cache[k]['expires'])[:50]
                for k in oldest:
                    del html_cache[k]
            logger.debug(f"🧹 Кэши очищены")
        except asyncio.CancelledError:
            logger.info("🧹 Задача cleanup_caches корректно завершена")
            break
        except Exception as ex:
            logger.error(f"Ошибка cleanup_caches: {ex}")

async def error_handler(update, context):
    logger.error(f"Ошибка: {context.error}")

# ---------- ВОССТАНОВЛЕНИЕ ----------
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

async def post_init(application):
    global _session_lock, _rate_lock
    _session_lock = asyncio.Lock()
    _rate_lock = asyncio.Lock()
    asyncio.create_task(cleanup_caches())

def main():
    # Автоустановка Playwright при первом запуске
    ensure_playwright_installed()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(auto_restore_all_users())
    except Exception as e:
        logger.error(f"Ошибка восстановления: {e}")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(CommandHandler("restore", restore_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("🚀 БОТ ЗАПУЩЕН (Playwright устанавливается из кода)")
    try:
        app.run_polling()
    finally:
        loop.run_until_complete(cleanup_http_session())

if __name__ == "__main__":
    main()

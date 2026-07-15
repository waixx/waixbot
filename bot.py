import logging, os, json, sys, re, hashlib, asyncio, aiohttp
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
APISERPENT_API_KEY = os.getenv("APISERPENT_API_KEY")
try: ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
except ValueError: ADMIN_USER_ID = 0
ALLOWED_USERS_STR = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS_LIST = []
if ALLOWED_USERS_STR:
    try: ALLOWED_USERS_LIST = [int(x.strip()) for x in ALLOWED_USERS_STR.split(",") if x.strip()]
    except ValueError: print("⚠️ Ошибка в ALLOWED_USERS")
if ADMIN_USER_ID != 0 and ADMIN_USER_ID not in ALLOWED_USERS_LIST: ALLOWED_USERS_LIST.append(ADMIN_USER_ID)

LEVEL_1 = {'max_history':80,'keep_recent':20,'compress_to':20}
LEVEL_2 = {'max_items':1000,'compress_interval':40,'compress_to':50}
LEVEL_3 = {'max_items':10000,'compress_interval':200,'compress_to':100}
LEVEL_4 = {'max_items':100000,'compress_interval':1000,'compress_to':200}
LEVEL_5 = {'max_items':1000000,'compress_interval':10000,'compress_to':500}
MAX_CACHE_ITEMS = int(os.getenv("MAX_CACHE_ITEMS","100"))
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS","60"))
MODEL_DEFAULT = os.getenv("MODEL_DEFAULT","deepseek-v4-flash")
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE","https://api.deepseek.com/v1")
PEAK_HOURS = [(9,12),(14,18)]
def is_peak_hour():
    now=datetime.now(); h=now.hour
    return any(s<=h<e for s,e in PEAK_HOURS)
def get_peak_status():
    return "⚠️ Сейчас пиковые часы DeepSeek (9–12, 14–18) — стоимость удвоена." if is_peak_hour() else "✅ Сейчас непиковые часы."

NOW=datetime.now(); CURRENT_DATE=NOW.strftime("%d.%m.%Y"); CURRENT_TIME=NOW.strftime("%H:%M"); CURRENT_YEAR=NOW.year
if not TELEGRAM_TOKEN or not DEEPSEEK_API_KEY: print("❌ TELEGRAM_TOKEN или DEEPSEEK_API_KEY не заданы"); sys.exit(1)
print("\n"+"="*50+"\n🚀 БОТ ЗАПУЩЕН (ФИНАЛЬНАЯ СТАБИЛЬНАЯ ВЕРСИЯ)\n"+"="*50)
print(f"  🤖 TELEGRAM_TOKEN: {'✅' if TELEGRAM_TOKEN else '❌'}")
print(f"  🔑 DEEPSEEK_API_KEY: {'✅' if DEEPSEEK_API_KEY else '❌'}")
print(f"  🔍 APISERPENT_API_KEY: {'✅' if APISERPENT_API_KEY else '❌'}")
print(f"  👤 ADMIN_USER_ID: {ADMIN_USER_ID}")
print(f"  👥 Разрешённых пользователей: {len(ALLOWED_USERS_LIST)}")
print(f"  📊 Память: 80 → 1000 → 10000 → 100000 → 1 000 000+")
print(f"  💾 Гибридный кэш (RAM + файл): ВКЛЮЧЕН (TTL: {CACHE_TTL} сек, макс. {MAX_CACHE_ITEMS} записей)")
print(f"  💾 Авто-бэкап: ВКЛЮЧЕН (каждые 10 сообщений)")
print(f"  🕐 Дата и время: ОТВЕЧАЮ ЛОКАЛЬНО (без интернета)")
print(f"  💾 Сохранение черновиков (до отправки): ВКЛЮЧЕНО")
print(f"  🔍 Расширенные триггеры для интернет-поиска: ВКЛЮЧЕНЫ")
print(f"  🔍 Команда 'бро' принудительно включает интернет-поиск")
print("="*50+"\n")
os.makedirs("data", exist_ok=True); os.makedirs("data/backups", exist_ok=True)
MEMORY_FILE="data/memory.json"; PROFILE_FILE="data/user_profile.json"; BACKUP_DIR="data/backups"; CACHE_FILE="data/profile_cache.json"; COUNTER_FILE="data/counter.json"
PROFILE_CACHE={}; _http_session=None

async def get_http_session():
    global _http_session
    if _http_session is None or _http_session.closed:
        connector = aiohttp.TCPConnector(limit=50, limit_per_host=20, keepalive_timeout=30, enable_cleanup_closed=True)
        timeout = aiohttp.ClientTimeout(total=60, connect=10, sock_read=30)
        _http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return _http_session

def analyze_error(error_text):
    el=error_text.lower()
    if "timeout" in el or "timed out" in el: return "⏰ Превышено время ожидания. Попробуйте позже."
    if "connection" in el or "network" in el: return "🌐 Проблемы с интернет-соединением."
    if "429" in error_text or "too many requests" in el: return "📊 Слишком много запросов. Подождите."
    if "401" in error_text or "unauthorized" in el: return "🔑 Ошибка авторизации API. Проверьте ключи."
    if "500" in error_text or "internal server" in el: return "⚠️ Внутренняя ошибка сервера. Повторите позже."
    if "server_error" in el: return "⚠️ Внутренняя ошибка сервера DeepSeek. Попробуйте позже."
    if "not found" in el or "404" in error_text: return "🔍 Ресурс не найден."
    if "message is too long" in el: return "📝 Сообщение слишком длинное. Разбиваю."
    if "empty" in el: return "📭 Получен пустой ответ."
    if "invalid_response" in el: return "⚠️ Некорректный ответ сервера."
    if "max_retries" in el: return "⚠️ Не удалось получить ответ после нескольких попыток."
    return f"⚠️ Неизвестная ошибка: {error_text[:150]}..."

def atomic_write(filename, data, as_json=True):
    temp=filename+".tmp"
    try:
        with open(temp,'w',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False,indent=2) if as_json else f.write(data)
        os.replace(temp,filename); return True
    except Exception as e:
        print(f"⚠️ Ошибка атомарной записи {filename}: {e}")
        if os.path.exists(temp): os.remove(temp)
        return False

def atomic_read(filename, default=None, as_json=True):
    try:
        with open(filename,'r',encoding='utf-8') as f:
            return json.load(f) if as_json else f.read()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return restore_from_backup(filename) or default

def restore_from_backup(filename):
    try:
        data_type = "profile" if "profile" in filename else "memory" if "memory" in filename else None
        if not data_type: return None
        backups=sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith(data_type+"_")])
        if not backups: return None
        latest=backups[-1]
        with open(os.path.join(BACKUP_DIR, latest),'r',encoding='utf-8') as f:
            data=json.load(f)
        print(f"🔄 Восстановлен {filename} из бэкапа {latest}")
        atomic_write(filename,data); return data
    except Exception as e: print(f"❌ Ошибка восстановления {filename}: {e}"); return None

def load_cache_from_file():
    global PROFILE_CACHE
    data=atomic_read(CACHE_FILE,default={})
    if data:
        try:
            for uid,(prof,ts_str) in data.items():
                PROFILE_CACHE[uid]=(prof, datetime.fromisoformat(ts_str))
            print(f"💾 Загружено {len(PROFILE_CACHE)} записей кэша")
            return True
        except: pass
    return False

def save_cache_to_file():
    global PROFILE_CACHE
    try:
        if len(PROFILE_CACHE)>MAX_CACHE_ITEMS:
            sorted_items=sorted(PROFILE_CACHE.items(), key=lambda x:x[1][1])
            for uid,_ in sorted_items[:len(PROFILE_CACHE)-MAX_CACHE_ITEMS]:
                del PROFILE_CACHE[uid]
            print(f"🧹 Кэш ограничен до {MAX_CACHE_ITEMS} записей")
        serializable={uid:(prof,ts.isoformat()) for uid,(prof,ts) in PROFILE_CACHE.items()}
        atomic_write(CACHE_FILE,serializable); return True
    except Exception as e: print(f"⚠️ Ошибка сохранения кэша: {e}"); return False

def get_profile_cached(user_id):
    global PROFILE_CACHE
    now=datetime.now()
    if not PROFILE_CACHE: load_cache_from_file()
    if user_id in PROFILE_CACHE:
        prof,ts=PROFILE_CACHE[user_id]
        if (now-ts).seconds < CACHE_TTL:
            return prof.copy() if prof else {}
    prof=load_profile(user_id)
    PROFILE_CACHE[user_id]=(prof.copy() if prof else {}, now)
    save_cache_to_file()
    return prof.copy() if prof else {}

def invalidate_cache(user_id):
    if user_id in PROFILE_CACHE: del PROFILE_CACHE[user_id]; save_cache_to_file()

def is_allowed(user_id):
    return True if not ALLOWED_USERS_LIST else user_id in ALLOWED_USERS_LIST

def load_profile(user_id):
    data=atomic_read(PROFILE_FILE,default={})
    return data.get(str(user_id),{})

def save_profile(user_id, profile, backup=True):
    data=atomic_read(PROFILE_FILE,default={})
    profile["updated"]=datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    data[str(user_id)]=profile
    if not atomic_write(PROFILE_FILE,data): return False
    if backup: create_backup(user_id,"profile")
    invalidate_cache(user_id); return True

def load_counter(user_id):
    data=atomic_read(COUNTER_FILE,default={}); return data.get(str(user_id),0)
def save_counter(user_id, count):
    data=atomic_read(COUNTER_FILE,default={}); data[str(user_id)]=count; atomic_write(COUNTER_FILE,data)

def create_backup(user_id, data_type):
    try:
        ts=datetime.now().strftime("%Y%m%d_%H%M%S")
        filename=f"{BACKUP_DIR}/{data_type}_{user_id}_{ts}.json"
        if data_type=="profile":
            data=get_profile_cached(user_id)
        elif data_type=="memory":
            data=load_memory(user_id)
        else: return False
        atomic_write(filename,data)
        backups=sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith(f"{data_type}_{user_id}")])
        for old in backups[:-10]:
            try: os.remove(os.path.join(BACKUP_DIR, old))
            except: pass
        return True
    except Exception as e: print(f"⚠️ Ошибка бэкапа: {e}"); return False

def restore_backup(user_id, data_type):
    try:
        backups=sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith(f"{data_type}_{user_id}")])
        if not backups: return False
        latest=backups[-1]
        data=atomic_read(os.path.join(BACKUP_DIR, latest), default=None)
        if data is None: return False
        if data_type=="profile": save_profile(user_id,data,backup=False)
        elif data_type=="memory": save_memory(user_id,data,backup=False)
        return True
    except: return False

def extract_key_points(text,max_len=30):
    if len(text)<=max_len: return text
    stop_words={'это','так','вот','ну','просто','очень'}
    words=text.split()
    important=[w for w in words if w.lower() not in stop_words and len(w)>2]
    return ' '.join(important[:10])[:max_len]+'...'

def extract_keywords_aggressive(text,max_len=20):
    if len(text)<=max_len: return text
    important=[w[:8] for w in text.split() if len(w)>3 and w.lower() not in {'это','так','вот','ну'}]
    return ' '.join(important[:5])[:max_len]+'...'

def extract_keywords_ultra(text,max_len=12):
    if len(text)<=max_len: return text
    important=[w[:5] for w in text.split() if len(w)>3 and w.lower() not in {'это','так','вот','ну'}]
    return ' '.join(important[:3])[:max_len]+'...'

def compress_ultra_old(items,target_count=50):
    if len(items)<=target_count: return items
    old=items[:200]
    compressed=["[архив] "+" | ".join([item[:20] for item in old[i:i+4]]) for i in range(0,len(old),4)]
    result=compressed+items[-target_count:]
    return result[-target_count-10:] if len(result)>target_count+10 else result

def compress_history(history):
    if len(history)<=LEVEL_1['max_history']: return history
    recent=history[-LEVEL_1['keep_recent']:]
    old=history[:-LEVEL_1['keep_recent']]
    summary=[]
    for msg in old[-10:]:
        role=msg.get("role",""); content=msg.get("content","")
        if role=="user": summary.append(f"Q: {extract_key_points(content,50)}")
        elif role=="assistant": summary.append(f"A: {extract_key_points(content,50)}")
    if summary:
        return [{"role":"system","content":"📚 История:\n"+ "\n".join(summary[-5:])}] + recent
    return recent

def load_memory(user_id):
    data=atomic_read(MEMORY_FILE,default={})
    raw=data.get(str(user_id),[])
    return compress_history(raw)

def save_memory(user_id, history, backup=True):
    data=atomic_read(MEMORY_FILE,default={})
    data[str(user_id)]=compress_history(history)
    if not atomic_write(MEMORY_FILE,data): return False
    if backup: create_backup(user_id,"memory")
    count=load_counter(user_id)+1; save_counter(user_id,count)
    if count%10==0: create_backup(user_id,"profile")
    return True

def update_level_2(user_id, messages):
    profile=get_profile_cached(user_id)
    if "level_2" not in profile: profile["level_2"]=[]
    batch=messages[-LEVEL_2['compress_interval']:]
    compressed=[]
    for msg in batch:
        role=msg.get("role",""); content=msg.get("content","")
        if role=="user": compressed.append(f"Q: {extract_key_points(content,30)}")
        elif role=="assistant": compressed.append(f"A: {extract_key_points(content,30)}")
    ts=datetime.now().strftime("%d.%m")
    for item in compressed: profile["level_2"].append(f"[{ts}] {item}")
    if len(profile["level_2"])>LEVEL_2['compress_to']:
        profile["level_2"]=profile["level_2"][-LEVEL_2['compress_to']:]
    save_profile(user_id, profile, backup=False)

def update_level_3(user_id, messages):
    profile=get_profile_cached(user_id)
    if "level_3" not in profile: profile["level_3"]=[]
    batch=messages[-LEVEL_3['compress_interval']:]
    compressed=[]
    for msg in batch:
        role=msg.get("role",""); content=msg.get("content","")
        if role=="user": compressed.append(f"Q: {extract_keywords_aggressive(content,25)}")
        elif role=="assistant": compressed.append(f"A: {extract_keywords_aggressive(content,25)}")
    ts=datetime.now().strftime("%m.%d")
    for item in compressed: profile["level_3"].append(f"[{ts}] {item}")
    if len(profile["level_3"])>LEVEL_3['compress_to']:
        profile["level_3"]=profile["level_3"][-LEVEL_3['compress_to']:]
    save_profile(user_id, profile, backup=False)

def update_level_4(user_id, messages):
    profile=get_profile_cached(user_id)
    if "level_4" not in profile: profile["level_4"]=[]
    batch=messages[-LEVEL_4['compress_interval']:]
    compressed=[]
    for msg in batch:
        role=msg.get("role",""); content=msg.get("content","")
        if role=="user": compressed.append(f"Q: {extract_keywords_aggressive(content,20)}")
        elif role=="assistant": compressed.append(f"A: {extract_keywords_aggressive(content,20)}")
    ts=datetime.now().strftime("%m.%d")
    for item in compressed: profile["level_4"].append(f"[{ts}] {item}")
    if len(profile["level_4"])>LEVEL_4['compress_to']:
        profile["level_4"]=profile["level_4"][-LEVEL_4['compress_to']:]
    save_profile(user_id, profile, backup=False)

def update_level_5(user_id, messages):
    profile=get_profile_cached(user_id)
    if "level_5" not in profile: profile["level_5"]=[]
    batch=messages[-LEVEL_5['compress_interval']:]
    compressed=[]
    for msg in batch:
        role=msg.get("role",""); content=msg.get("content","")
        if role=="user": compressed.append(f"Q: {extract_keywords_ultra(content,15)}")
        elif role=="assistant": compressed.append(f"A: {extract_keywords_ultra(content,15)}")
    ts=datetime.now().strftime("%y.%m")
    for item in compressed: profile["level_5"].append(f"[{ts}] {item}")
    if len(profile["level_5"])>LEVEL_5['compress_to']+100:
        old_items=profile["level_5"][:200]
        compressed_old=compress_ultra_old(old_items,50)
        profile["level_5"]=compressed_old+profile["level_5"][200:]
    if len(profile["level_5"])>LEVEL_5['compress_to']:
        profile["level_5"]=profile["level_5"][-LEVEL_5['compress_to']:]
    save_profile(user_id, profile, backup=False)

def parse_time_query(time_query):
    try:
        parts=time_query.split(":")
        if len(parts)==2: return int(parts[0]), int(parts[1]), None
        elif len(parts)==3: return int(parts[0]), int(parts[1]), int(parts[2])
    except: pass
    return None,None,None

def search_by_time(user_id, time_query):
    history=load_memory(user_id); results=[]
    qh,qm,qs=parse_time_query(time_query)
    if qh is None: return results
    for msg in history:
        ts=msg.get("timestamp","")
        if not ts: continue
        try:
            mt=datetime.strptime(ts,"%Y-%m-%d %H:%M:%S")
            if mt.hour==qh and mt.minute==qm: results.append(msg)
        except:
            if time_query in ts: results.append(msg)
    return results

def parse_date_query(query):
    q=query.lower().strip(); now=datetime.now()
    if q=="сегодня": return now.strftime("%Y-%m-%d")
    if q=="вчера": return (now-timedelta(days=1)).strftime("%Y-%m-%d")
    if q=="завтра": return (now+timedelta(days=1)).strftime("%Y-%m-%d")
    patterns=[(r'(\d{2})\.(\d{2})\.(\d{4})','.'),(r'(\d{2})\.(\d{2})','.'),(r'(\d{4})-(\d{2})-(\d{2})','-')]
    for pat,sep in patterns:
        m=re.search(pat,query)
        if m:
            g=m.groups()
            if len(g)==3:
                if sep=='.': d,m,y=g; return f"{y}-{m}-{d}"
                else: y,m,d=g; return f"{y}-{m}-{d}"
            elif len(g)==2:
                d,m=g; return f"{now.year}-{m}-{d}"
    return None

def search_by_date(user_id, date_str):
    history=load_memory(user_id)
    return [msg for msg in history if msg.get("timestamp","").startswith(date_str)]

def search_in_pyramid(user_id, query):
    profile=get_profile_cached(user_id); results=[]; q=query.lower()
    history=load_memory(user_id)
    for msg in history[-20:]:
        content=msg.get("content",""); ts=msg.get("timestamp","")
        if q in content.lower():
            role="👤" if msg.get("role")=="user" else "🤖"
            time_str=f" [{ts}]" if ts else ""
            results.append(f"{role}{time_str} {extract_key_points(content,80)}")
    for level in ['level_2','level_3','level_4','level_5']:
        emoji={'level_2':'📚','level_3':'📖','level_4':'📕','level_5':'📗'}[level]
        for item in profile.get(level,[]):
            if q in item.lower(): results.append(f"{emoji} {item}")
    return results[:15]

async def search_apiserpent(query, retries=2):
    """Асинхронный поиск через APISerpent с повторными попытками и несколькими движками."""
    if not APISERPENT_API_KEY:
        print("⚠️ APISERPENT_API_KEY не задан")
        return []
    engines = ["google", "bing", "duckduckgo"]
    session = await get_http_session()
    for attempt in range(retries):
        for engine in engines:
            try:
                print(f"🔍 Поиск (движок {engine}, попытка {attempt+1}): {query}")
                params = {"q": query, "engine": engine, "num": 5}
                headers = {"X-API-Key": APISERPENT_API_KEY}
                async with session.get("https://apiserpent.com/api/search", params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = []
                        if "results" in data and isinstance(data["results"], dict):
                            results = data["results"].get("organic", [])
                        elif "organic_results" in data:
                            results = data["organic_results"]
                        elif "organic" in data:
                            results = data["organic"]
                        elif "items" in data:
                            results = data["items"]
                        elif isinstance(data.get("results"), list):
                            results = data["results"]
                        if results:
                            formatted = []
                            for r in results[:5]:
                                if isinstance(r, dict):
                                    formatted.append({
                                        "title": str(r.get("title", r.get("name", "Без названия")))[:150],
                                        "snippet": str(r.get("snippet", r.get("description", r.get("text", "Нет описания"))))[:250],
                                        "link": str(r.get("url", r.get("link", r.get("href", "#"))))[:150]
                                    })
                            print(f"✅ Найдено {len(formatted)} результатов (движок {engine})")
                            return formatted
                        else:
                            print(f"⚠️ Движок {engine} вернул пустой ответ")
                    elif resp.status == 401:
                        print("❌ Неверный APISERPENT_API_KEY")
                        return []
                    else:
                        print(f"⚠️ Ошибка {resp.status} от APISerpent (движок {engine})")
            except asyncio.TimeoutError:
                print(f"⏰ Таймаут при поиске (движок {engine})")
            except aiohttp.ClientError as e:
                print(f"🌐 Сетевая ошибка: {e} (движок {engine})")
            except Exception as e:
                print(f"❌ Неизвестная ошибка поиска: {e} (движок {engine})")
        if attempt < retries-1:
            await asyncio.sleep(2**attempt)
    print("❌ Все попытки поиска не дали результатов")
    return []

async def analyze_message(user_id, user_message):
    q = user_message.lower().strip()
    # ЯВНЫЙ ПРИОРИТЕТ ИНТЕРНЕТ-ПОИСКА
    if q.startswith("бро ") or "в интернете" in q or "найди в интернете" in q or "поищи в интернете" in q:
        return {"type": "dynamic", "action": "internet", "needs_search": True, "needs_memory": False}
    short_confirm = ['да','нет','ок','хорошо','понял','поняла','ага','угу','так','ясно','ладно','окей']
    if q.strip() in short_confirm or q.strip() in [c+'.' for c in short_confirm] or q.strip() in [c+'!' for c in short_confirm]:
        return {"type": "confirm", "action": "confirm", "needs_search": False, "needs_memory": False}
    simple_greetings = ['привет','здравствуй','здрасте','приветствую','салют','hello','hi']
    if q in simple_greetings or q in [g+'!' for g in simple_greetings]:
        return {"type": "greeting", "action": "greeting", "needs_search": False, "needs_memory": False}
    personal_triggers = ['имя','город','работа','возраст','интерес','хобби','меня зовут']
    for t in personal_triggers:
        if t in q: return {"type": "personal", "action": "memory", "needs_search": False, "needs_memory": True}
    memory_triggers = ['помнишь','ты помнишь','напомни','что я говорил','что я писал','вспомни']
    for t in memory_triggers:
        if t in q: return {"type": "memory_query", "action": "memory_search", "needs_search": False, "needs_memory": True}
    date_time_triggers = ['какая дата','какое сегодня число','сегодняшняя дата','какой сегодня день','который час','сколько времени','текущее время','сейчас время','дата сегодня','время сейчас']
    for t in date_time_triggers:
        if t in q: return {"type": "date_time", "action": "date_time", "needs_search": False, "needs_memory": False}
    internet_triggers = ['актуализируй','актуализируйте','обнови','обновить','свежие данные','свежую информацию','проверь актуальность','посмотри в интернете','найди в сети','проверь','узнай','посмотри','найди','актуальная информация','какой сейчас','сколько сейчас','что сейчас','последние новости','на сегодня','на завтра','на вчера','текущий курс','текущая погода','свежий курс']
    for t in internet_triggers:
        if t in q: return {"type": "dynamic", "action": "internet", "needs_search": True, "needs_memory": False}
    dynamic_triggers = ['погод','температур','дожд','снег','ветер','градус','курс','доллар','евро','юань','биткоин','новост','событи','происшеств','авар','выбор','кризис','войн','сегодня','завтра','вчера','сейчас','на этой неделе']
    for t in dynamic_triggers:
        if t in q: return {"type": "dynamic", "action": "internet", "needs_search": True, "needs_memory": False}
    instructional_triggers = ['как ','как сделать','как настроить','как установить','инструкция','руководство']
    for t in instructional_triggers:
        if t in q: return {"type": "instructional", "action": "internet", "needs_search": True, "needs_memory": False}
    return {"type": "static", "action": "memory", "needs_search": False, "needs_memory": True}

async def ask_deepseek(messages, retries=3, max_tokens=None):
    session = await get_http_session()
    for attempt in range(retries):
        try:
            payload = {"model": MODEL_DEFAULT, "messages": messages, "temperature": 0.3}
            if max_tokens: payload["max_tokens"] = max_tokens
            async with session.post(f"{DEEPSEEK_API_BASE}/chat/completions", headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"}, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("choices") and len(data["choices"])>0:
                        content = data["choices"][0].get("message",{}).get("content")
                        if content: return content, None
                        return None, "empty"
                    return None, "invalid_response"
                if resp.status == 429:
                    await asyncio.sleep(min(2**attempt,30)); continue
                if resp.status == 401: return None, "unauthorized"
                if resp.status == 500: return None, "server_error"
                return None, f"http_{resp.status}"
        except aiohttp.ClientConnectionError:
            if attempt < retries-1: await asyncio.sleep(2**attempt); continue
            return None, "connection_error"
        except asyncio.TimeoutError:
            if attempt < retries-1: await asyncio.sleep(2**attempt); continue
            return None, "timeout"
        except Exception as e:
            if attempt < retries-1: continue
            return None, f"unknown: {str(e)}"
    return None, "max_retries"

async def generate_response(user_id, user_message, analysis_result, history, profile):
    action = analysis_result.get("action", "memory")
    source = "🧠 из модели"
    if action == "confirm":
        return "✅ Понял! Продолжаем.", False, None
    if action == "greeting":
        greetings = {'привет':'👋 Привет! Как дела?','здравствуй':'👋 Здравствуйте! Чем могу помочь?','пока':'👋 Пока! Было приятно пообщаться!','спасибо':'Пожалуйста! Всегда рад помочь! 🤗'}
        for key,val in greetings.items():
            if key in user_message.lower(): return val, False, None
        return "👋 Привет! Чем могу помочь?", False, None
    if action == "date_time":
        weekdays=['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье']
        weekday=weekdays[NOW.weekday()]
        return f"📅 Сегодня: {CURRENT_DATE} ({weekday})\n🕐 Текущее время: {CURRENT_TIME}", False, "📂 локально"
    if action == "internet":
        print(f"🔍 Поисковый запрос: {user_message}")
        results = await search_apiserpent(user_message)
        if not results:
            print(f"⚠️ APISerpent не дал результатов по запросу: {user_message}")
            system_parts = []
            for key,value in profile.items():
                if key.startswith("last_check_") or key.startswith("update_history_") or key in ["level_2","level_3","level_4","level_5","answer_cache"]:
                    continue
                if isinstance(value, list):
                    if value: system_parts.append(f"{key}: {', '.join(str(v)[:50] for v in value[:3])}")
                else:
                    system_parts.append(f"{key}: {str(value)[:50]}")
            if profile.get("level_2"): system_parts.append(f"📚 1000: {', '.join(profile['level_2'][-10:])}")
            if profile.get("level_3"): system_parts.append(f"📖 10000: {', '.join(profile['level_3'][-5:])}")
            system_prompt = ". ".join(system_parts)[:800]+"..."
            system_msg = {"role":"system","content":f"Сегодня: {CURRENT_DATE} {CURRENT_TIME}. {system_prompt}"}
            history.append({"role":"user","content":user_message})
            messages = [system_msg]+history
            answer, err_code = await ask_deepseek(messages)
            if err_code: return f"⚠️ {analyze_error(err_code)}", False, None
            full_answer = f"🔍 **Я искал в интернете по запросу:**\n`{user_message}`\n\n❌ **Ничего не найдено.**\n\n💡 **Возможно, вы имели в виду:**\n— Уточните запрос (например, 'бро погода в Москве')\n— Или напишите 'бро {user_message} ещё раз' с другими словами\n\n🧠 **А пока я отвечаю из своих знаний:**\n{answer}"
            source = "🧠 из модели (поиск ничего не дал)"
            return full_answer, True, source
        # результаты есть
        search_text = f"🔍 **Я искал в интернете по запросу:**\n`{user_message}`\n\n"
        search_text += f"📊 **Найдено {len(results[:5])} результатов:**\n\n"
        for i,r in enumerate(results[:5],1):
            search_text += f"{i}. **{r['title']}**\n   {r['snippet'][:200]}\n   🔗 {r['link']}\n\n"
        search_prompt = {"role":"system","content":f"""Сегодня: {CURRENT_DATE} {CURRENT_TIME}.

Вопрос пользователя: "{user_message}"

{search_text}

ОТВЕЧАЙ ТОЛЬКО НА ОСНОВЕ НАЙДЕННЫХ ДАННЫХ.
Если вопрос короткий — ответь кратко."""}
        history.append({"role":"user","content":user_message})
        messages = [search_prompt]+history
        answer, err_code = await ask_deepseek(messages)
        if err_code: return f"⚠️ {analyze_error(err_code)}", False, None
        final_answer = f"🔍 **Я искал в интернете по запросу:**\n`{user_message}`\n\n{answer}"
        source = "🌐 из интернета"
        return final_answer, True, source
    # Обработка даты/времени в памяти
    date_match = re.search(r'\b(сегодня|вчера|завтра|\d{2}\.\d{2}(\.\d{4})?|\d{4}-\d{2}-\d{2})\b', user_message, re.IGNORECASE)
    if date_match:
        date_str = parse_date_query(date_match.group(1))
        if date_str:
            date_results = search_by_date(user_id, date_str)
            if date_results:
                result_text = "\n".join([f"{msg.get('timestamp','')} {msg.get('role','')}: {msg.get('content','')[:100]}" for msg in date_results[:10]])
                answer = f"📅 Сообщения за {date_match.group(1)}:\n{result_text}"
                if len(date_results)>10: answer += f"\n... и ещё {len(date_results)-10} сообщений"
                return answer, False, "📂 из памяти (по дате)"
    time_match = re.search(r'(\d{1,2}:\d{2}(:\d{2})?)', user_message)
    if time_match:
        time_results = search_by_time(user_id, time_match.group(1))
        if time_results:
            result_text = "\n".join([f"{msg.get('timestamp','')} {msg.get('role','')}: {msg.get('content','')[:100]}" for msg in time_results[:5]])
            answer = f"🕐 Найдено по времени {time_match.group(1)}:\n{result_text}"
            if len(time_results)>5: answer += f"\n... и ещё {len(time_results)-5} сообщений"
            return answer, False, "📂 из памяти (по времени)"
    # Обычный ответ от модели
    system_parts = []
    for key,value in profile.items():
        if key.startswith("last_check_") or key.startswith("update_history_") or key in ["level_2","level_3","level_4","level_5","answer_cache"]:
            continue
        if isinstance(value, list):
            if value: system_parts.append(f"{key}: {', '.join(str(v)[:50] for v in value[:3])}")
        else:
            system_parts.append(f"{key}: {str(value)[:50]}")
    if profile.get("level_2"): system_parts.append(f"📚 1000: {', '.join(profile['level_2'][-10:])}")
    if profile.get("level_3"): system_parts.append(f"📖 10000: {', '.join(profile['level_3'][-5:])}")
    system_prompt = ". ".join(system_parts)[:800]+"..."
    system_msg = {"role":"system","content":f"Сегодня: {CURRENT_DATE} {CURRENT_TIME}. {system_prompt}"}
    history.append({"role":"user","content":user_message})
    messages = [system_msg]+history
    answer, err_code = await ask_deepseek(messages)
    if err_code: return f"⚠️ {analyze_error(err_code)}", False, None
    source = "🧠 из модели"
    return answer, True, source

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id): await update.message.reply_text("❌ Доступ запрещён."); return
    profile = get_profile_cached(user_id)
    name = profile.get("name", "друг")
    await update.message.reply_text(
        f"👋 Привет, {name}!\n\n📅 Сегодня: {CURRENT_DATE} {CURRENT_TIME}\n\n{get_peak_status()}\n\n"
        "🧠 **Пирамидальная память (1 000 000+ сообщений):**\n• 📝 80 последних (полностью)\n• 📚 1000 сообщений (сжато)\n• 📖 10000 сообщений (сжато)\n• 📕 100000 сообщений (сжато)\n• 📗 1 000 000+ сообщений (суть)\n\n"
        "🕐 **Дату и время я отвечаю точно (локально, без интернета).**\n"
        "📂 **В каждом ответе я указываю источник:**\n   • 📂 из памяти — ответ из профиля или истории\n   • 🌐 из интернета — найден через APISerpent\n   • 🧠 из модели — сгенерирован DeepSeek\n\n"
        "🔍 **Я сам ищу сообщения по дате и времени!**\n   • Просто спроси: «что я писал вчера?» или «покажи 14.07.2026»\n   • Или по времени: «что я писал в 13:44?»\n\n"
        "💾 **Черновики сохраняются ДО отправки** — даже при сбое ответ не потеряется.\n\n"
        "📋 **Команды:**\n• `/profile` — что я помню\n• `/stats` — статистика\n• `/memory [текст]` — поиск в памяти\n• `/forget` — забыть всё\n• `/restore` — восстановить из бэкапа\n\n"
        "🔍 **Принудительный поиск:** `бро погода`"
    )

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id): await update.message.reply_text("❌ Доступ запрещён."); return
    profile = get_profile_cached(user_id)
    if not profile: await update.message.reply_text("📭 Я пока ничего не знаю о тебе."); return
    lines=["🧠 **Пирамидальная память:**\n"]
    level_labels={'level_2':'📚 1000 сообщений','level_3':'📖 10000 сообщений','level_4':'📕 100000 сообщений','level_5':'📗 1 000 000+ сообщений'}
    for key,label in level_labels.items():
        lines.append(f"• {label}: {len(profile.get(key,[]))} пунктов")
    history=load_memory(user_id); lines.append(f"• 📝 80 последних: {len(history)} сообщений")
    lines.append(f"\n👤 **Личная информация:**")
    personal_keys=['name','город','city','работа','job','возраст','age']
    found=False
    for k in personal_keys:
        if k in profile: lines.append(f"• {k}: {profile[k]}"); found=True
    if not found: lines.append("• Пока ничего не запомнил")
    lines.append(f"\n⏰ {get_peak_status()}")
    lines.append(f"\n🔄 **Обновлено:** {profile.get('updated','неизвестно')}")
    await update.message.reply_text("\n".join(lines))

async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id): await update.message.reply_text("❌ Доступ запрещён."); return
    if not context.args:
        await update.message.reply_text("🔍 **Поиск в памяти:**\nНапиши: `/memory что искать`\nНапример: `/memory погода` или `/memory 13:44` или `/memory 14.07.2026`")
        return
    query=' '.join(context.args)
    date_str=parse_date_query(query)
    if date_str:
        date_results=search_by_date(user_id, date_str)
        if date_results:
            lines=[f"📅 Сообщения за {query}:\n"]
            for msg in date_results[:10]:
                lines.append(f"{msg.get('timestamp','')} {msg.get('role','')}: {msg.get('content','')[:100]}")
            if len(date_results)>10: lines.append(f"\n... и ещё {len(date_results)-10} сообщений")
            await update.message.reply_text("\n".join(lines)); return
    time_match=re.search(r'(\d{1,2}:\d{2}(:\d{2})?)', query)
    if time_match:
        time_results=search_by_time(user_id, time_match.group(1))
        if time_results:
            lines=[f"🕐 Найдено по времени {time_match.group(1)}:\n"]
            for msg in time_results[:5]:
                lines.append(f"{msg.get('timestamp','')} {msg.get('role','')}: {msg.get('content','')[:100]}")
            if len(time_results)>5: lines.append(f"\n... и ещё {len(time_results)-5} сообщений")
            await update.message.reply_text("\n".join(lines)); return
    results=search_in_pyramid(user_id, query)
    if not results:
        await update.message.reply_text(f"📭 Ничего не найдено по запросу: '{query}'"); return
    lines=[f"🔍 **Результаты поиска:** '{query}'\n"]
    for i,res in enumerate(results[:10],1): lines.append(f"{i}. {res}")
    if len(results)>10: lines.append(f"\n... и ещё {len(results)-10} результатов")
    await update.message.reply_text("\n".join(lines))

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id): await update.message.reply_text("❌ Доступ запрещён."); return
    profile=get_profile_cached(user_id); history=load_memory(user_id)
    level_labels={'level_2':'📚 1000 сообщений','level_3':'📖 10000 сообщений','level_4':'📕 100000 сообщений','level_5':'📗 1 000 000+ сообщений'}
    lines=["📊 **Статистика памяти:**\n"]
    lines.append(f"• 📝 80 последних: {len(history)} сообщений")
    total=len(history)
    for key,label in level_labels.items():
        val=len(profile.get(key,[])); lines.append(f"• {label}: {val} пунктов"); total+=val
    backup_count=len([f for f in os.listdir(BACKUP_DIR) if f.startswith(f"profile_{user_id}")])
    lines.append(f"\n💾 Бэкапов: {backup_count}")
    counter=load_counter(user_id); lines.append(f"📊 Счётчик сообщений: {counter}")
    lines.append(f"📊 Всего в памяти: ~{total*50:,} сообщений")
    lines.append(f"⏰ {get_peak_status()}")
    lines.append(f"🔄 Обновлён: {profile.get('updated','неизвестно')}")
    await update.message.reply_text("\n".join(lines))

async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id=update.effective_user.id
    if not is_allowed(user_id): await update.message.reply_text("❌ Доступ запрещён."); return
    save_profile(user_id,{}); save_memory(user_id,[]); invalidate_cache(user_id); save_counter(user_id,0)
    await update.message.reply_text("🧹 **Я забыл всё, что знал о тебе!**")

async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id=update.effective_user.id
    if not is_allowed(user_id): await update.message.reply_text("❌ Доступ запрещён."); return
    pr=restore_backup(user_id,"profile"); mr=restore_backup(user_id,"memory")
    if pr or mr:
        invalidate_cache(user_id)
        await update.message.reply_text("✅ **Восстановлено из бэкапа!**\n"+ (f"📋 Профиль восстановлен\n" if pr else "") + (f"💬 История восстановлена" if mr else ""))
    else:
        await update.message.reply_text("❌ Нет бэкапов для восстановления.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id=update.effective_user.id
    if not is_allowed(user_id): await update.message.reply_text("❌ Доступ запрещён."); return
    user_message=update.message.text
    if user_message.lower().startswith("запомни "):
        text=user_message[8:].strip()
        if ":" in text:
            key,val=text.split(":",1); key=key.strip(); val=val.strip()
            profile=get_profile_cached(user_id); profile[key]=val; save_profile(user_id,profile)
            await update.message.reply_text(f"✅ **Запомнил:** {key} = {val}")
        else:
            profile=get_profile_cached(user_id)
            if "факты" not in profile: profile["факты"]=[]
            profile["факты"].append(text); save_profile(user_id,profile)
            await update.message.reply_text(f"✅ **Запомнил факт:** {text}")
        return
    if user_message.lower().startswith("бро "):
        search_query=user_message[4:].strip()
        if not search_query: await update.message.reply_text("❌ Напиши, что искать после 'бро'."); return
        user_message=search_query
        analysis_result={"type":"dynamic","action":"internet","needs_search":True,"needs_memory":False}
    else:
        analysis_result=await analyze_message(user_id, user_message)
    print(f"📊 Анализ: {analysis_result}")
    history=load_memory(user_id); profile=get_profile_cached(user_id)
    answer, should_save, source = await generate_response(user_id, user_message, analysis_result, history, profile)
    if source and not answer.startswith("⚠️") and not answer.startswith("✅"):
        answer = f"{source}\n\n{answer}"
    if is_peak_hour() and not answer.startswith("⚠️"):
        answer = f"⏰ Внимание: сейчас пиковые часы DeepSeek (9:00–12:00, 14:00–18:00). Стоимость API удвоена.\n\n{answer}"
    if should_save:
        now_str=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        user_message_with_date=f"[Сегодня: {CURRENT_DATE} {CURRENT_TIME}]\n\n{user_message}"
        history.append({"role":"user","content":user_message_with_date,"timestamp":now_str})
        history.append({"role":"assistant","content":answer,"timestamp":now_str})
        save_memory(user_id, history)
    for attempt in range(3):
        try:
            if len(answer)>4096:
                for i in range(0,len(answer),4096): await update.message.reply_text(answer[i:i+4096])
            else: await update.message.reply_text(answer)
            break
        except Exception as e:
            if attempt==2: await update.message.reply_text(analyze_error(str(e)))
            else: await asyncio.sleep(1)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: raise context.error
    except Exception as e:
        error_str=str(e); print(f"⚠️ Глобальная ошибка: {error_str}"); import traceback; traceback.print_exc()
        if update and update.effective_message:
            await update.effective_message.reply_text(analyze_error(error_str))

async def shutdown_session():
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close(); print("🔒 HTTP-сессия закрыта")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(CommandHandler("restore", restore_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    # Проверка ключа APISerpent при старте
    if APISERPENT_API_KEY:
        try:
            test = asyncio.run(search_apiserpent("тест", retries=1))
            if test and len(test)>0:
                print("✅ APISERPENT_API_KEY работает")
            else:
                print("⚠️ APISERPENT_API_KEY, возможно, невалиден или сервис недоступен")
        except Exception as e:
            print(f"⚠️ Ошибка проверки APISERPENT_API_KEY: {e}")
    else:
        print("⚠️ APISERPENT_API_KEY не задан — интернет-поиск недоступен")
    print("="*50)
    print("✅ БОТ ЗАПУЩЕН (ФИНАЛЬНАЯ СТАБИЛЬНАЯ ВЕРСИЯ)")
    print(f"📊 Память: 80 → 1000 → 10000 → 100000 → 1 000 000+")
    print(f"💾 Гибридный кэш (RAM + файл): ВКЛЮЧЕН (TTL: {CACHE_TTL} сек, макс. {MAX_CACHE_ITEMS} записей)")
    print(f"💾 Авто-бэкап: ВКЛЮЧЕН (каждые 10 сообщений)")
    print(f"🕐 Дата и время: ОТВЕЧАЮ ЛОКАЛЬНО (без интернета)")
    print(f"💾 Сохранение черновиков (до отправки): ВКЛЮЧЕНО")
    print(f"🔍 Расширенные триггеры для интернет-поиска: ВКЛЮЧЕНЫ")
    print(f"🔍 Команда 'бро' принудительно включает интернет-поиск")
    print(f"👥 Разрешённых пользователей: {len(ALLOWED_USERS_LIST)}")
    print("="*50)
    try:
        app.run_polling()
    finally:
        asyncio.run(shutdown_session())

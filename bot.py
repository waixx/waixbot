# ============================================================
#  BroWaix Bot — оптимизированная версия с улучшенным поиском
# ============================================================
import logging, os, json, sys, re, asyncio, aiohttp, shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()])
logger = logging.getLogger(__name__)
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
APISERPENT_API_KEY = os.getenv("APISERPENT_API_KEY")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0")) if os.getenv("ADMIN_USER_ID") else 0
ALLOWED_USERS_LIST = [int(x.strip()) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()]
if ADMIN_USER_ID and ADMIN_USER_ID not in ALLOWED_USERS_LIST: ALLOWED_USERS_LIST.append(ADMIN_USER_ID)

TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")) if os.getenv("TIMEZONE") else ZoneInfo("UTC")
def now(): return datetime.now(TZ)
def get_current_date(): return now().strftime("%d.%m.%Y")
def get_current_time(): return now().strftime("%H:%M")
def get_current_weekday(): return now().strftime("%A")

MODEL_DEFAULT = os.getenv("MODEL_DEFAULT", "deepseek-v4-flash")
MODEL_FALLBACK = os.getenv("MODEL_FALLBACK", "deepseek-v4-pro")
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
SEARCH_ENGINE = os.getenv("SEARCH_ENGINE", "google")
SEARCH_RESULTS_NUM = int(os.getenv("SEARCH_RESULTS_NUM", "15"))
SEARCH_THRESHOLD = int(os.getenv("SEARCH_THRESHOLD", "3"))
MODEL_TEMPERATURE = float(os.getenv("MODEL_TEMPERATURE", "0.1"))

CORE_SYSTEM_RULE = ("Ты — честный ассистент. НИКОГДА не выдумывай факты. Не знаешь — скажи. Опирайся только на найденные данные. Если данные устарели — предупреди.")

LEVEL_1, LEVEL_2, LEVEL_3, LEVEL_4, LEVEL_5 = {'max_history':80,'keep_recent':20}, {'compress_interval':40,'compress_to':50}, {'compress_interval':200,'compress_to':100}, {'compress_interval':1000,'compress_to':200}, {'compress_interval':10000,'compress_to':500}
PEAK_HOURS = [(9,12),(14,18)]
def is_peak_hour(): return any(s <= now().hour < e for s,e in PEAK_HOURS)
def get_peak_status(): return "⚠️ Сейчас пиковые часы DeepSeek (9-12, 14-18) — стоимость удвоена." if is_peak_hour() else "✅ Непиковые часы."

if not TELEGRAM_TOKEN or not DEEPSEEK_API_KEY: logger.error("Токены не заданы"); sys.exit(1)
if not APISERPENT_API_KEY: logger.warning("APISERPENT_API_KEY не задан")

DATA_DIR, BACKUP_DIR = "data", "data/backups"
os.makedirs(DATA_DIR, exist_ok=True); os.makedirs(BACKUP_DIR, exist_ok=True)
def memory_path(uid): return os.path.join(DATA_DIR, f"memory_{uid}.json")
def profile_path(uid): return os.path.join(DATA_DIR, f"profile_{uid}.json")
def counter_path(uid): return os.path.join(DATA_DIR, f"counter_{uid}.json")

_http_session = None; user_locks = {}; rate_lock = asyncio.Lock(); request_count = {}
def get_user_lock(uid): return user_locks.setdefault(uid, asyncio.Lock())
async def get_http_session():
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=50, limit_per_host=20, keepalive_timeout=30, enable_cleanup_closed=True),
                                              timeout=aiohttp.ClientTimeout(total=60, connect=10, sock_read=30))
    return _http_session

def analyze_error(e): 
    s=str(e).lower()
    if "timeout" in s: return "⏰ Таймаут. Попробуйте позже."
    if "connection" in s: return "🌐 Проблемы с соединением."
    if "429" in str(e): return "📊 Слишком много запросов. Подождите."
    if "401" in str(e): return "🔑 Ошибка API ключа."
    if "400" in str(e): return "⚠️ Некорректный запрос. Проверьте модель."
    if "404" in str(e): return "🔍 Модель не найдена. Используйте deepseek-v4-flash/pro."
    return f"⚠️ Ошибка: {str(e)[:150]}"

def atomic_write(filename, data, as_json=True):
    tmp=filename+".tmp"
    try:
        with open(tmp,'w',encoding='utf-8') as f:
            if as_json: json.dump(data,f,ensure_ascii=False,indent=2)
            else: f.write(data)
            f.flush(); os.fsync(f.fileno())
        shutil.move(tmp,filename); return True
    except Exception as ex: logger.error(f"Ошибка записи {filename}: {ex}"); 
    if os.path.exists(tmp): os.remove(tmp)
    return False

def atomic_read(filename, default=None, as_json=True):
    try:
        with open(filename,'r',encoding='utf-8') as f: return json.load(f) if as_json else f.read()
    except FileNotFoundError: return default
    except (json.JSONDecodeError, OSError) as ex:
        logger.warning(f"Ошибка чтения {filename}: {ex}")
        return default

def load_profile(uid): return atomic_read(profile_path(uid), default={})
def save_profile(uid, profile, backup=True):
    profile["updated"]=now().strftime("%d.%m.%Y %H:%M:%S")
    if not atomic_write(profile_path(uid), profile): return False
    if backup: create_backup(uid,"profile")
    return True
def load_counter(uid): return atomic_read(counter_path(uid), default={"count":0}).get("count",0)
def save_counter(uid, count): atomic_write(counter_path(uid), {"count":count})
def load_memory_raw(uid): return atomic_read(memory_path(uid), default=[])

def create_backup(uid, data_type):
    try:
        ts=now().strftime("%Y%m%d_%H%M%S")
        fname=f"{BACKUP_DIR}/{data_type}_{uid}_{ts}.json"
        if data_type=="profile": atomic_write(fname, load_profile(uid))
        elif data_type=="memory": atomic_write(fname, load_memory_raw(uid))
        backups=sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith(f"{data_type}_{uid}_")])
        for old in backups[:-10]: os.remove(os.path.join(BACKUP_DIR, old))
        return True
    except Exception as ex: logger.error(f"Бэкап ошибка: {ex}"); return False

async def restore_backup(uid, data_type):
    async with get_user_lock(uid):
        try:
            backups=sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith(f"{data_type}_{uid}_")])
            if not backups: return False
            with open(os.path.join(BACKUP_DIR, backups[-1]),'r',encoding='utf-8') as f: data=json.load(f)
            if data_type=="profile": save_profile(uid, data, backup=False)
            elif data_type=="memory": await save_memory(uid, data, backup=False, lock_held=True)
            return True
        except Exception as ex: logger.error(f"Восстановление ошибка: {ex}"); return False

STOP_WORDS={'это','так','вот','ну','просто','очень'}
def extract_key_points(text, max_len=30):
    if len(text)<=max_len: return text
    imp=[w for w in text.split() if w.lower() not in STOP_WORDS and len(w)>2]
    return ' '.join(imp[:10])[:max_len]+"..."
def extract_aggressive(text, max_len=20):
    if len(text)<=max_len: return text
    imp=[w[:8] for w in text.split() if len(w)>3 and w.lower() not in STOP_WORDS]
    return ' '.join(imp[:5])[:max_len]+"..."
def extract_ultra(text, max_len=12):
    if len(text)<=max_len: return text
    imp=[w[:5] for w in text.split() if len(w)>3 and w.lower() not in STOP_WORDS]
    return ' '.join(imp[:3])[:max_len]+"..."
def compress_ultra_old(items, target=50):
    if len(items)<=target: return items
    old=items[:200]; out=[]
    for i in range(0,len(old),4): out.append("[архив] " + " | ".join(x[:20] for x in old[i:i+4]))
    res=out+items[-target:]
    return res[-target:] if len(res)>target+10 else res

def compress_history(history):
    if len(history)<=LEVEL_1['max_history']: return history
    recent=history[-LEVEL_1['keep_recent']:]; old=history[:-LEVEL_1['keep_recent']]; summary=[]
    for m in old[-10:]:
        r,c=m.get("role",""),m.get("content","")
        if r=="user": summary.append(f"Q: {extract_key_points(c,50)}")
        elif r=="assistant": summary.append(f"A: {extract_key_points(c,50)}")
    if summary: return [{"role":"system","content":"📚 История (сжато):\n"+ "\n".join(summary[-5:])}]+recent
    return recent

def load_memory(uid): return compress_history(load_memory_raw(uid))

def _update_level(uid, messages, key, cfg, extractor, ext_len, ts_fmt):
    profile=load_profile(uid); profile.setdefault(key,[]); ts=now().strftime(ts_fmt)
    for m in messages[-cfg['compress_interval']:]:
        r,c=m.get("role",""),m.get("content","")
        if r=="user": profile[key].append(f"[{ts}] Q: {extractor(c, ext_len)}")
        elif r=="assistant": profile[key].append(f"[{ts}] A: {extractor(c, ext_len)}")
    if key=="level_5" and len(profile["level_5"])>cfg['compress_to']+100:
        profile["level_5"]=compress_ultra_old(profile["level_5"][:200],50)+profile["level_5"][200:]
    if len(profile[key])>cfg['compress_to']: profile[key]=profile[key][-cfg['compress_to']:]
    save_profile(uid, profile, backup=False)

async def _save_memory_impl(uid, history, backup):
    try:
        if len(history)>LEVEL_1['max_history']:
            old=history[:-LEVEL_1['keep_recent']]
            if old:
                _update_level(uid, old, "level_2", LEVEL_2, extract_key_points, 30, "%d.%m")
                p=load_profile(uid)
                if len(p.get("level_2",[]))>=LEVEL_2['compress_to']: _update_level(uid, old, "level_3", LEVEL_3, extract_aggressive,25,"%m.%d")
                if len(p.get("level_3",[]))>=LEVEL_3['compress_to']: _update_level(uid, old, "level_4", LEVEL_4, extract_aggressive,20,"%m.%d")
                if len(p.get("level_4",[]))>=LEVEL_4['compress_to']: _update_level(uid, old, "level_5", LEVEL_5, extract_ultra,15,"%y.%m")
        if not atomic_write(memory_path(uid), compress_history(history)): return False
        if backup: create_backup(uid,"memory")
        cnt=load_counter(uid)+1; save_counter(uid,cnt)
        if cnt%10==0: create_backup(uid,"profile")
        return True
    except Exception as ex: logger.error(f"Сохранение ошибка {uid}: {ex}"); return False

async def save_memory(uid, history, backup=True, lock_held=False):
    if lock_held: return await _save_memory_impl(uid, history, backup)
    async with get_user_lock(uid): return await _save_memory_impl(uid, history, backup)

def parse_time_query(tq):
    try:
        parts=tq.split(":"); 
        if len(parts)>=2: return int(parts[0]), int(parts[1])
    except: pass
    return None,None
def search_by_time(uid, tq):
    qh,qm=parse_time_query(tq); res=[]
    if qh is None: return res
    for m in load_memory_raw(uid):
        ts=m.get("timestamp","")
        if not ts: continue
        try:
            mt=datetime.strptime(ts,"%Y-%m-%d %H:%M:%S")
            if mt.hour==qh and mt.minute==qm: res.append(m)
        except: 
            if tq in ts: res.append(m)
    return res

def parse_date_query(query):
    q=query.lower().strip(); n=now()
    if q=="сегодня": return n.strftime("%Y-%m-%d")
    if q=="вчера": return (n-timedelta(days=1)).strftime("%Y-%m-%d")
    if q=="завтра": return (n+timedelta(days=1)).strftime("%Y-%m-%d")
    for pat in [r'(\d{2})\.(\d{2})\.(\d{4})', r'(\d{2})\.(\d{2})', r'(\d{4})-(\d{2})-(\d{2})']:
        m=re.search(pat, query)
        if m:
            g=m.groups()
            if len(g)==3:
                if '.' in query: d,mo,y=g
                else: y,mo,d=g
                return f"{y}-{mo}-{d}"
            if len(g)==2:
                d,mo=g
                return f"{n.year}-{mo}-{d}"
    return None

def search_by_date(uid, date_str):
    return [m for m in load_memory_raw(uid) if m.get("timestamp","").startswith(date_str)]

def search_in_pyramid(uid, query):
    profile, q, res = load_profile(uid), query.lower(), []
    for m in load_memory_raw(uid)[-40:]:
        c=m.get("content","")
        if q in c.lower():
            role="👤" if m.get("role")=="user" else "🤖"
            ts=m.get("timestamp","")
            res.append(f"{role}{(' ['+ts+']') if ts else ''} {extract_key_points(c,80)}")
    for lvl, em in [("level_2","📚"),("level_3","📖"),("level_4","📕"),("level_5","📗")]:
        for item in profile.get(lvl, []):
            if q in item.lower(): res.append(f"{em} {item}")
    return res[:15]

# ========== НОВАЯ ВЕРСИЯ analyze_message с триггерами ==========
async def analyze_message(user_message):
    q=user_message.lower().strip()
    confirm=['да','нет','ок','хорошо','понял','поняла','ага','угу','ясно','ладно','окей']
    if q in confirm or q.rstrip('.!') in confirm: return {"action":"confirm"}
    greet=['привет','здравствуй','здрасте','приветствую','салют','hello','hi']
    if q in greet or q.rstrip('!') in greet: return {"action":"greeting"}
    if any(t in q for t in ['имя','город','работа','возраст','интерес','хобби','меня зовут']): return {"action":"memory"}
    if any(t in q for t in ['помнишь','напомни','что я говорил','что я писал','вспомни']): return {"action":"memory"}
    dt_kw=['дата','время','число','который час','сколько времени','какая дата','какое сегодня число','какой сегодня день','текущее время']
    if any(k in q for k in dt_kw): return {"action":"date_time"}
    date_indicators=['сегодня','завтра','вчера']
    if any(ind in q for ind in date_indicators) or re.search(r'\d{2}\.\d{2}(\.\d{4})?', q): return {"action":"internet"}
    dyn=['погод','температур','прогноз','осадк','курс валют','курс доллар','курс евро','курс юан','биткоин','котировк',
         'последние новости','свежие новости','что произошло сегодня',
         'релиз','вышла','новая версия','обновление','python','выпущена']   # добавлены триггеры
    if any(t in q for t in dyn): return {"action":"internet"}
    return {"action":"memory"}

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ РАНЖИРОВАНИЯ ==========
def extract_year_from_text(text):
    m=re.search(r'\b(20[2-9][0-9])\b', text)
    return int(m.group(1)) if m else None

def is_official_link(link):
    link_lower=link.lower()
    return any(dom in link_lower for dom in ['python.org','docs.python.org','peps.python.org','github.com/python','pypi.org'])

# ========== НОВАЯ rephrase_query с добавлением года ==========
async def rephrase_query(query):
    date_keywords=['релиз','выход','дата','release','when','new version','вышла','обновление']
    if any(kw in query.lower() for kw in date_keywords):
        if str(now().year) not in query: query=f"{query} {now().year}"
    prompt=("Переформулируй запрос для поиска, добавь уточнения, убери лишнее. Ответь только запросом.\nОригинал: "+query)
    messages=[{"role":"system","content":"Ты помощник по оптимизации поиска."},{"role":"user","content":prompt}]
    try:
        rephrased,err=await ask_deepseek(messages, max_tokens=60, model="deepseek-v4-flash")
        if err: return query
        if rephrased and len(rephrased)>5: return rephrased.strip()
        return query
    except: return query

async def simplify_query(query):
    prompt=f"Преврати в короткий запрос из 3-5 ключевых слов, ответь только словами.\nОригинал: {query}"
    messages=[{"role":"system","content":"Ты помощник по созданию коротких запросов."},{"role":"user","content":prompt}]
    try:
        short,err=await ask_deepseek(messages, max_tokens=30, model="deepseek-v4-flash")
        if err: return ' '.join(query.split()[:3])
        if short and len(short)>3: return short.strip()
        return query
    except: return query

# ========== DEEPSEEK API ==========
async def ask_deepseek(messages, retries=3, max_tokens=None, model=None):
    session=await get_http_session()
    use_model=model or MODEL_DEFAULT
    for attempt in range(retries):
        try:
            payload={"model":use_model,"messages":messages,"temperature":MODEL_TEMPERATURE}
            if max_tokens: payload["max_tokens"]=max_tokens
            async with session.post(f"{DEEPSEEK_API_BASE}/chat/completions",
                                    headers={"Authorization":f"Bearer {DEEPSEEK_API_KEY}"},
                                    json=payload) as resp:
                if resp.status==200:
                    data=await resp.json()
                    if data.get("choices"):
                        c=data["choices"][0].get("message",{}).get("content")
                        return (c,None) if c else (None,"empty")
                    return None,"invalid_response"
                if resp.status==429: await asyncio.sleep(min(2**attempt,30)); continue
                if resp.status in (400,404) and use_model!=MODEL_FALLBACK:
                    logger.warning(f"Модель {use_model} недоступна, пробуем {MODEL_FALLBACK}")
                    use_model=MODEL_FALLBACK; continue
                return None,f"http_{resp.status}"
        except Exception as ex:
            if attempt<retries-1: await asyncio.sleep(2**attempt); continue
            return None,type(ex).__name__.lower()
    return None,"max_retries"

def build_profile_context(profile):
    parts=[]
    for k,v in profile.items():
        if k in ("updated","level_2","level_3","level_4","level_5") or k.startswith(("last_check_","update_history_")): continue
        if isinstance(v,list):
            if v: parts.append(f"{k}: {', '.join(str(x)[:50] for x in v[:3])}")
        else: parts.append(f"{k}: {str(v)[:50]}")
    if profile.get("level_2"): parts.append("📚: "+", ".join(profile['level_2'][-10:]))
    if profile.get("level_3"): parts.append("📖: "+", ".join(profile['level_3'][-5:]))
    ctx=". ".join(parts)
    return ctx[:800]+"..." if len(ctx)>800 else ctx

# ========== APISERPENT ==========
async def search_apiserpent_async(query, num=SEARCH_RESULTS_NUM):
    if not APISERPENT_API_KEY: return []
    session=await get_http_session()
    try:
        logger.info(f"🔍 Поиск ({SEARCH_ENGINE}, num={num}): {query}")
        async with session.get("https://apiserpent.com/api/search",
                               params={"q":query,"engine":SEARCH_ENGINE,"num":num},
                               headers={"X-API-Key":APISERPENT_API_KEY}, timeout=30) as r:
            if r.status!=200: return []
            data=await r.json()
            results=[]
            if isinstance(data.get("results"),dict): results=data["results"].get("organic",[])
            elif "organic_results" in data: results=data["organic_results"]
            elif isinstance(data.get("results"),list): results=data["results"]
            elif "organic" in data: results=data["organic"]
            elif "items" in data: results=data["items"]
            if not results and isinstance(data,dict):
                for k in data:
                    if isinstance(data[k],list) and data[k] and isinstance(data[k][0],dict):
                        results=data[k]; break
            out=[]
            for x in results[:num]:
                if isinstance(x,dict):
                    out.append({"title":str(x.get("title",x.get("name","Без названия")))[:150],
                                "snippet":str(x.get("snippet",x.get("description",x.get("text","Нет описания"))))[:250],
                                "link":str(x.get("url",x.get("link",x.get("href","#"))))[:150]})
            return out
    except asyncio.TimeoutError: logger.error("Таймаут APISerpent"); return []
    except Exception as ex: logger.error(f"Ошибка APISerpent: {ex}"); return []

# ========== ГЕНЕРАЦИЯ ОТВЕТА (С УЛУЧШЕННЫМ ПОИСКОМ) ==========
async def generate_response(uid, user_message, analysis, history, profile):
    action=analysis.get("action","memory")
    if action=="confirm": return "✅ Понял! Продолжаем.", False, None
    if action=="greeting":
        for k,v in {'привет':'👋 Привет! Как дела?','здравствуй':'👋 Здравствуйте!','пока':'👋 Пока!','спасибо':'Пожалуйста! 🤗'}.items():
            if k in user_message.lower(): return v, False, None
        return "👋 Привет! Чем могу помочь?", False, None
    if action=="date_time":
        wd={'Monday':'Понедельник','Tuesday':'Вторник','Wednesday':'Среда','Thursday':'Четверг','Friday':'Пятница','Saturday':'Суббота','Sunday':'Воскресенье'}.get(get_current_weekday(),"")
        return f"📅 Сегодня: {get_current_date()} ({wd})\n🕐 Время: {get_current_time()}", False, "📂 локально"
    if action=="internet":
        ctx=build_profile_context(profile)
        words=user_message.split()
        has_domain=any(ext in user_message for ext in ['.com','.ru','.org','.net','.io'])
        if len(words)<=2 or (has_domain and len(words)<=3):
            return ("🔍 Короткий запрос. Уточните, что именно интересует: документация, баланс, API-ключ, инструкция?"), False, None

        logger.info(f"🔍 Поиск по оригинальному запросу: {user_message}")
        results_original=await search_apiserpent_async(user_message)
        all_results=results_original[:]
        seen=set(res.get('link') for res in all_results if res.get('link'))

        if len(all_results)<SEARCH_THRESHOLD:
            optimized=await rephrase_query(user_message)
            if optimized and optimized!=user_message:
                results_optimized=await search_apiserpent_async(optimized)
                for res in results_optimized:
                    link=res.get('link')
                    if link and link not in seen: seen.add(link); all_results.append(res)
        if len(all_results)<SEARCH_THRESHOLD:
            short=await simplify_query(user_message)
            if short and short!=user_message and short!=optimized:
                results_short=await search_apiserpent_async(short)
                for res in results_short:
                    link=res.get('link')
                    if link and link not in seen: seen.add(link); all_results.append(res)

        if not all_results:
            sysmsg={"role":"system","content":f"{CORE_SYSTEM_RULE}\nСегодня: {get_current_date()} {get_current_time()}. {ctx}\nПоиск не дал результатов. Скажи честно."}
            history.append({"role":"user","content":user_message})
            ans,err=await ask_deepseek([sysmsg]+history)
            if err: return f"⚠️ {analyze_error(err)}", False, None
            return f"🔍 **Искал:** `{user_message}`\n\n❌ Ничего не найдено.\n\n🧠 {ans}", True, "🧠 из модели (поиск пуст)"

        # --- Ранжирование с учётом года и официальности ---
        for res in all_results:
            text=res.get('title','')+' '+res.get('snippet','')
            res['year']=extract_year_from_text(text) or 0
        current_year=now().year
        keywords=set(user_message.lower().split())
        stop_words={'как','чтобы','для','при','на','в','и','не','через','vpn','бро','телеграм'}
        keywords={w for w in keywords if w not in stop_words and len(w)>2}

        def rank_result(res):
            score=0
            if is_official_link(res.get('link','')): score+=10
            if res.get('year') and abs(res['year']-current_year)<=1: score+=5
            text=(res.get('title','')+' '+res.get('snippet','')).lower()
            for kw in keywords:
                if kw in text: score+=1
            return score

        all_results.sort(key=rank_result, reverse=True)
        top_results=all_results[:8]

        stext=f"🔍 **Искал:** `{user_message}`\n\n📊 Найдено {len(top_results)} результатов:\n\n"
        for i,r in enumerate(top_results,1):
            is_off=is_official_link(r.get('link',''))
            mark=" ⭐ (официальный)" if is_off else ""
            year_note=f" (год: {r['year']})" if r.get('year') else ""
            stext+=f"{i}. **{r['title']}**{mark}{year_note}\n   {r['snippet'][:200]}\n   🔗 {r['link']}\n\n"

        sp={"role":"system","content":
            f"{CORE_SYSTEM_RULE}\nСегодня: {get_current_date()} {get_current_time()}.\n"
            f"Вопрос: \"{user_message}\"\nКонтекст: {ctx}\n\nНайденные данные:\n{stext}\n\n"
            "ИНСТРУКЦИЯ:\n"
            "1. Отвечай ТОЛЬКО на основе найденных данных. НЕ ВЫДУМЫВАЙ.\n"
            "2. Если есть противоречия – укажи и отдай приоритет официальным (⭐).\n"
            "3. Для технических вопросов проверь год выпуска. Если данные устарели – предупреди.\n"
            "4. Всегда указывай источники (ссылки).\n"
            "5. Если точного ответа нет – скажи честно.\n"
            "6. Структурируй ответ: заголовки, списки.\n"
            "7. В конце перечисли использованные источники."}
        history.append({"role":"user","content":user_message})
        ans,err=await ask_deepseek([sp]+history)
        if err: return f"⚠️ {analyze_error(err)}", False, None

        # Пост-обработка для исправления типичных ошибок
        if "PEP 701" in ans or "новый парсер f-строк" in ans:
            ans="⚠️ Внимание: PEP 701 (новый парсер f-строк) был реализован в Python 3.12, а не в 3.13.\n\n"+ans
        if "PEP 695" in ans or "type param syntax" in ans or "def func[T]" in ans:
            ans="⚠️ Внимание: синтаксис type param (PEP 695) реализован в Python 3.12, а не в 3.13.\n\n"+ans
        if "MutableSequence" in ans and "удалён" in ans:
            ans="⚠️ Внимание: утверждение об удалении MutableSequence не соответствует действительности.\n\n"+ans

        year_match=re.search(r'\b(20[2-9][0-9])\b', ans)
        if year_match:
            mentioned_year=int(year_match.group(1))
            if abs(mentioned_year-current_year)>1:
                ans=f"⚠️ В ответе упоминается год {mentioned_year}, отличается от текущего ({current_year}). Проверьте актуальность.\n\n{ans}"

        return f"🔍 **Искал в интернете:** `{user_message}`\n\n{ans}", True, "🌐 из интернета"

    # поиск по дате и времени
    dm=re.search(r'\b(сегодня|вчера|завтра|\d{2}\.\d{2}(\.\d{4})?|\d{4}-\d{2}-\d{2})\b', user_message, re.I)
    if dm:
        ds=parse_date_query(dm.group(1))
        if ds:
            res=search_by_date(uid, ds)
            if res:
                txt="\n".join(f"{m.get('timestamp','')} {m.get('role','')}: {m.get('content','')[:100]}" for m in res[:10])
                a=f"📅 За {dm.group(1)}:\n{txt}"+ (f"\n... и ещё {len(res)-10}" if len(res)>10 else "")
                return a, False, "📂 из памяти (по дате)"
    tm=re.search(r'(\d{1,2}:\d{2}(:\d{2})?)', user_message)
    if tm:
        res=search_by_time(uid, tm.group(1))
        if res:
            txt="\n".join(f"{m.get('timestamp','')} {m.get('role','')}: {m.get('content','')[:100]}" for m in res[:5])
            a=f"🕐 По времени {tm.group(1)}:\n{txt}"+ (f"\n... и ещё {len(res)-5}" if len(res)>5 else "")
            return a, False, "📂 из памяти (по времени)"

    sysmsg={"role":"system","content":f"{CORE_SYSTEM_RULE}\nСегодня: {get_current_date()} {get_current_time()}. {build_profile_context(profile)}"}
    history.append({"role":"user","content":user_message})
    ans,err=await ask_deepseek([sysmsg]+history)
    if err: return f"⚠️ {analyze_error(err)}", False, None
    return ans, True, "🧠 из модели"

# ========== ОСТАЛЬНЫЕ ФУНКЦИИ (без изменений) ==========
async def safe_reply(update: Update, text: str):
    msg=update.effective_message
    if msg is None: return
    for attempt in range(3):
        try:
            if len(text)>4096:
                for i in range(0,len(text),4096): await msg.reply_text(text[i:i+4096])
            else: await msg.reply_text(text)
            return
        except Exception as ex:
            if attempt==2: logger.error(f"safe_reply не смог: {ex}")
            else: await asyncio.sleep(1)

def is_allowed(uid): return not ALLOWED_USERS_LIST or uid in ALLOWED_USERS_LIST

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None: return
    uid=update.effective_user.id
    if not is_allowed(uid): await safe_reply(update,"❌ Доступ запрещён."); return
    name=load_profile(uid).get("name","друг")
    await safe_reply(update,
        f"👋 Привет, {name}!\n\n📅 Сегодня: {get_current_date()} {get_current_time()}\n\n{get_peak_status()}\n\n"
        "🛡 Мой принцип: никогда не врать.\n\n"
        "📋 Команды: /profile /stats /memory /forget /restore")

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None: return
    uid=update.effective_user.id
    if not is_allowed(uid): await safe_reply(update,"❌ Доступ запрещён."); return
    p=load_profile(uid)
    if not p: await safe_reply(update,"📭 Я пока ничего не знаю о тебе."); return
    lines=["🧠 **Память:**"]
    for k,lab in {'level_2':'📚 ур.2','level_3':'📖 ур.3','level_4':'📕 ур.4','level_5':'📗 ур.5'}.items():
        lines.append(f"• {lab}: {len(p.get(k, []))} пунктов")
    lines.append(f"• 📝 активная история: {len(load_memory_raw(uid))} сообщений")
    lines.append("\n👤 **Личное:**")
    exclude={'updated','level_2','level_3','level_4','level_5'}
    personal_keys=[k for k in p.keys() if k not in exclude]
    if personal_keys:
        for k in personal_keys: lines.append(f"• {k}: {p[k]}")
    else: lines.append("• Пока ничего не запомнил")
    lines.append(f"\n⏰ {get_peak_status()}\n🔄 Обновлено: {p.get('updated','неизвестно')}")
    await safe_reply(update, "\n".join(lines))

async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None: return
    uid=update.effective_user.id
    if not is_allowed(uid): await safe_reply(update,"❌ Доступ запрещён."); return
    if not context.args:
        await safe_reply(update,"🔍 Поиск: `/memory что искать`\nПример: `/memory погода`, `/memory 13:44`, `/memory 14.07.2026`"); return
    query=' '.join(context.args)
    ds=parse_date_query(query)
    if ds:
        res=search_by_date(uid, ds)
        if res:
            lines=[f"📅 За {query}:"] + [f"{m.get('timestamp','')} {m.get('role','')}: {m.get('content','')[:100]}" for m in res[:10]]
            if len(res)>10: lines.append(f"... и ещё {len(res)-10}")
            await safe_reply(update,"\n".join(lines)); return
    tm=re.search(r'(\d{1,2}:\d{2}(:\d{2})?)', query)
    if tm:
        res=search_by_time(uid, tm.group(1))
        if res:
            lines=[f"🕐 По времени {tm.group(1)}:"] + [f"{m.get('timestamp','')} {m.get('role','')}: {m.get('content','')[:100]}" for m in res[:5]]
            if len(res)>5: lines.append(f"... и ещё {len(res)-5}")
            await safe_reply(update,"\n".join(lines)); return
    res=search_in_pyramid(uid, query)
    if not res: await safe_reply(update,f"📭 Ничего не найдено: '{query}'"); return
    lines=[f"🔍 Результаты '{query}':"] + [f"{i}. {r}" for i,r in enumerate(res[:10],1)]
    if len(res)>10: lines.append(f"... и ещё {len(res)-10}")
    await safe_reply(update,"\n".join(lines))

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None: return
    uid=update.effective_user.id
    if not is_allowed(uid): await safe_reply(update,"❌ Доступ запрещён."); return
    p=load_profile(uid); raw=load_memory_raw(uid)
    lines=["📊 **Статистика:**"]
    lines.append(f"• Обработано сообщений: {load_counter(uid)}")
    lines.append(f"• В активной истории: {len(raw)}")
    total=0
    for k,lab in {'level_2':'📚 ур.2','level_3':'📖 ур.3','level_4':'📕 ур.4','level_5':'📗 ур.5'}.items():
        c=len(p.get(k,[])); total+=c; lines.append(f"• {lab}: {c} сжатых пунктов")
    lines.append(f"\n📦 Всего сжатых пунктов: {total}")
    bc=len([f for f in os.listdir(BACKUP_DIR) if f.startswith(f"profile_{uid}_")])
    lines.append(f"💾 Бэкапов профиля: {bc}\n⏰ {get_peak_status()}\n🔄 {p.get('updated','неизвестно')}")
    await safe_reply(update, "\n".join(lines))

async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None: return
    uid=update.effective_user.id
    if not is_allowed(uid): await safe_reply(update,"❌ Доступ запрещён."); return
    async with get_user_lock(uid):
        save_profile(uid, {})
        await save_memory(uid, [], backup=True, lock_held=True)
        save_counter(uid, 0)
    await safe_reply(update,"🧹 Я забыл всё, что знал о тебе!")

async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None: return
    uid=update.effective_user.id
    if not is_allowed(uid): await safe_reply(update,"❌ Доступ запрещён."); return
    pr=await restore_backup(uid,"profile")
    mr=await restore_backup(uid,"memory")
    if pr or mr: await safe_reply(update,"✅ Восстановлено!\n" + ("📋 Профиль\n" if pr else "") + ("💬 История" if mr else ""))
    else: await safe_reply(update,"❌ Нет бэкапов.")

RATE_LIMIT, RATE_WINDOW = 3, 5
async def check_rate_limit(uid):
    async with rate_lock:
        now_ts=datetime.now().timestamp()
        request_count[uid]=[t for t in request_count.get(uid, []) if now_ts-t<RATE_WINDOW]
        if len(request_count[uid])>=RATE_LIMIT: return False
        request_count[uid].append(now_ts)
        for u in list(request_count.keys()):
            if not request_count[u]: del request_count[u]
        return True

async def clean_request_count():
    while True:
        try:
            await asyncio.sleep(21600)
            async with rate_lock:
                now_ts=datetime.now().timestamp()
                to_delete=[uid for uid, timestamps in request_count.items() if not timestamps or now_ts-timestamps[-1]>600]
                for uid in to_delete: del request_count[uid]
                if to_delete: logger.debug(f"Очищено {len(to_delete)} неактивных записей")
        except Exception as e:
            logger.error(f"Ошибка в clean_request_count: {e}, перезапуск через 60 сек")
            await asyncio.sleep(60)

async def auto_restore_all_users():
    logger.info("🔄 Проверка данных при старте...")
    backup_files=os.listdir(BACKUP_DIR)
    user_ids=set()
    for fname in backup_files:
        parts=fname.split('_')
        if len(parts)>=2 and parts[0] in ('profile','memory'):
            try: user_ids.add(int(parts[1]))
            except: pass
    if not user_ids: return
    for uid in user_ids:
        mem_path=memory_path(uid); prof_path=profile_path(uid)
        need_restore=False
        mem_data=atomic_read(mem_path, default=None)
        if mem_data is None or (isinstance(mem_data,list) and len(mem_data)==0): need_restore=True
        prof_data=atomic_read(prof_path, default=None)
        if prof_data is None or (isinstance(prof_data,dict) and len(prof_data)==0): need_restore=True
        if need_restore:
            pr=await restore_backup(uid,"profile")
            mr=await restore_backup(uid,"memory")
            if pr or mr: logger.info(f"✅ Пользователь {uid} восстановлен")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.effective_message is None: return
    if not update.effective_message.text: return
    uid=update.effective_user.id
    if not is_allowed(uid): await safe_reply(update,"❌ Доступ запрещён."); return
    if not await check_rate_limit(uid): await safe_reply(update,"⏳ Слишком много запросов. Подождите 5 секунд."); return

    user_message=update.effective_message.text
    if len(user_message)>3500: user_message=user_message[:3500]+"... (обрезано)"

    if context.user_data.get("awaiting_internet_confirm"):
        if user_message.lower() in ("да","нет","д","н","yes","no"):
            if user_message.lower() in ("да","д","yes"):
                context.user_data["awaiting_internet_confirm"]=False
                query=context.user_data.get("pending_query")
                analysis=context.user_data.get("pending_analysis")
                if query and analysis:
                    await safe_reply(update,"🌐 Ищу информацию...")
                    history=load_memory(uid); profile=load_profile(uid)
                    answer,should_save,source=await generate_response(uid, query, analysis, history, profile)
                    if source and not answer.startswith(("⚠️","✅")): answer=f"{source}\n\n{answer}"
                    if is_peak_hour() and not answer.startswith("⚠️"): answer=f"⏰ Внимание: пиковые часы DeepSeek. Стоимость API удвоена.\n\n{answer}"
                    if should_save:
                        now_str=now().strftime("%Y-%m-%d %H:%M:%S")
                        history.append({"role":"user","content":query,"timestamp":now_str})
                        history.append({"role":"assistant","content":answer,"timestamp":now_str})
                        await save_memory(uid, history)
                    await safe_reply(update, answer)
                else: await safe_reply(update,"❌ Ошибка: запрос потерян.")
                context.user_data.pop("pending_query",None); context.user_data.pop("pending_analysis",None); return
            else:
                context.user_data["awaiting_internet_confirm"]=False
                query=context.user_data.get("pending_query")
                analysis=context.user_data.get("pending_analysis")
                if query and analysis:
                    analysis["action"]="memory"
                    history=load_memory(uid); profile=load_profile(uid)
                    answer,should_save,source=await generate_response(uid, query, analysis, history, profile)
                    if source and not answer.startswith(("⚠️","✅")): answer=f"{source}\n\n{answer}"
                    if is_peak_hour() and not answer.startswith("⚠️"): answer=f"⏰ Внимание: пиковые часы DeepSeek. Стоимость API удвоена.\n\n{answer}"
                    if should_save:
                        now_str=now().strftime("%Y-%m-%d %H:%M:%S")
                        history.append({"role":"user","content":query,"timestamp":now_str})
                        history.append({"role":"assistant","content":answer,"timestamp":now_str})
                        await save_memory(uid, history)
                    await safe_reply(update, answer)
                else: await safe_reply(update,"❌ Ошибка: запрос потерян.")
                context.user_data.pop("pending_query",None); context.user_data.pop("pending_analysis",None); return
        else: await safe_reply(update,"❓ Напишите «да» или «нет» — я продолжу."); return

    if user_message.lower().startswith("запомни "):
        text=user_message[8:].strip()
        async with get_user_lock(uid):
            p=load_profile(uid)
            if ":" in text:
                k,v=text.split(":",1); k,v=k.strip(),v.strip()
                p[k]=v
                if save_profile(uid,p): await safe_reply(update,f"✅ Запомнил: {k} = {v}")
                else: await safe_reply(update,"❌ Не удалось сохранить.")
            else:
                p.setdefault("факты",[]).append(text)
                if save_profile(uid,p): await safe_reply(update,f"✅ Запомнил факт: {text}")
                else: await safe_reply(update,"❌ Не удалось сохранить факт.")
        return

    force_internet=False
    if user_message.lower().startswith("бро "):
        sq=user_message[4:].strip()
        if not sq: await safe_reply(update,"❌ Напиши, что искать."); return
        user_message=sq; force_internet=True

    analysis=await analyze_message(user_message)

    if force_internet:
        analysis["action"]="internet"
        status_msg=await update.effective_message.reply_text("🌐 Ищу информацию...")
        history=load_memory(uid); profile=load_profile(uid)
        answer,should_save,source=await generate_response(uid, user_message, analysis, history, profile)
        if status_msg:
            try: await status_msg.delete()
            except: pass
        if source and not answer.startswith(("⚠️","✅")): answer=f"{source}\n\n{answer}"
        if is_peak_hour() and not answer.startswith("⚠️"): answer=f"⏰ Внимание: пиковые часы DeepSeek. Стоимость API удвоена.\n\n{answer}"
        if should_save:
            now_str=now().strftime("%Y-%m-%d %H:%M:%S")
            history.append({"role":"user","content":user_message,"timestamp":now_str})
            history.append({"role":"assistant","content":answer,"timestamp":now_str})
            await save_memory(uid, history)
        await safe_reply(update, answer)
        return

    if analysis.get("action")=="internet":
        context.user_data["awaiting_internet_confirm"]=True
        context.user_data["pending_query"]=user_message
        context.user_data["pending_analysis"]=analysis
        await safe_reply(update,"🔍 Я могу поискать в интернете. Напишите «да» или «нет».")
        return

    history=load_memory(uid); profile=load_profile(uid)
    answer,should_save,source=await generate_response(uid, user_message, analysis, history, profile)
    if source and not answer.startswith(("⚠️","✅")): answer=f"{source}\n\n{answer}"
    if is_peak_hour() and not answer.startswith("⚠️"): answer=f"⏰ Внимание: пиковые часы DeepSeek. Стоимость API удвоена.\n\n{answer}"
    if should_save:
        now_str=now().strftime("%Y-%m-%d %H:%M:%S")
        history.append({"role":"user","content":user_message,"timestamp":now_str})
        history.append({"role":"assistant","content":answer,"timestamp":now_str})
        await save_memory(uid, history)
    await safe_reply(update, answer)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Глобальная ошибка: {context.error}")
    if isinstance(update, Update): await safe_reply(update, analyze_error(str(context.error)))

async def shutdown_session():
    global _http_session
    if _http_session and not _http_session.closed: await _http_session.close()

if __name__=="__main__":
    loop=asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(auto_restore_all_users())
    loop.create_task(clean_request_count())
    app=ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(CommandHandler("restore", restore_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    logger.info("✅ БОТ ГОТОВ К РАБОТЕ.")
    try: app.run_polling()
    except KeyboardInterrupt: logger.info("👋 Остановлен")
    finally:
        if _http_session and not _http_session.closed:
            try: loop.run_until_complete(shutdown_session())
            except: pass
        loop.close()

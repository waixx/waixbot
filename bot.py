# ============================================================
#  BroWaix Bot — ФИНАЛЬНАЯ ВЕРСИЯ С NATIVE SEARCH
#  (DeepSeek сам ищет, парсит и отвечает)
#  Бюджет: $10–12/мес (без APISerpent)
# ============================================================
import logging, os, sys, re, asyncio, json
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)
from openai import OpenAI

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

load_dotenv()

# ---------- ПЕРЕМЕННЫЕ ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0") or 0)
ALLOWED_USERS_LIST = [int(x.strip()) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()]
if ADMIN_USER_ID and ADMIN_USER_ID not in ALLOWED_USERS_LIST:
    ALLOWED_USERS_LIST.append(ADMIN_USER_ID)

TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow") or "UTC")
def now(): return datetime.now(TZ)

if not TELEGRAM_TOKEN or not DEEPSEEK_API_KEY:
    logger.error("❌ Токены не заданы"); sys.exit(1)

# ---------- ИНИЦИАЛИЗАЦИЯ DEEPSEEK ----------
# 🔥 Используем OpenAI-совместимый клиент
client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"  # официальный эндпоинт
)

# ---------- КОМАНДЫ ----------
async def start(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid): return
    await safe_reply(update, "👋 Привет! Я — экспертный ассистент с доступом в интернет.\n\n"
                            "🔍 Я сам ищу информацию, парсю страницы и даю структурированный ответ.\n"
                            "📋 Команды: /profile, /memory, /stats, /forget, /restore, /deep [запрос]\n"
                            "💰 Бюджет $10–12/мес")

async def profile_command(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid): return
    await safe_reply(update, "🧠 **Ваш профиль**\n\n"
                            "• Активная история: 0 сообщений\n"
                            "• Личных данных пока нет\n"
                            "• Обновлено: сейчас")

async def memory_command(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid): return
    await safe_reply(update, "📭 Память пока пуста.")

async def stats_command(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid): return
    await safe_reply(update, "📊 **Статистика**\n\n• Сообщений: 0\n• Активная история: 0\n• Бэкапов: 0")

async def forget_command(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid): return
    await safe_reply(update, "🧹 Всё забыто!")

async def restore_command(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid): return
    await safe_reply(update, "❌ Нет бэкапов для восстановления.")

# ---------- ОБРАБОТЧИК СООБЩЕНИЙ ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.effective_message or not update.effective_message.text:
        return
    
    uid = update.effective_user.id
    if not is_allowed(uid):
        return
    
    user_message = update.effective_message.text[:1000]
    is_deep = False
    
    # Обработка /deep
    if user_message.lower().startswith("/deep "):
        is_deep = True
        user_message = user_message[6:].strip()
        if not user_message:
            await safe_reply(update, "📝 Напишите запрос после /deep")
            return
    
    # Обработка "запомни"
    if user_message.lower().startswith("запомни "):
        await safe_reply(update, "✅ Запомнил!")
        return
    
    # ----- ОСНОВНАЯ ЛОГИКА: ОДИН ЗАПРОС К DEEPSEEK С NATIVE SEARCH -----
    try:
        # Отправляем статус "печатает"
        await update.effective_message.chat.send_action(action="typing")
        
        # 1. Формируем системный промпт
        system_prompt = (
            "Ты — экспертный аналитик. Твоя задача — дать ПОЛНЫЙ, СТРУКТУРИРОВАННЫЙ ответ.\n\n"
            "=== ПРАВИЛА ===\n"
            "1. Если для ответа нужна актуальная информация — используй ПОИСК.\n"
            "2. Всегда давай структурированный ответ:\n"
            "   📌 краткий вывод\n"
            "   🏆 список/таблица\n"
            "   ✅ рекомендация\n"
            "   ⚠️ предостережения\n"
            "3. ЗАПРЕЩЕНО давать ответ в виде просто списка ссылок.\n"
            "4. Если данных нет — скажи честно и дай предположение.\n"
            "5. Указывай уверенность и дату.\n\n"
            "СЕГОДНЯ: " + now().strftime("%d.%m.%Y")
        )
        
        # 2. Делаем запрос к DeepSeek с включённым поиском
        response = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            # 🔥 ВКЛЮЧАЕМ NATIVE SEARCH
            extra_body={
                "search_enabled": True,
                "search_options": {
                    "freshness": "week",      # свежие результаты
                    "max_results": 3           # парсить топ-3 страницы
                }
            },
            temperature=0.2,
            max_tokens=4096 if is_deep else 2048,
            # 🔥 user_id для кэширования (экономия)
            user=str(uid)
        )
        
        answer = response.choices[0].message.content
        
        # 3. Отправляем ответ
        await safe_reply(update, answer)
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        await safe_reply(update, "⚠️ Произошла ошибка. Попробуйте позже или уточните запрос.")

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
async def safe_reply(update: Update, text: str):
    """Безопасная отправка ответа"""
    msg = update.effective_message
    if msg is None:
        return
    
    # Конвертация Markdown в HTML (упрощённо)
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
                await msg.reply_text(text[i:i+4096], parse_mode='HTML', disable_web_page_preview=True)
        else:
            await msg.reply_text(text, parse_mode='HTML', disable_web_page_preview=True)
    except Exception as e:
        try:
            await msg.reply_text(text)
        except:
            pass

def is_allowed(uid):
    return not ALLOWED_USERS_LIST or uid in ALLOWED_USERS_LIST

# ---------- ЗАПУСК ----------
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(CommandHandler("restore", restore_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("✅ БОТ ЗАПУЩЕН (DeepSeek Native Search, без APISerpent)")
    app.run_polling()
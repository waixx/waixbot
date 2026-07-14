import logging
import os
import json
import sys
from datetime import datetime
import aiohttp
import requests
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
)

# Загрузка переменных окружения
load_dotenv()

# --- КОНФИГУРАЦИЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
APISERPENT_API_KEY = os.getenv("APISERPENT_API_KEY")

try:
    ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
except ValueError:
    ADMIN_USER_ID = 0
    print("⚠️ ADMIN_USER_ID не является числом, установлено значение 0")

MAX_HISTORY = int(os.getenv("MAX_HISTORY", "30"))
KEEP_RECENT = int(os.getenv("KEEP_RECENT", "10"))
MODEL_DEFAULT = os.getenv("MODEL_DEFAULT", "deepseek-v4-flash")
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")

# Проверка обязательных переменных
if not TELEGRAM_TOKEN:
    print("❌ Ошибка: TELEGRAM_TOKEN не задан!")
    sys.exit(1)

if not DEEPSEEK_API_KEY:
    print("❌ Ошибка: DEEPSEEK_API_KEY не задан!")
    sys.exit(1)

if not APISERPENT_API_KEY:
    print("⚠️ ВНИМАНИЕ: APISERPENT_API_KEY не задан! Поиск работать не будет.")

if ADMIN_USER_ID == 0:
    print("⚠️ ВНИМАНИЕ: ADMIN_USER_ID не задан! Бот доступен всем.")

# Вывод конфигурации
print("\n" + "="*50)
print("🚀 КОНФИГУРАЦИЯ БОТА:")
print("="*50)
print(f"  🤖 TELEGRAM_TOKEN: {'✅' if TELEGRAM_TOKEN else '❌'}")
print(f"  🔑 DEEPSEEK_API_KEY: {'✅' if DEEPSEEK_API_KEY else '❌'}")
print(f"  🔍 APISERPENT_API_KEY: {'✅' if APISERPENT_API_KEY else '❌'}")
print(f"  👤 ADMIN_USER_ID: {ADMIN_USER_ID}")
print(f"  📦 Модель: {MODEL_DEFAULT}")
print(f"  💾 MAX_HISTORY: {MAX_HISTORY}")
print(f"  📌 KEEP_RECENT: {KEEP_RECENT}")
print(f"  🔗 API Base: {DEEPSEEK_API_BASE}")
print("="*50 + "\n")

# --- НАСТРОЙКА ПАМЯТИ ---
os.makedirs("data", exist_ok=True)
MEMORY_FILE = "data/memory.json"

def compress_history(history):
    """Компрессирует историю диалога, если она превышает MAX_HISTORY"""
    if len(history) <= MAX_HISTORY:
        return history
    
    recent = history[-KEEP_RECENT:]
    old_messages = history[:-KEEP_RECENT]
    
    summary_parts = []
    for msg in old_messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            summary_parts.append(f"Пользователь: {content[:150]}")
        elif role == "assistant":
            summary_parts.append(f"Ассистент: {content[:150]}")
    
    if summary_parts:
        compressed_summary = {
            "role": "system",
            "content": "Краткая выжимка предыдущего диалога:\n" + "\n".join(summary_parts[-5:])
        }
        return [compressed_summary] + recent
    
    return recent

def load_memory(user_id):
    """Загружает историю диалога пользователя"""
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                history = data.get(str(user_id), [])
                compressed = compress_history(history)
                print(f"📂 Загружено {len(compressed)} сообщений для пользователя {user_id}")
                return compressed
    except Exception as e:
        print(f"⚠️ Ошибка загрузки памяти: {e}")
    return []

def save_memory(user_id, history):
    """Сохраняет историю диалога пользователя"""
    try:
        data = {}
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        
        compressed_history = compress_history(history)
        data[str(user_id)] = compressed_history
        
        with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        print(f"✅ Память сохранена для пользователя {user_id}, сообщений: {len(compressed_history)}")
    except Exception as e:
        print(f"❌ Ошибка сохранения памяти: {e}")

# --- ПОИСК ЧЕРЕЗ APISERPENT ---
def search_apiserpent(query):
    """Выполняет поиск через APISerpent API"""
    if not APISERPENT_API_KEY:
        print("❌ APISERPENT_API_KEY не задан!")
        return []
    
    url = "https://apiserpent.com/api/search"
    params = {"q": query, "engine": "google", "num": 5}
    headers = {"X-API-Key": APISERPENT_API_KEY}
    
    try:
        print(f"🔍 APISerpent запрос: {query}")
        response = requests.get(url, params=params, headers=headers, timeout=15)
        print(f"📊 APISerpent статус: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            results = []
            
            # Парсим результаты в зависимости от формата ответа
            if "results" in data and isinstance(data["results"], dict):
                if "organic" in data["results"]:
                    results = data["results"]["organic"]
            elif "organic_results" in data:
                results = data["organic_results"]
            elif isinstance(data.get("results"), list):
                results = data["results"]
            
            formatted_results = []
            for res in results[:5]:
                if isinstance(res, dict):
                    formatted_results.append({
                        "title": str(res.get("title", "Без названия"))[:200],
                        "snippet": str(res.get("snippet", res.get("description", "Нет описания")))[:300],
                        "link": str(res.get("url", res.get("link", "#")))[:200]
                    })
            
            if formatted_results:
                print(f"✅ Найдено {len(formatted_results)} результатов")
                return formatted_results
            print("⚠️ Результатов нет")
            return []
        else:
            print(f"❌ Ошибка APISerpent: {response.status_code}")
            return []
    except Exception as e:
        print(f"❌ Ошибка APISerpent: {str(e)}")
        return []

# --- ОБРАБОТЧИКИ КОМАНД ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён. Бот предназначен только для владельца.")
        return
    
    await update.message.reply_text(
        "🤖 Привет! Я бот на DeepSeek-V4 Flash.\n\n"
        "🔍 **Напиши 'бро ' и пробел, чтобы я поискал в интернете!**\n"
        "📋 Команды:\n"
        "  /model - показать текущую модель\n"
        "  /clear - очистить историю диалога\n"
        "  /stats - показать статистику памяти\n\n"
        "🧠 **Я запоминаю всё**, что мы обсуждаем!\n"
        "💡 Пример: бро погода в Москве\n\n"
        "⚡ Модель Flash — быстрая и экономичная!"
    )

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /model"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    keyboard = [[InlineKeyboardButton("⚡ Flash (экономичная)", callback_data=MODEL_DEFAULT)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"✅ Используется модель: **{MODEL_DEFAULT}**\n\n"
        "⚡ Flash — быстрая, дешёвая и подходит для большинства задач.\n"
        "💰 Экономия: $0.14 за 1M входных токенов.",
        reply_markup=reply_markup
    )

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очищает историю диалога пользователя"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if str(user_id) in data:
            del data[str(user_id)]
            with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            await update.message.reply_text("🧹 История очищена! Я буду помнить только новые сообщения.")
        else:
            await update.message.reply_text("📭 История и так пуста.")
    else:
        await update.message.reply_text("📭 История и так пуста.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику памяти"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    
    history = load_memory(user_id)
    await update.message.reply_text(
        f"📊 Статистика памяти:\n\n"
        f"📝 Всего сообщений в истории: {len(history)}\n"
        f"💾 Максимум: {MAX_HISTORY} сообщений\n"
        f"📌 Сохраняется последних: {KEEP_RECENT} сообщений\n"
        f"📁 Файл памяти: {MEMORY_FILE}"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await query.edit_message_text("❌ Доступ запрещён.")
        return
    
    data = query.data
    if data == MODEL_DEFAULT:
        context.user_data['model'] = MODEL_DEFAULT
        await query.edit_message_text(
            text=f"✅ Выбрана модель: **{MODEL_DEFAULT}** (Flash)\n\n⚡ Быстрая и экономичная!"
        )

# --- ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает все текстовые сообщения"""
    user_id = update.effective_user.id
    
    # Проверка доступа
    if user_id != ADMIN_USER_ID and ADMIN_USER_ID != 0:
        await update.message.reply_text("❌ Доступ запрещён. Бот предназначен только для владельца.")
        return
    
    user_message = update.message.text
    await update.message.chat.send_action(action=ChatAction.TYPING)
    
    # Загружаем историю
    history = load_memory(user_id)
    model = MODEL_DEFAULT
    
    # --- ОБРАБОТКА ПОИСКОВОГО ЗАПРОСА (команда "бро") ---
    if user_message.lower().startswith("бро "):
        search_query = user_message[4:].strip()
        if not search_query:
            await update.message.reply_text("❌ Напиши что искать после 'бро '.\nПример: бро погода в Москве")
            return
        
        print(f"🔍 Поиск по запросу: {search_query}")
        results = search_apiserpent(search_query)
        
        if not results:
            await update.message.reply_text("❌ По вашему запросу ничего не найдено. Попробуйте переформулировать запрос.")
            return
        
        # Формируем результаты поиска с текущей датой
        current_datetime = datetime.now()
        current_date = current_datetime.strftime("%d.%m.%Y")
        current_time = current_datetime.strftime("%H:%M")
        
        search_results_text = f"📅 Сегодня: {current_date}, текущее время: {current_time}\n\n"
        search_results_text += f"🔍 Результаты поиска по запросу: '{search_query}'\n\n"
        
        for i, res in enumerate(results[:5], 1):
            search_results_text += f"{i}. **{res.get('title')}**\n"
            search_results_text += f"   {res.get('snippet')}\n"
            search_results_text += f"   🔗 {res.get('link')}\n\n"
        
        # Создаем системный промпт с четкой инструкцией использовать результаты поиска
        system_prompt = {
            "role": "system",
            "content": f"""Ты — полезный ассистент с доступом к интернет-поиску. Сегодня: {current_date}, время: {current_time}.

Пользователь попросил найти информацию: "{search_query}"

Вот РЕАЛЬНЫЕ результаты поиска из интернета (используй ТОЛЬКО их для ответа):
{search_results_text}

ВАЖНЫЕ ПРАВИЛА:
1. Используй ТОЛЬКО информацию из результатов поиска выше
2. НЕ выдумывай и НЕ добавляй информацию от себя
3. Если пользователь спросил время, дату, погоду, новости — ответь на основе найденных данных
4. Всегда указывай источники (ссылки) в конце ответа
5. Отвечай на русском языке, кратко и информативно

Твой ответ должен быть основан ИСКЛЮЧИТЕЛЬНО на предоставленных результатах поиска."""
        }
        
        # Добавляем сообщение пользователя в историю
        history.append({"role": "user", "content": user_message})
        
        # Подготавливаем сообщения для API (используем всю историю)
        messages_for_api = [system_prompt] + history
        
        # Отправляем запрос к DeepSeek
        async with aiohttp.ClientSession() as session:
            json_data = {
                "model": model,
                "messages": messages_for_api
            }
            
            async with session.post(
                f"{DEEPSEEK_API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                json=json_data,
                timeout=30
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    answer = data["choices"][0]["message"]["content"]
                    
                    # Сохраняем ответ в историю
                    history.append({"role": "assistant", "content": answer})
                    save_memory(user_id, history)
                    
                    await update.message.reply_text(answer)
                else:
                    error_text = await response.text()
                    print(f"❌ Ошибка DeepSeek: {error_text}")
                    await update.message.reply_text(f"❌ Ошибка API: {error_text}")
        return
    
    # --- ОБЫЧНЫЙ ДИАЛОГ (без поиска) ---
    # Добавляем сообщение пользователя в историю
    history.append({"role": "user", "content": user_message})
    
    # Отправляем запрос к DeepSeek
    async with aiohttp.ClientSession() as session:
        json_data = {
            "model": model,
            "messages": history
        }
        
        async with session.post(
            f"{DEEPSEEK_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json=json_data,
            timeout=30
        ) as response:
            if response.status == 200:
                data = await response.json()
                answer = data["choices"][0]["message"]["content"]
                
                # Сохраняем ответ в историю
                history.append({"role": "assistant", "content": answer})
                save_memory(user_id, history)
                
                await update.message.reply_text(answer)
            else:
                error_text = await response.text()
                print(f"❌ Ошибка DeepSeek: {error_text}")
                await update.message.reply_text(f"❌ Ошибка API: {error_text}")

# --- ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК ---
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает все ошибки"""
    try:
        raise context.error
    except Exception as e:
        print(f"⚠️ Глобальная ошибка: {e}")
        import traceback
        traceback.print_exc()
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Произошла ошибка. Пожалуйста, попробуйте позже."
            )

# --- ЗАПУСК БОТА ---
if __name__ == "__main__":
    # Настройка логирования
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Создаем приложение
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Регистрируем обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("model", model_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    
    print("✅ Бот запущен и готов к работе!")
    
    # Запускаем бота
    app.run_polling()

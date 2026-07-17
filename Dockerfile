FROM python:3.11-slim

WORKDIR /app

# Устанавливаем только базовые утилиты (wget и gnupg нужны для некоторых операций)
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Копируем и устанавливаем Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Устанавливаем Playwright и браузер Chromium с автоматической установкой системных зависимостей
RUN pip install playwright && \
    playwright install chromium && \
    playwright install-deps

# Копируем код бота
COPY . .

CMD ["python", "bot.py"]

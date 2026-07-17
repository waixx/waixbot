FROM python:3.11-slim

WORKDIR /app

# Устанавливаем все системные зависимости для Playwright
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    libnss3 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxkbcommon0 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libglib2.0-0 \
    libgobject-2.0-0 \
    libgdk-pixbuf2.0-0 \
    libgtk-3-0 \
    libdbus-1-3 \
    libnspr4 \
    libnssutil3 \
    libsmime3 \
    libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Устанавливаем Playwright и браузер Chromium
RUN pip install playwright
RUN playwright install chromium

# Копируем код бота
COPY . .

CMD ["python", "bot.py"]

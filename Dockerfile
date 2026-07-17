FROM ubuntu:22.04

# Устанавливаем Python и необходимые системные библиотеки
RUN apt-get update && apt-get install -y \
    python3.11 \
    python3-pip \
    python3.11-venv \
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
    libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем ссылку python → python3
RUN ln -s /usr/bin/python3.11 /usr/bin/python

WORKDIR /app

# Устанавливаем Python-зависимости
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Устанавливаем Playwright и браузер
RUN pip3 install playwright
RUN python -m playwright install chromium

# Копируем код бота
COPY . .

CMD ["python", "bot.py"]

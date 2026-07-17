FROM ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive

# Устанавливаем Python 3.11 из официального репозитория deadsnakes
RUN apt-get update && apt-get install -y software-properties-common \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y \
    python3.11 python3.11-dev python3.11-venv python3-pip \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

# Устанавливаем все системные библиотеки, необходимые для Playwright
RUN apt-get install -y \
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
    libdrm2 \
    libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Устанавливаем Playwright и браузер (без install-deps, т.к. зависимости уже установлены)
RUN pip3 install playwright && \
    playwright install chromium

COPY . .

CMD ["python", "bot.py"]

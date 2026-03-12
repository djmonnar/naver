FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    wget gnupg curl \
    fonts-unifont fonts-freefont-ttf \
    libglib2.0-0t64 libnss3 libnspr4 libatk1.0-0t64 \
    libatk-bridge2.0-0t64 libcups2t64 libdrm2 libdbus-1-3 \
    libxcb1 libxkbcommon0 libx11-6 libxcomposite1 \
    libxdamage1 libxext6 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2t64 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install chromium

COPY . .

EXPOSE 8000

CMD ["python", "main.py"]

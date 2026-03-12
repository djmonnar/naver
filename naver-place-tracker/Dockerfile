FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "main.py"]
```

**2️⃣ `requirements.txt`** → playwright 줄 삭제
```
fastapi==0.111.0
uvicorn==0.29.0
apscheduler==3.10.4
jinja2==3.1.4
aiosqlite==0.20.0
python-multipart==0.0.9
httpx==0.27.0

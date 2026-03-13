from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime
import asyncio
import os

import database
import crawler

app = FastAPI(title="네이버 플레이스 순위 트래커")
templates = Jinja2Templates(directory="templates")

scheduler = AsyncIOScheduler(timezone="Asia/Seoul")

async def keep_alive():
    """Render 무료 플랜 슬립 방지 - 14분마다 자기 자신에게 핑"""
    try:
        port = int(os.environ.get("PORT", 8000))
        async with httpx.AsyncClient() as client:
            await client.get(f"http://localhost:{port}/ping", timeout=10)
        print("💓 Keep-alive ping 전송")
    except Exception as e:
        print(f"Keep-alive 실패 (무시): {e}")

@app.get("/ping")
async def ping():
    return {"status": "ok"}

@app.on_event("startup")
async def startup():
    await database.init_db()
    scheduler.add_job(
        crawler.run_daily_check,
        CronTrigger(hour=9, minute=0, timezone="Asia/Seoul"),
        id="daily_check",
        replace_existing=True
    )
    scheduler.add_job(
        keep_alive,
        "interval",
        minutes=14,
        id="keep_alive",
        replace_existing=True
    )
    scheduler.start()
    print("스케줄러 시작 - 매일 오전 9시 자동 체크 + 슬립 방지 활성화")

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()

# ─── 메인 대시보드 ───────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    places = await database.get_places()
    keywords = await database.get_keywords()
    latest = await database.get_latest_rankings()
    rankings_30 = await database.get_rankings(days=30)

    chart_data = {}
    for row in rankings_30:
        key = f"{row['place_name'] or row['place_id']} | {row['keyword']}"
        if key not in chart_data:
            chart_data[key] = {"dates": [], "ranks": [], "color": None}
        if row['date'] not in chart_data[key]["dates"]:
            chart_data[key]["dates"].append(row['date'])
            chart_data[key]["ranks"].append(row['rank'] if row['rank'] > 0 else None)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "places": places,
        "keywords": keywords,
        "latest": latest,
        "chart_data": chart_data,
        "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })

# ─── 플레이스 관리 ───────────────────────────────────────
@app.post("/places/add")
async def add_place(place_id: str = Form(...), place_name: str = Form(...)):
    place_id = place_id.strip()
    place_name = place_name.strip()
    if place_id:
        await database.add_place(place_id, place_name)
    return RedirectResponse("/", status_code=303)

@app.post("/places/delete")
async def del_place(place_id: str = Form(...)):
    await database.delete_place(place_id)
    return RedirectResponse("/", status_code=303)

# ─── 키워드 관리 ─────────────────────────────────────────
@app.post("/keywords/add")
async def add_keyword(keyword: str = Form(...)):
    keyword = keyword.strip()
    if keyword:
        await database.add_keyword(keyword)
    return RedirectResponse("/", status_code=303)

@app.post("/keywords/delete")
async def del_keyword(keyword: str = Form(...)):
    await database.delete_keyword(keyword)
    return RedirectResponse("/", status_code=303)

# ─── 수동 체크 ───────────────────────────────────────────
@app.post("/check/now")
async def check_now(background_tasks: BackgroundTasks):
    background_tasks.add_task(crawler.run_daily_check)
    return JSONResponse({"status": "started", "message": "순위 체크를 시작했습니다. 완료까지 수분이 걸릴 수 있습니다."})

@app.get("/check/status")
async def check_status():
    latest = await database.get_latest_rankings()
    return JSONResponse({"latest": latest})

# ─── 고객용 리포트 ──────────────────────────────────────────
@app.get("/report", response_class=HTMLResponse)
async def report(request: Request):
    places = await database.get_places()
    keywords = await database.get_keywords()
    rankings_30 = await database.get_rankings(days=30)
    today = datetime.now().strftime("%Y년 %m월 %d일")

    chart_labels_set = sorted(set(r['date'] for r in rankings_30))
    chart_datasets = []
    for place in places:
        for kw in keywords:
            label = f"{place['place_name']} | {kw['keyword']}"
            data_map = {r['date']: r['rank'] for r in rankings_30
                        if r['place_id'] == place['place_id'] and r['keyword'] == kw['keyword']}
            data = [data_map.get(d) for d in chart_labels_set]
            chart_datasets.append({"label": label, "data": data})

    summary = []
    for place in places:
        for kw in keywords:
            rows = [r for r in rankings_30
                    if r['place_id'] == place['place_id'] and r['keyword'] == kw['keyword']]
            rows_sorted = sorted(rows, key=lambda x: x['date'], reverse=True)
            today_rank = rows_sorted[0]['rank'] if rows_sorted else -1
            prev_rank = rows_sorted[1]['rank'] if len(rows_sorted) > 1 else None
            week_rank = rows_sorted[6]['rank'] if len(rows_sorted) > 6 else None
            change_day = (today_rank - prev_rank) if (prev_rank and today_rank > 0) else None
            change_week = (today_rank - week_rank) if (week_rank and today_rank > 0) else None
            summary = [
                {"label": "오늘 순위", "rank": today_rank, "change": None},
                {"label": "어제 순위", "rank": prev_rank or -1, "change": change_day},
                {"label": "7일 전", "rank": week_rank or -1, "change": change_week},
            ]

    history = []
    for i, row in enumerate(rankings_30[:20]):
        change = None
        next_rows = [r for r in rankings_30
                     if r['place_id'] == row['place_id']
                     and r['keyword'] == row['keyword']
                     and r['date'] < row['date']]
        if next_rows:
            prev = sorted(next_rows, key=lambda x: x['date'], reverse=True)[0]
            if row['rank'] > 0 and prev['rank'] > 0:
                change = row['rank'] - prev['rank']
        history.append({**row, "change": change})

    return templates.TemplateResponse("report.html", {
        "request": request,
        "places": places,
        "keywords": keywords,
        "today": today,
        "summary": summary,
        "history": history,
        "chart_data": {
            "labels": [l[5:] for l in chart_labels_set],
            "datasets": chart_datasets
        },
    })

# ─── API ─────────────────────────────────────────────────
@app.get("/api/rankings")
async def api_rankings(place_id: str = None, keyword: str = None, days: int = 30):
    data = await database.get_rankings(place_id=place_id, keyword=keyword, days=days)
    return JSONResponse(data)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

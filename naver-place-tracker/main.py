from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timedelta
import asyncio
import os

import database
import crawler

app = FastAPI(title="네이버 플레이스 순위 트래커")
templates = Jinja2Templates(directory="templates")
scheduler = AsyncIOScheduler(timezone="Asia/Seoul")

async def keep_alive():
    try:
        import httpx
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
    scheduler.add_job(crawler.run_daily_check, CronTrigger(hour=9, minute=0, timezone="Asia/Seoul"), id="daily_check", replace_existing=True)
    scheduler.add_job(keep_alive, "interval", minutes=14, id="keep_alive", replace_existing=True)
    scheduler.start()
    print("스케줄러 시작 - 매일 오전 9시 자동 체크 + 슬립 방지 활성화")

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()

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

@app.post("/check/now")
async def check_now(background_tasks: BackgroundTasks):
    background_tasks.add_task(crawler.run_daily_check)
    return JSONResponse({"status": "started", "message": "순위 체크를 시작했습니다."})

@app.get("/check/status")
async def check_status():
    latest = await database.get_latest_rankings()
    return JSONResponse({"latest": latest})

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

    # 달력 데이터 (최근 30일)
    today_dt = datetime.now()
    date_range = [(today_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(29, -1, -1)]

    calendar = []
    for place in places:
        for kw in keywords:
            rows = [r for r in rankings_30
                    if r['place_id'] == place['place_id'] and r['keyword'] == kw['keyword']]
            data_map = {r['date']: r['rank'] for r in rows}
            days = []
            for d in date_range:
                rank = data_map.get(d, -1)
                prev_d = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
                prev_rank = data_map.get(prev_d)
                change = None
                if prev_rank and rank > 0:
                    change = rank - prev_rank
                days.append({"date": d[5:], "rank": rank, "change": change})
            calendar.append({
                "place_name": place['place_name'],
                "keyword": kw['keyword'],
                "days": days
            })

    return templates.TemplateResponse("report.html", {
        "request": request,
        "places": places,
        "keywords": keywords,
        "today": today,
        "summary": summary,
        "calendar": calendar,
        "chart_data": {
            "labels": [l[5:] for l in chart_labels_set],
            "datasets": chart_datasets
        },
    })

@app.get("/api/rankings")
async def api_rankings(place_id: str = None, keyword: str = None, days: int = 30):
    data = await database.get_rankings(place_id=place_id, keyword=keyword, days=days)
    return JSONResponse(data)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

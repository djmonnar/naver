from fastapi import FastAPI, Request, Form, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import asyncio, sqlite3, os

import database
import crawler

app = FastAPI(title="네이버 플레이스 순위 트래커 + 연해주 예약")
templates = Jinja2Templates(directory="templates")
scheduler = AsyncIOScheduler(timezone="Asia/Seoul")

# CORS (예약 페이지용)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════════════
#  연해주 예약 DB  (Render Disk 마운트 경로)
# ═══════════════════════════════════════════════
RES_DB = "/app/data/reservations.db"

def get_res_db():
    conn = sqlite3.connect(RES_DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_res_db():
    os.makedirs(os.path.dirname(RES_DB), exist_ok=True)
    conn = get_res_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reservations (
            id      TEXT PRIMARY KEY,
            date    TEXT NOT NULL,
            time    TEXT NOT NULL,
            name    TEXT NOT NULL,
            adult   INTEGER DEFAULT 0,
            child   INTEGER DEFAULT 0,
            cset    INTEGER DEFAULT 0,
            menu    TEXT DEFAULT '',
            note    TEXT DEFAULT '',
            created TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.commit()
    conn.close()

class Reservation(BaseModel):
    id: str
    date: str
    time: str
    name: str
    adult: int = 0
    child: int = 0
    cset: int = 0
    menu: str = ""
    note: str = ""

# ═══════════════════════════════════════════════
#  기존 네이버 트래커
# ═══════════════════════════════════════════════
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
    init_res_db()
    scheduler.add_job(crawler.run_daily_check, CronTrigger(hour=9, minute=0, timezone="Asia/Seoul"), id="daily_check", replace_existing=True)
    scheduler.add_job(keep_alive, "interval", minutes=14, id="keep_alive", replace_existing=True)
    scheduler.start()
    print("스케줄러 시작")

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
        "request": request, "places": places, "keywords": keywords,
        "latest": latest, "chart_data": chart_data,
        "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })

@app.post("/places/add")
async def add_place(place_id: str = Form(...), place_name: str = Form(...)):
    if place_id.strip():
        await database.add_place(place_id.strip(), place_name.strip())
    return RedirectResponse("/", status_code=303)

@app.post("/places/delete")
async def del_place(place_id: str = Form(...)):
    await database.delete_place(place_id)
    return RedirectResponse("/", status_code=303)

@app.post("/keywords/add")
async def add_keyword(keyword: str = Form(...)):
    if keyword.strip():
        await database.add_keyword(keyword.strip())
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
            chart_datasets.append({"label": label, "data": [data_map.get(d) for d in chart_labels_set]})
    summary_by_kw = {}
    for kw in keywords:
        kw_name = kw['keyword']
        for place in places:
            rows = sorted([r for r in rankings_30 if r['place_id'] == place['place_id'] and r['keyword'] == kw_name],
                          key=lambda x: x['date'], reverse=True)
            today_rank = rows[0]['rank'] if rows else -1
            prev_rank  = rows[1]['rank'] if len(rows) > 1 else None
            week_rank  = rows[6]['rank'] if len(rows) > 6 else None
            summary_by_kw[kw_name] = [
                {"label": "오늘 순위", "rank": today_rank, "change": None},
                {"label": "어제 순위", "rank": prev_rank or -1, "change": (today_rank-prev_rank) if (prev_rank and today_rank>0) else None},
                {"label": "7일 전",   "rank": week_rank or -1, "change": (today_rank-week_rank) if (week_rank and today_rank>0) else None},
            ]
    calendar = []
    for place in places:
        for kw in keywords:
            rows = [r for r in rankings_30 if r['place_id']==place['place_id'] and r['keyword']==kw['keyword'] and r['rank']>0]
            seen = {r['date']: r['rank'] for r in sorted(rows, key=lambda x: x['date'])}
            if not seen: continue
            dates_sorted = sorted(seen.keys())
            days = []
            for i, d in enumerate(dates_sorted):
                rank = seen[d]
                prev = seen[dates_sorted[i-1]] if i > 0 else None
                days.append({"date": d[5:], "rank": rank, "change": (rank-prev) if prev else None})
            calendar.append({"place_name": place['place_name'], "keyword": kw['keyword'], "days": days})
    return templates.TemplateResponse("report.html", {
        "request": request, "places": places, "keywords": keywords,
        "today": today, "summary_by_kw": summary_by_kw, "calendar": calendar,
        "chart_data": {"labels": [l[5:] for l in chart_labels_set], "datasets": chart_datasets},
    })

@app.get("/api/rankings")
async def api_rankings(place_id: str = None, keyword: str = None, days: int = 30):
    return JSONResponse(await database.get_rankings(place_id=place_id, keyword=keyword, days=days))

# ═══════════════════════════════════════════════
#  연해주 예약 페이지
# ═══════════════════════════════════════════════
@app.get("/reservation", response_class=HTMLResponse)
async def reservation_page():
    return FileResponse("reservation.html")

# ═══════════════════════════════════════════════
#  연해주 예약 REST API
# ═══════════════════════════════════════════════
@app.get("/api/reservations")
def get_reservations(date: Optional[str] = None):
    conn = get_res_db()
    rows = conn.execute(
        "SELECT * FROM reservations WHERE date=? ORDER BY time" if date else
        "SELECT * FROM reservations ORDER BY date, time",
        (date,) if date else ()
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/reservations")
def add_reservation(r: Reservation):
    conn = get_res_db()
    try:
        conn.execute(
            "INSERT INTO reservations (id,date,time,name,adult,child,cset,menu,note) VALUES (?,?,?,?,?,?,?,?,?)",
            (r.id,r.date,r.time,r.name,r.adult,r.child,r.cset,r.menu,r.note)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="이미 존재하는 ID")
    conn.close()
    return {"ok": True}

@app.put("/api/reservations/{rid}")
def update_reservation(rid: str, r: Reservation):
    conn = get_res_db()
    conn.execute(
        "UPDATE reservations SET date=?,time=?,name=?,adult=?,child=?,cset=?,menu=?,note=? WHERE id=?",
        (r.date,r.time,r.name,r.adult,r.child,r.cset,r.menu,r.note,rid)
    )
    conn.commit(); conn.close()
    return {"ok": True}

@app.delete("/api/reservations/{rid}")
def delete_reservation(rid: str):
    conn = get_res_db()
    conn.execute("DELETE FROM reservations WHERE id=?", (rid,))
    conn.commit(); conn.close()
    return {"ok": True}

@app.post("/api/reservations/bulk")
def bulk_add(items: list[Reservation]):
    conn = get_res_db()
    added = updated = 0
    for r in items:
        if conn.execute("SELECT id FROM reservations WHERE id=?", (r.id,)).fetchone():
            conn.execute(
                "UPDATE reservations SET date=?,time=?,name=?,adult=?,child=?,cset=?,menu=?,note=? WHERE id=?",
                (r.date,r.time,r.name,r.adult,r.child,r.cset,r.menu,r.note,r.id)
            ); updated += 1
        else:
            conn.execute(
                "INSERT INTO reservations (id,date,time,name,adult,child,cset,menu,note) VALUES (?,?,?,?,?,?,?,?,?)",
                (r.id,r.date,r.time,r.name,r.adult,r.child,r.cset,r.menu,r.note)
            ); added += 1
    conn.commit(); conn.close()
    return {"ok": True, "added": added, "updated": updated}

# ═══════════════════════════════════════════════
#  카카오 챗봇 스킬 API
# ═══════════════════════════════════════════════
DOW_MAP = {0:"월",1:"화",2:"수",3:"목",4:"금",5:"토",6:"일"}
BASE_URL = "https://naver-0zwy.onrender.com"

def fmt_time(t):
    h, m = t.split(":")
    h = int(h)
    return f"{h-12 if h>=13 else h}:{m}"

def fmt_res(r):
    child = r.get("child",0); adult = r.get("adult",0)
    pax = f"{adult}.{child}인" if child > 0 else f"{adult}인"
    return f"{fmt_time(r['time'])} {r['name']} {pax}{' '+r['menu'] if r.get('menu') else ''}{' 세트'+str(r['cset']) if r.get('cset') else ''}{' '+r['note'] if r.get('note') else ''}"

def kakao_res(text, buttons=None):
    out = {"version":"2.0","template":{"outputs":[{"simpleText":{"text":text}}]}}
    if buttons: out["template"]["quickReplies"] = buttons
    return out

LINK_BTN = [{"label":"📋 예약 페이지","action":"webLink","webLinkUrl":f"{BASE_URL}/reservation"}]

@app.post("/api/kakao/today")
async def kakao_today():
    today = datetime.now().strftime("%Y-%m-%d")
    d = datetime.now()
    dow = DOW_MAP[d.weekday()]
    conn = get_res_db()
    rsvs = [dict(r) for r in conn.execute(
        "SELECT * FROM reservations WHERE date=? ORDER BY time", (today,)
    ).fetchall()]
    conn.close()
    if not rsvs:
        return kakao_res(f"📭 {d.month}월 {d.day}일 ({dow}) 예약이 없습니다.", LINK_BTN)
    mg = [r for r in rsvs if r["time"]<"15:00"]
    ev = [r for r in rsvs if r["time"]>="15:00"]
    tA = sum(r.get("adult",0) for r in rsvs)
    tC = sum(r.get("child",0) for r in rsvs)
    lines = [f"📅 {d.month}월 {d.day}일 ({dow}) 예약현황",
             f"총 {len(rsvs)}건 · {tA+tC}석 · 음식 {tA}인분"]
    if mg: lines += ["\n🌅 점심"] + [f"  {fmt_res(r)}" for r in mg]
    if ev: lines += ["\n🌆 저녁"] + [f"  {fmt_res(r)}" for r in ev]
    return kakao_res("\n".join(lines), LINK_BTN)

@app.post("/api/kakao/date")
async def kakao_by_date(request: Request):
    import re
    try:
        body = await request.json()
        utterance = body["userRequest"]["utterance"]
    except:
        return kakao_res("날짜를 인식하지 못했어요.\n예) '3/28 예약'으로 입력해주세요.")
    m = re.search(r"(\d{1,2})[/월](\d{1,2})", utterance)
    if not m:
        return kakao_res("날짜 형식이 맞지 않아요.\n예) '3/28 예약'으로 입력해주세요.")
    year = datetime.now().year
    month, day = int(m.group(1)), int(m.group(2))
    date_str = f"{year}-{month:02d}-{day:02d}"
    dow = DOW_MAP[datetime(year,month,day).weekday()]
    conn = get_res_db()
    rsvs = [dict(r) for r in conn.execute(
        "SELECT * FROM reservations WHERE date=? ORDER BY time", (date_str,)
    ).fetchall()]
    conn.close()
    if not rsvs:
        return kakao_res(f"📭 {month}월 {day}일 ({dow}) 예약이 없습니다.")
    mg = [r for r in rsvs if r["time"]<"15:00"]
    ev = [r for r in rsvs if r["time"]>="15:00"]
    tA = sum(r.get("adult",0) for r in rsvs)
    tC = sum(r.get("child",0) for r in rsvs)
    lines = [f"📅 {month}월 {day}일 ({dow}) 예약현황",
             f"총 {len(rsvs)}건 · {tA+tC}석 · 음식 {tA}인분"]
    if mg: lines += ["\n🌅 점심"] + [f"  {fmt_res(r)}" for r in mg]
    if ev: lines += ["\n🌆 저녁"] + [f"  {fmt_res(r)}" for r in ev]
    return kakao_res("\n".join(lines))

@app.post("/api/kakao/link")
async def kakao_link():
    return {"version":"2.0","template":{"outputs":[{"basicCard":{
        "title":"연해주 예약 관리",
        "description":"예약 추가·수정·확인을 페이지에서 바로 하세요.",
        "buttons":[{"action":"webLink","label":"📋 예약 페이지 열기",
                    "webLinkUrl":f"{BASE_URL}/reservation"}]
    }}]}}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

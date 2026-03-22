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
    init_gmail_log_db()  # Gmail 로그 테이블 초기화
    scheduler.add_job(crawler.run_daily_check, CronTrigger(hour=9, minute=0, timezone="Asia/Seoul"), id="daily_check", replace_existing=True)
    scheduler.add_job(keep_alive, "interval", minutes=14, id="keep_alive", replace_existing=True)
    # 네이버 Gmail 동기화: 10분마다
    scheduler.add_job(sync_naver_gmail, "interval", minutes=10, id="gmail_sync", replace_existing=True)
    # 자동 백업: 매일 자정
    scheduler.add_job(auto_backup, CronTrigger(hour=0, minute=0, timezone="Asia/Seoul"), id="auto_backup", replace_existing=True)
    scheduler.start()
    print("스케줄러 시작")
    # 서버 시작 시 즉시 1회 동기화 (누락 방지)
    asyncio.create_task(sync_naver_gmail())

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

def now_kst():
    """한국 시간 반환"""
    from datetime import timezone, timedelta
    KST = timezone(timedelta(hours=9))
    return datetime.now(KST)

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

def build_rsv_text(rsvs, month, day, dow):
    """예약 목록을 텍스트로 변환 (공통 함수)"""
    if not rsvs:
        return f"📭 {month}월 {day}일 ({dow}) 예약이 없습니다."
    mg = [r for r in rsvs if r["time"] < "15:00"]
    ev = [r for r in rsvs if r["time"] >= "15:00"]
    tA = sum(r.get("adult", 0) for r in rsvs)
    tC = sum(r.get("child", 0) for r in rsvs)
    lines = [f"📅 {month}월 {day}일 ({dow}) 예약현황",
             f"총 {len(rsvs)}건 · {tA+tC}석 · 음식 {tA}인분"]
    if mg: lines += ["\n🌅 점심"] + [f"  {fmt_res(r)}" for r in mg]
    if ev: lines += ["\n🌆 저녁"] + [f"  {fmt_res(r)}" for r in ev]
    return "\n".join(lines)

def build_kakao_export(rsvs, month, day, dow):
    """카톡 내보내기 형식 텍스트"""
    if not rsvs:
        return f"📭 {month}월 {day}일 ({dow}) 예약이 없습니다."
    mg = [r for r in rsvs if r["time"] < "15:00"]
    ev = [r for r in rsvs if r["time"] >= "15:00"]
    def to_line(r):
        child = r.get("child", 0); adult = r.get("adult", 0)
        pax = f"{adult}.{child}인" if child > 0 else f"{adult}인"
        h = int(r["time"].split(":")[0]); mm = r["time"].split(":")[1]
        t = f"{h-12 if h>=13 else h}:{mm}"
        return f"{month}/{day} {dow} {t} {r['name']} {pax}{' '+r['menu'] if r.get('menu') else ''}{' 세트'+str(r['cset']) if r.get('cset') else ''}{' '+r['note'] if r.get('note') else ''}".strip()
    parts = []
    if mg: parts.append("[점심]"); parts += [to_line(r) for r in mg]
    if mg and ev: parts.append("")
    if ev: parts.append("[저녁]"); parts += [to_line(r) for r in ev]
    return "\n".join(parts)

LINK_BTN = [{"label":"📋 예약 페이지","action":"webLink","webLinkUrl":f"{BASE_URL}/reservation"}]

@app.post("/api/kakao/today")
async def kakao_today(request: Request):
    d = now_kst()
    today = d.strftime("%Y-%m-%d")
    dow = DOW_MAP[d.weekday()]
    conn = get_res_db()
    rsvs = [dict(r) for r in conn.execute(
        "SELECT * FROM reservations WHERE date=? ORDER BY time", (today,)
    ).fetchall()]
    conn.close()
    text = build_rsv_text(rsvs, d.month, d.day, dow)
    btns = [
        {"label": "💬 카톡 내보내기", "action": "block", "messageText": "오늘카톡"},
        {"label": "📋 예약 페이지", "action": "webLink", "webLinkUrl": f"{BASE_URL}/reservation"}
    ]
    return kakao_res(text, btns if rsvs else LINK_BTN)

# ── 내일 예약 확인 ──────────────────────────
@app.post("/api/kakao/tomorrow")
async def kakao_tomorrow(request: Request):
    from datetime import timedelta
    d = now_kst() + timedelta(days=1)
    tmr = d.strftime("%Y-%m-%d")
    dow = DOW_MAP[d.weekday()]
    conn = get_res_db()
    rsvs = [dict(r) for r in conn.execute(
        "SELECT * FROM reservations WHERE date=? ORDER BY time", (tmr,)
    ).fetchall()]
    conn.close()
    text = build_rsv_text(rsvs, d.month, d.day, dow)
    btns = [
        {"label": "📋 예약 페이지", "action": "webLink", "webLinkUrl": f"{BASE_URL}/reservation"}
    ]
    return kakao_res(text, btns)

# ── 오늘 카톡 내보내기 ───────────────────────
@app.post("/api/kakao/export")
async def kakao_export(request: Request):
    d = now_kst()
    today = d.strftime("%Y-%m-%d")
    dow = DOW_MAP[d.weekday()]
    conn = get_res_db()
    rsvs = [dict(r) for r in conn.execute(
        "SELECT * FROM reservations WHERE date=? ORDER BY time", (today,)
    ).fetchall()]
    conn.close()
    if not rsvs:
        return kakao_res(f"📭 {d.month}월 {d.day}일 ({dow}) 예약이 없습니다.", LINK_BTN)
    text = build_kakao_export(rsvs, d.month, d.day, dow)
    # 카톡 내보내기는 텍스트만 — 그대로 복사해서 공유방에 붙여넣기
    return kakao_res(
        f"📋 {d.month}월 {d.day}일 ({dow}) 카톡 내보내기\n아래 내용을 복사해서 공유방에 붙여넣으세요:\n\n{text}",
        LINK_BTN
    )

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
    year = now_kst().year
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
async def kakao_link(request: Request):
    return {"version":"2.0","template":{"outputs":[{"basicCard":{
        "title":"연해주 예약 관리",
        "description":"예약 추가·수정·확인을 페이지에서 바로 하세요.",
        "buttons":[{"action":"webLink","label":"📋 예약 페이지 열기",
                    "webLinkUrl":f"{BASE_URL}/reservation"}]
    }}]}}

# ═══════════════════════════════════════════════
#  카카오 챗봇 - 예약 자동 감지 & 등록
# ═══════════════════════════════════════════════
def parse_reservation_msg(text: str):
    """
    카톡 예약 메시지 파싱
    예) "4/4 토 12:30 박성은님 10.4인 시그 세트2 돌상탁자만 준비"
    """
    import re
    t = text.strip()

    date_m = re.search(r'(\d{1,2})[/월](\d{1,2})', t)
    time_m = re.search(r'(\d{1,2}):(\d{2})', t)
    pax_m  = re.search(r'(\d+(?:\.\d+)?)인', t)
    name_m = re.search(r'([가-힣A-Za-z0-9()（）]{2,10}님)', t)

    if not (date_m and time_m and pax_m and name_m):
        return None

    # 취소 여부
    is_cancel = t.endswith('취소') or '취소' in t.split()[-1:]

    # ── 메뉴 파싱 ──────────────────────────────
    MENU_MAP = {
        '시그니처': '시그', '시그': '시그',
        '스페셜': '스페셜', '스페샬': '스페셜', '스패셜': '스페셜',
        '수패셜': '스페셜', '사페셜': '스페셜',
        '스시': '스시', '수시': '스시',
        '민물': '민물장어', '장어': '민물장어',
        '미정': '미정',
        '의자시그': '시그',  # 의자시그 → 시그 (아기의자는 note로)
    }

    menu = ''
    menu_note = []

    # 혼합 메뉴 감지 (예: 스시2 민물2)
    mixed = re.findall(r'(시그니처|시그|스페셜|스페샬|스시|민물|장어|미정)(\d+)', t)
    if len(mixed) >= 2:
        menu = '+'.join([f"{MENU_MAP.get(m,m)}{n}" for m,n in mixed])
    else:
        for key, val in MENU_MAP.items():
            if key in t:
                menu = val
                if key == '의자시그':
                    menu_note.append('아기의자')
                break
        if not menu:
            menu = '시그'  # 기본값

    # ── 사이드 메뉴 파싱 ──────────────────────
    # 사추 (사시미추가) - 숫자 있으면 그 수, 없으면 인원수
    sa_m = re.search(r'사추(\d+)?', t)
    if sa_m:
        sa_n = sa_m.group(1) if sa_m.group(1) else ''
        menu_note.append(f"사시미추가{sa_n}" if sa_n else "사시미추가")

    # 우추 (우니추가)
    if '우추' in t:
        u_m = re.search(r'우추(\d+)?', t)
        u_n = u_m.group(1) if u_m and u_m.group(1) else ''
        menu_note.append(f"우니추가{u_n}" if u_n else "우니추가")

    # ── 세트 ────────────────────────────────────
    set_m = re.search(r'세트(\d+)', t)
    cset = int(set_m.group(1)) if set_m else 0

    # ── 비고 ────────────────────────────────────
    # 메뉴/사이드/세트/예약정보 이후 남은 텍스트
    note_text = t
    for strip_pat in [
        date_m.group(0), time_m.group(0), pax_m.group(0), name_m.group(0),
        r'[월화수목금토일]', r'시그니처|의자시그|시그|스페셜|스페샬|스시|민물|장어|미정',
        r'사추\d*', r'우추\d*', r'세트\d+', r'취소$'
    ]:
        note_text = re.sub(strip_pat, '', note_text).strip()
    note_text = re.sub(r'\s+', ' ', note_text).strip()

    # 모든 비고 합치기
    all_notes = menu_note[:]
    if note_text:
        all_notes.append(note_text)
    note = ' '.join(all_notes)

    # ── 날짜/시간 ──────────────────────────────
    now = now_kst()
    year = now.year
    month = int(date_m.group(1))
    day   = int(date_m.group(2))

    # 연도 넘김 처리
    # 예) 12월에 2월 예약 입력 → 2027년 2월로 처리
    if now.month >= 10 and month <= 3:
        year += 1  # 연말에 내년 초 예약

    date_str = f"{year}-{month:02d}-{day:02d}"

    raw_h = int(time_m.group(1))
    hh = raw_h + 12 if 1 <= raw_h <= 9 else raw_h
    time_str = f"{hh:02d}:{time_m.group(2)}"

    pax_f = float(pax_m.group(1))
    adult = int(pax_f)
    child = round((pax_f - adult) * 10)

    return {
        'date': date_str, 'time': time_str,
        'name': name_m.group(1),
        'adult': adult, 'child': child, 'cset': cset,
        'menu': menu, 'note': note,
        'month': month, 'day': day,
        'is_cancel': is_cancel,
    }

@app.post("/api/kakao/auto")
async def kakao_auto(request: Request):
    """
    사용자 발화에서 예약 메시지 감지 → 자동 등록
    빠른 응답을 위해 파싱 먼저, DB 저장은 백그라운드
    """
    import re, time, random

    try:
        body = await request.json()
        utterance = body["userRequest"]["utterance"]
    except:
        return kakao_res("메시지를 읽을 수 없어요.")

    parsed = parse_reservation_msg(utterance)

    # 날짜만 있는 경우 → 날짜 예약 조회
    import re as _re
    date_only = _re.search(r'(\d{1,2})[/월](\d{1,2})', utterance)
    is_date_query = date_only and ('예약' in utterance or '확인' in utterance) and not parsed

    if is_date_query:
        year = now_kst().year
        month, day = int(date_only.group(1)), int(date_only.group(2))
        date_str = f"{year}-{month:02d}-{day:02d}"
        dow = DOW_MAP[datetime(year, month, day).weekday()]
        conn = get_res_db()
        rsvs = [dict(r) for r in conn.execute(
            "SELECT * FROM reservations WHERE date=? ORDER BY time", (date_str,)
        ).fetchall()]
        conn.close()
        text = build_rsv_text(rsvs, month, day, dow)
        return kakao_res(text, LINK_BTN)

    # 예약 형식이 아니면 즉시 안내 반환
    if not parsed:
        return kakao_res(
            "안녕하세요 연해주입니다 😊\n원하시는 항목을 선택해주세요.",
            [
                {"label": "📅 오늘 예약 확인", "action": "block", "messageText": "오늘예약확인"},
                {"label": "📋 예약 페이지", "action": "webLink", "webLinkUrl": f"{BASE_URL}/reservation"}
            ]
        )

    # 파싱 성공 → 취소 처리
    if parsed.get('is_cancel'):
        conn = get_res_db()
        existing = conn.execute(
            "SELECT * FROM reservations WHERE date=? AND name=? ORDER BY created DESC",
            (parsed['date'], parsed['name'])
        ).fetchone()
        if existing:
            old = dict(existing)
            conn.execute("DELETE FROM reservations WHERE id=?", (old['id'],))
            # 카카오 히스토리 기록
            conn.execute(
                "INSERT INTO kakao_history (action,date,time,name,adult,child,menu,note) VALUES (?,?,?,?,?,?,?,?)",
                ("취소", old['date'], old['time'], old['name'], old['adult'], old.get('child',0), old.get('menu',''), old.get('note',''))
            )
            conn.commit()
            conn.close()
            year = now_kst().year
            d = datetime(year, parsed['month'], parsed['day'])
            dow = DOW_MAP[d.weekday()]
            return kakao_res(
                f"🗑️ 예약이 취소되었습니다.\n\n"
                f"📅 {parsed['month']}월 {parsed['day']}일 ({dow}) {fmt_time(old['time'])}\n"
                f"👤 {old['name']} {old['adult']}인",
                LINK_BTN
            )
        else:
            conn.close()
            return kakao_res(
                f"⚠️ 취소할 예약을 찾지 못했어요.\n"
                f"날짜와 이름을 확인해주세요.",
                LINK_BTN
            )
    year = now_kst().year
    d = datetime(year, parsed['month'], parsed['day'])
    dow = DOW_MAP[d.weekday()]
    child_str = f".{parsed['child']}" if parsed['child'] > 0 else ""
    pax_str = f"{parsed['adult']}{child_str}인"
    slot = "점심" if parsed['time'] < "15:00" else "저녁"

    # 중복 확인 (빠르게)
    conn = get_res_db()
    existing = conn.execute(
        "SELECT * FROM reservations WHERE date=? AND name=? ORDER BY created DESC",
        (parsed['date'], parsed['name'])
    ).fetchone()
    conn.close()

    if existing:
        old = dict(existing)
        # 동일하면 이미 등록됨
        if (old['time'] == parsed['time'] and old['adult'] == parsed['adult'] and old.get('menu','') == parsed['menu']):
            return kakao_res(
                f"⚠️ 이미 등록된 예약입니다!\n📅 {parsed['month']}월 {parsed['day']}일 ({dow}) {fmt_time(old['time'])}\n👤 {old['name']} {old['adult']}{'.'+str(old.get('child',0)) if old.get('child') else ''}인",
                LINK_BTN
            )
        # 다르면 수정
        conn2 = get_res_db()
        conn2.execute(
            "UPDATE reservations SET time=?,adult=?,child=?,cset=?,menu=?,note=? WHERE id=?",
            (parsed['time'], parsed['adult'], parsed['child'], parsed['cset'], parsed['menu'], parsed['note'], old['id'])
        )
        # 카카오 히스토리 기록
        conn2.execute(
            "INSERT INTO kakao_history (action,date,time,name,adult,child,menu,note) VALUES (?,?,?,?,?,?,?,?)",
            ("수정", parsed['date'], parsed['time'], parsed['name'], parsed['adult'], parsed['child'], parsed['menu'], parsed['note'])
        )
        conn2.commit(); conn2.close()
        return kakao_res(
            f"🔄 예약이 수정되었습니다!\n📅 {parsed['month']}월 {parsed['day']}일 ({dow})\n👤 {parsed['name']} {pax_str}",
            LINK_BTN
        )

    # 새 예약 등록
    new_id = hex(int(time.time()*1000))[2:] + ''.join(random.choices('abcdefghijklmnopqrstuvwxyz', k=4))
    conn3 = get_res_db()
    conn3.execute(
        "INSERT INTO reservations (id,date,time,name,adult,child,cset,menu,note) VALUES (?,?,?,?,?,?,?,?,?)",
        (new_id, parsed['date'], parsed['time'], parsed['name'],
         parsed['adult'], parsed['child'], parsed['cset'], parsed['menu'], parsed['note'])
    )
    # 카카오 히스토리 기록
    conn3.execute(
        "INSERT INTO kakao_history (action,date,time,name,adult,child,menu,note) VALUES (?,?,?,?,?,?,?,?)",
        ("추가", parsed['date'], parsed['time'], parsed['name'], parsed['adult'], parsed['child'], parsed['menu'], parsed['note'])
    )
    conn3.commit(); conn3.close()

    lines = [
        f"✅ 예약이 등록되었습니다!",
        f"",
        f"📅 {parsed['month']}월 {parsed['day']}일 ({dow}) {fmt_time(parsed['time'])} {slot}",
        f"👤 {parsed['name']}",
        f"👥 {pax_str}",
    ]
    if parsed['menu']: lines.append(f"🍽️ {parsed['menu']}")
    if parsed['note']: lines.append(f"📌 {parsed['note']}")

    return kakao_res("\n".join(lines), LINK_BTN)

# ═══════════════════════════════════════════════
#  네이버 예약 Gmail 자동 동기화
# ═══════════════════════════════════════════════

def init_gmail_log_db():
    """처리한 이메일 ID 저장 (중복 처리 방지) + 네이버 예약 히스토리"""
    conn = get_res_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gmail_log (
            email_id TEXT PRIMARY KEY,
            action   TEXT,
            rsv_id   TEXT,
            created  TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS naver_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            action     TEXT NOT NULL,
            rsv_id     TEXT,
            date       TEXT,
            time       TEXT,
            name       TEXT,
            adult      INTEGER,
            menu       TEXT,
            old_date   TEXT,
            old_time   TEXT,
            old_adult  INTEGER,
            created    TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kakao_history (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            action   TEXT NOT NULL,
            date     TEXT,
            time     TEXT,
            name     TEXT,
            adult    INTEGER,
            child    INTEGER,
            menu     TEXT,
            note     TEXT,
            created  TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.commit()
    conn.close()
    """Gmail API 서비스 객체 생성"""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=os.environ.get("GMAIL_REFRESH_TOKEN"),
        client_id=os.environ.get("GMAIL_CLIENT_ID"),
        client_secret=os.environ.get("GMAIL_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"]
    )
    return build("gmail", "v1", credentials=creds)

def parse_naver_email(subject: str, body: str):
    """
    네이버 예약 이메일 파싱
    확정/취소 구분 후 예약 정보 추출
    """
    import re

    # 확정/취소/변경 구분
    if "확정" in subject:
        action = "confirm"
    elif "취소" in subject:
        action = "cancel"
    elif "변경" in subject:
        action = "change"  # 변경: 취소내역 삭제 + 새 예약은 확정 이메일에서 처리
    else:
        if "확정" in body:
            action = "confirm"
        elif "취소" in body:
            action = "cancel"
        elif "변경" in body:
            action = "change"
        else:
            return None  # 접수는 무시

    # 예약번호 — 확정/취소는 첫번째, 변경은 신규예약내역의 번호
    if action == "change":
        # 신규예약내역 섹션의 첫 번째 예약번호
        new_section = body[body.find("신규예약내역"):] if "신규예약내역" in body else body
        rsv_num_m = re.search(r'예약번호\s+(\d+)', new_section)
    else:
        rsv_num_m = re.search(r'예약번호\s+(\d+)', body)

    if not rsv_num_m:
        return None
    rsv_num = rsv_num_m.group(1)

    # 예약자명
    name_m = re.search(r'예약자명\s+([^\s\n]+님)', body)
    name = name_m.group(1) if name_m else "네이버예약"

    # 이용일시: "2026.03.23.(월) 오후 7:00, 2명"
    dt_m = re.search(
        r'이용일시\s+(\d{4})\.(\d{2})\.(\d{2})\.\([가-힣]\)\s+(오전|오후)\s+(\d{1,2}):(\d{2}),\s*(\d+)명',
        body
    )
    if not dt_m:
        return None

    year, month, day = dt_m.group(1), dt_m.group(2), dt_m.group(3)
    ampm  = dt_m.group(4)
    hour  = int(dt_m.group(5))
    minute = dt_m.group(6)
    pax   = int(dt_m.group(7))

    if ampm == "오후" and hour != 12:
        hour += 12
    elif ampm == "오전" and hour == 12:
        hour = 0

    date_str = f"{year}-{month}-{day}"
    time_str = f"{hour:02d}:{minute}"

    # 예약상품 → 메뉴 매핑
    menu = ""
    prod_m = re.search(r'예약상품\s+(.+)', body)
    if prod_m:
        prod = prod_m.group(1)
        if "시그니처" in prod or "시그" in prod:
            menu = "시그"
        elif "스시" in prod:
            menu = "스시"
        elif "스페셜" in prod:
            menu = "스페셜"

    return {
        "action":   action,
        "rsv_num":  rsv_num,   # 네이버 예약번호 → id로 사용
        "date":     date_str,
        "time":     time_str,
        "name":     name,
        "adult":    pax,
        "child":    0,
        "cset":     0,
        "menu":     menu,
        "note":     "네이버예약",
    }

async def sync_naver_gmail():
    """
    Gmail에서 네이버 예약 이메일 가져와서 DB 동기화
    확정 → 추가, 취소 → 삭제
    """
    try:
        from googleapiclient.discovery import build
        import base64

        service = get_gmail_service()
        conn = get_res_db()

        # 검색 범위 넓힘 — 발신자 제한 없이 제목만으로 검색
        query = '네이버 예약 newer_than:3d'
        result = service.users().messages().list(
            userId="me", q=query, maxResults=50
        ).execute()

        messages = result.get("messages", [])
        if not messages:
            print("📭 새 네이버 예약 이메일 없음")
            conn.close()
            return

        added = updated = deleted = skipped = 0

        for msg in messages:
            email_id = msg["id"]

            # 이미 처리한 이메일이면 건너뜀
            already = conn.execute(
                "SELECT email_id FROM gmail_log WHERE email_id=?", (email_id,)
            ).fetchone()
            if already:
                skipped += 1
                continue

            # 이메일 상세 가져오기
            detail = service.users().messages().get(
                userId="me", id=email_id, format="full"
            ).execute()

            # 제목 추출
            headers = detail["payload"].get("headers", [])
            subject = next((h["value"] for h in headers if h["name"] == "Subject"), "")

            # 본문 추출 (text/plain 우선, 없으면 HTML에서 태그 제거)
            body = ""
            payload = detail["payload"]

            def extract_body(payload):
                """재귀적으로 본문 추출"""
                import base64, re
                if "parts" in payload:
                    # text/plain 먼저 시도
                    for part in payload["parts"]:
                        if part["mimeType"] == "text/plain":
                            data = part["body"].get("data", "")
                            if data:
                                return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                    # text/plain 없으면 HTML
                    for part in payload["parts"]:
                        if part["mimeType"] == "text/html":
                            data = part["body"].get("data", "")
                            if data:
                                html = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                                # HTML 태그 제거
                                text = re.sub(r'<[^>]+>', ' ', html)
                                text = re.sub(r'&nbsp;', ' ', text)
                                text = re.sub(r'&gt;', '>', text)
                                text = re.sub(r'\s+', ' ', text)
                                return text
                    # 멀티파트면 재귀
                    for part in payload["parts"]:
                        result = extract_body(part)
                        if result:
                            return result
                elif "body" in payload:
                    data = payload["body"].get("data", "")
                    if data:
                        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                return ""

            body = extract_body(payload)

            # 파싱
            parsed = parse_naver_email(subject, body)
            if not parsed:
                # 처리 불필요한 이메일도 로그에 기록 (재처리 방지)
                conn.execute(
                    "INSERT OR IGNORE INTO gmail_log (email_id, action) VALUES (?,?)",
                    (email_id, "skip")
                )
                skipped += 1
                continue

            rsv_id = f"naver_{parsed['rsv_num']}"

            if parsed["action"] == "confirm":
                # 이미 있으면 업데이트, 없으면 추가
                existing = conn.execute(
                    "SELECT id FROM reservations WHERE id=?", (rsv_id,)
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE reservations SET date=?,time=?,name=?,adult=?,menu=?,note=? WHERE id=?",
                        (parsed["date"], parsed["time"], parsed["name"],
                         parsed["adult"], parsed["menu"], parsed["note"], rsv_id)
                    )
                    updated += 1
                else:
                    conn.execute(
                        "INSERT INTO reservations (id,date,time,name,adult,child,cset,menu,note) VALUES (?,?,?,?,?,?,?,?,?)",
                        (rsv_id, parsed["date"], parsed["time"], parsed["name"],
                         parsed["adult"], 0, 0, parsed["menu"], parsed["note"])
                    )
                    # 히스토리 기록
                    conn.execute(
                        "INSERT INTO naver_history (action,rsv_id,date,time,name,adult,menu) VALUES (?,?,?,?,?,?,?)",
                        ("추가", rsv_id, parsed["date"], parsed["time"],
                         parsed["name"], parsed["adult"], parsed["menu"])
                    )
                    added += 1
                    # 카카오 채널 알림
                    asyncio.create_task(send_kakao_notification(parsed))

            elif parsed["action"] == "cancel":
                # 히스토리 기록 (삭제 전에)
                old = conn.execute("SELECT * FROM reservations WHERE id=?", (rsv_id,)).fetchone()
                if old:
                    old = dict(old)
                    conn.execute(
                        "INSERT INTO naver_history (action,rsv_id,date,time,name,adult,menu) VALUES (?,?,?,?,?,?,?)",
                        ("취소", rsv_id, old["date"], old["time"],
                         old["name"], old["adult"], old.get("menu",""))
                    )
                conn.execute("DELETE FROM reservations WHERE id=?", (rsv_id,))
                deleted += 1

            elif parsed["action"] == "change":
                # 변경: 본문에서 취소내역 예약번호 찾아서 삭제
                import re as _re
                cancel_section = body[body.find("취소내역"):] if "취소내역" in body else ""
                new_section    = body[body.find("신규예약내역"):body.find("취소내역")] if "신규예약내역" in body and "취소내역" in body else ""
                cancel_nums = _re.findall(r'예약번호\s+(\d+)', cancel_section)
                for cnum in cancel_nums:
                    old_id = f"naver_{cnum}"
                    old = conn.execute("SELECT * FROM reservations WHERE id=?", (old_id,)).fetchone()
                    if old:
                        old = dict(old)
                        conn.execute(
                            "INSERT INTO naver_history (action,rsv_id,date,time,name,adult,menu,old_date,old_time,old_adult) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            ("변경", rsv_id,
                             parsed["date"], parsed["time"], parsed["name"], parsed["adult"], parsed["menu"],
                             old["date"], old["time"], old["adult"])
                        )
                        conn.execute("DELETE FROM reservations WHERE id=?", (old_id,))
                        deleted += 1
                        print(f"🔄 변경으로 인한 취소 처리: {old_id}")

            # 처리 완료 로그
            conn.execute(
                "INSERT OR IGNORE INTO gmail_log (email_id, action, rsv_id) VALUES (?,?,?)",
                (email_id, parsed["action"], rsv_id)
            )

        conn.commit()
        conn.close()
        print(f"✅ 네이버 예약 동기화: 추가 {added} / 수정 {updated} / 삭제 {deleted} / 스킵 {skipped}")

    except Exception as e:
        print(f"❌ Gmail 동기화 오류: {e}")

async def send_kakao_notification(parsed: dict):
    """
    네이버 예약 확정 시 카카오 채널로 알림 전송
    카카오 비즈메시지 API 필요 (현재는 로그만 기록)
    → 추후 카카오 알림톡 API 연동 시 활성화
    """
    d = datetime(int(parsed["date"][:4]), int(parsed["date"][5:7]), int(parsed["date"][8:]))
    dow = DOW_MAP[d.weekday()]
    h = int(parsed["time"].split(":")[0])
    t = f"{h-12 if h>=13 else h}:{parsed['time'].split(':')[1]}"
    print(f"🔔 네이버 새 예약 알림: {d.month}/{d.day}({dow}) {t} {parsed['name']} {parsed['adult']}인 {parsed.get('menu','')}")

# ── 확인용 API ──────────────────────────────
@app.get("/api/naver-sync")
async def manual_sync():
    """수동 동기화 (테스트용)"""
    await sync_naver_gmail()
    return {"ok": True, "message": "동기화 완료"}

@app.get("/api/naver-history")
def get_naver_history(limit: int = 10):
    """네이버 예약 히스토리 조회 (기본 10건)"""
    conn = get_res_db()
    rows = conn.execute(
        "SELECT * FROM naver_history ORDER BY created DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/kakao-history")
def get_kakao_history(limit: int = 10):
    """카카오 챗봇 히스토리 조회 (기본 10건)"""
    conn = get_res_db()
    rows = conn.execute(
        "SELECT * FROM kakao_history ORDER BY created DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ═══════════════════════════════════════════════
#  백업 기능 — 매일 자정 자동 백업
# ═══════════════════════════════════════════════

@app.get("/api/backup")
def backup_download():
    """
    현재 예약 DB 전체를 JSON으로 다운로드
    브라우저에서 접속하면 바로 저장 가능
    """
    conn = get_res_db()
    rows = conn.execute("SELECT * FROM reservations ORDER BY date, time").fetchall()
    conn.close()
    data = [dict(r) for r in rows]
    now = datetime.now().strftime("%Y%m%d_%H%M")
    from fastapi.responses import Response
    content = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=reservation_backup_{now}.json"}
    )

async def auto_backup():
    """매일 자정 자동 백업 — Render 로그에 기록"""
    try:
        conn = get_res_db()
        rows = conn.execute("SELECT * FROM reservations ORDER BY date, time").fetchall()
        conn.close()
        count = len(rows)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        print(f"💾 자동 백업 완료: {now} / 총 {count}건")
    except Exception as e:
        print(f"❌ 백업 오류: {e}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

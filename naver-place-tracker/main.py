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
async def kakao_today(request: Request):
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
    name_m = re.search(r'([가-힣A-Za-z0-9()（）]{2,6}님)', t)

    if not (date_m and time_m and pax_m and name_m):
        return None

    MENU_KEYS = ['의자시그','시그니처','시그','스시','스페셜','스페샬','스패셜','수패셜','사페셜','미정','기타']
    MENU_MAP  = {'스페샬':'스페셜','스패셜':'스페셜','수패셜':'스페셜','사페셜':'스페셜','시그니처':'시그'}

    menu = ''
    for mk in MENU_KEYS:
        if mk in t:
            menu = MENU_MAP.get(mk, mk)
            break
    if not menu:
        menu = '시그'  # 메뉴 없으면 시그니처 기본값

    set_m = re.search(r'세트(\d+)', t)
    cset = int(set_m.group(1)) if set_m else 0

    # 비고: 메뉴 이후 내용
    note = ''
    mpos = t.find(menu) if menu else -1
    if mpos != -1:
        after = t[mpos + len(menu):].strip()
        if set_m: after = after.replace(set_m.group(0), '').strip()
        note = re.sub(r'010[-\s]?\d{3,4}[-\s]?\d{4}', '', after).strip()

    year = datetime.now().year
    month, day = int(date_m.group(1)), int(date_m.group(2))
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
        'month': month, 'day': day
    }

@app.post("/api/kakao/auto")
async def kakao_auto(request: Request):
    """
    사용자 발화에서 예약 메시지 감지 → 자동 등록
    M/D 패턴 + 시간 + 인원 + 이름 있으면 예약으로 처리
    """
    import re, time, random, string

    try:
        body = await request.json()
        utterance = body["userRequest"]["utterance"]
    except:
        return kakao_res("메시지를 읽을 수 없어요.")

    parsed = parse_reservation_msg(utterance)

    # 예약 형식이 아니면 일반 안내
    if not parsed:
        return kakao_res(
            "안녕하세요 연해주입니다 😊\n원하시는 항목을 선택해주세요.",
            [
                {"label": "📅 오늘 예약 확인", "action": "block",
                 "messageText": "오늘예약확인"},
                {"label": "📋 예약 페이지", "action": "webLink",
                 "webLinkUrl": f"{BASE_URL}/reservation"}
            ]
        )

    # 중복 확인 (같은 날짜 + 이름)
    conn = get_res_db()
    existing = conn.execute(
        "SELECT * FROM reservations WHERE date=? AND name=? ORDER BY created DESC",
        (parsed['date'], parsed['name'])
    ).fetchone()

    dow = DOW_MAP[datetime(datetime.now().year, parsed['month'], parsed['day']).weekday()]
    child_str = f".{parsed['child']}" if parsed['child'] > 0 else ""
    pax_str   = f"{parsed['adult']}{child_str}인"
    slot      = "점심" if parsed['time'] < "15:00" else "저녁"

    # 중복 감지 → 수정 여부 확인
    if existing:
        old = dict(existing)
        old_slot = "점심" if old['time'] < "15:00" else "저녁"
        old_child = f".{old['child']}" if old.get('child') else ""

        # 완전 동일하면 이미 등록됨 안내
        if (old['time'] == parsed['time'] and
            old['adult'] == parsed['adult'] and
            old.get('menu','') == parsed['menu']):
            conn.close()
            return kakao_res(
                f"⚠️ 이미 등록된 예약입니다!\n\n"
                f"📅 {parsed['month']}월 {parsed['day']}일 ({dow}) {old['time']} {slot}\n"
                f"👤 {old['name']}\n"
                f"👥 {old['adult']}{old_child}인"
                f"{' / '+old['menu'] if old.get('menu') else ''}",
                [{"label":"📋 예약 페이지","action":"webLink","webLinkUrl":f"{BASE_URL}/reservation"}]
            )

        # 다른 내용 → 수정 처리
        conn.execute(
            "UPDATE reservations SET time=?,adult=?,child=?,cset=?,menu=?,note=? WHERE id=?",
            (parsed['time'], parsed['adult'], parsed['child'],
             parsed['cset'], parsed['menu'], parsed['note'], old['id'])
        )
        conn.commit()
        conn.close()

        changes = []
        if old['time'] != parsed['time']:
            changes.append(f"시간: {fmt_time(old['time'])} → {fmt_time(parsed['time'])}")
        if old['adult'] != parsed['adult'] or old.get('child',0) != parsed['child']:
            changes.append(f"인원: {old['adult']}{old_child}인 → {pax_str}")
        if old.get('menu','') != parsed['menu']:
            changes.append(f"메뉴: {old.get('menu','-')} → {parsed['menu'] or '-'}")

        return kakao_res(
            f"🔄 예약이 수정되었습니다!\n\n"
            f"📅 {parsed['month']}월 {parsed['day']}일 ({dow})\n"
            f"👤 {parsed['name']}\n"
            + "\n".join(f"  {c}" for c in changes),
            [{"label":"📋 예약 페이지","action":"webLink","webLinkUrl":f"{BASE_URL}/reservation"}]
        )

    # 새 예약 등록
    new_id = hex(int(time.time()*1000))[2:] + ''.join(random.choices('abcdefghijklmnopqrstuvwxyz', k=4))
    conn.execute(
        "INSERT INTO reservations (id,date,time,name,adult,child,cset,menu,note) VALUES (?,?,?,?,?,?,?,?,?)",
        (new_id, parsed['date'], parsed['time'], parsed['name'],
         parsed['adult'], parsed['child'], parsed['cset'], parsed['menu'], parsed['note'])
    )
    conn.commit()
    conn.close()

    lines = [
        f"✅ 예약이 등록되었습니다!",
        f"",
        f"📅 {parsed['month']}월 {parsed['day']}일 ({dow}) {fmt_time(parsed['time'])} {slot}",
        f"👤 {parsed['name']}",
        f"👥 {pax_str}",
    ]
    if parsed['child'] > 0:
        lines.append(f"  어른 {parsed['adult']}인분 / 어린이 {parsed['child']}명")
        if parsed['cset'] > 0:
            lines.append(f"  어린이세트 {parsed['cset']}개")
    if parsed['menu']:
        lines.append(f"🍽️ {parsed['menu']}")
    if parsed['note']:
        lines.append(f"📌 {parsed['note']}")

    return kakao_res(
        "\n".join(lines),
        [{"label":"📋 예약 페이지","action":"webLink","webLinkUrl":f"{BASE_URL}/reservation"}]
    )

# ═══════════════════════════════════════════════
#  네이버 예약 Gmail 자동 동기화
# ═══════════════════════════════════════════════

def init_gmail_log_db():
    """처리한 이메일 ID 저장 (중복 처리 방지)"""
    conn = get_res_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gmail_log (
            email_id TEXT PRIMARY KEY,
            action   TEXT,
            rsv_id   TEXT,
            created  TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.commit()
    conn.close()

def get_gmail_service():
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

    # 확정/취소 구분
    if "확정" in subject:
        action = "confirm"
    elif "취소" in subject:
        action = "cancel"
    else:
        # 제목에 없으면 본문에서도 확인
        if "확정" in body:
            action = "confirm"
        elif "취소" in body:
            action = "cancel"
        else:
            return None  # 접수/변경은 무시

    # 예약번호
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
                    added += 1

            elif parsed["action"] == "cancel":
                conn.execute("DELETE FROM reservations WHERE id=?", (rsv_id,))
                deleted += 1

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

# ── 확인용 API ──────────────────────────────
@app.get("/api/naver-sync")
async def manual_sync():
    """수동 동기화 (테스트용)"""
    await sync_naver_gmail()
    return {"ok": True, "message": "동기화 완료"}

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

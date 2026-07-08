from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo
import asyncio, os

import auth
import database
import crawler
from firebase_config import get_admin_app, get_data_backend, get_web_config, uses_firestore, web_config_ready

app = FastAPI(title="네이버 플레이스 순위 트래커")
templates = Jinja2Templates(directory="templates")
scheduler = AsyncIOScheduler(timezone="Asia/Seoul")
APP_VERSION = "inactive-user-skip-20260708"
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "djmonnar4@gmail.com").strip().lower()
MANUAL_CHECK_LIMIT_PER_DAY = int(os.environ.get("MANUAL_CHECK_LIMIT_PER_DAY", "3"))
# 이 일수 이상 로그인하지 않은 사용자는 매일 자동 체크에서 제외 (수동 체크는 가능)
AUTO_CHECK_INACTIVE_DAYS = int(os.environ.get("AUTO_CHECK_INACTIVE_DAYS", "10"))
RUNNING_RANK_CHECKS: set[str] = set()
QUEUED_RANK_CHECK_USERS: set[str] = set()
RANK_CHECK_QUEUE: asyncio.Queue | None = None
RANK_CHECK_WORKER_TASK: asyncio.Task | None = None


@app.middleware("http")
async def firebase_auth_middleware(request: Request, call_next):
    if auth.path_requires_auth(request.url.path):
        user = await auth.authenticate_request(request)
        if user is None:
            return auth.unauthenticated_response(request)
        request.state.user = user
    else:
        request.state.user = auth.local_user()

    return await call_next(request)


class SessionLogin(BaseModel):
    idToken: str


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


@app.get("/api/server/status")
async def server_status():
    admin_ready = False
    admin_error = None
    if uses_firestore():
        try:
            await asyncio.to_thread(get_admin_app)
            admin_ready = True
        except Exception as exc:
            admin_error = exc.__class__.__name__

    return {
        "status": "ok",
        "version": APP_VERSION,
        "running_rank_checks": len(RUNNING_RANK_CHECKS),
        "queued_rank_checks": len(QUEUED_RANK_CHECK_USERS),
        "manual_check_limit_per_day": MANUAL_CHECK_LIMIT_PER_DAY,
        "auto_check_inactive_days": AUTO_CHECK_INACTIVE_DAYS,
        "data_backend": get_data_backend(),
        "uses_firestore": uses_firestore(),
        "auth_enabled": auth.auth_enabled(),
        "firebase_admin_ready": admin_ready,
        "firebase_admin_error": admin_error,
    }


def _is_admin_user(user: dict | None) -> bool:
    return (user or {}).get("email", "").strip().lower() == ADMIN_EMAIL


def _profile_complete(profile: dict | None) -> bool:
    return bool(
        profile
        and profile.get("profile_complete")
        and str(profile.get("name") or "").strip()
        and str(profile.get("email") or "").strip()
        and str(profile.get("business_name") or "").strip()
    )


async def _require_member_profile(user: dict) -> None:
    if not uses_firestore():
        return

    def _get_profile_sync():
        from firebase_admin import firestore

        get_admin_app()
        snapshot = firestore.client().collection("users").document(user["uid"]).get()
        return snapshot.to_dict() or {}

    profile = await asyncio.to_thread(_get_profile_sync)
    if not _profile_complete(profile):
        raise HTTPException(status_code=403, detail="member_profile_required")


def _today_kst() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")


def _rank_check_queue() -> asyncio.Queue:
    global RANK_CHECK_QUEUE
    if RANK_CHECK_QUEUE is None:
        RANK_CHECK_QUEUE = asyncio.Queue()
    return RANK_CHECK_QUEUE


def _ensure_rank_check_worker() -> None:
    global RANK_CHECK_WORKER_TASK
    if RANK_CHECK_WORKER_TASK is None or RANK_CHECK_WORKER_TASK.done():
        RANK_CHECK_WORKER_TASK = asyncio.create_task(_rank_check_worker())


async def _rank_check_worker() -> None:
    queue = _rank_check_queue()
    while True:
        item = await queue.get()
        user_id = str(item.get("user_id") or "")
        source = str(item.get("source") or "manual")
        if user_id:
            QUEUED_RANK_CHECK_USERS.discard(user_id)
        try:
            if user_id:
                await _run_rank_check_job(user_id, source)
        except Exception as exc:
            print(f"순위 체크 워커 오류 [{user_id}]: {exc}")
        finally:
            queue.task_done()


async def _enqueue_rank_check(user_id: str, source: str = "manual") -> dict:
    queue = _rank_check_queue()
    _ensure_rank_check_worker()

    if user_id in RUNNING_RANK_CHECKS:
        message = "이미 Render 서버에서 순위 체크가 진행 중입니다."
        await database.set_check_status(user_id, "running", message, {"source": source})
        return {"status": "running", "message": message, "queue_position": 0}

    if user_id in QUEUED_RANK_CHECK_USERS:
        message = "이미 순위 체크 대기열에 등록되어 있습니다."
        await database.set_check_status(user_id, "queued", message, {"source": source})
        return {"status": "queued", "message": message, "queue_position": None}

    position = queue.qsize() + len(RUNNING_RANK_CHECKS) + 1
    message = (
        f"순위 체크 대기열에 등록되었습니다. 예상 순번: {position}번째"
        if source == "manual"
        else f"자동 순위 체크 대기열에 등록되었습니다. 예상 순번: {position}번째"
    )
    QUEUED_RANK_CHECK_USERS.add(user_id)
    await database.set_check_status(
        user_id,
        "queued",
        message,
        {"queue_position": position, "source": source},
    )
    await queue.put({"user_id": user_id, "source": source})
    return {"status": "queued", "message": message, "queue_position": position}


async def _enqueue_daily_rank_checks() -> None:
    active_ids, inactive_ids = await database.list_active_user_ids(AUTO_CHECK_INACTIVE_DAYS)
    if inactive_ids:
        print(f"자동 순위 체크: {AUTO_CHECK_INACTIVE_DAYS}일 이상 미로그인 {len(inactive_ids)}명 제외")
    if not active_ids:
        print("자동 순위 체크: 대상 사용자 없음")
        return

    enqueued = 0
    for user_id in active_ids:
        if user_id in RUNNING_RANK_CHECKS or user_id in QUEUED_RANK_CHECK_USERS:
            continue
        await _enqueue_rank_check(user_id, "scheduled")
        enqueued += 1
    print(f"자동 순위 체크: {enqueued}/{len(active_ids)}명 대기열 등록")


async def _consume_manual_check_quota_or_raise(user_id: str) -> dict:
    usage = await database.consume_manual_check_quota(
        user_id,
        _today_kst(),
        MANUAL_CHECK_LIMIT_PER_DAY,
    )
    if not usage.get("allowed"):
        raise HTTPException(
            status_code=429,
            detail=(
                f"오늘 수동 체크 횟수 {usage.get('limit', MANUAL_CHECK_LIMIT_PER_DAY)}회를 모두 사용했습니다. "
                "내일 다시 실행해주세요."
            ),
        )
    return usage


@app.get("/api/admin/users")
async def api_admin_users(request: Request):
    try:
        user = await auth.authenticate_bearer_request(request, raise_config_errors=True)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Render Firebase Admin 환경변수를 확인해주세요.",
        ) from exc

    if not user:
        raise HTTPException(status_code=401, detail="Firebase 로그인이 필요합니다.")
    if not _is_admin_user(user):
        raise HTTPException(status_code=403, detail="admin_only")
    if not uses_firestore():
        raise HTTPException(status_code=503, detail="Render DATA_BACKEND=firestore 환경변수를 확인해주세요.")

    def _list_users_sync():
        from firebase_admin import firestore

        get_admin_app()
        rows = []
        for doc_snapshot in firestore.client().collection("users").stream():
            data = doc_snapshot.to_dict() or {}
            rows.append({
                "uid": doc_snapshot.id,
                "name": data.get("name") or data.get("auth_name") or "",
                "email": data.get("email") or "",
                "business_name": data.get("business_name") or "",
                "profile_complete": bool(data.get("profile_complete")),
                "updated_at": data.get("updated_at") or "",
                "last_login_at": data.get("last_login_at") or "",
                "created_at": data.get("created_at") or "",
            })
        rows.sort(key=lambda row: (row["profile_complete"], row["last_login_at"], row["updated_at"]), reverse=True)
        return rows

    users = await asyncio.to_thread(_list_users_sync)
    return JSONResponse({"status": "ok", "users": users})


@app.get("/api/places/search")
async def api_search_places(request: Request, q: str = "", limit: int = 5):
    try:
        user = await auth.authenticate_bearer_request(request, raise_config_errors=True)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Render Firebase Admin 환경변수를 확인해주세요.",
        ) from exc

    if not user:
        raise HTTPException(status_code=401, detail="Firebase 로그인이 필요합니다.")

    await _require_member_profile(user)
    return JSONResponse(await _search_places_payload(q, limit))


@app.get("/places/search")
async def search_places(request: Request, q: str = "", limit: int = 5):
    if not auth.get_request_user(request):
        raise HTTPException(status_code=401, detail="Firebase 로그인이 필요합니다.")

    return JSONResponse(await _search_places_payload(q, limit))


async def _search_places_payload(q: str = "", limit: int = 5) -> dict:
    query = (q or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="검색어를 입력해주세요.")

    safe_limit = max(1, min(int(limit or 5), crawler.PLACE_SEARCH_LIMIT))
    try:
        results = await asyncio.wait_for(
            crawler.search_place_candidates(query, safe_limit),
            timeout=60,
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="플레이스 ID 검색 시간이 초과되었습니다.") from exc

    return {
        "status": "ok",
        "query": query,
        "results": results,
    }

@app.on_event("startup")
async def startup():
    await database.init_db()
    stale = await database.reset_stale_check_statuses()
    if stale:
        print(f"재시작으로 중단된 체크 상태 {stale}건 리셋")
    _ensure_rank_check_worker()
    scheduler.add_job(_enqueue_daily_rank_checks, CronTrigger(hour=9, minute=0, timezone="Asia/Seoul"), id="daily_check", replace_existing=True)
    scheduler.add_job(keep_alive, "interval", minutes=14, id="keep_alive", replace_existing=True)
    scheduler.start()
    print("스케줄러 시작")

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()
    if RANK_CHECK_WORKER_TASK and not RANK_CHECK_WORKER_TASK.done():
        RANK_CHECK_WORKER_TASK.cancel()


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if not auth.auth_enabled():
        return RedirectResponse(next or "/", status_code=303)

    current_user = await auth.authenticate_request(request)
    if current_user:
        return RedirectResponse(next or "/", status_code=303)

    return templates.TemplateResponse("login.html", {
        "request": request,
        "firebase_config": get_web_config(),
        "firebase_ready": web_config_ready(),
        "next": next or "/",
    })


@app.post("/session/login")
async def session_login(request: Request, payload: SessionLogin):
    if not auth.auth_enabled():
        return JSONResponse({"status": "auth_disabled"})

    try:
        session_cookie, decoded, expires_in = await auth.create_session_cookie(payload.idToken)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Firebase ID token") from exc

    await database.ensure_user(
        decoded["uid"],
        decoded.get("email"),
        decoded.get("name"),
        decoded.get("picture"),
    )
    response = JSONResponse({"status": "ok"})
    auth.set_session_cookie(response, request, session_cookie, expires_in)
    return response


@app.post("/session/logout")
async def session_logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    auth.clear_session_cookie(response, request)
    return response


@app.get("/session/logout")
async def session_logout_get(request: Request):
    response = RedirectResponse("/login", status_code=303)
    auth.clear_session_cookie(response, request)
    return response


def _parse_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _format_mmdd(value: str | None) -> str:
    parsed = _parse_date(value)
    return parsed.strftime("%m/%d") if parsed else "-"


def _build_rank_summary(rows: list[dict]) -> dict:
    valid = [row for row in rows if int(row.get("rank") or -1) > 0]
    if not valid:
        return {
            "best_rank": "-",
            "best_date": "-",
            "worst_rank": "-",
            "worst_date": "-",
            "tracking_days": 0,
        }

    best = min(valid, key=lambda row: int(row["rank"]))
    worst = max(valid, key=lambda row: int(row["rank"]))
    dates = sorted(filter(None, (_parse_date(row.get("date")) for row in valid)))
    tracking_days = (dates[-1] - dates[0]).days + 1 if len(dates) >= 2 else len(dates)
    return {
        "best_rank": f"{best['rank']}위",
        "best_date": _format_mmdd(best.get("date")),
        "worst_rank": f"{worst['rank']}위",
        "worst_date": _format_mmdd(worst.get("date")),
        "tracking_days": tracking_days,
    }


def _build_chart_points(rows: list[dict]) -> list[dict]:
    points = []
    seen = set()
    for row in sorted(rows, key=lambda item: item.get("date", "")):
        date_key = row.get("date")
        if not date_key or date_key in seen:
            continue
        seen.add(date_key)
        rank = int(row.get("rank") or -1)
        points.append({
            "date": date_key,
            "rank": rank if rank > 0 else None,
        })
    return points


def _fallback_top_results(latest: list[dict], keyword: str | None, selected_place_id: str | None) -> dict:
    rows = [
        row for row in latest
        if row.get("keyword") == keyword and int(row.get("rank") or -1) > 0
    ]
    rows.sort(key=lambda row: int(row.get("rank") or 999))
    latest_date = max((row.get("date") for row in rows if row.get("date")), default=None)
    results = []
    for row in rows[:30]:
        results.append({
            "rank": row.get("rank"),
            "place_id": row.get("place_id") or "",
            "place_name": row.get("place_name") or row.get("place_id") or "",
            "is_target": row.get("place_id") == selected_place_id,
        })
    return {"date": latest_date, "results": results}


def _legacy_keyword_place_id(places: list[dict]) -> str:
    if not places:
        return ""
    oldest = min(places, key=lambda row: row.get("created_at") or "")
    return str(oldest.get("place_id") or "")


def _keyword_place_id(keyword_row: dict, places: list[dict]) -> str:
    return str(keyword_row.get("place_id") or "").strip() or _legacy_keyword_place_id(places)


def _keywords_for_place(keywords: list[dict], places: list[dict], place_id: str | None) -> list[dict]:
    target_place_id = str(place_id or "")
    rows = [
        row for row in keywords
        if _keyword_place_id(row, places) == target_place_id
    ]
    return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)


def _configured_keyword_pairs(places: list[dict], keywords: list[dict]) -> list[dict]:
    places_by_id = {str(place.get("place_id") or ""): place for place in places}
    pairs = []
    seen = set()
    for keyword_row in keywords:
        keyword = str(keyword_row.get("keyword") or "").strip()
        place_id = _keyword_place_id(keyword_row, places)
        if not keyword or place_id not in places_by_id:
            continue
        key = (place_id, keyword)
        if key in seen:
            continue
        seen.add(key)
        pairs.append({
            "place": places_by_id[place_id],
            "keyword": keyword,
            "keyword_row": keyword_row,
        })
    return pairs


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    place_id: Optional[str] = None,
    keyword: Optional[str] = None,
):
    user = auth.get_request_user(request)
    user_id = user["uid"]
    places = await database.get_places(user_id)
    keywords = await database.get_keywords(user_id)
    latest = await database.get_latest_rankings(user_id)
    pair_keys = {
        (pair["place"].get("place_id"), pair["keyword"])
        for pair in _configured_keyword_pairs(places, keywords)
    }
    latest = [
        row for row in latest
        if (row.get("place_id"), row.get("keyword")) in pair_keys
    ]

    selected_place = next((row for row in places if row.get("place_id") == place_id), places[0] if places else None)
    selected_place_id = selected_place.get("place_id") if selected_place else None
    selected_keywords = _keywords_for_place(keywords, places, selected_place_id)
    selected_keyword = keyword if any(row.get("keyword") == keyword for row in selected_keywords) else (
        selected_keywords[0]["keyword"] if selected_keywords else None
    )

    selected_rows = []
    if selected_place_id and selected_keyword:
        selected_rows = await database.get_rankings(
            place_id=selected_place_id,
            keyword=selected_keyword,
            days=3650,
            user_id=user_id,
        )

    summary = _build_rank_summary(selected_rows)
    chart_points = _build_chart_points(selected_rows)

    top_payload = {"date": None, "results": []}
    if selected_keyword:
        top_payload = await database.get_latest_keyword_results(selected_keyword, user_id=user_id, limit=30)
        if not top_payload["results"]:
            top_payload = _fallback_top_results(latest, selected_keyword, selected_place_id)
        else:
            for row in top_payload["results"]:
                row["is_target"] = row.get("place_id") == selected_place_id

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "places": places,
        "keywords": selected_keywords,
        "latest": latest,
        "selected_place": selected_place,
        "selected_place_id": selected_place_id or "",
        "selected_keyword": selected_keyword,
        "summary": summary,
        "chart_points": chart_points,
        "top_results": top_payload["results"],
        "top_results_date": top_payload["date"],
        "user": user, "auth_enabled": auth.auth_enabled(),
        "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })

@app.post("/places/add")
async def add_place(request: Request, place_id: str = Form(...), place_name: str = Form(...)):
    if place_id.strip():
        await database.add_place(place_id.strip(), place_name.strip(), auth.get_request_user(request)["uid"])
    return RedirectResponse("/", status_code=303)

@app.post("/places/delete")
async def del_place(request: Request, place_id: str = Form(...)):
    await database.delete_place(place_id, auth.get_request_user(request)["uid"])
    return RedirectResponse("/", status_code=303)

@app.post("/keywords/add")
async def add_keyword(request: Request, keyword: str = Form(...), place_id: str = Form("")):
    target_place_id = place_id.strip()
    if keyword.strip() and target_place_id:
        await database.add_keyword(keyword.strip(), auth.get_request_user(request)["uid"], target_place_id)
    suffix = f"?place_id={target_place_id}" if target_place_id else ""
    return RedirectResponse(f"/{suffix}", status_code=303)

@app.post("/keywords/delete")
async def del_keyword(request: Request, keyword: str = Form(...), place_id: str = Form("")):
    target_place_id = place_id.strip()
    await database.delete_keyword(keyword, auth.get_request_user(request)["uid"], target_place_id)
    suffix = f"?place_id={target_place_id}" if target_place_id else ""
    return RedirectResponse(f"/{suffix}", status_code=303)

@app.post("/check/now")
async def check_now(request: Request):
    request_user = auth.get_request_user(request)
    user_id = request_user["uid"]
    if (
        not _is_admin_user(request_user)
        and user_id not in RUNNING_RANK_CHECKS
        and user_id not in QUEUED_RANK_CHECK_USERS
    ):
        await _consume_manual_check_quota_or_raise(user_id)
    result = await _enqueue_rank_check(user_id, "manual")
    return JSONResponse({
        **result,
        "data_backend": get_data_backend(),
        "manual_check_limit_per_day": MANUAL_CHECK_LIMIT_PER_DAY,
    })


async def _run_rank_check_job(user_id: str, source: str = "manual") -> None:
    if user_id in RUNNING_RANK_CHECKS:
        await database.set_check_status(
            user_id,
            "running",
            "이미 순위 체크가 진행 중입니다.",
        )
        return

    RUNNING_RANK_CHECKS.add(user_id)
    try:
        await database.set_check_status(
            user_id,
            "running",
            "Render 서버에서 순위 체크 중입니다.",
            {"source": source},
        )
        summary = await crawler.run_daily_check(user_id)
    except Exception as exc:
        await database.set_check_status(
            user_id,
            "error",
            f"순위 체크 실패: {exc.__class__.__name__}",
            {"error": str(exc)[:500]},
        )
        raise
    finally:
        RUNNING_RANK_CHECKS.discard(user_id)

    summary = summary or {}
    rankings_saved = int(summary.get("rankings_saved") or 0)
    failed_keywords = list(summary.get("failed_keywords") or [])
    if rankings_saved > 0 and not failed_keywords:
        state = "done"
        message = f"순위 체크 완료: {rankings_saved}건 저장"
    elif rankings_saved > 0:
        state = "done"
        message = (
            f"순위 체크 완료: {rankings_saved}건 저장 "
            f"(수집 실패: {', '.join(failed_keywords)})"
        )
    elif failed_keywords:
        state = "error"
        message = (
            "네이버 검색 결과를 수집하지 못해 순위를 저장하지 않았습니다. "
            "잠시 후 다시 시도해주세요."
        )
    else:
        state = "empty"
        message = "순위 체크 완료: 저장된 순위가 없습니다."

    await database.set_check_status(
        user_id,
        state,
        message,
        {"source": source, **summary},
    )


@app.post("/api/check/now")
async def api_check_now(request: Request):
    try:
        user = await auth.authenticate_bearer_request(request, raise_config_errors=True)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Render Firebase Admin 환경변수를 확인해주세요.",
        ) from exc

    if not user:
        raise HTTPException(status_code=401, detail="Firebase 로그인이 필요합니다.")
    if not uses_firestore():
        raise HTTPException(
            status_code=503,
            detail="Render DATA_BACKEND=firestore 환경변수를 확인해주세요.",
        )

    await _require_member_profile(user)
    await database.ensure_user(
        user["uid"],
        user.get("email"),
        user.get("name"),
        user.get("photo_url"),
    )
    if (
        not _is_admin_user(user)
        and user["uid"] not in RUNNING_RANK_CHECKS
        and user["uid"] not in QUEUED_RANK_CHECK_USERS
    ):
        await _consume_manual_check_quota_or_raise(user["uid"])

    result = await _enqueue_rank_check(user["uid"], "manual")
    return JSONResponse({
        **result,
        "data_backend": get_data_backend(),
        "manual_check_limit_per_day": MANUAL_CHECK_LIMIT_PER_DAY,
    })


@app.get("/check/status")
async def check_status(request: Request):
    latest = await database.get_latest_rankings(auth.get_request_user(request)["uid"])
    return JSONResponse({"latest": latest})

@app.get("/report", response_class=HTMLResponse)
async def report(request: Request):
    user_id = auth.get_request_user(request)["uid"]
    places = await database.get_places(user_id)
    keywords = await database.get_keywords(user_id)
    place_keyword_pairs = _configured_keyword_pairs(places, keywords)
    rankings_30 = await database.get_rankings(days=30, user_id=user_id)
    today = datetime.now().strftime("%Y년 %m월 %d일")
    chart_labels_set = sorted(set(r['date'] for r in rankings_30))
    chart_datasets = []
    for pair in place_keyword_pairs:
        place = pair["place"]
        kw_name = pair["keyword"]
        label = f"{place['place_name']} | {kw_name}"
        data_map = {r['date']: r['rank'] for r in rankings_30
                    if r['place_id'] == place['place_id'] and r['keyword'] == kw_name}
        chart_datasets.append({"label": label, "data": [data_map.get(d) for d in chart_labels_set]})
    summary_by_kw = {}
    for pair in place_keyword_pairs:
        place = pair["place"]
        kw_name = pair["keyword"]
        rows = sorted([r for r in rankings_30 if r['place_id'] == place['place_id'] and r['keyword'] == kw_name],
                      key=lambda x: x['date'], reverse=True)
        today_rank = rows[0]['rank'] if rows else -1
        prev_rank  = rows[1]['rank'] if len(rows) > 1 else None
        week_rank  = rows[6]['rank'] if len(rows) > 6 else None
        summary_by_kw[f"{place['place_name']} | {kw_name}"] = [
            {"label": "오늘 순위", "rank": today_rank, "change": None},
            {"label": "어제 순위", "rank": prev_rank or -1, "change": (today_rank-prev_rank) if (prev_rank and today_rank>0) else None},
            {"label": "7일 전",   "rank": week_rank or -1, "change": (today_rank-week_rank) if (week_rank and today_rank>0) else None},
        ]
    calendar = []
    for pair in place_keyword_pairs:
        place = pair["place"]
        kw_name = pair["keyword"]
        rows = [r for r in rankings_30 if r['place_id']==place['place_id'] and r['keyword']==kw_name and r['rank']>0]
        seen = {r['date']: r['rank'] for r in sorted(rows, key=lambda x: x['date'])}
        if not seen: continue
        dates_sorted = sorted(seen.keys())
        days = []
        for i, d in enumerate(dates_sorted):
            rank = seen[d]
            prev = seen[dates_sorted[i-1]] if i > 0 else None
            days.append({"date": d[5:], "rank": rank, "change": (rank-prev) if prev else None})
        calendar.append({"place_name": place['place_name'], "keyword": kw_name, "days": days})
    return templates.TemplateResponse("report.html", {
        "request": request, "places": places, "keywords": [{"keyword": key} for key in summary_by_kw.keys()],
        "today": today, "summary_by_kw": summary_by_kw, "calendar": calendar,
        "chart_data": {"labels": [l[5:] for l in chart_labels_set], "datasets": chart_datasets},
    })

@app.get("/api/rankings")
async def api_rankings(request: Request, place_id: str = None, keyword: str = None, days: int = 30):
    return JSONResponse(await database.get_rankings(
        place_id=place_id,
        keyword=keyword,
        days=days,
        user_id=auth.get_request_user(request)["uid"],
    ))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

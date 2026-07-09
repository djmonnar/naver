import asyncio
import os
import random
import time
import urllib.parse
import re
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright
import database

KST = ZoneInfo("Asia/Seoul")

# 한 키워드의 목록 수집에 쓸 최대 시간(초). 이 시간이 지나면 그때까지 모은 결과를 사용한다.
# Render 무료 플랜에서 무한정 스크롤하다 타임아웃/메모리 초과로 실패하는 것을 방지.
COLLECT_BUDGET_SECONDS = int(os.environ.get("RANK_COLLECT_BUDGET_SECONDS", "40"))
# search_keyword_results 전체(브라우저 기동 포함)에 대한 안전 타임아웃(초).
KEYWORD_CRAWL_TIMEOUT = int(os.environ.get("RANK_KEYWORD_TIMEOUT_SECONDS", "80"))
MAX_SCROLL_ITERATIONS = int(os.environ.get("RANK_MAX_SCROLL_ITERATIONS", "40"))

# 크롬 실행 인자 — 자동화 탐지 회피 + Render 무료 플랜(512MB) 메모리 절약
BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--renderer-process-limit=1",
    "--disable-features=site-per-process,TranslateUI",
]


def _now_kst() -> datetime:
    return datetime.now(KST)

MAX_RANK = 100
# 일일 체크에서 수집·저장·추적하는 깊이(위). 기본 50 — 무료/Starter 플랜에서
# 밀도 높은 키워드가 끝까지 스크롤되지 않고 일찍 끝나 속도·안정성이 좋아진다.
# 이보다 밖이면 "N위 밖"으로 기록. RANK_TRACK_LIMIT 환경변수로 조절.
TOP_LIST_LIMIT = int(os.environ.get("RANK_TRACK_LIMIT", "50"))
PLACE_SEARCH_LIMIT = 8

_PLACE_NAME_NOISE_PHRASES = [
    "네이버페이예약",
    "네이버페이",
    "네이버예약",
    "방문자리뷰",
    "블로그리뷰",
    "이미지수",
    "예약",
    "저장",
    "공유",
    "길찾기",
]

_PLACE_ID_PATTERNS = [
    re.compile(r"/p/search/[^?#]+/place/(\d+)"),
    re.compile(r"https?://pcmap\.place\.naver\.com/[^/?#]+/(\d+)"),
    re.compile(r"/(?:place|restaurant|hospital|hairshop|accommodation|attraction|cafe|shopping|beauty|mart|lodging|tour|culture|education|public|business|leisure|sports)/(\d+)"),
    re.compile(r"[?&]placeId=(\d+)"),
    re.compile(r"placeId[\"']?\s*[:=]\s*[\"']?(\d+)"),
]


def _decode_json_text(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return value


def _clean_place_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name or "").strip()
    blocked = {"광고", "저장", "공유", "방문자리뷰", "블로그리뷰", "예약", "길찾기"}
    if cleaned in blocked:
        return ""
    for phrase in _PLACE_NAME_NOISE_PHRASES:
        index = cleaned.find(phrase)
        if index == 0:
            return ""
        if index > 0:
            cleaned = cleaned[:index].strip()
            break
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ·ㆍ-_/|,")
    return "" if cleaned in blocked else cleaned


def _normalize_place_name(name: str) -> str:
    cleaned = _clean_place_name(name)
    return re.sub(r"[\s·ㆍ\-_(){}\[\].,'\"`]+", "", cleaned).lower()


def _place_names_match(left: str, right: str) -> bool:
    left_key = _normalize_place_name(left)
    right_key = _normalize_place_name(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    shorter, longer = sorted([left_key, right_key], key=len)
    return len(shorter) >= 6 and longer.startswith(shorter)


def _rank_from_place_name(results: list[dict], target_place_name: str) -> int | None:
    for row in results:
        if _place_names_match(row.get("place_name") or "", target_place_name):
            rank = int(row.get("rank") or 0)
            return rank if rank > 0 else None
    return None


def _extract_place_id_from_text(value: str) -> str:
    if not value:
        return ""
    for pattern in _PLACE_ID_PATTERNS:
        match = pattern.search(value)
        if match:
            return next((group for group in match.groups() if group), "")
    return ""


def _extract_current_place_id(page) -> str:
    urls = "\n".join([page.url, *[frame.url for frame in page.frames]])
    return _extract_place_id_from_text(urls)


def _build_rank_indexes(results: list[dict]) -> tuple[dict[str, int], dict[str, int]]:
    by_place_id = {}
    by_name = {}
    duplicate_names = set()

    for row in results:
        rank = int(row.get("rank") or 0)
        if rank <= 0:
            continue

        place_id = str(row.get("place_id") or "").strip()
        if place_id:
            by_place_id[place_id] = rank

        name_key = _normalize_place_name(row.get("place_name") or "")
        if not name_key:
            continue
        if name_key in by_name and by_name[name_key] != rank:
            duplicate_names.add(name_key)
            continue
        by_name[name_key] = rank

    for name_key in duplicate_names:
        by_name.pop(name_key, None)

    return by_place_id, by_name


def _extract_place_name(content: str, place_id: str) -> str:
    id_pos = content.find(f'"id":"{place_id}"')
    if id_pos == -1:
        id_pos = content.find(place_id)
    if id_pos == -1:
        return ""

    window = content[max(0, id_pos - 2000): id_pos + 4000]
    for key in ("name", "placeName", "businessName", "displayName", "title"):
        match = re.search(rf'"{key}"\s*:\s*"((?:\\.|[^"\\])*)"', window)
        if match:
            name = _clean_place_name(_decode_json_text(match.group(1)))
            if name and not name.isdigit():
                return name
    return ""


def _extract_json_results(content: str, limit: int) -> list[dict]:
    pattern = r'"(?:RestaurantListSummary|PlaceListSummary|CafeListSummary|HotelListSummary):(\d+):\d+"'
    matches = re.findall(pattern, content)
    results = []
    seen = set()

    for place_id in matches:
        if place_id in seen:
            continue
        seen.add(place_id)

        id_pos = content.find(f'"id":"{place_id}"')
        window = content[id_pos:id_pos + 3000] if id_pos >= 0 else content
        if re.search(r'"isAd"\s*:\s*true', window):
            continue

        results.append({
            "rank": len(results) + 1,
            "place_id": place_id,
            "place_name": _extract_place_name(content, place_id),
        })

        if len(results) >= limit:
            break

    return results


async def _extract_dom_results(frame, limit: int) -> list[dict]:
    selectors = ["li.UEzoS", "li[data-laim-exp-id]", "li.VLTHu", "li.CHC5F"]
    items = []
    for selector in selectors:
        items = await frame.query_selector_all(selector)
        if items:
            break

    results = []
    seen = set()
    for item in items:
        item_text = await item.inner_text()
        if any("광고" in line for line in item_text.splitlines()[:3]):
            continue

        item_html = await item.inner_html()
        id_match = re.search(r'place/(\d+)|"id"\s*:\s*"(\d+)"|placeId["\']?\s*[:=]\s*["\']?(\d+)', item_html)
        place_id = next((group for group in (id_match.groups() if id_match else []) if group), "")
        if place_id and place_id in seen:
            continue
        if place_id:
            seen.add(place_id)

        place_name = await item.evaluate("""
            (el) => {
                // 상호명이 아닌 라벨(사진 개수·리뷰 수·거리·버튼 등)을 걸러낸다.
                const NOISE = /(광고|방문자리뷰|블로그리뷰|이미지수|쿠폰|톡톡|길찾기|저장|공유|예약)/;
                const isNoise = (t) => !t || t.length < 2 || NOISE.test(t)
                    || /^\\d+(\\.\\d+)?\\s*(m|km|개|장|명)?$/.test(t);
                const selectors = [
                    'a.place_bluelink span',
                    'span.TYaxT',
                    'span.Fc1rA',
                    'span.YwYLL',
                    'strong'
                ];
                for (const selector of selectors) {
                    const text = el.querySelector(selector)?.textContent?.trim();
                    if (!isNoise(text)) return text;
                }
                return (el.innerText || '')
                    .split('\\n')
                    .map((line) => line.trim())
                    .find((line) => !isNoise(line)) || '';
            }
        """)

        results.append({
            "rank": len(results) + 1,
            "place_id": place_id,
            "place_name": _clean_place_name(place_name),
        })

        if len(results) >= limit:
            break

    return results


def _merge_results(target: list[dict], rows: list[dict], seen: set[str], limit: int) -> None:
    for row in rows:
        place_id = str(row.get("place_id") or "").strip()
        place_name = _clean_place_name(row.get("place_name") or "")
        key = place_id or place_name
        if not key or key in seen:
            continue
        seen.add(key)
        target.append({
            "rank": len(target) + 1,
            "place_id": place_id,
            "place_name": place_name,
        })
        if len(target) >= limit:
            break


async def _scroll_result_lists(frame) -> dict:
    return await frame.evaluate("""
        () => {
            const candidates = [
                document.scrollingElement,
                document.documentElement,
                document.body,
                ...document.querySelectorAll('div, section, main, ul, ol, [role="list"]')
            ].filter(Boolean);
            let moved = false;
            let signature = [];

            for (const el of candidates) {
                const max = Math.max(0, el.scrollHeight - el.clientHeight);
                if (max < 40) continue;
                const before = el.scrollTop;
                const step = Math.max(500, Math.round((el.clientHeight || 700) * 0.85));
                el.scrollTop = Math.min(max, before + step);
                if (el.scrollTop !== before) moved = true;
                signature.push(`${Math.round(el.scrollTop)}/${Math.round(max)}`);
            }

            window.scrollBy(0, 700);
            return {
                moved,
                y: Math.round(window.scrollY || 0),
                height: Math.round(document.body?.scrollHeight || 0),
                signature: signature.slice(0, 12).join('|')
            };
        }
    """)


async def _collect_results_from_frame(frame, limit: int) -> list[dict]:
    results = []
    seen = set()
    stagnant = 0
    previous_count = 0
    previous_signature = ""
    started = time.monotonic()

    for _ in range(MAX_SCROLL_ITERATIONS):
        content = await frame.content()
        _merge_results(results, _extract_json_results(content, limit), seen, limit)
        if len(results) < limit:
            _merge_results(results, await _extract_dom_results(frame, limit), seen, limit)
        if len(results) >= limit:
            break

        # 시간 예산 초과 시 그때까지 모은 결과로 종료 (무한 스크롤 방지)
        if time.monotonic() - started >= COLLECT_BUDGET_SECONDS:
            print(f"  ⏱️ 수집 시간 예산({COLLECT_BUDGET_SECONDS}s) 도달 — {len(results)}개로 종료")
            break

        try:
            scroll_state = await _scroll_result_lists(frame)
        except Exception:
            break

        signature = f"{scroll_state.get('y')}|{scroll_state.get('height')}|{scroll_state.get('signature')}"
        if len(results) == previous_count and signature == previous_signature:
            stagnant += 1
        else:
            stagnant = 0
        if stagnant >= 4 or not scroll_state.get("moved"):
            break

        previous_count = len(results)
        previous_signature = signature
        await asyncio.sleep(random.uniform(0.5, 0.9))

    return results


_STEALTH_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
_STEALTH_INIT = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = { runtime: {} };
"""


async def _new_stealth_context(browser):
    context = await browser.new_context(
        user_agent=_STEALTH_UA,
        viewport={"width": 1280, "height": 900},
        locale="ko-KR",
    )
    await context.add_init_script(_STEALTH_INIT)
    return context


async def _collect_keyword_in_context(context, keyword: str, limit: int) -> list[dict]:
    """이미 열려있는 컨텍스트(브라우저)로 한 키워드의 TOP 목록을 수집한다.

    페이지만 새로 열고 닫으므로, 여러 키워드가 브라우저를 재사용해 기동 오버헤드를 없앤다.
    """
    page = await context.new_page()
    try:
        encoded_keyword = urllib.parse.quote(keyword)
        search_url = f"https://map.naver.com/p/search/{encoded_keyword}"
        print(f"  TOP 목록 접속 URL: {search_url}")
        await page.goto(search_url, wait_until="domcontentloaded", timeout=40000)
        await asyncio.sleep(random.uniform(3, 5))

        frame = await _get_search_frame(page)
        results = await _collect_results_from_frame(frame, limit)
        print(f"  TOP 목록 {len(results)}개 수집")
        return results
    except Exception as e:
        print(f"  TOP 목록 수집 오류 [{keyword}]: {e}")
        return []
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def search_keyword_results(keyword: str, limit: int = TOP_LIST_LIMIT) -> list[dict]:
    """단발 호출용 — 브라우저를 새로 띄워 한 키워드를 수집한다."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
        try:
            context = await _new_stealth_context(browser)
            return await _collect_keyword_in_context(context, keyword, limit)
        finally:
            await browser.close()


async def _get_search_frame(page):
    frame = None
    for _ in range(15):
        for candidate in page.frames:
            if "search" in candidate.url and candidate.url != page.url:
                frame = candidate
                break
        if frame:
            break
        await asyncio.sleep(1)

    if not frame:
        iframe_el = await page.query_selector("iframe#searchIframe")
        if iframe_el:
            frame = await iframe_el.content_frame()
    return frame or page


async def _get_place_candidate_items(frame) -> list:
    selectors = ["li.UEzoS", "li[data-laim-exp-id]", "li.VLTHu", "li.CHC5F"]
    for selector in selectors:
        items = await frame.query_selector_all(selector)
        if items:
            return items
    return []


async def _extract_place_candidate_meta(item) -> dict:
    data = await item.evaluate("""
        (el) => {
            const textOf = (selector) => {
                const target = el.querySelector(selector);
                return target?.textContent?.trim() || "";
            };
            const lines = (el.innerText || "")
                .split("\\n")
                .map((line) => line.trim())
                .filter(Boolean);
            const NOISE = /(광고|방문자리뷰|블로그리뷰|이미지수|쿠폰|톡톡|길찾기|저장|공유|예약)/;
            const isNoise = (t) => !t || t.length < 2 || NOISE.test(t)
                || /^\\d+(\\.\\d+)?\\s*(m|km|개|장|명)?$/.test(t);
            const name = [
                "a.place_bluelink span",
                "span.TYaxT",
                "span.Fc1rA",
                "span.YwYLL",
                "strong",
                "a.U70Fj"
            ].map(textOf).find((value) => !isNoise(value)) || "";
            const category = textOf("span.YzBgS");
            const address = lines.find((line) =>
                /^(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주)\\s/.test(line)
            ) || "";
            const distance = lines.find((line) => /\\d+(\\.\\d+)?\\s*(m|km)$/.test(line)) || "";
            return {
                place_name: name,
                category,
                address,
                distance,
                text: lines.slice(0, 12).join(" · ")
            };
        }
    """)
    data["place_name"] = _clean_place_name(data.get("place_name") or "")
    return data


async def _click_candidate_for_place_id(page, item) -> str:
    previous_url = page.url
    before_place_id = _extract_current_place_id(page)

    await item.evaluate("""
        (el) => {
            const target = el.querySelector("a.U70Fj, a.place_thumb, a[href='#'], button, [role='button']") || el;
            for (const type of ["mouseover", "mousedown", "mouseup", "click"]) {
                target.dispatchEvent(new MouseEvent(type, {
                    bubbles: true,
                    cancelable: true,
                    view: window
                }));
            }
        }
    """)

    for _ in range(12):
        await asyncio.sleep(0.35)
        if page.url != previous_url:
            place_id = _extract_place_id_from_text(page.url) or _extract_current_place_id(page)
            if place_id:
                return place_id

    place_id = _extract_current_place_id(page)
    if place_id and place_id != before_place_id:
        return place_id
    return ""


async def search_place_candidates(query: str, limit: int = 5) -> list[dict]:
    query = (query or "").strip()
    limit = max(1, min(int(limit or 5), PLACE_SEARCH_LIMIT))
    if not query:
        return []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="ko-KR",
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        page = await context.new_page()

        try:
            encoded_query = urllib.parse.quote(query)
            search_url = f"https://map.naver.com/p/search/{encoded_query}"
            print(f"  플레이스 ID 검색 URL: {search_url}")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=40000)
            await asyncio.sleep(random.uniform(3, 5))

            frame = await _get_search_frame(page)
            items = await _get_place_candidate_items(frame)
            initial_place_id = _extract_current_place_id(page)

            results = []
            seen = set()
            for index, item in enumerate(items[: limit * 2], start=1):
                meta = await _extract_place_candidate_meta(item)
                place_name = meta.get("place_name") or query
                item_html = await item.inner_html()
                place_id = _extract_place_id_from_text(item_html)

                if index == 1 and initial_place_id:
                    place_id = initial_place_id
                if not place_id:
                    place_id = await _click_candidate_for_place_id(page, item)

                key = place_id or "|".join([
                    _normalize_place_name(place_name),
                    meta.get("category") or "",
                    meta.get("address") or "",
                    meta.get("distance") or "",
                ])
                if not key or key in seen:
                    continue
                seen.add(key)

                results.append({
                    "rank": len(results) + 1,
                    "place_id": place_id,
                    "place_name": place_name,
                    "category": meta.get("category") or "",
                    "address": meta.get("address") or "",
                    "distance": meta.get("distance") or "",
                })
                if len(results) >= limit:
                    break

            if not results and initial_place_id:
                results.append({
                    "rank": 1,
                    "place_id": initial_place_id,
                    "place_name": query,
                    "category": "",
                    "address": "",
                    "distance": "",
                })

            print(f"  플레이스 ID 후보 {len(results)}개 수집")
            return results
        except Exception as e:
            print(f"  플레이스 ID 검색 오류 [{query}]: {e}")
            import traceback
            traceback.print_exc()
            return []
        finally:
            await browser.close()


async def search_place_rank(keyword: str, target_place_id: str, target_place_name: str | None = None) -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="ko-KR",
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        page = await context.new_page()
        rank = -1

        try:
            encoded_keyword = urllib.parse.quote(keyword)
            search_url = f"https://map.naver.com/p/search/{encoded_keyword}"
            print(f"  접속 URL: {search_url}")

            await page.goto(search_url, wait_until="domcontentloaded", timeout=40000)
            await asyncio.sleep(random.uniform(3, 5))

            frame = None
            for _ in range(15):
                for f in page.frames:
                    if "search" in f.url and f.url != page.url:
                        frame = f
                        break
                if frame:
                    break
                await asyncio.sleep(1)

            if not frame:
                iframe_el = await page.query_selector("iframe#searchIframe")
                if iframe_el:
                    frame = await iframe_el.content_frame()
            if not frame:
                frame = page

            print(f"  iframe: {frame.url}")
            await asyncio.sleep(2)

            results = await _collect_results_from_frame(frame, MAX_RANK)
            print(f"  목록에서 {len(results)}개 플레이스 발견")
            target_name_key = _normalize_place_name(target_place_name or "")
            for row in results:
                place_id = row.get("place_id")
                place_name = row.get("place_name") or ""
                real_rank = int(row.get("rank") or 0)
                print(f"  {real_rank}위: {place_id or '-'} {place_name}")
                if place_id == target_place_id or (
                    target_name_key and _place_names_match(place_name, target_place_name or "")
                ):
                    rank = real_rank
                    print(f"  ✅ 발견! 실제 {rank}위")
                    break

            if rank == -1:
                print(f"  못찾음 → {TOP_LIST_LIMIT}위 밖")

        except Exception as e:
            print(f"  크롤링 오류 [{keyword}]: {e}")
            import traceback
            traceback.print_exc()
            rank = -1
        finally:
            await browser.close()

        return rank


async def _set_check_progress(user_id: str, message: str, details: dict | None = None) -> None:
    try:
        await database.set_check_status(user_id, "running", message, details)
    except Exception as exc:
        print(f"  체크 상태 업데이트 실패: {exc}")


def _legacy_keyword_place_id(places: list[dict]) -> str:
    if not places:
        return ""
    oldest = min(places, key=lambda row: row.get("created_at") or "")
    return str(oldest.get("place_id") or "")


def _build_place_keyword_pairs(places: list[dict], keywords: list[dict]) -> list[dict]:
    places_by_id = {str(place.get("place_id") or ""): place for place in places}
    legacy_place_id = _legacy_keyword_place_id(places)
    pairs = []
    seen = set()

    for keyword_row in keywords:
        keyword = str(keyword_row.get("keyword") or "").strip()
        if not keyword:
            continue
        place_id = str(keyword_row.get("place_id") or "").strip() or legacy_place_id
        place = places_by_id.get(place_id)
        if not place:
            continue
        key = (place_id, keyword)
        if key in seen:
            continue
        seen.add(key)
        pairs.append({
            "place": place,
            "keyword": keyword,
            "keyword_row": keyword_row,
        })

    return pairs


async def run_daily_check(user_id: str | None = None, force: bool = False):
    # force=True면 "오늘 이미 체크됨" 스킵을 무시하고 전 키워드를 다시 크롤링한다.
    # (크롤러 로직 수정 후 오늘치 목록을 새로 덮어쓰고 싶을 때 사용)
    # 날짜는 KST 기준으로 기록 (서버가 UTC여도 한국 날짜로 저장)
    today = _now_kst().strftime("%Y-%m-%d")
    user_ids = [user_id] if user_id else await database.list_user_ids()
    summary = {
        "users": len(user_ids),
        "checked_users": 0,
        "skipped_users": 0,
        "places": 0,
        "keywords": 0,
        "rankings_saved": 0,
        "keyword_results_saved": 0,
        "skipped_already_checked": 0,
        "failed_keywords": [],
    }

    if not user_ids:
        print("등록된 사용자 없음")
        return summary

    print(f"[{_now_kst().strftime('%Y-%m-%d %H:%M:%S')}] 순위 체크 시작")

    for owner_uid in user_ids:
        places = await database.get_places(owner_uid)
        keywords = await database.get_keywords(owner_uid)
        place_keyword_pairs = _build_place_keyword_pairs(places, keywords)

        if not places or not place_keyword_pairs:
            print(f"사용자 {owner_uid}: 등록된 플레이스 또는 플레이스별 키워드 없음")
            summary["skipped_users"] += 1
            continue

        summary["checked_users"] += 1
        summary["places"] += len(places)
        summary["keywords"] += len(place_keyword_pairs)

        pairs_by_keyword: dict[str, list[dict]] = {}
        for pair in place_keyword_pairs:
            pairs_by_keyword.setdefault(pair["keyword"], []).append(pair)

        # 오늘 이미 체크한 조합은 (즉시 체크를 돌려도) 다시 크롤링/저장하지 않는다.
        # force면 이 스킵을 끄고 전부 다시 수집한다.
        checked_today = set() if force else await database.get_checked_pairs_for_date(owner_uid, today)

        unique_keywords = list(pairs_by_keyword.keys())
        total_keywords = len(unique_keywords)

        # 이 사용자의 모든 조합이 오늘 이미 체크됐으면 브라우저를 아예 띄우지 않는다.
        has_pending = any(
            (pair["place"]["place_id"], kw) not in checked_today
            for kw in unique_keywords
            for pair in pairs_by_keyword[kw]
        )
        if not has_pending:
            summary["skipped_already_checked"] += sum(len(v) for v in pairs_by_keyword.values())
            print(f"  ⏭️ 사용자 {owner_uid}: 오늘 모든 키워드 이미 체크됨 — 건너뜀")
            continue

        # 브라우저를 한 번만 띄워 이 사용자의 모든 키워드에 재사용 (기동 오버헤드 제거)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
            try:
                context = await _new_stealth_context(browser)

                for keyword_index, keyword in enumerate(unique_keywords, start=1):
                    # 이 키워드에서 오늘 아직 체크하지 않은 플레이스만 대상으로 남긴다.
                    pending_pairs = [
                        pair for pair in pairs_by_keyword[keyword]
                        if (pair["place"]["place_id"], keyword) not in checked_today
                    ]
                    skipped = len(pairs_by_keyword[keyword]) - len(pending_pairs)
                    summary["skipped_already_checked"] += skipped

                    if not pending_pairs:
                        print(f"  ⏭️ [{keyword}] 오늘 이미 체크됨 — 건너뜀")
                        await _set_check_progress(
                            owner_uid,
                            f"{keyword} 오늘 이미 체크됨 — 건너뜀 ({keyword_index}/{total_keywords})",
                            {"current_keyword": keyword, "keyword_index": keyword_index, "keyword_total": total_keywords},
                        )
                        continue

                    await _set_check_progress(
                        owner_uid,
                        f"{keyword} TOP {TOP_LIST_LIMIT} 수집 중 ({keyword_index}/{total_keywords})",
                        {"current_keyword": keyword, "keyword_index": keyword_index, "keyword_total": total_keywords},
                    )

                    try:
                        keyword_results = await asyncio.wait_for(
                            _collect_keyword_in_context(context, keyword, TOP_LIST_LIMIT),
                            timeout=KEYWORD_CRAWL_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        keyword_results = []
                        print(f"  ⏱️ [{keyword}] 크롤 타임아웃({KEYWORD_CRAWL_TIMEOUT}s) — 건너뜀")

                    if not keyword_results:
                        # 수집 0건 = 크롤링 실패로 간주 (네이버 구조 변경/차단 가능성).
                        # "순위 밖(-1)"으로 잘못 기록하지 않도록 이 키워드는 저장을 건너뜀.
                        summary["failed_keywords"].append(keyword)
                        print(f"  ⚠️ [{keyword}] 검색 결과 수집 실패 — 순위 저장 건너뜀")
                        await _set_check_progress(
                            owner_uid,
                            f"{keyword} 수집 실패 — 저장 건너뜀 ({keyword_index}/{total_keywords})",
                            {"current_keyword": keyword, "keyword_index": keyword_index, "keyword_total": total_keywords},
                        )
                        continue

                    await database.save_keyword_results(keyword, keyword_results, today, owner_uid)
                    summary["keyword_results_saved"] += len(keyword_results)
                    rank_by_place_id, rank_by_name = _build_rank_indexes(keyword_results)

                    for pair in pending_pairs:
                        place = pair["place"]
                        place_id = place["place_id"]
                        place_name = place["place_name"]
                        print(f"  사용자 {owner_uid} 체크 중: [{keyword}] {place_name}({place_id})")

                        rank = rank_by_place_id.get(place_id)
                        if rank is None:
                            rank = rank_by_name.get(_normalize_place_name(place_name))
                        if rank is None:
                            rank = _rank_from_place_name(keyword_results, place_name)
                        if rank is None:
                            rank = -1
                        await database.save_ranking(place_id, keyword, rank, today, owner_uid)
                        summary["rankings_saved"] += 1

                        rank_str = f"{rank}위" if rank > 0 else f"{TOP_LIST_LIMIT}위 밖"
                        print(f"  결과: [{keyword}] {place_name} → {rank_str}")

                    await _set_check_progress(
                        owner_uid,
                        f"{keyword} 기본 체크 저장 완료 ({keyword_index}/{total_keywords})",
                        {"current_keyword": keyword, "keyword_index": keyword_index, "keyword_total": total_keywords},
                    )
            finally:
                await browser.close()

    print(f"[{_now_kst().strftime('%Y-%m-%d %H:%M:%S')}] 순위 체크 완료")
    return summary

if __name__ == "__main__":
    async def main():
        await database.init_db()
        await run_daily_check()

    asyncio.run(main())

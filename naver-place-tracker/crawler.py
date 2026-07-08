import asyncio
import random
import urllib.parse
import re
import json
from datetime import datetime
from playwright.async_api import async_playwright
import database

MAX_RANK = 100
TOP_LIST_LIMIT = 30


def _decode_json_text(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return value


def _clean_place_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name or "").strip()
    blocked = {"광고", "저장", "공유", "방문자리뷰", "블로그리뷰", "예약", "길찾기"}
    return "" if cleaned in blocked else cleaned


def _normalize_place_name(name: str) -> str:
    cleaned = _clean_place_name(name)
    return re.sub(r"[\s·ㆍ\-_(){}\[\].,'\"`]+", "", cleaned).lower()


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
                const selectors = [
                    'a.place_bluelink span',
                    'span.TYaxT',
                    'span.Fc1rA',
                    'span.YwYLL',
                    'strong',
                    'a'
                ];
                for (const selector of selectors) {
                    const target = el.querySelector(selector);
                    const text = target?.textContent?.trim();
                    if (text && text.length > 1 && !text.includes('광고')) return text;
                }
                return (el.innerText || '')
                    .split('\\n')
                    .map((line) => line.trim())
                    .find((line) => line && !line.includes('광고') && !line.includes('방문자리뷰')) || '';
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

    for _ in range(40):
        content = await frame.content()
        _merge_results(results, _extract_json_results(content, limit), seen, limit)
        if len(results) < limit:
            _merge_results(results, await _extract_dom_results(frame, limit), seen, limit)
        if len(results) >= limit:
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
        if stagnant >= 5 or not scroll_state.get("moved"):
            break

        previous_count = len(results)
        previous_signature = signature
        await asyncio.sleep(random.uniform(0.8, 1.4))

    return results


async def search_keyword_results(keyword: str, limit: int = TOP_LIST_LIMIT) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
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
            encoded_keyword = urllib.parse.quote(keyword)
            search_url = f"https://map.naver.com/p/search/{encoded_keyword}"
            print(f"  TOP 목록 접속 URL: {search_url}")
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

            results = await _collect_results_from_frame(frame, limit)

            print(f"  TOP 목록 {len(results)}개 수집")
            return results
        except Exception as e:
            print(f"  TOP 목록 수집 오류 [{keyword}]: {e}")
            import traceback
            traceback.print_exc()
            return []
        finally:
            await browser.close()

async def search_place_rank(keyword: str, target_place_id: str, target_place_name: str | None = None) -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
        )
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
                    target_name_key and _normalize_place_name(place_name) == target_name_key
                ):
                    rank = real_rank
                    print(f"  ✅ 발견! 실제 {rank}위")
                    break

            if rank == -1:
                print(f"  못찾음 → 100위 밖")

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


async def run_daily_check(user_id: str | None = None):
    today = datetime.now().strftime("%Y-%m-%d")
    user_ids = [user_id] if user_id else await database.list_user_ids()
    summary = {
        "users": len(user_ids),
        "checked_users": 0,
        "skipped_users": 0,
        "places": 0,
        "keywords": 0,
        "rankings_saved": 0,
        "keyword_results_saved": 0,
    }

    if not user_ids:
        print("등록된 사용자 없음")
        return summary

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 순위 체크 시작")

    for owner_uid in user_ids:
        places = await database.get_places(owner_uid)
        keywords = await database.get_keywords(owner_uid)

        if not places or not keywords:
            print(f"사용자 {owner_uid}: 등록된 플레이스 또는 키워드 없음")
            summary["skipped_users"] += 1
            continue

        summary["checked_users"] += 1
        summary["places"] += len(places)
        summary["keywords"] += len(keywords)

        pending_deep_checks = []
        total_keywords = len(keywords)

        for keyword_index, keyword_row in enumerate(keywords, start=1):
            keyword = keyword_row["keyword"]
            await _set_check_progress(
                owner_uid,
                f"{keyword} TOP 30 수집 중 ({keyword_index}/{total_keywords})",
                {"current_keyword": keyword, "keyword_index": keyword_index, "keyword_total": total_keywords},
            )

            keyword_results = await search_keyword_results(keyword, TOP_LIST_LIMIT)
            if keyword_results:
                await database.save_keyword_results(
                    keyword,
                    keyword_results,
                    today,
                    owner_uid,
                )
                summary["keyword_results_saved"] += len(keyword_results)
            rank_by_place_id, rank_by_name = _build_rank_indexes(keyword_results)

            for place in places:
                place_id = place["place_id"]
                place_name = place["place_name"]
                print(f"  사용자 {owner_uid} 체크 중: [{keyword}] {place_name}({place_id})")

                rank = rank_by_place_id.get(place_id)
                if rank is None:
                    rank = rank_by_name.get(_normalize_place_name(place_name))
                if rank is None:
                    if len(keyword_results) >= TOP_LIST_LIMIT:
                        rank = 0
                        pending_deep_checks.append((keyword, place_id, place_name))
                    else:
                        rank = -1
                await database.save_ranking(place_id, keyword, rank, today, owner_uid)
                summary["rankings_saved"] += 1

                if rank > 0:
                    rank_str = f"{rank}위"
                elif rank == 0:
                    rank_str = "상세 확인 중"
                else:
                    rank_str = "TOP 30 밖"
                print(f"  결과: [{keyword}] {place_name} → {rank_str}")

            await _set_check_progress(
                owner_uid,
                f"{keyword} 기본 체크 저장 완료 ({keyword_index}/{total_keywords})",
                {"current_keyword": keyword, "keyword_index": keyword_index, "keyword_total": total_keywords},
            )

        total_deep_checks = len(pending_deep_checks)
        for deep_index, (keyword, place_id, place_name) in enumerate(pending_deep_checks, start=1):
            await _set_check_progress(
                owner_uid,
                f"{keyword} 상세 순위 확인 중 ({deep_index}/{total_deep_checks})",
                {"current_keyword": keyword, "deep_index": deep_index, "deep_total": total_deep_checks},
            )
            print(f"  상세 확인 중: [{keyword}] {place_name}({place_id})")
            rank = await search_place_rank(keyword, place_id, place_name)
            if rank > 0:
                await database.save_ranking(place_id, keyword, rank, today, owner_uid)
                print(f"  상세 결과 갱신: [{keyword}] {place_name} → {rank}위")
            else:
                await database.save_ranking(place_id, keyword, -1, today, owner_uid)
                print(f"  상세 결과: [{keyword}] {place_name} → 100위 밖")

            await asyncio.sleep(random.uniform(1, 2))

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 순위 체크 완료")
    return summary

if __name__ == "__main__":
    async def main():
        await database.init_db()
        await run_daily_check()

    asyncio.run(main())

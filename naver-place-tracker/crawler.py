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
            await page.goto(search_url, wait_until="networkidle", timeout=40000)
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

            prev_height = 0
            for scroll in range(20):
                try:
                    curr_height = await frame.evaluate("document.body.scrollHeight")
                    if curr_height == prev_height and scroll > 3:
                        break
                    prev_height = curr_height
                    await frame.evaluate("window.scrollBy(0, 800)")
                    await asyncio.sleep(random.uniform(0.8, 1.5))
                except Exception:
                    break

            content = await frame.content()
            results = _extract_json_results(content, limit)
            if not results:
                results = await _extract_dom_results(frame, limit)

            print(f"  TOP 목록 {len(results)}개 수집")
            return results
        except Exception as e:
            print(f"  TOP 목록 수집 오류 [{keyword}]: {e}")
            import traceback
            traceback.print_exc()
            return []
        finally:
            await browser.close()

async def search_place_rank(keyword: str, target_place_id: str) -> int:
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

            await page.goto(search_url, wait_until="networkidle", timeout=40000)
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

            prev_height = 0
            for scroll in range(20):
                try:
                    curr_height = await frame.evaluate("document.body.scrollHeight")
                    if curr_height == prev_height and scroll > 3:
                        break
                    prev_height = curr_height
                    await frame.evaluate("window.scrollBy(0, 800)")
                    await asyncio.sleep(random.uniform(0.8, 1.5))
                except:
                    break

            content = await frame.content()
            pattern = r'"(?:RestaurantListSummary|PlaceListSummary|CafeListSummary|HotelListSummary):(\d+):\d+"'
            matches = re.findall(pattern, content)

            if matches:
                print(f"  JSON에서 {len(matches)}개 플레이스 발견")
                real_rank = 0
                seen = []
                for place_id in matches:
                    if place_id in seen:
                        continue
                    seen.append(place_id)

                    ad_pattern = f'"id":"{place_id}".*?"isAd":true'
                    is_ad = bool(re.search(ad_pattern, content))

                    if is_ad:
                        print(f"  ⚡ 광고 건너뜀: {place_id}")
                        continue

                    real_rank += 1
                    print(f"  {real_rank}위: {place_id}")

                    if place_id == target_place_id:
                        rank = real_rank
                        print(f"  ✅ 발견! 실제 {rank}위")
                        break

                    if real_rank >= MAX_RANK:
                        break

            if rank == -1:
                print(f"  JSON 방법 실패, li 태그 방법 시도")
                items = []
                for selector in ["li.UEzoS", "li[data-laim-exp-id]", "li.VLTHu", "li.CHC5F"]:
                    items = await frame.query_selector_all(selector)
                    if items:
                        print(f"  {len(items)}개 li 항목 발견")
                        break

                real_rank = 0
                for item in items:
                    item_html = await item.inner_html()

                    is_ad = False
                    ad_spans = await item.query_selector_all("span.place_blind")
                    for span in ad_spans:
                        text = await span.inner_text()
                        if "광고" in text:
                            is_ad = True
                            break

                    if is_ad:
                        print(f"  ⚡ 광고 건너뜀")
                        continue

                    real_rank += 1

                    if target_place_id in item_html:
                        rank = real_rank
                        print(f"  ✅ li에서 발견! {rank}위")
                        break

                    if real_rank >= MAX_RANK:
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

        for keyword_row in keywords:
            keyword = keyword_row["keyword"]
            keyword_results = await search_keyword_results(keyword, MAX_RANK)
            if keyword_results:
                await database.save_keyword_results(
                    keyword,
                    keyword_results[:TOP_LIST_LIMIT],
                    today,
                    owner_uid,
                )
                summary["keyword_results_saved"] += min(len(keyword_results), TOP_LIST_LIMIT)
            rank_by_place_id = {
                row["place_id"]: row["rank"]
                for row in keyword_results
                if row.get("place_id")
            }

            for place in places:
                place_id = place["place_id"]
                place_name = place["place_name"]
                print(f"  사용자 {owner_uid} 체크 중: [{keyword}] {place_name}({place_id})")

                rank = rank_by_place_id.get(place_id)
                if rank is None and len(keyword_results) < MAX_RANK:
                    rank = await search_place_rank(keyword, place_id)
                if rank is None:
                    rank = -1
                await database.save_ranking(place_id, keyword, rank, today, owner_uid)
                summary["rankings_saved"] += 1

                rank_str = f"{rank}위" if rank > 0 else "100위 밖"
                print(f"  결과: [{keyword}] {place_name} → {rank_str}")

                await asyncio.sleep(random.uniform(3, 7))

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 순위 체크 완료")
    return summary

if __name__ == "__main__":
    async def main():
        await database.init_db()
        await run_daily_check()

    asyncio.run(main())

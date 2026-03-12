import asyncio
import random
import urllib.parse
import re
import json
from datetime import datetime
from playwright.async_api import async_playwright
import database

MAX_RANK = 100

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


async def run_daily_check():
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 순위 체크 시작")

    places = await database.get_places()
    keywords = await database.get_keywords()

    if not places or not keywords:
        print("등록된 플레이스 또는 키워드 없음")
        return

    for keyword_row in keywords:
        keyword = keyword_row["keyword"]
        for place in places:
            place_id = place["place_id"]
            place_name = place["place_name"]
            print(f"  체크 중: [{keyword}] {place_name}({place_id})")

            rank = await search_place_rank(keyword, place_id)
            await database.save_ranking(place_id, keyword, rank, today)

            rank_str = f"{rank}위" if rank > 0 else "100위 밖"
            print(f"  결과: [{keyword}] {place_name} → {rank_str}")

            await asyncio.sleep(random.uniform(3, 7))

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 순위 체크 완료")


if __name__ == "__main__":
    asyncio.run(run_daily_check())

if __name__ == "__main__":
    asyncio.run(run_daily_check())

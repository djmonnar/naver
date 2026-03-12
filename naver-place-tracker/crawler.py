import asyncio
import random
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
            search_url = f"https://map.naver.com/v5/search/{keyword}"
            await page.goto(search_url, wait_until="networkidle", timeout=40000)
            await asyncio.sleep(random.uniform(2, 4))

            # searchIframe 찾기
            frame = None
            for _ in range(10):
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

            await asyncio.sleep(2)

            current_rank = 0
            found = False
            scroll_count = 0

            while scroll_count < 25 and not found and current_rank < MAX_RANK:
                # 여러 selector 시도
                items = []
                for selector in ["li.UEzoS", "li[data-laim-exp-id]", ".place_bluelink", "li.VLTHu"]:
                    items = await frame.query_selector_all(selector)
                    if items:
                        break

                for item in items:
                    # ID 추출 시도
                    item_id = await item.get_attribute("data-laim-exp-id") or ""
                    inner_html = await item.inner_html()

                    if target_place_id in item_id or target_place_id in inner_html:
                        current_rank += 1
                        rank = current_rank
                        found = True
                        break

                    current_rank += 1
                    if current_rank >= MAX_RANK:
                        break

                if found:
                    break

                # 스크롤
                try:
                    await frame.evaluate("window.scrollBy(0, 600)")
                except:
                    await page.evaluate("window.scrollBy(0, 600)")

                await asyncio.sleep(random.uniform(0.8, 1.5))
                scroll_count += 1

        except Exception as e:
            print(f"크롤링 오류 [{keyword}]: {e}")
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

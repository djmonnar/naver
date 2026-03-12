import asyncio
import random
import urllib.parse
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

            # searchIframe 찾기
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

            found = False
            scroll_count = 0
            prev_height = 0
            real_rank = 0  # 광고 제외 실제 순위

            while scroll_count < 30 and not found and real_rank < MAX_RANK:
                # 리스트 아이템 전체 가져오기
                items = []
                for selector in ["li.UEzoS", "li[data-laim-exp-id]", "li.VLTHu", "li.CHC5F"]:
                    items = await frame.query_selector_all(selector)
                    if items:
                        break

                for item in items:
                    item_html = await item.inner_html()

                    # 광고 여부 확인 - place_blind 태그 안에 "광고" 텍스트
                    is_ad = False
                    ad_spans = await item.query_selector_all("span.place_blind")
                    for span in ad_spans:
                        text = await span.inner_text()
                        if "광고" in text:
                            is_ad = True
                            break

                    if is_ad:
                        print(f"  ⚡ 광고 건너뜀")
                        continue  # 광고는 순위 카운트 안 함

                    real_rank += 1  # 실제 순위 카운트

                    if target_place_id in item_html:
                        rank = real_rank
                        found = True
                        print(f"  ✅ 발견! 실제 {rank}위 (광고 제외)")
                        break

                    if real_rank >= MAX_RANK:
                        break

                if found:
                    break

                # 스크롤
                try:
                    curr_height = await frame.evaluate("document.body.scrollHeight")
                    if curr_height == prev_height and scroll_count > 5:
                        print(f"  스크롤 끝, 탐색 종료")
                        break
                    prev_height = curr_height
                    await frame.evaluate("window.scrollBy(0, 800)")
                except:
                    await page.keyboard.press("PageDown")

                await asyncio.sleep(random.uniform(1, 2))
                scroll_count += 1

                if scroll_count % 5 == 0:
                    print(f"  스크롤 {scroll_count}회, 실제 {real_rank}위까지 탐색")

        except Exception as e:
            print(f"  크롤링 오류 [{keyword}]: {e}")
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

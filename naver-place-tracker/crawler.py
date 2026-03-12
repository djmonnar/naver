
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
            # 새 URL 구조 /p/ 사용
            import urllib.parse
            encoded_keyword = urllib.parse.quote(keyword)
            search_url = f"https://map.naver.com/p/search/{encoded_keyword}"
            print(f"  접속 URL: {search_url}")

            await page.goto(search_url, wait_until="networkidle", timeout=40000)
            await asyncio.sleep(random.uniform(3, 5))

            # searchIframe 찾기 (최대 15초 대기)
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
                print(f"  iframe 못찾음, page로 대체")
                frame = page

            print(f"  iframe 확인: {frame.url}")
            await asyncio.sleep(2)

            current_rank = 0
            found = False
            scroll_count = 0
            prev_height = 0

            while scroll_count < 30 and not found and current_rank < MAX_RANK:
                # 페이지 전체 HTML에서 place ID 검색
                try:
                    content = await frame.content()
                    if target_place_id in content:
                        # 몇 번째인지 계산
                        # 리스트 아이템 찾기
                        items = []
                        for selector in [
                            "li.UEzoS", 
                            "li[data-laim-exp-id]",
                            "li.VLTHu",
                            ".place_bluelink",
                            "li.CHC5F",
                        ]:
                            items = await frame.query_selector_all(selector)
                            if items:
                                break

                        if items:
                            for i, item in enumerate(items):
                                item_html = await item.inner_html()
                                if target_place_id in item_html:
                                    rank = i + 1
                                    found = True
                                    print(f"  ✅ 발견! {rank}위 (selector로)")
                                    break
                        
                        if not found:
                            # selector 못찾으면 텍스트 위치로 추정
                            idx = content.find(target_place_id)
                            before = content[:idx]
                            # 앞에 나온 place entry 개수 세기
                            count = before.count('data-laim-exp-id')
                            if count == 0:
                                count = before.count('/entry/place/')
                            rank = max(1, count)
                            found = True
                            print(f"  📍 텍스트 탐지: 약 {rank}위")

                except Exception as e:
                    print(f"  content 검색 오류: {e}")

                if found:
                    break

                # 스크롤
                try:
                    curr_height = await frame.evaluate("document.body.scrollHeight")
                    if curr_height == prev_height and scroll_count > 5:
                        print(f"  스크롤 끝 도달, 탐색 종료")
                        break
                    prev_height = curr_height
                    await frame.evaluate("window.scrollBy(0, 800)")
                except:
                    await page.keyboard.press("PageDown")

                await asyncio.sleep(random.uniform(1, 2))
                scroll_count += 1
                
                if scroll_count % 5 == 0:
                    print(f"  스크롤 {scroll_count}회, 현재 {current_rank}위까지 탐색")

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

import asyncio
import random
from datetime import datetime
from playwright.async_api import async_playwright
import database

# 네이버 플레이스 검색 시 확인할 최대 순위
MAX_RANK = 100

async def search_place_rank(keyword: str, target_place_id: str) -> int:
    """
    네이버 지도에서 키워드 검색 후 플레이스 ID의 순위 반환
    못 찾으면 -1 반환
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="ko-KR",
        )
        # 자동화 감지 우회
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        page = await context.new_page()
        rank = -1
        try:
            # 네이버 지도 검색
            search_url = f"https://map.naver.com/v5/search/{keyword}"
            await page.goto(search_url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(random.uniform(2, 4))

            # 검색 결과 iframe 진입
            frame = None
            for frame_candidate in page.frames:
                if "search" in frame_candidate.url:
                    frame = frame_candidate
                    break

            if not frame:
                # iframe 직접 찾기
                iframe_element = await page.query_selector("iframe#searchIframe")
                if iframe_element:
                    frame = await iframe_element.content_frame()

            if not frame:
                frame = page

            await asyncio.sleep(random.uniform(1, 2))

            # 순위 추적 - 스크롤하며 리스트 수집
            current_rank = 0
            found = False
            scroll_count = 0
            max_scrolls = 20

            while scroll_count < max_scrolls and not found and current_rank < MAX_RANK:
                # 리스트 아이템 수집 (여러 selector 시도)
                items = await frame.query_selector_all("li.UEzoS") 
                if not items:
                    items = await frame.query_selector_all("li[data-laim-exp-id]")
                if not items:
                    items = await frame.query_selector_all(".place_bluelink")

                for item in items:
                    # place ID 추출 시도
                    item_id = await item.get_attribute("data-laim-exp-id") or ""
                    
                    # href에서 ID 추출
                    link = await item.query_selector("a")
                    href = ""
                    if link:
                        href = await link.get_attribute("href") or ""
                    
                    # onclick이나 다른 속성에서 ID 확인
                    onclick = await item.get_attribute("onclick") or ""
                    
                    if (target_place_id in item_id or 
                        target_place_id in href or 
                        target_place_id in onclick):
                        current_rank += 1
                        rank = current_rank
                        found = True
                        break
                    
                    current_rank += 1

                if found:
                    break

                # 스크롤 다운
                try:
                    await frame.evaluate("window.scrollBy(0, 800)")
                except:
                    await page.evaluate("window.scrollBy(0, 800)")
                await asyncio.sleep(random.uniform(0.5, 1.5))
                scroll_count += 1

        except Exception as e:
            print(f"크롤링 오류 [{keyword}]: {e}")
            rank = -1
        finally:
            await browser.close()

        return rank


async def run_daily_check():
    """매일 실행되는 전체 순위 체크"""
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

            # 요청 간 딜레이 (차단 방지)
            await asyncio.sleep(random.uniform(3, 7))

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 순위 체크 완료")


if __name__ == "__main__":
    asyncio.run(run_daily_check())

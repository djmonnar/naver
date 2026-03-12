import asyncio
import random
import httpx
from datetime import datetime
import database

MAX_RANK = 100
PAGE_SIZE = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://map.naver.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Origin": "https://map.naver.com",
}


async def search_place_rank(keyword: str, target_place_id: str) -> int:
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
        for page in range(1, (MAX_RANK // PAGE_SIZE) + 2):
            try:
                url = "https://map.naver.com/v5/api/search"
                params = {
                    "query": keyword,
                    "type": "all",
                    "page": page,
                    "displayCount": PAGE_SIZE,
                    "lang": "ko",
                }
                resp = await client.get(url, params=params)

                if resp.status_code != 200:
                    print(f"  API 응답 오류: {resp.status_code}")
                    break

                data = resp.json()

                # 결과에서 place 목록 추출
                places = []
                if "result" in data:
                    result = data["result"]
                    for key in ["place", "places", "business", "businesses"]:
                        if key in result and result[key]:
                            items = result[key].get("list", result[key]) if isinstance(result[key], dict) else result[key]
                            if items:
                                places = items
                                break

                if not places:
                    # 전체 응답 텍스트에서 ID 탐색 (백업)
                    if target_place_id in resp.text:
                        before = resp.text[:resp.text.find(target_place_id)]
                        approx_rank = before.count('"id"') + (page - 1) * PAGE_SIZE + 1
                        print(f"  📍 텍스트 탐지: [{keyword}] 약 {approx_rank}위")
                        return min(approx_rank, MAX_RANK)
                    break

                base_rank = (page - 1) * PAGE_SIZE
                for i, place in enumerate(places):
                    rank = base_rank + i + 1
                    pid = str(place.get("id", ""))
                    pid2 = str(place.get("placeId", ""))
                    pid3 = str(place.get("poiId", ""))

                    if target_place_id in (pid, pid2, pid3):
                        name = place.get("name", place.get("placeName", ""))
                        print(f"  ✅ 발견! [{keyword}] {name} → {rank}위")
                        return rank

                    if rank >= MAX_RANK:
                        return -1

                await asyncio.sleep(random.uniform(0.5, 1.5))

            except Exception as e:
                print(f"  오류 [{keyword}] 페이지 {page}: {e}")
                break

    return -1


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

            await asyncio.sleep(random.uniform(2, 4))

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 순위 체크 완료")


if __name__ == "__main__":
    asyncio.run(run_daily_check())

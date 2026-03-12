# 📍 네이버 플레이스 순위 트래커

네이버 플레이스의 키워드별 순위를 매일 자동으로 추적하고 차트로 시각화합니다.

---

## 🚀 배포 방법 (Railway - 무료)

### 1단계: GitHub에 코드 올리기

1. [github.com](https://github.com) 회원가입 (이미 있으면 생략)
2. 새 Repository 만들기 (이름: `place-tracker`)
3. 이 폴더의 모든 파일을 업로드

```
place-tracker/
├── main.py
├── crawler.py
├── database.py
├── requirements.txt
├── Dockerfile
├── railway.toml
└── templates/
    └── dashboard.html
```

### 2단계: Railway에 배포하기

1. [railway.app](https://railway.app) 접속 → GitHub으로 로그인
2. **New Project** → **Deploy from GitHub repo**
3. 방금 만든 `place-tracker` 선택
4. 자동으로 빌드 시작 (5~10분 소요)
5. **Settings** → **Domains** → **Generate Domain** 클릭
6. 생성된 URL로 접속 완료!

### 3단계: 볼륨(데이터 저장소) 설정

1. Railway 프로젝트 → **Add Service** → **Volume**
2. Mount Path: `/app/data`
3. `main.py`에서 DB_PATH를 `/app/data/tracker.db`로 설정됨

---

## 💡 플레이스 ID 찾는 방법

1. [네이버 지도](https://map.naver.com) 접속
2. 찾고 싶은 가게 검색 후 클릭
3. URL 확인: `https://map.naver.com/v5/entry/place/`**`123456789`**
4. 숫자 부분이 플레이스 ID

---

## ⚠️ 주의사항

- 네이버 크롤링 차단 정책으로 인해 순위 탐지가 실패할 수 있습니다
- 차단 시 자동으로 -1(미탐지)로 기록됩니다
- 과도한 요청은 IP 차단의 원인이 될 수 있습니다
- 매일 1회 체크를 권장합니다

---

## 📊 기능

- ✅ 플레이스 등록/삭제
- ✅ 검색 키워드 등록/삭제
- ✅ 매일 오전 9시 자동 순위 체크
- ✅ 수동 즉시 체크
- ✅ 30일 순위 추이 차트
- ✅ 현재 순위 요약 테이블

# 📍 네이버 플레이스 순위 트래커

네이버 플레이스의 키워드별 순위를 매일 자동으로 추적하고 차트로 시각화합니다.

---

## 🚀 배포 방법

### 📋 준비물
- GitHub 계정 (없으면 github.com에서 무료 가입)
- Render 계정 (없으면 render.com에서 무료 가입)

---

## 1단계: GitHub에 코드 올리기

### 1-1. GitHub 저장소 만들기
1. [github.com](https://github.com) 로그인
2. 오른쪽 상단 **`+`** 버튼 → **New repository**
3. Repository name: `place-tracker`
4. **Public** 선택
5. **Create repository** 클릭

### 1-2. 파일 업로드
1. 생성된 저장소 페이지에서 **`uploading an existing file`** 클릭
2. 아래 파일들을 모두 드래그해서 업로드:

```
✅ main.py
✅ crawler.py
✅ database.py
✅ requirements.txt
✅ Dockerfile
✅ render.yaml
```

3. `templates/dashboard.html` 은 GitHub에서 직접:
   - **Add file** → **Create new file**
   - 파일명: `templates/dashboard.html`
   - 내용 붙여넣기 후 저장

4. **Commit changes** 클릭

---

## 2단계: Render에 배포하기

### 2-1. Render 가입 및 연결
1. [render.com](https://render.com) 접속
2. **Get Started for Free** → **GitHub으로 로그인**
3. GitHub 권한 허용

### 2-2. 새 서비스 만들기
1. 대시보드에서 **New +** → **Web Service**
2. **Connect a repository** → `place-tracker` 선택
3. 아래와 같이 설정:

| 항목 | 값 |
|------|-----|
| Name | place-tracker |
| Region | Singapore (한국과 가장 가까움) |
| Branch | main |
| Runtime | **Docker** |
| Plan | **Free** |

4. **Create Web Service** 클릭
5. 자동으로 빌드 시작 (처음엔 5~15분 소요)

### 2-3. 데이터 저장 볼륨 추가 (중요!)
> 이걸 안 하면 서버 재시작마다 데이터가 사라져요!

1. 생성된 서비스 페이지 → 왼쪽 메뉴 **Disks**
2. **Add Disk** 클릭
3. 설정:

| 항목 | 값 |
|------|-----|
| Name | tracker-data |
| Mount Path | /app/data |
| Size | 1 GB |

4. **Save** 클릭 → 자동 재배포

### 2-4. 환경변수 설정
1. 왼쪽 메뉴 **Environment**
2. **Add Environment Variable** 클릭:

| Key | Value |
|-----|-------|
| DB_PATH | /app/data/tracker.db |

3. **Save Changes**

### 2-4-1. Firebase 로그인/Firestore 사용
Firebase 프로젝트를 만든 뒤 아래 설정을 추가하면 계정별 데이터 저장으로 전환됩니다.

1. Firebase 콘솔 → **Authentication** → **Sign-in method**에서 **Email/Password** 활성화
2. Firebase 콘솔 → **Firestore Database** 생성
3. Firebase 콘솔 → 프로젝트 설정 → **웹 앱** 추가 후 SDK 설정값 복사
4. Firebase 콘솔 → 프로젝트 설정 → **서비스 계정** → 새 비공개 키 생성
5. Render 환경변수에 추가:

| Key | Value |
|-----|-------|
| DATA_BACKEND | firestore |
| FIREBASE_AUTH_ENABLED | true |
| FIREBASE_API_KEY | 웹 앱 설정의 apiKey |
| FIREBASE_AUTH_DOMAIN | 웹 앱 설정의 authDomain |
| FIREBASE_PROJECT_ID | 웹 앱 설정의 projectId |
| FIREBASE_STORAGE_BUCKET | 웹 앱 설정의 storageBucket |
| FIREBASE_MESSAGING_SENDER_ID | 웹 앱 설정의 messagingSenderId |
| FIREBASE_APP_ID | 웹 앱 설정의 appId |
| FIREBASE_SERVICE_ACCOUNT_JSON | 서비스 계정 JSON 전체 |
| SESSION_COOKIE_SECURE | true |

Firestore 규칙을 직접 설정할 때는 `firestore.rules.example`을 기준으로 사용자 `uid`별 접근만 허용하세요.

### 2-5. 접속 확인
- 상단에 `https://place-tracker-xxxx.onrender.com` 형태의 URL 생성
- 클릭해서 대시보드가 뜨면 배포 성공! 🎉

---

## 3단계: 사용 방법

### 플레이스 ID 찾기
1. [네이버 지도](https://map.naver.com) 접속
2. 가게 검색 후 클릭
3. URL 확인:
   ```
   https://map.naver.com/v5/entry/place/123456789
                                              ↑ 이 숫자가 플레이스 ID
   ```

### 순위 등록 및 확인
1. 대시보드 접속
2. **플레이스 등록**: 상호명 + ID 입력 후 추가
3. **키워드 등록**: `진주 맛집`, `경상대 맛집` 등 입력
4. **지금 순위 체크하기** 버튼 클릭
5. 이후 매일 오전 9시(한국 시간) 자동 체크

---

## ⚠️ 알아두실 점

- **슬립 방지**: 앱 내부에서 14분마다 자동 핑을 보내 슬립을 방지해요
- **크롤링 성공률**: 네이버 차단 정책에 따라 탐지 실패 시 `100위 밖`으로 기록됩니다
- **Disk 기능**: Render 무료 플랜 Disk는 베타 제공 중 (변경될 수 있음)

---

## 📁 파일 구조

```
place-tracker/
├── main.py              ← 웹서버 + 스케줄러
├── crawler.py           ← 네이버 플레이스 크롤러
├── database.py          ← 데이터 저장/조회
├── requirements.txt     ← Python 패키지 목록
├── Dockerfile           ← 서버 환경 설정
├── render.yaml          ← Render 배포 설정
└── templates/
    └── dashboard.html   ← 대시보드 화면
```

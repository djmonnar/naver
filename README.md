# 네이버 순위 기록기

GitHub Pages에서 Firebase Auth/Firestore를 사용하고, Render 서버가 네이버 순위 크롤링을 실행하는 대시보드입니다.

## 접속

- https://djmonnar.github.io/naver/

## Firebase 설정

1. Firebase Authentication에서 Google 로그인을 활성화합니다.
2. Authentication 승인 도메인에 `djmonnar.github.io`를 추가합니다.
3. Firestore Database를 생성합니다.
4. `firestore.rules` 내용을 Firestore Rules에 배포합니다.

## Render 실행 서버

Render는 크롤링만 실행하고 데이터는 Firestore에 저장합니다.

Render Environment에 아래 값을 추가해야 합니다.

- `FIREBASE_SERVICE_ACCOUNT_JSON`: Firebase 서비스 계정 JSON 전체
- `DATA_BACKEND`: `firestore`
- `FIREBASE_AUTH_ENABLED`: `true`

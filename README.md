# 네이버 순위 기록기

GitHub Pages에서 Firebase Auth/Firestore를 사용하는 정적 대시보드입니다.

## 접속

- https://djmonnar.github.io/naver/

## Firebase 설정

1. Firebase Authentication에서 Google 로그인을 활성화합니다.
2. Authentication 승인 도메인에 `djmonnar.github.io`를 추가합니다.
3. Firestore Database를 생성합니다.
4. `firestore.rules` 내용을 Firestore Rules에 배포합니다.

## 자동 크롤링

GitHub Actions의 `Daily Naver rank check` 워크플로가 매일 오전 9시(KST)에 실행됩니다.

크롤러가 Firestore에 저장하려면 GitHub repository secret에 아래 값을 추가해야 합니다.

- `FIREBASE_SERVICE_ACCOUNT_JSON`: Firebase 서비스 계정 JSON 전체

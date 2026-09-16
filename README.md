# 공포·탐욕 CHECK (FearGreed-Korea)

한국 주식시장 심리 종합지수(공포·탐욕)와 시장 폭(breadth) 대시보드.
GitHub Pages + GitHub Actions로 매 거래일 자동 갱신됩니다.

- 공개 주소: https://bluelagoon1222.github.io/FearGreed-Korea/
- 갱신 시각: 평일 18:20 KST(당일 종가), 다음날 07:20 KST(보정)
- 데이터: 네이버 금융(지수·종목·ETF 일별 시세, 시가총액 목록, 투자자별 매매동향), 금융투자협회(신용융자, 가용 시)
- 인증키 불필요

## 구성 지표 (8종, 0~100점, 종합 = 단순 평균)

| 지표 | 원자료 | 방향 |
|---|---|---|
| 시장 모멘텀 | KOSPI ÷ 125일 이동평균 − 1 | 높을수록 탐욕 |
| 주가 강도 | 유니버스 52주 신고가 비율 − 신저가 비율 (5일 평균) | 높을수록 탐욕 |
| 시장 폭 | 유니버스 상승 비율 − 하락 비율 (10일 평균) | 높을수록 탐욕 |
| 변동성 (공포지수 대용) | KOSPI 20일 실현변동성(연율) | 높을수록 공포 (반전) |
| 안전자산 수요 | KOSPI 20일 수익률 − KODEX 국고채3년 20일 수익률 | 높을수록 탐욕 |
| 외국인 수급 | KOSPI 외국인 순매수 20일 누적 | 높을수록 탐욕 |
| 레버리지 선호 | 레버리지 ETF 거래대금 ÷ (레버리지+인버스), 5일 | 높을수록 탐욕 |
| 신용융자 (실험) | 신용거래융자 잔고 20일 변화율 | 높을수록 탐욕 |

점수는 각 지표 값이 직전 250거래일 분포에서 차지하는 백분위입니다.
구간: 0~25 극단적 공포 / 25~45 공포 / 45~55 중립 / 55~75 탐욕 / 75~100 극단적 탐욕.

유니버스: KOSPI200 구성종목(네이버 편입종목 페이지, 실패 시 KOSPI 시총 상위 200) + KOSDAQ 시총 상위 150.
우선주(종목코드 끝자리 0이 아닌 것)·ETF·ETN·스팩·리츠는 제외합니다.

## 파일 구조

```
index.html                 대시보드 (data/latest.json을 읽어 그림)
scripts/collect.py         수집·계산 스크립트
scripts/requirements.txt   파이썬 패키지
data/latest.json           최신 스냅샷 + 250일 시계열 (Actions가 생성)
data/history.json          일별 누적 기록 (전종목 상승·하락 종목수 포함)
.github/workflows/update.yml   자동 실행 예약
```

## 설치 순서 (GitHub 웹에서)

1. 새 저장소 `FearGreed-Korea` 생성 (Public).
2. zip 안의 파일을 드래그 업로드 (`.github`, `.nojekyll`처럼 점(.)으로 시작하는 파일은 드래그에서 빠집니다).
3. 워크플로 파일은 아래 링크로 직접 생성:
   `https://github.com/bluelagoon1222/FearGreed-Korea/new/main?filename=.github/workflows/update.yml`
   → `.github/workflows/update.yml` 내용을 붙여 넣고 Commit.
4. Settings → Pages → Branch `main` / `(root)` → Save.
5. Actions → "Update Fear & Greed data" → Run workflow (첫 실행 약 3~5분).
6. 실행이 끝나면 `data/latest.json`이 생기고 공개 주소에서 화면이 채워집니다.

## 로컬 시험

```
python scripts/collect.py --demo   # 네트워크 없이 모의 데이터로 data/ 생성
python scripts/collect.py          # 실제 수집
```

## 알려진 한계

- VKOSPI는 무료 공개 경로가 없어 실현변동성으로 대체. KRX Open API(openapi.krx.co.kr) 승인 후 교체 예정.
- 오늘의 유니버스 구성으로 과거를 계산하므로 생존편향이 있음.
- 장중 수동 실행 시 당일 값은 장중가.
- 금융투자협회 신용융자 조회가 막히는 날은 해당 지표만 자동 제외되고 나머지 7개로 종합지수 산출.

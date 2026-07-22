# Kelly Allocation Lab

Kelly Allocation Lab은 과거 수익률과 사용자가 입력한 기대값을 바탕으로 성과지표, Kelly 비중, 레버리지별 기대 로그성장, 다자산 배분, 리밸런싱 효과를 같은 정의로 비교하는 공개 연구 도구입니다.

이 프로젝트는 투자 추천 서비스가 아닙니다. 과거 데이터에서 추정한 Kelly 값은 `선택기간 일간 재조정 기준 in-sample` 결과이며 미래의 적정 비중으로 표시하지 않습니다.

## 현재 공개 데이터 상태

정적 카탈로그 50개 중 해외 주식·ETF·지수·환율 48개는 API 키가 필요 없는 연구용 수집 경로로 갱신합니다. Yahoo Chart가 미국 주식·ETF의 배당·분할 조정값과 지수의 종가를 제공하고, FinanceDataReader는 같은 Yahoo 경로의 어댑터 대체 수단으로만 사용합니다. Stooq와 FRED는 독립 가격·환율 확인 또는 대체 경로이고, Finviz 값은 최근 구간 교차검증에만 쓰며 공개 파일에 복제하지 않습니다.

한국 주식 2개는 KRX 공식 Open API만 사용합니다. `KRX_API_KEY`와 명시적 `KRX_PUBLIC_DISPLAY_APPROVED=true` 중 하나라도 없으면 이 두 종목만 사유 코드와 함께 `unavailable`로 남고, 나머지 무료 소스 갱신은 계속됩니다. 현재 관측 수, 출처, 수익률 기준, 교차검증 결과는 각 자산 JSON과 화면에서 확인할 수 있습니다. 직접 가정 모드는 공급자 없이 브라우저에서 완전히 계산됩니다.

## 계산 범위

- 누적수익률, 연환산 산술평균, CAGR, 변동성, MDD, Sharpe, Sortino, 선택기간 Calmar-style
- 단일자산 GBM Kelly: `f* = e / σ²`
- 선택기간 일간 수익을 직접 최적화하는 exact in-sample Kelly
- Quarter / Half / Full Kelly와 절대 1배·2배 비교
- 다자산 이론값 `Σ⁻¹e`와 롱 전용(long-only)·총 노출 3배 상한 적용값
- 없음·일·주·월·분기·연 리밸런싱, 편도비용, 회전율, 총/비용/순 효과
- 해외자산 원통화·KRW 환산과 과거 FX 무선행 결합
- 합성 고정 2배 경로와 실제 일간목표 레버리지 ETF 경로의 분리

## 로컬 실행

Python 3.12와 `uv`, Node.js가 필요합니다.

```bash
uv sync
npm ci
npm run build
npm run serve
```

브라우저에서 `http://127.0.0.1:8765`를 엽니다.

계산 CLI 예시:

```bash
uv run kelly-lab assumptions --excess-return 0.06 --volatility 0.20 --risk-free 0.02
uv run kelly-lab analyze data/assets/etf-spy.json --risk-free 0 --start 2021-01-01 --end 2025-12-31
uv run kelly-lab analyze data/assets/etf-spy.json --currency krw --fx data/assets/fx-usd-krw.json
uv run kelly-lab portfolio-history data/assets/etf-spy.json data/assets/etf-qqq.json --fx data/assets/fx-usd-krw.json --risk-free 0.02
uv run kelly-lab rebalance rebalance-input.json --frequency monthly --cost-bps 10 --risk-free 0.02 --borrowing-spread 0.01
```

`rebalance-input.json`은 `dates`, `returnsMatrix`, `targetWeights`를 포함합니다. 정상 계산과 실행 중 계약 오류는 표준 출력에 JSON으로 기록되며, 사용할 수 없는 데이터나 잘못된 입력은 `status=unavailable`과 기계 판독 가능한 `reason`으로 종료됩니다. 명령 자체의 필수 인수 누락이나 알 수 없는 옵션은 `argparse` 사용법 오류로 처리됩니다.

정적 시장 데이터 갱신 예시:

```bash
uv run python -m kelly_lab.refresh --catalog config/catalog.json
uv run python -m kelly_lab.refresh --catalog config/catalog.json --backfill --start 2021-01-01
uv run python -m kelly_lab.verify
```

일반 갱신은 검증된 기존 이력을 보존하면서 새 관측치를 붙입니다. 교차검증 통계의 `windowStart`·`windowEnd`는 해당 갱신에서 실제로 비교한 구간이며, 증분 갱신에서는 전체 저장 이력보다 짧을 수 있습니다. 공급자의 과거 조정값이 바뀌었거나 시작일을 다시 잡아야 할 때만 명시적으로 `--backfill`을 사용합니다.
특정 종목만 다시 만들 때는 `--asset-id stock-aapl`을 붙이며 여러 종목은 이 옵션을 반복합니다.

## 검증

```bash
make test
make verify
```

Python과 브라우저 계산 엔진은 같은 골든 픽스처를 검증합니다. 주요 기준값은 이항 Kelly 20%, `e=6%`, `σ=20%`, `r=2%`일 때 Full Kelly 1.5배, 최대 로그성장 6.5%, 절대 2배 로그성장 6.0%, Half Kelly 초과성장 75%, MDD 25%입니다.

## 프로젝트 구조

```text
src/kelly_lab/       Python 기준 계산·검증·정적 빌드·CLI
site/                GitHub Pages UI와 브라우저 계산 엔진
data/                정규화된 공개 정적 계약
schemas/             JSON Schema 계약
worker/              라이선스 확인형 Cloudflare Worker
tests/python/        수학·경계·데이터 테스트
tests/js/            브라우저 엔진·UI 상태 테스트
docs/                방법론·공급자·운영 문서
```

## 공개 엔드포인트

- Pages: `https://sonchanggi.github.io/kelly/`
- Summary: `https://sonchanggi.github.io/kelly/data/summary.json`
- Worker: `/v1/search`, `/v1/history`, `/v1/fx`, `/v1/health`

Pages의 정적 데이터 갱신은 평일 예약 실행과 수동 실행을 모두 지원합니다. 무료 해외 소스에는 secret이 필요하지 않으며 KRX 키는 선택 사항입니다. KRX 공개 게시에는 키와 외부표시 확인 변수가 모두 필요합니다. Cloudflare Worker와 Twelve Data 즉시조회 경로는 이 정적 수집과 분리된 선택 기능으로, 서버 측 secret과 별도 권한 확인 없이는 `unavailable`을 유지합니다.

## 계산 원칙

단일자산 GBM에서 현금금리 `r`, 기대초과수익률 `e`, 변동성 `σ`, 위험자산 비중 `f`의 연속복리 기대 로그성장은 다음과 같습니다.

```text
g(f) = r + f·e - ½f²σ²
```

`f > 1`일 때 차입 스프레드가 있으면 `(f-1)·spread`를 추가로 차감합니다. 화면의 기대 연 복리성장률은 `exp(g)-1`로 변환합니다. 이 연속시간 모형과 실제 일간 수익 경로는 서로 다른 결과로 명시합니다.

성과지표와 데이터 기준의 상세 정의는 [docs/methodology.md](docs/methodology.md), 공개 계약은 [docs/data-contract.md](docs/data-contract.md), 공급자 활성화 조건은 [docs/provider-policy.md](docs/provider-policy.md)를 참고하십시오.

## 근거 문헌

- [Kelly, A New Interpretation of Information Rate (1956)](https://onlinelibrary.wiley.com/doi/10.1002/j.1538-7305.1956.tb03809.x)
- [Merton, Lifetime Portfolio Selection under Uncertainty](https://www.sfu.ca/~kkasa/Merton_69.pdf)
- [Sharpe, The Sharpe Ratio](https://web.stanford.edu/~wfsharpe/art/sr/SR.htm)
- [FINRA, daily-reset leveraged ETF guidance](https://www.finra.org/rules-guidance/notices/09-31)
- [Vanguard, rational rebalancing](https://marketing.vanguard.com/content/dam/corp/research/pdf/rational_rebalancing_analytical_approach_to_multiasset_portfolio_rebalancing.pdf)

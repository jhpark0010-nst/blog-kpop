# blog-kpop

K-pop 한글 뉴스를 영어로 번역해 자동 발행하는 WordPress 블로그 자동화.

## 아키텍처

```
[cron-job.org] → [GitHub Actions workflow_dispatch]
  → [Python + anthropic SDK] → [git commit + push]
  → [publish-to-wordpress.yml 자동 발동] → [WP REST API]
```

## Tier 구성

| Tier | 역할 | 빈도 (KST) | 스크립트 |
|------|------|------------|---------|
| 1 | RSS 수집 + K-pop 필터 | 매 2시간 :55 | `scripts/collect_and_filter.py` |
| 2 | Evaluator (Anthropic API) | 매 2시간 :00 | `scripts/evaluate.py` |
| 3 | Writer (Anthropic API, 번역) | 매시간 (08~22) | `scripts/write.py` |
| 4 | Publish to WordPress | draft push 자동 | `scripts/publish_drafts.py` |
| 5 | Reviewer (Anthropic API) | 03:00 | `scripts/review.py` |
| 6 | Review Apply | review-actions.json push 자동 | `scripts/apply_review_actions.py` |

## GitHub Secrets

| Key | 용도 |
|-----|------|
| `ANTHROPIC_API_KEY` | Anthropic API 인증 |
| `CLAUDE_MODEL` | (선택) 모델 오버라이드. 기본 `claude-sonnet-4-6` |
| `WP_URL` | K-pop WordPress 사이트 URL |
| `WP_USERNAME` | WP 관리자 |
| `WP_APP_PASSWORD` | WP Application Password |
| `SLACK_WEBHOOK_URL` | `#blog-kpop` 알림 |

## 로컬 테스트

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-api03-...
python scripts/evaluate.py  # pending.json 있어야 의미 있음
```

## RSS 소스

- 연합뉴스 엔터: `https://www.yna.co.kr/rss/entertainment.xml`
- 스포츠동아: `https://sports.donga.com/rss`

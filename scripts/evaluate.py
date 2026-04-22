#!/usr/bin/env python3
"""Tier 2 Evaluator (GitHub Actions 진입점).

pending.json의 unreviewed 항목을 Anthropic API로 점수 매김 →
70점 이상 candidates.json 승격, 70점 미만 pending에서 제거.
"""
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (
    CANDIDATES_PATH,
    EVAL_BATCH_SIZE,
    PENDING_MAX_AGE_DAYS,
    PENDING_PATH,
    SCORE_THRESHOLD,
)
from src.anthropic_helper import call_json, estimate_cost_usd

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """당신은 K-pop 뉴스 영어 번역 블로그의 편집자입니다.
한국어 K-pop 뉴스를 받아 영어권 독자에게 블로그 포스트로 제공할 가치가 있는지 0~100점으로 평가합니다.

평가 기준:
1. 국제적 관심도 (40점): 글로벌 K-pop 팬이 궁금해할 내용인가? (해외 차트, 투어, 컴백 등)
2. 구체성 (30점): 숫자/날짜/고유명사/순위 등 팩트가 구체적인가?
3. 정보 희소성 (20점): 다른 매체가 이미 많이 다루지 않은 내용인가?
4. 시의성 (10점): 최근 발생한 이벤트/뉴스인가?

감점 대상:
- 단순 루머/가십/스캔들 (해외 독자 관심 낮음)
- 연예인 개인 사생활
- 광고성 보도
- 팩트 없이 추측만 있는 기사

반드시 **순수 JSON**으로만 응답하세요. 마크다운 코드블록 사용 금지.

형식:
{"scores": [{"guid": "...", "score": 정수}, ...]}
"""


def load_json(path: Path, default=None):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default if default is not None else []


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def slack_notify(text: str) -> None:
    if not SLACK_WEBHOOK:
        return
    try:
        requests.post(SLACK_WEBHOOK, json={"text": text}, timeout=10)
    except Exception as e:
        logger.warning(f"Slack 알림 실패: {e}")


def build_eval_input(items: list[dict]) -> str:
    """평가 대상을 Claude가 보기 좋게 정리."""
    entries = []
    for item in items:
        entries.append({
            "guid": item["guid"],
            "title": item.get("title", ""),
            "summary": item.get("summary", "")[:300],
            "content_preview": item.get("content", "")[:500],
        })
    return (
        f"다음 K-pop 뉴스 {len(items)}건을 평가하고 각 guid의 score(0~100) JSON을 리턴하세요:\n\n"
        + json.dumps(entries, ensure_ascii=False, indent=2)
    )


def main() -> int:
    logger.info("=" * 50)
    logger.info(f"Evaluator 시작: {datetime.now().isoformat()}")

    pending_data = load_json(PENDING_PATH, {"last_updated": "", "items": []})
    candidates_data = load_json(CANDIDATES_PATH, {"last_updated": "", "items": []})
    pending_items = pending_data.get("items", [])
    cand_items = candidates_data.get("items", [])

    cutoff = (datetime.now() - timedelta(days=PENDING_MAX_AGE_DAYS)).isoformat()
    unreviewed = [
        i for i in pending_items
        if i.get("status") == "unreviewed" and i.get("collected_at", "") >= cutoff
    ]
    to_eval = sorted(unreviewed, key=lambda x: x.get("collected_at", ""), reverse=True)[:EVAL_BATCH_SIZE]

    logger.info(f"pending {len(pending_items)}건 중 unreviewed {len(unreviewed)}건, 평가 대상 {len(to_eval)}건")

    if not to_eval:
        slack_notify("📝 *Evaluator*: 평가할 항목 없음")
        return 0

    try:
        result, meta = call_json(
            system=SYSTEM_PROMPT,
            user=build_eval_input(to_eval),
            max_tokens=2000,
            temperature=0.2,
        )
    except Exception as e:
        logger.error(f"API 호출 실패: {e}")
        slack_notify(f"❌ *Evaluator 실패*\nAPI 에러: {str(e)[:300]}")
        return 1

    scores = result.get("scores", [])
    if not scores:
        logger.error("Claude 응답에 scores 없음")
        slack_notify(f"❌ *Evaluator 실패*: 응답에 scores 없음\n{json.dumps(result)[:300]}")
        return 1

    # 점수 맵
    score_map = {s["guid"]: s.get("score", 0) for s in scores if "guid" in s}

    passed_guids = {g for g, sc in score_map.items() if sc >= SCORE_THRESHOLD}
    rejected_guids = {g for g, sc in score_map.items() if sc < SCORE_THRESHOLD}

    # pending 갱신: 평가된 것은 전부 제거, 그 외는 유지 + 만료 정리
    new_pending = [
        i for i in pending_items
        if i.get("collected_at", "") >= cutoff
        and i["guid"] not in passed_guids
        and i["guid"] not in rejected_guids
    ]

    # candidates로 승격
    passed_items = [i for i in pending_items if i["guid"] in passed_guids]
    for it in passed_items:
        it["status"] = "passed"
        it["score"] = score_map.get(it["guid"], 0)
        it["evaluated_at"] = datetime.now().isoformat()

    cand_items.extend(passed_items)

    save_json(PENDING_PATH, {
        "last_updated": datetime.now().isoformat(),
        "items": new_pending,
    })
    save_json(CANDIDATES_PATH, {
        "last_updated": datetime.now().isoformat(),
        "items": cand_items,
    })

    cost = estimate_cost_usd(meta["input_tokens"], meta["output_tokens"], meta["model"])

    summary = (
        f"🔍 *Evaluator 완료*\n"
        f"평가 대상: {len(to_eval)}건 → 승격 {len(passed_guids)}건 / 탈락 {len(rejected_guids)}건\n"
        f"pending: {len(new_pending)}건 / candidates: {len(cand_items)}건\n"
        f"모델: {meta['model']} | 토큰 {meta['input_tokens']}in/{meta['output_tokens']}out | "
        f"약 ${cost:.4f}"
    )
    logger.info(summary)
    slack_notify(summary)

    # 승격된 상위 3건 로그
    top3 = sorted(passed_items, key=lambda x: x.get("score", 0), reverse=True)[:3]
    for it in top3:
        logger.info(f"  ✅ [{it['score']}점] {it.get('title', '')[:50]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

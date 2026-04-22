#!/usr/bin/env python3
"""GitHub Actions 진입점 (Tier 1).

RSS 수집 → K-pop 관련성 필터 → 제목 유사도 dedup → pending.json 갱신.
"""
import json
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (
    CANDIDATES_PATH,
    DEDUP_DAYS,
    PENDING_MAX_AGE_DAYS,
    PENDING_PATH,
    PROCESSED_PATH,
)
from src.content_filter import filter_items
from src.rss_collector import collect_all_feeds

DRAFTS_PUBLISHED_DIR = PROJECT_ROOT / "data" / "drafts" / "published"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def load_json(path: Path, default=None):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default if default is not None else []


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_recent_titles(pending_items: list) -> list[str]:
    """최근 N일 내 pending + candidates + published HTML 제목 목록 (중복 체크용)."""
    cutoff = (datetime.now() - timedelta(days=DEDUP_DAYS)).isoformat()
    titles = []

    # pending 중 최근 것
    for item in pending_items:
        if item.get("collected_at", "") >= cutoff:
            titles.append(item.get("title", ""))

    # candidates
    if CANDIDATES_PATH.exists():
        try:
            with open(CANDIDATES_PATH, encoding="utf-8") as f:
                cand = json.load(f)
            for item in cand.get("items", []):
                t = item.get("title", "")
                if t:
                    titles.append(t)
        except Exception as e:
            logger.warning(f"candidates.json 로드 실패: {e}")

    # drafts/published/ HTML 주석의 OriginalTitle (있으면) + Title
    if DRAFTS_PUBLISHED_DIR.exists():
        for html_path in DRAFTS_PUBLISHED_DIR.glob("*.html"):
            try:
                text = html_path.read_text(encoding="utf-8")[:3000]
                for key in ("OriginalTitle", "Title"):
                    m = re.search(rf"{key}:\s*(.+)", text)
                    if m:
                        titles.append(m.group(1).strip())
            except Exception as e:
                logger.warning(f"Title 추출 실패 ({html_path.name}): {e}")

    return titles


def cleanup_old_pending(items: list) -> list:
    """N일 이상 지난 pending 항목 제거."""
    cutoff = (datetime.now() - timedelta(days=PENDING_MAX_AGE_DAYS)).isoformat()
    before = len(items)
    items = [i for i in items if i.get("collected_at", "") >= cutoff]
    removed = before - len(items)
    if removed:
        logger.info(f"[정리] {PENDING_MAX_AGE_DAYS}일+ 지난 pending {removed}건 제거")
    return items


def main():
    logger.info("=" * 50)
    logger.info(f"RSS 수집 시작: {datetime.now().isoformat()}")

    processed_guids = set(load_json(PROCESSED_PATH, []))
    pending_data = load_json(PENDING_PATH, {"last_updated": "", "items": []})
    pending_items = pending_data.get("items", [])

    logger.info(f"기존 상태: 처리됨 {len(processed_guids)}건, 대기 {len(pending_items)}건")

    new_items = collect_all_feeds(processed_guids)
    logger.info(f"신규 수집: {len(new_items)}건")

    if not new_items:
        logger.info("신규 항목 없음. 종료.")
        return

    recent_titles = get_recent_titles(pending_items)
    passed_items = filter_items(new_items, recent_titles)

    # 모든 신규 GUID를 processed에 추가 (필터 통과 여부 무관)
    for item in new_items:
        processed_guids.add(item["guid"])

    pending_items.extend(passed_items)
    pending_items = cleanup_old_pending(pending_items)

    save_json(PROCESSED_PATH, sorted(processed_guids))
    save_json(PENDING_PATH, {
        "last_updated": datetime.now().isoformat(),
        "items": pending_items,
    })

    logger.info("=" * 50)
    logger.info(
        f"수집 완료: 신규 {len(new_items)}건, 필터 통과 {len(passed_items)}건, "
        f"대기 {len(pending_items)}건, 누적 {len(processed_guids)}건"
    )
    print(
        f"::notice::수집 {len(new_items)}건, 통과 {len(passed_items)}건, "
        f"대기 {len(pending_items)}건"
    )


if __name__ == "__main__":
    main()

"""K-pop 관련성 + 중복 필터.

- K-pop 키워드 OR 엔티티 매칭으로 K-pop 관련 판별
- 제목 bigram 기반 Jaccard similarity dedup (한국어 제목에 강건)
"""
import logging
import re

from config.settings import (
    KPOP_ENTITIES,
    KPOP_KEYWORDS,
    TITLE_SIMILARITY_THRESHOLD,
)

logger = logging.getLogger(__name__)


def _title_bigrams(title: str) -> set:
    """제목에서 한글/영숫자만 남기고 bigram 집합 반환."""
    cleaned = re.sub(r'[^\w가-힣]', '', title)
    return {cleaned[i:i+2] for i in range(len(cleaned) - 1)}


def is_kpop_relevant(title: str, summary: str, content: str) -> tuple[bool, list[str]]:
    """K-pop 관련성 판별.

    엔티티(아티스트명/소속사명) 1개 이상 OR 일반 K-pop 키워드 1개 이상 매치.
    매칭된 문자열 리스트도 반환 (디버깅용).
    """
    text = f"{title} {summary} {content}"
    matched = []

    # 엔티티는 대소문자 구분 (BTS vs bts 등)
    for entity in KPOP_ENTITIES:
        if entity in text:
            matched.append(entity)

    # 일반 키워드는 lowercase 매칭 (대소문자 섞여 있을 수 있음)
    text_lower = text.lower()
    for kw in KPOP_KEYWORDS:
        if kw.lower() in text_lower:
            matched.append(kw)

    return (len(matched) > 0, matched)


def is_similar(title: str, existing_titles: list[str], min_common_bigrams: int = 6) -> bool:
    """Bigram 공통 개수 기반 유사도. min_common_bigrams 이상이면 유사."""
    tbg = _title_bigrams(title)
    if len(tbg) < min_common_bigrams:
        return False
    for existing in existing_titles:
        ebg = _title_bigrams(existing)
        if len(tbg & ebg) >= min_common_bigrams:
            return True
    return False


def jaccard_similar(title: str, existing_titles: list[str], threshold: float = TITLE_SIMILARITY_THRESHOLD) -> tuple[bool, str | None]:
    """Bigram Jaccard similarity 기반 유사도.

    임계값 이상이면 True + 매칭된 기존 제목 반환.
    2026-04-17 K-pop RSS 실측에서 0.35 권장.
    """
    tbg = _title_bigrams(title)
    if len(tbg) < 3:
        return (False, None)
    for existing in existing_titles:
        ebg = _title_bigrams(existing)
        if not ebg:
            continue
        union = len(tbg | ebg)
        if union == 0:
            continue
        jaccard = len(tbg & ebg) / union
        if jaccard >= threshold:
            return (True, existing)
    return (False, None)


def filter_items(
    items: list[dict],
    recent_titles: list[str] | None = None,
) -> list[dict]:
    """K-pop 관련성 + 중복 필터링.

    recent_titles: 최근 수집/발행된 제목 목록 (중복 체크용)
    Returns: 통과한 항목 리스트 (filter_reason 필드 추가됨)
    """
    if recent_titles is None:
        recent_titles = []

    passed = []
    rejected_count = 0

    for item in items:
        title = item.get("title", "")
        summary = item.get("summary", "")
        content = item.get("content", "")

        # 1) 제목 최소 길이
        if len(title) < 5:
            rejected_count += 1
            continue

        # 2) K-pop 관련성
        is_kpop, matched_kws = is_kpop_relevant(title, summary, content)
        if not is_kpop:
            logger.debug(f"  [제외] {title[:50]} — K-pop 무관")
            rejected_count += 1
            continue

        # 3) Jaccard 유사도 중복
        is_dup, dup_title = jaccard_similar(title, recent_titles)
        if is_dup:
            logger.debug(f"  [제외] {title[:50]} — 유사 제목: {dup_title[:40]}")
            rejected_count += 1
            continue

        item["filter_reason"] = f"K-pop 키워드: {', '.join(matched_kws[:5])}"
        passed.append(item)
        logger.info(f"  [통과] {title[:50]} — {item['filter_reason']}")

    logger.info(f"[필터링] 전체 {len(items)}건 → 통과 {len(passed)}건, 제외 {rejected_count}건")
    return passed

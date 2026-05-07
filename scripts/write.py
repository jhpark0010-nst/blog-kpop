#!/usr/bin/env python3
"""Tier 3 Writer (GitHub Actions 진입점).

번역 중심 K-pop 블로그. candidates 상위 N건을 순차 처리:
  1건 선택 → API 번역 → draft 저장 → WP 발행 (inline) → candidates 제거
  → git commit + push → Slack 알림 → 다음 1건

한 건이 완전히 끝나야 다음 건 시작. 중간 실패해도 이미 발행된 건은 보존.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (
    CANDIDATES_PATH,
    WRITER_ARTICLES_PER_RUN,
    WRITER_TARGET_WORD_COUNT_MAX,
    WRITER_TARGET_WORD_COUNT_MIN,
)
from scripts.publish_drafts import publish_single_draft
from src.anthropic_helper import call_json, estimate_cost_usd
from src.content_filter import is_similar, jaccard_similar

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")
DRAFTS_DIR = PROJECT_ROOT / "data" / "drafts"
PUBLISHED_DIR = DRAFTS_DIR / "published"
DRAFTS_DIR.mkdir(parents=True, exist_ok=True)

# 발행 직전 중복 체크 윈도우 (일).
WRITER_DEDUP_DAYS = 7

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


STYLE_GUIDE_PATH = PROJECT_ROOT / "config" / "style_guide.md"


def load_style_guide() -> str:
    """config/style_guide.md 내용 반환. 없으면 빈 문자열."""
    if STYLE_GUIDE_PATH.exists():
        return STYLE_GUIDE_PATH.read_text(encoding="utf-8")
    return ""


SYSTEM_PROMPT_BASE = f"""You translate Korean K-pop news into SEO-optimized English blog posts for global K-pop fans.

## Goal

**Pure translation, minimal editorial intervention**. Your job is to translate Korean → English while preserving the source article's structure, paragraph order, and content as-is. Do NOT restructure, merge, reframe, or editorialize.

### Translation faithfulness rules (critical)

- **Paragraph order**: `body_paragraphs` should follow the source article's paragraph order. 1 source paragraph ≈ 1 target paragraph where possible.
- **Quotes**: translate each quote as a separate unit. If the source has two separate quotes from the same speaker, keep them as two separate sentences/paragraphs — do NOT merge into one continuous speech.
- **Lists**: when the source lists items (e.g., group names, song titles, members), translate the full list in the same order, no additions, no omissions, no substitutions.
- **Framing**: do not add interpretive/transitional language that's not in the source ("In a shocking move...", "Industry observers note...").
- **No invented context**: only state what the source states. If the source doesn't give background, don't add it.

Exception: intro/lead sentence may be slightly adapted for SEO (include artist name + fact), but the rest of the body stays faithful to the source's order and content.

## Output

Reply with **raw JSON only**. The first character of your response must be `{{` and the last character must be `}}`. No markdown fence, no prose wrapper, no thinking-out-loud preface (e.g., "I need to work with..."). Schema:

```
{{
  "title_en": "40-65 chars; main keyword (artist/song/event) in first 30 chars",
  "slug": "lowercase-hyphens; 3-6 meaningful words",
  "meta_desc": "120-155 chars; includes main keyword + specific fact (number/date)",
  "tags": ["3-5 tags: artists, events, key concepts"],
  "featured_alt": "Short alt text including artist name",
  "body_paragraphs": ["paragraph 1", "paragraph 2", "..."]
}}
```

## Body rules

- **{WRITER_TARGET_WORD_COUNT_MIN}-{WRITER_TARGET_WORD_COUNT_MAX} total words** across all paragraphs.
- 3-6 paragraphs, each 50-90 words.
- **First paragraph MUST include: artist/group name + specific fact (number, date, chart position, venue)**.
- Translate the source article faithfully. Preserve original numbers, dates, quotes, chart positions.
- K-pop proper nouns in standard English: BTS, BLACKPINK, NewJeans, Jennie, Rosé, Jungkook, RM, etc.
- For uniquely Korean concepts (e.g., "컴백", "팬미팅"), use standard English ("comeback", "fan meeting") or add brief gloss on first mention.
- **Never invent facts** not present in the source.

## SEO essentials

- Main keyword (artist/song/event) appears naturally in: `title_en` (front-loaded), `meta_desc`, `slug`, first paragraph.
- Readability trumps keyword stuffing. Write as native English prose.
- `tags`: 3-5 relevant tags — artist names, song titles, events, major keywords.
- `featured_alt`: concise, includes artist name and situation (e.g., "BTS performing at Tokyo Dome 2026 Arirang tour").

## Tone

Fan-friendly informative blog voice. Not tabloid (no sensationalism), not wire copy (not dry).

## ⚠️ JSON string escaping (CRITICAL — this has broken past runs)

Your entire response is a single JSON object parsed by `json.loads()` in Python. Song titles, album names, and quoted phrases inside `title_en`, `meta_desc`, or `body_paragraphs` items must **NEVER use raw ASCII double quotes**, because they will terminate the JSON string and break parsing.

**Song/album titles → use SINGLE quotes or italics wording. Not double quotes.**

WRONG (breaks JSON — do not emit this):
```
"body_paragraphs": ["LE SSERAFIM dropped their lead single "Celebration" today..."]
```

CORRECT (single quotes):
```
"body_paragraphs": ["LE SSERAFIM dropped their lead single 'Celebration' today..."]
```

ALSO CORRECT (italics wording, no quotes at all):
```
"body_paragraphs": ["LE SSERAFIM dropped Celebration, the lead single from their second album, today..."]
```

Same rule for every string field. When in doubt, use single quotes `'…'`. Do not emit a literal ASCII `"` inside any string value."""


def build_system_prompt() -> str:
    """기본 프롬프트 + 프로젝트 스타일 가이드 결합."""
    guide = load_style_guide()
    if guide:
        return (
            SYSTEM_PROMPT_BASE
            + "\n\n---\n\n## PROJECT STYLE GUIDE (MANDATORY — follow strictly)\n\n"
            + guide
        )
    return SYSTEM_PROMPT_BASE


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


def pick_top_candidates(items: list[dict], n: int) -> list[dict]:
    """score 상위 N건 선택. 동점 시 collected_at 최신 우선.

    주의: batch 내 dedup 미포함. filter_against_recent_published 가 published 와만
    비교하므로, 한 cron 안에 N>1 픽하는 경우 후보들끼리 비슷하면 둘 다 통과 가능.
    이 케이스는 pick_top_candidates_with_intra_dedup 사용.
    """
    return sorted(
        items,
        key=lambda x: (x.get("score", 0), x.get("collected_at", "")),
        reverse=True,
    )[:n]


def pick_top_candidates_with_intra_dedup(
    items: list[dict],
    n: int,
    initial_recent_titles: list[str],
) -> tuple[list[dict], list[str]]:
    """score 정렬한 후보를 순회하며 (과거 발행글 + 이번 batch 내 이미 선택된 것)
    과 dedup 비교해서 통과한 것 N건까지 픽.

    NewJeans 민지 ADOR 처럼 같은 cron 안에서 비슷한 두 후보가 동시 픽되어
    중복 발행되는 케이스 차단.
    """
    sorted_items = sorted(
        items,
        key=lambda x: (x.get("score", 0), x.get("collected_at", "")),
        reverse=True,
    )
    picked: list[dict] = []
    seen_titles = list(initial_recent_titles)
    skipped_guids: list[str] = []
    for cand in sorted_items:
        if len(picked) >= n:
            break
        title = cand.get("title", "")
        is_dup_j, dup_title = jaccard_similar(title, seen_titles)
        is_dup_b = is_similar(title, seen_titles, min_common_bigrams=6)
        if is_dup_j or is_dup_b:
            method = "jaccard" if is_dup_j else "bigram6"
            target = dup_title if dup_title else "(bigram match)"
            logger.warning(
                f"  [intra-batch 스킵-{method}] {title[:55]} ↔ {target[:40]}"
            )
            skipped_guids.append(cand.get("guid", ""))
            continue
        picked.append(cand)
        seen_titles.append(title)
    return picked, skipped_guids


def _recent_published_titles(days: int = WRITER_DEDUP_DAYS) -> list[str]:
    """data/drafts/published/*.html 중 최근 N일 발행글의 제목 (한국어 OriginalTitle 우선).

    영어 Title 도 공용해 K-pop 명사(아티스트/곡명) bigram 매칭 가능.
    """
    if not PUBLISHED_DIR.exists():
        return []
    now = datetime.now()
    titles: list[str] = []
    for html_path in PUBLISHED_DIR.glob("*.html"):
        text = html_path.read_text(encoding="utf-8")[:3000]
        title = None
        original_title = None
        created_str = None
        for m in re.finditer(r"<!--(.*?)-->", text, re.DOTALL):
            for line in m.group(1).splitlines():
                if ":" not in line:
                    continue
                k, _, v = line.partition(":")
                k = k.strip().lower()
                v = v.strip()
                if k == "title" and not title:
                    title = v
                elif k == "originaltitle" and not original_title:
                    original_title = v
                elif k in ("publishedat", "created") and not created_str:
                    created_str = v
        if not created_str:
            continue
        try:
            dt = datetime.fromisoformat(created_str)
            dt_naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
        except ValueError:
            continue
        if (now - dt_naive).total_seconds() > days * 86400:
            continue
        # 한국어 원제와 영문 제목 둘 다 비교 풀에 추가 (jaccard bigram 은 둘 다 잘 동작)
        if original_title:
            titles.append(original_title)
        if title:
            titles.append(title)
    return titles


def filter_against_recent_published(candidates: list[dict]) -> tuple[list[dict], list[str]]:
    """후보 중 최근 7일 발행글과 유사한 것 제외. (남은 후보, 스킵된 guid 리스트) 반환.

    jaccard (0.35+) OR bigram 공통 6+ 둘 중 하나라도 매칭되면 중복 판정.
    jaccard 만으로는 동일 사건의 매체별 헤드라인 변형(예: 같은 I.O.I MV 티저
    뉴스를 MyDaily 와 TVReport 가 다른 헤드라인으로 보도)을 못 잡는 케이스 대응.
    """
    recent = _recent_published_titles()
    if not recent:
        return candidates, []
    kept: list[dict] = []
    skipped_guids: list[str] = []
    for cand in candidates:
        title = cand.get("title", "")
        # 1) jaccard 0.35+ 매칭
        is_dup_j, dup_title = jaccard_similar(title, recent)
        # 2) bigram 공통 6+ (단순 카운트)
        is_dup_b = is_similar(title, recent, min_common_bigrams=6)
        if is_dup_j or is_dup_b:
            method = "jaccard" if is_dup_j else "bigram6"
            target = dup_title if dup_title else "(bigram match)"
            logger.warning(
                f"  [스킵-{method}] 최근 발행과 유사: {title[:55]} ↔ {target[:40]}"
            )
            skipped_guids.append(cand.get("guid", ""))
            continue
        kept.append(cand)
    return kept, skipped_guids


def build_user_message(item: dict) -> str:
    return (
        "## Source Article (Korean)\n\n"
        f"**Source URL**: {item.get('link', '')}\n"
        f"**Original Title**: {item.get('title', '')}\n"
        f"**Published**: {item.get('published', '')}\n"
        f"**Image URL**: {item.get('thumbnail_url') or '(none)'}\n\n"
        f"**Summary**:\n{item.get('summary', '')}\n\n"
        f"**Content**:\n{item.get('content', '')}\n\n"
        "---\n\n"
        "Translate this into an English SEO blog post per the schema above. "
        "Return **only the JSON object**, no additional text."
    )


def assemble_html(article: dict, source_item: dict) -> str:
    """Claude JSON 결과 + 원본 메타 → 간결한 번역글 HTML."""
    created_at = datetime.now().isoformat()
    source_url = source_item.get("link", "")
    source_host = ""
    if source_url:
        m = re.match(r"https?://([^/]+)/", source_url)
        if m:
            source_host = f"https://{m.group(1)}/"

    thumbnail = source_item.get("thumbnail_url") or ""
    tags = ", ".join(article.get("tags", []))

    # 주석 헤더
    header = (
        "<!--\n"
        f"Title: {article['title_en']}\n"
        f"Slug: {article['slug']}\n"
        f"Meta: {article['meta_desc']}\n"
        f"Tags: {tags}\n"
        f"Score: {source_item.get('score', 0)}\n"
        f"OriginalTitle: {source_item.get('title', '')}\n"
        f"OriginalURL: {source_url}\n"
        f"ImageSourceURL: {thumbnail}\n"
        f"ImageSourceReferer: {source_host}\n"
        f"FeaturedAlt: {article.get('featured_alt', '')}\n"
        f"Created: {created_at}\n"
        "-->\n"
    )

    parts = [header]

    # Featured image은 본문에 직접 삽입하지 않음 (WP 테마가 featured_media를 상단 표시).
    # ImageSourceURL은 주석 헤더에 기록돼있고 publish_drafts.py가 featured_media로 업로드.

    # Body paragraphs — 순수 번역 문단들
    for para in article.get("body_paragraphs", []):
        if not para.strip():
            continue
        parts.append(
            f'<p style="margin-bottom:1.5em;line-height:1.8;">{para}</p>\n'
        )

    # Source credit
    if source_url:
        original_title = source_item.get("title", source_url)
        parts.append(
            f'<p style="margin-top:32px;font-size:0.9em;color:#64748B;">'
            f'Source: <a href="{source_url}" target="_blank" rel="noopener">'
            f"{original_title}</a></p>\n"
        )

    return "".join(parts)


def git_commit_push(message: str) -> bool:
    """로컬 git add/commit/push. 성공/실패 반환. 변경사항 없으면 True."""
    try:
        subprocess.run(["git", "add", "data/"], check=True, cwd=PROJECT_ROOT)
        # staged 변경사항 확인
        result = subprocess.run(
            ["git", "diff", "--staged", "--quiet"],
            cwd=PROJECT_ROOT,
        )
        if result.returncode == 0:
            logger.info("변경사항 없음 (commit 생략)")
            return True
        subprocess.run(
            ["git", "commit", "-m", message],
            check=True,
            cwd=PROJECT_ROOT,
        )
        # pull rebase + push (충돌 대비)
        subprocess.run(
            ["git", "pull", "--rebase", "origin", "main"],
            check=True,
            cwd=PROJECT_ROOT,
        )
        subprocess.run(["git", "push", "origin", "main"], check=True, cwd=PROJECT_ROOT)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"git 실패: {e}")
        return False


def process_one_candidate(candidate: dict, idx: int, total: int) -> dict:
    """단일 candidate 처리: API 번역 → draft 저장 → 발행 → candidates 제거 → commit+push.

    Returns: {"status": "success"|"skipped"|"failed", "title": ..., ...}
    """
    title_original = candidate.get("title", "(no title)")[:60]
    logger.info(f"[{idx}/{total}] 시작: [{candidate.get('score', 0)}점] {title_original}")

    # 1) API 호출
    try:
        article, api_meta = call_json(
            system=build_system_prompt(),
            user=build_user_message(candidate),
            max_tokens=2500,
            temperature=0.4,
        )
    except Exception as e:
        logger.error(f"API 실패: {e}")
        return {
            "status": "api_failed",
            "title": title_original,
            "error": str(e)[:300],
            "api_meta": None,
        }

    # 필수 필드 검증
    required = ["title_en", "slug", "meta_desc", "body_paragraphs"]
    missing = [k for k in required if not article.get(k)]
    if missing:
        logger.error(f"응답 필드 누락: {missing}")
        return {
            "status": "schema_failed",
            "title": title_original,
            "error": f"응답 필드 누락: {missing}",
            "api_meta": api_meta,
        }

    # slug 정규화
    slug = re.sub(r"[^a-z0-9-]", "", article["slug"].lower())
    if not slug:
        return {
            "status": "schema_failed",
            "title": title_original,
            "error": f"slug 정규화 실패 ({article['slug']})",
            "api_meta": api_meta,
        }
    article["slug"] = slug

    # 2) draft 저장
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"{today}-{slug}.html"
    draft_path = DRAFTS_DIR / filename
    # 동일 파일 이미 있으면 카운터 접미
    if draft_path.exists():
        i = 2
        while (DRAFTS_DIR / f"{today}-{slug}-{i}.html").exists():
            i += 1
        filename = f"{today}-{slug}-{i}.html"
        draft_path = DRAFTS_DIR / filename

    html = assemble_html(article, candidate)
    draft_path.write_text(html, encoding="utf-8")
    logger.info(f"draft 저장: {filename} ({len(html)} chars)")

    # 3) 발행 inline (publish_single_draft 호출)
    pub_result = publish_single_draft(draft_path)
    pub_status = pub_result.get("status")
    logger.info(f"publish 결과: {pub_status}")

    # 4) candidates.json에서 이 guid 제거 (발행 성공/스킵 모두 제거 — 재시도 방지)
    candidates_data = load_json(CANDIDATES_PATH, {"last_updated": "", "items": []})
    items = candidates_data.get("items", [])
    remaining = [i for i in items if i.get("guid") != candidate.get("guid")]
    save_json(CANDIDATES_PATH, {
        "last_updated": datetime.now().isoformat(),
        "items": remaining,
    })

    # 5) git commit + push (이 한 건의 모든 변경사항)
    commit_msg = {
        "success": f"publish: {article['title_en']}",
        "skipped": f"skip (image fail): {article['title_en']}",
        "failed": f"draft (publish fail): {article['title_en']}",
    }.get(pub_status, f"draft: {article['title_en']}")
    git_commit_push(commit_msg)

    return {
        "status": pub_status,
        "title_en": article["title_en"],
        "title": title_original,
        "slug": slug,
        "link": pub_result.get("link"),
        "error": pub_result.get("error") or pub_result.get("reason"),
        "api_meta": api_meta,
        "candidates_remaining": len(remaining),
    }


def main() -> int:
    logger.info("=" * 50)
    logger.info(f"Writer 시작: {datetime.now().isoformat()} (per-run 목표: {WRITER_ARTICLES_PER_RUN}편)")

    # git config 초기 설정 (GitHub Actions 러너 기준)
    try:
        subprocess.run(
            ["git", "config", "user.email", "writer@blog-kpop.local"],
            check=True, cwd=PROJECT_ROOT,
        )
        subprocess.run(
            ["git", "config", "user.name", "blog-kpop-writer"],
            check=True, cwd=PROJECT_ROOT,
        )
    except subprocess.CalledProcessError:
        pass  # 이미 설정돼있거나 로컬 테스트

    candidates_data = load_json(CANDIDATES_PATH, {"last_updated": "", "items": []})
    items = candidates_data.get("items", [])
    logger.info(f"candidates: {len(items)}건")

    if not items:
        slack_notify("⏸️ *Writer*: 작성 후보 없음")
        return 0

    # 최근 7일 발행글과 유사한 후보 제외 — Evaluator/collect 가 못 잡은
    # "candidates 에 머무는 사이 비슷한 글이 발행된" 케이스 방어.
    items_filtered, skipped_guids = filter_against_recent_published(items)

    if skipped_guids:
        skip_set = set(skipped_guids)
        new_items = [i for i in items if i.get("guid", "") not in skip_set]
        save_json(CANDIDATES_PATH, {
            "last_updated": datetime.now().isoformat(),
            "items": new_items,
        })
        logger.info(f"중복 스킵된 후보 {len(skipped_guids)}건 candidates 에서 제거")

    if not items_filtered:
        slack_notify(
            f"⏸️ *Writer*: 작성 후보 없음"
            + (f" (최근 발행 중복으로 {len(skipped_guids)}건 스킵)" if skipped_guids else "")
        )
        return 0

    # 같은 cron 안에서 비슷한 후보 둘 다 픽돼 중복 발행되는 것 방지.
    # 위 filter_against_recent_published 에서 모은 recent 와 picked 누적 비교.
    recent_for_intra = _recent_published_titles()
    selected, intra_skipped = pick_top_candidates_with_intra_dedup(
        items_filtered, WRITER_ARTICLES_PER_RUN, recent_for_intra,
    )
    if intra_skipped:
        skip_set = set(intra_skipped)
        # candidates 에서도 제거 (다음 cron 에 또 픽되지 않게)
        cur = load_json(CANDIDATES_PATH, {"last_updated": "", "items": []}).get("items", [])
        cur_clean = [i for i in cur if i.get("guid", "") not in skip_set]
        save_json(CANDIDATES_PATH, {
            "last_updated": datetime.now().isoformat(),
            "items": cur_clean,
        })
        logger.info(f"intra-batch 중복 {len(intra_skipped)}건 candidates 에서 제거")
    actual_n = len(selected)
    logger.info(f"선택: {actual_n}건")

    results = []
    total_in = total_out = 0
    model_used = "?"

    for idx, candidate in enumerate(selected, 1):
        result = process_one_candidate(candidate, idx, actual_n)
        results.append(result)

        api_meta = result.get("api_meta") or {}
        total_in += api_meta.get("input_tokens", 0)
        total_out += api_meta.get("output_tokens", 0)
        if api_meta.get("model"):
            model_used = api_meta["model"]

        status = result["status"]
        if status == "success":
            slack_notify(
                f"✍️📝 *작성+발행 완료* ({idx}/{actual_n})\n"
                f"제목: {result['title_en']}\n"
                f"WordPress: {result.get('link', '-')}\n"
                f"candidates 남음: {result.get('candidates_remaining', '?')}건"
            )
        elif status == "skipped":
            slack_notify(
                f"⚠️ *이미지 실패, 발행 스킵* ({idx}/{actual_n})\n"
                f"제목: {result['title_en']}\n"
                f"사유: {result.get('error', '-')}"
            )
        elif status in ("failed", "api_failed", "schema_failed"):
            slack_notify(
                f"❌ *{idx}/{actual_n} 실패*\n"
                f"제목: {result.get('title_en', result.get('title', '-'))}\n"
                f"에러: {str(result.get('error', ''))[:300]}"
            )
        # 다음 편으로 계속

    cost = estimate_cost_usd(total_in, total_out, model_used)
    success_count = sum(1 for r in results if r["status"] == "success")
    skipped_count = sum(1 for r in results if r["status"] == "skipped")
    failed_count = len(results) - success_count - skipped_count

    summary = (
        f"🏁 *Writer 완료*\n"
        f"처리: {len(results)}건 (성공 {success_count} / 스킵 {skipped_count} / 실패 {failed_count})\n"
        f"모델: {model_used} | 토큰 {total_in}in/{total_out}out | 약 ${cost:.4f}"
    )
    logger.info(summary)
    slack_notify(summary)

    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

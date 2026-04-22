#!/usr/bin/env python3
"""Tier 3 Writer (GitHub Actions 진입점).

candidates.json 상위 1건 선택 → Anthropic API로 영어 번역 + SEO HTML 생성 →
data/drafts/YYYY-MM-DD-{slug}.html 저장.
"""
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (
    CANDIDATES_PATH,
    WRITER_TARGET_WORD_COUNT_MAX,
    WRITER_TARGET_WORD_COUNT_MIN,
)
from src.anthropic_helper import call_json, estimate_cost_usd

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")
DRAFTS_DIR = PROJECT_ROOT / "data" / "drafts"
DRAFTS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


SYSTEM_PROMPT = f"""You are a bilingual editor who translates Korean K-pop news into SEO-optimized English blog posts for global K-pop fans.

## Output rules

Reply with **raw JSON only** (no markdown code fence, no prose wrapper). Schema:

```
{{
  "title_en": "English title, 40-65 chars, main keyword front-loaded",
  "slug": "english-slug-with-hyphens (3-6 meaningful words)",
  "meta_desc": "140-155 char English meta description with main keyword",
  "tags": ["tag1", "tag2", "tag3"],
  "featured_alt": "Descriptive alt text for lead image including artist name",
  "lead_paragraph": "Opening paragraph, 60-90 words, includes artist name + number/date in first sentence",
  "summary_bullets": ["3-4 concise bullet points for key summary box"],
  "body_sections": [
    {{"h2": "Question-style or keyword H2", "paragraphs": ["paragraph 1", "paragraph 2"]}},
    {{"h2": "...", "paragraphs": ["..."]}}
  ],
  "faq": [
    {{"q": "Question in PAA style (Why/When/How/Who)", "a": "Concise answer"}},
    {{"q": "...", "a": "..."}},
    {{"q": "...", "a": "..."}}
  ],
  "one_liner": "One-sentence key takeaway"
}}
```

## Content rules

- Total English body (lead + sections + faq answers): **{WRITER_TARGET_WORD_COUNT_MIN}-{WRITER_TARGET_WORD_COUNT_MAX} words**
- 3-5 body_sections. Each section: 1-2 paragraphs.
- Exactly 3 FAQ entries in PAA ("People Also Ask") style
- Preserve Korean proper nouns as standard English transliteration (BTS, BLACKPINK, Jennie, Rosé, etc.)
- For K-pop industry terms unfamiliar to Western readers, add brief parenthetical gloss (e.g., "comeback (a new album release)")
- Never fabricate numbers, dates, chart positions. Only use facts explicitly in the source.
- If the source is weak in specific facts, focus on what IS stated; do not invent.
- Tone: Informative fan-friendly blog voice. Neither tabloid-sensational nor dry-wire-copy.

## SEO checklist (follow strictly)

- Title: main keyword (artist name or song title) in first 30 characters
- Meta description: includes title keyword + specific number/fact
- Slug: English lowercase hyphen-separated, 3-6 words
- Lead paragraph: artist name + specific fact/date/number in first sentence
- H2s: questions or keyword-rich phrases (not generic "Introduction"/"Conclusion")
- FAQ questions: natural PAA phrasings
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


def pick_top_candidate(items: list[dict]) -> dict | None:
    """score 최상위 + collected_at 최신 1건 선택."""
    if not items:
        return None
    # 상위 3개 중 가장 최신
    top_n = sorted(items, key=lambda x: x.get("score", 0), reverse=True)[:3]
    return max(top_n, key=lambda x: x.get("collected_at", ""))


def build_user_message(item: dict) -> str:
    """Claude에게 번역 요청할 입력."""
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


def assemble_html(article: dict, meta: dict, source_item: dict) -> str:
    """Claude JSON 결과 + 원본 메타를 최종 HTML 주석 + 본문으로 조립."""
    created_at = datetime.now().isoformat()
    source_url = source_item.get("link", "")
    source_host = ""
    if source_url:
        m = re.match(r"https?://([^/]+)/", source_url)
        if m:
            source_host = f"https://{m.group(1)}/"

    thumbnail = source_item.get("thumbnail_url") or ""
    tags = ", ".join(article.get("tags", []))

    # ── 주석 헤더 ──
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

    # ── 본문 HTML ──
    parts = [header]

    # Featured image (원본 URL — publish-to-wordpress.yml이 WP media로 교체)
    if thumbnail:
        parts.append(
            f'<figure style="margin:0 0 24px 0;">'
            f'<img src="{thumbnail}" alt="{article.get("featured_alt", "")}" '
            f'style="width:100%;border-radius:8px;"/></figure>\n'
        )

    # Lead paragraph
    parts.append(
        f'<p style="margin-bottom:1.5em;line-height:1.8;">{article["lead_paragraph"]}</p>\n'
    )

    # Summary bullets box
    bullets = article.get("summary_bullets") or []
    if bullets:
        parts.append(
            '<div style="background:#EFF6FF;border-left:4px solid #2563EB;'
            'padding:20px 24px;border-radius:8px;margin:24px 0;">\n'
            '<strong>Key Points</strong>\n<ul>\n'
        )
        for b in bullets:
            parts.append(f"  <li>{b}</li>\n")
        parts.append("</ul>\n</div>\n")

    # Body sections
    for section in article.get("body_sections", []):
        h2 = section.get("h2", "")
        paragraphs = section.get("paragraphs", [])
        parts.append(
            f'<h2 style="margin-top:40px;border-bottom:2px solid #E2E8F0;'
            f'padding-bottom:8px;">{h2}</h2>\n'
        )
        for p in paragraphs:
            parts.append(f'<p style="margin-bottom:1.5em;line-height:1.8;">{p}</p>\n')

    # FAQ
    faqs = article.get("faq") or []
    if faqs:
        parts.append(
            '<h2 style="margin-top:40px;border-bottom:2px solid #E2E8F0;'
            'padding-bottom:8px;">FAQ</h2>\n'
        )
        for f in faqs:
            q = f.get("q", "")
            a = f.get("a", "")
            parts.append(
                '<details style="margin-bottom:12px;border:1px solid #E2E8F0;'
                'border-radius:6px;padding:12px;">\n'
                f"<summary><strong>Q. {q}</strong></summary>\n"
                f"<p>A. {a}</p>\n"
                "</details>\n"
            )

    # One-liner conclusion
    one_liner = article.get("one_liner", "").strip()
    if one_liner:
        parts.append(
            f'<p style="margin-top:32px;padding:16px 20px;background:#F8FAFC;'
            f'border-left:3px solid #64748B;border-radius:4px;font-style:italic;">'
            f"{one_liner}</p>\n"
        )

    # Source link
    if source_url:
        parts.append(
            f'<p style="margin-top:24px;font-size:0.9em;color:#64748B;">'
            f'Source: <a href="{source_url}" target="_blank" rel="noopener">'
            f"{source_item.get('title', source_url)}</a></p>\n"
        )

    return "".join(parts)


def main() -> int:
    logger.info("=" * 50)
    logger.info(f"Writer 시작: {datetime.now().isoformat()}")

    candidates_data = load_json(CANDIDATES_PATH, {"last_updated": "", "items": []})
    items = candidates_data.get("items", [])
    logger.info(f"candidates: {len(items)}건")

    if not items:
        slack_notify("⏸️ *Writer*: 작성 후보 없음")
        return 0

    selected = pick_top_candidate(items)
    if not selected:
        slack_notify("⏸️ *Writer*: 후보 선택 실패")
        return 0

    logger.info(f"선택: [{selected.get('score', 0)}점] {selected.get('title', '')}")

    try:
        article, meta = call_json(
            system=SYSTEM_PROMPT,
            user=build_user_message(selected),
            max_tokens=3500,
            temperature=0.4,
        )
    except Exception as e:
        logger.error(f"API 호출 실패: {e}")
        slack_notify(f"❌ *Writer 실패*\n제목: {selected.get('title', '')[:60]}\n에러: {str(e)[:300]}")
        return 1

    # 필수 필드 검증
    required = ["title_en", "slug", "meta_desc", "lead_paragraph", "body_sections"]
    missing = [k for k in required if not article.get(k)]
    if missing:
        logger.error(f"응답 필드 누락: {missing}")
        logger.error(f"응답: {json.dumps(article, ensure_ascii=False)[:500]}")
        slack_notify(f"❌ *Writer 실패*: 응답 필드 누락 {missing}")
        return 1

    # Slug 정규화 (영문 소문자 하이픈만)
    slug = re.sub(r"[^a-z0-9-]", "", article["slug"].lower())
    if not slug:
        slack_notify(f"❌ *Writer 실패*: slug 정규화 실패 ({article['slug']})")
        return 1
    article["slug"] = slug

    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"{today}-{slug}.html"
    html = assemble_html(article, meta, selected)

    filepath = DRAFTS_DIR / filename
    filepath.write_text(html, encoding="utf-8")
    logger.info(f"draft 저장: {filepath.name} ({len(html)} chars)")

    # candidates.json에서 이 guid 제거
    remaining = [i for i in items if i.get("guid") != selected.get("guid")]
    save_json(CANDIDATES_PATH, {
        "last_updated": datetime.now().isoformat(),
        "items": remaining,
    })

    # 단어 수 추정
    word_count = sum(
        len(s.split())
        for section in article.get("body_sections", [])
        for s in section.get("paragraphs", [])
    )
    word_count += len(article.get("lead_paragraph", "").split())
    word_count += sum(len(f.get("a", "").split()) for f in article.get("faq", []))

    cost = estimate_cost_usd(meta["input_tokens"], meta["output_tokens"], meta["model"])

    summary = (
        f"✍️ *Writer 완료*\n"
        f"제목: {article['title_en']}\n"
        f"slug: {slug}\n"
        f"점수: {selected.get('score', 0)} | 약 {word_count} words\n"
        f"candidates 남음: {len(remaining)}건\n"
        f"모델: {meta['model']} | 토큰 {meta['input_tokens']}in/{meta['output_tokens']}out | "
        f"약 ${cost:.4f}"
    )
    logger.info(summary)
    slack_notify(summary)

    return 0


if __name__ == "__main__":
    sys.exit(main())

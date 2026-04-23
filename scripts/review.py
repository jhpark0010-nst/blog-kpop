#!/usr/bin/env python3
"""Tier 5 Reviewer (GitHub Actions 진입점).

최근 24시간 발행된 글을 Anthropic API로 사후 감사:
- 중복: 최근 7일 발행 제목과 비교
- 팩트: 원문 URL fetch하여 대조
- 가독성/오탈자 (영어)
- SEO 규칙 준수

결과:
- FIX 지시 → review-actions.json에 append (review-apply workflow가 WP 적용)
- NOTIFY → Slack 경고만
- PASS → 아무 것도 안 함
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.anthropic_helper import call_json, estimate_cost_usd

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")
REVIEW_ACTIONS_PATH = PROJECT_ROOT / "data" / "review-actions.json"
PUBLISHED_DIR = PROJECT_ROOT / "data" / "drafts" / "published"

REVIEW_WINDOW_HOURS = 24
DEDUP_LOOKBACK_DAYS = 7

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
COMMENT_META_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are a post-publication auditor for an English K-pop news blog. Review a freshly published article against 4 criteria and decide an action.

## Criteria

1. **Duplicate**: Does this article heavily overlap with a recent post (same event, same artist, same angle)?
2. **Factual accuracy**: Compared with the source summary, are numbers/dates/chart positions/names consistent? Any invented facts?
3. **Readability**: Typos, awkward sentences, unnatural phrasing in English body.
4. **SEO**: Title length (40-65 chars), meta length (140-155), slug format, H2 quality, FAQ PAA style.

## Output

Reply with **raw JSON only** (no markdown fence). Schema:

```
{
  "action": "fix" | "notify" | "pass",
  "reason": "한 줄 요약 (한국어로 작성)",
  "issues": ["상세 이슈 1 (한국어)", "상세 이슈 2 (한국어)"],
  "new_content": "(if action=fix) full replacement HTML body in English (content only, no comment header)",
  "new_meta_desc": "(optional, if meta needs fixing — English, for WP)",
  "new_title": "(optional — English, for WP)"
}
```

**중요**: `reason` 과 `issues` 필드는 **반드시 한국어로** 작성 (사용자가 Slack에서 읽음). `new_content`/`new_title`/`new_meta_desc` 는 영어 유지 (블로그 본문은 영어 독자 대상).

## Action rules

- `fix`: minor readability/SEO fixes (typos, meta length, awkward sentences). Provide corrected HTML in `new_content`.
- `notify`: factual error or duplicate detected. Human must decide — do NOT set new_content. Just list issues.
- `pass`: no material issues.

Be conservative: when unsure, prefer `pass` over `fix`. Never invent facts in `new_content`.
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


def parse_comment_meta(html: str) -> dict[str, str]:
    m = COMMENT_META_RE.search(html)
    if not m:
        return {}
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip()
            if k and v:
                meta[k.lower()] = v
    return meta


def fetch_source_summary(url: str, max_chars: int = 2000) -> str | None:
    """원문 URL fetch → 본문 요약. 실패 시 None."""
    if not url or not url.startswith("http"):
        return None
    try:
        from bs4 import BeautifulSoup
        resp = requests.get(url, timeout=15, headers={"User-Agent": BROWSER_UA})
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup.select("script, style, nav, footer, aside, .ad"):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # 빈 줄 압축
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines)[:max_chars]
    except Exception as e:
        logger.warning(f"원문 fetch 실패 ({url}): {e}")
        return None


def collect_recent_titles(exclude_path: Path) -> list[str]:
    """최근 7일 내 published 글 제목 (자신 제외)."""
    cutoff = time.time() - DEDUP_LOOKBACK_DAYS * 86400
    titles = []
    for html_path in PUBLISHED_DIR.glob("*.html"):
        if html_path == exclude_path:
            continue
        if html_path.stat().st_mtime < cutoff:
            continue
        text = html_path.read_text(encoding="utf-8")[:3000]
        meta = parse_comment_meta(text)
        if t := meta.get("title"):
            titles.append(t)
    return titles


def build_review_input(
    article_html: str,
    article_meta: dict,
    source_summary: str | None,
    recent_titles: list[str],
) -> str:
    return (
        "## Article under review\n\n"
        f"**Title**: {article_meta.get('title', '')}\n"
        f"**Meta**: {article_meta.get('meta', '')}\n"
        f"**Slug**: {article_meta.get('slug', '')}\n"
        f"**Original (Korean)**: {article_meta.get('originaltitle', '')}\n"
        f"**Source URL**: {article_meta.get('originalurl', '')}\n\n"
        "### Full HTML body\n\n"
        f"{article_html[:8000]}\n\n"
        "### Source article excerpt (for fact check)\n\n"
        f"{source_summary or '(원문 fetch 실패 — 팩트체크 스킵)'}\n\n"
        "### Recent 7-day published titles (for duplicate check)\n\n"
        + "\n".join(f"- {t}" for t in recent_titles[:30])
        + "\n\nReturn JSON per schema."
    )


def strip_comment_header(html: str) -> str:
    """발행 후 맨 위 주석 헤더 + 본문만 (하단 WP 정보 주석은 유지)."""
    # 맨 앞 주석 블록 제거 후 return
    return COMMENT_META_RE.sub("", html, count=1).strip()


def review_one_file(filepath: Path, recent_titles: list[str]) -> dict:
    """한 파일 검증 → {action, ...} 결과 dict (원본 파일 정보 포함)."""
    html = filepath.read_text(encoding="utf-8")
    meta = parse_comment_meta(html)
    body_only = strip_comment_header(html)

    source_url = meta.get("originalurl", "")
    source_summary = fetch_source_summary(source_url) if source_url else None

    try:
        result, api_meta = call_json(
            system=SYSTEM_PROMPT,
            user=build_review_input(body_only, meta, source_summary, recent_titles),
            max_tokens=3500,
            temperature=0.2,
        )
    except Exception as e:
        logger.error(f"API 호출 실패 ({filepath.name}): {e}")
        return {
            "file": filepath.name,
            "action": "error",
            "reason": str(e)[:200],
            "_api_meta": {"input_tokens": 0, "output_tokens": 0, "model": "?"},
        }

    result["_file"] = filepath.name
    result["_meta"] = meta
    result["_api_meta"] = api_meta
    return result


def main() -> int:
    logger.info("=" * 50)
    logger.info(f"Reviewer 시작: {datetime.now().isoformat()}")

    if not PUBLISHED_DIR.exists():
        logger.info("published 디렉토리 없음. 종료.")
        slack_notify("🔍 *Reviewer*: published 디렉토리 없음")
        return 0

    cutoff = time.time() - REVIEW_WINDOW_HOURS * 3600
    files = sorted(
        (p for p in PUBLISHED_DIR.glob("*.html") if p.stat().st_mtime >= cutoff),
        key=lambda p: p.stat().st_mtime,
    )
    logger.info(f"최근 {REVIEW_WINDOW_HOURS}h 발행글: {len(files)}건")

    if not files:
        slack_notify(f"🔍 *Reviewer*: 최근 {REVIEW_WINDOW_HOURS}h 발행글 없음")
        return 0

    actions_data = load_json(REVIEW_ACTIONS_PATH, {"last_updated": "", "actions": []})
    existing_actions = actions_data.get("actions", [])

    pass_count = 0
    fix_actions = []
    notify_messages = []
    error_count = 0
    total_in = total_out = 0
    model_used = "?"

    for filepath in files:
        logger.info(f"검토 중: {filepath.name}")
        recent_titles = collect_recent_titles(filepath)
        result = review_one_file(filepath, recent_titles)

        api_meta = result.get("_api_meta", {})
        total_in += api_meta.get("input_tokens", 0)
        total_out += api_meta.get("output_tokens", 0)
        model_used = api_meta.get("model", model_used)

        action = result.get("action", "pass")
        reason = result.get("reason", "")
        meta = result.get("_meta", {})

        if action == "error":
            error_count += 1
        elif action == "pass":
            pass_count += 1
            logger.info(f"  PASS: {reason}")
        elif action == "fix":
            # review-actions.json용 액션 생성
            fix_entry = {
                "slug": meta.get("slug"),
                "action": "fix",
                "reason": reason,
                "source_file": filepath.name,
            }
            if post_id := meta.get("wppostid"):
                try:
                    fix_entry["post_id"] = int(post_id)
                except ValueError:
                    pass
            if nc := result.get("new_content"):
                fix_entry["new_content"] = nc
            if nmd := result.get("new_meta_desc"):
                fix_entry["new_meta_desc"] = nmd
            if nt := result.get("new_title"):
                fix_entry["new_title"] = nt
            fix_actions.append(fix_entry)
            logger.info(f"  FIX: {reason}")
        elif action == "notify":
            issues = result.get("issues", [])
            notify_messages.append({
                "title": meta.get("title", filepath.name),
                "reason": reason,
                "issues": issues,
            })
            logger.warning(f"  NOTIFY: {reason}")

    # FIX 액션 병합 → review-actions.json 저장 (있을 때만)
    if fix_actions:
        existing_actions.extend(fix_actions)
        save_json(REVIEW_ACTIONS_PATH, {
            "last_updated": datetime.now().isoformat(),
            "actions": existing_actions,
        })

    cost = estimate_cost_usd(total_in, total_out, model_used)

    # Slack 통합 알림
    lines = [
        f"🔍 *Reviewer 완료 (최근 {REVIEW_WINDOW_HOURS}h {len(files)}건)*",
        f"PASS: {pass_count} | FIX: {len(fix_actions)} | NOTIFY: {len(notify_messages)} | ERROR: {error_count}",
    ]
    if fix_actions:
        lines.append("\n*FIX (자동 수정 지시 → 잠시 후 WP 반영)*")
        for f in fix_actions[:5]:
            lines.append(f"• {f['slug']}: {f['reason']}")
    if notify_messages:
        lines.append("\n⚠️ *NOTIFY (사람 판단 필요)*")
        for n in notify_messages[:5]:
            lines.append(f"• {n['title'][:50]}: {n['reason']}")
            for issue in n.get("issues", [])[:2]:
                lines.append(f"  - {issue}")
    lines.append(f"\n모델: {model_used} | 토큰 {total_in}in/{total_out}out | 약 ${cost:.4f}")
    slack_notify("\n".join(lines))

    logger.info(
        f"Reviewer 완료: PASS {pass_count}, FIX {len(fix_actions)}, "
        f"NOTIFY {len(notify_messages)}, ERROR {error_count}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

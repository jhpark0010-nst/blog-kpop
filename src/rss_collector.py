"""RSS 수집기 (K-pop 블로그용).

- 각 피드에서 항목 가져오기
- 이미 처리된 GUID는 스킵
- 이미지 URL(thumbnail_url) 추출
- 원문 내용 스크래핑 (본문 길이 부족 시)
"""
import hashlib
import logging
from datetime import datetime

import feedparser
import requests
from bs4 import BeautifulSoup

from config.settings import RSS_FEEDS

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

logger = logging.getLogger(__name__)

DATE_FORMATS = [
    "%a, %d %b %Y %H:%M:%S %z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
]


def parse_date(date_str: str | None) -> str | None:
    if not date_str:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(date_str, fmt).isoformat()
        except ValueError:
            continue
    return date_str


def generate_guid(entry: dict, source: str) -> str:
    if entry.get("id"):
        return entry["id"]
    if entry.get("link"):
        return entry["link"]
    raw = f"{source}:{entry.get('title', '')}".encode()
    return hashlib.sha256(raw).hexdigest()


def extract_thumbnail_url(entry) -> str | None:
    """RSS entry에서 이미지 URL 추출."""
    mc = entry.get("media_content")
    if mc and isinstance(mc, list):
        for m in mc:
            url = m.get("url")
            if url:
                return url
    mt = entry.get("media_thumbnail")
    if mt and isinstance(mt, list):
        for m in mt:
            url = m.get("url")
            if url:
                return url
    for enc in entry.get("enclosures", []) or []:
        typ = enc.get("type", "") or ""
        href = enc.get("href", "") or enc.get("url", "")
        if typ.startswith("image/") and href:
            return href
    for src_html in (entry.get("summary", ""), _get_content_html(entry)):
        if not src_html:
            continue
        img = BeautifulSoup(src_html, "html.parser").find("img")
        if img and img.get("src"):
            return img.get("src")
    return None


def _get_content_html(entry) -> str:
    content = entry.get("content")
    if content and isinstance(content, list) and content:
        return content[0].get("value", "") or ""
    return ""


def fetch_full_content(url: str) -> str | None:
    """원문 링크에서 본문 스크래핑."""
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": BROWSER_UA})
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "html.parser")

        selectors = [
            "div.article-txt", "div#articleWrap", "div.news-contents",
            "div.article_view", "div.view_con", "div#content",
            "article", "div.board_view",
        ]
        for selector in selectors:
            el = soup.select_one(selector)
            if el:
                for tag in el.select("script, style, .sns_share, .ad, .file_area"):
                    tag.decompose()
                text = el.get_text(separator="\n", strip=True)
                if len(text) > 100:
                    return text[:5000]
        return None
    except Exception as e:
        logger.warning(f"스크래핑 실패 ({url}): {e}")
        return None


def collect_all_feeds(processed_guids: set) -> list[dict]:
    """모든 RSS 피드에서 신규 항목 수집."""
    all_items = []

    for name, config in RSS_FEEDS.items():
        url = config["url"]
        category = config["category"]
        priority = config["priority"]
        new_count = 0

        logger.info(f"[수집] {name} ({url})")

        try:
            try:
                resp = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=15)
                resp.raise_for_status()
                feed = feedparser.parse(resp.content)
            except requests.RequestException as e:
                logger.error(f"[수집] {name} HTTP 요청 실패: {e}")
                continue

            if feed.bozo and not feed.entries:
                logger.error(f"[수집] {name} 파싱 실패: {feed.bozo_exception}")
                continue

            for entry in feed.entries:
                guid = generate_guid(entry, name)

                if guid in processed_guids:
                    continue

                published = None
                if hasattr(entry, "published"):
                    published = parse_date(entry.published)
                elif hasattr(entry, "updated"):
                    published = parse_date(entry.updated)

                summary = ""
                if hasattr(entry, "summary"):
                    summary = BeautifulSoup(
                        entry.summary, "html.parser"
                    ).get_text(strip=True)

                content = ""
                if hasattr(entry, "content") and entry.content:
                    content = BeautifulSoup(
                        entry.content[0].value, "html.parser"
                    ).get_text(strip=True)

                link = entry.get("link", "")
                if len(summary) < 200 and len(content) < 200 and link:
                    full = fetch_full_content(link)
                    if full:
                        content = full

                item = {
                    "guid": guid,
                    "source": name,
                    "title": entry.get("title", "").strip(),
                    "link": link,
                    "published": published,
                    "summary": summary[:2000],
                    "content": content[:5000],
                    "thumbnail_url": extract_thumbnail_url(entry),
                    "category": category,
                    "priority": priority,
                    "collected_at": datetime.now().isoformat(),
                    "status": "unreviewed",
                    "evaluated_at": None,
                    "score": None,
                }
                all_items.append(item)
                new_count += 1

            logger.info(f"[수집] {name}: 전체 {len(feed.entries)}건, 신규 {new_count}건")

        except Exception as e:
            logger.error(f"[수집] {name} 오류: {e}")

    logger.info(f"[수집 완료] 총 신규 {len(all_items)}건")
    return all_items

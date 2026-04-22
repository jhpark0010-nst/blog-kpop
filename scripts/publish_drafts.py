"""Tier 4: data/drafts/*.html → WordPress REST API 발행.

- HTML 주석에서 Title/Slug/Meta/FeaturedAlt/ImageSourceURL/ImageSourceReferer 추출
- 이미지 다운로드 → WP 미디어 업로드 → featured_media 지정 + 본문 src 교체
- 발행 후 data/drafts/published/ 로 이동, 파일에 WpPostId/WpLink 주석 추가
- 이미지 다운로드 실패 시 drafts/skipped/ 로 이동 + Slack 경고
"""
from __future__ import annotations

import base64
import mimetypes
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# env 변수 지연 접근 (write.py import 시 env 없어도 안 깨지도록)
WP_URL = os.environ.get("WP_URL", "").rstrip("/")
WP_USERNAME = os.environ.get("WP_USERNAME", "")
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "")
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")

DRAFTS_DIR = Path("data/drafts")
PUBLISHED_DIR = DRAFTS_DIR / "published"
SKIPPED_DIR = DRAFTS_DIR / "skipped"
PUBLISHED_DIR.mkdir(parents=True, exist_ok=True)
SKIPPED_DIR.mkdir(parents=True, exist_ok=True)

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}-")
COMMENT_META_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def auth_header() -> dict:
    token = base64.b64encode(f"{WP_USERNAME}:{WP_APP_PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def parse_comment_meta(html: str) -> dict[str, str]:
    """HTML 상단 주석에서 'Key: Value' 메타 추출."""
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


def download_image(url: str, referer: str | None = None) -> tuple[bytes, str] | None:
    """이미지 다운로드. 실패 시 None.

    2026-04-17 K-pop RSS 실측:
    - 연합뉴스: HEAD는 400 에러, GET은 200. Referer 불필요.
    - 안전을 위해 Referer 옵션은 유지 (다른 소스에 필요할 수도)
    """
    headers = {"User-Agent": BROWSER_UA}
    if referer:
        headers["Referer"] = referer

    try:
        resp = requests.get(url, headers=headers, timeout=20, stream=False)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
        if not content_type.startswith("image/"):
            # 확장자로 추정
            ext = url.split("?")[0].rsplit(".", 1)
            if len(ext) == 2:
                guessed = mimetypes.types_map.get("." + ext[1].lower())
                if guessed and guessed.startswith("image/"):
                    content_type = guessed
            if not content_type.startswith("image/"):
                print(f"  ⚠ 이미지 아님: {url} (Content-Type={content_type})", file=sys.stderr)
                return None
        return resp.content, content_type
    except requests.RequestException as e:
        print(f"  ❌ 이미지 다운로드 실패: {url} — {e}", file=sys.stderr)
        return None


def upload_to_wp_media(
    image_bytes: bytes,
    filename: str,
    content_type: str,
    alt_text: str = "",
) -> tuple[int, str] | None:
    """WP 미디어 라이브러리 업로드. (media_id, source_url) 반환."""
    try:
        resp = requests.post(
            f"{WP_URL}/wp-json/wp/v2/media",
            headers={
                **auth_header(),
                "Content-Type": content_type,
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
            data=image_bytes,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        media_id = data["id"]
        source_url = data.get("source_url") or data.get("guid", {}).get("rendered", "")

        # alt_text PATCH (Content-Type: application/json)
        if alt_text:
            try:
                requests.post(
                    f"{WP_URL}/wp-json/wp/v2/media/{media_id}",
                    headers={**auth_header(), "Content-Type": "application/json"},
                    json={"alt_text": alt_text},
                    timeout=15,
                )
            except Exception as e:
                print(f"  alt 설정 실패 (media_id={media_id}): {e}", file=sys.stderr)

        return media_id, source_url
    except requests.HTTPError as e:
        print(
            f"  ❌ WP 미디어 업로드 실패: HTTP {e.response.status_code} — "
            f"{e.response.text[:200]}",
            file=sys.stderr,
        )
        return None
    except Exception as e:
        print(f"  ❌ WP 미디어 업로드 실패: {e}", file=sys.stderr)
        return None


def process_images(html: str, meta: dict[str, str]) -> tuple[str, int | None]:
    """HTML의 모든 img src → WP 미디어로 교체. (새 HTML, featured_media_id) 반환.

    다운로드 완전 실패 시 (None, None) 반환 → 호출자가 skip 결정.
    """
    soup = BeautifulSoup(html, "html.parser")
    imgs = soup.find_all("img")
    if not imgs:
        return html, None

    referer = meta.get("imagesourcereferer")
    featured_alt = meta.get("featuredalt", "")

    featured_media_id: int | None = None
    any_success = False

    for idx, img in enumerate(imgs):
        src = img.get("src")
        if not src or not src.startswith("http"):
            continue

        result = download_image(src, referer=referer)
        if not result:
            continue
        image_bytes, content_type = result

        # 파일명 생성
        ext = content_type.split("/")[-1]
        ext = {"jpeg": "jpg"}.get(ext, ext)
        safe_slug = meta.get("slug", "image")
        filename = f"{safe_slug}-{idx + 1}.{ext}"

        alt = img.get("alt") or (featured_alt if idx == 0 else "")
        upload = upload_to_wp_media(image_bytes, filename, content_type, alt_text=alt)
        if not upload:
            continue

        media_id, new_src = upload
        if new_src:
            img["src"] = new_src
        any_success = True

        if idx == 0:
            featured_media_id = media_id

    if not any_success:
        return "", None

    return str(soup), featured_media_id


def publish_to_wp(
    title: str,
    content: str,
    meta_desc: str,
    slug: str,
    featured_media: int | None,
) -> tuple[int, str]:
    payload = {
        "title": title,
        "content": content,
        "status": "publish",
        "slug": slug,
    }
    if featured_media:
        payload["featured_media"] = featured_media
    if meta_desc:
        payload["meta"] = {"yoast_wpseo_metadesc": meta_desc}
    resp = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts",
        headers={**auth_header(), "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    d = resp.json()
    return d["id"], d["link"]


def slack_notify(text: str) -> None:
    if not SLACK_WEBHOOK:
        return
    try:
        requests.post(SLACK_WEBHOOK, json={"text": text}, timeout=10)
    except Exception as e:
        print(f"Slack 알림 실패: {e}", file=sys.stderr)


def append_publish_comment(html: str, post_id: int, wp_link: str) -> str:
    """발행 성공 후 HTML 끝에 WpPostId/WpLink 주석 추가 (Reviewer 참조용)."""
    tail = (
        "\n<!--\n"
        f"WpPostId: {post_id}\n"
        f"WpLink: {wp_link}\n"
        f"PublishedAt: {datetime.now().isoformat()}\n"
        "-->\n"
    )
    return html + tail


def publish_single_draft(path: Path) -> dict:
    """단일 draft 파일을 이미지 업로드 + WP 발행 + published/ 이동까지 처리.

    Slack 알림은 보내지 않음 (호출자가 결정).

    Returns:
        dict with keys:
          - status: "success" | "skipped" | "failed"
          - title, file (always)
          - link, post_id (success only)
          - reason (skipped only)
          - error (failed only)
    """
    html = path.read_text(encoding="utf-8")
    meta = parse_comment_meta(html)
    title = meta.get("title") or path.stem
    slug = meta.get("slug") or DATE_PATTERN.sub("", path.stem).lower()
    meta_desc = meta.get("meta", "")

    new_html, featured_media = process_images(html, meta)
    if not new_html:
        dest = SKIPPED_DIR / path.name
        shutil.move(str(path), str(dest))
        return {
            "status": "skipped",
            "title": title,
            "file": path.name,
            "reason": "이미지 다운로드/업로드 실패",
        }

    try:
        post_id, link = publish_to_wp(title, new_html, meta_desc, slug, featured_media)
        final_html = append_publish_comment(new_html, post_id, link)
        dest = PUBLISHED_DIR / path.name
        dest.write_text(final_html, encoding="utf-8")
        path.unlink()
        return {
            "status": "success",
            "title": title,
            "file": path.name,
            "link": link,
            "post_id": post_id,
        }
    except requests.HTTPError as e:
        return {
            "status": "failed",
            "title": title,
            "file": path.name,
            "error": f"HTTP {e.response.status_code}: {e.response.text[:300]}",
        }
    except Exception as e:
        return {
            "status": "failed",
            "title": title,
            "file": path.name,
            "error": str(e),
        }


def main() -> int:
    files = sorted(
        f for f in DRAFTS_DIR.glob("*.html") if DATE_PATTERN.match(f.name)
    )
    if not files:
        print("발행할 초안 없음")
        return 0

    print(f"발행 대상 {len(files)}건")
    success, failures, skipped = [], [], []

    for path in files:
        result = publish_single_draft(path)
        status = result["status"]

        if status == "success":
            success.append(result)
            slack_notify(
                f"📝 *새 글 공개 발행*\n제목: {result['title']}\nWordPress: {result['link']}"
            )
            print(f"OK: {result['file']} → {result['link']} (post_id={result['post_id']})")
        elif status == "skipped":
            skipped.append(result)
            slack_notify(
                f"⚠️ *발행 스킵*\n제목: {result['title']}\n"
                f"사유: {result['reason']}. drafts/skipped/ 로 이동."
            )
            print(f"SKIP: {result['file']}", file=sys.stderr)
        else:  # failed
            failures.append(result)
            slack_notify(
                f"❌ *WordPress 발행 실패*\n파일: {result['file']}\n에러: {result['error']}"
            )
            print(f"FAIL: {result['file']} - {result['error']}", file=sys.stderr)

    slack_notify(
        f"📊 *발행 요약*\n"
        f"성공: {len(success)}건 / 실패: {len(failures)}건 / 스킵: {len(skipped)}건"
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

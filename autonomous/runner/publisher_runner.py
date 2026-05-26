"""
Publisher Runner — Ghost CMS

Publishes a finished article to Ghost as a DRAFT via the Ghost Admin API.
Never publishes directly — status is always "draft".

Authentication uses JWT (HS256) as required by Ghost Admin API.

On any error (bad credentials, wrong URL, network failure), logs clearly
and saves the article locally instead of crashing.
"""

import os
import sys
import re
import time
import logging
import json
from pathlib import Path
from typing import Dict, Optional, Any

import requests

_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT))

log = logging.getLogger(__name__)


# ── JWT helper ────────────────────────────────────────────────────────────────

def _ghost_jwt(admin_api_key: str) -> str:
    """Generate a short-lived JWT for Ghost Admin API authentication."""
    try:
        import jwt as pyjwt  # PyJWT

        key_id, secret = admin_api_key.split(":", 1)
        iat = int(time.time())
        payload = {
            "iat": iat,
            "exp": iat + 300,  # 5-minute expiry
            "aud": "/admin/",
        }
        token = pyjwt.encode(
            payload,
            bytes.fromhex(secret),
            algorithm="HS256",
            headers={"kid": key_id},
        )
        # PyJWT >= 2.0 returns str directly
        return token if isinstance(token, str) else token.decode("utf-8")
    except ImportError:
        raise RuntimeError("PyJWT is required: pip install PyJWT")
    except ValueError as exc:
        raise ValueError(
            f"GHOST_ADMIN_API_KEY format must be 'key_id:hex_secret' — got error: {exc}"
        )


# ── Markdown → HTML ────────────────────────────────────────────────────────────

def _md_to_html(markdown_text: str) -> str:
    """Convert markdown to HTML. Strips meta title/description lines."""
    try:
        import markdown as md_lib  # type: ignore

        # Strip out meta fields that shouldn't appear in the article body
        clean = re.sub(
            r"\*\*Meta (Title|Description)\*\*:.*?(?:\n|$)",
            "",
            markdown_text,
            flags=re.IGNORECASE,
        )
        clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
        html = md_lib.markdown(clean, extensions=["extra", "sane_lists"])
        return html
    except ImportError:
        raise RuntimeError("markdown library is required: pip install markdown")


# ── Image injection ──────────────────────────────────────────────────────────

def _inject_images_into_html(html: str, images: list) -> str:
    """
    Insert section images after each <h2> tag in the HTML.
    Feature image is handled separately (set as Ghost feature_image).
    Each image is wrapped in a <figure> with alt + figcaption.
    """
    if not images:
        return html

    section_images = [img for img in images if img.get("position") == "section"]
    if not section_images:
        return html

    # Find all <h2> tags and inject one image after each (up to available images)
    h2_pattern = re.compile(r"(<h2[^>]*>.*?</h2>)", re.IGNORECASE | re.DOTALL)
    h2_positions = [m.end() for m in h2_pattern.finditer(html)]

    if not h2_positions:
        return html

    # Build insertion map: position → figure HTML
    insertions = {}
    for i, img in enumerate(section_images):
        if i >= len(h2_positions):
            break
        pos = h2_positions[i]
        figure = (
            f'<figure class="kg-image-card">'
            f'<img src="{img["url"]}" alt="{img["alt"]}" loading="lazy" '
            f'class="kg-image" />'
            f'<figcaption>{img["caption"]}</figcaption>'
            f'</figure>'
        )
        insertions[pos] = figure

    # Build final HTML with injections (process in reverse to preserve offsets)
    result = html
    for pos in sorted(insertions.keys(), reverse=True):
        result = result[:pos] + insertions[pos] + result[pos:]

    return result


# ── Tag helpers ───────────────────────────────────────────────────────────────

def _extract_tags_from_article(article: str, keyword: str) -> list:
    """
    Derive tag names from keyword and article content.
    Ghost will auto-create tags that don't exist.
    """
    tags = []
    # Primary tag from keyword (e.g. "best phones" → "Best Phones")
    primary = keyword.title()
    tags.append({"name": primary})

    # Look for explicit tags in the draft (format: **Tags**: tag1, tag2)
    tag_match = re.search(r"\*\*Tags?\*\*:\s*(.+?)(?:\n|$)", article, re.IGNORECASE)
    if tag_match:
        for t in tag_match.group(1).split(","):
            t = t.strip()
            if t and t.lower() != primary.lower():
                tags.append({"name": t})

    # Cap at 5 tags
    return tags[:5]


# ── Ghost API client ──────────────────────────────────────────────────────────

class GhostPublisher:
    def __init__(self, ghost_url: str, admin_api_key: str):
        self.base_url = ghost_url.rstrip("/")
        self.admin_api_key = admin_api_key
        self.api_version = "v5.0"

    def _headers(self) -> Dict:
        token = _ghost_jwt(self.admin_api_key)
        return {
            "Authorization": f"Ghost {token}",
            "Content-Type": "application/json",
            "Accept-Version": self.api_version,
        }

    def _api_url(self, path: str) -> str:
        return f"{self.base_url}/ghost/api/admin/{path.lstrip('/')}"

    def create_draft(
        self,
        title: str,
        html: str,
        meta_title: str = "",
        meta_description: str = "",
        custom_excerpt: str = "",
        tags: Optional[list] = None,
        slug: str = "",
        feature_image: str = "",
        feature_image_alt: str = "",
        feature_image_caption: str = "",
    ) -> Dict:
        """
        Create a new draft post in Ghost.

        Returns dict with: ghost_id, ghost_editor_url, ghost_url
        Raises on non-recoverable errors.
        """
        payload: Dict[str, Any] = {
            "title": title,
            "html": html,
            "status": "draft",  # ALWAYS draft
        }
        if meta_title:
            payload["meta_title"] = meta_title[:255]
        if meta_description:
            payload["meta_description"] = meta_description[:500]
        if custom_excerpt:
            payload["custom_excerpt"] = custom_excerpt[:300]
        if tags:
            payload["tags"] = tags
        if slug:
            payload["slug"] = slug[:185]
        if feature_image:
            payload["feature_image"] = feature_image
        if feature_image_alt:
            payload["feature_image_alt"] = feature_image_alt[:500]
        if feature_image_caption:
            payload["feature_image_caption"] = feature_image_caption[:500]

        url = self._api_url("posts/?source=html")
        response = requests.post(
            url,
            headers=self._headers(),
            json={"posts": [payload]},
            timeout=30,
        )

        if response.status_code == 401:
            raise PermissionError(
                "Ghost API: 401 Unauthorized — check GHOST_ADMIN_API_KEY is correct"
            )
        if response.status_code == 404:
            raise ConnectionError(
                f"Ghost API: 404 Not Found — check GHOST_URL is correct: {self.base_url}"
            )
        if not response.ok:
            raise RuntimeError(
                f"Ghost API error {response.status_code}: {response.text[:300]}"
            )

        data = response.json()
        posts = data.get("posts", [])
        if not posts:
            raise RuntimeError("Ghost API returned no posts in response")

        post = posts[0]
        post_id = post.get("id", "")
        admin_url = f"{self.base_url}/ghost/#/editor/post/{post_id}"

        return {
            "ghost_id": post_id,
            "ghost_editor_url": admin_url,
            "ghost_url": post.get("url", ""),
            "ghost_status": post.get("status", "draft"),
        }


# ── Credentials check ─────────────────────────────────────────────────────────

def _ghost_configured() -> bool:
    return bool(os.getenv("GHOST_URL") and os.getenv("GHOST_ADMIN_API_KEY"))


# ── Local fallback save ───────────────────────────────────────────────────────

def _save_locally(slug: str, article: str, reason: str) -> str:
    """Save article to review-required/ as local fallback."""
    review_dir = _ROOT / "review-required"
    review_dir.mkdir(exist_ok=True)
    path = review_dir / f"{slug}.md"
    path.write_text(article, encoding="utf-8")
    log.info("     Saved locally to review-required/%s.md (%s)", slug, reason)
    return str(path)


# ── Public API ────────────────────────────────────────────────────────────────

def run(
    keyword: str,
    final_article: str,
    title: str,
    slug: str,
    meta_title: str,
    meta_description: str,
    seo_score: int,
    config: Dict,
    dry_run: bool = False,
    images: Optional[list] = None,
) -> Dict:
    """
    Publish the article to Ghost as a draft.

    Returns dict with: published (bool), ghost_id, ghost_editor_url,
    ghost_url, local_path, status
    """
    log.info("  → Step 5/5: Publishing to Ghost as draft…")

    min_score = config.get("min_score_to_publish", 65)

    # ── Score gate ────────────────────────────────────────────────────────────
    if seo_score < min_score:
        log.warning(
            "     Score %d < minimum %d — saving locally instead of publishing",
            seo_score, min_score,
        )
        local_path = _save_locally(slug, final_article, f"low score: {seo_score}")
        return {
            "published": False,
            "status": "low_score",
            "local_path": local_path,
            "ghost_id": "",
            "ghost_editor_url": "",
            "ghost_url": "",
        }

    # ── Dry run ───────────────────────────────────────────────────────────────
    if dry_run:
        local_path = _save_locally(slug, final_article, "dry-run")
        log.info("     Dry run — not publishing to Ghost")
        return {
            "published": False,
            "status": "dry_run",
            "local_path": local_path,
            "ghost_id": "",
            "ghost_editor_url": "",
            "ghost_url": "",
        }

    # ── Ghost not configured ──────────────────────────────────────────────────
    if not _ghost_configured():
        local_path = _save_locally(slug, final_article, "Ghost not configured")
        log.warning("     GHOST_URL or GHOST_ADMIN_API_KEY not set — saved locally")
        return {
            "published": False,
            "status": "no_ghost_config",
            "local_path": local_path,
            "ghost_id": "",
            "ghost_editor_url": "",
            "ghost_url": "",
        }

    # ── Convert to HTML ───────────────────────────────────────────────────────
    try:
        html = _md_to_html(final_article)
    except Exception as exc:
        log.error("     Markdown→HTML conversion failed: %s", exc)
        local_path = _save_locally(slug, final_article, "html conversion failed")
        return {
            "published": False,
            "status": "failed",
            "local_path": local_path,
            "ghost_id": "",
            "ghost_editor_url": "",
            "ghost_url": "",
            "error": str(exc),
        }

    # ── Inject section images into HTML ──────────────────────────────────────
    images = images or []
    html = _inject_images_into_html(html, images)

    # Extract feature image (first image with position="feature")
    feature_image_url = ""
    feature_image_alt = ""
    feature_image_caption = ""
    for img in images:
        if img.get("position") == "feature":
            feature_image_url = img.get("url", "")
            feature_image_alt = img.get("alt", "")
            feature_image_caption = img.get("caption", "")
            break

    # ── Build tags ────────────────────────────────────────────────────────────
    tags = _extract_tags_from_article(final_article, keyword)

    # ── Publish to Ghost ──────────────────────────────────────────────────────
    try:
        ghost_url = os.getenv("GHOST_URL", "")
        ghost_key = os.getenv("GHOST_ADMIN_API_KEY", "")
        publisher = GhostPublisher(ghost_url, ghost_key)

        result = publisher.create_draft(
            title=title,
            html=html,
            meta_title=meta_title,
            meta_description=meta_description,
            custom_excerpt=meta_description[:300] if meta_description else "",
            tags=tags,
            slug=slug,
            feature_image=feature_image_url,
            feature_image_alt=feature_image_alt,
            feature_image_caption=feature_image_caption,
        )

        log.info("  ✅ Draft created: %s", result["ghost_editor_url"])

        return {
            "published": True,
            "status": "published_draft",
            "local_path": "",
            **result,
        }

    except (PermissionError, ConnectionError, RuntimeError) as exc:
        log.error("     Ghost publishing failed: %s", exc)
        local_path = _save_locally(slug, final_article, f"Ghost error: {exc}")
        return {
            "published": False,
            "status": "failed",
            "local_path": local_path,
            "ghost_id": "",
            "ghost_editor_url": "",
            "ghost_url": "",
            "error": str(exc),
        }
    except Exception as exc:
        log.error("     Unexpected Ghost publishing error: %s", exc)
        local_path = _save_locally(slug, final_article, f"unexpected error: {exc}")
        return {
            "published": False,
            "status": "failed",
            "local_path": local_path,
            "ghost_id": "",
            "ghost_editor_url": "",
            "ghost_url": "",
            "error": str(exc),
        }

"""
Image Runner

Generates and uploads images for each article:
  1. Claude analyses the article + competitor image alts → writes image prompts
  2. Flux 1.1 Pro (Replicate) generates photorealistic / conceptual images
  3. Ideogram generates any callout/stat graphic that needs embedded text
  4. All images are uploaded to Ghost via POST /ghost/api/admin/images/upload/
  5. Returns a list of {url, alt, caption, position} dicts ready for insertion

Config keys (pipeline_config.yaml):
  images_enabled: true
  images_per_article: 3          # total images including feature
  use_ideogram_for_callouts: true
  image_width: 1344              # Flux recommended width (landscape)
  image_height: 768
  replicate_model: "black-forest-labs/flux-1.1-pro"

Env vars required:
  REPLICATE_API_KEY
  IDEOGRAM_API_KEY   (optional — only if use_ideogram_for_callouts: true)
  GHOST_URL + GHOST_ADMIN_API_KEY  (to upload images)
"""

import io
import os
import sys
import re
import time
import logging
import mimetypes
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests

_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT))

log = logging.getLogger(__name__)

# ── Competitor image extraction ───────────────────────────────────────────────

def _extract_competitor_image_alts(competitor_data: List[Dict]) -> List[str]:
    """Pull alt text from competitor scraped data for prompt context."""
    alts = []
    for comp in competitor_data:
        for alt in comp.get("image_alts", []):
            alt = alt.strip()
            if alt and len(alt) > 5:
                alts.append(alt)
    return alts[:20]


# ── Claude prompt generation ──────────────────────────────────────────────────

def _call_claude(system: str, user: str, model: str, max_tokens: int = 1500) -> str:
    import anthropic
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text


def _generate_image_prompts(
    keyword: str,
    article: str,
    title: str,
    competitor_alts: List[str],
    n_images: int,
    use_callout: bool,
    model: str,
) -> List[Dict]:
    """
    Ask Claude to produce n_images image prompts.

    Returns a list of dicts:
      {type: "photo"|"callout", prompt: str, alt: str, caption: str, position: "feature"|"section"}
    """
    # Extract first ~600 words of article for context
    article_preview = " ".join(article.split()[:600])

    # Extract H2 headings as section anchors
    headings = re.findall(r"^##\s+(.+)$", article, re.MULTILINE)[:6]
    headings_str = "\n".join(f"- {h}" for h in headings) if headings else "None extracted"

    competitor_alts_str = (
        "\n".join(f"- {a}" for a in competitor_alts[:10])
        if competitor_alts
        else "None available"
    )

    callout_instruction = (
        f"\n- Image {n_images}: type=callout — a graphic with a short bold stat or key insight "
        "embedded as text inside the image. This will be generated with Ideogram (handles text well). "
        "Write a short Ideogram prompt: describe the visual style AND include the exact text that should "
        "appear in the image (e.g. '72% of marketers use AI'). Keep embedded text under 10 words."
        if use_callout
        else ""
    )

    system = (
        "You are an expert SEO image strategist. You write precise, detailed image generation prompts "
        "that produce original, high-quality images relevant to the article topic. "
        "Each image should be visually distinct from the others and support the surrounding content. "
        "Always write descriptive, keyword-rich alt text for SEO."
    )

    user = f"""Article keyword: {keyword}
Article title: {title}

Article sections (H2 headings):
{headings_str}

Article preview (first 600 words):
{article_preview}

Competitor images on this topic use these visuals (for reference — do NOT copy, use as context):
{competitor_alts_str}

Generate exactly {n_images} image prompts for this article.

Rules:
- Image 1: type=photo — the hero/feature image. Cinematic, high-quality, no text overlay. 
  Should represent the article topic visually. Photorealistic, professional.
- Images 2 to {n_images - 1 if use_callout else n_images}: type=photo — in-article images, 
  each tied to a different H2 section. No text overlay. Photorealistic or conceptual illustration style.{callout_instruction}

Return ONLY valid JSON — an array of objects, one per image:
[
  {{
    "type": "photo",
    "prompt": "detailed Flux image generation prompt here",
    "alt": "SEO-optimised alt text for this image",
    "caption": "short descriptive caption (1 sentence)",
    "position": "feature"
  }},
  ...
]

For position: first image = "feature", remaining = "section".
No explanation, no markdown fences — raw JSON array only."""

    raw = _call_claude(system, user, model)

    # Strip markdown fences if Claude added them
    raw = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("```").strip()

    try:
        import json
        prompts = json.loads(raw)
        if isinstance(prompts, list):
            return prompts
    except Exception as exc:
        log.warning("Failed to parse image prompts JSON: %s — raw: %.200s", exc, raw)

    return []


# ── Flux (Replicate) ──────────────────────────────────────────────────────────

def _flux_generate(prompt: str, width: int, height: int, replicate_model: str) -> Optional[str]:
    """
    Submit a Flux generation job to Replicate and poll for the result URL.
    Returns the image URL or None on failure.
    """
    api_key = os.getenv("REPLICATE_API_KEY")
    if not api_key:
        raise RuntimeError("REPLICATE_API_KEY is not set")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Prefer": "wait",  # synchronous mode — waits up to 60s
    }

    payload = {
        "input": {
            "prompt": prompt,
            "aspect_ratio": "custom",
            "width": width,
            "height": height,
            "output_format": "webp",
            "output_quality": 90,
        }
    }

    url = f"https://api.replicate.com/v1/models/{replicate_model}/predictions"
    resp = requests.post(url, headers=headers, json=payload, timeout=120)

    if resp.status_code == 201 or resp.status_code == 200:
        data = resp.json()
        # Prefer=wait returns completed prediction immediately
        output = data.get("output")
        if output:
            return output if isinstance(output, str) else output[0]
        # Fallback: poll if not complete
        pred_url = data.get("urls", {}).get("get") or data.get("url")
        if pred_url:
            return _replicate_poll(pred_url, headers)
    else:
        log.warning("Replicate Flux error %d: %.200s", resp.status_code, resp.text)

    return None


def _replicate_poll(prediction_url: str, headers: dict, max_wait: int = 120) -> Optional[str]:
    """Poll a Replicate prediction URL until it completes."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        r = requests.get(prediction_url, headers=headers, timeout=30)
        if not r.ok:
            break
        data = r.json()
        status = data.get("status")
        if status == "succeeded":
            output = data.get("output")
            return output if isinstance(output, str) else (output[0] if output else None)
        if status in ("failed", "canceled"):
            log.warning("Replicate prediction %s: %s", status, data.get("error"))
            return None
        time.sleep(3)
    log.warning("Replicate prediction timed out after %ds", max_wait)
    return None


# ── Ideogram ──────────────────────────────────────────────────────────────────

def _ideogram_generate(prompt: str, width: int, height: int) -> Optional[str]:
    """
    Generate an image with embedded text via Ideogram v2 API.
    Returns image URL or None on failure.
    """
    api_key = os.getenv("IDEOGRAM_API_KEY")
    if not api_key:
        log.warning("IDEOGRAM_API_KEY not set — skipping callout image")
        return None

    headers = {
        "Api-Key": api_key,
        "Content-Type": "application/json",
    }

    # Ideogram uses aspect ratio strings
    aspect = "ASPECT_16_9" if width > height else "ASPECT_1_1"

    payload = {
        "image_request": {
            "prompt": prompt,
            "model": "V_2",
            "magic_prompt_option": "OFF",
            "aspect_ratio": aspect,
            "style_type": "DESIGN",
        }
    }

    resp = requests.post(
        "https://api.ideogram.ai/generate",
        headers=headers,
        json=payload,
        timeout=60,
    )

    if resp.ok:
        data = resp.json()
        images = data.get("data", [])
        if images:
            return images[0].get("url")
    else:
        log.warning("Ideogram error %d: %.200s", resp.status_code, resp.text)

    return None


# ── Ghost image upload ────────────────────────────────────────────────────────

def _upload_to_ghost(image_url: str, filename: str, ghost_url: str, ghost_key: str) -> Optional[str]:
    """
    Download an image from image_url and upload it to Ghost Media API.
    Returns the Ghost CDN URL or None on failure.
    """
    # Import JWT helper from publisher
    sys.path.insert(0, str(Path(__file__).parent))
    from publisher_runner import _ghost_jwt  # type: ignore

    try:
        # Download the generated image
        img_resp = requests.get(image_url, timeout=60)
        img_resp.raise_for_status()
        image_data = img_resp.content

        # Determine content type
        content_type = img_resp.headers.get("Content-Type", "image/webp")
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".webp"
        if ext == ".jpe":
            ext = ".jpg"
        safe_filename = filename + ext

        token = _ghost_jwt(ghost_key)
        upload_url = f"{ghost_url.rstrip('/')}/ghost/api/admin/images/upload/"
        headers = {
            "Authorization": f"Ghost {token}",
            "Accept-Version": "v5.0",
        }

        files = {
            "file": (safe_filename, io.BytesIO(image_data), content_type),
            "purpose": (None, "image"),
        }

        up_resp = requests.post(upload_url, headers=headers, files=files, timeout=60)

        if up_resp.ok:
            data = up_resp.json()
            return data.get("images", [{}])[0].get("url") or data.get("url")
        else:
            log.warning(
                "Ghost image upload failed %d: %.200s", up_resp.status_code, up_resp.text
            )
    except Exception as exc:
        log.warning("Ghost image upload error: %s", exc)

    return None


# ── Public API ────────────────────────────────────────────────────────────────

def run(
    keyword: str,
    article: str,
    title: str,
    slug: str,
    research: Dict,
    config: Dict,
) -> List[Dict]:
    """
    Generate images for an article and upload them to Ghost.

    Returns a list of image dicts:
      [{url, alt, caption, position, type}, ...]

    position "feature" = hero image (set as Ghost feature_image)
    position "section" = in-article image (injected at H2 boundaries)

    On any failure, returns an empty list — never raises.
    """
    if not config.get("images_enabled", False):
        return []

    log.info("  → Step 3b/5: Generating images…")

    model = config.get("anthropic_model", "claude-sonnet-4-5")
    n_images = max(1, config.get("images_per_article", 3))
    use_callout = config.get("use_ideogram_for_callouts", True) and bool(os.getenv("IDEOGRAM_API_KEY"))
    width = config.get("image_width", 1344)
    height = config.get("image_height", 768)
    replicate_model = config.get("replicate_model", "black-forest-labs/flux-1.1-pro")

    ghost_url = os.getenv("GHOST_URL", "")
    ghost_key = os.getenv("GHOST_ADMIN_API_KEY", "")
    ghost_configured = bool(ghost_url and ghost_key)

    # Extract competitor image alt texts for prompt context
    competitor_data = research.get("competitor_data", [])
    competitor_alts = _extract_competitor_image_alts(competitor_data)

    # ── Generate prompts via Claude ───────────────────────────────────────────
    try:
        prompts_list = _generate_image_prompts(
            keyword=keyword,
            article=article,
            title=title,
            competitor_alts=competitor_alts,
            n_images=n_images,
            use_callout=use_callout,
            model=model,
        )
    except Exception as exc:
        log.error("     Image prompt generation failed: %s", exc)
        return []

    if not prompts_list:
        log.warning("     No image prompts generated — skipping images")
        return []

    log.info("     Generated %d image prompts", len(prompts_list))

    # ── Generate + upload each image ──────────────────────────────────────────
    results = []
    for i, img_spec in enumerate(prompts_list):
        img_type = img_spec.get("type", "photo")
        prompt = img_spec.get("prompt", "")
        alt = img_spec.get("alt", keyword)
        caption = img_spec.get("caption", "")
        position = img_spec.get("position", "section")

        if not prompt:
            continue

        log.info(
            "     Generating image %d/%d (%s, %s)…",
            i + 1, len(prompts_list), img_type, position,
        )

        # Generate image
        image_url = None
        try:
            if img_type == "callout" and use_callout:
                image_url = _ideogram_generate(prompt, width, height)
            else:
                if not os.getenv("REPLICATE_API_KEY"):
                    log.warning("     REPLICATE_API_KEY not set — skipping image generation")
                    break
                image_url = _flux_generate(prompt, width, height, replicate_model)
        except Exception as exc:
            log.warning("     Image generation failed for image %d: %s", i + 1, exc)
            continue

        if not image_url:
            log.warning("     Image %d returned no URL — skipping", i + 1)
            continue

        # Upload to Ghost (if configured), otherwise use the temporary generation URL
        ghost_cdn_url = image_url
        if ghost_configured:
            filename = f"{slug}-img-{i + 1}"
            uploaded = _upload_to_ghost(image_url, filename, ghost_url, ghost_key)
            if uploaded:
                ghost_cdn_url = uploaded
                log.info("     Uploaded image %d → %s", i + 1, ghost_cdn_url)
            else:
                log.warning("     Ghost upload failed for image %d — using generation URL", i + 1)
        else:
            log.warning("     Ghost not configured — using temporary image URL for image %d", i + 1)

        results.append({
            "url": ghost_cdn_url,
            "alt": alt,
            "caption": caption,
            "position": position,
            "type": img_type,
            "prompt": prompt,
        })

    log.info("     Images ready: %d", len(results))
    return results

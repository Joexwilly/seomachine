"""
Writer Runner

Wraps the article writing workflow:
1. Loads brand context + write command + research brief
2. Calls Claude with full context to produce a complete article
3. Saves draft to drafts/ directory as markdown

Returns a dict with the article content and metadata.
"""

import os
import sys
import re
import time
import logging
from pathlib import Path
from typing import Dict, Optional, Any

_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT))

import anthropic

log = logging.getLogger(__name__)

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _call_claude(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 8000,
    model: str = "claude-sonnet-4-20250514",
) -> str:
    client = _get_client()
    for attempt in range(2):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return response.content[0].text
        except Exception as exc:
            if attempt == 0:
                log.warning("Claude API call failed (%s), retrying in 10s…", exc)
                time.sleep(10)
            else:
                raise


def run(
    keyword: str,
    research: Dict,
    config: Dict,
    prompts: Any,
) -> Dict:
    """
    Write a full article for the given keyword using the research brief.

    Returns dict with keys: article, title, word_count, draft_path
    """
    log.info("  → Step 2/5: Writing article…")

    max_tokens = config.get("max_tokens_writing", 8000)
    model = config.get("anthropic_model", "claude-sonnet-4-20250514")

    # ── Load prompts ──────────────────────────────────────────────────────────
    context = prompts.load_context()
    write_command = prompts.load_command("write")

    # ── Build user prompt ─────────────────────────────────────────────────────
    user_prompt = f"""
KEYWORD / TOPIC: {keyword}

RESEARCH BRIEF:
{research.get('content_brief', '')}

TARGET WORD COUNT: {research.get('target_word_count', 2000)} words

COMPETITOR AVERAGE WORD COUNT: {research.get('competitor_avg_word_count', 0)} words

COMPETITOR HEADINGS (for reference — do NOT copy, use for inspiration):
{chr(10).join(f'- {h}' for h in research.get('competitor_headings', [])[:30])}

RELATED KEYWORDS TO NATURALLY INCLUDE:
{chr(10).join(f'- {k}' for k in research.get('related_keywords', [])[:15])}

PEOPLE ALSO ASK (address these in the article):
{chr(10).join(f'- {q}' for q in research.get('people_also_ask', [])[:10])}

SEARCH INTENT: {research.get('search_intent', 'informational')}

WRITE COMMAND INSTRUCTIONS:
{write_command}

---

Write the complete article now. Requirements:
- Start with the H1 title
- Minimum {research.get('target_word_count', 2000)} words
- At least 4 H2 sections
- Use the brand voice from the context files
- Include a meta title and meta description at the end in this format:
  **Meta Title**: <title here>
  **Meta Description**: <description here>
- Natural keyword placement (1-2% density)
- Do NOT use: em-dashes, "in conclusion", "delve into", "it's worth noting",
  "leverage", "utilize", "in today's world", "game-changer", "transformative"
- Take clear positions. Sound human, not robotic.
"""

    system_prompt = f"""You are an expert content writer and SEO specialist.
Write high-quality, human-sounding articles that rank well and convert readers.

{context}"""

    # ── Call Claude ───────────────────────────────────────────────────────────
    log.info("     Calling Claude to write article…")
    article = _call_claude(system_prompt, user_prompt, max_tokens=max_tokens, model=model)

    # ── Extract metadata from article ─────────────────────────────────────────
    title = _extract_h1(article) or keyword.title()
    meta_title = _extract_field(article, "Meta Title") or title[:60]
    meta_description = _extract_field(article, "Meta Description") or ""
    word_count = len(article.split())

    log.info("     Generated: %d words", word_count)

    # ── Save draft ────────────────────────────────────────────────────────────
    slug = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")
    draft_path = _save_draft(slug, article)

    return {
        "keyword": keyword,
        "article": article,
        "title": title,
        "meta_title": meta_title,
        "meta_description": meta_description,
        "word_count": word_count,
        "slug": slug,
        "draft_path": str(draft_path),
    }


def _extract_h1(text: str) -> str:
    match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _extract_field(text: str, field_name: str) -> str:
    pattern = rf"\*\*{field_name}\*\*:\s*(.+?)(?:\n|$)"
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _save_draft(slug: str, content: str) -> Path:
    drafts_dir = _ROOT / "drafts"
    drafts_dir.mkdir(exist_ok=True)
    path = drafts_dir / f"{slug}.md"
    path.write_text(content, encoding="utf-8")
    log.debug("     Saved draft: %s", path)
    return path

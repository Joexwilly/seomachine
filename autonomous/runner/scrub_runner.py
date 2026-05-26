"""
Scrub Runner

Wraps the editor/humanization workflow:
1. Loads the editor agent system prompt
2. Calls Claude to humanize the article — removing AI patterns, robotic phrasing,
   em-dashes, filler phrases, and generic transitions
3. Also applies a regex scrub for known AI tell-tale patterns
4. Updates the draft file with the scrubbed content
5. Returns the scrubbed article

This is the final quality gate before publishing.
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

# ── Known AI patterns to remove via regex (fast pre-pass) ────────────────────
_AI_PATTERNS = [
    # em-dash and double-dash variants
    (r" — ", " - "),
    (r"—", " - "),
    (r"–", " - "),          # en dash (U+2013)
    (r" -- ", " - "),      # double hyphen with spaces (em dash substitute)
    (r"(?<!-)-{2}(?!-)", " - "),  # exactly -- not part of --- (horizontal rule)
    # filler openers
    (r"\bIn today's (fast-paced |ever-evolving |digital )?world[,.]?\s*", ""),
    (r"\bIn conclusion[,.]?\s*", ""),
    (r"\bTo (sum|wrap) (up|things up)[,.]?\s*", ""),
    (r"\bIt's (worth|important to) (noting|mention|note) (that )?", ""),
    (r"\bIt is (worth|important to) (noting|mention|note) (that )?", ""),
    (r"\bDelve into\b", "explore"),
    (r"\bLeverage\b", "use"),
    (r"\bUtilize\b", "use"),
    (r"\bGame[- ]changer\b", "major improvement"),
    (r"\bTransformative\b", "significant"),
    (r"\bGroundbreaking\b", "notable"),
    (r"\bRevolutionary\b", "new"),
    (r"\bSeamlessly\b", "smoothly"),
    (r"\bRobust\b", "strong"),
    (r"\bComprehensive guide\b", "guide"),
    (r"\bUltimate guide\b", "guide"),
    (r"\bIn this (article|guide|post)[,.]?\s*(we will|we'll|I will|I'll)?\s*(explore|dive into|cover|discuss|look at)\b", ""),
    (r"\bThis article (will )?(explore|cover|discuss|look at|dive into)\b", ""),
    # Doubled spaces from above removals
    (r"  +", " "),
]


def _regex_scrub(text: str) -> str:
    """Apply fast regex scrub of known AI patterns."""
    for pattern, replacement in _AI_PATTERNS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    # Clean up blank lines created by removals
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _call_claude(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 8000,
    model: str = "claude-sonnet-4-20250514",
) -> str:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = anthropic.Anthropic(api_key=api_key)

    for attempt in range(2):
        try:
            response = _client.messages.create(
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
    writer_result: Dict,
    optimizer_result: Dict,
    config: Dict,
    prompts: Any,
) -> Dict:
    """
    Run the humanization/scrub pass on the article.

    Returns dict with: article (scrubbed), word_count, draft_path
    """
    log.info("  → Step 4/5: Scrubbing AI patterns…")

    article = writer_result.get("article", "")
    model = config.get("anthropic_model", "claude-sonnet-4-20250514")
    max_tokens = config.get("max_tokens_writing", 8000)

    # ── Fast regex pre-pass ───────────────────────────────────────────────────
    article = _regex_scrub(article)

    # ── Claude humanization pass ──────────────────────────────────────────────
    try:
        editor_prompt = prompts.load_agent("editor")
        scrub_command = prompts.load_command("scrub")

        user_prompt = f"""
KEYWORD: {keyword}

SCRUB INSTRUCTIONS:
{scrub_command}

ARTICLE TO HUMANIZE:
{article}

Return ONLY the improved article. Do not add commentary or explanations.
Preserve all headings, links, and markdown formatting.
Preserve the Meta Title and Meta Description lines at the end if present.
"""
        scrubbed = _call_claude(editor_prompt, user_prompt, max_tokens=max_tokens, model=model)
        article = _regex_scrub(scrubbed)  # clean any dashes Claude re-introduced
        log.info("     AI scrub pass complete")
    except Exception as exc:
        log.warning("     Claude scrub call failed (%s) — using regex-scrubbed version", exc)

    # ── Save updated draft ────────────────────────────────────────────────────
    draft_path = writer_result.get("draft_path", "")
    if draft_path:
        try:
            Path(draft_path).write_text(article, encoding="utf-8")
            log.debug("     Updated draft with scrubbed content: %s", draft_path)
        except Exception as exc:
            log.warning("     Could not update draft file: %s", exc)

    word_count = len(article.split())

    return {
        "keyword": keyword,
        "article": article,
        "word_count": word_count,
        "draft_path": draft_path,
        "meta_title": optimizer_result.get("meta_title", writer_result.get("meta_title", "")),
        "meta_description": optimizer_result.get("meta_description", writer_result.get("meta_description", "")),
        "title": writer_result.get("title", keyword.title()),
        "slug": writer_result.get("slug", ""),
    }

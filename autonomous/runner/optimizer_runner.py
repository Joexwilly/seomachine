"""
Optimizer Runner

Wraps the SEO optimization + scoring workflow:
1. Runs Python scoring modules (no AI needed):
   - KeywordAnalyzer → density, distribution, LSI
   - SEOQualityRater → 0-100 score
   - ReadabilityScorer → Flesch score, grade level
2. Calls Claude with seo-optimizer agent to get specific fix recommendations
3. Calls Claude with meta-creator agent to generate meta title + description options
4. Saves a score report JSON alongside the draft
5. Optionally updates the draft file with improved meta tags

Returns a dict with scores, recommendations, and final meta tags.
"""

import os
import sys
import re
import json
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any

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
    max_tokens: int = 4000,
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


# ── Python scoring ────────────────────────────────────────────────────────────

def _run_keyword_analysis(article: str, keyword: str, related: List[str]) -> Dict:
    try:
        from data_sources.modules.keyword_analyzer import KeywordAnalyzer  # type: ignore
        analyzer = KeywordAnalyzer()
        return analyzer.analyze(article, keyword, secondary_keywords=related[:5])
    except Exception as exc:
        log.warning("     KeywordAnalyzer failed: %s", exc)
        # Fallback: simple density calc
        words = article.lower().split()
        kw_words = keyword.lower().split()
        count = sum(
            1 for i in range(len(words) - len(kw_words) + 1)
            if words[i : i + len(kw_words)] == kw_words
        )
        density = (count / max(len(words), 1)) * 100
        return {"primary_keyword": {"density": round(density, 2), "count": count}, "recommendations": []}


def _run_seo_quality(
    article: str,
    meta_title: str,
    meta_description: str,
    keyword: str,
    related: List[str],
    internal_links: int = 0,
) -> Dict:
    try:
        from data_sources.modules.seo_quality_rater import SEOQualityRater  # type: ignore
        rater = SEOQualityRater()
        kw_data = _run_keyword_analysis(article, keyword, related)
        density = (
            kw_data.get("primary_keyword", {}).get("density")
            or kw_data.get("density")
            or None
        )
        return rater.rate(
            content=article,
            meta_title=meta_title or None,
            meta_description=meta_description or None,
            primary_keyword=keyword,
            secondary_keywords=related[:5],
            keyword_density=density,
            internal_link_count=internal_links,
        )
    except Exception as exc:
        log.warning("     SEOQualityRater failed: %s", exc)
        return {"overall_score": 0, "grade": "F", "recommendations": []}


def _run_readability(article: str) -> Dict:
    try:
        from data_sources.modules.readability_scorer import ReadabilityScorer  # type: ignore
        scorer = ReadabilityScorer()
        return scorer.analyze(article)
    except Exception as exc:
        log.warning("     ReadabilityScorer failed: %s", exc)
        return {"flesch_reading_ease": 0, "overall_score": 0}


# ── Claude optimization ───────────────────────────────────────────────────────

def _get_seo_recommendations(
    article: str,
    seo_data: Dict,
    keyword_data: Dict,
    readability_data: Dict,
    keyword: str,
    prompts: Any,
    model: str,
) -> str:
    try:
        agent_prompt = prompts.load_agent("seo-optimizer")
        context = prompts.load_context()
        user_prompt = f"""
KEYWORD: {keyword}

SEO SCORE: {seo_data.get('overall_score', 0)}/100 (Grade: {seo_data.get('grade', 'N/A')})

KEYWORD DENSITY: {keyword_data.get('primary_keyword', {}).get('density', 'N/A')}%

READABILITY: Flesch {readability_data.get('flesch_reading_ease', 'N/A')} | Grade: {readability_data.get('grade', 'N/A')}

WORD COUNT: {len(article.split())}

CURRENT SEO ISSUES FOUND:
{chr(10).join(f'- {r}' for r in seo_data.get('recommendations', [])[:15])}

ARTICLE (first 3000 chars for context):
{article[:3000]}

Provide specific, actionable SEO recommendations to improve the score.
Focus on the most impactful changes. Be concise and direct.
"""
        system_prompt = f"{agent_prompt}\n\n=== CONTEXT ===\n{context}"
        return _call_claude(system_prompt, user_prompt, max_tokens=2000, model=model)
    except Exception as exc:
        log.warning("     SEO recommendations call failed: %s", exc)
        return ""


def _generate_meta_tags(article: str, keyword: str, existing_meta_title: str, existing_meta_desc: str, prompts: Any, model: str) -> Dict:
    """Use meta-creator agent to generate improved meta title + description."""
    try:
        agent_prompt = prompts.load_agent("meta-creator")
        user_prompt = f"""
KEYWORD: {keyword}
EXISTING META TITLE: {existing_meta_title}
EXISTING META DESCRIPTION: {existing_meta_desc}

ARTICLE (first 2000 chars):
{article[:2000]}

Generate an optimized meta title (50-60 chars) and meta description (150-160 chars).
Format your response exactly as:
META TITLE: <title here>
META DESCRIPTION: <description here>
"""
        result = _call_claude(agent_prompt, user_prompt, max_tokens=500, model=model)

        title_match = re.search(r"META TITLE:\s*(.+?)(?:\n|$)", result, re.IGNORECASE)
        desc_match = re.search(r"META DESCRIPTION:\s*(.+?)(?:\n|$)", result, re.IGNORECASE)

        return {
            "meta_title": title_match.group(1).strip() if title_match else existing_meta_title,
            "meta_description": desc_match.group(1).strip() if desc_match else existing_meta_desc,
        }
    except Exception as exc:
        log.warning("     Meta creation call failed: %s", exc)
        return {"meta_title": existing_meta_title, "meta_description": existing_meta_desc}


# ── Score display helpers ─────────────────────────────────────────────────────

def _format_score_block(seo_data: Dict, word_count: int, readability: Dict) -> str:
    score = seo_data.get("overall_score", 0)
    grade = seo_data.get("grade", "?")
    flesch = readability.get("readability_metrics", {}).get("flesch_reading_ease", 0)
    lines = [
        "     ┌─────────────────────────────────────┐",
        f"     │  SEO SCORE: {score}/100  Grade: {grade:<5}       │",
        f"     │  Words: {word_count:<6} | Readability: {flesch:.0f}  │",
        "     └─────────────────────────────────────┘",
    ]
    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────────────

def run(
    keyword: str,
    writer_result: Dict,
    research: Dict,
    config: Dict,
    prompts: Any,
) -> Dict:
    """
    Run SEO scoring + optimization on the written article.

    Returns dict with: seo_score, grade, readability, keyword_data,
    recommendations, meta_title, meta_description, score_report_path
    """
    log.info("  → Step 3/5: Scoring…")

    article = writer_result.get("article", "")
    meta_title = writer_result.get("meta_title", "")
    meta_description = writer_result.get("meta_description", "")
    word_count = writer_result.get("word_count", len(article.split()))
    related_keywords = research.get("related_keywords", [])
    model = config.get("anthropic_model", "claude-sonnet-4-20250514")

    # ── Run Python scorers ────────────────────────────────────────────────────
    keyword_data = _run_keyword_analysis(article, keyword, related_keywords)
    seo_data = _run_seo_quality(
        article, meta_title, meta_description, keyword, related_keywords
    )
    readability_data = _run_readability(article)

    score = seo_data.get("overall_score", 0)
    grade = seo_data.get("grade", "?")

    # Print score block to console via logger
    print(_format_score_block(seo_data, word_count, readability_data))

    # Print recommendations
    for rec in seo_data.get("recommendations", [])[:8]:
        marker = "⚠ " if any(w in rec.lower() for w in ["missing", "too", "low", "high", "not"]) else "✓ "
        log.info("     %s%s", marker, rec)

    # ── Readability gate — rewrite if Flesch < 40 ────────────────────────────
    flesch = readability_data.get("readability_metrics", {}).get("flesch_reading_ease", 0)
    min_flesch = config.get("min_flesch_score", 40)
    if flesch > 0 and flesch < min_flesch:
        log.warning(
            "     ⚠ Readability too low (Flesch %.0f < %d) — requesting simplification pass…",
            flesch, min_flesch,
        )
        simplify_prompt = f"""The following article scored {flesch:.0f} on the Flesch Reading Ease scale (target: {min_flesch}+).

Rewrite it so it's easier to read:
- Shorter sentences (aim for 15–20 words average)
- Replace jargon with plain English
- Break long paragraphs into 2–3 sentence chunks
- Use active voice

Preserve all headings, keyword placement, and the Meta Title / Meta Description lines.
Return ONLY the rewritten article.

ARTICLE:
{article}"""
        try:
            simplified = _call_claude(
                "You are a plain-English editor who simplifies dense writing without dumbing it down.",
                simplify_prompt,
                max_tokens=writer_result.get("word_count", 3000) * 2,
                model=model,
            )
            if simplified:
                article = simplified
                writer_result["article"] = article  # propagate so downstream steps see it
                log.info("     Simplification pass complete")
        except Exception as exc:
            log.warning("     Simplification pass failed (%s) — continuing with original", exc)

    # ── Claude SEO recommendations ────────────────────────────────────────────
    seo_recs = _get_seo_recommendations(
        article, seo_data, keyword_data, readability_data, keyword, prompts, model
    )

    # ── Generate improved meta tags ───────────────────────────────────────────
    meta = _generate_meta_tags(
        article, keyword, meta_title, meta_description, prompts, model
    )

    # ── Build result ──────────────────────────────────────────────────────────
    result = {
        "keyword": keyword,
        "seo_score": score,
        "grade": grade,
        "word_count": word_count,
        "readability": readability_data,
        "keyword_data": keyword_data,
        "seo_issues": seo_data.get("recommendations", []),
        "seo_recommendations": seo_recs,
        "meta_title": meta.get("meta_title", meta_title),
        "meta_description": meta.get("meta_description", meta_description),
    }

    # ── Save score report ─────────────────────────────────────────────────────
    if config.get("save_score_reports", True):
        slug = writer_result.get("slug") or re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")
        report_path = _save_score_report(slug, result)
        result["score_report_path"] = str(report_path)

    return result


def _save_score_report(slug: str, data: Dict) -> Path:
    drafts_dir = _ROOT / "drafts"
    drafts_dir.mkdir(exist_ok=True)
    path = drafts_dir / f"{slug}-score-report.json"
    try:
        # Make data JSON-serialisable (remove non-serialisable objects)
        safe = json.loads(json.dumps(data, default=str))
        path.write_text(json.dumps(safe, indent=2, ensure_ascii=False), encoding="utf-8")
        log.debug("     Saved score report: %s", path)
    except Exception as exc:
        log.warning("     Could not save score report: %s", exc)
    return path

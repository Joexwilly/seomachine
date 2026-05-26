"""
Research Runner

Wraps the research workflow:
1. Calls DataForSEO for keyword metrics, SERP URLs, related keywords, PAA
2. Falls back to free SERP scraping (trafilatura + beautifulsoup4) if no creds
3. Scrapes top competitor pages to extract headings and word counts
4. Calls Claude with the content-analyzer system prompt + research command
   to produce a structured content brief
5. Saves the brief as JSON + markdown to research/

Returns a dict with all research data for downstream runners.
"""

import os
import sys
import json
import time
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests
from bs4 import BeautifulSoup

# Allow imports from project root
_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT))

import anthropic

log = logging.getLogger(__name__)

# ── Anthropic client (lazy-init) ─────────────────────────────────────────────

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
    model: str = "claude-sonnet-4-5",
) -> str:
    """Call Claude with one retry on failure."""
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


# ── DataForSEO helpers ────────────────────────────────────────────────────────

def _dataforseo_available() -> bool:
    return bool(os.getenv("DATAFORSEO_LOGIN") and os.getenv("DATAFORSEO_PASSWORD"))


def _dataforseo_research(keyword: str, location_code: int, language_code: str) -> Dict:
    """Fetch keyword metrics and SERP data from DataForSEO."""
    try:
        from data_sources.modules.dataforseo import DataForSEO  # type: ignore

        dfs = DataForSEO()
        result: Dict[str, Any] = {
            "search_volume": 0,
            "keyword_difficulty": 0,
            "cpc": 0.0,
            "serp_urls": [],
            "related_keywords": [],
            "people_also_ask": [],
        }

        # SERP data — get_serp_data also returns search_volume and cpc in one call
        try:
            serp = dfs.get_serp_data(keyword, location_code=location_code, limit=10)
            urls = []
            for item in serp.get("organic_results", [])[:10]:
                if isinstance(item, dict) and item.get("url"):
                    urls.append(item["url"])
            result["serp_urls"] = urls
            result["search_volume"] = serp.get("search_volume") or 0
            result["cpc"] = serp.get("cpc") or 0.0
        except Exception as e:
            log.warning("DataForSEO SERP call failed: %s", e)

        # Related keywords
        try:
            related = dfs.get_keyword_ideas(keyword, location_code=location_code, limit=20)
            result["related_keywords"] = [
                r.get("keyword", "") for r in (related or [])[:15] if r.get("keyword")
            ]
        except Exception as e:
            log.warning("DataForSEO related keywords call failed: %s", e)

        # People Also Ask (question queries)
        try:
            questions = dfs.get_questions(keyword, location_code=location_code, limit=20)
            result["people_also_ask"] = [
                q.get("keyword", "") for q in (questions or [])[:10] if q.get("keyword")
            ]
        except Exception as e:
            log.warning("DataForSEO questions call failed: %s", e)

        return result

    except Exception as exc:
        log.error("DataForSEO research failed entirely: %s", exc)
        return {
            "search_volume": 0,
            "keyword_difficulty": 0,
            "cpc": 0.0,
            "serp_urls": [],
            "related_keywords": [],
            "people_also_ask": [],
        }


# ── Free scraping helpers ─────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def _google_serp_urls(keyword: str, num: int = 10) -> List[str]:
    """Fetch top Google SERP URLs via scraping (fallback)."""
    try:
        query = keyword.replace(" ", "+")
        url = f"https://www.google.com/search?q={query}&num={num}"
        resp = requests.get(url, headers=_HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        urls = []
        for a in soup.select("a[href]"):
            href = a["href"]
            if href.startswith("/url?q="):
                actual = href.split("/url?q=")[1].split("&")[0]
                if actual.startswith("http") and "google" not in actual:
                    urls.append(actual)
                    if len(urls) >= num:
                        break
        return urls
    except Exception as exc:
        log.warning("Free SERP scraping failed: %s", exc)
        return []


def _scrape_page_headings_and_wordcount(url: str) -> Dict:
    """Scrape a URL and return headings list + word count."""
    try:
        import trafilatura  # type: ignore

        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return {"headings": [], "word_count": 0}

        # Extract plain text for word count
        text = trafilatura.extract(downloaded) or ""
        word_count = len(text.split())

        # Extract headings from raw HTML
        soup = BeautifulSoup(downloaded, "html.parser")
        headings = []
        for tag in soup.find_all(["h1", "h2", "h3"]):
            text_content = tag.get_text(strip=True)
            if text_content:
                headings.append(f"{tag.name.upper()}: {text_content}")

        # Extract image alt texts for image generation context
        image_alts = []
        for img in soup.find_all("img", alt=True):
            alt = img["alt"].strip()
            if alt and len(alt) > 5:
                image_alts.append(alt)

        return {"headings": headings[:20], "word_count": word_count, "url": url, "image_alts": image_alts[:15]}
    except Exception as exc:
        log.debug("Failed to scrape %s: %s", url, exc)
        return {"headings": [], "word_count": 0, "url": url}


def _scrape_competitors(urls: List[str], max_pages: int = 5) -> List[Dict]:
    """Scrape up to max_pages competitor URLs."""
    results = []
    for url in urls[:max_pages]:
        data = _scrape_page_headings_and_wordcount(url)
        if data.get("word_count", 0) > 200:
            results.append(data)
        time.sleep(0.5)  # polite crawl
    return results


# ── Intent detection ──────────────────────────────────────────────────────────

def _detect_intent(keyword: str) -> str:
    """Simple heuristic search intent detection."""
    kw = keyword.lower()
    if any(w in kw for w in ["buy", "price", "cost", "cheap", "best", "vs", "review", "top"]):
        return "commercial"
    if any(w in kw for w in ["how to", "what is", "why", "guide", "tutorial", "learn"]):
        return "informational"
    if any(w in kw for w in ["near me", "login", "sign in", "download", "contact"]):
        return "navigational"
    return "informational"


# ── Claude brief generation ───────────────────────────────────────────────────

def _generate_brief(
    keyword: str,
    serp_data: Dict,
    competitor_data: List[Dict],
    context: str,
    agent_prompt: str,
    command_prompt: str,
    model: str = "claude-sonnet-4-5",
) -> str:
    """Call Claude to generate a structured content brief."""
    all_headings = []
    for comp in competitor_data:
        all_headings.extend(comp.get("headings", []))

    word_counts = [c.get("word_count", 0) for c in competitor_data if c.get("word_count", 0) > 0]
    avg_wc = int(sum(word_counts) / len(word_counts)) if word_counts else 2000

    user_prompt = f"""
Keyword: {keyword}

SERP DATA:
- Search Volume: {serp_data.get('search_volume', 'Unknown')}/mo
- Keyword Difficulty: {serp_data.get('keyword_difficulty', 'Unknown')}
- Search Intent: {serp_data.get('search_intent', _detect_intent(keyword))}
- CPC: ${serp_data.get('cpc', 0):.2f}

TOP COMPETITOR URLs:
{chr(10).join(f'- {u}' for u in serp_data.get('serp_urls', [])[:10])}

COMPETITOR HEADINGS (from top pages):
{chr(10).join(f'- {h}' for h in all_headings[:40])}

COMPETITOR AVERAGE WORD COUNT: {avg_wc} words

RELATED KEYWORDS:
{chr(10).join(f'- {k}' for k in serp_data.get('related_keywords', [])[:15])}

PEOPLE ALSO ASK:
{chr(10).join(f'- {q}' for q in serp_data.get('people_also_ask', [])[:10])}

RESEARCH COMMAND INSTRUCTIONS:
{command_prompt}

Based on the above data, generate a comprehensive content brief for this keyword.
Include: recommended H1, suggested H2 structure, key points to cover,
target word count, primary + secondary keywords, and content angle.
Format the brief clearly with sections.
"""

    system_prompt = f"{agent_prompt}\n\n=== BRAND CONTEXT ===\n{context}"
    return _call_claude(system_prompt, user_prompt, max_tokens=4000, model=model)


# ── Public API ────────────────────────────────────────────────────────────────

def run(
    keyword: str,
    config: Dict,
    prompts: Any,  # prompts module (has load_command, load_agent, load_context)
) -> Dict:
    """
    Run the full research pipeline for a keyword.

    Returns a dict with all research data. Also saves brief JSON + markdown
    to research/ directory.
    """
    log.info("  → Step 1/5: Research")

    model = config.get("anthropic_model", "claude-sonnet-4-5")
    location_code = config.get("dataforseo_location_code", 2840)
    language_code = config.get("dataforseo_language_code", "en")
    fallback = config.get("dataforseo_fallback_to_scraping", True)

    # ── Fetch SERP + keyword data ─────────────────────────────────────────────
    if _dataforseo_available():
        log.info("     Using DataForSEO for keyword data")
        serp_data = _dataforseo_research(keyword, location_code, language_code)
    elif fallback:
        log.info("     DataForSEO not configured — using free SERP scraping")
        serp_data = {
            "search_volume": 0,
            "keyword_difficulty": 0,
            "cpc": 0.0,
            "serp_urls": _google_serp_urls(keyword),
            "related_keywords": [],
            "people_also_ask": [],
        }
    else:
        log.warning("     No data source available — proceeding without SERP data")
        serp_data = {
            "search_volume": 0,
            "keyword_difficulty": 0,
            "cpc": 0.0,
            "serp_urls": [],
            "related_keywords": [],
            "people_also_ask": [],
        }

    serp_data["search_intent"] = _detect_intent(keyword)

    # ── Scrape competitor pages ───────────────────────────────────────────────
    competitor_data: List[Dict] = []
    if serp_data.get("serp_urls"):
        log.info("     Scraping %d competitor URLs…", min(5, len(serp_data["serp_urls"])))
        competitor_data = _scrape_competitors(serp_data["serp_urls"], max_pages=5)

    # ── Calculate target word count ───────────────────────────────────────────
    word_counts = [c.get("word_count", 0) for c in competitor_data if c.get("word_count", 0) > 0]
    avg_wc = int(sum(word_counts) / len(word_counts)) if word_counts else 2000
    multiplier = config.get("target_word_count_multiplier", 1.15)
    target_wc = max(int(avg_wc * multiplier), config.get("min_word_count", 1800))

    log.info(
        "     Volume: %s/mo | Difficulty: %s | Intent: %s",
        serp_data.get("search_volume", "?"),
        serp_data.get("keyword_difficulty", "?"),
        serp_data.get("search_intent"),
    )

    # ── All competitor headings (flat list for brief) ─────────────────────────
    all_headings = []
    for comp in competitor_data:
        all_headings.extend(comp.get("headings", []))

    # ── Generate content brief via Claude ─────────────────────────────────────
    log.info("     Generating content brief via Claude…")
    try:
        context = prompts.load_context()
        agent_prompt = prompts.load_agent("content-analyzer")
        command_prompt = prompts.load_command("research")
        brief_text = _generate_brief(
            keyword, serp_data, competitor_data, context, agent_prompt, command_prompt, model=model
        )
    except Exception as exc:
        log.error("     Claude brief generation failed: %s", exc)
        brief_text = f"Research brief for: {keyword}\n\nKeyword data collected. Manual brief required."

    # ── Build result dict ─────────────────────────────────────────────────────
    result = {
        "keyword": keyword,
        "search_volume": serp_data.get("search_volume", 0),
        "keyword_difficulty": serp_data.get("keyword_difficulty", 0),
        "cpc": serp_data.get("cpc", 0.0),
        "search_intent": serp_data.get("search_intent", "informational"),
        "serp_urls": serp_data.get("serp_urls", []),
        "competitor_headings": all_headings[:40],
        "competitor_avg_word_count": avg_wc,
        "target_word_count": target_wc,
        "related_keywords": serp_data.get("related_keywords", []),
        "people_also_ask": serp_data.get("people_also_ask", []),
        "content_brief": brief_text,
        "competitor_data": competitor_data,
    }

    # ── Save research brief ───────────────────────────────────────────────────
    if config.get("save_research_briefs", True):
        _save_brief(keyword, result)

    return result


def _save_brief(keyword: str, data: Dict) -> None:
    """Save research brief as JSON + markdown to research/ directory."""
    slug = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")
    research_dir = _ROOT / "research"
    research_dir.mkdir(exist_ok=True)

    # JSON
    json_path = research_dir / f"{slug}-brief.json"
    try:
        json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.debug("     Saved research brief: %s", json_path)
    except Exception as exc:
        log.warning("     Could not save research JSON: %s", exc)

    # Markdown summary
    md_path = research_dir / f"{slug}-brief.md"
    try:
        lines = [
            f"# Research Brief: {data['keyword']}",
            "",
            f"**Search Volume:** {data['search_volume']}/mo  ",
            f"**Keyword Difficulty:** {data['keyword_difficulty']}  ",
            f"**Search Intent:** {data['search_intent']}  ",
            f"**Target Word Count:** {data['target_word_count']}  ",
            "",
            "## Content Brief",
            "",
            data["content_brief"],
            "",
            "## Related Keywords",
            "",
        ] + [f"- {k}" for k in data.get("related_keywords", [])] + [
            "",
            "## People Also Ask",
            "",
        ] + [f"- {q}" for q in data.get("people_also_ask", [])]
        md_path.write_text("\n".join(lines), encoding="utf-8")
    except Exception as exc:
        log.warning("     Could not save research markdown: %s", exc)

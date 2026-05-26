# Agent Prompt: Repurpose SEOMachine into a Fully Autonomous SEO Pipeline

## Your Mission

You are a senior Python engineer. Your task is to take the open-source **SEOMachine** repository (`https://github.com/TheCraigHewitt/seomachine`) and transform it from a Claude Code workspace (where a human types slash commands) into a **fully autonomous, unattended pipeline** that:

1. Reads a list of keywords from a file
2. Runs the full research → write → score → scrub → publish pipeline for every keyword
3. Publishes finished articles to **Ghost CMS** as drafts (via Ghost Admin API)
4. Logs everything, tracks what has been processed, and handles errors gracefully
5. Requires zero human interaction after launch

---

## Step 0 — Clone and Understand the Repo First

```bash
git clone https://github.com/TheCraigHewitt/seomachine.git
cd seomachine
```

Before writing a single line of code, read and fully understand these files in order:

1. `README.md` — full architecture overview
2. `CLAUDE.md` — how Claude Code interprets the workspace
3. `QUICK-START.md` — setup flow
4. `.claude/commands/research.md` — the research pipeline prompt/workflow
5. `.claude/commands/write.md` — the writing pipeline prompt/workflow
6. `.claude/commands/optimize.md` — the optimization workflow
7. `.claude/commands/scrub.md` — the AI pattern removal workflow
8. `.claude/agents/content-analyzer.md` — the content analyzer agent
9. `.claude/agents/seo-optimizer.md` — the SEO optimizer agent
10. `.claude/agents/keyword-mapper.md` — keyword mapping agent
11. `.claude/agents/meta-creator.md` — meta tag generation agent
12. `.claude/agents/editor.md` — humanization/editor agent
13. `data_sources/modules/keyword_analyzer.py` — keyword density + clustering
14. `data_sources/modules/seo_quality_rater.py` — SEO scoring (0-100)
15. `data_sources/modules/content_length_comparator.py` — SERP competitor analysis
16. `data_sources/modules/readability_scorer.py` — readability metrics
17. `data_sources/modules/search_intent_analyzer.py` — search intent detection
18. `data_sources/modules/dataforseo.py` — DataForSEO API client
19. `data_sources/modules/wordpress_publisher.py` — study this as a reference for the publisher you'll write
20. `data-sources-setup.md` — how data integrations are configured
21. `config/competitors.example.json` — competitor config format

Do not skip this reading step. The quality of your implementation depends on understanding what already exists.

---

## Step 1 — Understand What Claude Code Was Doing

The SEOMachine slash commands (`.claude/commands/*.md`) are markdown prompt templates that Claude Code would execute interactively. Each one describes a multi-step workflow. You need to understand that **these prompts ARE the logic** — they tell you exactly what the pipeline should do at each stage.

For example, `.claude/commands/research.md` tells Claude to:
- Take a keyword
- Run keyword research via DataForSEO
- Scrape top 10 SERP competitors
- Extract their headings, word counts, content gaps
- Produce a structured research brief
- Save it to `/research/`

Your job is to implement this same logic in Python, calling the Anthropic API where Claude reasoning is needed, and calling the existing Python modules where computation is needed.

---

## Step 2 — Architecture to Build

Create a new directory `autonomous/` inside the seomachine repo with this structure:

```
autonomous/
├── pipeline.py                  # Main orchestrator — the file the user runs
├── runner/
│   ├── __init__.py
│   ├── research_runner.py       # Wraps research workflow
│   ├── writer_runner.py         # Wraps writing workflow
│   ├── optimizer_runner.py      # Wraps optimization + scoring
│   ├── scrub_runner.py          # Wraps editor/humanization agent
│   └── publisher_runner.py      # Ghost CMS publisher (new — see Step 6)
├── prompts/
│   ├── __init__.py
│   └── loader.py                # Loads + renders .claude/commands/*.md as prompts
├── config/
│   └── pipeline_config.yaml     # Pipeline settings (thresholds, batch size, etc.)
├── keywords.txt                  # User's keyword input list
├── logs/
│   ├── processed.json            # Tracks completed keywords (prevents re-runs)
│   └── pipeline.log              # Full run log
└── README.md                    # Setup + usage instructions for the autonomous pipeline
```

Do NOT restructure or modify the existing seomachine files. Build alongside them. Import from them.

---

## Step 3 — The Prompt Loader

The `.claude/commands/*.md` files contain the exact instructions for each pipeline stage. Build a loader that reads them and uses them as system prompts or user prompts for Anthropic API calls.

```python
# autonomous/prompts/loader.py

from pathlib import Path
import re

COMMANDS_DIR = Path(__file__).parent.parent.parent / ".claude" / "commands"
AGENTS_DIR   = Path(__file__).parent.parent.parent / ".claude" / "agents"
CONTEXT_DIR  = Path(__file__).parent.parent.parent / "context"

def load_command(name: str) -> str:
    """Load a slash command's markdown as a prompt string."""
    path = COMMANDS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Command not found: {path}")
    return path.read_text()

def load_agent(name: str) -> str:
    """Load an agent's markdown as a system prompt."""
    path = AGENTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Agent not found: {path}")
    return path.read_text()

def load_context() -> str:
    """Load all context files (brand voice, style guide, SEO guidelines, etc.)"""
    parts = []
    for f in sorted(CONTEXT_DIR.glob("*.md")):
        parts.append(f"=== {f.stem.upper()} ===\n{f.read_text()}")
    return "\n\n".join(parts)

def render(template: str, variables: dict) -> str:
    """Replace {{variable}} placeholders in a prompt template."""
    for key, value in variables.items():
        template = template.replace(f"{{{{{key}}}}}", str(value))
    return template
```

---

## Step 4 — DataForSEO Integration

The existing `data_sources/modules/dataforseo.py` already has a DataForSEO client. Use it. Do not rewrite it.

The research runner should use DataForSEO for:
- **Keyword volume + difficulty** — `DataForSEOKeywordsData` or similar method in the existing module
- **SERP results** — get actual top 10 URLs for a keyword
- **Related keywords + questions** — "People Also Ask" style expansions
- **Competitor domain analysis** — if `config/competitors.json` exists

If DataForSEO credentials are not set, fall back gracefully to free SERP scraping using `trafilatura` + `beautifulsoup4`.

The research runner output should be a structured dict (and saved as JSON) containing:
```json
{
  "keyword": "...",
  "search_volume": 1200,
  "keyword_difficulty": 34,
  "serp_urls": ["url1", "url2", ...],
  "competitor_headings": ["heading 1", "heading 2", ...],
  "competitor_avg_word_count": 2400,
  "target_word_count": 2700,
  "related_keywords": ["kw1", "kw2", ...],
  "people_also_ask": ["question 1", "question 2", ...],
  "search_intent": "informational",
  "content_brief": "..."
}
```

---

## Step 5 — Claude API Calls

Every runner that needs AI reasoning must call the Anthropic API directly using the `anthropic` Python SDK. Always use `claude-sonnet-4-20250514` as the model.

### Standard pattern for all runners:

```python
import anthropic
import os

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

def call_claude(system_prompt: str, user_prompt: str, max_tokens: int = 8000) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}]
    )
    return response.content[0].text
```

### For each runner, here is what to pass:

**research_runner.py**
- `system`: `load_agent("content-analyzer")` + `load_context()`
- `user`: `load_command("research")` rendered with `{keyword}` and the DataForSEO data

**writer_runner.py**
- `system`: `load_context()` (brand voice, style guide, SEO guidelines)
- `user`: `load_command("write")` rendered with `{topic}`, `{research_brief}`, `{target_word_count}`, `{competitor_headings}`, `{related_keywords}`, `{people_also_ask}`

**optimizer_runner.py**
- Run the existing Python modules first (no AI needed for scoring):
  - `keyword_analyzer.py` → keyword density + LSI
  - `seo_quality_rater.py` → 0-100 score
  - `readability_scorer.py` → Flesch score, passive voice ratio
  - `content_length_comparator.py` → word count vs competitors
- Then call Claude with `load_agent("seo-optimizer")` + the article + the scores to get specific fix recommendations
- Then call Claude with `load_agent("meta-creator")` to generate meta title + description options

**scrub_runner.py**
- `system`: `load_agent("editor")`
- `user`: `load_command("scrub")` rendered with `{article_content}`
- This is the humanization pass — remove AI patterns, fix robotic phrasing

---

## Step 6 — Ghost CMS Publisher

The existing repo has a `wordpress_publisher.py`. Study its structure and write an equivalent `publisher_runner.py` for Ghost Admin API.

Ghost Admin API uses JWT authentication:

```python
import jwt  # PyJWT
import time

def ghost_jwt(admin_api_key: str) -> str:
    key_id, secret = admin_api_key.split(":")
    iat = int(time.time())
    payload = {"iat": iat, "exp": iat + 300, "aud": "/admin/"}
    return jwt.encode(
        payload,
        bytes.fromhex(secret),
        algorithm="HS256",
        headers={"kid": key_id}
    )
```

The publisher must:
1. Convert the markdown article body to HTML (use the `markdown` library)
2. Create or find Ghost tags matching the article's tags
3. POST to `/ghost/api/admin/posts/` with `status: "draft"` — NEVER publish directly
4. Set `meta_title`, `meta_description`, `custom_excerpt` from the article metadata
5. Return the Ghost post ID and admin editor URL
6. Handle errors with clear messages (wrong URL, bad API key, etc.)

Ghost API payload format:
```json
{
  "posts": [{
    "title": "...",
    "html": "...",
    "status": "draft",
    "meta_title": "...",
    "meta_description": "...",
    "custom_excerpt": "...",
    "tags": [{"name": "tag1"}, {"name": "tag2"}]
  }]
}
```

---

## Step 7 — The Main Pipeline Orchestrator

`autonomous/pipeline.py` is the file the user runs. It must:

### Input
- Read keywords from `autonomous/keywords.txt` (one per line, `#` for comments)
- Accept CLI arguments:
  - `--keyword "foo"` — run a single keyword
  - `--batch N` — process N unprocessed keywords
  - `--dry-run` — full pipeline but don't publish to Ghost
  - `--no-scrub` — skip the editor/humanization pass
  - `--reprocess` — ignore processed.json, rerun everything
  - `--min-score N` — override the minimum score threshold (default: 65)

### Processing loop
For each keyword:
```
1. Check processed.json — skip if already done (unless --reprocess)
2. research_runner.py    → produces research brief (JSON)
3. writer_runner.py      → produces article draft (markdown)
4. optimizer_runner.py   → produces SEO score + recommendations
5. [optional] scrub_runner.py  → humanizes the article
6. publisher_runner.py   → sends to Ghost as draft (if score >= min_score)
7. Update processed.json with result
8. Sleep DELAY_BETWEEN_KEYWORDS seconds
```

### Error handling
- Wrap each step in try/except
- If a step fails, log the error and continue to the next keyword
- Never crash the entire pipeline because one keyword failed
- Save partial results — if research succeeded but writing failed, save the research brief

### Tracking
`logs/processed.json` format:
```json
{
  "best-phones-under-50k-naira": {
    "keyword": "best phones under 50k naira",
    "status": "published_draft",
    "score": 78,
    "ghost_id": "abc123",
    "ghost_editor_url": "https://myblog.ghost.io/ghost/#/editor/post/abc123",
    "processed_at": "2025-10-29T14:23:00"
  }
}
```

Status values: `published_draft`, `low_score`, `failed`, `dry_run`

### Console output
Keep it clean and informative:
```
============================================================
  [1/10] best phones under 50k naira
============================================================
  → Step 1/5: Research (DataForSEO + SERP scraping)
     Volume: 2,400/mo | Difficulty: 38 | Intent: commercial
  → Step 2/5: Writing article...
     Generated: 2,847 words
  → Step 3/5: Scoring...
     ┌─────────────────────────────────┐
     │  SEO SCORE: 74/100  Grade: B   │
     │  Words: 2,847 | Readability: 62│
     └─────────────────────────────────┘
     ⚠  Keyword not in first H2
     ✓  Good keyword density: 1.4%
     ✓  Meta title + description present
  → Step 4/5: Scrubbing AI patterns...
  → Step 5/5: Publishing to Ghost as draft...
  ✅ Draft created: https://myblog.ghost.io/ghost/#/editor/post/abc123

⏳ Waiting 5s...
```

---

## Step 8 — Configuration File

`autonomous/config/pipeline_config.yaml`:

```yaml
# Pipeline behaviour
min_score_to_publish: 65      # Articles below this score are saved locally only
delay_between_keywords: 5     # Seconds between keywords (be polite to APIs)
batch_size: 0                 # 0 = process all unprocessed keywords
ai_scrub_pass: true           # Set false to skip humanization (cheaper)

# Writing settings
target_language: english
min_word_count: 1800
target_word_count_multiplier: 1.15   # Beat competitors by 15%

# DataForSEO settings
dataforseo_location_code: 2566       # Nigeria = 2566, UK = 2826, US = 2840
dataforseo_language_code: en
dataforseo_fallback_to_scraping: true  # If no DataForSEO creds, use free scraping

# Ghost settings
ghost_post_status: draft      # Always draft — never change to published

# Logging
log_level: INFO
save_research_briefs: true
save_score_reports: true
```

---

## Step 9 — Environment Variables

The `.env` file (copy from `.env.example`) must support:

```bash
# Required
ANTHROPIC_API_KEY=sk-ant-...

# Ghost (required for publishing)
GHOST_URL=https://yourblog.ghost.io
GHOST_ADMIN_API_KEY=your_id:your_secret

# DataForSEO (optional but strongly recommended)
DATAFORSEO_LOGIN=your@email.com
DATAFORSEO_PASSWORD=your_password

# Google integrations (optional — from existing seomachine setup)
GOOGLE_ANALYTICS_PROPERTY_ID=
GOOGLE_APPLICATION_CREDENTIALS=
```

---

## Step 10 — Requirements

Add to (or create) `autonomous/requirements.txt`:

```
anthropic>=0.40.0
python-dotenv>=1.0.0
requests>=2.31.0
beautifulsoup4>=4.12.0
trafilatura>=1.8.0
PyJWT>=2.8.0
markdown>=3.5.0
PyYAML>=6.0
nltk>=3.8
textstat>=0.7
scikit-learn>=1.3
```

Also install the existing seomachine requirements:
```bash
pip install -r data_sources/requirements.txt
pip install -r autonomous/requirements.txt
```

---

## Step 11 — The README for the Autonomous Pipeline

Write `autonomous/README.md` covering:
1. What it does (in plain language)
2. Prerequisites
3. Step-by-step setup including how to get Ghost Admin API key and DataForSEO credentials
4. All CLI usage examples
5. How to interpret the SEO score report
6. What to do with low-scoring articles
7. Cost breakdown per article (Claude API + DataForSEO)
8. Troubleshooting common errors

---

## Quality Rules You Must Follow

### Do not reinvent what already exists
- The existing `data_sources/modules/` Python files are well-written. Import them, don't rewrite them.
- The `.claude/commands/*.md` and `.claude/agents/*.md` files contain the exact prompts and logic. Use them as prompts for your Claude API calls — do not paraphrase or rewrite them.
- The `context/` files are the user's brand configuration. Always load them into the system prompt.

### Ghost publishing rules
- ALWAYS publish as `draft`. Never set `status: "published"`. The user must approve manually.
- If Ghost credentials are missing or wrong, log a clear error and save the article locally instead of crashing.

### DataForSEO rules
- Wrap all DataForSEO calls in try/except
- If credentials are missing or the API call fails, fall back to free SERP scraping via `trafilatura` + `beautifulsoup4`
- Log which data source was used for each keyword

### Anthropic API rules
- Always use model `claude-sonnet-4-20250514`
- Set `max_tokens: 8000` for writing tasks, `4000` for analysis tasks
- If an API call fails, retry once after 10 seconds, then log and skip

### Article quality rules
- Never publish to Ghost if `seo_quality_rater.py` score is below `min_score_to_publish` (default 65)
- Always run the scrubber — it removes em-dashes, AI filler phrases, and robotic patterns
- The final article must have: H1 title, minimum 4 H2s, meta title, meta description, word count ≥ 1800

### Idempotency
- Never reprocess a keyword that's already in `processed.json` unless `--reprocess` flag is used
- This means the user can run `python pipeline.py` daily and it only processes new keywords

---

## Deliverables Checklist

When you are done, the following must all work:

- [ ] `python autonomous/pipeline.py --dry-run --batch 1` runs one keyword end-to-end without Ghost publishing and produces a markdown draft in `drafts/`
- [ ] `python autonomous/pipeline.py --batch 3` processes 3 keywords and creates 3 Ghost drafts
- [ ] `python autonomous/pipeline.py` with an empty `keywords.txt` exits cleanly with a helpful message
- [ ] Re-running `python autonomous/pipeline.py` on already-processed keywords skips them
- [ ] A score report JSON is saved alongside every draft
- [ ] `logs/processed.json` is updated after every keyword regardless of success or failure
- [ ] If `DATAFORSEO_LOGIN` is not set, pipeline still works using free scraping
- [ ] If `GHOST_ADMIN_API_KEY` is wrong, pipeline logs a clear error, saves draft locally, and continues to next keyword
- [ ] All existing seomachine `context/` files are respected and loaded into every Claude API call

---

## Final Note on Tone and Style

The writing pipeline must produce articles that:
- Sound human, not robotic
- Take clear positions ("X is better than Y because...")
- Use the user's brand voice from `context/brand-voice.md`
- Include real-world examples relevant to the user's audience
- Never use: em-dashes, "in conclusion", "delve into", "it's worth noting", "leverage", "utilize", "in today's world"

The scrub runner (using `load_agent("editor")`) is the final quality gate before publishing. Do not skip it.

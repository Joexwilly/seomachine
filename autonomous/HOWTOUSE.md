
## How the pipeline works

### The mental model

The system converts SEOMachine's interactive Claude Code slash commands into a fully automated Python loop. Instead of *you* typing `/research best budget smartphones`, the pipeline does it programmatically — calling the Anthropic API with the exact same prompt templates that the original commands used.

### The 5-step pipeline (per keyword)

```
keywords.txt
    ↓
1. RESEARCH     → DataForSEO (or free scraping) + Claude generates a content brief
    ↓
2. WRITE        → Claude writes the full article (~2000+ words)
    ↓
3. SCORE        → Python modules rate SEO quality (0–100), Claude gives specific fixes
    ↓
4. SCRUB        → Regex removes em-dashes/filler, Claude humanizes the article
    ↓
5. PUBLISH      → Ghost Admin API creates a draft (you approve before it goes live)
    ↓
logs/processed.json  (records every outcome, prevents re-runs)
```

**Key design principle:** Steps are independent and fault-tolerant. If step 3 (scoring) crashes, the pipeline saves what it has and moves to the next keyword instead of crashing entirely.

---

## What each file does

| File | Role |
|---|---|
| keywords.txt | Your input queue — add/remove keywords here |
| processed.json | The idempotency log — processed keywords are skipped on re-run |
| pipeline_config.yaml | All tuneable settings (score threshold, delays, model, etc.) |
| context/*.md | **This is your brand** — every Claude call loads these files |
| .claude/commands/*.md | The original prompt templates — loaded as Claude instructions |
| .claude/agents/*.md | Agent system prompts — editor, seo-optimizer, meta-creator, etc. |

---

## The most important thing: context

Every single Claude API call in the pipeline loads **all files from context** into the system prompt. These files are the difference between generic AI content and content that sounds like your brand:

- brand-voice.md — tone, personality, writing style
- style-guide.md — formatting conventions
- seo-guidelines.md — keyword rules
- internal-links-map.md — pages to link to internally
- features.md — product features Claude can reference

**Before running real keywords, these are the files to customize first.** The example files are placeholders for Castos (a podcast company). Replace them with your actual brand.

---

## How to fix your current error

Your first run failed because `ANTHROPIC_API_KEY` isn't set. Steps:

```bash
cp .env.example .env
# then edit .env and add:
# ANTHROPIC_API_KEY=sk-ant-...
# GHOST_URL=https://yourblog.ghost.io
# GHOST_ADMIN_API_KEY=your_id:your_hex_secret
```

Since `best budget smartphones 2025` is in processed.json as `failed`, re-run it with:
```bash
source .venv/bin/activate
python autonomous/pipeline.py --reprocess --dry-run
```

`--reprocess` forces it to ignore the `failed` entry. `--dry-run` skips Ghost publishing so you can see the output first.

---

## Best ways to use this system

### 1. Start with `--dry-run` for every new brand setup
Always run dry first until you're happy with the output quality. Articles land in drafts and `review-required/` — no Ghost credentials needed.

```bash
python autonomous/pipeline.py --dry-run --batch 3
```

### 2. Batch by topic cluster
Don't dump 100 random keywords at once. Group related keywords together so you can review a cluster's output quality before moving to the next topic. The pipeline processes them in order.

```
# phones cluster
best budget smartphones 2025
best phone under 50000 naira
best android phones for students

# productivity cluster  
best note taking apps 2025
notion vs obsidian
```

### 3. Use `--no-scrub` when testing to go faster
The scrub step is an extra Claude call — skip it during initial testing to cut cost and time in half, then enable it for real production runs.

```bash
python autonomous/pipeline.py --dry-run --no-scrub --batch 1
```

### 4. Set a realistic score threshold
The default `min_score_to_publish: 65` is a good starting point. If you're seeing mostly low scores early on, drop it temporarily to 50 while you tune context files, then raise it back up.

### 5. Run it on a schedule (unattended)
Once output quality is good, automate it with a cron job:

```bash
# Add to crontab (runs at 3am daily)
0 3 * * * cd /path/to/seomachine && .venv/bin/python autonomous/pipeline.py --batch 5
```

The idempotency of processed.json means you can schedule this daily — it only ever processes new keywords.

### 6. The review workflow
Articles published to Ghost are **always drafts**. The intended workflow is:
1. Pipeline runs overnight → creates Ghost drafts
2. You log into Ghost in the morning → review drafts
3. Fix anything that needs work → publish
4. Any articles in `review-required/` (low score) → manually improve or discard

---

## Cost expectations

| Config | Cost per article |
|---|---|
| With `--no-scrub` | ~$0.05 |
| Full pipeline (default) | ~$0.085 |
| With DataForSEO | +$0.002–$0.01 per keyword |

At $0.085/article, you can produce 100 articles for ~$8.50 in Claude API costs.

---

## What to watch out for

- **DataForSEO is optional but strongly recommended** — without it, volume/difficulty data shows as `0`, and the research brief is weaker because it's based on scraped headings only
- **Google blocks aggressive scraping** — if you're running large batches without DataForSEO, the free SERP fallback can get throttled; increase `delay_between_keywords` to 15–30 seconds
- **Quality scales with your context files** — a well-written `brand-voice.md` with real examples of your writing style will dramatically improve output quality
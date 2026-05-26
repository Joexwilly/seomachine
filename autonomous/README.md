# Autonomous SEO Pipeline

Transforms the SEOMachine workspace into a **fully autonomous, unattended pipeline** that researches, writes, scores, scrubs, and publishes articles to Ghost CMS — zero human interaction after launch.

---

## What it does

For each keyword in `keywords.txt`, the pipeline:

1. **Research** — Fetches keyword volume, difficulty, SERP URLs, competitor headings, related keywords and People Also Ask (via DataForSEO or free scraping fallback)
2. **Write** — Calls Claude to write a full-length, SEO-optimised article based on the research brief and your brand context
3. **Score** — Runs Python scoring modules (keyword density, readability, SEO quality) and gets Claude's specific improvement recommendations
4. **Scrub** — Passes the article through an AI-pattern removal step to sound human: no em-dashes, filler phrases, or robotic transitions
5. **Publish** — Posts the article to Ghost CMS as a **draft** (you always approve before publishing)

---

## Prerequisites

- Python 3.10+
- An Anthropic API key (`claude-sonnet-4-20250514`)
- A Ghost CMS instance with Admin API access
- (Optional but recommended) DataForSEO account

---

## Setup

### 1. Clone and activate the virtual environment

```bash
git clone https://github.com/TheCraigHewitt/seomachine.git
cd seomachine
python3 -m venv .venv
source .venv/bin/activate          # macOS/Linux
.venv\Scripts\activate             # Windows
```

### 2. Install dependencies

```bash
pip install -r data_sources/requirements.txt
pip install -r autonomous/requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ | Your Anthropic API key |
| `GHOST_URL` | ✅ | Your Ghost site URL, e.g. `https://myblog.ghost.io` |
| `GHOST_ADMIN_API_KEY` | ✅ | Ghost Admin API key (see below) |
| `DATAFORSEO_LOGIN` | Optional | DataForSEO email |
| `DATAFORSEO_PASSWORD` | Optional | DataForSEO password |

#### Getting your Ghost Admin API key

1. Log in to Ghost Admin → **Settings** → **Integrations**
2. Click **Add custom integration**
3. Name it (e.g. "SEO Machine") and click **Create**
4. Copy the **Admin API Key** (format: `key_id:hex_secret`)
5. Paste it into `.env` as `GHOST_ADMIN_API_KEY`

#### Getting DataForSEO credentials

1. Sign up at [dataforseo.com](https://dataforseo.com)
2. Go to **API Access** and note your login email + password
3. Paste them into `.env`

If DataForSEO is not configured, the pipeline falls back to free Google SERP scraping.

### 4. Add your keywords

Edit `autonomous/keywords.txt` — one keyword per line:

```
best budget smartphones 2025
how to improve website speed
content marketing for startups
```

Lines starting with `#` are treated as comments and skipped.

### 5. Update your brand context (important)

Edit the files in `context/` to match your brand:
- `brand-voice.md` — tone, writing style
- `style-guide.md` — grammar and formatting standards
- `seo-guidelines.md` — keyword and structure rules
- `internal-links-map.md` — key pages to link to
- `features.md` — your product features (for context)

### 6. (Optional) Adjust pipeline settings

Edit `autonomous/config/pipeline_config.yaml` to tune:
- `min_score_to_publish` — minimum SEO score before Ghost publish (default: 65)
- `delay_between_keywords` — seconds between keywords (default: 5)
- `dataforseo_location_code` — `2840` = US, `2826` = UK, `2566` = Nigeria
- `ai_scrub_pass` — set `false` to skip humanization (faster + cheaper)

---

## Usage

```bash
# Always activate the venv first
source .venv/bin/activate

# Process all unprocessed keywords from keywords.txt
python autonomous/pipeline.py

# Process next 3 unprocessed keywords
python autonomous/pipeline.py --batch 3

# Run for a single keyword (doesn't need to be in keywords.txt)
python autonomous/pipeline.py --keyword "best coffee grinders 2025"

# Full pipeline but DON'T publish to Ghost (saves to drafts/)
python autonomous/pipeline.py --dry-run

# Skip the humanization/scrub pass (faster, slightly cheaper)
python autonomous/pipeline.py --no-scrub

# Re-run keywords already in processed.json
python autonomous/pipeline.py --reprocess

# Override the minimum score threshold
python autonomous/pipeline.py --min-score 70

# Combine flags
python autonomous/pipeline.py --batch 5 --dry-run --no-scrub
```

---

## Understanding the SEO score

| Score | Grade | Action |
|---|---|---|
| 80–100 | A | Published to Ghost automatically |
| 65–79 | B | Published to Ghost automatically |
| 50–64 | C | Saved to `review-required/` — needs manual review |
| Below 50 | D/F | Saved to `review-required/` — significant work needed |

Common issues that lower the score:
- **Keyword density too low/high** — aim for 1–2%
- **Missing keyword in first H2** — the optimizer will flag this
- **Meta title too long** — keep under 60 characters
- **Meta description too long** — keep under 160 characters
- **Too few internal links** — add links to your key pages in `context/internal-links-map.md`

### What to do with low-scoring articles

1. Open the file in `review-required/`
2. Check the score report JSON in `drafts/` (same slug, `-score-report.json` suffix)
3. Apply the Claude recommendations in the score report
4. Move the improved file to `review-required/` and publish manually to Ghost

---

## Tracking

All processed keywords are logged to `autonomous/logs/processed.json`:

```json
{
  "best-coffee-grinders-2025": {
    "keyword": "best coffee grinders 2025",
    "status": "published_draft",
    "score": 78,
    "ghost_id": "abc123",
    "ghost_editor_url": "https://myblog.ghost.io/ghost/#/editor/post/abc123",
    "processed_at": "2025-10-29T14:23:00+00:00"
  }
}
```

Status values:
- `published_draft` — successfully created as Ghost draft
- `low_score` — score below threshold, saved locally
- `dry_run` — dry-run mode, saved locally
- `failed` — an error occurred (check `logs/pipeline.log`)

Re-running the pipeline skips any keyword already in `processed.json`. Use `--reprocess` to override.

---

## Cost breakdown

Approximate Claude API costs per article (claude-sonnet-4-20250514):

| Step | Estimated tokens | Approx. cost |
|---|---|---|
| Research brief | ~4,000 | ~$0.015 |
| Article writing | ~8,000 | ~$0.030 |
| SEO recommendations | ~2,000 | ~$0.008 |
| Meta tag generation | ~500 | ~$0.002 |
| Scrub/humanization | ~8,000 | ~$0.030 |
| **Total per article** | **~22,500** | **~$0.085** |

DataForSEO charges per API call — typically $0.002–$0.01 per keyword lookup.

---

## Troubleshooting

### "ANTHROPIC_API_KEY is not set"
Make sure `.env` exists in the repo root and contains `ANTHROPIC_API_KEY=sk-ant-...`
Confirm the venv is activated: `source .venv/bin/activate`

### "Ghost API: 401 Unauthorized"
Your `GHOST_ADMIN_API_KEY` is wrong. Re-copy it from Ghost Admin → Integrations.
Format must be `key_id:hex_secret` (e.g. `6783abc:4f8...`)

### "Ghost API: 404 Not Found"
Your `GHOST_URL` is wrong. Check it includes `https://` and no trailing slash.
Example: `GHOST_URL=https://myblog.ghost.io`

### DataForSEO errors
The pipeline falls back to free scraping automatically. Check `logs/pipeline.log`
to confirm which data source was used. If you want DataForSEO, verify credentials
in `.env` and that your account has credits.

### Score is always 0
The Python scoring modules need `scikit-learn` and `textstat`. Confirm they're installed:
```bash
pip install scikit-learn textstat
```

### Articles sound robotic
Enable the scrub pass in `config/pipeline_config.yaml`:
```yaml
ai_scrub_pass: true
```
And update `context/brand-voice.md` with specific tone examples from your existing content.

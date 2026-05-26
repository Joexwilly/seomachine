"""
Autonomous SEO Pipeline

The main entry point. Reads keywords from autonomous/keywords.txt, runs the
full research → write → score → scrub → publish pipeline for each one,
and logs all results to autonomous/logs/processed.json.

Usage:
    python autonomous/pipeline.py                  # Process all unprocessed keywords
    python autonomous/pipeline.py --batch 3        # Process next 3 unprocessed
    python autonomous/pipeline.py --keyword "foo"  # Run one specific keyword
    python autonomous/pipeline.py --dry-run        # Full pipeline, no Ghost publish
    python autonomous/pipeline.py --no-scrub       # Skip humanization pass
    python autonomous/pipeline.py --reprocess      # Re-run already-processed keywords
    python autonomous/pipeline.py --min-score 70   # Override minimum score threshold
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

# ── Path setup ────────────────────────────────────────────────────────────────
_AUTO_DIR = Path(__file__).parent
_ROOT = _AUTO_DIR.parent
sys.path.insert(0, str(_ROOT))

# Load .env from repo root
load_dotenv(_ROOT / ".env")

# ── Logging setup ─────────────────────────────────────────────────────────────

def _setup_logging(level: str = "INFO") -> None:
    log_dir = _AUTO_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "pipeline.log"

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    handlers: list = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    # Quieten noisy third-party loggers
    for noisy in ("urllib3", "requests", "anthropic", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


log = logging.getLogger(__name__)

# ── Config loading ────────────────────────────────────────────────────────────

def _load_config() -> dict:
    config_path = _AUTO_DIR / "config" / "pipeline_config.yaml"
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    log.warning("pipeline_config.yaml not found — using defaults")
    return {}


# ── Processed log helpers ─────────────────────────────────────────────────────

_PROCESSED_PATH = _AUTO_DIR / "logs" / "processed.json"


def _load_processed() -> dict:
    if _PROCESSED_PATH.exists():
        try:
            return json.loads(_PROCESSED_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_processed(data: dict) -> None:
    _PROCESSED_PATH.parent.mkdir(exist_ok=True)
    _PROCESSED_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _log_key(keyword: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")


# ── Keywords loading ──────────────────────────────────────────────────────────

def _load_keywords() -> list:
    kw_path = _AUTO_DIR / "keywords.txt"
    if not kw_path.exists():
        log.error("keywords.txt not found at %s", kw_path)
        return []
    lines = kw_path.read_text(encoding="utf-8").splitlines()
    keywords = []
    for line in lines:
        kw = line.strip()
        if kw and not kw.startswith("#"):
            keywords.append(kw)
    return keywords


# ── Console banner helpers ────────────────────────────────────────────────────

def _banner(index: int, total: int, keyword: str) -> None:
    line = "=" * 60
    print(f"\n{line}")
    print(f"  [{index}/{total}] {keyword}")
    print(line)


# ── Main pipeline for one keyword ─────────────────────────────────────────────

def _process_keyword(
    keyword: str,
    config: dict,
    prompts,
    dry_run: bool,
    no_scrub: bool,
) -> dict:
    """
    Run the full pipeline for a single keyword.
    Returns a status dict suitable for processed.json.
    Always returns — never raises.
    """
    from autonomous.runner import (  # type: ignore
        research_runner,
        writer_runner,
        optimizer_runner,
        scrub_runner,
        publisher_runner,
        image_runner,
    )

    slug = _log_key(keyword)
    result = {
        "keyword": keyword,
        "status": "failed",
        "score": 0,
        "ghost_id": "",
        "ghost_editor_url": "",
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }

    # ── Step 1: Research ──────────────────────────────────────────────────────
    try:
        research = research_runner.run(keyword, config, prompts)
    except Exception as exc:
        log.error("     Research failed: %s", exc)
        result["error"] = f"research: {exc}"
        return result

    # ── Step 2: Write ─────────────────────────────────────────────────────────
    try:
        writer_result = writer_runner.run(keyword, research, config, prompts)
    except Exception as exc:
        log.error("     Writing failed: %s", exc)
        result["error"] = f"writing: {exc}"
        return result

    # ── Step 3: Optimize + Score ──────────────────────────────────────────────
    try:
        optimizer_result = optimizer_runner.run(
            keyword, writer_result, research, config, prompts
        )
    except Exception as exc:
        log.error("     Optimization failed: %s", exc)
        # Non-fatal — continue with score=0
        optimizer_result = {
            "seo_score": 0,
            "grade": "?",
            "meta_title": writer_result.get("meta_title", ""),
            "meta_description": writer_result.get("meta_description", ""),
        }

    seo_score = optimizer_result.get("seo_score", 0)
    result["score"] = seo_score

    # ── Step 4: Scrub (optional) ──────────────────────────────────────────────
    if not no_scrub and config.get("ai_scrub_pass", True):
        try:
            scrub_result = scrub_runner.run(
                keyword, writer_result, optimizer_result, config, prompts
            )
            final_article = scrub_result.get("article", writer_result.get("article", ""))
            final_meta_title = scrub_result.get("meta_title", optimizer_result.get("meta_title", ""))
            final_meta_desc = scrub_result.get("meta_description", optimizer_result.get("meta_description", ""))
            final_title = scrub_result.get("title", writer_result.get("title", keyword.title()))
        except Exception as exc:
            log.warning("     Scrub failed (%s) — using unscrubbed article", exc)
            final_article = writer_result.get("article", "")
            final_meta_title = optimizer_result.get("meta_title", writer_result.get("meta_title", ""))
            final_meta_desc = optimizer_result.get("meta_description", writer_result.get("meta_description", ""))
            final_title = writer_result.get("title", keyword.title())
    else:
        log.info("  → Step 4/5: Skipping scrub (--no-scrub or disabled in config)")
        final_article = writer_result.get("article", "")
        final_meta_title = optimizer_result.get("meta_title", writer_result.get("meta_title", ""))
        final_meta_desc = optimizer_result.get("meta_description", writer_result.get("meta_description", ""))
        final_title = writer_result.get("title", keyword.title())

    # ── Step 4b: Images (optional) ───────────────────────────────────────────
    article_images = []
    if config.get("images_enabled", False) and not dry_run:
        try:
            article_images = image_runner.run(
                keyword=keyword,
                article=final_article,
                title=final_title,
                slug=slug,
                research=research,
                config=config,
            )
        except Exception as exc:
            log.warning("     Image generation failed (%s) — continuing without images", exc)

    # ── Step 5: Publish ───────────────────────────────────────────────────────
    try:
        pub_result = publisher_runner.run(
            keyword=keyword,
            final_article=final_article,
            title=final_title,
            slug=slug,
            meta_title=final_meta_title,
            meta_description=final_meta_desc,
            seo_score=seo_score,
            config=config,
            dry_run=dry_run,
            images=article_images,
        )
    except Exception as exc:
        log.error("     Publishing failed unexpectedly: %s", exc)
        pub_result = {
            "published": False,
            "status": "failed",
            "ghost_id": "",
            "ghost_editor_url": "",
            "error": str(exc),
        }

    # ── Assemble final result ─────────────────────────────────────────────────
    result["status"] = pub_result.get("status", "failed")
    result["ghost_id"] = pub_result.get("ghost_id", "")
    result["ghost_editor_url"] = pub_result.get("ghost_editor_url", "")
    result["local_path"] = pub_result.get("local_path", "")
    if pub_result.get("error"):
        result["error"] = pub_result["error"]

    if pub_result.get("published"):
        print(f"\n  ✅ Draft created: {pub_result['ghost_editor_url']}")
    elif pub_result.get("status") == "dry_run":
        print(f"\n  📝 Dry run — saved to: {pub_result.get('local_path', 'drafts/')}")
    elif pub_result.get("status") == "low_score":
        print(f"\n  ⚠  Score {seo_score} below threshold {config.get('min_score_to_publish', 65)} — saved locally")
    else:
        print(f"\n  ❌ Not published — check logs for details")

    return result


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Autonomous SEO Pipeline — research, write, score, scrub, publish"
    )
    parser.add_argument("--keyword", type=str, help="Run pipeline for a single keyword")
    parser.add_argument("--batch", type=int, default=0, help="Process N unprocessed keywords (0 = all)")
    parser.add_argument("--dry-run", action="store_true", help="Full pipeline but don't publish to Ghost")
    parser.add_argument("--no-scrub", action="store_true", help="Skip the editor/humanization pass")
    parser.add_argument("--reprocess", action="store_true", help="Ignore processed.json and rerun everything")
    parser.add_argument("--min-score", type=int, default=None, help="Override minimum score threshold")
    args = parser.parse_args()

    # ── Load config ───────────────────────────────────────────────────────────
    config = _load_config()
    _setup_logging(config.get("log_level", "INFO"))

    if args.min_score is not None:
        config["min_score_to_publish"] = args.min_score

    log.info("Autonomous SEO Pipeline starting")
    if args.dry_run:
        log.info("DRY RUN mode — articles will NOT be published to Ghost")

    # ── Load prompts module ───────────────────────────────────────────────────
    from autonomous.prompts import loader as prompts  # type: ignore

    # ── Determine keyword list ────────────────────────────────────────────────
    if args.keyword:
        keywords = [args.keyword.strip()]
        log.info("Single keyword mode: %s", args.keyword)
    else:
        keywords = _load_keywords()
        if not keywords:
            print("\nNo keywords found in autonomous/keywords.txt")
            print("Add one keyword per line (use # for comments) and try again.\n")
            sys.exit(0)
        log.info("Loaded %d keywords from keywords.txt", len(keywords))

    # ── Filter already-processed ──────────────────────────────────────────────
    processed = _load_processed()

    if not args.reprocess:
        pending = [kw for kw in keywords if _log_key(kw) not in processed]
        skipped = len(keywords) - len(pending)
        if skipped:
            log.info("Skipping %d already-processed keywords (use --reprocess to override)", skipped)
        keywords = pending

    if not keywords:
        print("\nAll keywords have already been processed.")
        print("Use --reprocess to re-run them, or add new keywords to keywords.txt\n")
        sys.exit(0)

    # ── Apply batch limit ─────────────────────────────────────────────────────
    batch = args.batch or config.get("batch_size", 0)
    if batch > 0:
        keywords = keywords[:batch]
        log.info("Batch mode: processing %d keywords", len(keywords))

    total = len(keywords)
    delay = config.get("delay_between_keywords", 5)
    success_count = 0
    fail_count = 0

    # ── Main loop ─────────────────────────────────────────────────────────────
    for i, keyword in enumerate(keywords, start=1):
        _banner(i, total, keyword)

        try:
            result = _process_keyword(
                keyword=keyword,
                config=config,
                prompts=prompts,
                dry_run=args.dry_run,
                no_scrub=args.no_scrub,
            )
        except Exception as exc:
            log.exception("Unhandled error for keyword '%s': %s", keyword, exc)
            result = {
                "keyword": keyword,
                "status": "failed",
                "score": 0,
                "ghost_id": "",
                "ghost_editor_url": "",
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
            }

        # ── Update processed log ──────────────────────────────────────────────
        key = _log_key(keyword)
        processed[key] = result
        _save_processed(processed)

        if result["status"] in ("published_draft", "dry_run", "low_score"):
            success_count += 1
        else:
            fail_count += 1

        # ── Delay between keywords ────────────────────────────────────────────
        if i < total and delay > 0:
            print(f"\n⏳ Waiting {delay}s…\n")
            time.sleep(delay)

    # ── Summary ───────────────────────────────────────────────────────────────
    line = "=" * 60
    print(f"\n{line}")
    print(f"  PIPELINE COMPLETE")
    print(f"  Keywords processed: {total}")
    print(f"  Succeeded:  {success_count}")
    print(f"  Failed:     {fail_count}")
    print(f"  Log:        {_AUTO_DIR}/logs/pipeline.log")
    print(f"  Tracking:   {_PROCESSED_PATH}")
    print(line + "\n")


if __name__ == "__main__":
    main()

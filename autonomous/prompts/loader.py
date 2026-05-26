"""
Prompt Loader

Loads .claude/commands/*.md and .claude/agents/*.md files as prompt strings
for use with the Anthropic API. Also loads context/ files as system context.
"""

from pathlib import Path
import re

_BASE = Path(__file__).parent.parent.parent

COMMANDS_DIR = _BASE / ".claude" / "commands"
AGENTS_DIR   = _BASE / ".claude" / "agents"
CONTEXT_DIR  = _BASE / "context"


def load_command(name: str) -> str:
    """Load a slash command markdown file as a prompt string."""
    path = COMMANDS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Command not found: {path}")
    return path.read_text(encoding="utf-8")


def load_agent(name: str) -> str:
    """Load an agent markdown file as a system prompt string."""
    path = AGENTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Agent not found: {path}")
    return path.read_text(encoding="utf-8")


def load_context() -> str:
    """Load all context/ markdown files and return as combined string."""
    parts = []
    if CONTEXT_DIR.exists():
        for f in sorted(CONTEXT_DIR.glob("*.md")):
            parts.append(f"=== {f.stem.upper().replace('-', ' ')} ===\n{f.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


def render(template: str, variables: dict) -> str:
    """Replace {{variable}} placeholders in a prompt template."""
    for key, value in variables.items():
        template = template.replace(f"{{{{{key}}}}}", str(value))
    return template

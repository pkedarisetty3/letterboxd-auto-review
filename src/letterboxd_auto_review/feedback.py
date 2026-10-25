from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from letterboxd_auto_review.movie_files import title_from_filename
from uuid import uuid4


DEFAULT_FEEDBACK_PATH = Path("feedback/review_edits.jsonl")
MAX_FEEDBACK_CHARS = 2400


def parse_heading(markdown: str, path: Path) -> str:
    """Read the movie title from the first Markdown heading in a draft file."""

    match = re.search(r"(?m)^#\s+(.+?)\s*$", markdown)
    if match:
        return match.group(1).strip()
    return title_from_filename(path)


def parse_rating(markdown: str) -> str | None:
    """Read the display rating from the generated draft metadata."""

    match = re.search(r"(?m)^\*\*Rating:\*\*\s+(.+?)\s*$", markdown)
    if not match:
        return None
    return match.group(1).strip()


def parse_section(markdown: str, heading: str) -> str:
    """Extract a top-level draft section by its exact Markdown heading."""

    pattern = rf"(?ms)^## {re.escape(heading)}\s*\n(?P<section>.*?)(?=^##\s+|\Z)"
    match = re.search(pattern, markdown)
    if not match:
        raise ValueError(f"Could not find a `## {heading}` section in the draft file.")

    section = match.group("section").strip()
    if not section:
        raise ValueError(f"The `## {heading}` section is empty.")
    return section


def normalize_for_comparison(text: str) -> str:
    """Normalize whitespace so harmless spacing changes do not count as edits."""

    return "\n\n".join(line.strip() for line in text.replace("\r\n", "\n").splitlines() if line.strip())


def truncate_feedback_text(text: str, limit: int = MAX_FEEDBACK_CHARS) -> str:
    """Keep stored examples prompt-sized while preserving the start and ending."""

    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped

    half = max((limit - 40) // 2, 1)
    return f"{stripped[:half].rstrip()}\n\n[...]\n\n{stripped[-half:].lstrip()}"


def build_feedback_record(draft_path: Path) -> dict:
    """Create one reusable feedback record from an edited generated draft."""

    markdown = draft_path.read_text(encoding="utf-8")
    generated_review = parse_section(markdown, "Generated Review")
    edited_review = parse_section(markdown, "Markdown Review")

    if normalize_for_comparison(generated_review) == normalize_for_comparison(edited_review):
        raise ValueError(
            "The editable review matches the generated review. Edit `## Markdown Review` first, "
            "then run this command again."
        )

    return {
        "id": uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "movie": parse_heading(markdown, draft_path),
        "rating": parse_rating(markdown),
        "draft_path": str(draft_path),
        "generated_review": truncate_feedback_text(generated_review),
        "edited_review": truncate_feedback_text(edited_review),
    }


def append_feedback_record(record: dict, feedback_path: Path) -> None:
    """Append one feedback record to the local JSONL feedback store."""

    feedback_path.parent.mkdir(parents=True, exist_ok=True)
    with feedback_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_feedback_records(feedback_path: Path, limit: int) -> list[dict]:
    """Load the most recent valid feedback records from disk."""

    if limit <= 0 or not feedback_path.exists():
        return []

    records = []
    for line in feedback_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # A damaged line should not prevent drafting. Skip it and keep going.
            continue

    return records[-limit:]


def format_feedback_examples(records: list[dict]) -> str:
    """Format before/after edits so the model can learn from them in context."""

    if not records:
        return "- None yet"

    examples = []
    for index, record in enumerate(records, start=1):
        movie = record.get("movie") or "Unknown movie"
        rating = record.get("rating") or "Unrated"
        generated = (record.get("generated_review") or "").strip()
        edited = (record.get("edited_review") or "").strip()
        examples.append(
            "\n".join(
                [
                    f"Feedback example {index}: {movie} ({rating})",
                    "Model draft:",
                    generated,
                    "User-edited version:",
                    edited,
                ]
            )
        )

    return "\n\n---\n\n".join(examples)


def learn_from_draft(draft_path: Path, feedback_path: Path = DEFAULT_FEEDBACK_PATH) -> dict:
    """Store the user's edit to a generated review as reusable feedback."""

    record = build_feedback_record(draft_path)
    append_feedback_record(record, feedback_path)
    return record


def main() -> None:
    """Command-line entry point for saving feedback from an edited review."""

    parser = argparse.ArgumentParser(
        description="Save your edited Markdown review as feedback for future drafts."
    )
    parser.add_argument("draft", type=Path, help="Path to an edited `*-review.md` draft.")
    parser.add_argument(
        "--feedback",
        type=Path,
        default=DEFAULT_FEEDBACK_PATH,
        help="Where to store local before/after feedback examples.",
    )
    args = parser.parse_args()

    record = learn_from_draft(args.draft, args.feedback)
    print(f"Saved feedback for {record['movie']} to {args.feedback}")


if __name__ == "__main__":
    # This makes the file runnable as:
    # python -m letterboxd_auto_review.feedback drafts/movie-review.md
    main()

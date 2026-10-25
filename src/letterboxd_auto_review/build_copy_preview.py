from __future__ import annotations

import argparse
import re
from pathlib import Path

from letterboxd_auto_review.movie_files import title_from_filename

from letterboxd_auto_review.draft_review import format_copy_preview_html


def parse_draft_title(markdown: str, path: Path) -> str:
    """Read the movie title from the first Markdown heading in a draft file."""

    match = re.search(r"(?m)^#\s+(.+?)\s*$", markdown)
    if match:
        return match.group(1).strip()

    # Keep the command useful even if the heading was accidentally deleted.
    # The generated filenames end in -review.md, so remove that suffix first.
    return title_from_filename(path)


def parse_draft_rating(markdown: str) -> str | None:
    """Read the display rating from the generated draft metadata."""

    match = re.search(r"(?m)^\*\*Rating:\*\*\s+(.+?)\s*$", markdown)
    if not match:
        return None
    return match.group(1).strip()


def parse_markdown_review(markdown: str) -> str:
    """Extract the editable review body from the `## Markdown Review` section."""

    match = re.search(
        r"(?ms)^## Markdown Review\s*\n(?P<review>.*?)(?=^##\s+|\Z)",
        markdown,
    )
    if not match:
        raise ValueError("Could not find a `## Markdown Review` section in the draft file.")

    review = match.group("review").strip()
    if not review:
        raise ValueError("The `## Markdown Review` section is empty.")
    return review


def output_path_for_draft(input_path: Path) -> Path:
    """Choose the standard HTML copy-preview path for a draft Markdown file."""

    if input_path.name.endswith("-review.md"):
        output_name = input_path.name.removesuffix("-review.md") + "-letterboxd-copy.html"
    else:
        output_name = input_path.stem + "-letterboxd-copy.html"
    return input_path.with_name(output_name)


def build_copy_preview(input_path: Path, output_path: Path | None = None) -> Path:
    """Regenerate the HTML copy preview from an edited Markdown draft."""

    markdown = input_path.read_text(encoding="utf-8")
    movie = parse_draft_title(markdown, input_path)
    rating = parse_draft_rating(markdown)
    review = parse_markdown_review(markdown)
    destination = output_path or output_path_for_draft(input_path)

    destination.write_text(format_copy_preview_html(movie, rating, review), encoding="utf-8")
    return destination


def main() -> None:
    """Command-line entry point for refreshing the Letterboxd copy preview."""

    parser = argparse.ArgumentParser(
        description="Regenerate the Letterboxd copy HTML from an edited draft Markdown file."
    )
    parser.add_argument("draft", type=Path, help="Path to a generated `*-review.md` draft.")
    parser.add_argument("--output", type=Path, help="Optional output path for the HTML preview.")
    args = parser.parse_args()

    output_path = build_copy_preview(args.draft, args.output)
    print(f"Wrote Letterboxd copy preview to {output_path}")


if __name__ == "__main__":
    # This makes the file runnable as:
    # python -m letterboxd_auto_review.build_copy_preview drafts/movie-review.md
    main()

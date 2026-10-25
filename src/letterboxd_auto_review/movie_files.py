"""Keep movie filenames and watch-date metadata consistent across the workflow."""

from __future__ import annotations

import csv
import json
import re
from datetime import date
from pathlib import Path

DEFAULT_DIARY_PATH = Path("data/raw/letterboxd_export/diary.csv")


def validate_watched_date(value: str) -> str:
    """Accept real calendar dates written in the sortable YYYY-MM-DD format."""

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Watch dates must use YYYY-MM-DD.")
    return date.fromisoformat(value).isoformat()


def parse_watched_date(notes: str) -> str | None:
    """Read the optional watch-date field near the top of a notes file."""

    match = re.search(r"(?im)^Watched:[ \t]*([^\n\r]*)", notes)
    value = match.group(1).strip() if match else ""
    return validate_watched_date(value) if value else None


def with_watched_date(notes: str, watched: str) -> str:
    """Add or replace metadata while leaving the user's actual notes intact."""

    watched = validate_watched_date(watched)
    if re.search(r"(?im)^Watched:[^\n\r]*", notes):
        return re.sub(r"(?im)^Watched:[^\n\r]*", f"Watched: {watched}", notes)
    heading = re.search(r"(?m)^# [^\n\r]*", notes)
    if heading:
        return notes[:heading.end()] + f"\n\nWatched: {watched}" + notes[heading.end():]
    return f"Watched: {watched}\n\n" + notes


def title_key(title: str) -> str:
    """Ignore punctuation and capitalization when matching exported movie titles."""

    return "".join(character for character in title.casefold() if character.isalnum())


def resolve_watched_date(
    movie: str,
    notes: str,
    reviews: list[dict],
    override: str | None = None,
    diary_path: Path = DEFAULT_DIARY_PATH,
    synced_diary_path: Path | None = None,
) -> str:
    """Use explicit metadata first, then require one unambiguous exported date.

    The diary includes movies not yet present in the normalized review examples.
    Never substitute the export's logging date or a file's modification time.
    """

    if override is not None:
        return validate_watched_date(override)
    explicit = parse_watched_date(notes)
    if explicit:
        return explicit
    key = title_key(movie)
    dates = {
        validate_watched_date(row["watched_date"])
        for row in reviews
        if title_key(row.get("movie", "")) == key and row.get("watched_date")
    }
    if diary_path.exists():
        with diary_path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                if title_key(row.get("Name", "")) == key and row.get("Watched Date"):
                    dates.add(validate_watched_date(row["Watched Date"].strip()))
    # The web app keeps new feed dates alongside the untouched original export.
    # Custom export paths get their corresponding data directory automatically.
    synced_diary = synced_diary_path
    if synced_diary is None and diary_path.parent.name == "letterboxd_export" and diary_path.parent.parent.name == "raw":
        synced_diary = diary_path.parents[2] / "diary.json"
    if synced_diary and synced_diary.exists():
        for row in json.loads(synced_diary.read_text(encoding="utf-8")):
            if title_key(row.get("movie", "")) == key and row.get("watched_date"):
                dates.add(validate_watched_date(row["watched_date"]))
    if len(dates) == 1:
        return dates.pop()
    detail = f"Multiple watch dates found: {', '.join(sorted(dates))}." if dates else "No watch date found."
    raise ValueError(f"{movie}: {detail} Add Watched: YYYY-MM-DD to the notes or pass --watched.")


def movie_file_stem(movie: str, watched: str) -> str:
    """Put the watch date before the title so filename sorting is chronological."""

    slug = re.sub(r"[^a-z0-9]+", "-", movie.lower()).strip("-") or "review"
    return f"{validate_watched_date(watched)}-{slug}"


def title_from_filename(path: Path) -> str:
    """Provide a readable fallback when a draft's title heading is missing."""

    stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", path.stem)
    return stem.removesuffix("-review").replace("-", " ").strip().title() or "Review"

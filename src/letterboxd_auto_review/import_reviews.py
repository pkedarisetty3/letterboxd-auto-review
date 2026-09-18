from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path


# These defaults let the script work without arguments when the Letterboxd export
# is placed in the expected folder. They can still be overridden from the command
# line in main().
DEFAULT_EXPORT_DIR = Path("data/raw/letterboxd_export")
DEFAULT_OUTPUT_PATH = Path("data/reviews.json")


@dataclass(frozen=True)
class Review:
    """A cleaned-up version of one row from Letterboxd's reviews.csv file.

    The Letterboxd export stores everything as text. This class gives the rest
    of the project a predictable shape to work with: numbers become numbers,
    yes/no values become booleans, and missing optional values become None.
    """

    logged_date: str
    movie: str
    year: int | None
    letterboxd_uri: str
    rating: float | None
    rewatch: bool
    review: str
    tags: list[str]
    watched_date: str | None


def parse_bool(value: str | None) -> bool:
    """Convert Letterboxd's yes/no text into a real Python boolean."""

    # Treat missing or blank values as False. Letterboxd uses "Yes" for rewatches,
    # so anything other than a case-insensitive "yes" should not count as true.
    return (value or "").strip().lower() == "yes"


def parse_float(value: str | None) -> float | None:
    """Convert a rating string like "4.5" into a float, or None if blank."""

    # CSV files do not have real number types, so ratings arrive as strings.
    # Stripping first also handles values with accidental leading/trailing spaces.
    value = (value or "").strip()
    return float(value) if value else None


def parse_int(value: str | None) -> int | None:
    """Convert a year string like "1999" into an int, or None if blank."""

    value = (value or "").strip()
    return int(value) if value else None


def parse_tags(value: str | None) -> list[str]:
    """Split Letterboxd's comma-separated tag field into a list of clean tags."""

    if not value:
        return []
    # Ignore empty pieces so a value like "horror, , favorite" stays tidy.
    return [tag.strip() for tag in value.split(",") if tag.strip()]


def read_reviews(export_dir: Path) -> list[Review]:
    """Read Letterboxd's reviews.csv and return only rows that contain reviews."""

    reviews_path = export_dir / "reviews.csv"
    if not reviews_path.exists():
        raise FileNotFoundError(f"Could not find {reviews_path}")

    reviews: list[Review] = []
    # utf-8-sig handles normal UTF-8 files and files that start with a BOM
    # character, which some spreadsheet/export tools include at the beginning.
    with reviews_path.open(newline="", encoding="utf-8-sig") as handle:
        # DictReader lets us refer to columns by their header names instead of
        # by fragile numeric positions like row[0] or row[3].
        reader = csv.DictReader(handle)
        for row in reader:
            review_text = (row.get("Review") or "").strip()
            # The drafting tool learns from written reviews. Rows with only a
            # rating would not provide useful writing style examples.
            if not review_text:
                continue

            # Normalize the raw CSV row into the Review dataclass so future code
            # does not need to remember Letterboxd's exact column names.
            reviews.append(
                Review(
                    logged_date=(row.get("Date") or "").strip(),
                    movie=(row.get("Name") or "").strip(),
                    year=parse_int(row.get("Year")),
                    letterboxd_uri=(row.get("Letterboxd URI") or "").strip(),
                    rating=parse_float(row.get("Rating")),
                    rewatch=parse_bool(row.get("Rewatch")),
                    review=review_text,
                    tags=parse_tags(row.get("Tags")),
                    watched_date=(row.get("Watched Date") or "").strip() or None,
                )
            )

    return reviews


def write_reviews(reviews: list[Review], output_path: Path) -> None:
    """Write normalized reviews to JSON for the drafting script to reuse."""

    # Create the output folder if it does not exist yet, such as data/.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # asdict() turns each dataclass object into a plain dictionary that json.dumps
    # knows how to serialize.
    output_path.write_text(
        json.dumps([asdict(review) for review in reviews], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def main() -> None:
    """Command-line entry point for importing a Letterboxd export."""

    parser = argparse.ArgumentParser(description="Normalize Letterboxd reviews into JSON.")
    # Path arguments are converted into pathlib.Path objects automatically.
    parser.add_argument("--export-dir", type=Path, default=DEFAULT_EXPORT_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args()

    # Keep reading and writing separate so each piece can be reused or tested.
    reviews = read_reviews(args.export_dir)
    write_reviews(reviews, args.output)
    print(f"Wrote {len(reviews)} reviews to {args.output}")


if __name__ == "__main__":
    # This makes the file runnable as:
    # python -m letterboxd_auto_review.import_reviews
    main()

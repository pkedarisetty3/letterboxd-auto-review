from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from letterboxd_auto_review.prompts import (
    COVERAGE_REVISION_PROMPT,
    DRAFT_REVIEW_PROMPT,
    STYLE_ANALYSIS_PROMPT,
)


DEFAULT_REVIEWS_PATH = Path("data/reviews.json")
DEFAULT_DRAFTS_DIR = Path("drafts")
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def slugify(value: str) -> str:
    """Turn a movie title into a filesystem-friendly filename piece."""

    # Replace groups of non-letter/non-number characters with dashes. This keeps
    # filenames readable while avoiding punctuation that can be awkward in paths.
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    # If the title contains no usable characters, still return a safe fallback.
    return slug or "review"


def load_reviews(path: Path) -> list[dict]:
    """Load the normalized review examples created by import_reviews.py."""

    if not path.exists():
        raise FileNotFoundError(
            f"Could not find {path}. Run `python -m letterboxd_auto_review.import_reviews` first."
        )
    # The importer writes a JSON list of dictionaries, so json.loads gives us the
    # exact structure the example-selection code expects.
    return json.loads(path.read_text(encoding="utf-8"))


def parse_rating_from_notes(notes: str) -> float | None:
    """Read the user's rating from a Markdown notes file.

    The notes template includes a line like `Rating: 4` near the top. Keep this
    parser forgiving so small human variations like `Rating: 4/5` or
    `Rating: 4.5 stars` still work.
    """

    match = re.search(r"(?im)^rating\s*:\s*([0-5](?:\.\d+)?)", notes)
    if not match:
        return None
    return float(match.group(1))


def extract_required_note_points(notes: str) -> list[str]:
    """Turn a notes Markdown file into concrete points the draft must cover.

    The notes template has headings like `What I liked:` that are useful for the
    human writer but not meaningful review content by themselves. This keeps the
    filled-in lines and removes empty labels so the model sees a numbered
    checklist of the actual opinions, comparisons, and reactions it must preserve.
    """

    points = []
    for raw_line in notes.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        label_match = re.match(r"^([A-Za-z /]+):\s*(.*)$", line)
        if label_match:
            label = label_match.group(1).strip().lower()
            if label == "rating":
                continue
            # Skip empty template labels like `What I liked:`. If a line has a
            # label plus real content, keep only the content after the colon.
            line = label_match.group(2).strip()
            if not line:
                continue

        points.append(line)
    return points


def format_required_note_points(notes: str) -> str:
    """Format required note points for prompts as an easy-to-scan checklist."""

    points = extract_required_note_points(notes)
    if not points:
        return "- None"
    return "\n".join(f"{index}. {point}" for index, point in enumerate(points, start=1))


def keyword_score(notes: str, review: dict, rating: float | None) -> tuple[float, int]:
    """Score how useful an old review is as a style example for new notes.

    Higher scores mean the old review shares more words or a similar rating with
    the user's new movie notes. The review length is included as a tiebreaker so
    richer examples tend to win when relevance is otherwise similar.
    """

    # Search across the movie title, tags, and review text so a matching theme or
    # genre tag can help surface a useful example.
    haystack = " ".join(
        [
            review.get("movie", ""),
            " ".join(review.get("tags", [])),
            review.get("review", ""),
        ]
    ).lower()
    # Only use words with at least four letters. Very short words like "the" and
    # "and" would create noisy matches that do not say much about relevance.
    words = {word for word in re.findall(r"[a-z]{4,}", notes.lower())}
    overlap = sum(1 for word in words if word in haystack)

    rating_bonus = 0.0
    if rating is not None and review.get("rating") is not None:
        # Give up to 2 extra points for examples with a similar rating. A review
        # for a loved movie often has a different tone than one for a mixed movie.
        rating_bonus = max(0.0, 2.0 - abs(float(review["rating"]) - rating))

    review_length = len(review.get("review", ""))
    # Python sorts tuples from left to right. The overlap/rating score is primary,
    # and review_length is secondary.
    return (overlap + rating_bonus, review_length)


def meaningful_note_words(notes: str) -> set[str]:
    """Pick note words that are useful for explaining example selection.

    This mirrors the simple keyword matching used by the selector, but removes
    common review-template labels and filler words so the explanations point to
    recognizable movie-specific details.
    """

    ignored_words = {
        "about",
        "actor",
        "also",
        "because",
        "bigger",
        "could",
        "didnt",
        "doesnt",
        "ending",
        "expectation",
        "film",
        "from",
        "immediate",
        "liked",
        "mention",
        "movie",
        "notes",
        "performances",
        "rating",
        "reaction",
        "reason",
        "review",
        "scenes",
        "stars",
        "still",
        "themes",
        "there",
        "thought",
        "watched",
        "what",
        "when",
        "with",
        "work",
        "would",
    }
    return {
        word
        for word in re.findall(r"[a-z]{4,}", notes.lower())
        if word not in ignored_words
    }


def matching_note_words(notes: str, review: dict, limit: int = 5) -> list[str]:
    """Find note words that also appear in a candidate reference review."""

    haystack = " ".join(
        [
            review.get("movie", ""),
            " ".join(review.get("tags", [])),
            review.get("review", ""),
        ]
    ).lower()
    matches = [word for word in sorted(meaningful_note_words(notes)) if word in haystack]
    return matches[:limit]


def explain_reference_choice(notes: str, rating: float | None, review: dict) -> str:
    """Explain why a past review was selected as a style reference."""

    reasons = []
    matches = matching_note_words(notes, review)
    if matches:
        reasons.append("shared note keywords: " + ", ".join(matches))

    review_rating = review.get("rating")
    if rating is not None and review_rating is not None:
        rating_gap = abs(float(review_rating) - rating)
        if rating_gap == 0:
            reasons.append("same rating as these notes")
        elif rating_gap <= 1:
            reasons.append("similar rating to these notes")

    if not reasons:
        reasons.append("longer past review with useful style signal")
    return "; ".join(reasons)


def select_examples(
    reviews: list[dict],
    notes: str,
    rating: float | None,
    limit: int,
) -> list[dict]:
    """Pick the most relevant past reviews to show the language model."""

    # Sort best-to-worst because the language model has limited context space;
    # sending the strongest examples usually produces a closer style match.
    scored = sorted(
        reviews,
        key=lambda review: keyword_score(notes, review, rating),
        reverse=True,
    )
    return scored[:limit]


def format_examples(examples: list[dict]) -> str:
    """Convert example review dictionaries into readable prompt text."""

    formatted = []
    for index, review in enumerate(examples, start=1):
        # Give each example a small amount of context before the actual review so
        # the model can connect tone with movie/rating information.
        formatted.append(
            "\n".join(
                [
                    f"Example {index}: {review.get('movie')} ({review.get('year')})",
                    f"Rating: {review.get('rating')}",
                    "Review:",
                    review.get("review", "").strip(),
                ]
            )
        )
    return "\n\n---\n\n".join(formatted)


def format_reference_rating(rating: object) -> str:
    """Turn a stored rating value into a short label for the reference list."""

    if rating is None:
        return "Unrated"

    try:
        rating_value = float(rating)
    except (TypeError, ValueError):
        return str(rating)

    # Whole-number ratings look cleaner without a trailing ".0", while half-star
    # and other decimal ratings still keep their useful precision.
    if rating_value.is_integer():
        return f"{int(rating_value)}/5 stars"
    return f"{rating_value:g}/5 stars"


def format_reference_list(examples: list[dict], notes: str, rating: float | None) -> str:
    """Create the audit list of past reviews used as style references."""

    if not examples:
        return "## Referenced Reviews\n\n- None"

    lines = ["## Referenced Reviews", ""]
    for review in examples:
        movie = review.get("movie") or "Unknown movie"
        year = review.get("year")
        reference_rating = format_reference_rating(review.get("rating"))
        reason = explain_reference_choice(notes, rating, review)
        title = f"{movie} ({year})" if year else movie
        lines.append(f"- **{title}**: {reference_rating}")
        lines.append(f"  Reason: {reason}.")
    return "\n".join(lines)


def format_draft_markdown(
    movie: str,
    rating: float | None,
    notes: str,
    review_text: str,
    examples: list[dict],
) -> str:
    """Create the final Markdown file with metadata, review, and references."""

    rating_label = format_reference_rating(rating)
    return "\n\n".join(
        [
            f"# {movie}",
            f"**Rating:** {rating_label}",
            "## Review",
            review_text.strip(),
            format_reference_list(examples, notes, rating),
        ]
    )


def get_env_int(name: str, default: int) -> int:
    """Read an integer setting from the environment, with a default fallback."""

    value = os.getenv(name, "").strip()
    return int(value) if value else default


def get_env_float(name: str, default: float) -> float:
    """Read a float setting from the environment, with a default fallback."""

    value = os.getenv(name, "").strip()
    return float(value) if value else default


def get_env_optional_int(name: str) -> int | None:
    """Read an optional integer setting from the environment."""

    value = os.getenv(name, "").strip()
    return int(value) if value else None


def create_llm_client() -> tuple[OpenAI, str, dict[str, str]]:
    """Create the API client, model name, and optional headers for LLM calls.

    OpenRouter is preferred when OPENROUTER_API_KEY is present. If it is absent,
    the script falls back to calling OpenAI directly with OPENAI_API_KEY.
    """

    openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if openrouter_api_key:
        # The OpenAI Python client can call OpenRouter because OpenRouter exposes
        # an OpenAI-compatible API endpoint.
        client = OpenAI(
            base_url=os.getenv("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL),
            api_key=openrouter_api_key,
        )
        model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
        # OpenRouter recommends these headers for app/site attribution. They are
        # harmless defaults for local use and can be customized in the environment.
        headers = {
            "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "http://localhost"),
            "X-OpenRouter-Title": os.getenv("OPENROUTER_APP_NAME", "letterboxd-auto-review"),
        }
        return client, model, headers

    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if openai_api_key:
        # Direct OpenAI usage does not need the extra OpenRouter attribution headers.
        client = OpenAI(api_key=openai_api_key)
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        return client, model, {}

    # Stop early with a clear setup message instead of failing later during an API call.
    raise RuntimeError(
        "Missing API key. Set OPENROUTER_API_KEY in `.env`, or set OPENAI_API_KEY "
        "if you want to call OpenAI directly."
    )


def call_llm(
    client: OpenAI,
    model: str,
    extra_headers: dict[str, str],
    system_prompt: str,
    user_prompt: str,
) -> str:
    """Send one chat-completion request and return the generated text."""

    max_tokens = get_env_optional_int("LLM_MAX_TOKENS")
    # Keep request construction in one dictionary so optional settings can be
    # added only when they are actually configured.
    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": get_env_float("LLM_TEMPERATURE", 0.7),
    }
    if extra_headers:
        # These are mainly used for OpenRouter attribution.
        request["extra_headers"] = extra_headers
    if max_tokens is not None:
        # If unset, let the provider use its normal default/max behavior.
        request["max_tokens"] = max_tokens

    response = client.chat.completions.create(**request)
    # The SDK may return None for content in unusual cases; normalize that to an
    # empty string so callers can always treat the result as text.
    return response.choices[0].message.content or ""


def draft_review(
    client: OpenAI,
    model: str,
    extra_headers: dict[str, str],
    movie: str,
    rating: float | None,
    notes: str,
    examples: list[dict],
) -> str:
    """Create the first version of a Letterboxd review from the user's notes."""

    # Fill the prompt template with the current movie, the user's rough notes, and
    # selected past reviews that demonstrate the user's writing style.
    prompt = DRAFT_REVIEW_PROMPT.format(
        movie=movie,
        rating=rating if rating is not None else "Not provided",
        notes=notes.strip(),
        note_points=format_required_note_points(notes),
        examples=format_examples(examples),
    )
    return call_llm(client, model, extra_headers, STYLE_ANALYSIS_PROMPT, prompt)


def revise_review_for_note_coverage(
    client: OpenAI,
    model: str,
    extra_headers: dict[str, str],
    movie: str,
    rating: float | None,
    notes: str,
    draft: str,
) -> str:
    """Revise a draft so the final review includes all meaningful note details."""

    # The first model call focuses on creating a coherent review. This second call
    # gives the model one narrower job: compare the draft against the notes and
    # naturally add any missing opinions, comparisons, or reactions.
    prompt = COVERAGE_REVISION_PROMPT.format(
        movie=movie,
        rating=rating if rating is not None else "Not provided",
        notes=notes.strip(),
        note_points=format_required_note_points(notes),
        draft=draft.strip(),
    )
    return call_llm(client, model, extra_headers, STYLE_ANALYSIS_PROMPT, prompt)


def main() -> None:
    """Command-line entry point for drafting a review from a notes file."""

    parser = argparse.ArgumentParser(description="Draft a Letterboxd review from notes.")
    parser.add_argument("--movie", required=True)
    parser.add_argument("--notes", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, default=DEFAULT_REVIEWS_PATH)
    parser.add_argument("--examples", type=int)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_DRAFTS_DIR)
    args = parser.parse_args()

    # Read local inputs first. The notes file contains the user's current thoughts,
    # while reviews.json contains older reviews to use as style references.
    notes = args.notes.read_text(encoding="utf-8")
    rating = parse_rating_from_notes(notes)
    reviews = load_reviews(args.reviews)
    # Load environment variables from .env if present. We do not read .env manually;
    # python-dotenv handles it and os.getenv reads only the requested names.
    load_dotenv()
    client, model, extra_headers = create_llm_client()
    # Command-line --examples wins when provided; otherwise use the environment
    # setting; otherwise fall back to 5.
    example_count = args.examples or get_env_int("REVIEW_EXAMPLE_COUNT", 5)
    examples = select_examples(reviews, notes, rating, example_count)
    draft = draft_review(client, model, extra_headers, args.movie, rating, notes, examples)
    final_review = revise_review_for_note_coverage(
        client,
        model,
        extra_headers,
        args.movie,
        rating,
        notes,
        draft,
    )
    formatted_draft = format_draft_markdown(args.movie, rating, notes, final_review, examples)

    # Store the finished draft in a predictable Markdown file based on the movie title.
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / f"{slugify(args.movie)}-review.md"
    output_path.write_text(formatted_draft + "\n", encoding="utf-8")
    print(f"Wrote draft to {output_path}")


if __name__ == "__main__":
    # This makes the file runnable as:
    # python -m letterboxd_auto_review.draft_review --movie "..." --notes notes.md
    main()

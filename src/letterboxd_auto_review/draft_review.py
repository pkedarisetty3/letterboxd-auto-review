from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from letterboxd_auto_review.feedback import (
    DEFAULT_FEEDBACK_PATH,
    format_feedback_examples,
    load_feedback_records,
)
from letterboxd_auto_review.prompts import (
    COVERAGE_REVISION_PROMPT,
    DRAFT_REVIEW_PROMPT,
    STYLE_ANALYSIS_PROMPT,
)


DEFAULT_REVIEWS_PATH = Path("data/reviews.json")
DEFAULT_DRAFTS_DIR = Path("drafts")
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
WORD_PATTERN = re.compile(r"[a-z]{4,}")
IGNORED_NOTE_WORDS = {
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
    "feels",
    "film",
    "from",
    "good",
    "great",
    "immediate",
    "like",
    "liked",
    "mention",
    "movie",
    "notes",
    "performances",
    "rating",
    "reaction",
    "reason",
    "really",
    "review",
    "scenes",
    "stars",
    "still",
    "themes",
    "there",
    "thing",
    "thought",
    "watched",
    "what",
    "when",
    "with",
    "work",
    "would",
}


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


def parse_movie_from_notes(notes: str) -> str:
    """Read the movie title from the user's Markdown notes.

    The notes template starts with a Markdown heading like `# Movie Title`. This
    also accepts `Movie:` or `Title:` labels so older or quickly typed note files
    can still work without a separate command-line value.
    """

    for raw_line in notes.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        heading_match = re.match(r"^#\s+(.+?)\s*#*$", line)
        if heading_match:
            title = heading_match.group(1).strip()
            if title:
                return title

        label_match = re.match(r"(?i)^(?:movie|title)\s*:\s*(.+)$", line)
        if label_match:
            title = label_match.group(1).strip()
            if title:
                return title

    raise ValueError(
        "Could not find a movie title in the notes. Add it as the first heading, "
        "for example `# Movie Title`, or pass --movie as an override."
    )


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
            if label in {"movie", "rating", "title"}:
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


def review_search_text(review: dict) -> str:
    """Collect the old-review fields that are useful for reference matching."""

    return " ".join(
        [
            review.get("movie", ""),
            " ".join(review.get("tags", [])),
            review.get("review", ""),
        ]
    ).lower()


def text_words(text: str) -> set[str]:
    """Return searchable words from text as a set for whole-word matching."""

    return set(WORD_PATTERN.findall(text.lower()))


def meaningful_note_words(notes: str) -> set[str]:
    """Pick note words that are useful for selecting and explaining examples.

    The notes template and casual movie-writing language contain many broad words
    that are not good evidence of topical similarity. Removing them keeps the
    reference selector focused on more specific details from the current notes.
    """

    return {
        word
        for word in WORD_PATTERN.findall(notes.lower())
        if word not in IGNORED_NOTE_WORDS
    }


def review_document_frequencies(reviews: list[dict]) -> dict[str, int]:
    """Count how many past reviews contain each searchable word.

    Rare overlaps usually say more about a useful reference than words that show
    up across a large chunk of the user's Letterboxd history. These counts let
    the scorer give those rarer matches more weight.
    """

    frequencies: dict[str, int] = {}
    for review in reviews:
        for word in text_words(review_search_text(review)):
            frequencies[word] = frequencies.get(word, 0) + 1
    return frequencies


def keyword_score(
    notes: str,
    review: dict,
    rating: float | None,
    document_frequencies: dict[str, int] | None = None,
    review_count: int = 1,
) -> tuple[float, int, int]:
    """Score how useful an old review is as a style example for new notes.

    Higher scores mean the old review shares specific words or a similar rating
    with the user's new movie notes. Word matches are weighted so common words do
    not cause the same handful of broad reviews to win every time.
    """

    words = meaningful_note_words(notes)
    matches = words & text_words(review_search_text(review))
    frequencies = document_frequencies or {}
    # IDF-style weighting: a shared word that appears in many past reviews still
    # counts, but a more unusual shared word gets a stronger say.
    overlap = sum(
        math.log((review_count + 1) / (frequencies.get(word, 0) + 1)) + 1 for word in matches
    )

    rating_bonus = 0.0
    if rating is not None and review.get("rating") is not None:
        # Rating helps match emotional intensity, but it should not overpower
        # topical relevance and cause the same same-rating examples to repeat.
        rating_bonus = max(0.0, 1.25 - (abs(float(review["rating"]) - rating) * 0.75))

    review_length = len(review.get("review", ""))
    length_bonus = min(review_length / 2000, 1.0) * 0.25
    # Python sorts tuples from left to right. Relevance is primary; the number of
    # matched note words and raw length only break close calls.
    return (overlap + rating_bonus + length_bonus, len(matches), review_length)


def matching_note_words(notes: str, review: dict, limit: int = 5) -> list[str]:
    """Find note words that also appear in a candidate reference review."""

    review_words = text_words(review_search_text(review))
    matches = [word for word in sorted(meaningful_note_words(notes)) if word in review_words]
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

    document_frequencies = review_document_frequencies(reviews)
    review_count = max(len(reviews), 1)
    # Sort best-to-worst because the language model has limited context space;
    # sending the strongest examples usually produces a closer style match.
    scored = sorted(
        reviews,
        key=lambda review: keyword_score(
            notes,
            review,
            rating,
            document_frequencies,
            review_count,
        ),
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


def normalize_review_paragraphs(review_text: str) -> str:
    """Give review text dependable blank-line paragraph breaks.

    The model sometimes returns single newlines between paragraph-like thoughts.
    Markdown preview panes can treat those as soft line breaks, which makes the
    text paste into Letterboxd as one crowded paragraph. Generated reviews are
    not hard-wrapped, so each non-empty line is safest to treat as a paragraph.
    """

    normalized = review_text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    paragraphs = []

    for line in normalized.splitlines():
        stripped_line = line.strip()
        if stripped_line:
            paragraphs.append(stripped_line)

    return "\n\n".join(paragraphs)


def format_letterboxd_review_text(review_text: str) -> str:
    """Create the body that can be pasted directly into Letterboxd.

    Keep Markdown emphasis in place so Letterboxd can render movie-title italics
    after the text is pasted into the review field.
    """

    return normalize_review_paragraphs(review_text)


def fenced_plain_text(text: str) -> str:
    """Wrap text in a Markdown code fence that is safe to copy from preview.

    Copying rendered paragraphs from a Markdown preview can lose the paragraph
    breaks. A fenced block makes the preview expose the exact raw text, including
    blank lines and Markdown emphasis like `*Scream*`.
    """

    longest_backtick_run = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest_backtick_run + 1)
    return f"{fence}text\n{text}\n{fence}"


def markdown_review_to_html(review_text: str) -> str:
    """Render the small Markdown subset used in reviews as HTML paragraphs."""

    paragraphs = []
    for paragraph in normalize_review_paragraphs(review_text).split("\n\n"):
        escaped = html.escape(paragraph)
        # The draft prompts only ask for Markdown italics around other titles, but
        # supporting bold too keeps the preview faithful if the model returns it.
        escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"\*([^*\n]+)\*", r"<em>\1</em>", escaped)
        paragraphs.append(f"<p>{escaped}</p>")
    return "\n".join(paragraphs)


def markdown_review_to_inline_html(review_text: str) -> str:
    """Render review Markdown as inline HTML separated by explicit blank lines.

    Letterboxd's editor can flatten pasted paragraph tags into single line breaks.
    Using literal break tags between paragraphs gives its paste handler the blank
    lines we want while still preserving inline italics.
    """

    paragraphs = []
    for paragraph in normalize_review_paragraphs(review_text).split("\n\n"):
        escaped = html.escape(paragraph)
        escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"\*([^*\n]+)\*", r"<em>\1</em>", escaped)
        paragraphs.append(escaped)
    return "<br><br>\n".join(paragraphs)


def markdown_review_to_plain_text(review_text: str) -> str:
    """Render review Markdown as plain text for clipboard fallback."""

    plain = normalize_review_paragraphs(review_text)
    plain = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", plain)
    plain = re.sub(r"\*([^*\n]+)\*", r"\1", plain)
    return plain


def italic_unicode_char(character: str) -> str:
    """Return a mathematical italic version of a basic Latin letter."""

    if "A" <= character <= "Z":
        return chr(ord("𝐴") + (ord(character) - ord("A")))
    if "a" <= character <= "z":
        # Mathematical italic small h is not contiguous with the rest of the
        # alphabet, so handle it explicitly.
        if character == "h":
            return "ℎ"
        return chr(ord("𝑎") + (ord(character) - ord("a")))
    return character


def markdown_review_to_unicode_text(review_text: str) -> str:
    """Render review Markdown as plain text with visual Unicode italics.

    Letterboxd's normal paste path prefers rich HTML when it exists, but its
    sanitizer collapses paragraph spacing. Copying plain text preserves blank
    lines, and Unicode italics keep simple title emphasis visible.
    """

    def replace_italic(match: re.Match[str]) -> str:
        return "".join(italic_unicode_char(character) for character in match.group(1))

    text = normalize_review_paragraphs(review_text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*\n]+)\*", replace_italic, text)
    return text


def format_copy_preview_html(movie: str, rating: object, review_text: str) -> str:
    """Create an HTML page whose copy button preserves rich formatting.

    The copy button writes plain text directly because Letterboxd's normal rich
    paste path collapses paragraph spacing. Movie-title italics are converted to
    Unicode italic characters so visual emphasis survives in the plain text.
    """

    rating_label = html.escape(format_reference_rating(rating))
    movie_label = html.escape(movie)
    review_html = markdown_review_to_html(review_text)
    clipboard_plain_text = markdown_review_to_unicode_text(review_text)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{movie_label} Letterboxd Copy</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f7f5ef;
      --panel: #fffdf8;
      --text: #202124;
      --muted: #68645d;
      --border: #d8d1c4;
      --accent: #007c7a;
      --accent-text: #ffffff;
    }}

    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #171717;
        --panel: #222222;
        --text: #f3f0e8;
        --muted: #bbb4a8;
        --border: #45413a;
        --accent: #00a6a2;
        --accent-text: #101010;
      }}
    }}

    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.55;
    }}

    main {{
      max-width: 860px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}

    h1 {{
      margin: 0 0 4px;
      font-size: 28px;
      line-height: 1.2;
      letter-spacing: 0;
    }}

    .rating {{
      margin: 0 0 20px;
      color: var(--muted);
      font-size: 14px;
    }}

    .copy-shell {{
      border: 1px solid var(--border);
      background: var(--panel);
    }}

    .copy-bar {{
      position: sticky;
      top: 0;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
    }}

    .copy-label {{
      color: var(--muted);
      font-size: 13px;
    }}

    button {{
      border: 0;
      background: var(--accent);
      color: var(--accent-text);
      min-height: 34px;
      padding: 0 14px;
      font: inherit;
      font-size: 14px;
      cursor: pointer;
    }}

    button:focus-visible {{
      outline: 2px solid currentColor;
      outline-offset: 2px;
    }}

    #review-copy {{
      padding: 16px 18px;
      font-size: 16px;
    }}

    #review-copy p {{
      margin: 0 0 16px;
    }}

    #review-copy p:last-child {{
      margin-bottom: 0;
    }}

    #status {{
      color: var(--muted);
      font-size: 13px;
      min-width: 64px;
      text-align: right;
    }}
  </style>
</head>
<body>
  <main>
    <h1>{movie_label}</h1>
    <p class="rating">{rating_label}</p>
    <section class="copy-shell" aria-label="Letterboxd copy preview">
      <div class="copy-bar">
        <span class="copy-label">Letterboxd copy preview</span>
        <div>
          <button type="button" id="copy-button">Copy</button>
          <span id="status" aria-live="polite"></span>
        </div>
      </div>
      <div id="review-copy">
{review_html}
      </div>
    </section>
  </main>
  <script>
    const button = document.getElementById("copy-button");
    const status = document.getElementById("status");
    const clipboardPlainText = {json.dumps(clipboard_plain_text)};

    async function writeCleanClipboard() {{
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        await navigator.clipboard.writeText(clipboardPlainText);
        return true;
      }}

      return false;
    }}

    function writeCleanEventFallback() {{
      const scratch = document.createElement("textarea");
      scratch.value = clipboardPlainText;
      scratch.setAttribute("readonly", "");
      scratch.style.position = "fixed";
      scratch.style.left = "-9999px";
      document.body.appendChild(scratch);
      scratch.select();

      const handler = (event) => {{
        event.clipboardData.setData("text/plain", clipboardPlainText);
        event.preventDefault();
      }};

      document.addEventListener("copy", handler);
      const copied = document.execCommand("copy");
      document.removeEventListener("copy", handler);
      document.body.removeChild(scratch);
      return copied;
    }}

    button.addEventListener("click", async () => {{
      let copied = false;
      try {{
        copied = await writeCleanClipboard();
      }} catch (error) {{
        copied = false;
      }}
      if (!copied) {{
        copied = writeCleanEventFallback();
      }}

      status.textContent = copied ? "Copied" : "Select text";

      window.setTimeout(() => {{
        status.textContent = "";
      }}, 1800);
    }});
  </script>
</body>
</html>
"""


def format_draft_markdown(
    movie: str,
    rating: float | None,
    notes: str,
    review_text: str,
    generated_review_text: str,
    examples: list[dict],
) -> str:
    """Create the final Markdown file with metadata, review, and references."""

    rating_label = format_reference_rating(rating)
    markdown_review = normalize_review_paragraphs(review_text)
    generated_review = normalize_review_paragraphs(generated_review_text)
    return "\n\n".join(
        [
            f"# {movie}",
            f"**Rating:** {rating_label}",
            "## Markdown Review",
            markdown_review,
            "## Generated Review",
            generated_review,
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
    feedback_examples: str,
) -> str:
    """Create the first version of a Letterboxd review from the user's notes."""

    # Fill the prompt template with the current movie, the user's rough notes, and
    # selected past reviews that demonstrate the user's writing style.
    prompt = DRAFT_REVIEW_PROMPT.format(
        movie=movie,
        rating=rating if rating is not None else "Not provided",
        notes=notes.strip(),
        note_points=format_required_note_points(notes),
        feedback_examples=feedback_examples,
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
    feedback_examples: str,
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
        feedback_examples=feedback_examples,
        draft=draft.strip(),
    )
    return call_llm(client, model, extra_headers, STYLE_ANALYSIS_PROMPT, prompt)


def main() -> None:
    """Command-line entry point for drafting a review from a notes file."""

    parser = argparse.ArgumentParser(description="Draft a Letterboxd review from notes.")
    parser.add_argument("--movie", help="Optional title override. By default, read from notes.")
    parser.add_argument("--notes", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, default=DEFAULT_REVIEWS_PATH)
    parser.add_argument("--examples", type=int)
    parser.add_argument("--feedback", type=Path, default=DEFAULT_FEEDBACK_PATH)
    parser.add_argument("--feedback-examples", type=int)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_DRAFTS_DIR)
    args = parser.parse_args()

    # Read local inputs first. The notes file contains the user's current thoughts,
    # while reviews.json contains older reviews to use as style references.
    notes = args.notes.read_text(encoding="utf-8")
    movie = args.movie or parse_movie_from_notes(notes)
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
    feedback_count = (
        args.feedback_examples
        if args.feedback_examples is not None
        else get_env_int("REVIEW_FEEDBACK_EXAMPLE_COUNT", 3)
    )
    feedback_records = load_feedback_records(args.feedback, feedback_count)
    feedback_examples = format_feedback_examples(feedback_records)
    draft = draft_review(
        client,
        model,
        extra_headers,
        movie,
        rating,
        notes,
        examples,
        feedback_examples,
    )
    final_review = revise_review_for_note_coverage(
        client,
        model,
        extra_headers,
        movie,
        rating,
        notes,
        draft,
        feedback_examples,
    )
    formatted_draft = format_draft_markdown(
        movie,
        rating,
        notes,
        final_review,
        final_review,
        examples,
    )
    copy_preview = format_copy_preview_html(movie, rating, final_review)

    # Store the finished draft files with predictable names based on the movie title.
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_slug = slugify(movie)
    output_path = args.output_dir / f"{output_slug}-review.md"
    copy_preview_path = args.output_dir / f"{output_slug}-letterboxd-copy.html"
    output_path.write_text(formatted_draft + "\n", encoding="utf-8")
    copy_preview_path.write_text(copy_preview, encoding="utf-8")
    print(f"Wrote draft to {output_path}")
    print(f"Wrote Letterboxd copy preview to {copy_preview_path}")


if __name__ == "__main__":
    # This makes the file runnable as:
    # python -m letterboxd_auto_review.draft_review --notes notes.md
    main()

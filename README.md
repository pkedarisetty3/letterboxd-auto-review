# Letterboxd Auto Review

A personal writing assistant for turning quick post-movie notes into a full Letterboxd review in your own voice.

The project starts with your Letterboxd export, normalizes your past written reviews, then uses a few relevant examples as style references while drafting from your rough notes.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env
```

Add your OpenRouter API key to `.env`.

Personal inputs and outputs stay local: `.env`, your Letterboxd export,
`data/reviews.json`, filled-in note files, and generated drafts are ignored by
Git. Only `notes/example_notes.md` and `drafts/.gitkeep` are meant to be
committed from those folders.

## Import Letterboxd Reviews

Your raw Letterboxd export lives here:

```text
data/raw/letterboxd_export/
```

Normalize written reviews into JSON:

```bash
python -m letterboxd_auto_review.import_reviews
```

This creates:

```text
data/reviews.json
```

## Draft A Review

Create a notes file:

```bash
cp notes/example_notes.md notes/my_movie.md
```

Replace the first heading with the movie title and fill in the `Rating:` line;
the drafting script reads both from the notes file.

Then draft:

```bash
python -m letterboxd_auto_review.draft_review \
  --notes notes/my_movie.md
```

Drafts are written to `drafts/` and ignored by Git.
The script writes two files for each movie:

- `movie-review.md` keeps the editable `Markdown Review`, the untouched
  `Generated Review` used for feedback comparison, rating, and referenced
  reviews. The referenced reviews include a short explanation of why each one
  was selected.
- `movie-letterboxd-copy.html` is the easiest paste workflow. Open it in a
  browser and use the Copy button above the review text. That copy keeps
  paragraph breaks for normal paste into Letterboxd. Markdown italics are copied
  as visual Unicode italics, so titles like *Black Mirror* still look emphasized
  without triggering Letterboxd's rich-paste formatting quirks.

If you want to revise a generated review, edit only the `## Markdown Review`
section in `movie-review.md`. The `## Generated Review` section keeps the
model's original version so your edits can be compared later. Then rebuild the
copy page without calling the LLM:

```bash
python -m letterboxd_auto_review.build_copy_preview drafts/movie-review.md
```

This refreshes the matching `movie-letterboxd-copy.html` file from your edited
Markdown text.

## Learn From Your Edits

After you edit the `## Markdown Review` section, save that edit as feedback:

```bash
python -m letterboxd_auto_review.feedback drafts/movie-review.md
```

This appends a local before/after example to:

```text
feedback/review_edits.jsonl
```

Future drafts automatically include the most recent feedback examples in the
prompt, so the model can see how you changed previous drafts before writing the
next one. This is in-context learning, not model fine-tuning: the model weights
do not change, but each new generation gets better guidance from your saved
editing history.

To control how many feedback examples are included:

```bash
python -m letterboxd_auto_review.draft_review \
  --notes notes/my_movie.md \
  --feedback-examples 5
```

## Design Principles

- Your notes are the source of truth.
- The final review should represent every meaningful opinion, comparison, reaction,
  criticism, praise, and conclusion from the notes.
- The review should usually have a clear positive section before a clearly separated
  negative section, without adding labeled pro/con headings.
- Paragraph breaks should follow the user's style, but generated drafts use blank
  lines between every paragraph so copy/paste into Letterboxd does not collapse
  separate thoughts together.
- The model should not invent scenes, facts, opinions, or personal experiences.
- Past reviews are style references, not content sources.
- The review body should avoid naming the movie being reviewed, but may mention
  other movie titles when they come from the notes.
- The first draft should make editing easier, not replace your judgment.

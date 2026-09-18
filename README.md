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

Fill in the `Rating:` line inside the notes file; the drafting script reads it from there.

Then draft:

```bash
python -m letterboxd_auto_review.draft_review \
  --movie "Movie Title" \
  --notes notes/my_movie.md
```

Drafts are written to `drafts/` and ignored by Git.
Each draft includes the movie title, rating, review body, and referenced reviews
as formatted Markdown sections. Referenced reviews include a short explanation
of why each one was selected.

## Design Principles

- Your notes are the source of truth.
- The final review should represent every meaningful opinion, comparison, reaction,
  criticism, praise, and conclusion from the notes.
- The model should not invent scenes, facts, opinions, or personal experiences.
- Past reviews are style references, not content sources.
- The review body should avoid naming the movie being reviewed, but may mention
  other movie titles when they come from the notes.
- The first draft should make editing easier, not replace your judgment.

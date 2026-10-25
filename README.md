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

## Open the Review Desk (Recommended)

On your Mac, open this project folder in Finder and double-click
**Open Review Desk.command**. It opens the app in your browser at
[http://localhost:8765](http://localhost:8765). Keep the launcher’s Terminal window
open while you use it; you do not need to type commands. The launcher reuses the
project’s Python environment and installs the listed dependencies only if missing.
Python 3.10 or newer must already be installed.

Your existing dated movie files appear in the library, newest watched first.

1. Click **+** to start a new viewing. Enter its title, watch date, rating, and notes.
2. Click **Save notes**, or **Generate review** to save notes and draft together.
3. Open **Your review**, edit the draft, and click **Save review**.
4. Click **Copy for Letterboxd**, then paste into Letterboxd. On phone browsers
   that restrict clipboard access, the app offers a manual copy box.
5. Click **Learn from my edits** to save your edited review as feedback.

Notes and reviews are saved only when you click an action, so save before leaving
the page. Generation uses the model/API configuration you already set up. Opening,
browsing, and editing the app do not load your API credentials or call the model.
Regenerating requires confirmation and keeps the previous saved draft in
`drafts/history/`. This folder is ignored by Git along with the rest of your
personal notes and drafts. An old tab cannot silently overwrite newer file edits.

### Sync Letterboxd Without Downloading Exports

Connect your public profile once using its username in the **Letterboxd** box.
The app checks its RSS feed whenever the laptop app starts. Click **Sync
Letterboxd** for an additional refresh while it is running. The box shows sync
progress, the last successful sync, and a retry message if Letterboxd is offline
or temporarily blocks a request. No password or API key is needed for syncing.

- **My writing** shows your local notes and drafts.
- **Watched** shows your dated diary history, newest watched first. Select a
  viewing to fill in a new notes form with its title, watch date, and available
  rating. Existing writing opens directly instead of being replaced. A new
  notes file is created only when you save or generate.

The first sync combines the existing diary export with recent RSS entries.
Following syncs add or update individual entries and retain older history as it
falls out of the feed. Available public reviews also update `data/reviews.json`,
so future drafts can use them as style examples. A recovery copy of the original
review examples is kept in `data/backups/before-letterboxd-sync-reviews.json`.
The original CSV export, your notes, drafts, and feedback files stay unchanged.
Repeated syncing does not duplicate already matched entries. Ambiguous export
matches are left unchanged and reported rather than guessed.

The merged diary is saved in `data/diary.json`; the connected username and last
successful sync are saved in `data/letterboxd_sync.json`. These files and the
backup are ignored by Git. The date resolver used by the terminal workflow also
checks the merged diary.

RSS contains recent film activity rather than a complete account mirror. Older
changes, deletions, private review text, and films merely marked watched without
a dated diary entry are not reliably synchronized. Keep your existing export as
the historical baseline; a new export may still be needed to fill a long gap or
reconcile older changes. When offline, the app keeps using the last saved history.
Syncing does not call the review-generation model or use paid AI credits.

### Use Your Phone

1. Start the app on your laptop and keep the laptop awake.
2. Connect your phone and laptop to the same Wi-Fi.
3. On the laptop page, click **Phone setup**. Type the displayed `http://…:8765`
   address into Safari or Chrome on your phone.
4. Enter the six-digit pairing code shown on your laptop. Bookmark the page on
   your phone for next time. Restarting the app requires pairing again, and the
   laptop’s address may change when you switch networks.

If the phone cannot connect, check that the laptop is awake and the launcher is
still running. Guest Wi-Fi sometimes prevents devices from connecting to each
other. If macOS asks whether Python may accept incoming network connections,
allow it to use the app from your phone. Phone access uses your local Wi-Fi;
there is no hosted account or remote-access setup.

To stop the app, select its Terminal window and press **Control-C**. Double-click
the launcher to start it again. If it reports the port is already in use, close
the other running copy first.

For developers, the laptop-only command is
`python -m letterboxd_auto_review.web --open`. The double-click launcher also
allows phone connections. To use another port, pass `--port 8766`.

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

Replace the first heading with the movie title and fill in `Rating:` and
`Watched:` near the top:

```markdown
# Sliding Doors

Watched: 2026-08-28
Rating: 4
```

Use `YYYY-MM-DD` for the date you watched the movie. If `Watched:` is blank,
the script looks for a unique watch date in `data/reviews.json` and the
Letterboxd `diary.csv` export. If no date is found, or the movie has multiple
watch dates, it asks you to provide one before calling the model. You can also
pass `--watched 2026-08-28` to override the date, and `--diary path/to/diary.csv`
to use a different diary export.

Then draft:

```bash
python -m letterboxd_auto_review.draft_review \
  --notes notes/my_movie.md
```

Drafts are written to `drafts/` and ignored by Git.
After a successful draft, the script saves the resolved `Watched:` field and
renames the input notes to `YYYY-MM-DD-movie-notes.md` in the same folder. It
stops before calling the model if that rename would replace another notes file.
Use the renamed path for subsequent runs.

The script writes two files for each viewing:

- `YYYY-MM-DD-movie-review.md` keeps the editable `Markdown Review`, the untouched
  `Generated Review` used for feedback comparison, rating, and referenced
  reviews. The referenced reviews include a short explanation of why each one
  was selected.
- `YYYY-MM-DD-movie-letterboxd-copy.html` is the easiest paste workflow. Open it in a
  browser and use the Copy button above the review text. That copy keeps
  paragraph breaks and supplies rich text for editors that support formatted
  paste. The plain-text fallback uses ordinary letters and punctuation, without
  formatting tags or special italic characters.

If you want to revise a generated review, edit only the `## Markdown Review`
section in `YYYY-MM-DD-movie-review.md`. The `## Generated Review` section keeps the
model's original version so your edits can be compared later. Then rebuild the
copy page without calling the LLM:

```bash
python -m letterboxd_auto_review.build_copy_preview drafts/2026-08-28-sliding-doors-review.md
```

This refreshes the matching `YYYY-MM-DD-movie-letterboxd-copy.html` file from your edited
Markdown text. The date prefix stays the same.

Movie files stay in `notes/` and `drafts/`, with matching date prefixes:

```text
notes/2026-08-28-sliding-doors-notes.md
drafts/2026-08-28-sliding-doors-review.md
drafts/2026-08-28-sliding-doors-letterboxd-copy.html
```

Sort by filename ascending for chronological order, or descending for newest
first. Different watch dates keep rewatches separate. Re-running the same movie
and date refreshes its draft and copy page, so save any edits you want to retain
before generating again.

## Learn From Your Edits

After you edit the `## Markdown Review` section, save that edit as feedback:

```bash
python -m letterboxd_auto_review.feedback drafts/2026-08-28-sliding-doors-review.md
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

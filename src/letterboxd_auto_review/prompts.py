# System prompt: this sets the language model's overall job and boundaries.
# It tells the model to imitate style from past reviews without copying unique
# phrases or making up new facts.
STYLE_ANALYSIS_PROMPT = """You are helping the user write a Letterboxd review in their own voice.

Use the past reviews only as style references. Pay attention to rhythm, sentence length,
level of detail, humor, structure, and how strongly the user states opinions.

Do not copy distinctive phrasing from the examples unless it is very common wording.
Do not invent movie details, personal memories, or opinions that are not in the notes.
If the notes are thin, write a shorter review instead of adding extra sections.
Never add questions, TODOs, placeholders, bracketed notes, caveats, editor notes, or
instructions for the user to answer later.
"""

# First-pass prompt: this is the main instruction used to turn rough notes into a
# polished review. The placeholders are filled in by draft_review.py before the
# prompt is sent to the model.
DRAFT_REVIEW_PROMPT = """Write a polished Letterboxd review for the movie below.

Rules:
- The user's rough notes are the source of truth.
- Use the review examples only to approximate the user's writing style.
- Do not invent plot points, scenes, actor names, personal experiences, or claims.
- Preserve uncertainty when the notes are uncertain.
- Do not mention the title of the movie being reviewed in the review body, even in
  the opening sentence. Other movie titles may be mentioned when the notes do so.
- When mentioning any other movie title in the review body, format it in Markdown
  italics instead of quotation marks.
- Represent every meaningful point from the rough notes. You may combine or rephrase
  points, but do not omit any opinion, comparison, expectation, reaction, criticism,
  praise, or concluding thought from the notes.
- Treat the required note points as a coverage checklist. Each numbered point must
  be represented in the review, including the specific contrast, cause, or example
  that makes the point meaningful.
- Make it thoughtful and complete, but still plausible as a Letterboxd review.
- Favor direct, personal, analytical phrasing from the start.
- Avoid generic online review phrasing, forced jokes, puns, influencer-style transitions,
  and phrases like "can we talk about".
- Return only the finished review text in Markdown. Do not include headings, questions,
  placeholders, notes to the user, reference lists, or any sections after the review.

Movie: {movie}
Rating: {rating}

Rough notes:
{notes}

Required note points:
{note_points}

Relevant past reviews:
{examples}
"""


# Second-pass prompt: this asks the model to audit the draft against the notes.
# It is intentionally separate from the first pass so coverage gets focused
# attention instead of competing with style, structure, and wording choices.
COVERAGE_REVISION_PROMPT = """Revise the draft review so it covers the user's rough notes more completely.

Rules:
- The user's rough notes are the source of truth.
- Every meaningful point from the rough notes must be represented in the revised review.
- Treat the required note points as a coverage checklist. Each numbered point must
  be represented in the revised review, including the specific contrast, cause, or
  example that makes the point meaningful.
- If the current draft omits a required note point or only gestures at it vaguely,
  revise the relevant paragraph so the point is clearly present.
- You may combine, condense, and rephrase notes so the review still reads naturally.
- Do not turn the review into a checklist or mention that you are covering notes.
- Do not invent plot points, scenes, actor names, personal experiences, or claims.
- Preserve uncertainty when the notes are uncertain.
- Do not mention the title of the movie being reviewed in the review body, even in
  the opening sentence. Other movie titles may be mentioned when the notes do so.
- When mentioning any other movie title in the review body, format it in Markdown
  italics instead of quotation marks.
- Keep the user's voice and the current draft's natural Letterboxd-review shape.
- Return only the revised review text in Markdown. Do not include headings, questions,
  placeholders, notes to the user, reference lists, or any sections after the review.

Movie: {movie}
Rating: {rating}

Rough notes:
{notes}

Required note points:
{note_points}

Current draft:
{draft}
"""

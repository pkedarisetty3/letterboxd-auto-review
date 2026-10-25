"""Check date resolution and filenames without loading credentials or calling an API."""

import tempfile
import unittest
from pathlib import Path

from letterboxd_auto_review.movie_files import (
    movie_file_stem, parse_watched_date, resolve_watched_date,
    title_from_filename, with_watched_date,
)
from letterboxd_auto_review.draft_review import extract_required_note_points, format_draft_markdown
from letterboxd_auto_review.build_copy_preview import output_path_for_draft


class MovieFilesTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.diary = Path(self.folder.name) / "diary.csv"

    def resolve(self, notes="", reviews=None, override=None):
        return resolve_watched_date("Don't Breathe", notes, reviews or [], override, self.diary)

    def test_notes_and_override_take_priority_over_export(self):
        reviews = [{"movie": "Don't Breathe", "watched_date": "2026-08-21"}]
        self.assertEqual(self.resolve("Watched: 2026-10-01", reviews), "2026-10-01")
        self.assertEqual(self.resolve("Watched: invalid", reviews, "2026-10-02"), "2026-10-02")

    def test_diary_supplies_dates_for_movies_without_written_reviews(self):
        self.diary.write_text("Name,Watched Date\nDont Breathe,2026-08-21\n")
        self.assertEqual(self.resolve(), "2026-08-21")

    def test_rewatch_is_ambiguous_even_across_export_sources(self):
        self.diary.write_text("Name,Watched Date\nDon't Breathe,2026-10-01\n")
        with self.assertRaisesRegex(ValueError, "Multiple watch dates"):
            self.resolve(reviews=[{"movie": "Don't Breathe", "watched_date": "2026-08-21"}])

    def test_logged_date_is_not_a_watch_date(self):
        with self.assertRaisesRegex(ValueError, "No watch date"):
            self.resolve(reviews=[{"movie": "Don't Breathe", "logged_date": "2026-08-22"}])

    def test_dates_are_validated_and_blank_field_stays_blank(self):
        self.assertIsNone(parse_watched_date("Watched:\nRating: 4"))
        for value in ["2026-02-30", "10/21/2026", "2026-1-01"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_watched_date(f"Watched: {value}")

    def test_metadata_does_not_become_a_required_review_point(self):
        notes = "# Don't Breathe\n\nRating: 4\n\nWhat I liked: Suspense.\n"
        updated = with_watched_date(notes, "2026-08-21")
        self.assertEqual(with_watched_date(updated, "2026-08-21"), updated)
        self.assertEqual(extract_required_note_points(updated), ["Suspense."])
        self.assertIn("**Watched:** 2026-08-21", format_draft_markdown(
            "Don't Breathe", 4, updated, "Review.", "Review.", []))
        self.assertIn("Rating: 4\n\nWhat I liked: Suspense.", updated)

    def test_preview_and_title_fallback_keep_dated_names_working(self):
        stem = movie_file_stem("Don't Breathe", "2026-08-21")
        path = Path("drafts") / f"{stem}-review.md"
        self.assertEqual(output_path_for_draft(path).name, f"{stem}-letterboxd-copy.html")
        self.assertEqual(title_from_filename(path), "Don T Breathe")


class DraftWorkflowTests(unittest.TestCase):
    def test_successful_draft_dates_notes_and_both_outputs_without_api_calls(self):
        import contextlib
        import io
        import json
        from unittest.mock import patch
        from letterboxd_auto_review import draft_review as workflow

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            notes = root / "movie.md"
            notes.write_text("# Sliding Doors\n\nWatched:\nRating: 4\n\nWhat I liked: The cast.\n")
            reviews = root / "reviews.json"
            reviews.write_text(json.dumps([{"movie": "Sliding Doors", "watched_date": "2026-08-28"}]))
            output = root / "drafts"
            argv = ["draft_review", "--notes", str(notes), "--reviews", str(reviews),
                    "--diary", str(root / "absent.csv"), "--output-dir", str(output),
                    "--feedback", str(root / "absent.jsonl")]
            # Patch the environment loader as well as the model calls: this test
            # must never read local credentials or contact a provider.
            with patch("sys.argv", argv), patch.object(workflow, "load_dotenv"), \
                 patch.object(workflow, "create_llm_client", return_value=(None, "test", {})), \
                 patch.object(workflow, "get_env_int", side_effect=lambda key, default: default), \
                 patch.object(workflow, "select_examples", return_value=[]), \
                 patch.object(workflow, "draft_review", return_value="The cast was great."), \
                 patch.object(workflow, "revise_review_for_note_coverage", return_value="The cast was great."), \
                 contextlib.redirect_stdout(io.StringIO()):
                workflow.main()
            self.assertFalse(notes.exists())
            dated_notes = root / "2026-08-28-sliding-doors-notes.md"
            self.assertIn("Watched: 2026-08-28", dated_notes.read_text())
            self.assertTrue((output / "2026-08-28-sliding-doors-review.md").exists())
            self.assertTrue((output / "2026-08-28-sliding-doors-letterboxd-copy.html").exists())


if __name__ == "__main__":
    unittest.main()

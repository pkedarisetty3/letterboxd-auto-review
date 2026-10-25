"""Verify RSS parsing and incremental history merges without network or API calls."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from letterboxd_auto_review.letterboxd_sync import (
    LetterboxdSync, MAX_FEED_BYTES, load_diary, merge_entries, parse_feed, validate_username,
)
from letterboxd_auto_review.movie_files import resolve_watched_date


def film_item(title='The Fly', watched='2026-10-23', rating='', kind='watch', slug='the-fly-1986', year=1986, review=''):
    return f'''<item><link>https://letterboxd.com/tester/film/{slug}/</link>
    <guid>letterboxd-{kind}-123</guid><pubDate>Sat, 24 Oct 2026 14:09:34 +1300</pubDate>
    <letterboxd:filmTitle>{title}</letterboxd:filmTitle><letterboxd:filmYear>{year}</letterboxd:filmYear>
    <letterboxd:watchedDate>{watched}</letterboxd:watchedDate>
    <letterboxd:memberRating>{rating}</letterboxd:memberRating><letterboxd:rewatch>No</letterboxd:rewatch>
    <description><![CDATA[<p><img src="poster.jpg"/></p>{review or '<p>Watched on Friday October 23, 2026.</p>'}]]></description></item>'''


def feed(*items):
    return ('''<rss version="2.0" xmlns:letterboxd="https://letterboxd.com"><channel>
    <link>https://letterboxd.com/tester/</link>''' + ''.join(items) + '</channel></rss>').encode()


class FeedTests(unittest.TestCase):
    def test_watches_are_not_mistaken_for_reviews_and_lists_are_skipped(self):
        entries, skipped = parse_feed(feed(film_item(), '<item><title>My list</title></item>'), 'tester')
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['watched_date'], '2026-10-23')
        self.assertIsNone(entries[0]['rating'])
        self.assertEqual(entries[0]['review'], '')
        self.assertEqual(skipped, 0)

    def test_review_paragraphs_emphasis_and_entities_survive_without_html(self):
        content = film_item(title='Now You See Me: Now You Don&#039;t', kind='review', rating='4.5',
                            review='<p>A &amp; B. <i>Another Film</i>.</p><p>Next<br/>line.</p><script>ignore me</script>')
        entries, _ = parse_feed(feed(content), 'tester')
        self.assertEqual(entries[0]['movie'], "Now You See Me: Now You Don't")
        self.assertEqual(entries[0]['review'], 'A & B. *Another Film*.\n\nNext\nline.')
        self.assertEqual(entries[0]['rating'], 4.5)

    def test_review_without_diary_date_is_available_as_an_example(self):
        entries, _ = parse_feed(feed(film_item(kind='review', watched='', review='<p>A review.</p>')), 'tester')
        self.assertIsNone(entries[0]['watched_date'])
        self.assertEqual(entries[0]['review'], 'A review.')

    def test_publication_date_never_substitutes_for_watch_date(self):
        entries, _ = parse_feed(feed(film_item(watched='')), 'tester')
        self.assertIsNone(entries[0]['watched_date'])

    def test_wrong_profile_html_and_unsafe_xml_are_rejected(self):
        for content in [b'<html>challenge</html>', b'<!DOCTYPE rss><rss/>', b'<!ENTITY x "y"><rss/>', b'x' * (MAX_FEED_BYTES + 1), feed().replace(b'/tester/', b'/someoneelse/')]:
            with self.subTest(content=content[:40]), self.assertRaises(ValueError):
                parse_feed(content, 'tester')

    def test_invalid_films_do_not_discard_other_valid_entries(self):
        entries, skipped = parse_feed(feed(film_item(), film_item(slug='invalid', watched='2026-02-30')), 'tester')
        self.assertEqual(len(entries), 1)
        self.assertEqual(skipped, 1)
        with self.assertRaises(ValueError):
            parse_feed(feed(film_item(rating='nan')), 'tester')

    def test_username_cannot_be_a_url_or_path(self):
        for username in ['../../.env', 'https://letterboxd.com/tester/', 'user?x=1', '', None]:
            with self.subTest(username=username), self.assertRaises(ValueError):
                validate_username(username)

    def test_duplicate_items_are_not_imported_twice(self):
        entries, _ = parse_feed(feed(film_item(), film_item()), 'tester')
        self.assertEqual(len(entries), 1)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.entry = parse_feed(feed(film_item(kind='review', rating='4', review='<p>Good ending.</p>')), 'tester')[0][0]

    def test_export_short_link_is_matched_and_tags_are_preserved(self):
        existing = [{**self.entry, 'letterboxd_uri': 'https://boxd.it/abc', 'tags': ['favorite']}]
        existing[0].pop('rss_entry_url')
        merged, counts = merge_entries(existing, [self.entry])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['tags'], ['favorite'])
        self.assertEqual(counts['added'], 0)
        self.assertEqual(counts['updated'], 1)
        again, counts = merge_entries(merged, [self.entry])
        self.assertEqual(again, merged)
        self.assertEqual(counts, {'added': 0, 'updated': 0, 'ambiguous': 0})

    def test_newer_watch_becoming_a_review_is_one_entry(self):
        watched = {**self.entry, 'rss_guid': 'letterboxd-watch-456', 'review': '', 'rating': None}
        merged, counts = merge_entries([watched], [self.entry])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['review'], 'Good ending.')
        self.assertEqual(counts['updated'], 1)

    def test_rewatches_and_same_day_distinct_entry_urls_are_retained(self):
        another_day = {**self.entry, 'watched_date': '2026-10-24', 'rss_entry_url': self.entry['rss_entry_url'] + '2/'}
        same_day = {**self.entry, 'rss_entry_url': self.entry['rss_entry_url'] + '3/', 'rewatch': True}
        merged, counts = merge_entries([self.entry], [another_day, same_day])
        self.assertEqual(len(merged), 3)
        self.assertEqual(counts['added'], 2)

    def test_ambiguous_export_rows_are_left_unchanged(self):
        old = {key: value for key, value in self.entry.items() if key != 'rss_entry_url'}
        old['letterboxd_uri'] = 'https://boxd.it/a'
        existing = [old, {**old, 'letterboxd_uri': 'https://boxd.it/b'}]
        merged, counts = merge_entries(existing, [self.entry])
        self.assertEqual(merged, existing)
        self.assertEqual(counts['ambiguous'], 1)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / 'data/raw/letterboxd_export').mkdir(parents=True)
        (self.root / 'data/raw/letterboxd_export/diary.csv').write_text(
            'Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n'
            '2020-01-02,An Older Movie,2000,https://boxd.it/old,3,,classic,2020-01-01\n')
        self.old_reviews = [{'movie': 'An Older Movie', 'year': 2000, 'watched_date': '2020-01-01', 'review': 'Keep this old review.'}]
        (self.root / 'data/reviews.json').write_text(json.dumps(self.old_reviews))
        (self.root / 'notes').mkdir()
        (self.root / 'notes/private-notes.md').write_text('My untouched notes.')
        self.sync = LetterboxdSync(self.root)

    def wait(self):
        for _ in range(200):
            if self.sync.snapshot()['state'] != 'running':
                return self.sync.snapshot()
            threading.Event().wait(.01)
        self.fail('Sync did not finish')

    def run_sync(self, content):
        with patch('letterboxd_auto_review.letterboxd_sync.fetch_feed', return_value=content):
            self.sync.start('tester')
            return self.wait()

    def test_initial_merge_repeat_sync_and_rolling_window_keep_history(self):
        content = feed(film_item(kind='review', review='<p>A new review.</p>'))
        status = self.run_sync(content)
        self.assertEqual(status['state'], 'done')
        self.assertEqual(status['counts']['viewings_added'], 1)
        self.assertEqual(len(load_diary(self.root)), 2)
        self.assertEqual(len(json.loads((self.root / 'data/reviews.json').read_text())), 2)
        self.assertEqual(json.loads((self.root / 'data/backups/before-letterboxd-sync-reviews.json').read_text()), self.old_reviews)
        repeated = self.run_sync(content)
        self.assertEqual(repeated['counts']['viewings_added'], 0)
        self.assertEqual(repeated['counts']['reviews_added'], 0)
        self.assertEqual(repeated['counts']['reviews_updated'], 0)
        self.run_sync(feed())
        self.assertEqual(len(load_diary(self.root)), 2)
        self.assertEqual((self.root / 'notes/private-notes.md').read_text(), 'My untouched notes.')
        self.assertEqual(LetterboxdSync(self.root).snapshot()['username'], 'tester')

    def test_failure_preserves_saved_history_and_is_retryable(self):
        self.run_sync(feed(film_item()))
        before = (self.root / 'data/diary.json').read_bytes()
        with patch('letterboxd_auto_review.letterboxd_sync.fetch_feed', side_effect=ValueError('Offline.')):
            self.sync.start()
            status = self.wait()
        self.assertEqual(status['state'], 'error')
        self.assertEqual((self.root / 'data/diary.json').read_bytes(), before)
        self.assertIn('last_success', status)
        self.assertEqual(self.run_sync(feed(film_item()))['state'], 'done')

    def test_damaged_local_reviews_are_not_overwritten(self):
        reviews = self.root / 'data/reviews.json'
        reviews.write_text('damaged data')
        status = self.run_sync(feed(film_item()))
        self.assertEqual(status['state'], 'error')
        self.assertEqual(reviews.read_text(), 'damaged data')
        self.assertFalse((self.root / 'data/diary.json').exists())

    def test_background_fetch_is_nonblocking_and_only_one_runs(self):
        started, finish = threading.Event(), threading.Event()
        def fetch(username):
            started.set()
            finish.wait(3)
            return feed(film_item())
        with patch('letterboxd_auto_review.letterboxd_sync.fetch_feed', side_effect=fetch) as request:
            self.sync.start('tester')
            self.assertTrue(started.wait(2))
            self.assertEqual(self.sync.start('tester')['state'], 'running')
            self.assertEqual(len(self.sync.watched()), 1)
            request.assert_called_once()
            finish.set()
            self.assertEqual(self.wait()['state'], 'done')

    def test_another_account_cannot_mix_into_this_history(self):
        self.run_sync(feed(film_item()))
        with self.assertRaisesRegex(ValueError, 'another profile'):
            self.sync.start('anotheruser')

    def test_watch_dates_resolve_from_synced_diary_for_cli(self):
        self.run_sync(feed(film_item()))
        result = resolve_watched_date('The Fly', '', [], diary_path=self.root / 'data/raw/letterboxd_export/diary.csv')
        self.assertEqual(result, '2026-10-23')

    def test_undated_reviews_dont_invent_a_diary_entry(self):
        self.run_sync(feed(film_item(kind='review', watched='', review='<p>No date.</p>')))
        self.assertEqual(len(load_diary(self.root)), 1)
        self.assertEqual(len(json.loads((self.root / 'data/reviews.json').read_text())), 2)


if __name__ == '__main__':
    unittest.main()

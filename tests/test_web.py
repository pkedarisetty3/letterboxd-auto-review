"""Exercise the local UI's file workflow with fake model calls and disposable files."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from letterboxd_auto_review import draft_review as drafting
from letterboxd_auto_review.web import Conflict, ReviewDesk


class ReviewDeskTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / 'data').mkdir()
        (self.root / 'data/reviews.json').write_text('[]')
        self.desk = ReviewDesk(self.root)
        self.data = {'title': 'Sliding Doors', 'watched': '2026-08-28',
                     'rating': '3.5', 'notes': 'What I liked: The two timelines.\n\nA great ending.'}

    def new_movie(self):
        return self.desk.save_notes(self.data)

    def add_draft(self, item):
        notes, draft, _ = self.desk.paths(item['id'])
        draft.parent.mkdir(exist_ok=True)
        draft.write_text(drafting.format_draft_markdown(
            item['title'], item['rating'], notes.read_text(), 'Original review.', 'Original review.', []))
        return self.desk.read(item['id'])

    def test_create_reload_and_newest_first(self):
        first = self.new_movie()
        second = self.desk.save_notes({**self.data, 'watched': '2026-09-01'})
        self.assertEqual(first['id'], '2026-08-28-sliding-doors')
        self.assertEqual(first['notes'], self.data['notes'])
        self.assertEqual([item['id'] for item in self.desk.library()], [second['id'], first['id']])
        with self.assertRaises(Conflict):
            self.new_movie()

    def test_invalid_input_and_path_traversal_are_rejected(self):
        for movie_id in ['../../.env', '../notes', '/tmp/test', '', 'a/b', '.env']:
            with self.subTest(movie_id=movie_id), self.assertRaises(ValueError):
                self.desk.read(movie_id)
        for values in [{'watched': '2026-02-30'}, {'rating': '9'}, {'rating': '3.7'}, {'rating': 'nan'}, {'title': 'x\ny'}]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.desk.save_notes({**self.data, **values})

    def test_stale_tab_cannot_replace_newer_notes(self):
        item = self.new_movie()
        updated = self.desk.save_notes({**self.data, **item, 'notes': 'Newer thoughts.'})
        with self.assertRaises(Conflict):
            self.desk.save_notes({**self.data, **item, 'notes': 'Outdated thoughts.'})
        self.assertEqual(self.desk.read(item['id'])['notes'], updated['notes'])

    def test_review_save_keeps_baseline_and_refreshes_html_and_feedback(self):
        item = self.add_draft(self.new_movie())
        updated = self.desk.save_review({**item, 'review': 'My edited *title*.\n\nAnother paragraph.'})
        _, path, preview = self.desk.paths(item['id'])
        self.assertIn('## Generated Review\n\nOriginal review.', path.read_text())
        self.assertIn('Another paragraph.', preview.read_text())
        self.assertNotEqual(item['revision'], updated['revision'])
        self.desk.feedback(updated)
        record = json.loads((self.root / 'feedback/review_edits.jsonl').read_text())
        self.assertEqual(record['generated_review'], 'Original review.')
        self.assertIn('My edited', record['edited_review'])
        self.assertIn('Another paragraph.', updated['copy_text'])

    def test_rating_update_does_not_replace_the_review(self):
        item = self.add_draft(self.new_movie())
        updated = self.desk.save_notes({**item, 'rating': '4.5'})
        _, draft, preview = self.desk.paths(item['id'])
        self.assertIn('**Rating:** 4.5/5', draft.read_text())
        self.assertEqual(updated['review'], 'Original review.')
        self.assertIn('4.5/5', preview.read_text())

    def test_letterboxd_copy_uses_native_italics_and_keeps_paragraphs(self):
        item = self.add_draft(self.new_movie())
        review = "Films like *Mean Girls* and *Pitch Perfect*.\n\nMaitreyi's **fun** performance & good company."
        updated = self.desk.save_review({**item, 'review': review})
        expected = "Films like Mean Girls and Pitch Perfect.\n\nMaitreyi's fun performance & good company."
        self.assertEqual(updated['copy_text'], expected)
        self.assertIn('<em>Mean Girls</em>', updated['copy_html'])
        self.assertIn('</p>\n\n<p><br></p>\n\n<p>', updated['copy_html'])
        self.assertIn('color: #99aabb;', updated['copy_html'])
        self.assertNotIn('<em>', updated['copy_text'])
        self.assertNotIn('&#', updated['copy_text'])
        self.assertEqual(updated['review'], review)
        _, _, preview = self.desk.paths(item['id'])
        self.assertIn(json.dumps(expected), preview.read_text())

    def test_generation_is_asynchronous_and_preserves_previous_draft(self):
        item = self.add_draft(self.new_movie())
        with self.assertRaises(Conflict):
            self.desk.generate(item)
        started, finish = threading.Event(), threading.Event()
        def fake_generate(*args):
            started.set()
            finish.wait(3)
            return 'A new review.', []
        with patch.object(drafting, 'generate_review', side_effect=fake_generate):
            self.desk.generate({**item, 'replace': True})
            self.assertTrue(started.wait(2))
            self.assertEqual(self.desk.job['state'], 'running')
            self.assertEqual(len(self.desk.library()), 1)
            with self.assertRaises(Conflict):
                self.desk.save_notes(item)
            finish.set()
            self.wait_for_job()
        self.assertEqual(self.desk.job['state'], 'done')
        self.assertEqual(self.desk.read(item['id'])['review'], 'A new review.')
        archive = list((self.root / 'drafts/history').glob('*.md'))
        self.assertEqual(len(archive), 1)
        self.assertIn('Original review.', archive[0].read_text())

    def wait_for_job(self):
        # Poll only in the isolated test; the real browser polls the job endpoint.
        for _ in range(200):
            if self.desk.job['state'] != 'running':
                return
            threading.Event().wait(.01)
        self.fail('Generation did not finish')

    def test_provider_error_is_sanitized_and_notes_are_preserved(self):
        item = self.new_movie()
        with patch.object(drafting, 'generate_review', side_effect=RuntimeError('private provider details')):
            self.desk.generate(item)
            self.wait_for_job()
        self.assertEqual(self.desk.job['state'], 'error')
        self.assertNotIn('private provider details', self.desk.job['message'])
        self.assertEqual(self.desk.read(item['id'])['notes'], item['notes'])

    def test_empty_template_does_not_make_model_calls(self):
        item = self.desk.save_notes({**self.data, 'notes': 'What I liked:\n\nImmediate reaction:'})
        with patch.object(drafting, 'generate_review') as generate:
            with self.assertRaisesRegex(ValueError, 'Add your thoughts'):
                self.desk.generate(item)
            generate.assert_not_called()

    def test_on_disk_edit_during_generation_is_preserved(self):
        item = self.new_movie()
        started, finish = threading.Event(), threading.Event()
        def fake_generate(*args):
            started.set()
            finish.wait(3)
            return 'New draft.', []
        with patch.object(drafting, 'generate_review', side_effect=fake_generate):
            self.desk.generate(item)
            self.assertTrue(started.wait(2))
            notes, draft, _ = self.desk.paths(item['id'])
            notes.write_text(notes.read_text() + '\nAn external edit.\n')
            finish.set()
            self.wait_for_job()
        self.assertEqual(self.desk.job['state'], 'error')
        self.assertIn('external edit', notes.read_text())
        self.assertFalse(draft.exists())

    def test_symlink_is_not_read_or_written(self):
        notes = self.root / 'notes'
        notes.mkdir()
        outside = self.root / 'outside.md'
        outside.write_text('Do not change.')
        (notes / '2026-08-28-sliding-doors-notes.md').symlink_to(outside)
        with self.assertRaises(ValueError):
            self.new_movie()
        self.assertEqual(outside.read_text(), 'Do not change.')


class HttpBoundaryTests(unittest.TestCase):
    """Use in-memory HTTP requests so tests do not need a listening network socket."""

    def setUp(self):
        import io
        from types import SimpleNamespace
        self.io = io
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.desk = ReviewDesk(Path(self.folder.name))
        self.server = SimpleNamespace(desk=self.desk, server_port=8765, server_address=('0.0.0.0', 8765))

    def request(self, path, method='GET', body=None, host='localhost:8765', peer='127.0.0.1', headers=None):
        from letterboxd_auto_review.web import Handler
        payload = json.dumps(body).encode() if body is not None else b''
        fields = {'Host': host, 'Content-Length': str(len(payload)), **(headers or {})}
        raw = f'{method} {path} HTTP/1.0\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in fields.items()) + '\r\n'
        stream = self.io.BytesIO(raw.encode() + payload)
        class FakeSocket:
            output = bytearray()
            def makefile(self, *args):
                return stream
            def sendall(self, value):
                self.output.extend(value)
        connection = FakeSocket()
        Handler(connection, (peer, 1234), self.server)
        head, content = bytes(connection.output).split(b'\r\n\r\n', 1)
        status = int(head.split(b' ', 2)[1])
        return status, head, content

    def test_phone_requires_pairing_and_cannot_read_pairing_code(self):
        self.assertEqual(self.request('/api/library', host='192.168.1.10:8765', peer='192.168.1.11')[0], 401)
        headers = {'X-Review-Desk': '1', 'Origin': 'http://192.168.1.10:8765'}
        status, head, _ = self.request('/api/pair', 'POST', {'code': self.desk.pairing_code},
                                      host='192.168.1.10:8765', peer='192.168.1.11', headers=headers)
        self.assertEqual(status, 200)
        cookie = next(line for line in head.decode().split('\r\n') if line.startswith('Set-Cookie:')).split(': ', 1)[1].split(';', 1)[0]
        phone = {'host': '192.168.1.10:8765', 'peer': '192.168.1.11', 'headers': {'Cookie': cookie}}
        self.assertEqual(self.request('/api/library', **phone)[0], 200)
        self.assertEqual(self.request('/api/phone', **phone)[0], 403)

    def test_project_files_are_never_served(self):
        for path in ['/.env', '/data/reviews.json', '/notes/a.md', '/../.env', '/static/../../.env']:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_cross_site_writes_and_rebound_hosts_are_rejected(self):
        self.assertEqual(self.request('/api/save-notes', 'POST', {})[0], 403)
        self.assertEqual(self.request('/api/save-notes', 'POST', {}, headers={
            'X-Review-Desk': '1', 'Origin': 'https://another-site.example'})[0], 403)
        self.assertEqual(self.request('/api/library', host='another-site.example:8765')[0], 401)

    def test_assets_and_valid_local_save(self):
        self.assertEqual(self.request('/')[0], 200)
        self.assertEqual(self.request('/app.js')[0], 200)
        status, _, content = self.request('/api/save-notes', 'POST', {
            'title': 'Test Movie', 'watched': '2026-10-24', 'rating': 4, 'notes': 'A good ending.'
        }, headers={'X-Review-Desk': '1', 'Origin': 'http://localhost:8765'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)['id'], '2026-10-24-test-movie')

    def test_watched_picker_links_existing_writing_without_creating_files(self):
        item = self.desk.save_notes({'title': 'The Fly', 'watched': '2026-10-23', 'rating': 4, 'notes': 'My notes.'})
        (self.desk.root / 'data').mkdir(exist_ok=True)
        (self.desk.root / 'data/diary.json').write_text(json.dumps([
            {'movie': 'The Fly', 'year': 1986, 'watched_date': '2026-10-23', 'rating': 4,
             'letterboxd_uri': 'https://letterboxd.com/tester/film/the-fly/', 'review': 'Not returned to the picker'},
            {'movie': 'Another Movie', 'year': 2026, 'watched_date': '2026-10-24', 'rating': None},
        ]))
        before = list((self.desk.root / 'notes').glob('*.md'))
        status, _, content = self.request('/api/library')
        self.assertEqual(status, 200)
        result = json.loads(content)
        self.assertEqual(len(result['watched']), 2)
        self.assertEqual(result['watched'][1]['local_id'], item['id'])
        self.assertIsNone(result['watched'][0]['local_id'])
        self.assertNotIn('review', result['watched'][1])
        self.assertEqual(before, list((self.desk.root / 'notes').glob('*.md')))

    def test_sync_routes_require_authentication_and_validate_username(self):
        self.assertEqual(self.request('/api/sync-status', host='192.168.1.10:8765', peer='192.168.1.11')[0], 401)
        self.assertEqual(self.request('/api/sync', 'POST', {'username': 'tester'},
            host='192.168.1.10:8765', peer='192.168.1.11', headers={'X-Review-Desk': '1'})[0], 401)
        with patch('letterboxd_auto_review.letterboxd_sync.fetch_feed') as fetch:
            self.assertEqual(self.request('/api/sync', 'POST', {'username': '../invalid'}, headers={'X-Review-Desk': '1'})[0], 400)
            fetch.assert_not_called()
        self.assertEqual(self.request('/api/sync-status')[0], 200)


if __name__ == '__main__':
    unittest.main()

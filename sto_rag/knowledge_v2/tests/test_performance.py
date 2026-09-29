import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from knowledge_v2.performance import session, span, summary, path_for, http_measured, observe


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'timings.jsonl'

    def test_nested_times_do_not_double_count(self):
        with patch('knowledge_v2.performance.time.perf_counter', side_effect=[0, 1, 2, 5, 7, 8]):
            with session(self.path), span('outer'):
                with span('inner'):
                    pass
        result = summary(self.path)
        self.assertEqual(result['wall_seconds'], 8)
        self.assertEqual(result['phases']['outer']['seconds'], 6)
        self.assertEqual(result['phases']['outer']['exclusive_seconds'], 3)
        self.assertEqual(result['phases']['inner']['exclusive_seconds'], 3)
        self.assertFalse(result['incomplete'])

    def test_resume_appends_sessions_and_keeps_errors(self):
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'private document text'):
                with session(self.path), span('check'):
                    raise ValueError('private document text')
        result = summary(self.path)
        self.assertEqual(result['sessions'], 2)
        self.assertEqual(result['completed_sessions'], 2)
        self.assertEqual(result['phases']['check']['errors'], 2)
        self.assertNotIn('private document text', self.path.read_text())

    def test_http_preserves_result_without_logging_payload(self):
        class Client:
            @http_measured
            def http(self, path, payload):
                return dict(usage={'prompt_tokens': 11}, timings={'prompt_ms': 23}, secret=payload)
        with session(self.path):
            result = Client().http('/v1/chat/completions', 'PRIVATE CONTENT')
            observe('bad', float('nan'))
        self.assertEqual(result['secret'], 'PRIVATE CONTENT')
        self.assertNotIn('PRIVATE CONTENT', self.path.read_text())
        data = summary(self.path)
        self.assertEqual(data['observations']['model.prompt_tokens']['sum'], 11)
        self.assertEqual(data['observations']['server.prompt_ms']['sum'], 23)
        self.assertNotIn('bad', data['observations'])

    def test_unwritable_diagnostics_do_not_change_application_result(self):
        with patch.object(Path, 'open', side_effect=PermissionError):
            with session(self.path), span('work'):
                result = 42
        self.assertEqual(result, 42)

    def test_interrupted_or_damaged_session_is_visible(self):
        self.path.write_text(json.dumps(dict(session='crash', kind='start'))+'\n{"broken"')
        self.assertTrue(summary(self.path)['incomplete'])
        self.assertFalse(summary(self.path.with_suffix('.missing'))['available'])

    def test_job_identity_cannot_escape_output_directory(self):
        output = path_for(Path(self.directory.name), '../../private')
        self.assertEqual(output.parent, Path(self.directory.name)/'performance')
        self.assertNotIn('private', output.name)

    def test_disabled_span_has_no_files(self):
        with span('work'):
            observe('number', 1)
        self.assertFalse(self.path.exists())

    def test_queue_wait_is_separate_from_owned_time_and_ticket_is_released(self):
        from knowledge_v2.model_queue import model_turn
        from knowledge_v2.store import KnowledgeStore
        from types import SimpleNamespace
        store=KnowledgeStore(Path(self.directory.name)/'store')
        client=SimpleNamespace(timeout=30)
        with session(self.path):
            with self.assertRaises(ValueError):
                with model_turn(store,client):
                    self.assertTrue(client._model_ticket)
                    raise ValueError('private failure')
        data=summary(self.path)
        self.assertEqual(data['phases']['queue.wait']['errors'],0)
        self.assertEqual(data['phases']['queue.held']['errors'],1)
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM model_tickets').fetchone()[0],0)
        self.assertIsNone(client._model_ticket)

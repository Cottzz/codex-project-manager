import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_bridge import BridgeError, discover, metadata, preview_from_meta, message
from service import Store, Conflict
from workspace import Workspace


class ProjectDiscoveryTest(unittest.TestCase):
    def test_preview_late_text_and_async_question_not_answer(self):
        raw = [{'type': 'commandExecution', 'id': f'tool-{i}'} for i in range(45)]
        raw += [{'type': 'agentMessage', 'id': 'question', 'text': 'Choose\n- Accepted',
                 'questions': [{'title': 'Choose', 'options': ['Accepted']}], 'delivery': 'async'},
                {'type': 'agentMessage', 'id': 'final', 'text': 'Not verified'}]
        with patch('codex_bridge.request', return_value={'data': [{'id': 'turn', 'items': raw}]}):
            preview = preview_from_meta({'id': 'thread'}, 'thread')
        turn = preview['turns'][0]
        self.assertEqual([i['id'] for i in turn['items']], ['question', 'final'])
        self.assertEqual(turn['items'][0]['message_kind'], 'assistant_question')
        self.assertFalse(turn['text_items_truncated'])
        self.assertEqual(turn['raw_items_observed'], 47)

    def test_preview_text_cap_and_oversized_fallback_are_incomplete(self):
        raw = [{'type': 'agentMessage', 'id': str(i), 'text': 'text'} for i in range(31)]
        with patch('codex_bridge.request', return_value={'data': [{'id': 'turn', 'items': raw}]}):
            preview = preview_from_meta({'id': 'thread'}, 'thread')
        self.assertEqual(len(preview['turns'][0]['items']), 30)
        self.assertTrue(preview['turns'][0]['text_items_truncated'])
        self.assertIn('不能补齐', preview['warning'])
        with patch('codex_bridge.request', side_effect=[BridgeError('超过预览上限'),
                {'data': [{'id': 'turn'}]}, {'data': [], 'nextCursor': 'more-items'}]):
            fallback = preview_from_meta({'id': 'thread'}, 'thread')
        self.assertTrue(fallback['turns'][0]['text_items_truncated'])
        self.assertEqual(fallback['turns'][0]['items_next_cursor'], 'more-items')

    def test_exact_original_retains_question_classification(self):
        item = {'id': 'question', 'type': 'agentMessage', 'text': 'Allow?',
                'questions': [{'title': 'Allow?', 'options': ['Allow']}], 'delivery': 'async'}
        with patch('codex_bridge.metadata', return_value={'path': 'fixture'}), \
                patch('codex_bridge.request', return_value={'data': [{'turnId': 'turn', 'item': item}]}):
            original = message(self.home, 'thread', 'turn', 'question')
        self.assertEqual(original['message_kind'], 'assistant_question')
        self.assertEqual(original['role'], 'assistant')
        self.assertEqual(original['text'], 'Allow?')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = Store(root / 'home')
        self.home.root.mkdir()
        self.home.init('home', 'home goal')
        self.ws = Workspace(self.home)

    def test_preview_attach_alias_and_missing_directory(self):
        target = Path(self.tmp.name) / 'new'
        target.mkdir()
        alias = Path(self.tmp.name) / 'alias'
        alias.symlink_to(target, target_is_directory=True)
        self.assertEqual(self.ws.attachment_preview(str(alias))['action'], 'initialize')
        attached = self.ws.attach(str(alias), 'new', 'new goal')
        self.assertEqual(attached['root'], str(target.resolve()))
        self.assertEqual(self.ws.attachment_preview(str(target))['action'], 'already_registered')
        self.assertEqual(self.ws.attach(str(target))['id'], attached['id'])
        self.assertEqual(len(self.ws.entries()), 2)
        with self.assertRaises(Conflict):
            self.ws.attachment_preview(str(Path(self.tmp.name) / 'absent'))

    def test_thread_cwd_boundary_and_historical_alias(self):
        task = self.home.call('task_create', {'title': 'task', 'body': 'scope', 'request_id': 'task'})
        self.home.call('thread_link', {'title': '旧标题', 'thread_id': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                                       'task_id': task['id'], 'request_id': 'thread'})
        def fake(method, params, timeout=15):
            if method == 'thread/list':
                return {'data': [], 'nextCursor': None}
            return {'thread': {'id': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                               'cwd': str(self.home.root), 'name': '新标题', 'source': 'vscode',
                               'status': {'type': 'notLoaded'}}}
        with patch('codex_bridge.request', side_effect=fake):
            found = discover(self.home, '旧标题')
            self.assertEqual(len(found['threads']), 1)
            self.assertIn('历史登记名', found['threads'][0]['title'])
            self.assertFalse(found['threads'][0]['control_verified'])
        with patch('codex_bridge.request', return_value={'thread': {'cwd': str(Path(self.tmp.name) / 'other')}}):
            with self.assertRaises(BridgeError):
                metadata(self.home, 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa')

    def test_unlink_keeps_thread_and_other_task_links(self):
        first = self.home.call('task_create', {'title': 'first', 'body': 'scope', 'request_id': 'first'})
        second = self.home.call('task_create', {'title': 'second', 'body': 'scope', 'request_id': 'second'})
        thread = self.home.call('thread_link', {'title': 'chat', 'thread_id': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                                                'task_id': first['id'], 'request_id': 'chat'})
        edge = self.home.call('relation_add', {'source_id': second['id'], 'target_id': thread['id'],
                                               'relation': 'executed_by', 'basis': 'other task', 'request_id': 'edge'})
        self.home.call('relation_remove', {'id': edge['id'], 'basis': 'wrong task', 'request_id': 'unlink'})
        self.assertEqual(self.home.call('graph_get', {'id': thread['id']})['node']['data']['thread_id'],
                         'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa')
        remaining = self.home.call('graph_get', {'id': thread['id']})['edges']
        self.assertTrue(any(e['source'] == first['id'] and e['relation'] == 'executed_by' for e in remaining))
        self.assertFalse(any(e['source'] == second['id'] and e['relation'] == 'executed_by' for e in remaining))


if __name__ == '__main__':
    unittest.main()

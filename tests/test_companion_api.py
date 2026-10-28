"""Real isolated engine + real HTTP, without external model credentials."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import companion_api as api

NOW = int(datetime(2026, 10, 28, 10, tzinfo=timezone.utc).timestamp() * 1000)

def record(i, text, at=NOW):
    return dict(id=i, text=text, source='turn-' + i, at=at, pinned=False)

def request(incoming=(), state=None, query='花生', now=NOW):
    return dict(protocol=1, now=now, utc_offset_minutes=480, query=query,
                state=state or dict(memories=[], archive=[], audit=[]), incoming=list(incoming), semantic=False, judge=False)

class CompanionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old = {k: v for k, v in os.environ.items() if k.startswith(('OPENAI_', 'MEMORY_'))}
        for key in cls.old:
            os.environ.pop(key)
        cls.server = api.make_server('127.0.0.1', 0, 'x' * 32)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        os.environ.update(cls.old)
    def http(self, path, payload=None, token='x' * 32):
        data = None if payload is None else json.dumps(payload).encode()
        return urllib.request.urlopen(urllib.request.Request(self.url + path, data=data, headers={'Authorization': 'Bearer ' + token}), timeout=30)
    def test_auth_and_health(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.http('/health', token='wrong')
        self.assertEqual(401, error.exception.code)
        with self.http('/health') as response:
            self.assertEqual('request-scoped', json.load(response)['storage'])
    def test_http_real_engine(self):
        with self.http('/v1/evaluate', request([record('one', '我对花生过敏')])) as response:
            result = json.load(response)
        self.assertEqual('我对花生过敏', result['state']['memories'][0]['text'])
        self.assertIn('one', result['hits'])
    def test_correction_and_historical_provenance(self):
        first = api.run_isolated(request([record('old', '记住，我对花生过敏，很严重')]))
        result = api.run_isolated(request([record('new', '更正：我对花生不过敏了')], first['state']))
        self.assertEqual(['new'], [r['id'] for r in result['state']['memories']])
        old = result['state']['archive'][0]
        self.assertEqual('new', old['engine']['superseded_by'])
        self.assertEqual('turn-old', old['source'])
        self.assertNotIn('old', result['hits'])
    def test_distinct_people_and_allergens_do_not_merge(self):
        for first, second in [('张三喜欢打篮球', '李四喜欢打篮球'), ('我对花生过敏', '我对花粉过敏')]:
            result = api.run_isolated(request([record('a', first), record('b', second)]))
            self.assertEqual(2, len(result['state']['memories']))
    def test_merge_keeps_quote_and_all_sources(self):
        result = api.run_isolated(request([record('a', '我喜欢跑步'), record('b', '我喜欢跑步，每周跑三次')], query='跑步'))
        rows = result['state']['memories']
        self.assertEqual(1, len(rows))
        self.assertEqual('我喜欢跑步，每周跑三次', rows[0]['text'])
        self.assertEqual({'turn-a', 'turn-b'}, set(rows[0]['sources']))
    def test_original_quote_and_observation_timezone(self):
        observed = int(datetime(2026, 10, 28, 17, tzinfo=timezone.utc).timestamp() * 1000)
        result = api.run_isolated(request([record('a', '我明天有个重要考试', observed)], now=observed + 86400000))
        row = result['state']['memories'][0]
        self.assertEqual('我明天有个重要考试', row['text'])
        self.assertEqual('2026-10-30', row['engine']['event_date'])
        self.assertEqual('2026-10-30T16:00:00+00:00', row['engine']['expires_at'])
    def test_expiration_on_read_without_new_capture(self):
        first = api.run_isolated(request([record('a', '我明天有个重要考试')], query='考试'))
        result = api.run_isolated(request(state=first['state'], query='考试', now=NOW + 3 * 86400000))
        self.assertEqual([], result['state']['memories'])
        self.assertEqual('expired', result['state']['archive'][0]['engine']['archived_reason'])
        self.assertEqual([], result['hits'])
    def test_native_memory_migration_uses_original_date(self):
        p = request(state=dict(memories=[record('a', '我明天有个重要考试')]), query='考试', now=NOW + 4 * 86400000)
        result = api.run_isolated(p)
        self.assertEqual([], result['state']['memories'])
        self.assertEqual('2026-10-29', result['state']['archive'][0]['engine']['event_date'])
    def test_secrets_and_questions_rejected(self):
        result = api.run_isolated(request([record('a', '我的密码是 test-only-secret'), record('b', '你觉得咖啡好喝吗？')]))
        self.assertEqual([], result['state']['memories'])
        self.assertEqual(2, len(result['rejected']))
    def test_isolation_and_empty_snapshot_cannot_resurrect(self):
        api.run_isolated(request([record('private', '我喜欢蓝色')], query='蓝色'))
        result = api.run_isolated(request(query='蓝色'))
        self.assertEqual([], result['state']['memories'])
        self.assertEqual([], result['hits'])
    def test_invalid_inputs(self):
        for mutate in [lambda p: p.update(protocol=2), lambda p: p.update(utc_offset_minutes=9999),
                       lambda p: p.update(incoming=[record('../a', '我喜欢蓝色')]),
                       lambda p: p.update(incoming=[record('a', '我喜欢蓝色')] * 2),
                       lambda p: p.update(state={'vector_cache': {'vectors': {'a'*16: [float('nan')]}}})]:
            p = request()
            mutate(p)
            with self.assertRaises(ValueError):
                api.validate(p)
    def test_invalid_http_request(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.http('/v1/evaluate', {'protocol': 9})
        self.assertEqual(400, error.exception.code)
    def test_no_credentials_disables_requested_remote_judge(self):
        p = request([record('a', '我喜欢蓝色')])
        p['judge'] = True
        self.assertFalse(api.run_isolated(p)['features']['judge'])
    def test_semantic_cache_roundtrip_and_provider_failure(self):
        from test_embeddings import FakeEmbeddingServer
        server = FakeEmbeddingServer()
        try:
            with patch.dict(os.environ, {'MEMORY_EMBEDDING_PROVIDER': 'openai', 'MEMORY_EMBEDDING_BASE_URL': server.url,
                                         'MEMORY_EMBEDDING_MODEL': 'fake-test', 'MEMORY_EMBEDDING_API_KEY': 'test', 'NO_PROXY': '127.0.0.1'}):
                p = request([record('a', '我喜欢海边散步')], query='散步')
                p['semantic'] = True
                first = api.run_isolated(p)
                self.assertTrue(first['state']['vector_cache']['vectors'])
                count = server.texts_sent().count('我喜欢海边散步')
                p = request(state=first['state'], query='海边')
                p['semantic'] = True
                second = api.run_isolated(p)
                self.assertEqual(count, server.texts_sent().count('我喜欢海边散步'))
                self.assertIn('a', second['hits'])
                second['state']['vector_cache']['service_scope'] = 'changed-provider'
                p = request(state=second['state'], query='海边')
                p['semantic'] = True
                api.run_isolated(p)
                self.assertGreater(server.texts_sent().count('我喜欢海边散步'), count)
                p = request(query='海边')
                p['semantic'] = True
                cleared = api.run_isolated(p)
                self.assertFalse(cleared['state']['vector_cache'].get('vectors'))
        finally:
            server.httpd.shutdown()
            server.httpd.server_close()
    def test_reasoning_and_truncated_answers_cannot_invalidate(self):
        import io
        from llm_judge import LLMJudge
        settings = dict(base_url='https://example.invalid/v1', model='test', api_key='test')
        judge = LLMJudge(settings)
        for choice in [dict(message={'reasoning_content': '{"decision":"SUPERSEDE","targets":[1]}'}),
                       dict(message={'content': '{"decision":"SUPERSEDE","targets":[1]}'}, finish_reason='length')]:
            with patch('llm_judge.urllib.request.urlopen', return_value=io.BytesIO(json.dumps({'choices': [choice]}).encode())):
                self.assertIsNone(judge.judge({'content': '我喜欢蓝色'}, [{'content': '我喜欢红色'}]))
    def test_llm_supersede_through_protocol(self):
        from test_llm_judge import FakeChatServer
        server = FakeChatServer()
        try:
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'test', 'OPENAI_BASE_URL': server.url, 'NO_PROXY': '127.0.0.1'}):
                first = api.run_isolated(request([record('a', '我女朋友叫苏晴')], query='苏晴'))
                p = request([record('b', '我和苏晴分手了')], state=first['state'], query='苏晴')
                p['judge'] = True
                result = api.run_isolated(p)
                self.assertTrue(server.prompts)
                self.assertEqual(['b'], [r['id'] for r in result['state']['memories']])
                self.assertTrue(any(e.get('decided_by') == 'llm' for e in result['state']['audit']))
                server.mode = 'error'
                p = request([record('c', '苏晴在杭州工作')], state=result['state'], query='苏晴')
                p['judge'] = True
                result = api.run_isolated(p)
                self.assertTrue(any(r['id'] == 'c' for r in result['state']['memories']))
                self.assertTrue(result['degraded'])
        finally:
            server.httpd.shutdown()
            server.httpd.server_close()

if __name__ == '__main__':
    unittest.main()

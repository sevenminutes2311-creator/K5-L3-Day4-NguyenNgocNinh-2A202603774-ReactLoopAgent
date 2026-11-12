"""Tests offline; FakeProvider là test double, không đánh giá năng lực model."""
import json
import os
import sys
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import run_agent
from status_tools import StatusTools
from providers import Provider, post_json
from run import load_env_file


class ProviderErrorTests(unittest.TestCase):
    @patch('providers.urllib.request.urlopen', side_effect=urllib.error.HTTPError('url', 401, 'Unauthorized', {}, None))
    def test_401_reports_authentication_failure(self, _urlopen):
        with self.assertRaisesRegex(RuntimeError, 'từ chối API key'):
            post_json('https://example.test', {}, {})


class EnvFileTests(unittest.TestCase):
    def test_loads_supported_assignments_without_overriding_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text(
                '$env:MODEL=file-model\nexport PROVIDER=openai\nAPI_KEY="value=with=equals"\n',
                encoding='utf-8',
            )
            with patch.dict(os.environ, {'MODEL': 'process-model'}, clear=True):
                load_env_file(str(path))
                self.assertEqual(os.environ['MODEL'], 'process-model')
                self.assertEqual(os.environ['PROVIDER'], 'openai')
                self.assertEqual(os.environ['API_KEY'], 'value=with=equals')


def tool_call(call_id, name, arguments):
    c = {'id': call_id, 'name': name, 'arguments': arguments}
    m = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}]}
    return {'message': m, 'calls': [c], 'finish': 'tool_calls', 'usage': {}}


class FakeProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.histories = []
    def complete(self, messages, schemas):
        self.histories.append(list(messages))
        return next(self.responses)


def final(text='Xong'):
    return {'message': {'role': 'assistant', 'content': text}, 'calls': [], 'finish': 'stop', 'usage': {}}


class ToolsTests(unittest.TestCase):
    def setUp(self):
        self.tools = StatusTools('snapshot')
    def test_fedramp_scope_present(self):
        result = self.tools.get_incident_details('cloudflare', 'wp49bp8bqo6c')
        self.assertTrue(result['ok'])
        self.assertIn('Non-fedramp customers are not impacted', ' '.join(u['body'] for u in result['updates']))
    def test_status_filters_and_resolved(self):
        result = self.tools.get_status('github', 'Actions')
        self.assertTrue(any(i['status'] == 'resolved' for i in result['incidents']))
        self.assertEqual(result['overall']['indicator'], 'none')
    def test_components_empty_search_uses_title(self):
        result = self.tools.get_status('github', 'Copilot Mission Control')
        self.assertTrue(any(i['id'] == 'tc28c0wzrnpz' for i in result['incidents']))
    def test_errors_are_observations(self):
        self.assertFalse(StatusTools(fail=True).execute('get_status', {'provider': 'github'})['ok'])
        self.assertFalse(self.tools.execute('missing', {})['ok'])
        self.assertFalse(self.tools.execute('get_status', {'provider': 'invalid'})['ok'])


class LoopTests(unittest.TestCase):
    def test_tool_result_is_returned_with_id(self):
        fake = FakeProvider([tool_call('call-1', 'get_status', '{"provider":"github"}'), final()])
        result = run_agent('kiểm tra', fake, StatusTools())
        self.assertEqual(result['status'], 'completed')
        tool_message = fake.histories[1][-1]
        self.assertEqual(tool_message['tool_call_id'], 'call-1')
        self.assertEqual(json.loads(tool_message['content'])['provider'], 'github')
    def test_invalid_json_sent_back_to_model(self):
        fake = FakeProvider([tool_call('call-2', 'get_status', '{'), final()])
        result = run_agent('kiểm tra', fake, StatusTools())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(json.loads(fake.histories[1][-2]['content'])['error'], 'INVALID_ARGUMENTS')
        self.assertIn('không thành công', fake.histories[1][-1]['content'])
    def test_tool_failure_is_explicitly_not_evidence_of_operational_status(self):
        class FailingTools:
            def execute(self, name, args):
                return {'ok': False, 'error': 'TimeoutError'}

        fake = FakeProvider([
            tool_call('timeout', 'get_status', '{"provider":"cloudflare"}'),
            final('Cloudflare timeout nên chưa xác minh được trạng thái.'),
        ])
        result = run_agent('Kiểm tra Cloudflare.', fake, FailingTools())
        self.assertEqual(result['status'], 'completed')
        history = fake.histories[1]
        self.assertEqual(history[-2]['role'], 'tool')
        self.assertEqual(json.loads(history[-2]['content'])['error'], 'TimeoutError')
        self.assertEqual(history[-1]['role'], 'user')
        self.assertIn('không phải bằng chứng dịch vụ đang operational', history[-1]['content'])
    def test_max_steps(self):
        fake = FakeProvider([tool_call('a', 'get_status', '{"provider":"github"}')])
        self.assertEqual(run_agent('x', fake, StatusTools(), 1)['status'], 'max_steps')
    def test_model_truncation_is_not_completed(self):
        truncated = final('partial')
        truncated['finish'] = 'length'
        self.assertEqual(run_agent('x', FakeProvider([truncated]), StatusTools())['status'], 'error')
    def test_multiple_tool_calls(self):
        a = tool_call('a', 'get_status', '{"provider":"github"}')
        b = tool_call('b', 'get_status', '{"provider":"cloudflare"}')
        a['calls'] += b['calls']
        a['message']['tool_calls'] += b['message']['tool_calls']
        fake = FakeProvider([a, final()])
        self.assertEqual(run_agent('x', fake, StatusTools())['status'], 'completed')
        self.assertEqual([m['tool_call_id'] for m in fake.histories[1][-2:]], ['a', 'b'])


class ProviderTests(unittest.TestCase):
    @patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test'})
    @patch('providers.post_json')
    def test_anthropic_groups_results(self, post):
        post.return_value = {'content': [{'type': 'text', 'text': 'ok'}], 'stop_reason': 'end_turn', 'usage': {}}
        client = Provider('anthropic', 'test-model')
        call = tool_call('a', 'get_status', '{"provider":"github"}')['message']
        call['tool_calls'].append({'id': 'b', 'type': 'function', 'function': {'name': 'get_status', 'arguments': '{"provider":"cloudflare"}'}})
        messages = [{'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': 'hi'}, call,
                    {'role': 'tool', 'tool_call_id': 'a', 'content': '{}'}, {'role': 'tool', 'tool_call_id': 'b', 'content': '{}'}]
        result = client.complete(messages, [])
        self.assertEqual(result['finish'], 'stop')
        self.assertEqual(len(post.call_args.args[2]['messages'][-1]['content']), 2)

if __name__ == '__main__':
    unittest.main()

class FullTrajectoryTests(unittest.TestCase):
    def test_two_tools_and_final_with_real_snapshot(self):
        class ScenarioProvider:
            def __init__(self):
                self.count = 0
            def complete(self, messages, schemas):
                self.count += 1
                if self.count == 1:
                    return tool_call('s1', 'get_status', '{"provider":"cloudflare","component":"Cache Purge"}')
                if self.count == 2:
                    status = json.loads(messages[-1]['content'])
                    incident = next(i for i in status['incidents'] if i['id'] == 'wp49bp8bqo6c')
                    return tool_call('s2', 'get_incident_details', json.dumps({'provider':'cloudflare', 'incident_id':incident['id']}))
                details = json.loads(messages[-1]['content'])
                assert any('Non-fedramp customers are not impacted' in u['body'] for u in details['updates'])
                return final('Theo snapshot, incident ảnh hưởng FedRAMP; không đủ căn cứ kết luận lỗi website do incident này. https://www.cloudflarestatus.com/incidents/wp49bp8bqo6c')
        result = run_agent('Kiểm tra cache purge', ScenarioProvider(), StatusTools())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([x['name'] for x in result['trace'] if x['event'] == 'tool'], ['get_status', 'get_incident_details'])
        self.assertIn('FedRAMP', result['answer'])

    def test_all_provider_adapters_form_requests_without_keys_leaking(self):
        sample = {'choices':[{'message':{'role':'assistant','content':'ok'},'finish_reason':'stop'}], 'usage':{}}
        for name, key in [('openai','OPENAI_API_KEY'),('gemini','GEMINI_API_KEY'),('openrouter','OPENROUTER_API_KEY')]:
            with self.subTest(name=name), patch.dict('os.environ', {key:'dummy'}), patch('providers.post_json', return_value=sample) as post:
                result = Provider(name,'test-model').complete([{'role':'user','content':'hello'}], [])
                self.assertEqual(result['finish'], 'stop')
                self.assertEqual(post.call_args.args[2]['model'], 'test-model')
                self.assertIn('chat/completions', post.call_args.args[0])

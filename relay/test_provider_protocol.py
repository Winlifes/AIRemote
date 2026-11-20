import unittest

from protocol import validate_request


SESSION = 'session-11111111-2222-3333-4444-555555555555'


class ProviderProtocolTests(unittest.TestCase):
    def request(self, method, path):
        return validate_request({'method':method,'path':path,'body':{},'key':'provider-test-key'})

    def test_deepseek_fixed_routes_survive_relay(self):
        for method,path in [
            ('GET','/api/providers'),
            ('GET','/api/providers/deepseek/projects'),
            ('GET','/api/providers/deepseek/threads/'+SESSION+'/messages?limit=40&before=abcDEF0123'),
            ('POST','/api/providers/deepseek/threads/'+SESSION+'/messages'),
            ('POST','/api/providers/deepseek/threads'),
        ]:
            self.assertEqual(self.request(method,path)['path'],path)

    def test_provider_prefix_cannot_route_deepseek_identity_to_codex(self):
        with self.assertRaises(ValueError):self.request('POST','/api/threads/'+SESSION+'/messages')
        with self.assertRaises(ValueError):self.request('POST','/api/providers/deepseek/threads/'+SESSION.replace('session-','')+'/messages')

    def test_claude_routes_allow_history_and_same_session_writes(self):
        tid = SESSION.removeprefix('session-')
        root = '/api/providers/claude'
        thread = f'{root}/threads/{tid}'
        for method, path in [
            ('GET', f'{root}/projects/{tid}/threads'),
            ('GET', f'{thread}/messages?limit=40&before=abcdef-123'),
            ('GET', f'{thread}/artifacts/{"f" * 24}/download'),
            ('GET', f'{root}/operations/{tid}'),
            ('POST', f'{root}/threads'),
            ('POST', f'{thread}/messages'),
            ('GET', f'{thread}/queue'),
            ('POST', f'{thread}/queue'),
            ('PATCH', f'{thread}/queue/{tid}'),
            ('DELETE', f'{thread}/queue/{tid}'),
            ('POST', f'{thread}/queue/{tid}/send'),
            ('PUT', f'{thread}/queue/order'),
            ('DELETE', f'{thread}/queue'),
            ('POST', f'{thread}/stop'),
            ('PATCH', f'{thread}/settings'),
            *[(method, f'{thread}/goal') for method in ['GET', 'POST', 'PATCH', 'DELETE']],
        ]:
            self.assertEqual(self.request(method, path)['path'], path)
        for method, path in [('POST', f'{root}/threads/{SESSION}/messages'), ('GET', f'{root}/auth'),
                             ('DELETE', f'{thread}/stop'), ('POST', f'{thread}/settings'),
                             ('GET', f'{thread}/queue/{tid}/send'), ('POST', f'{thread}/goal/arbitrary')]:
            with self.assertRaises(ValueError):
                self.request(method, path)

    def test_provider_does_not_enable_arbitrary_native_api_or_file_access(self):
        for path in ['/api/providers/deepseek/api/session/prompt','/api/providers/deepseek/files/etc/passwd','/api/providers/deepseek/auth','/api/providers/codebuddy/projects']:
            with self.assertRaises(ValueError):self.request('GET',path)

    def test_only_fixed_codex_goal_routes_are_exposed(self):
        tid = SESSION.replace('session-', '')
        for method in ['GET', 'POST', 'PATCH', 'DELETE']:
            path = '/api/threads/' + tid + '/goal'
            self.assertEqual(self.request(method, path)['path'], path)
        for method, path in [('PUT', '/api/threads/' + tid + '/goal'),
                             ('POST', '/api/threads/' + tid + '/goal/arbitrary'),
                             ('PATCH', '/api/providers/deepseek/threads/' + SESSION + '/goal')]:
            with self.assertRaises(ValueError): self.request(method, path)


if __name__ == '__main__':unittest.main()

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

    def test_provider_does_not_enable_arbitrary_native_api_or_file_access(self):
        for path in ['/api/providers/deepseek/api/session/prompt','/api/providers/deepseek/files/etc/passwd','/api/providers/deepseek/auth','/api/providers/codebuddy/projects']:
            with self.assertRaises(ValueError):self.request('GET',path)


if __name__ == '__main__':unittest.main()

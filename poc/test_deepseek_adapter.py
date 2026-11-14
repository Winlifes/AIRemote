import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from control_server import Journal
from deepseek_adapter import DeepSeekAdapter, DeepSeekClient, DeepSeekError


TID = 'session-11111111-2222-3333-4444-555555555555'
PID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


class DeepSeekAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'storages').mkdir()
        (self.root / 'storages/workspace.json').write_text(json.dumps({'global': {'workspaceIds': [PID]},
            'tables': {'workspaces': {PID: {'path': str(self.root), 'title': 'native project', 'sessionIds': [TID]}}}}))
        self.adapter = DeepSeekAdapter(self.root / 'connection.json', Journal(self.root / 'journal'), [self.root], home=self.root)
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def test_project_directory_does_not_open_any_session(self):
        with patch.object(self.adapter.client, 'call', side_effect=AssertionError('No chat reads on project index')):
            self.assertEqual(self.adapter.projects()[0]['id'], PID)
            self.assertNotIn('sessionIds', self.adapter.projects()[0])

    def test_expanded_project_runtime_does_not_load_chat_content(self):
        def call(endpoint,request):
            self.calls.append((endpoint,request))
            if endpoint=='session/projections':return {'values':{'title':'live chat'}}
            if endpoint=='session/state':return {'running':True,'pendingInput':[]}
            raise AssertionError('Expanded metadata must not fetch messages')
        with patch.object(self.adapter.client,'call',side_effect=call):
            rows=self.adapter.threads(PID)
        self.assertEqual(rows[0]['runtimeStatus']['type'],'active')
        self.assertEqual([entry[0] for entry in self.calls],['session/projections','session/state'])
        self.assertTrue(all(entry[1]['sessionId']==TID for entry in self.calls))

    def test_model_context_and_reasoning_are_not_rendered_as_chat_messages(self):
        records = [
            {'event': {'type':'turn/start','seq':1,'time':1000,'data':{'turn':3}}},
            {'event': {'type':'user/message','seq':2,'time':1100,'data':{'id':'user','content':[{'type':'text','text':'hello'}],'source':{'kind':'user','rpcId':'client'}}}},
            {'event': {'type':'user/message','seq':3,'time':1150,'data':{'content':[{'type':'text','text':'internal context'}],'source':{'kind':'runtime-context'}}}},
            {'event': {'type':'assistant/message','seq':4,'time':1300,'data':{'turn':3,'message':{'id':'assistant','content':[{'type':'reasoning','text':'private reasoning'},{'type':'text','text':'hello back'}]}}}},
            {'event': {'type':'turn/end','seq':5,'time':1500,'data':{'turn':3}}},
        ]
        with patch.object(self.adapter.client, 'call', return_value={'records':records,'hasMore':False}):
            result = self.adapter.message_page(TID)
        self.assertEqual([m['text'] for m in result['messages']], ['hello', 'hello back'])
        self.assertEqual([m['turnId'] for m in result['messages']], ['3','3'])
        self.assertEqual(result['turns'][0]['started_at'], 1)
        self.assertEqual(result['turns'][0]['completed_at'], 1.5)

    def test_cursor_is_bound_to_original_session(self):
        with patch.object(self.adapter.client, 'call', return_value={'records':[{'event':{'type':'user/message','seq':20,'time':1,'data':{'content':[{'type':'text','text':'user'}],'source':{'kind':'user'}}}}],'hasMore':True}) as call:
            cursor = self.adapter.message_page(TID)['olderCursor']
            self.adapter.message_page(TID, before=cursor)
            self.assertEqual(call.call_args.args[1]['beforeSeq'], 20)
            with patch.object(self.adapter, 'workspace_for_thread'):
                with self.assertRaises(ValueError):
                    self.adapter.message_page('session-other', before=cursor)

    def test_runtime_context_only_page_is_skipped_with_bounded_native_reads(self):
        pages=[{'records':[{'event':{'type':'user/message','seq':20,'time':1,'data':{'content':[{'type':'text','text':'context'}],'source':{'kind':'runtime-context'}}}}],'hasMore':True},
               {'records':[{'event':{'type':'user/message','seq':10,'time':1,'data':{'content':[{'type':'text','text':'human'}],'source':{'kind':'user'}}}}],'hasMore':False}]
        with patch.object(self.adapter.client,'call',side_effect=pages) as call:
            result=self.adapter.message_page(TID,1)
        self.assertEqual([m['text'] for m in result['messages']],['human'])
        self.assertEqual(call.call_args.args[1]['beforeSeq'],20)
        self.assertEqual(call.call_count,2)

    def test_network_outcome_is_not_retried_with_the_same_key(self):
        with patch.object(self.adapter, '_apply', side_effect=TimeoutError('lost after admission')) as apply:
            with self.assertRaises(TimeoutError): self.adapter.perform('stable-key', 'send', {'threadId':TID})
            result = self.adapter.perform('stable-key', 'send', {'threadId':TID})
            self.assertEqual(result['requestStatus'], 'outcome_unknown')
            self.assertEqual(apply.call_count, 1)

    def test_linked_artifacts_must_stay_within_allowed_roots(self):
        good = self.root / 'output.json'; good.write_text('{"ok":true}')
        outside = self.root.parent / 'not-linked-private'
        messages=[{'role':'assistant','text':f'[file](<{good}>) [outside]({outside})'}]
        with patch.object(self.adapter, 'message_page', return_value={'messages':messages}):
            items=self.adapter.artifacts(TID)
            self.assertEqual([i['name'] for i in items],['output.json'])
            self.assertEqual(self.adapter.read_artifact(TID,items[0]['id'])[1],good.read_bytes())
            with self.assertRaises(LookupError):self.adapter.read_artifact(TID,'arbitrary')

    def test_connection_rejects_remote_hosts_before_credentials_leave_mac(self):
        path=self.root/'connection.json'
        path.write_text(json.dumps({'origin':'http://example.com:3000','authenticatedUrl':'http://example.com:3000/?token=secret','pid':123,'desktopVersion':'0.2.2-rc.1'}));path.chmod(0o600)
        with self.assertRaises(PermissionError):DeepSeekClient(path).connection()

    def test_receipt_excludes_other_turns(self):
        page={'messages':[
            {'role':'user','turnId':'1','ordinal':1,'clientUserMessageId':'client'},
            {'itemId':'a1','role':'assistant','turnId':'1','ordinal':2,'text':'expected'},
            {'itemId':'a2','role':'assistant','turnId':'2','ordinal':4,'text':'unrelated'},
        ],'turns':[{'turn_id':'1','status':'completed'}]}
        with patch.object(self.adapter,'message_page',return_value=page),patch.object(self.adapter,'state',return_value={'running':True}):
            value=self.adapter.delivery(TID,'client')
        self.assertEqual(value['turnStatus'],'completed')
        self.assertEqual([m['text'] for m in value['laterAssistantMessages']],['expected'])

    def test_confirmed_execution_survives_recent_page_moving_past_the_user_message(self):
        user={'itemId':'u1','role':'user','turnId':'1','ordinal':1,'clientUserMessageId':'client'}
        previous={'receivedUserMessage':user,'receivedInHistory':True,'turnStatus':'completed','laterAssistantMessages':[{'itemId':'a1','role':'assistant','turnId':'1','ordinal':2,'text':'verified'}]}
        with patch.object(self.adapter,'message_page',return_value={'messages':[],'turns':[]}):
            result=self.adapter.delivery(TID,'client',previous)
        self.assertTrue(result['receivedInHistory'])
        self.assertEqual(result['turnStatus'],'completed')
        self.assertEqual(result['laterAssistantMessages'][0]['text'],'verified')

    def test_native_failed_turn_is_not_reported_as_completed(self):
        page={'records':[{'event':{'type':'turn/end','seq':9,'time':1000,'data':{'turn':2,'reason':{'kind':'error'}}}}],'hasMore':False}
        with patch.object(self.adapter.client,'call',return_value=page):
            result=self.adapter.message_page(TID)
        self.assertEqual(result['turns'][0]['status'],'failed')


if __name__ == '__main__':unittest.main()

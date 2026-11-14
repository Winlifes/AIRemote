import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source = ts.transpileModule(readFileSync(new URL('../src/sync.ts', import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText;
const { ConversationReader, mergeLatestPage, mergeTurns, visibleMessages } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const flush = () => new Promise(resolve => setImmediate(resolve));
const message = (i, extra = {}) => ({ itemId: `m${i}`, turnId: 't', threadId: 'A', ordinal: i, text: `text${i}`, role: 'assistant', ...extra });
function observer() {
  const events = [];
  return { events, callbacks: { thread: v => events.push(['thread', v]), messages: v => events.push(['messages', v]), queue: v => events.push(['queue', v]), error: (source, error) => events.push(['error', source, error.message]), success: source => events.push(['success', source]) } };
}

test('messages keep updating while the native queue request is stalled', async () => {
  const reader = new ConversationReader('A'), queue = deferred(), state = deferred(), watch = observer();
  let reads = 0, queueReads = 0;
  const read = path => path.endsWith('/queue') ? (queueReads++, queue.promise) : path.includes('/messages') ? Promise.resolve({ messages: [message(++reads)] }) : state.promise;
  const first = reader.refresh(read, watch.callbacks); await flush();
  assert.equal(watch.events.find(e => e[0] === 'messages')[1].messages[0].itemId, 'm1');
  const second = reader.refresh(read, watch.callbacks); await flush();
  assert.equal(watch.events.filter(e => e[0] === 'messages').length, 2);
  assert.equal(queueReads, 1);
  state.resolve({ threadId: 'A' }); queue.resolve({ items: [] }); await Promise.all([first, second]);
});
test('A → B → A rejects responses from the first A even when it returns last', async () => {
  const reader = new ConversationReader('A'), old = deferred(), watch = observer();
  const first = reader.refresh(() => old.promise, watch.callbacks); await flush();
  reader.select('B'); reader.select('A');
  await reader.refresh(path => Promise.resolve(path.endsWith('/queue') ? { items: [] } : path.includes('/messages') ? { messages: [message(2)] } : { threadId: 'A' }), watch.callbacks);
  old.resolve({ threadId: 'A', messages: [message(1)], items: [] }); await first;
  assert.deepEqual(watch.events.filter(e => e[0] === 'messages').map(e => e[1].messages[0].itemId), ['m2']);
});
test('failed message reads remain failures while status and queue succeed', async () => {
  const reader = new ConversationReader('A'), watch = observer();
  await reader.refresh(path => path.includes('/messages') ? Promise.reject(new Error('offline')) : Promise.resolve(path.endsWith('/queue') ? { items: [] } : { threadId: 'A' }), watch.callbacks);
  assert.ok(watch.events.some(e => e[0] === 'error' && e[1] === 'messages'));
  assert.ok(!watch.events.some(e => e[0] === 'success' && e[1] === 'messages'));
  assert.ok(watch.events.some(e => e[0] === 'success' && e[1] === 'thread'));
});
test('a disconnected recent page resets pagination rather than skipping the missing middle', () => {
  const page = { messages: [message(80), message(81)], hasMore: true, olderCursor: 'middle-cursor' };
  const result = mergeLatestPage([message(1), message(2)], page);
  assert.equal(result.reset, true);
  assert.deepEqual(result.messages, page.messages);
});
test('recent pages replace streamed text and remove obsolete messages while preserving loaded older history', () => {
  const result = mergeLatestPage([message(1), message(2), message(3), message(4)], { messages: [message(2), message(4, { text: 'finished' }), message(5)], hasMore: true });
  assert.equal(result.reset, false);
  assert.deepEqual(result.messages.map(m => m.itemId), ['m1', 'm2', 'm4', 'm5']);
  assert.equal(result.messages.find(m => m.itemId === 'm4').text, 'finished');
});
test('current turn progress stays visible when older progress is collapsed', () => {
  const messages = [message(1, { phase: 'commentary', turnId: 'old' }), message(2, { phase: 'commentary', turnId: 'active' }), message(3, { phase: 'final_answer' })];
  assert.deepEqual(visibleMessages(messages, false, 'active').map(m => m.itemId), ['m2', 'm3']);
  assert.equal(visibleMessages(messages, true).length, 3);
});
test('an older page cannot change a completed turn back to in progress', () => {
  const turns = mergeTurns([{ turn_id: 't', status: 'completed', started_at: 10, completed_at: 20 }], [{ turn_id: 't', status: 'inProgress', started_at: 10, completed_at: null }]);
  assert.equal(turns[0].completed_at, 20);
  assert.equal(turns[0].status, 'completed');
});

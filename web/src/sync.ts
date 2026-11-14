import type { Message, MessagePage, QueueItem, Thread } from './api';

export type ReadSource = 'thread' | 'messages' | 'queue';
type Callbacks = {
  thread: (value: Thread) => void;
  messages: (value: MessagePage) => void;
  queue: (value: { items: QueueItem[] }) => void;
  error: (source: ReadSource, error: unknown) => void;
  success: (source: ReadSource) => void;
};

// Every lane publishes as soon as it arrives. A slow queue/status request never
// blocks a newer message page, and A → B → A invalidates the first A response.
export class ConversationReader {
  private id: string;
  private generation = 0;
  private pending = new Map<string, Promise<void>>();
  constructor(id = '') { this.id = id; }
  select(id: string) { if (id !== this.id) { this.id = id; this.generation++; } }
  invalidate() { this.generation++; }
  scope() { const id = this.id, generation = this.generation; return () => this.id === id && this.generation === generation; }
  refresh(read: <T>(path: string) => Promise<T>, callbacks: Callbacks) {
    const id = this.id, generation = this.generation;
    if (!id) return Promise.resolve();
    const current = () => this.id === id && this.generation === generation;
    const paths = { thread: '', messages: '/messages?limit=40', queue: '/queue' };
    const tasks = (Object.keys(paths) as ReadSource[]).map(source => {
      const key = `${generation}:${source}`;
      if (this.pending.has(key)) return this.pending.get(key)!;
      const task = Promise.resolve().then(() => read(`/api/threads/${id}${paths[source]}`)).then(value => {
        if (!current()) return;
        if (source === 'thread') {
          const thread = value as Thread;
          if (thread.threadId !== id) throw new Error('返回的对话与当前对话不一致，请重试。');
          callbacks.thread(thread);
        } else if (source === 'messages') {
          const page = value as MessagePage;
          if (page.messages.some(message => message.threadId && message.threadId !== id)) throw new Error('返回的消息不属于当前对话，请重试。');
          callbacks.messages(page);
        } else callbacks.queue(value as { items: QueueItem[] });
        callbacks.success(source);
      }).catch(error => { if (current()) callbacks.error(source, error); })
        .finally(() => { this.pending.delete(key); });
      this.pending.set(key, task);
      return task;
    });
    return Promise.all(tasks).then(() => {});
  }
}

export function mergeLatestPage(existing: Message[], page: MessagePage) {
  const incoming = page.messages;
  const keys = new Set(incoming.map(message => message.itemId));
  const overlap = existing.some(message => keys.has(message.itemId));
  // A disjoint recent page must carry its own older cursor. Appending it would
  // silently skip the messages produced during an outage or background interval.
  const reset = !existing.length || !incoming.length || !page.hasMore || !overlap;
  if (reset) return { messages: incoming, reset: true };
  const boundary = incoming[0];
  const prefix = existing.filter(message => message.ordinal < boundary.ordinal ||
    message.ordinal === boundary.ordinal && message.itemId.localeCompare(boundary.itemId) < 0);
  return { messages: [...prefix, ...incoming], reset: false };
}

export function visibleMessages(messages: Message[], expanded: boolean, activeTurnId?: string) {
  return messages.filter(message => expanded || message.phase !== 'commentary' || message.turnId === activeTurnId);
}

export function mergeTurns(existing: Thread['turns'], incoming: Thread['turns']) {
  const terminal = new Set(['completed', 'failed', 'interrupted']);
  const merged = new Map(existing.map(turn => [turn.turn_id, turn]));
  for (const turn of incoming) {
    const previous = merged.get(turn.turn_id);
    if (previous && terminal.has(previous.status) && !terminal.has(turn.status)) continue;
    merged.set(turn.turn_id, turn);
  }
  return [...merged.values()].sort((a, b) => (b.started_at || 0) - (a.started_at || 0) || b.turn_id.localeCompare(a.turn_id));
}

export type Device = { online: boolean; connectorOnline: boolean; name?: string; last_seen?: number; error?: string; deviceCount?: number; onlineCount?: number; catalogRevision?: string };
export type RelaySession = { username: string; accountId?: string; isAdmin?: boolean; csrf: string; device: Device };
declare global {
  interface Window { AIRemoteAndroid?: { download: (url: string, name: string, mime: string) => void; openSettings: () => void } }
}
export type ProviderId = 'codex' | 'deepseek' | 'claude';
export type Provider = { id: ProviderId; name: string; clientName: string; installed: boolean; connected: boolean; readable: boolean; status: string; statusLabel: string; version?: string; features: string[]; message?: string; helpUrl?: string };
export const providerPath = (id: ProviderId, path: string) => id === 'codex' ? path : path.replace(/^\/api\//, `/api/providers/${id}/`);
export type Project = { id: string; name: string; path: string; roots: string[]; writable?: boolean };
export type Thread = {
  threadId: string; title: string; cwd: string; runtimeStatus: string | { type: string; activeFlags?: string[] } | null;
  latestPersistedTurnStatus: string | null; runtimeSource?: string; projectId?: string;
  turns: { turn_id: string; status: string; started_at?: number | null; completed_at?: number | null }[];
  modelSettings?: ModelSettings | null;
  goalState?: GoalState;
  writeAvailable?: boolean;
  writeMessage?: string;
  queueAvailable?: boolean;
};
export type Goal = { threadId: string; objective: string; status: 'active' | 'paused' | 'blocked' | 'usageLimited' | 'budgetLimited' | 'complete'; tokenBudget: number | null; tokensUsed: number; timeUsedSeconds: number; createdAt: number; updatedAt: number };
export type GoalState = { goal: Goal | null; readable: boolean; writable: boolean; revision: string | null; source: string; message: string };
export function goalPresentation(goal: Goal) {
  const labels: Record<string, string> = { active: '进行中', paused: '已暂停', blocked: '受阻', usageLimited: '用量受限', budgetLimited: '预算已用完', complete: '已完成' };
  const seconds = Math.max(0, Math.floor(goal.timeUsedSeconds || 0));
  const time = seconds >= 3600 ? `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分` : seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒`;
  return { label: labels[goal.status] || '状态未知', active: goal.status === 'active', time,
    tokens: Math.max(0, goal.tokensUsed || 0).toLocaleString('zh-CN'), budget: goal.tokenBudget == null ? null : goal.tokenBudget.toLocaleString('zh-CN') };
}
export type ModelSettings = { model?: string | null; reasoningEffort?: string | null; collaborationMode?: string | null; permissionMode?: string | null; permissionProfile?: string | null; approvalPolicy?: string | null; source: string };

export function modelPresentation(settings?: ModelSettings | null) {
  const model = settings?.model;
  const names: Record<string, string> = { 'gpt-6.1-sol': 'GPT-6.1 Sol', 'gpt-6-astra': 'GPT-6 Astra', 'gpt-6.1-terra': 'GPT-6.1 Terra', 'gpt-6-sol': 'GPT-6 Sol', 'gpt-6-luna': 'GPT-6 Luna' };
  const efforts: Record<string, string> = { minimal: '最低', low: '低', medium: '中', high: '高', xhigh: '最高', max: 'Max', ultra: 'Ultra' };
  const modes: Record<string, string> = { default: '工作', plan: '计划' };
  const permissions: Record<string, string> = { dangerFullAccess: '完全访问', workspaceWrite: '工作区写入', readOnly: '只读' };
  return {
    model: model ? names[model] || model : '模型设置待同步',
    effort: settings?.reasoningEffort ? efforts[settings.reasoningEffort] || settings.reasoningEffort : null,
    mode: settings?.collaborationMode ? modes[settings.collaborationMode] || settings.collaborationMode : null,
    permission: settings?.permissionMode ? permissions[settings.permissionMode] || '自定义权限' : null,
  };
}
export type Message = { itemId: string; turnId: string; threadId?: string; role: string; phase?: string; text: string; createdAtMs: number; ordinal: number };
export type MessagePage = { messages: Message[]; hasMore: boolean; olderCursor: string | null; turns: Thread['turns'] };
export type Artifact = { id: string; name: string; path: string; size: number; threadId: string };
export type QueueItem = { id: string; input: { type: string; text?: string }[] };
export type Receipt = {
  operationId: string; requestStatus: string; kind?: string;
  result?: { threadId?: string; deliveryState?: string; transport?: string; [key: string]: unknown };
  delivery?: { accepted?: boolean; receivedInHistory?: boolean; turnStatus?: string; laterAssistantMessages?: Message[] };
};

let csrf = '';
let generation = 0;
export function forgetSession() { csrf = ''; generation++; }
export class APIError extends Error {
  constructor(message: string, public status = 0, public requestId?: string) { super(message); }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const started = generation;
  let response: Response;
  try {
    response = await fetch(path, {
      ...options, credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-AIRemote-CSRF': csrf, ...options.headers },
    });
  } catch { throw new APIError('网络未连接，请检查手机网络后重试。'); }
  const body = await response.json().catch(() => ({}));
  if (started !== generation) throw new APIError('登录状态已变化。');
  if (!response.ok) {
    if (response.status === 401 && path !== '/api/login' && path !== '/api/register') window.dispatchEvent(new Event('airemote:logout'));
    throw new APIError(body.error || `请求失败 (${response.status})`, response.status);
  }
  return body as T;
}

export async function session() {
  const result = await api<RelaySession>('/api/session');
  csrf = result.csrf;
  return result;
}
export async function login(username: string, password: string) {
  await api('/api/login', { method: 'POST', body: JSON.stringify({ username, password }) });
  forgetSession();
  return session();
}
export async function register(code: string, password: string) {
  await api('/api/register', { method: 'POST', body: JSON.stringify({ code, password }) });
  forgetSession();
  return session();
}
export async function logout() {
  await api('/api/logout', { method: 'POST', body: '{}' });
  forgetSession();
}
const delay = (ms: number) => new Promise(resolve => window.setTimeout(resolve, ms));

export async function bridgeResponse(path: string, method = 'GET', body: object = {}, key: string = crypto.randomUUID()): Promise<Response> {
  const started = generation;
  const request = { path, method, body, key };
  let job: { id: string; state: string } | undefined;
  for (let attempt = 0; attempt < 2; attempt++) {
    if (started !== generation) throw new APIError('登录状态已变化。');
    try { job = await api('/api/requests', { method: 'POST', body: JSON.stringify(request) }); break; }
    catch (error) {
      if (attempt || (error instanceof APIError && error.status)) throw error;
      await delay(600);
    }
  }
  if (!job) throw new APIError('暂时无法提交请求。');
  const id = job.id;
  const deadline = Date.now() + 145_000;
  while (Date.now() < deadline) {
    if (started !== generation) throw new APIError('登录状态已变化。');
    const state = await api<{ state: string }>(`/api/requests/${id}`);
    if (state.state === 'done') {
      const response = await fetch(`/api/requests/${id}/result`, { credentials: 'same-origin', cache: 'no-store' });
      if (started !== generation) throw new APIError('登录状态已变化。');
      if (!response.ok) {
        const value = await response.json().catch(() => ({}));
        throw new APIError(value.error || 'Mac 返回了错误，请刷新后查看对话。', response.status, id);
      }
      return response;
    }
    if (state.state === 'result_expired') {
      throw new APIError('该请求已有处理记录，响应缓存已过期。请核对对话和回执，避免重复发送。', 409, id);
    }
    if (state.state === 'expired' || state.state === 'unknown') {
      throw new APIError(state.state === 'unknown' ? 'Mac 已领取请求，结果暂不确定。请先查看对话，再决定是否重新发送。' : '请求已过期，未执行。请重试。', 409, id);
    }
    await delay(500);
  }
  throw new APIError('仍在等待 Mac 的结果。请先回读对话；不要反复发送同一指令。', 504, id);
}
export async function bridge<T>(path: string, method = 'GET', body: object = {}, key?: string): Promise<T> {
  return (await bridgeResponse(path, method, body, key)).json();
}
export const queueText = (item: QueueItem) => item.input.map(p => p.text || '').join('\n');
export function runtime(thread?: Thread): { label: string; tone: string; hint: string } {
  if (!thread) return { label: '状态未知', tone: 'muted', hint: '等待 Mac 的实时状态' };
  const type = typeof thread.runtimeStatus === 'string' ? thread.runtimeStatus : thread.runtimeStatus?.type;
  const flags = thread.runtimeStatus && typeof thread.runtimeStatus === 'object' ? thread.runtimeStatus.activeFlags || [] : [];
  if (flags.some(f => /approval|input|attention/i.test(f))) return { label: '等待处理', tone: 'amber', hint: '请在 Mac 上处理审批或补充输入' };
  if (type === 'active') return { label: '运行中', tone: 'green', hint: '已从 Mac 读取到实时运行状态' };
  if (type === 'idle') return { label: '空闲', tone: 'blue', hint: thread.latestPersistedTurnStatus === 'completed' ? '上一轮已完成，随时可以继续' : '当前没有正在执行的轮次' };
  if (type === 'notLoaded') return { label: '未加载', tone: 'muted', hint: thread.runtimeSource?.includes('connector-created') ? '历史已同步，发送新指令即可继续' : '历史可读；已有桌面对话需在 Mac 中打开才能继续' };
  return { label: '状态未知', tone: 'muted', hint: '未取得实时状态，历史记录仍可查看' };
}

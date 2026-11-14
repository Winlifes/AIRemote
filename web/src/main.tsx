import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { createPortal } from 'react-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Activity, ArrowDown, ArrowLeft, ArrowUp, ArrowUpRight, Check, CheckCheck, ChevronDown, ChevronRight, Clock3, Code2, Copy, CornerDownRight, CornerUpRight, Download, File, FileText, Folder, Inbox, ListOrdered, LoaderCircle, LogOut, Menu, MessageSquare, Monitor, MoreHorizontal, Pencil, Plus, Radio, RefreshCw, Search, Send, ShieldCheck, Sparkles, Target, Terminal, Trash2, WifiOff, X } from 'lucide-react';
import { api, APIError, Artifact, bridge as remoteBridge, bridgeResponse as remoteBridgeResponse, Device, login, logout, Message, MessagePage, Project, Provider, ProviderId, providerPath, QueueItem, queueText, Receipt, runtime, session, Thread, modelPresentation } from './api';
import { ConversationReader, mergeLatestPage, mergeTurns, visibleMessages as filterVisibleMessages } from './sync';
import { GoalControl } from './goals';
import './styles.css';
import './conversation.css';
import './projects.css';
import './providers.css';
import './mobile.css';

const shortTime = (ms: number) => new Date(ms).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
const fileSize = (n: number) => n < 1024 ? `${n} B` : n < 1024 ** 2 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 ** 2).toFixed(1)} MB`;
const errorText = (e: unknown) => e instanceof Error ? e.message : '操作暂时失败，请重试。';
const threadPath = (id: string) => `/api/threads/${id}`;
const PAGE_SIZE = 40;
function mergeMessages(existing: Message[], incoming: Message[]) {
  const merged = new Map(existing.map(message => [message.itemId, message]));
  for (const message of incoming) merged.set(message.itemId, message);
  return [...merged.values()].sort((a, b) => a.ordinal - b.ordinal || a.itemId.localeCompare(b.itemId));
}
function messageDate(ms: number) {
  const date = new Date(ms), today = new Date();
  const yesterday = new Date(today); yesterday.setDate(today.getDate() - 1);
  const day = date.toDateString() === today.toDateString() ? '今天' : date.toDateString() === yesterday.toDateString() ? '昨天' : date.toLocaleDateString('zh-CN', { month: 'long', day: 'numeric' });
  return `${day} ${shortTime(ms)}`;
}
function userPresentation(text: string): { text: string; attachments: number } {
  const marker = '## My request:';
  const at = text.indexOf(marker);
  if (at >= 0 && /^\s*# Files mentioned by the user:/.test(text)) {
    return { text: text.slice(at + marker.length).trim(), attachments: (text.slice(0, at).match(/^## /gm) || []).length };
  }
  const response = text.match(/^\s*<send_user_message_question_reply>\s*([\s\S]*?)\s*<\/send_user_message_question_reply>\s*$/);
  if (response) {
    try {
      const values: unknown = JSON.parse(response[1]);
      if (Array.isArray(values) && values.every(v => typeof v?.answer === 'string')) return { text: values.map(v => v.answer).join('\n\n'), attachments: 0 };
    } catch { /* Preserve the original message when the envelope is unfamiliar. */ }
  }
  return { text, attachments: 0 };
}
function Logo({ small = false }: { small?: boolean }) {
  return <div className={`brand ${small ? 'small' : ''}`}><img src="/icon.svg" alt="" /><span>AIRemote<span className="brand-dot">.</span></span></div>;
}
function Modal({ title, children, close, className = '' }: { title: string; children: React.ReactNode; close: () => void; className?: string }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const d = ref.current!; d.showModal(); return () => d.close(); }, []);
  return <dialog className={`modal ${className}`} ref={ref} onCancel={e => { e.preventDefault(); close(); }} onClick={e => { if (e.target === ref.current) close(); }}>
    <div className="modal-heading"><h2>{title}</h2><button className="icon-button" aria-label="关闭弹窗" onClick={close}><X size={21} /></button></div>{children}
  </dialog>;
}
function Login({ onLogin }: { onLogin: (device: Device) => void }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  return <main className="login-page"><div className="login-decoration"><div className="orb orb-one" /><div className="orb orb-two" /><div className="login-signal"><Monitor size={40} /><div className="signal-line" /><Radio size={30} /></div></div>
    <section className="login-card"><Logo /><div className="eyebrow">桌面 AI · 随时连接</div><h1>你的 AI，<br />随时继续。</h1><p>离开电脑，也能看到新的回复、取走完成的文件，接着推进下一步。</p>
      <form onSubmit={async e => { e.preventDefault(); setBusy(true); setError(''); try { const s = await login(username, password); setPassword(''); onLogin(s.device); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } }}>
        <label>账号<input autoComplete="username" value={username} onChange={e => setUsername(e.target.value)} required /></label>
        <label>访问密码<input type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} required placeholder="输入你的 AIRemote 密码" /></label>
        {error && <p role="alert" className="error-text">{error}</p>}<button className="primary full" disabled={busy}>{busy ? <LoaderCircle className="spin" size={18} /> : <ArrowUpRight size={18} />}连接我的 Mac</button>
      </form><div className="login-foot"><ShieldCheck size={15} />私有服务 · 加密连接 · 仅授权对话</div>
    </section><span className="login-copyright">AIRemote / 从这里，继续创造。</span></main>;
}

const providerNames = { codex: 'Codex', deepseek: 'DeepSeek' };
const selectedChat = () => location.hash.slice(1).replace(/^(codex|deepseek)\/?/, '');
function App({ providerId, switchProvider, drafts, setDrafts, drawerOpen, setDrawerOpen }: { providerId: ProviderId; switchProvider: (id: ProviderId) => void; drafts: Record<string, string>; setDrafts: React.Dispatch<React.SetStateAction<Record<string, string>>>; drawerOpen: boolean; setDrawerOpen: React.Dispatch<React.SetStateAction<boolean>> }) {
  const providerName = providerNames[providerId];
  const bridge = <T,>(path: string, method = 'GET', body: object = {}, key?: string) => remoteBridge<T>(providerPath(providerId, path), method, body, key);
  const bridgeResponse = (path: string) => remoteBridgeResponse(providerPath(providerId, path));
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [hostDevice, setDevice] = useState<Device>({ online: false, connectorOnline: false });
  const [providers, setProviders] = useState<Provider[]>([]);
  const provider = providers.find(item => item.id === providerId);
  const device = { ...hostDevice, connectorOnline: hostDevice.online && hostDevice.connectorOnline && !!provider?.readable };
  const [searchOpen, setSearchOpen] = useState(false);
  const [providerInfoOpen, setProviderInfoOpen] = useState(false);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [expandedProjects, setExpandedProjects] = useState(new Set<string>());
  const [projectThreads, setProjectThreads] = useState<Record<string, string[]>>({});
  const [projectLoading, setProjectLoading] = useState<Record<string, boolean>>({});
  const [projectErrors, setProjectErrors] = useState<Record<string, string>>({});
  const [selected, setSelected] = useState(selectedChat);
  const [search, setSearch] = useState('');
  const [filesOpen, setFilesOpen] = useState(false);
  const [goalOpen, setGoalOpen] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [files, setFiles] = useState<Artifact[]>([]);
  const [filesLoading, setFilesLoading] = useState(false);
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [sendMode, setSendMode] = useState<'send' | 'queue'>('send');
  const [busy, setBusy] = useState('');
  const [loading, setLoading] = useState(false);
  const [listLoading, setListLoading] = useState(false);
  const [error, setError] = useState('');
  const [toast, setToast] = useState('');
  const [newChat, setNewChat] = useState(false);
  const [editing, setEditing] = useState<QueueItem | null>(null);
  const [editText, setEditText] = useState('');
  const [clearing, setClearing] = useState(false);
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [expanded, setExpanded] = useState(true);
  const [syncErrors, setSyncErrors] = useState<Record<string, string>>({});
  const [hasNewMessages, setHasNewMessages] = useState(false);
  const [followLatest, setFollowLatest] = useState(true);
  const [olderCursor, setOlderCursor] = useState<string | null>(null);
  const [hasOlder, setHasOlder] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<number>();
  const [mobile, setMobile] = useState(() => window.matchMedia('(max-width: 700px)').matches);
  const drawerRef = useRef<HTMLElement>(null);
  const drawerOpenRef = useRef(drawerOpen);
  drawerOpenRef.current = drawerOpen;
  const selectionRef = useRef(selected);
  const reader = useRef(new ConversationReader(selected));
  const messagesRef = useRef<Message[]>([]);
  const pendingLatest = useRef<MessagePage | null>(null);
  const listRefreshing = useRef(false);
  const expandedProjectsRef = useRef(new Set<string>());
  const projectRequests = useRef(new Set<string>());
  const pageLoadedRef = useRef(false);
  const olderCursorRef = useRef<string | null>(null);
  const olderRequestRef = useRef(false);
  const atLatestRef = useRef(true);
  const scrollRestore = useRef<{ threadId: string; height: number; top: number } | null>(null);
  const historyIntentRef = useRef(0);
  const touchStartRef = useRef<number | null>(null);
  const uncertainWrites = useRef(new Map<string, string>());
  const scrollRef = useRef<HTMLDivElement>(null);
  const thread = threads.find(t => t.threadId === selected);
  const draft = drafts[selected] || '';
  const canWrite = device.online && device.connectorOnline && !!provider?.connected && provider.features.includes('send') && !busy;
  const stat = runtime(syncErrors.thread ? undefined : thread);
  const workspaces = [...new Set(projects.flatMap(project => project.roots))];
  const preferredWorkspace = projects.find(project => project.id === thread?.projectId)?.roots[0] || projects.find(project => expandedProjects.has(project.id))?.roots[0] || workspaces[0];

  function closeDrawer() {
    setDrawerOpen(false);
    requestAnimationFrame(() => document.querySelector<HTMLButtonElement>('[data-drawer-trigger]')?.focus({ preventScroll: true }));
  }
  function showNewChat() { setDrawerOpen(false); setNewChat(true); }
  useEffect(() => {
    const media = window.matchMedia('(max-width: 700px)');
    const resize = () => { setMobile(media.matches); if (!media.matches) setDrawerOpen(false); };
    const viewport = () => document.documentElement.style.setProperty('--airemote-viewport-height', `${window.visualViewport?.height || window.innerHeight}px`);
    resize(); viewport();
    media.addEventListener('change', resize);
    window.visualViewport?.addEventListener('resize', viewport);
    window.addEventListener('resize', viewport);
    return () => { media.removeEventListener('change', resize); window.visualViewport?.removeEventListener('resize', viewport); window.removeEventListener('resize', viewport); };
  }, []);
  useEffect(() => {
    if (!mobile || !drawerOpen || !authenticated) return;
    drawerRef.current?.focus({ preventScroll: true });
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); closeDrawer(); return; }
      if (event.key !== 'Tab') return;
      const controls = [...drawerRef.current!.querySelectorAll<HTMLElement>('button:not(:disabled), input, [tabindex="0"]')].filter(element => element.getClientRects().length);
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === drawerRef.current)) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === drawerRef.current)) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener('keydown', keyboard);
    return () => document.removeEventListener('keydown', keyboard);
  }, [mobile, drawerOpen, authenticated]);

  useEffect(() => {
    const handler = () => { const id = selectedChat(); reader.current.select(id); selectionRef.current = id; setSelected(id); setFilesOpen(false); };
    const out = () => setAuthenticated(false);
    window.addEventListener('hashchange', handler);
    window.addEventListener('airemote:logout', out);
    session().then(s => { setAuthenticated(true); setDevice(s.device); }).catch(() => setAuthenticated(false));
    return () => { reader.current.invalidate(); window.removeEventListener('hashchange', handler); window.removeEventListener('airemote:logout', out); };
  }, []);
  useEffect(() => { const timer = window.setTimeout(() => setToast(''), 4000); return () => clearTimeout(timer); }, [toast]);
  const notifyError = (e: unknown) => setError(errorText(e));
  function openThread(id: string) { reader.current.select(id); selectionRef.current = id; location.hash = providerId === 'codex' ? id : `${providerId}/${id}`; setSelected(id); setLoading(true); setFilesOpen(false); setReceipt(null); closeDrawer(); }

  async function loadProjects() {
    if (listRefreshing.current) return projects;
    listRefreshing.current = true; setListLoading(true);
    try {
      const value = await bridge<{ projects: Project[] }>('/api/projects');
      setProjects(value.projects); return value.projects;
    } catch (e) { notifyError(e); return projects; }
    finally { listRefreshing.current = false; setListLoading(false); }
  }
  async function loadProject(projectId: string) {
    if (projectRequests.current.has(projectId) || !device.connectorOnline) return;
    projectRequests.current.add(projectId);
    setProjectLoading(old => ({ ...old, [projectId]: true }));
    setProjectErrors(old => ({ ...old, [projectId]: '' }));
    try {
      const value = await bridge<{ threads: Thread[] }>(`/api/projects/${projectId}/threads`);
      setThreads(old => {
        const merged = new Map(old.map(chat => [chat.threadId, chat]));
        for (const chat of value.threads) {
          const previous = merged.get(chat.threadId);
          merged.set(chat.threadId, chat.threadId === selectionRef.current && previous ? { ...chat, ...previous, title: chat.title, projectId } : chat);
        }
        return [...merged.values()];
      });
      setProjectThreads(old => ({ ...old, [projectId]: value.threads.map(chat => chat.threadId) }));
    } catch (e) { setProjectErrors(old => ({ ...old, [projectId]: errorText(e) })); }
    finally { projectRequests.current.delete(projectId); setProjectLoading(old => ({ ...old, [projectId]: false })); }
  }
  function toggleProject(projectId: string) {
    const next = new Set(expandedProjectsRef.current);
    if (next.has(projectId)) next.delete(projectId); else next.add(projectId);
    expandedProjectsRef.current = next; setExpandedProjects(next);
    if (next.has(projectId)) loadProject(projectId);
  }
  async function refreshProjectTree() {
    const current = await loadProjects();
    await Promise.all(current.filter(project => expandedProjectsRef.current.has(project.id)).map(project => loadProject(project.id)));
  }
  function mergePageTurns(id: string, turns: Thread['turns']) {
    setThreads(old => old.map(chat => chat.threadId === id ? { ...chat, turns: mergeTurns(chat.turns, turns) } : chat));
  }
  async function loadDetail(id = selected, silent = false) {
    if (!id || selectionRef.current !== id) return;
    if (!silent) setLoading(true);
    await reader.current.refresh(bridge, {
      thread: snapshot => {
        setThreads(old => {
          const previous = old.find(chat => chat.threadId === id);
          const next = { ...snapshot, turns: mergeTurns(previous?.turns || [], snapshot.turns) };
          return previous ? old.map(chat => chat.threadId === id ? next : chat) : [...old, next];
        });
      },
      messages: page => {
        const next = mergeLatestPage(messagesRef.current, page);
        if (pageLoadedRef.current && !atLatestRef.current && next.reset && messagesRef.current.length) {
          pendingLatest.current = page; setHasNewMessages(true);
        } else {
          if (!atLatestRef.current && page.messages.some(message => !messagesRef.current.some(old => old.itemId === message.itemId))) setHasNewMessages(true);
          pendingLatest.current = null;
          messagesRef.current = next.messages; setMessages(next.messages);
        }
        if (!pageLoadedRef.current || next.reset && !pendingLatest.current) {
          olderCursorRef.current = page.olderCursor; setOlderCursor(page.olderCursor); setHasOlder(page.hasMore);
          pageLoadedRef.current = true;
        }
        mergePageTurns(id, page.turns); setLastUpdated(Date.now()); setLoading(false);
      },
      queue: value => setQueue(value.items),
      success: source => setSyncErrors(old => ({ ...old, [source]: '' })),
      error: (source, error) => {
        setSyncErrors(old => ({ ...old, [source]: errorText(error) }));
        if (source === 'messages') setLoading(false);
      },
    });
  }
  function jumpToLatest() {
    const page = pendingLatest.current;
    if (page) {
      messagesRef.current = page.messages; setMessages(page.messages);
      olderCursorRef.current = page.olderCursor; setOlderCursor(page.olderCursor); setHasOlder(page.hasMore);
      pendingLatest.current = null;
    }
    atLatestRef.current = true; setFollowLatest(true); setHasNewMessages(false);
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'instant' });
    loadDetail(selected, true);
  }
  async function loadOlder() {
    const id = selectionRef.current, cursor = olderCursorRef.current;
    const current = reader.current.scope();
    if (!id || !cursor || olderRequestRef.current || !device.connectorOnline) return;
    olderRequestRef.current = true; setLoadingOlder(true); atLatestRef.current = false; setFollowLatest(false);
    historyIntentRef.current = 0; touchStartRef.current = null;
    try {
      const page = await bridge<MessagePage>(`${threadPath(id)}/messages?limit=${PAGE_SIZE}&before=${cursor}`);
      if (!current()) return;
      const element = scrollRef.current;
      if (element) scrollRestore.current = { threadId: id, height: element.scrollHeight, top: element.scrollTop };
      messagesRef.current = mergeMessages(page.messages, messagesRef.current); setMessages(messagesRef.current); mergePageTurns(id, page.turns);
      olderCursorRef.current = page.olderCursor; setOlderCursor(page.olderCursor); setHasOlder(page.hasMore);
    } catch (e) { if (current()) notifyError(e); }
    finally { if (current()) { olderRequestRef.current = false; historyIntentRef.current = 0; setLoadingOlder(false); } }
  }
  function historyScrollIntent() {
    historyIntentRef.current = Date.now();
    if ((scrollRef.current?.scrollTop || 0) < 80 && pageLoadedRef.current && olderCursorRef.current) loadOlder();
  }
  async function loadFiles(id = selected) {
    setFilesLoading(true);
    try {
      const value = await bridge<{ artifacts: Artifact[] }>(`${threadPath(id)}/artifacts`);
      if (selectionRef.current === id) setFiles(value.artifacts);
      return value.artifacts;
    } catch (e) { if (selectionRef.current === id) notifyError(e); return []; }
    finally { if (selectionRef.current === id) setFilesLoading(false); }
  }
  async function downloadLocal(path: string) {
    const list = files.some(file => file.path === path) ? files : await loadFiles();
    const artifact = list.find(file => file.path === path);
    if (artifact) await download(artifact);
    else { setFilesOpen(true); setError('这份文件暂未关联到可下载的授权产物。'); }
  }
  useEffect(() => {
    if (!authenticated) return;
    let live = true;
    const poll = async () => {
      if (document.hidden) return;
      try { const value = await api<Device>('/api/device'); if (live) setDevice(value); } catch { /* Keep the latest connection observation. */ }
    };
    poll(); const timer = setInterval(poll, 5000);
    return () => { live = false; clearInterval(timer); };
  }, [authenticated]);
  useEffect(() => {
    if (!authenticated || !hostDevice.online) return;
    let cancelled = false, running = false;
    const poll = async () => {
      if (cancelled || running || document.hidden) return;
      running = true;
      try { const result = await remoteBridge<{ providers: Provider[] }>('/api/providers'); if (!cancelled) setProviders(result.providers); }
      catch (error) { if (!cancelled) setError(errorText(error)); }
      finally { running = false; }
    };
    poll(); const timer = setInterval(poll, 10000);
    const visible = () => { if (!document.hidden) poll(); };
    document.addEventListener('visibilitychange', visible);
    return () => { cancelled = true; clearInterval(timer); document.removeEventListener('visibilitychange', visible); };
  }, [authenticated, hostDevice.online]);
  useEffect(() => {
    if (!authenticated || !device.connectorOnline) return;
    // Only the project directory is fetched on connection; no chat or history prefetch.
    loadProjects().then(current => {
      if (!window.matchMedia('(max-width: 700px)').matches || drawerOpenRef.current) {
        for (const project of current) if (expandedProjectsRef.current.has(project.id)) loadProject(project.id);
      }
    });
  }, [authenticated, device.connectorOnline]);
  useEffect(() => {
    if (!authenticated || !device.connectorOnline || !expandedProjects.size) return;
    let cancelled = false, inFlight = false;
    const mobile = window.matchMedia('(max-width: 700px)');
    const poll = async () => {
      if (cancelled || inFlight || document.hidden || mobile.matches && !drawerOpenRef.current) return;
      inFlight = true;
      try { await Promise.all([...expandedProjectsRef.current].map(id => loadProject(id))); }
      finally { inFlight = false; }
    };
    poll(); const timer = setInterval(poll, 6000);
    const visible = () => { if (!document.hidden) poll(); };
    document.addEventListener('visibilitychange', visible);
    mobile.addEventListener('change', visible);
    return () => { cancelled = true; clearInterval(timer); document.removeEventListener('visibilitychange', visible); mobile.removeEventListener('change', visible); };
  }, [authenticated, device.connectorOnline, expandedProjects, selected, drawerOpen]);
  useEffect(() => {
    selectionRef.current = selected; pageLoadedRef.current = false; olderCursorRef.current = null; atLatestRef.current = true;
    messagesRef.current = []; pendingLatest.current = null; setHasNewMessages(false); setSyncErrors({});
    olderRequestRef.current = false;
    scrollRestore.current = null;
    setMessages([]); setFiles([]); setFilesLoading(false); setQueue([]); setHasOlder(false); setOlderCursor(null); setLoadingOlder(false); setLastUpdated(undefined);
    historyIntentRef.current = 0; touchStartRef.current = null;
    setExpanded(true); setFollowLatest(true); setError('');
    setGoalOpen(false);
    setReceipt(current => current?.result?.threadId === selected ? current : null);
  }, [selected]);
  useEffect(() => {
    if (!authenticated || !selected || !device.connectorOnline || mobile && drawerOpen) return;
    const poll = (initial = false) => { if (!document.hidden) loadDetail(selected, !initial); };
    const visible = () => { if (!document.hidden) poll(); };
    poll(true);
    const timer = window.setInterval(() => poll(), 2500);
    document.addEventListener('visibilitychange', visible);
    return () => { clearInterval(timer); document.removeEventListener('visibilitychange', visible); };
  }, [selected, authenticated, device.connectorOnline, mobile, drawerOpen]);
  useLayoutEffect(() => {
    const restore = scrollRestore.current, element = scrollRef.current;
    if (restore && element && restore.threadId === selected) {
      element.scrollTop = restore.top + element.scrollHeight - restore.height; scrollRestore.current = null;
    }
  }, [messages]);
  useEffect(() => {
    if (followLatest && !scrollRestore.current) scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'instant' });
  }, [messages, followLatest, queue.length]);
  useEffect(() => {
    if (filesOpen && authenticated && selected && device.connectorOnline) loadFiles(selected);
  }, [filesOpen, selected, authenticated, device.connectorOnline]);
  useEffect(() => {
    if (!receipt?.operationId || !selected || receipt.result?.threadId !== selected || !authenticated || !device.connectorOnline || receipt.delivery?.turnStatus === 'completed' || mobile && drawerOpen) return;
    let cancelled = false, running = false;
    const poll = async () => {
      if (running || document.hidden) return;
      running = true;
      try { const next = await bridge<Receipt>(`/api/operations/${receipt.operationId}`); if (!cancelled) setReceipt(next); } catch { /* User can refresh the active chat to reconcile. */ }
      finally { running = false; }
    };
    const timer = setInterval(poll, 3500); poll();
    return () => { cancelled = true; clearInterval(timer); };
  }, [receipt?.operationId, receipt?.delivery?.turnStatus, selected, authenticated, device.connectorOnline, mobile, drawerOpen]);

  async function mutate(path: string, method: string, body: object, label: string) {
    if (busy) return null;
    setBusy(label); setError('');
    const fingerprint = JSON.stringify({ path, method, body });
    const key = uncertainWrites.current.get(fingerprint) || crypto.randomUUID();
    uncertainWrites.current.set(fingerprint, key);
    try {
      const result = await bridge<Receipt>(path, method, body, key);
      if (!['in_flight', 'outcome_unknown'].includes(result.requestStatus)) uncertainWrites.current.delete(fingerprint);
      if (['send', 'queue_send', 'queue_add', 'create'].includes(result.kind || '')) setReceipt(result);
      const output = result.result;
      const item = output?.item as QueueItem | undefined;
      if (item?.id && Array.isArray(item.input) && result.kind === 'queue_add' && !result.delivery?.receivedInHistory) setQueue(old => [...old.filter(previous => previous.id !== item.id), item]);
      if (item?.id && Array.isArray(item.input) && result.kind === 'queue_update') setQueue(old => old.map(previous => previous.id === item.id ? item : previous));
      if (result.kind === 'queue_reorder' && Array.isArray(output?.items)) setQueue(output.items as QueueItem[]);
      if (result.kind === 'queue_delete' && output?.deleted) { const id = path.split('/').pop(); setQueue(old => old.filter(item => item.id !== id)); setReceipt(null); }
      if (result.kind === 'queue_clear' && Array.isArray(output?.deletedIds)) { const ids = new Set(output.deletedIds as string[]); setQueue(old => old.filter(item => !ids.has(item.id))); setReceipt(null); }
      if (result.kind === 'queue_send' && output?.removedAfterAcceptance) setQueue(old => old.filter(item => item.id !== output.queueId));
      return result;
    } catch (e) {
      if (e instanceof APIError && e.status && e.status < 500 && !/不确定|未知|缓存已过期|outcome is unknown|do not blindly retry/i.test(e.message)) uncertainWrites.current.delete(fingerprint);
      notifyError(e); return null;
    }
    finally { setBusy(''); }
  }
  async function send() {
    if (!draft.trim() || !canWrite) return;
    const text = draft;
    const result = await mutate(`${threadPath(selected)}/${sendMode === 'send' ? 'messages' : 'queue'}`, 'POST', { text }, sendMode === 'send' ? '正在发送到 Mac…' : '正在加入原生队列…');
    if (result) {
      setDrafts(d => ({ ...d, [selected]: d[selected] === text ? '' : d[selected] }));
      setFollowLatest(true);
      setToast(sendMode === 'queue' ? `已加入 ${providerName} 原生队列` : 'Mac 已受理，正在核对对话回执');
      await loadDetail(selected, true);
    }
  }
  async function changeGoal(method: string, body: object) {
    const id = selected, current = reader.current.scope();
    const result = await mutate(`${threadPath(id)}/goal`, method, body, '正在更新 Codex 目标…');
    await loadDetail(id, true);
    return current() ? result : null;
  }
  async function download(artifact: Artifact) {
    setBusy(`正在获取 ${artifact.name}…`); setError('');
    try {
      const response = await bridgeResponse(`${threadPath(artifact.threadId)}/artifacts/${artifact.id}/download`);
      if (window.AIRemoteAndroid && new URL(response.url).origin === location.origin) {
        void response.body?.cancel();
        window.AIRemoteAndroid.download(response.url, artifact.name, response.headers.get('Content-Type') || 'application/octet-stream');
        setToast('文件已交给系统下载'); return;
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a'); a.href = url; a.download = artifact.name; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 60000); setToast('文件已下载');
    } catch (e) { notifyError(e); } finally { setBusy(''); }
  }
  async function queueAction(item: QueueItem, action: 'delete' | 'send' | 'up' | 'down') {
    const root = `${threadPath(selected)}/queue`;
    let result: Receipt | null;
    if (action === 'up' || action === 'down') {
      const ids = queue.map(i => i.id), at = ids.indexOf(item.id), to = at + (action === 'up' ? -1 : 1);
      if (to < 0 || to >= ids.length) return;
      [ids[at], ids[to]] = [ids[to], ids[at]];
      result = await mutate(`${root}/order`, 'PUT', { ids }, '正在调整顺序…');
    } else result = await mutate(`${root}/${item.id}${action === 'send' ? '/send' : ''}`, action === 'send' ? 'POST' : 'DELETE', {}, action === 'send' ? '正在立即执行…' : '正在删除队列项…');
    if (result) { setToast(action === 'send' ? '已交给同一对话执行' : '队列已更新'); await loadDetail(selected, true); }
  }
  const filteredProjects = projects.filter(project => project.name.toLowerCase().includes(search.toLowerCase()));
  const visibleMessages = filterVisibleMessages(messages, expanded, stat.tone === 'green' ? thread?.turns[0]?.turn_id : undefined);
  const commentaryCount = messages.length - messages.filter(m => m.phase !== 'commentary').length;

  if (authenticated === null) return <div className="boot"><Logo /><LoaderCircle className="spin" /><span>连接你的工作空间…</span></div>;
  if (!authenticated) return <Login onLogin={d => { setAuthenticated(true); setDevice(d); }} />;

  return <div className={`app ${selected ? 'has-selection' : ''} ${drawerOpen ? 'drawer-open' : ''}`}>
    {mobile && <button className="drawer-backdrop" aria-label="关闭对话侧栏遮罩" aria-hidden={!drawerOpen} tabIndex={-1} onClick={closeDrawer} />}
    <aside ref={drawerRef} id="conversation-sidebar" className="sidebar" role={mobile ? 'dialog' : undefined} aria-label={mobile ? '项目与对话侧栏' : undefined} aria-modal={mobile && drawerOpen ? true : undefined} aria-hidden={mobile && !drawerOpen ? true : undefined} inert={mobile && !drawerOpen ? true : undefined} tabIndex={-1}>
      <div className="sidebar-top"><Logo /><span className="version-tag">BETA</span><button className="icon-button drawer-close" aria-label="关闭对话侧栏" onClick={closeDrawer}><X size={21} /></button></div>
      <section className="provider-strip" aria-label="AI 客户端连接"><div className="provider-switcher" role="group" aria-label="切换 AI 客户端">{(['codex', 'deepseek'] as ProviderId[]).map(id => {
        const item = providers.find(p => p.id === id); const online = device.online && !!item?.connected;
        const status = online ? '已连接' : device.online ? item?.statusLabel || '检查中' : 'Mac 离线';
        return <button key={id} className={`provider-option ${providerId === id ? 'selected' : ''}`} aria-label={`切换到 ${providerNames[id]}，${status}`} title={`${providerNames[id]} · ${status}${providerId === id ? ' · 点击查看连接信息' : ''}`} aria-pressed={providerId === id} onClick={() => { if (id !== providerId) switchProvider(id); else setProviderInfoOpen(true); }}><span className="provider-logo"><img src={`/providers/${id}.png`} alt="" /><i className={`provider-dot ${online ? 'connected' : 'disconnected'}`} /></span></button>;
      })}</div></section>
      <div className="sidebar-title"><div><span className="eyebrow">工作空间</span><h1>项目</h1></div><div className="sidebar-title-actions"><button className={`search-toggle icon-button ${searchOpen ? 'chosen' : ''}`} aria-label={searchOpen ? '关闭项目搜索' : '搜索项目'} aria-expanded={searchOpen} onClick={() => { setSearchOpen(!searchOpen); if (searchOpen) setSearch(''); }}><Search size={20} /></button><button className="new-button" aria-label="新建对话" title="新建对话" disabled={!canWrite || !workspaces.length} onClick={showNewChat}><Plus size={21} /></button></div></div>
      {searchOpen && <label className="search-box provider-search"><Search size={17} /><input autoFocus aria-label="搜索项目" value={search} onChange={e => setSearch(e.target.value)} onKeyDown={e => { if (e.key === 'Escape') { setSearchOpen(false); setSearch(''); } }} placeholder={`搜索 ${providerName} 项目…`} /><button className="icon-button" aria-label="清除并关闭搜索" onClick={() => { setSearch(''); setSearchOpen(false); }}><X size={16} /></button></label>}
      <div className="project-list-toolbar"><span>{projects.length ? `${projects.length} 个项目` : 'Mac 上的项目'}</span><button className="icon-button" aria-label="刷新项目列表" onClick={refreshProjectTree} disabled={listLoading || !device.connectorOnline}><RefreshCw size={15} className={listLoading ? 'spin' : ''} /></button></div>
      <div className="thread-list project-tree" aria-label="项目与对话">
        {listLoading && !projects.length ? <div className="list-empty"><LoaderCircle className="spin" />正在读取 Mac 项目…</div> : filteredProjects.length ? filteredProjects.map(project => {
          const open = expandedProjects.has(project.id), ids = projectThreads[project.id];
          return <section className={`project-branch ${open ? 'expanded' : ''}`} key={project.id}>
            <button className="project-row" aria-label={`${open ? '折叠' : '展开'}项目 ${project.name}`} aria-expanded={open} aria-controls={`project-${project.id}`} onClick={() => toggleProject(project.id)}><ChevronRight className="project-chevron" size={14} /><Folder size={19} /><span>{project.name}</span></button>
            {open && <div className="project-children" id={`project-${project.id}`} role="group" aria-label={`${project.name} 项目的对话`}>
              {projectLoading[project.id] && !ids && <div className="project-placeholder"><LoaderCircle size={13} className="spin" />正在读取对话…</div>}
              {projectErrors[project.id] && <div className="project-error">{projectErrors[project.id]}<button onClick={() => loadProject(project.id)}>重试</button></div>}
              {!projectLoading[project.id] && !projectErrors[project.id] && ids?.length === 0 && <div className="project-placeholder">此项目暂无对话</div>}
              {!device.connectorOnline && !ids && <div className="project-placeholder">Mac 离线，恢复连接后再展开</div>}
              {ids?.map(id => {
                const chat = threads.find(chat => chat.threadId === id); if (!chat) return null;
                const active = device.connectorOnline && !!provider?.connected && runtime(chat).tone === 'green';
                return <button className={`project-thread ${selected === id ? 'selected' : ''}`} key={id} data-thread-id={id} aria-label={`打开对话 ${chat.title || '未命名对话'}${active ? '，运行中' : ''}`} title={chat.title} onClick={() => openThread(id)}><span>{chat.title || '未命名对话'}</span>{active && <LoaderCircle className="spin thread-running" size={15} role="status" aria-label="运行中" />}</button>;
              })}
            </div>}
          </section>;
        }) : <div className="list-empty"><Folder size={30} /><strong>{search ? '没有匹配的项目' : device.connectorOnline ? '这里还没有项目' : '等待 Mac 连接'}</strong><span>{device.connectorOnline ? `在 Mac ${providerName} 中添加项目后刷新` : 'Mac 上的连接器会主动连接这里'}</span></div>}
      </div>
      <div className="sidebar-footer compact-footer"><button className="icon-button" aria-label="退出登录" title="退出登录" onClick={async () => { try { await logout(); setAuthenticated(false); } catch (e) { notifyError(e); } }}><LogOut size={18} /></button></div>
    </aside>
    <main className={`workspace ${!selected ? 'empty-workspace' : ''}`} inert={mobile && drawerOpen ? true : undefined} aria-hidden={mobile && drawerOpen ? true : undefined}>
      {!selected ? <>
        <header className="welcome-top"><button className="icon-button drawer-trigger" data-drawer-trigger aria-label="打开项目与对话" aria-controls="conversation-sidebar" aria-expanded={drawerOpen} onClick={() => setDrawerOpen(true)}><Menu size={23} /></button><span><i className={`dot ${device.connectorOnline ? 'green' : 'muted'}`} />{device.connectorOnline ? '与 Mac 保持连接' : '等待 Mac 上线'}</span>{window.AIRemoteAndroid ? <button className="icon-button" aria-label="Android 应用设置与更新" onClick={() => window.AIRemoteAndroid?.openSettings()}><MoreHorizontal size={19} /></button> : <span>你的 AI，随时继续</span>}</header>
        <div className="welcome"><div className="welcome-art"><div className="art-orbit one" /><div className="art-orbit two" /><div className="art-core"><Terminal size={42} /></div><span className="art-spark"><Sparkles size={21} /></span><span className="art-status"><i className={`dot ${device.connectorOnline ? 'green' : 'muted'}`} />{device.connectorOnline ? '已就绪' : '等待连接'}</span></div><div className="eyebrow">从这里，继续创造</div><h1>离开桌面，<br />思路继续。</h1><p>从左上角选择一个对话，继续你的工作。</p><button className="primary" disabled={!canWrite || !workspaces.length} onClick={showNewChat}><Plus size={18} />新建对话</button>
          <div className="feature-strip"><span><Activity size={16} />实时状态</span><span><FileText size={16} />回复与文件</span><span><ListOrdered size={16} />原生队列</span></div></div>
      </> : <>
        <header className="conversation-header">
          <button className="icon-button mobile-back drawer-trigger" data-drawer-trigger aria-label="打开项目与对话" aria-controls="conversation-sidebar" aria-expanded={drawerOpen} onClick={() => setDrawerOpen(true)}><Menu size={23} /></button>
          <div className="conversation-title"><div className="conversation-breadcrumb"><img className="inline-provider-icon" src={`/providers/${providerId}.png`} alt="" />{providerName}<span>/</span>{thread?.cwd?.split('/').pop() || 'Mac'}</div><h1>{thread?.title || '正在读取对话…'}</h1></div>
          <button className="header-file-button" aria-label="查看对话文件" title="对话文件" onClick={() => setFilesOpen(true)}><Folder size={18} /><span className="header-file-label">文件</span>{!!files.length && <span className="header-file-count">{files.length}</span>}</button>
          {providerId === 'codex' && <button className="icon-button" aria-label="管理对话目标" title="对话目标" onClick={() => { setError(''); setGoalOpen(true); }}><Target size={19} /></button>}
          <button className="icon-button refresh-detail" aria-label="刷新当前对话" disabled={loading || !device.connectorOnline} onClick={() => loadDetail()}><RefreshCw size={18} className={loading ? 'spin' : ''} /></button>
          {window.AIRemoteAndroid && <button className="icon-button" aria-label="Android 应用设置与更新" onClick={() => window.AIRemoteAndroid?.openSettings()}><MoreHorizontal size={19} /></button>}
        </header>
        <div className="conversation-subheader"><span className={`status-label ${device.connectorOnline && !syncErrors.thread ? stat.tone : 'muted'}`}><i className="dot" />{syncErrors.thread ? '状态待同步' : device.connectorOnline ? stat.label : device.online ? '连接器离线' : 'Mac 离线'}</span><span className="status-hint">{device.connectorOnline ? stat.hint : '恢复连接后继续同步'}</span><span className="updated">{syncErrors.messages ? '消息同步中断' : lastUpdated ? `${shortTime(lastUpdated)} 同步` : '等待同步'}</span></div>
        {!device.connectorOnline && <div className="offline-banner" role="status"><WifiOff size={17} /><span>{device.online ? 'Mac 在线，但本地连接器暂不可用。' : 'Mac 暂时离线，正在等待重新连接。'}<small>当前显示的是本次打开时已读取的内容。</small></span></div>}
        {error && <div className="error-banner" role="alert"><span>{error}</span><button className="icon-button" aria-label="关闭错误提示" onClick={() => setError('')}><X size={16} /></button></div>}
        {!error && (syncErrors.messages || syncErrors.thread || syncErrors.queue) && <div className="error-banner" role="status"><span>{syncErrors.messages ? `消息暂未更新：${syncErrors.messages}` : syncErrors.thread ? `运行状态暂未更新：${syncErrors.thread}` : `队列暂未更新：${syncErrors.queue}`}</span></div>}
        <div className="content-scroll" ref={scrollRef} role="region" aria-label="对话消息" tabIndex={0} onWheel={event => { if (event.deltaY < 0) historyScrollIntent(); }} onKeyDown={event => { if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)) historyScrollIntent(); }} onTouchStart={event => { touchStartRef.current = event.touches[0]?.clientY ?? null; }} onTouchMove={event => {
          if (touchStartRef.current !== null && event.touches[0] && event.touches[0].clientY - touchStartRef.current > 24) historyScrollIntent();
        }} onScroll={() => {
          const element = scrollRef.current; if (!element) return;
          const atBottom = !olderRequestRef.current && element.scrollHeight - element.scrollTop - element.clientHeight < 120;
          if (atBottom) { atLatestRef.current = true; setFollowLatest(true); if (!pendingLatest.current) setHasNewMessages(false); }
          else if (Date.now() - historyIntentRef.current < 800) { atLatestRef.current = false; setFollowLatest(false); }
          if (element.scrollTop < 80 && pageLoadedRef.current && olderCursorRef.current && !scrollRestore.current && Date.now() - historyIntentRef.current < 800) loadOlder();
        }}>
          {loading && !messages.length ? <div className="panel-empty"><LoaderCircle className="spin" size={27} /><h3>正在从 Mac 读取…</h3><p>通过你的阿里云中继安全连接</p></div> : <div className="messages-panel">
            <div className="history-note"><span>与 Mac 上的同一个对话</span><button onClick={() => setExpanded(!expanded)}>{expanded ? '收起过程消息' : `显示过程消息${commentaryCount ? ` (${commentaryCount})` : ''}`}<ChevronDown size={13} /></button></div>
            {(hasOlder || loadingOlder) && <button className="history-paging" aria-label="加载更早消息" disabled={loadingOlder || !olderCursor} onClick={loadOlder}>{loadingOlder ? <LoaderCircle size={13} className="spin" /> : <ArrowUp size={13} />}{loadingOlder ? '正在读取更早消息…' : '向上滚动，查看更早消息'}</button>}
            {!messages.length && <div className="panel-empty"><MessageSquare size={30} /><h3>新的思路，从一句话开始</h3><p>在下方发送指令，{providerName} 会在 Mac 上执行。</p></div>}
            {!!messages.length && !visibleMessages.length && <div className="progress-placeholder">{providerName} 正在处理。展开过程消息可查看最近进度。</div>}
            {visibleMessages.map((m, index) => {
              const presentation = m.role === 'user' ? userPresentation(m.text) : { text: m.text, attachments: 0 };
              const turn = thread?.turns.find(t => t.turn_id === m.turnId);
              const firstReply = m.role === 'assistant' && !visibleMessages.slice(0, index).some(previous => previous.role === 'assistant' && previous.turnId === m.turnId);
              const dateDivider = m.role === 'user' && (!index || m.createdAtMs - visibleMessages[index - 1].createdAtMs > 5 * 60 * 1000);
              return <React.Fragment key={m.itemId}>
                {dateDivider && <div className="message-date">{messageDate(m.createdAtMs)}</div>}
                {firstReply && turn?.completed_at != null && <TurnDuration turn={turn} />}
                <article data-item-id={m.itemId} className={`message ${m.role} ${m.phase === 'commentary' ? 'commentary' : ''}`}>
                  {m.role !== 'user' && <div className="message-avatar assistant"><Code2 size={19} /></div>}
                  <div className="message-main">
                    {m.role !== 'user' && <div className="message-meta"><strong>{providerName}</strong>{m.phase === 'commentary' && <span className="progress-tag">执行进度</span>}{(m.phase === 'final' || m.phase === 'final_answer') && <span className="final-tag"><Check size={11} />回复</span>}<time>{shortTime(m.createdAtMs)}</time></div>}
                    {!!presentation.attachments && <div className="message-attachments"><File size={12} />{presentation.attachments} 个附件</div>}
                    <MessageBody text={presentation.text} files={files} download={download} resolveLocal={downloadLocal} />
                    {m.role === 'assistant' && m.phase !== 'commentary' && <button className="copy-message" onClick={async () => { try { await navigator.clipboard.writeText(m.text); setToast('回复已复制'); } catch { setToast('请长按文字复制'); } }}><Copy size={13} />复制回复</button>}
                  </div>
                </article>
              </React.Fragment>;
            })}
            {stat.tone === 'green' && device.connectorOnline && <div className="live-processing"><TurnDuration turn={thread?.turns[0]} live /><div className="working-indicator"><span className="typing-dots"><i /><i /><i /></span><span>{providerName} 正在执行，新的回复会自动出现</span></div></div>}
          </div>}
        </div>
        {!followLatest && <button className="jump-latest" onClick={jumpToLatest}><ArrowDown size={14} />{hasNewMessages ? '有新消息 · 回到最新' : '回到最新消息'}</button>}
        <div className="composer-wrap">
          {receipt && <ReceiptView receipt={receipt} close={() => setReceipt(null)} />}
          {providerId === 'codex' && <ModelStatus thread={thread} online={device.connectorOnline && !syncErrors.thread} />}
          <div className="composer-stack">
          <QueueDock reorder={!!provider?.features.includes('queue-reorder')} items={queue} active={stat.tone === 'green'} enabled={!!canWrite} onSend={item => queueAction(item, 'send')} onDelete={item => queueAction(item, 'delete')} onEdit={item => { setEditing(item); setEditText(queueText(item)); }} onMove={(item, direction) => queueAction(item, direction)} onClear={() => setClearing(true)} />
        {providerId === 'codex' && <GoalControl key={selected} state={thread?.goalState} open={goalOpen} close={() => setGoalOpen(false)} show={() => { setError(''); setGoalOpen(true); }} online={device.connectorOnline && !syncErrors.thread && !loading} busy={!!busy} error={error} change={changeGoal} Modal={Modal} />}
          <Composer mobile={mobile} value={draft} onChange={value => setDrafts(d => ({ ...d, [selected]: value }))} onSend={send} mode={sendMode} setMode={setSendMode} providerId={providerId} providerName={providerName} busy={!!busy} enabled={!!canWrite} hasQueue={!!queue.length} hasGoal={!!thread?.goalState?.goal} onFiles={() => setFilesOpen(true)} onNewChat={showNewChat} />
          </div>
          <div className="composer-caption">{busy || (sendMode === 'queue' ? `原生队列由 ${providerName} 自动执行，空闲时可能立即开始` : stat.tone === 'green' ? '运行中发送会追加到当前对话 · ⌘ / Ctrl + Enter 发送' : '在同一对话继续下一轮 · ⌘ / Ctrl + Enter 发送')}</div>
        </div>
      </>}
    </main>
    {!selected && error && <div className="global-error" role="alert">{error}<button aria-label="关闭错误提示" onClick={() => setError('')}><X size={16} /></button></div>}
    {toast && <div className="toast" role="status"><CheckCheck size={17} />{toast}</div>}
    {providerInfoOpen && <Modal title={`${providerName} 连接信息`} close={() => setProviderInfoOpen(false)}><div className="provider-info"><img src={`/providers/${providerId}.png`} alt="" /><div><strong>{provider?.clientName || providerName}</strong><span>{device.online ? provider?.statusLabel || '正在检查…' : 'Mac 离线'}{provider?.version ? ` · ${provider.version}` : ''}</span></div></div><p className="modal-description">{device.connectorOnline ? `已连接 Mac 上正在运行的 ${provider?.clientName || providerName}。展开项目后读取对话，打开对话后同步消息。` : `请在 Mac 上打开 ${provider?.clientName || providerName}，完成登录后重试。`}</p><div className="provider-capabilities"><span>项目与对话</span><span>回复与文件</span><span>原生队列</span><span>同一对话续聊</span></div><button className="secondary full" onClick={() => setProviderInfoOpen(false)}>知道了</button></Modal>}
    {filesOpen && <Modal className="files-modal" title="对话文件" close={() => setFilesOpen(false)}><div className="files-toolbar"><p>需要时从 Mac 获取文件。</p><button className="icon-button" aria-label="刷新文件列表" disabled={filesLoading || !device.connectorOnline} onClick={() => loadFiles()}><RefreshCw size={16} className={filesLoading ? 'spin' : ''} /></button></div>{error && <p className="error-text" role="alert">{error}</p>}{filesLoading && !files.length ? <div className="panel-empty"><LoaderCircle size={24} className="spin" /><p>正在读取文件…</p></div> : <FilesView files={files} disabled={!canWrite} download={download} />}</Modal>}
    {newChat && <NewChat providerName={providerName} workspaces={workspaces} defaultWorkspace={preferredWorkspace} busy={!!busy} error={error} close={() => { setNewChat(false); setError(''); }} submit={async data => {
      const result = await mutate('/api/threads', 'POST', data, '正在 Mac 上创建对话…');
      if (result?.result?.threadId) {
        setNewChat(false); openThread(result.result.threadId); setReceipt(result); setToast('新对话已创建');
        const project = projects.find(project => project.roots.includes(data.cwd));
        if (project && expandedProjectsRef.current.has(project.id)) loadProject(project.id);
      }
    }} />}
    {editing && <Modal title="编辑队列指令" close={() => setEditing(null)}><form onSubmit={async e => { e.preventDefault(); const result = await mutate(`${threadPath(selected)}/queue/${editing.id}`, 'PATCH', { text: editText }, '正在保存队列…'); if (result) { setEditing(null); setToast('指令已更新'); await loadDetail(selected, true); } }}><label>指令内容<textarea autoFocus value={editText} onChange={e => setEditText(e.target.value)} rows={7} required /></label><div className="modal-actions"><button type="button" className="secondary" onClick={() => setEditing(null)}>取消</button><button className="primary" disabled={!!busy || !editText.trim()}>保存修改</button></div></form></Modal>}
    {clearing && <Modal title="清空当前对话的队列？" close={() => setClearing(false)}><p className="modal-description">将删除当前看到的 {queue.length} 条待执行指令。已发送的消息和正在执行的任务不受影响。</p><div className="modal-actions"><button className="secondary" onClick={() => setClearing(false)}>保留队列</button><button className="primary danger-bg" disabled={!!busy} onClick={async () => { const result = await mutate(`${threadPath(selected)}/queue`, 'DELETE', { ids: queue.map(q => q.id) }, '正在清空队列…'); if (result) { setClearing(false); await loadDetail(selected, true); } }}>确认清空</button></div></Modal>}
  </div>;
}

function ModelStatus({ thread, online }: { thread?: Thread; online: boolean }) {
  const settings = online ? thread?.modelSettings : null;
  const labels = modelPresentation(settings);
  return <div className="model-status" role="status" aria-label="当前模型与工作模式">
    <span className="model-name" title={settings?.model || ''}>{online ? labels.model : '模型设置待同步'}{labels.effort && <span className="model-effort">{labels.effort}</span>}</span>
    <span className="model-modes">{labels.mode && <span>{labels.mode}</span>}{labels.permission && <span className={labels.permission === '完全访问' ? 'full-access' : ''}><ShieldCheck size={12} />{labels.permission}</span>}</span>
  </div>;
}

function Composer({ mobile, value, onChange, onSend, mode, setMode, providerId, providerName, busy, enabled, hasQueue, hasGoal, onFiles, onNewChat }: {
  mobile: boolean; value: string; onChange: (value: string) => void; onSend: () => void;
  mode: 'send' | 'queue'; setMode: (mode: 'send' | 'queue') => void;
  providerId: ProviderId; providerName: string; busy: boolean; enabled: boolean; hasQueue: boolean; hasGoal: boolean;
  onFiles: () => void; onNewChat: () => void;
}) {
  const [focused, setFocused] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const form = useRef<HTMLFormElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const tools = useRef<HTMLButtonElement>(null);
  const expanded = !mobile || focused || !!value || mode === 'queue' || hasQueue || hasGoal || menuOpen;
  useLayoutEffect(() => {
    const element = input.current;
    if (!element) return;
    element.style.height = '0px';
    element.style.height = `${expanded ? Math.min(140, Math.max(28, element.scrollHeight)) : 28}px`;
  }, [value, expanded]);
  useEffect(() => {
    if (!menuOpen) return;
    const outside = (event: PointerEvent) => { if (!form.current?.contains(event.target as Node)) setMenuOpen(false); };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); setMenuOpen(false); tools.current?.focus(); return; }
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
      const items = [...form.current!.querySelectorAll<HTMLButtonElement>('.composer-actions button:not(:disabled)')];
      if (!items.length) return;
      const at = items.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : event.key === 'ArrowDown' ? (at + 1) % items.length : (at - 1 + items.length) % items.length;
      event.preventDefault(); items[next].focus();
    };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape); };
  }, [menuOpen]);
  const chooseMode = (next: 'send' | 'queue') => { setMode(next); setMenuOpen(false); input.current?.focus(); };
  return <form ref={form} className={`composer ${expanded ? 'expanded' : 'collapsed'}`} onSubmit={event => { event.preventDefault(); onSend(); }} onBlurCapture={() => {
    requestAnimationFrame(() => { if (!form.current?.contains(document.activeElement)) setFocused(false); });
  }}>
    <textarea ref={input} aria-label="新的 AI 指令" value={value} onChange={event => onChange(event.target.value)} onFocus={() => setFocused(true)} placeholder={mode === 'queue' ? '写下下一步指令…' : `用 ${providerName} 工作`} rows={1} maxLength={120000} onKeyDown={event => {
      if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); onSend(); }
    }} />
    <div className="composer-bottom">
      <div className="composer-tools">
        <button ref={tools} type="button" className="icon-button composer-add" aria-label="对话操作" aria-expanded={menuOpen} aria-haspopup="menu" onClick={() => setMenuOpen(!menuOpen)}><Plus size={24} strokeWidth={1.8} /></button>
        {menuOpen && <div className="composer-actions" role="menu" aria-label="对话操作菜单">
          <button type="button" role="menuitemradio" aria-checked={mode === 'send'} onClick={() => chooseMode('send')}><ArrowUpRight size={17} />立即发送{mode === 'send' && <Check size={15} />}</button>
          <button type="button" role="menuitemradio" aria-checked={mode === 'queue'} onClick={() => chooseMode('queue')}><ListOrdered size={17} />加入原生队列{mode === 'queue' && <Check size={15} />}</button>
          <div className="composer-menu-separator" />
          <button type="button" role="menuitem" onClick={() => { setMenuOpen(false); onFiles(); }}><Folder size={17} />对话文件</button>
          <button type="button" role="menuitem" disabled={!enabled} onClick={() => { setMenuOpen(false); onNewChat(); }}><MessageSquare size={17} />新建对话</button>
        </div>}
      </div>
      <div className="composer-provider"><img className="inline-provider-icon" src={`/providers/${providerId}.png`} alt="" />{providerName}</div>
      <div className="composer-controls"><label className="send-mode"><select aria-label="指令发送方式" value={mode} onChange={event => setMode(event.target.value as 'send' | 'queue')}><option value="send">立即发送</option><option value="queue">加入队列</option></select><ChevronDown size={13} /></label><button type="submit" className="send-button" aria-label={mode === 'send' ? '发送指令' : '加入队列'} disabled={!enabled || !value.trim()}>{busy ? <LoaderCircle size={18} className="spin" /> : mode === 'queue' ? <ListOrdered size={20} /> : <ArrowUp size={21} />}</button></div>
    </div>
  </form>;
}

function TurnDuration({ turn, live = false }: { turn?: Thread['turns'][number]; live?: boolean }) {
  const [clock, setClock] = useState(Date.now());
  useEffect(() => {
    if (!live) return;
    setClock(Date.now());
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [turn?.turn_id, live]);
  if (typeof turn?.started_at !== 'number') return null;
  // The native thread history stores seconds since the Unix epoch.
  const end = typeof turn.completed_at === 'number' ? turn.completed_at * 1000 : live ? clock : null;
  if (end === null || end < turn.started_at * 1000) return null;
  const total = Math.floor((end - turn.started_at * 1000) / 1000);
  const hours = Math.floor(total / 3600), minutes = Math.floor(total % 3600 / 60), seconds = total % 60;
  const duration = `${hours ? `${hours} 小时 ` : ''}${minutes || hours ? `${minutes} 分钟 ` : ''}${seconds} 秒`;
  return <div className={`turn-duration ${live ? 'live' : ''}`} data-turn-id={turn.turn_id}>已处理 {duration}</div>;
}

function QueueDock({ reorder, items, active, enabled, onSend, onDelete, onEdit, onMove, onClear }: {
  reorder: boolean; items: QueueItem[]; active: boolean; enabled: boolean;
  onSend: (item: QueueItem) => void; onDelete: (item: QueueItem) => void; onEdit: (item: QueueItem) => void;
  onMove: (item: QueueItem, direction: 'up' | 'down') => void; onClear: () => void;
}) {
  const [menu, setMenu] = useState<{ id: string; left: number; top: number } | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (!menu) return;
    const outside = (event: PointerEvent) => {
      const target = event.target as Element;
      if (!menuRef.current?.contains(target) && !target.closest('[data-queue-more]')) setMenu(null);
    };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { setMenu(null); triggerRef.current?.focus(); } };
    const close = () => setMenu(null);
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    window.addEventListener('resize', close);
    menuRef.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus();
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape); window.removeEventListener('resize', close); };
  }, [menu?.id]);
  useEffect(() => { if (menu && !items.some(item => item.id === menu.id)) setMenu(null); }, [items, menu?.id]);
  if (!items.length) return null;
  const item = items.find(item => item.id === menu?.id);
  const index = item ? items.indexOf(item) : -1;
  return <>
    <section className="inline-queue" aria-label="待执行指令队列">
      <div className="inline-queue-rows">
        {items.map((item, index) => <div className="inline-queue-row" key={item.id}>
          <CornerDownRight className="queue-indent" size={17} />
          <button className="queue-preview" title={queueText(item)} aria-label={`编辑第 ${index + 1} 条排队指令`} disabled={!enabled} onClick={() => onEdit(item)}>{queueText(item)}</button>
          <div className="inline-queue-actions">
            <button className="queue-guide" aria-label={`立即执行第 ${index + 1} 条排队指令`} title={active ? '引导当前对话执行这条指令' : '立即执行这条指令'} disabled={!enabled} onClick={() => onSend(item)}><CornerUpRight size={16} /><span>{active ? '引导' : '发送'}</span></button>
            <button className="queue-icon" aria-label={`删除第 ${index + 1} 条排队指令`} title="删除指令" disabled={!enabled} onClick={() => onDelete(item)}><Trash2 size={16} /></button>
            <button className="queue-icon" data-queue-more aria-label={`第 ${index + 1} 条排队指令的更多操作`} title="更多操作" aria-expanded={menu?.id === item.id} aria-haspopup="menu" disabled={!enabled} onClick={event => {
              const rect = event.currentTarget.getBoundingClientRect(); triggerRef.current = event.currentTarget;
              setMenu(menu?.id === item.id ? null : { id: item.id, left: Math.max(8, Math.min(rect.right - 188, window.innerWidth - 196)), top: Math.max(8, Math.min(rect.bottom + 6, window.innerHeight - 216)) });
            }}><MoreHorizontal size={18} /></button>
          </div>
        </div>)}
      </div>
    </section>
    {menu && item && createPortal(<div ref={menuRef} className="queue-context-menu" role="menu" aria-label="队列指令操作" style={{ left: menu.left, top: menu.top }}>
      <button role="menuitem" disabled={!enabled} onClick={() => { setMenu(null); onEdit(item); }}><Pencil size={15} />编辑指令</button>
      {reorder && <button role="menuitem" disabled={!enabled || index === 0} onClick={() => { setMenu(null); onMove(item, 'up'); }}><ArrowUp size={15} />上移</button>}
      {reorder && <button role="menuitem" disabled={!enabled || index === items.length - 1} onClick={() => { setMenu(null); onMove(item, 'down'); }}><ArrowDown size={15} />下移</button>}
      <div className="queue-menu-separator" />
      <button role="menuitem" className="danger" disabled={!enabled} onClick={() => { setMenu(null); onClear(); }}><Trash2 size={15} />清空队列…</button>
    </div>, document.body)}
  </>;
}

function FilesView({ files, disabled, download }: { files: Artifact[]; disabled: boolean; download: (file: Artifact) => void }) {
  return <>
    {!files.length ? <div className="panel-empty"><div className="empty-icon"><Folder size={30} /></div><h3>文件准备好后，会出现在这里</h3><p>让 AI 在回复里附上生成文件的路径链接。</p></div> : <div className="file-list">{files.map(file => <div className="file-card" key={file.id}><div className={`file-icon ${file.name.endsWith('.json') ? 'json' : ''}`}>{file.name.endsWith('.json') ? <Code2 size={23} /> : <FileText size={23} />}</div><div className="file-info"><strong>{file.name}</strong><span>{fileSize(file.size)}<i />来自当前对话</span></div><button className="icon-button download-button" aria-label={`下载 ${file.name}`} disabled={disabled} onClick={() => download(file)}><Download size={20} /></button></div>)}</div>}
    <div className="scope-note"><ShieldCheck size={15} />仅可下载 Mac 上已授权目录内的文件 · 单个文件最大 64 MB</div>
  </>;
}

function MessageBody({ text, files, download, resolveLocal }: { text: string; files: Artifact[]; download: (file: Artifact) => void; resolveLocal: (path: string) => void }) {
  const [open, setOpen] = useState(false);
  const long = text.length > 8000;
  const visible = long && !open ? text.slice(0, 5000) + '\n\n…' : text;
  return <><div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{
    a: ({ href, children }) => {
      let local = href || ''; try { local = decodeURIComponent(local).replace(/:\d+$/, ''); } catch { /* keep original */ }
      if (local.startsWith('/') && !local.startsWith('//')) {
        const file = files.find(f => f.path === local);
        return file ? <button className="artifact-link" onClick={() => download(file)}><File size={13} />{children}<Download size={13} /></button> : <button className="artifact-link" onClick={() => resolveLocal(local)}><File size={13} />{children}<Download size={13} /></button>;
      }
      return /^https?:\/\//.test(href || '') ? <a href={href} target="_blank" rel="noopener noreferrer">{children}<ArrowUpRight size={11} /></a> : <span>{children}</span>;
    },
    img: ({ alt }) => <span className="image-placeholder">[图片：{alt || '附件'}]</span>,
  }}>{visible}</ReactMarkdown></div>{long && <button className="expand-message" onClick={() => setOpen(!open)}>{open ? '收起长消息' : '展开完整消息'}<ChevronDown size={13} /></button>}</>;
}
function ReceiptView({ receipt: r, close }: { receipt: Receipt; close: () => void }) {
  const received = r.delivery?.receivedInHistory;
  const completed = r.delivery?.turnStatus === 'completed';
  const replies = r.delivery?.laterAssistantMessages?.filter(m => m.phase === 'final' || m.phase === 'final_answer').length || 0;
  const queued = r.kind === 'queue_add' && !received;
  return <div className="receipt"><div className={`receipt-icon ${received ? 'confirmed' : ''}`}>{received ? <CheckCheck size={17} /> : <Clock3 size={17} />}</div><div><strong>{completed ? '这一轮已完成' : received ? '同一对话已收到' : queued ? '已保存到原生队列' : r.requestStatus === 'outcome_unknown' ? '执行结果待核对' : 'Mac 已受理'}</strong><span>{completed ? (replies ? '已读到 AI 的最终回复' : '已核对本轮完成状态') : received ? '正在等待 AI 的后续回复' : queued ? 'AI 空闲时可能立即自动执行' : '正在核对原对话中的用户消息'}</span></div><span className="receipt-id" title={r.operationId}>#{r.operationId.slice(0, 8)}</span><button className="icon-button" aria-label="关闭发送回执" onClick={close}><X size={15} /></button></div>;
}
function NewChat({ providerName, workspaces, defaultWorkspace, busy, error, close, submit }: { providerName: string; workspaces: string[]; defaultWorkspace?: string; busy: boolean; error: string; close: () => void; submit: (data: { title: string; cwd: string; text: string }) => Promise<void> }) {
  const [title, setTitle] = useState('');
  const [cwd, setCwd] = useState(defaultWorkspace || workspaces[0] || '');
  const [text, setText] = useState('');
  return <Modal title="开始一个新对话" close={close}><p className="modal-description">在你的 Mac 上创建真实 {providerName} 对话。它会保存在本机对话库中。</p><form onSubmit={e => { e.preventDefault(); submit({ title, cwd, text }); }}><label>对话名称<input autoFocus placeholder="例如：完成登录页面" value={title} onChange={e => setTitle(e.target.value)} required maxLength={120} /></label><label>工作目录<select aria-label="新对话工作目录" value={cwd} onChange={e => setCwd(e.target.value)} required>{workspaces.map(w => <option key={w} value={w}>{w}</option>)}</select><small>使用 Mac 连接器已授权的项目目录</small></label><label>第一条指令 <span className="optional">可选</span><textarea placeholder={`告诉 ${providerName} 想完成什么…`} value={text} onChange={e => setText(e.target.value)} rows={4} /></label>{error && <p role="alert" className="error-text">{error}</p>}<div className="modal-actions"><button type="button" className="secondary" onClick={close}>取消</button><button className="primary" disabled={busy || !title.trim() || !cwd}>{busy ? <LoaderCircle className="spin" size={17} /> : <Plus size={17} />}创建对话</button></div></form></Modal>;
}

function AIRemote() {
  const initialProvider = (): ProviderId => location.hash.startsWith('#deepseek') ? 'deepseek' : location.hash.length > 1 ? 'codex' : localStorage.getItem('airemote:provider') === 'deepseek' ? 'deepseek' : 'codex';
  const [providerId, setProviderId] = useState<ProviderId>(initialProvider);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [draftsByProvider, setDraftsByProvider] = useState<Record<ProviderId, Record<string, string>>>({ codex: {}, deepseek: {} });
  useEffect(() => {
    const change = () => { if (location.hash.startsWith('#deepseek')) setProviderId('deepseek'); else if (location.hash) setProviderId('codex'); };
    window.addEventListener('hashchange', change);
    return () => window.removeEventListener('hashchange', change);
  }, []);
  const switchProvider = (id: ProviderId) => {
    localStorage.setItem('airemote:provider', id);
    location.hash = id === 'codex' ? '' : id;
    setProviderId(id);
  };
  // Remounting gives each provider its own requests, draft, cursor and poll lifetime.
  const setDrafts: React.Dispatch<React.SetStateAction<Record<string, string>>> = value => setDraftsByProvider(previous => ({ ...previous, [providerId]: typeof value === 'function' ? value(previous[providerId]) : value }));
  return <App key={providerId} providerId={providerId} switchProvider={switchProvider} drafts={draftsByProvider[providerId]} setDrafts={setDrafts} drawerOpen={drawerOpen} setDrawerOpen={setDrawerOpen} />;
}
createRoot(document.getElementById('root')!).render(<AIRemote />);

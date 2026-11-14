import { useState } from 'react';
import { LoaderCircle, Pause, Pencil, Play, Target, Trash2, X } from 'lucide-react';
import { GoalState, Receipt, goalPresentation } from './api';
import './goals.css';

type ModalType = React.ComponentType<{ title: string; children: React.ReactNode; close: () => void; className?: string }>;
type Props = { state?: GoalState; open: boolean; close: () => void; show: () => void; online: boolean; busy: boolean; error: string;
  change: (method: string, body: object) => Promise<Receipt | null>; Modal: ModalType };

export function GoalControl({ state, open, close, show, online, busy, error, change, Modal }: Props) {
  const goal = state?.goal, labels = goal ? goalPresentation(goal) : null;
  const [editing, setEditing] = useState(false);
  const [closing, setClosing] = useState(false);
  const [feedback, setFeedback] = useState('');
  const enabled = !!state?.readable && state.writable && online && !busy;
  const dismiss = () => { if (!busy) { setEditing(false); setClosing(false); setFeedback(''); close(); } };
  async function act(method: string, body: object) {
    setFeedback('');
    const result = await change(method, { ...body, revision: state?.revision });
    if (!result) return false;
    if (result.requestStatus !== 'applied' || !result.result?.acknowledged) {
      setFeedback('操作结果暂不确定，请刷新核对当前目标后再操作。'); return false;
    }
    setFeedback(result.result.nativeConfirmed ? 'Codex 已确认目标更新' : 'Codex 已受理，目标随后发生了变化，请查看最新状态。');
    setEditing(false); setClosing(false); return true;
  }
  return <>
    {goal && labels && <div className={`goal-dock ${labels.active ? 'active' : ''}`}>
      <Target size={17} />
      <button className="goal-dock-main" onClick={show} aria-label={`查看对话目标，${labels.label}`}><strong>{labels.active ? '进行中的目标' : `${labels.label}的目标`}</strong><span>{goal.objective}</span></button>
      <time className="goal-dock-time" title={`已处理 ${labels.time}`}>{elapsed(goal.timeUsedSeconds)}</time>
      <button className="icon-button" aria-label="关闭目标" title="关闭目标" disabled={!enabled} onClick={() => { setClosing(true); show(); }}><Trash2 size={17} /></button>
      {goal.status !== 'complete' && <button className="icon-button" aria-label={labels.active ? '暂停目标' : '继续目标'} title={labels.active ? '暂停目标' : '继续目标'} disabled={!enabled} onClick={() => act('PATCH', { status: labels.active ? 'paused' : 'active' })}>{labels.active ? <Pause size={17} /> : <Play size={17} />}</button>}
    </div>}
    {open && <Modal title={goal ? '对话目标' : '设置对话目标'} close={dismiss} className="goal-modal">
      {error && <p className="error-text" role="alert">{error}</p>}
      {feedback && <p className="goal-feedback" role="status">{feedback}</p>}
      {!state?.readable && <p className="modal-description">目标状态暂未同步。连接 Mac 后刷新此对话。</p>}
      {state?.readable && !state.writable && <p className="goal-setup" role="status">{state.message || '请在 Mac AIRemote 中启用目标通道。'}<small>首次启用：正常退出 Codex，再点 Mac AIRemote 的“通过目标通道启动 Codex”。</small></p>}
      {!online && <p className="error-text">Mac 未连接，当前内容可能已变化。</p>}
      {closing && goal ? <div className="goal-close-confirm"><p>关闭这个目标？</p><span>目标将从当前对话移除，停止按这个目标自动推进。对话消息会保留；正在执行的这一轮仍可能继续。</span><div className="modal-actions"><button className="secondary" disabled={busy} onClick={() => setClosing(false)}>保留目标</button><button className="primary danger-bg" disabled={!enabled} onClick={() => act('DELETE', {})}>关闭目标</button></div></div>
      : (editing || !goal) && state?.readable ? <GoalEditor key={editing ? 'edit' : 'new'} state={state} enabled={enabled} busy={busy} cancel={goal ? () => setEditing(false) : dismiss} submit={data => act(goal ? 'PATCH' : 'POST', data)} />
      : goal && labels && <>
        <div className={`goal-state-label ${goal.status}`}><i className="dot" />{labels.label}</div>
        <p className="goal-objective">{goal.objective}</p>
        <div className="goal-stats"><span>已用 <strong>{labels.tokens}</strong> tokens{labels.budget && <> / {labels.budget}</>}</span><span>已处理 <strong>{labels.time}</strong></span></div>
        {state?.source === 'persisted-goal' && <p className="goal-note">当前为已保存的目标状态。</p>}
        <div className="goal-actions">
          <button className="secondary" disabled={!enabled} onClick={() => { setFeedback(''); setEditing(true); }}><Pencil size={15} />修改目标</button>
          {goal.status !== 'complete' && <button className="secondary" disabled={!enabled} onClick={() => act('PATCH', { status: labels.active ? 'paused' : 'active' })}>{labels.active ? <Pause size={15} /> : <Play size={15} />}{labels.active ? '暂停目标' : '继续目标'}</button>}
          <button className="secondary goal-close" disabled={!enabled} onClick={() => setClosing(true)}><X size={15} />关闭目标</button>
        </div>
        <p className="goal-note">暂停后不再自动推进目标，当前轮次仍可能继续。继续目标会恢复 Codex 的自动执行。</p>
      </>}
    </Modal>}
  </>;
}

function elapsed(value: number) {
  const s = Math.max(0, Math.floor(value || 0)), d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return `${d ? `${d}d ` : ''}${d || h ? `${h}h ` : ''}${d || h || m ? `${m}m ` : ''}${s % 60}s`;
}

function GoalEditor({ state, enabled, busy, cancel, submit }: { state: GoalState; enabled: boolean; busy: boolean; cancel: () => void; submit: (data: object) => Promise<boolean> }) {
  const [objective, setObjective] = useState(state.goal?.objective || '');
  const [budget, setBudget] = useState(state.goal?.tokenBudget?.toString() || '');
  const [paused, setPaused] = useState(false);
  const [revision] = useState(state.revision);
  const stale = revision !== state.revision;
  const budgetNumber = budget.trim() ? Number(budget) : null;
  const validBudget = budgetNumber == null || Number.isSafeInteger(budgetNumber) && budgetNumber > 0;
  return <form className="goal-editor" onSubmit={event => {
    event.preventDefault();
    if (!enabled || stale || !validBudget || !objective.trim()) return;
    submit({ objective: objective.trim(), tokenBudget: budgetNumber, ...(!state.goal ? { status: paused ? 'paused' : 'active' } : {}) });
  }}>
    <label>目标<textarea autoFocus value={objective} maxLength={32000} rows={5} required disabled={busy} onChange={event => setObjective(event.target.value)} placeholder="告诉 Codex 需要持续完成什么…" /></label>
    <label>Token 预算（可选）<input inputMode="numeric" value={budget} disabled={busy} onChange={event => setBudget(event.target.value)} placeholder="留空表示不设置预算" /></label>
    {!validBudget && <p className="error-text">请输入大于 0 的整数。</p>}
    {!state.goal && <label className="goal-pause-option"><input type="checkbox" checked={paused} disabled={busy} onChange={event => setPaused(event.target.checked)} />先保存为暂停状态</label>}
    {!state.goal && !paused && <p className="goal-note">创建后 Codex 会开始按目标自动执行。</p>}
    {stale && <p className="error-text" role="alert">目标已经变化，请返回查看最新状态后再修改。</p>}
    <div className="modal-actions"><button type="button" className="secondary" disabled={busy} onClick={cancel}>返回</button><button className="primary" disabled={!enabled || stale || !validBudget || !objective.trim()}>{busy && <LoaderCircle size={15} className="spin" />}{state.goal ? '保存修改' : paused ? '保存目标' : '开始目标'}</button></div>
  </form>;
}

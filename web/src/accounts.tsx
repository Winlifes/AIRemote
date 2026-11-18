import React, { useEffect, useState } from 'react';
import { ArrowLeft, Copy, Link2, Monitor, RefreshCw, ShieldCheck } from 'lucide-react';
import { api, type RelaySession } from './api';
import './accounts.css';

type Computer = { id: string; account: string; name: string; platform: string; online: boolean; connectorOnline: boolean; revoked?: boolean };
type Account = { id: string; username: string; enabled: number };
type Invitation = { id: string; username: string; expiresAt: number };
type Overview = { devices: Computer[]; accounts?: Account[]; invitations?: Invitation[] };
type Code = { code: string; expiresAt: number; username?: string };

function post<T>(path: string, value: object = {}) {
  return api<T>(path, { method: 'POST', body: JSON.stringify(value) });
}

function Computers({ devices, revoke, busy }: { devices: Computer[]; revoke: (computer: Computer) => void; busy: boolean }) {
  if (!devices.length) return <p className="account-muted">还没有配对的电脑。</p>;
  return <ul className="account-list">{devices.map(mac => <li key={mac.id}><Monitor size={19} aria-hidden="true" /><div><strong>{mac.name}</strong><small>{mac.platform === 'windows' ? 'Windows' : 'Mac'} · {mac.revoked ? '已解绑' : mac.connectorOnline ? '已连接' : mac.online ? '连接器未就绪' : '离线'}</small></div>{!mac.revoked && <button type="button" className="account-text-button" disabled={busy} onClick={() => revoke(mac)}>解绑</button>}</li>)}</ul>;
}

export function ManagementPage({ identity, admin = false }: { identity: RelaySession; admin?: boolean }) {
  const [data, setData] = useState<Overview>();
  const [code, setCode] = useState<Code>();
  const [username, setUsername] = useState('');
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const path = admin ? '/api/admin' : '/api/account';
  useEffect(() => {
    let active = true;
    const refresh = async () => {
      try {
        const value = await api<Overview>(path);
        if (active) setData(value);
      } catch (e) { if (active) setError(String(e instanceof Error ? e.message : e)); }
    };
    void refresh();
    const timer = setInterval(() => { if (!document.hidden) void refresh(); }, 6000);
    return () => { active = false; clearInterval(timer); };
  }, [path]);
  async function action(run: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true); setError('');
    try { await run(); setData(await api<Overview>(path)); }
    catch (e) { setError(e instanceof Error ? e.message : '操作未完成，请重试。'); }
    finally { setBusy(false); }
  }
  const revoke = (device: Computer) => {
    if (window.confirm(`解绑 ${device.name}？这台电脑需要重新配对才能连接。`)) {
      void action(() => post(`${path}/devices/${device.id}/revoke`));
    }
  };
  const generate = (url: string, value?: object) => action(async () => { setCode(await post<Code>(url, value)); setCopied(false); });
  return <main className="account-page">
    <header className="account-top"><a className="account-back" href="/"><ArrowLeft size={17} />返回对话</a><span>{identity.username}</span></header>
    <div className="account-heading"><h1>{admin ? '管理员' : '账号与设备'}</h1>{(identity.isAdmin || admin) && <a className="account-text-button" href={admin ? '/account' : '/admin'}>{admin ? <Monitor size={17} /> : <ShieldCheck size={17} />}{admin ? '我的设备' : '管理员'}</a>}</div>
    {error && <p className="error-text" role="alert">{error}</p>}
    <section className="account-card">
      {admin
        ? <form className="account-invite" onSubmit={event => { event.preventDefault(); void generate('/api/admin/invitations', { username }); }}><label>新账号名称<input required minLength={3} maxLength={32} value={username} onChange={event => setUsername(event.target.value)} autoComplete="off" placeholder="例如 lin" /></label><button className="primary" disabled={busy}>生成邀请码</button></form>
        : <><h2>配对电脑</h2><p className="account-muted">在电脑端 AIRemote 填写此网站地址和配对码。</p><button className="primary" disabled={busy} onClick={() => void generate('/api/account/pairings')}><Link2 size={16} />生成配对码</button></>}
      {code && <div className="account-code"><div><strong>{code.username ? `${code.username} 的邀请码` : '一次性配对码'}</strong><small>{admin ? '在登录页选择「使用邀请码」' : location.origin} · {new Date(code.expiresAt * 1000).toLocaleString('zh-CN')} 到期</small></div><input aria-label={admin ? '邀请码' : '配对码'} readOnly value={code.code} onFocus={event => event.target.select()} /><button className="secondary" onClick={async () => { try { await navigator.clipboard.writeText(code.code); setCopied(true); } catch { setError('请选中验证码并复制。'); } }}><Copy size={15} />{copied ? '已复制' : '复制'}</button></div>}
    </section>
    {!data && <p className="account-muted" role="status">正在读取…</p>}
    {data && (admin ? <>
      {data.invitations && data.invitations.length > 0 && <section className="account-card"><h2>待使用的邀请码</h2><ul className="account-list">{data.invitations.map(invite => <li key={invite.id}><div><strong>{invite.username}</strong><small>{new Date(invite.expiresAt * 1000).toLocaleString('zh-CN')} 到期</small></div><button className="account-text-button" disabled={busy} onClick={() => void action(async () => { await post(`/api/admin/invitations/${invite.id}/revoke`); if (code?.username === invite.username) setCode(undefined); })}>撤销</button></li>)}</ul></section>}
      <section className="account-card"><h2>账号和设备</h2>{data.accounts?.map(account => <div className="managed-account" key={account.id}><div className="managed-account-title"><div><strong>{account.username}</strong> <span className="account-muted">{account.id === 'owner' ? '管理员' : account.enabled ? '已启用' : '已停用'}</span></div>{account.id !== 'owner' && <button className="account-text-button" disabled={busy} onClick={() => { if (!account.enabled || window.confirm(`停用 ${account.username}？其网页登录和设备连接会中断。`)) void action(() => post(`/api/admin/accounts/${account.id}/enabled`, { enabled: !account.enabled })); }}>{account.enabled ? '停用账号' : '启用账号'}</button>}</div><Computers devices={data.devices.filter(mac => mac.account === account.id)} revoke={revoke} busy={busy} /></div>)}</section>
    </> : <section className="account-card"><div className="account-heading"><h2>我的电脑</h2><button className="icon-button" aria-label="刷新设备" onClick={() => void action(async () => {})} disabled={busy}><RefreshCw size={16} /></button></div><Computers devices={data.devices} revoke={revoke} busy={busy} /></section>)}
  </main>;
}

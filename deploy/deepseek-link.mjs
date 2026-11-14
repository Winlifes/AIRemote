// Local companion for the existing DeepSeek Host. No new Host or agent is run.
import { chmodSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';

export const name = 'AIRemote local connection';
export const inject = ['connection', 'webServer'];

export function apply(ctx, config) {
  const target = resolve(config.connectionFile);
  let published = '';
  const publish = () => {
    if (ctx.webServer.port === undefined) return;
    const origin = ctx.webServer.getOrigin();
    if (new URL(origin).hostname !== '127.0.0.1') throw new Error('AIRemote requires a loopback DeepSeek Host');
    const authenticatedUrl = ctx.connection.authenticatedUrl(origin + '/');
    const content = JSON.stringify({ version: 1, origin, authenticatedUrl, pid: process.pid, desktopVersion: '0.2.2-rc.1' });
    if (content === published) return;
    chmodSync(dirname(target), 0o700);
    const temporary = target + '.next';
    writeFileSync(temporary, content, { mode: 0o600 });
    chmodSync(temporary, 0o600);
    renameSync(temporary, target);
    published = content;
  };
  ctx.effect(() => {
    publish();
    const timer = setInterval(publish, 1000);
    return () => {
      clearInterval(timer);
      try { if (readFileSync(target, 'utf8') === published) unlinkSync(target); } catch { /* Another Host generation may own the file. */ }
    };
  });
}

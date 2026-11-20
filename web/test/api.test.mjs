import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source = ts.transpileModule(readFileSync(new URL('../src/api.ts', import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText;
const { runtime, providerPath, modelPresentation, goalPresentation } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

test('metadata-only chat rows can have no live runtime yet', () => {
  assert.equal(runtime({ runtimeStatus: null }).label, '状态未知');
  assert.equal(runtime({ runtimeStatus: undefined }).label, '状态未知');
});
test('idle read-only sessions report completion without offering to continue', () => {
  assert.deepEqual(runtime({ runtimeStatus: 'idle', latestPersistedTurnStatus: 'completed', writeAvailable: false }), {
    label: '空闲', tone: 'blue', hint: '上一轮已完成',
  });
});
test('native approvals stay visible rather than looking completed', () => {
  assert.equal(runtime({ runtimeStatus: { type: 'active', activeFlags: ['approval'] } }).label, '等待处理');
});
test('provider request scopes preserve pagination and never address the other AI', () => {
  const path = '/api/threads/session-fixture/messages?limit=40&before=cursor';
  assert.equal(providerPath('deepseek', path), '/api/providers/deepseek/threads/session-fixture/messages?limit=40&before=cursor');
  assert.equal(providerPath('codex', '/api/projects'), '/api/projects');
});

test('model status shows the actual model, effort, mode and access separately', () => {
  assert.deepEqual(modelPresentation({ model: 'gpt-6.1-sol', reasoningEffort: 'max', collaborationMode: 'plan', permissionMode: 'workspaceWrite' }), {
    model: 'GPT-6.1 Sol', effort: 'Max', mode: '计划', permission: '工作区写入',
  });
});
test('missing model metadata never pretends to be a selected model or full access', () => {
  assert.deepEqual(modelPresentation(null), { model: '模型设置待同步', effort: null, mode: null, permission: null });
});

test('goal states distinguish autonomous execution from pause, limits and completion', () => {
  for (const [status, label] of Object.entries({ active: '进行中', paused: '已暂停', blocked: '受阻', usageLimited: '用量受限', budgetLimited: '预算已用完', complete: '已完成' })) {
    const value = goalPresentation({ status, tokensUsed: 1234, timeUsedSeconds: 127, tokenBudget: null });
    assert.equal(value.label, label);
    assert.equal(value.active, status === 'active');
    assert.equal(value.tokens, '1,234');
    assert.equal(value.time, '2 分 7 秒');
    assert.equal(value.budget, null);
  }
});
test('goal counters preserve budgets and display actual elapsed usage', () => {
  assert.equal(goalPresentation({ status: 'paused', timeUsedSeconds: 3661.8, tokenBudget: 10000 }).time, '1 小时 1 分');
  assert.equal(goalPresentation({ status: 'active', timeUsedSeconds: 0, tokenBudget: 10000 }).budget, '10,000');
});

import { describe, expect, it } from 'vitest';
import type { Approval } from '../../api/approvals';
import type { Evidence } from '../../api/quality';
import type { ReviewThread } from '../../api/reviews';
import { canSubmitReview } from '../../lib/permissions';
import {
  anchorLabel,
  cycleIndex,
  defaultSubmitThreads,
  diffEvidence,
  diffRows,
  gateCategory,
  isSafeRelativePath,
  openThreadCount,
  placeThreads,
  rowTarget,
  threadOrder,
  validateThreadDraft,
  verdictOptions,
} from './reviewModel';

const HEAD = 'b'.repeat(40);
const OLD = 'a'.repeat(40);

const RAW = `diff --git a/src/export.ts b/src/export.ts
--- a/src/export.ts
+++ b/src/export.ts
@@ -1,3 +1,4 @@
 export function exportReport() {
-  return toJson();
+  return toCsv();
+  // --- not a header
 }
\\ No newline at end of file`;

function thread(id: string, init: { path?: string; side?: 'base' | 'head'; start?: number; end?: number; commit?: string; status?: 'OPEN' | 'RESOLVED'; created?: string } = {}): ReviewThread {
  return {
    id,
    project_id: 'p',
    repository_id: 'r',
    run_id: 'run',
    task_id: null,
    finding_id: null,
    anchor: {
      commit_sha: init.commit ?? HEAD,
      base_commit_sha: null,
      side: init.side ?? 'head',
      file_path: init.path ?? 'src/export.ts',
      start_line: init.start ?? 2,
      end_line: init.end ?? init.start ?? 2,
    },
    status: init.status ?? 'OPEN',
    outdated: false,
    current_commit_sha: HEAD,
    resolved_by: null,
    resolved_at: null,
    created_by: { id: 'u', display_name: 'Анна' },
    created_at: init.created ?? '2026-11-08T09:00:00Z',
    updated_at: '2026-11-08T09:00:00Z',
    version: 1,
    comment_count: 1,
    comments: [],
  };
}

describe('diffRows', () => {
  it('numbers both sides by hunk counters and keeps code that looks like a header', () => {
    const rows = diffRows(RAW);
    expect(rows.map((r) => [r.kind, r.oldLine, r.newLine])).toEqual([
      ['hunk', null, null],
      ['context', 1, 1],
      ['del', 2, null],
      ['add', null, 2],
      ['add', null, 3],
      ['context', 3, 4],
      ['note', null, null],
    ]);
    expect(rows[4]?.text).toBe('  // --- not a header');
  });

  it('maps rows to comment targets: removed → base, added/context → head', () => {
    const rows = diffRows(RAW);
    expect(rowTarget(rows[2]!)).toEqual({ side: 'base', line: 2 });
    expect(rowTarget(rows[3]!)).toEqual({ side: 'head', line: 2 });
    expect(rowTarget(rows[5]!)).toEqual({ side: 'head', line: 4 });
    expect(rowTarget(rows[0]!)).toBeNull();
  });
});

describe('placeThreads', () => {
  const rows = diffRows(RAW);
  it('places threads of the viewed commit under their end line; other commits and hidden lines are listed apart', () => {
    const t1 = thread('t1', { side: 'head', start: 2, end: 3 });
    const t2 = thread('t2', { side: 'base', start: 2 });
    const t3 = thread('t3', { start: 40 });
    const t4 = thread('t4', { commit: OLD });
    const t5 = thread('t5', { path: 'other.ts' });
    const placed = placeThreads([t1, t2, t3, t4, t5], 'src/export.ts', HEAD, rows);
    expect(placed.byRow.get('head:3')?.map((x) => x.id)).toEqual(['t1']);
    expect(placed.byRow.get('base:2')?.map((x) => x.id)).toEqual(['t2']);
    expect(placed.unplaced.map((x) => x.id)).toEqual(['t3']);
    expect(placed.otherCommit.map((x) => x.id)).toEqual(['t4']);
    expect(placed.all).toHaveLength(4);
  });

  it('without a diff commit nothing is placed inline', () => {
    const placed = placeThreads([thread('t1')], 'src/export.ts', null, rows);
    expect(placed.byRow.size).toBe(0);
    expect(placed.otherCommit).toHaveLength(1);
  });

  it('orders threads by file, line, side, creation; counts open per file', () => {
    const list = [thread('b', { start: 9 }), thread('a', { start: 2, created: '2026-11-08T10:00:00Z' }), thread('c', { path: 'a.ts', start: 50 }), thread('d', { start: 2, created: '2026-11-08T08:00:00Z', status: 'RESOLVED' })];
    expect(threadOrder(list).map((x) => x.id)).toEqual(['c', 'd', 'a', 'b']);
    expect(openThreadCount(list, 'src/export.ts')).toBe(2);
    expect(anchorLabel(thread('x', { start: 3, end: 5 }))).toBe('src/export.ts:3–5');
    expect(anchorLabel(thread('x', { start: 3 }))).toBe('src/export.ts:3');
  });

  it('cycles indexes for next/previous navigation', () => {
    expect(cycleIndex(3, -1, 1)).toBe(0);
    expect(cycleIndex(3, -1, -1)).toBe(2);
    expect(cycleIndex(3, 2, 1)).toBe(0);
    expect(cycleIndex(3, 0, -1)).toBe(2);
    expect(cycleIndex(0, 0, 1)).toBe(-1);
  });
});

describe('anchor validation', () => {
  it('rejects bad ranges, unknown base and empty bodies', () => {
    expect(validateThreadDraft({ side: 'head', startLine: 3, endLine: 5 }, 'ok', null)).toEqual([]);
    expect(validateThreadDraft({ side: 'head', startLine: 0, endLine: 5 }, 'ok', null)).toEqual(['lines']);
    expect(validateThreadDraft({ side: 'head', startLine: 6, endLine: 5 }, 'ok', null)).toEqual(['range']);
    expect(validateThreadDraft({ side: 'head', startLine: 1, endLine: 1001 }, 'ok', null)).toEqual(['tooLong']);
    expect(validateThreadDraft({ side: 'base', startLine: 1, endLine: 1 }, '  ', null)).toEqual(['baseUnknown', 'body']);
    expect(validateThreadDraft({ side: 'base', startLine: 1, endLine: 1 }, 'x'.repeat(8001), OLD)).toEqual(['bodyTooLong']);
  });

  it('accepts only safe relative paths', () => {
    expect(isSafeRelativePath('src/a.ts')).toBe(true);
    for (const bad of ['', '/etc/passwd', 'a/../b', './a', 'a//b', 'a\\b', 'a/\u0001', 'dir/']) expect(isSafeRelativePath(bad), bad).toBe(false);
  });
});

describe('diff evidence and gates', () => {
  const ev = (id: string, kind: string, task: string | null, created: string): Evidence =>
    ({
      id,
      run_id: 'run',
      task_id: task,
      kind,
      producer: 'verification-engine',
      verification_status: 'VERIFIED',
      artifact: { id: `a-${id}`, kind, sha256: 'c'.repeat(64), size_bytes: 10, mime_type: 'text/plain', download_url: '' },
      created_at: created,
    }) as Evidence;

  it('picks git_diff evidence of the scope, newest first', () => {
    const items = [ev('1', 'git_diff', 't1', '2026-11-08T09:00:00Z'), ev('2', 'test_report', 't1', '2026-11-08T09:01:00Z'), ev('3', 'git_diff', 't2', '2026-11-08T09:02:00Z')];
    expect(diffEvidence(items, null).map((e) => e.id)).toEqual(['3', '1']);
    expect(diffEvidence(items, 't1').map((e) => e.id)).toEqual(['1']);
  });

  it('classifies gates into tests and security', () => {
    expect(gateCategory('UNIT_TESTS')).toBe('tests');
    expect(gateCategory('E2E')).toBe('tests');
    expect(gateCategory('SAST')).toBe('security');
    expect(gateCategory('SECRETS')).toBe('security');
    expect(gateCategory('SCA')).toBe('security');
    expect(gateCategory('LINT')).toBe('other');
  });
});

describe('verdicts (§5.6, ADR-0015 §3)', () => {
  const approval = (canDecide: boolean, status: Approval['status'] = 'PENDING'): Approval =>
    ({
      id: 'ap',
      workspace_id: 'w',
      run_id: 'run',
      action_type: 'MANUAL_REVIEW',
      status,
      risk_level: 'medium',
      summary: 'Ревью',
      requested_by: { actor_type: 'agent' },
      can_decide: canDecide,
      created_at: '2026-11-08T09:00:00Z',
      version: 1,
    }) as Approval;
  const blocked = (role: string, approvals: Approval[]) => Object.fromEntries(verdictOptions(role, approvals).map((o) => [o.verdict, o.blocked]));

  it('APPROVE exists only with a pending approval the user can decide', () => {
    expect(blocked('developer', [])).toEqual({ COMMENT: null, APPROVE: 'noApproval', REQUEST_CHANGES: null });
    expect(blocked('developer', [approval(false)])).toEqual({ COMMENT: null, APPROVE: 'cannotDecide', REQUEST_CHANGES: null });
    expect(blocked('developer', [approval(true)])).toEqual({ COMMENT: null, APPROVE: null, REQUEST_CHANGES: null });
    expect(blocked('developer', [approval(true, 'APPROVED')]).APPROVE).toBe('noApproval');
  });

  it('reviewer cannot request changes; viewer cannot submit at all', () => {
    expect(blocked('reviewer', [approval(true)])).toEqual({ COMMENT: null, APPROVE: null, REQUEST_CHANGES: 'role' });
    expect(blocked('viewer', [approval(true)])).toEqual({ COMMENT: 'role', APPROVE: 'role', REQUEST_CHANGES: 'role' });
    expect(canSubmitReview('owner', 'REQUEST_CHANGES')).toBe(true);
    expect(canSubmitReview('reviewer', 'COMMENT')).toBe(true);
    expect(canSubmitReview(null, 'COMMENT')).toBe(false);
  });

  it('defaults to open threads within the contract limit', () => {
    const list = Array.from({ length: 60 }, (_, i) => thread(`t${i}`, { status: i % 2 ? 'RESOLVED' : 'OPEN' }));
    expect(defaultSubmitThreads(list)).toHaveLength(30);
    expect(defaultSubmitThreads(list, 5)).toEqual(['t0', 't2', 't4', 't6', 't8']);
  });
});

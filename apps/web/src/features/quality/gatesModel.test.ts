import { describe, expect, it } from 'vitest';
import type { AnalyticsSummary } from '../../api/analytics';
import type { EvidenceVerificationStatus, GateExecution, GateExecutionStatus } from '../../api/quality';
import { categoryAggregates, categorySummaries, gateCategory, gateRows, gateTypeVerifiedRate, qualityWindow, worstState } from './gatesModel';

const gate = (id: string, gate_type: string, status: GateExecutionStatus, evidence_id: string | null = null): GateExecution => ({
  id,
  gate_id: `g-${id}`,
  gate_type,
  required: true,
  status,
  evidence_id,
});

type GateStats = AnalyticsSummary['gates'][number];
const stats = (gate_type: string, s: Partial<GateStats>): GateStats => ({
  gate_type,
  total: 0,
  passed: 0,
  passed_verified: 0,
  passed_unverified: 0,
  failed: 0,
  errored: 0,
  skipped: 0,
  pass_rate: null,
  avg_duration_seconds: null,
  ...s,
});

describe('gate categories (F7)', () => {
  it('maps only exact contract gate types; look-alike names go to "other"', () => {
    expect(gateCategory('LINT')).toBe('lint');
    expect(gateCategory('FORMAT')).toBe('lint');
    expect(gateCategory('TYPECHECK')).toBe('typecheck');
    expect(gateCategory('BUILD')).toBe('build');
    expect(gateCategory('UNIT')).toBe('unit');
    expect(gateCategory('INTEGRATION')).toBe('integration');
    expect(gateCategory('E2E')).toBe('e2e');
    expect(gateCategory('REVIEW')).toBe('review');
    expect(gateCategory('COVERAGE')).toBe('coverage');
    for (const t of ['SAST', 'SCA', 'SECRETS', 'SBOM', 'IAC', 'SIGN']) expect(gateCategory(t)).toBe('security');
    expect(gateCategory('UNIT_TESTS')).toBe('other');
    expect(gateCategory('lint')).toBe('other');
  });

  it('worst state wins; empty → null', () => {
    expect(worstState([])).toBeNull();
    expect(worstState(['PASS', 'FAIL', 'BLOCKED'])).toBe('FAIL');
    expect(worstState(['PASS', 'INSUFFICIENT_EVIDENCE'])).toBe('INSUFFICIENT_EVIDENCE');
    expect(worstState(['PASS', 'NOT_RUN'])).toBe('NOT_RUN');
    expect(worstState(['PASS', 'NOT_APPLICABLE'])).toBe('PASS');
    expect(worstState(['NOT_APPLICABLE'])).toBe('NOT_APPLICABLE');
  });

  it('table of every gate status × evidence status: PASS only with VERIFIED evidence; SKIPPED never green', () => {
    const statuses: GateExecutionStatus[] = ['PENDING', 'RUNNING', 'PASS', 'FAIL', 'ERROR', 'SKIPPED', 'NOT_APPLICABLE'];
    const evidence: (EvidenceVerificationStatus | undefined)[] = [undefined, 'PENDING', 'VERIFIED', 'REJECTED', 'UNVERIFIED'];
    for (const status of statuses) {
      for (const ev of evidence) {
        for (const withId of [false, true]) {
          const [row] = gateRows([gate('1', 'UNIT', status, withId ? 'e1' : null)], () => ev);
          const expected =
            status === 'PASS'
              ? withId && ev === 'VERIFIED'
                ? 'PASS'
                : 'INSUFFICIENT_EVIDENCE'
              : status === 'FAIL'
                ? 'FAIL'
                : status === 'ERROR'
                  ? 'BLOCKED'
                  : status === 'RUNNING'
                    ? 'RUNNING'
                    : status === 'NOT_APPLICABLE'
                      ? 'NOT_APPLICABLE'
                      : 'NOT_RUN';
          expect(row?.verification.state, `${status} × ${ev ?? 'unknown'} × evidence_id=${withId}`).toBe(expected);
          expect(row?.verification.source).toBe(status);
        }
      }
    }
  });

  it('category summaries per run: worst gate state, groups without gates have no state, "other" only when present', () => {
    const rows = gateRows(
      [gate('1', 'LINT', 'PASS', 'e1'), gate('2', 'FORMAT', 'SKIPPED'), gate('3', 'UNIT', 'PASS', 'e2'), gate('4', 'SECRETS', 'FAIL')],
      (id) => (id === 'e1' ? 'VERIFIED' : id === 'e2' ? 'UNVERIFIED' : undefined),
    );
    const byCat = new Map(categorySummaries(rows).map((c) => [c.category, c]));
    expect(byCat.get('lint')?.state).toBe('NOT_RUN');
    expect(byCat.get('unit')?.state).toBe('INSUFFICIENT_EVIDENCE');
    expect(byCat.get('security')?.state).toBe('FAIL');
    expect(byCat.get('e2e')?.state).toBeNull();
    expect(byCat.has('other')).toBe(false);
    expect(categorySummaries(gateRows([gate('9', 'DOCS_CHECK', 'PASS')], () => undefined)).at(-1)?.category).toBe('other');
  });

  it('analytics aggregates by category reuse D-8 outcomes; verified rate excludes SKIPPED; null without executions', () => {
    const agg = categoryAggregates([
      stats('LINT', { total: 4, passed: 3, passed_verified: 2, passed_unverified: 1, skipped: 1 }),
      stats('FORMAT', { total: 2, passed: 1, passed_verified: 1, failed: 1 }),
      stats('E2E', { total: 1, skipped: 1 }),
    ]);
    const lint = agg.find((a) => a.category === 'lint');
    expect(lint?.gateTypes).toEqual(['FORMAT', 'LINT']);
    expect(lint?.total).toBe(6);
    expect(lint?.outcomes.map((o) => [o.state, o.count])).toEqual([
      ['PASS', 3],
      ['INSUFFICIENT_EVIDENCE', 1],
      ['FAIL', 1],
      ['BLOCKED', 0],
      ['NOT_RUN', 1],
    ]);
    expect(lint?.verifiedRate).toBeCloseTo(3 / 5);
    expect(agg.find((a) => a.category === 'e2e')?.verifiedRate).toBeNull();
    expect(agg.find((a) => a.category === 'build')?.total).toBe(0);
    expect(gateTypeVerifiedRate(stats('X', { passed: 2, passed_verified: 0, passed_unverified: 2 }))).toBe(0);
  });

  it('analytics window ends at the next full UTC hour', () => {
    const w = qualityWindow(30, new Date('2026-11-09T10:20:00Z'));
    expect(w.to).toBe('2026-11-09T11:00:00.000Z');
    expect(w.from).toBe('2026-10-10T11:00:00.000Z');
  });
});

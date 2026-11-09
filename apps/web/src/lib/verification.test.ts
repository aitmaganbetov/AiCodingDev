import { describe, expect, it } from 'vitest';
import type { ComplianceStatus } from '../api/compliance';
import type { EvidenceVerificationStatus, GateExecutionStatus } from '../api/quality';
import { complianceVerificationState, gateAggregateOutcomes, gateVerificationState, type VerificationDisplayState } from './verification';

const GATE: readonly GateExecutionStatus[] = ['PENDING', 'RUNNING', 'PASS', 'FAIL', 'ERROR', 'SKIPPED', 'NOT_APPLICABLE'];
const EVIDENCE: readonly (EvidenceVerificationStatus | null | undefined)[] = ['PENDING', 'VERIFIED', 'REJECTED', 'UNVERIFIED', null, undefined];

describe('gateVerificationState (D-8)', () => {
  it('maps every gate status × evidence combination; green PASS only with VERIFIED evidence', () => {
    for (const status of GATE) {
      for (const hasEvidence of [true, false]) {
        for (const ev of EVIDENCE) {
          const { state, source } = gateVerificationState({ status, evidence_id: hasEvidence ? 'e1' : null }, ev);
          expect(source).toBe(status);
          const expected: Record<GateExecutionStatus, VerificationDisplayState> = {
            PASS: hasEvidence && ev === 'VERIFIED' ? 'PASS' : 'INSUFFICIENT_EVIDENCE',
            FAIL: 'FAIL',
            ERROR: 'BLOCKED',
            PENDING: 'NOT_RUN',
            SKIPPED: 'NOT_RUN',
            RUNNING: 'RUNNING',
            NOT_APPLICABLE: 'NOT_APPLICABLE',
          };
          expect(state, `${status} evidence=${String(hasEvidence)} ${String(ev)}`).toBe(expected[status]);
          if (state === 'PASS') expect([hasEvidence, ev]).toEqual([true, 'VERIFIED']);
        }
      }
    }
  });

  it('SKIPPED is never PASS; ERROR is BLOCKED; PASS without evidence or with unverified evidence is INSUFFICIENT_EVIDENCE', () => {
    expect(gateVerificationState({ status: 'SKIPPED', evidence_id: 'e' }, 'VERIFIED').state).toBe('NOT_RUN');
    expect(gateVerificationState({ status: 'ERROR', evidence_id: null }).state).toBe('BLOCKED');
    expect(gateVerificationState({ status: 'PASS', evidence_id: null }, 'VERIFIED').state).toBe('INSUFFICIENT_EVIDENCE');
    expect(gateVerificationState({ status: 'PASS', evidence_id: 'e' }).state).toBe('INSUFFICIENT_EVIDENCE');
    expect(gateVerificationState({ status: 'PASS', evidence_id: 'e' }, 'REJECTED').state).toBe('INSUFFICIENT_EVIDENCE');
  });

  it('missing execution of a required gate is NOT_RUN, or BLOCKED when the task is blocked upstream', () => {
    expect(gateVerificationState(null)).toEqual({ state: 'NOT_RUN', source: null });
    expect(gateVerificationState(undefined, undefined, { blockedUpstream: true })).toEqual({ state: 'BLOCKED', source: null });
    expect(gateVerificationState({ status: 'PENDING', evidence_id: null }, null, { blockedUpstream: true }).state).toBe('BLOCKED');
  });

  it('an unknown future contract status is never shown as success', () => {
    const state = gateVerificationState({ status: 'SOMETHING_NEW' as GateExecutionStatus, evidence_id: 'e' }, 'VERIFIED').state;
    expect(state).toBe('NOT_RUN');
  });
});

describe('complianceVerificationState', () => {
  it.each<[ComplianceStatus, boolean, VerificationDisplayState]>([
    ['PASS', true, 'PASS'],
    ['PASS', false, 'INSUFFICIENT_EVIDENCE'],
    ['FAIL', false, 'FAIL'],
    ['FAIL', true, 'FAIL'],
    ['UNVERIFIED', true, 'INSUFFICIENT_EVIDENCE'],
    ['MANUAL_REVIEW', false, 'MANUAL_REVIEW'],
    ['NOT_APPLICABLE', false, 'NOT_APPLICABLE'],
  ])('%s (verified evidence: %s) → %s', (status, verified, expected) => {
    expect(complianceVerificationState(status, verified)).toEqual({ state: expected, source: status });
  });
});

describe('gateAggregateOutcomes (D-8)', () => {
  it('«Пройдено» — только PASS с VERIFIED evidence; PASS без него — «Недостаточно доказательств»', () => {
    const o = gateAggregateOutcomes({ passed_verified: 4, passed_unverified: 2, failed: 1, errored: 3, skipped: 5 });
    expect(o.map((x) => [x.state, x.count])).toEqual([
      ['PASS', 4],
      ['INSUFFICIENT_EVIDENCE', 2],
      ['FAIL', 1],
      ['BLOCKED', 3],
      ['NOT_RUN', 5],
    ]);
  });

  it('PASS без проверенного evidence никогда не попадает в «Пройдено»', () => {
    const o = gateAggregateOutcomes({ passed_verified: 0, passed_unverified: 7, failed: 0, errored: 0, skipped: 0 });
    expect(o.find((x) => x.state === 'PASS')?.count).toBe(0);
    expect(o.find((x) => x.state === 'INSUFFICIENT_EVIDENCE')?.count).toBe(7);
  });
});

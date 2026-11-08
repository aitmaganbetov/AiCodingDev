import { describe, expect, it } from 'vitest';
import { decisionReasonRequired, isApprovalExpired } from '../../api/approvals';
import { canReviewCompliance, isComplianceReviewable, type ComplianceRow } from '../../api/compliance';
import { acceptedRiskBlockedByProfile, allowedFindingTransitions, canTransitionFinding } from '../../api/findings';
import { gateCountsAsPass, type Evidence } from '../../api/quality';
import { rationaleRequired } from '../../api/standards';
import { missingEvidenceFields } from './EvidencePage';

describe('quality gates', () => {
  it('only PASS counts as passed: SKIPPED / ERROR / NOT_APPLICABLE never do', () => {
    expect(gateCountsAsPass('PASS')).toBe(true);
    for (const s of ['SKIPPED', 'ERROR', 'FAIL', 'NOT_APPLICABLE', 'PENDING', 'RUNNING']) expect(gateCountsAsPass(s)).toBe(false);
  });
});

describe('finding FSM (QUALITY_GATES §5.3)', () => {
  it('allows only contract transitions; FIXED / VERIFIED_FIXED are engine-only', () => {
    expect(allowedFindingTransitions('OPEN')).toEqual(['ACKNOWLEDGED', 'FALSE_POSITIVE', 'ACCEPTED_RISK']);
    expect(allowedFindingTransitions('ACKNOWLEDGED')).toEqual(['OPEN', 'FALSE_POSITIVE', 'ACCEPTED_RISK']);
    expect(allowedFindingTransitions('FALSE_POSITIVE')).toEqual(['OPEN']);
    expect(allowedFindingTransitions('ACCEPTED_RISK')).toEqual(['OPEN']);
    expect(allowedFindingTransitions('FIXED')).toEqual([]);
    expect(allowedFindingTransitions('VERIFIED_FIXED')).toEqual([]);
    expect(allowedFindingTransitions('SOMETHING_NEW')).toEqual([]);
  });

  it('roles: ACCEPTED_RISK admin+, others reviewer+; viewer and unknown roles none', () => {
    expect(canTransitionFinding('reviewer', 'ACKNOWLEDGED')).toBe(true);
    expect(canTransitionFinding('reviewer', 'ACCEPTED_RISK')).toBe(false);
    expect(canTransitionFinding('developer', 'ACCEPTED_RISK')).toBe(false);
    expect(canTransitionFinding('admin', 'ACCEPTED_RISK')).toBe(true);
    expect(canTransitionFinding('owner', 'OPEN')).toBe(true);
    expect(canTransitionFinding('viewer', 'FALSE_POSITIVE')).toBe(false);
    expect(canTransitionFinding('auditor', 'OPEN')).toBe(false);
    expect(canTransitionFinding(null, 'OPEN')).toBe(false);
  });

  it('critical profile forbids accepting a critical finding', () => {
    expect(acceptedRiskBlockedByProfile('critical', 'critical')).toBe(true);
    expect(acceptedRiskBlockedByProfile('high', 'critical')).toBe(false);
    expect(acceptedRiskBlockedByProfile('critical', 'strict')).toBe(false);
  });
});

function row(overrides: Partial<ComplianceRow>): ComplianceRow {
  return {
    control_id: 'c1',
    standard_code: 'OWASP-ASVS',
    edition: '4.0.3',
    clause_ref: 'V2.1.1',
    control_code: 'ASVS-2.1.1',
    applicability: 'APPLICABLE',
    status: 'MANUAL_REVIEW',
    verification_method: 'manual',
    ...overrides,
  };
}

describe('compliance review', () => {
  it('reviewable: manual-like MANUAL_REVIEW, any UNVERIFIED; not gate verdicts, N/A or unapproved applicability', () => {
    expect(isComplianceReviewable(row({}))).toBe(true);
    expect(isComplianceReviewable(row({ verification_method: 'document_check' }))).toBe(true);
    expect(isComplianceReviewable(row({ status: 'UNVERIFIED', verification_method: 'automated_gate' }))).toBe(true);
    expect(isComplianceReviewable(row({ status: 'PASS', verification_method: 'automated_gate' }))).toBe(false);
    expect(isComplianceReviewable(row({ status: 'FAIL' }))).toBe(false);
    expect(isComplianceReviewable(row({ status: 'NOT_APPLICABLE', applicability: 'NOT_APPLICABLE' }))).toBe(false);
    expect(isComplianceReviewable(row({ applicability: 'PENDING_REVIEW' }))).toBe(false);
    expect(isComplianceReviewable(row({ verification_method: 'automated_gate' }))).toBe(false);
  });

  it('compliance:review is reviewer+', () => {
    expect(canReviewCompliance('reviewer')).toBe(true);
    expect(canReviewCompliance('viewer')).toBe(false);
    expect(canReviewCompliance(undefined)).toBe(false);
  });

  it('rationale is required unless applicable', () => {
    expect(rationaleRequired('applicable')).toBe(false);
    expect(rationaleRequired('tailored')).toBe(true);
    expect(rationaleRequired('not_applicable')).toBe(true);
  });
});

describe('approvals', () => {
  it('reason is required for REJECTED and for critical risk', () => {
    expect(decisionReasonRequired('APPROVED', 'high')).toBe(false);
    expect(decisionReasonRequired('APPROVED', 'critical')).toBe(true);
    expect(decisionReasonRequired('REJECTED', 'low')).toBe(true);
  });

  it('expiry by client clock', () => {
    const now = Date.parse('2026-11-08T10:00:00Z');
    expect(isApprovalExpired({ expires_at: null }, now)).toBe(false);
    expect(isApprovalExpired({ expires_at: '2026-11-08T09:59:59Z' }, now)).toBe(true);
    expect(isApprovalExpired({ expires_at: '2026-11-08T10:00:01Z' }, now)).toBe(false);
  });
});

describe('evidence completeness (AC-14)', () => {
  const full: Evidence = {
    id: 'e1',
    run_id: 'r1',
    kind: 'test_report',
    producer: 'verification-engine',
    verification_status: 'VERIFIED',
    created_at: '2026-11-08T09:00:00Z',
    commit_sha: 'a'.repeat(40),
    command: 'pnpm test',
    exit_code: 0,
    started_at: '2026-11-08T08:59:00Z',
    finished_at: '2026-11-08T09:00:00Z',
    environment: { node: '22' } as unknown as Evidence['environment'],
    artifact: { id: 'a1', kind: 'test_report', sha256: 'b'.repeat(64), size_bytes: 10, mime_type: 'application/json', download_url: '/x' },
  };

  it('complete evidence has no missing fields; exit code 0 is a value, not a gap', () => {
    expect(missingEvidenceFields(full)).toEqual([]);
  });

  it('lists every missing AC-14 field', () => {
    expect(missingEvidenceFields({ ...full, commit_sha: null, command: null, exit_code: null, started_at: null, finished_at: null, environment: {} })).toEqual([
      'commit',
      'command',
      'exitCode',
      'startedAt',
      'finishedAt',
      'environment',
    ]);
  });
});

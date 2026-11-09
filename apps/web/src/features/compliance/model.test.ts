import { describe, expect, it } from 'vitest';
import type { ComplianceRow } from '../../api/compliance';
import type { EvidenceVerificationStatus } from '../../api/quality';
import type { ProjectControl } from '../../api/standards';
import { evidenceGap, filterControls, filterRows, rowVerification, standardsRegistry } from './model';

const control = (code: string, init: Partial<ProjectControl> = {}): ProjectControl => ({
  control_id: `id-${code}`,
  standard_code: 'OWASP-ASVS',
  edition: '4.0.3',
  standard_title: 'OWASP ASVS',
  clause_ref: code,
  control_code: code,
  description: `Описание ${code}`,
  verification_method: 'automated_gate',
  approval_state: 'approved',
  applicability: 'APPLICABLE',
  blocking: null,
  effective_blocking: false,
  ...init,
});

const row = (code: string, init: Partial<ComplianceRow> = {}): ComplianceRow => ({
  control_id: `id-${code}`,
  standard_code: 'OWASP-ASVS',
  edition: '4.0.3',
  clause_ref: code,
  control_code: code,
  applicability: 'APPLICABLE',
  status: 'PASS',
  evidence_id: null,
  ...init,
});

describe('standards registry from project controls', () => {
  it('groups by standard + edition with approval/applicability/blocking counts', () => {
    const reg = standardsRegistry([
      control('V5.3.4', { effective_blocking: true }),
      control('V2.1.1', { approval_state: 'draft', applicability: 'PENDING_REVIEW', verification_method: 'manual' }),
      control('PW.7.2', { standard_code: 'NIST-SSDF', edition: '1.1', standard_title: undefined, applicability: 'NOT_APPLICABLE' }),
    ]);
    expect(reg.map((e) => e.code)).toEqual(['NIST-SSDF', 'OWASP-ASVS']);
    const asvs = reg[1]!;
    expect(asvs).toMatchObject({ controls: 2, approved: 1, pendingApproval: 1, applicable: 1, blocking: 1, title: 'OWASP ASVS' });
    expect(asvs.methods).toEqual({ automated_gate: 1, manual: 1 });
    expect(reg[0]).toMatchObject({ notApplicable: 1, title: null });
  });

  it('filters controls by standard, applicability, blocking and text', () => {
    const items = [control('V5.3.4', { effective_blocking: true }), control('V2.1.1', { applicability: 'NOT_APPLICABLE' }), control('PW.7.2', { standard_code: 'NIST-SSDF' })];
    expect(filterControls(items, { standard: 'NIST-SSDF', applicability: '', blocking: '', q: '' }).map((c) => c.control_code)).toEqual(['PW.7.2']);
    expect(filterControls(items, { standard: '', applicability: 'NOT_APPLICABLE', blocking: '', q: '' }).map((c) => c.control_code)).toEqual(['V2.1.1']);
    expect(filterControls(items, { standard: '', applicability: '', blocking: 'yes', q: '' }).map((c) => c.control_code)).toEqual(['V5.3.4']);
    expect(filterControls(items, { standard: '', applicability: '', blocking: 'no', q: 'описание v2' }).map((c) => c.control_code)).toEqual(['V2.1.1']);
  });
});

describe('compliance matrix evidence mapping', () => {
  const statuses: Record<string, EvidenceVerificationStatus> = { ok: 'VERIFIED', bad: 'UNVERIFIED' };
  const statusOf = (id: string) => statuses[id];

  it('missing evidence is flagged only for applicable controls', () => {
    expect(evidenceGap(row('a'), undefined)).toBe('missing');
    expect(evidenceGap(row('b', { applicability: 'NOT_APPLICABLE', status: 'NOT_APPLICABLE' }), undefined)).toBe('not_required');
    expect(evidenceGap(row('c', { applicability: 'PENDING_REVIEW', status: 'UNVERIFIED' }), undefined)).toBe('not_required');
    expect(evidenceGap(row('d', { evidence_id: 'ok' }), 'VERIFIED')).toBe('verified');
    expect(evidenceGap(row('e', { evidence_id: 'bad' }), 'UNVERIFIED')).toBe('unverified');
    expect(evidenceGap(row('f', { evidence_id: 'zzz' }), undefined)).toBe('unknown');
  });

  it('PASS is green only with VERIFIED evidence; UNVERIFIED → insufficient evidence', () => {
    expect(rowVerification(row('a', { evidence_id: 'ok' }), 'VERIFIED').state).toBe('PASS');
    expect(rowVerification(row('a', { evidence_id: 'bad' }), 'UNVERIFIED').state).toBe('INSUFFICIENT_EVIDENCE');
    expect(rowVerification(row('a'), undefined).state).toBe('INSUFFICIENT_EVIDENCE');
    expect(rowVerification(row('a', { status: 'UNVERIFIED' }), undefined).state).toBe('INSUFFICIENT_EVIDENCE');
    expect(rowVerification(row('a', { status: 'FAIL', evidence_id: 'ok' }), 'VERIFIED').state).toBe('FAIL');
    expect(rowVerification(row('a', { status: 'NOT_APPLICABLE' }), undefined).state).toBe('NOT_APPLICABLE');
  });

  it('row filters: control text and "only without proof"', () => {
    const rows = [row('V5.3.4', { evidence_id: 'ok' }), row('V2.1.1'), row('PW.7.2', { evidence_id: 'bad' }), row('X', { applicability: 'NOT_APPLICABLE', status: 'NOT_APPLICABLE' })];
    expect(filterRows(rows, { q: 'v5', missingOnly: false }, statusOf).map((r) => r.control_code)).toEqual(['V5.3.4']);
    expect(filterRows(rows, { q: '', missingOnly: true }, statusOf).map((r) => r.control_code)).toEqual(['V2.1.1', 'PW.7.2']);
  });
});

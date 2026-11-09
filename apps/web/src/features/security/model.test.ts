import { describe, expect, it } from 'vitest';
import type { Finding } from '../../api/findings';
import {
  applyClientFilters,
  controlMappings,
  cweMappings,
  distinctRunIds,
  filtersToQuery,
  parseFilters,
  securityKind,
  severityDistribution,
  statusDistribution,
  withParam,
} from './model';

const f = (id: string, init: Partial<Finding> = {}): Finding => ({ id, severity: 'high', rule_id: `rule-${id}`, status: 'OPEN', run_id: 'r1', ...init });

describe('security center filters (URL ↔ GET /findings)', () => {
  it('parses only contract values and maps server filters to the query', () => {
    const params = new URLSearchParams('severity=high&severity=bogus&status=OPEN&status=ACKNOWLEDGED&source=gate&project=p1&run=r9&cwe=CWE-89');
    const filters = parseFilters(params);
    expect(filters).toEqual({ severity: ['high'], status: ['OPEN', 'ACKNOWLEDGED'], source: ['gate'], project: 'p1', run: 'r9', cwe: 'CWE-89', control: '' });
    expect(filtersToQuery(filters)).toEqual({ severity: ['high'], status: ['OPEN', 'ACKNOWLEDGED'], source: ['gate'], project_id: 'p1', run_id: 'r9' });
    expect(filtersToQuery(parseFilters(new URLSearchParams()))).toEqual({});
  });

  it('withParam replaces repeated values and drops empty ones', () => {
    const p = withParam(new URLSearchParams('severity=low&x=1'), 'severity', ['high', 'critical']);
    expect(p.getAll('severity')).toEqual(['high', 'critical']);
    expect(withParam(p, 'severity', []).has('severity')).toBe(false);
    expect(withParam(p, 'run', '').has('run')).toBe(false);
    expect(p.get('x')).toBe('1');
  });

  it('client filters by exact CWE and control ref', () => {
    const items = [f('1', { cwe_id: 'CWE-89', control_refs: ['ASVS-5.3.4'] }), f('2', { cwe_id: 'CWE-79' }), f('3')];
    expect(applyClientFilters(items, { cwe: 'CWE-89', control: '' }).map((x) => x.id)).toEqual(['1']);
    expect(applyClientFilters(items, { cwe: '', control: 'ASVS-5.3.4' }).map((x) => x.id)).toEqual(['1']);
    expect(applyClientFilters(items, { cwe: '', control: '' })).toHaveLength(3);
  });
});

describe('distributions and mappings come only from finding fields', () => {
  const items = [
    f('1', { severity: 'critical', cwe_id: 'CWE-89', control_refs: ['ASVS-5.3.4', 'ASVS-5.3.4'] }),
    f('2', { severity: 'high', status: 'FIXED', cwe_id: 'CWE-89' }),
    f('3', { severity: 'low', status: 'ACKNOWLEDGED', control_refs: ['SSDF-PW.7.2'] }),
    f('4', { severity: 'low', status: 'FALSE_POSITIVE' }),
  ];

  it('severity and status counts', () => {
    expect(severityDistribution(items)).toEqual({ critical: 1, high: 1, medium: 0, low: 2, info: 0 });
    expect(statusDistribution(items)).toMatchObject({ OPEN: 1, ACKNOWLEDGED: 1, FIXED: 1, FALSE_POSITIVE: 1, VERIFIED_FIXED: 0, ACCEPTED_RISK: 0 });
  });

  it('CWE and control mappings: present values only, deduplicated per finding, unresolved counted', () => {
    expect(cweMappings(items)).toEqual([{ key: 'CWE-89', count: 2, unresolved: 1 }]);
    expect(controlMappings(items)).toEqual([
      { key: 'ASVS-5.3.4', count: 1, unresolved: 1 },
      { key: 'SSDF-PW.7.2', count: 1, unresolved: 1 },
    ]);
    expect(cweMappings([f('x')])).toEqual([]);
  });
});

describe('security kind by gate type of gate_execution_id', () => {
  const gateTypes = new Map([
    ['ge-sca', 'SCA'],
    ['ge-sbom', 'SBOM'],
    ['ge-sec', 'SECRETS'],
    ['ge-sast', 'SAST'],
    ['ge-lint', 'LINT'],
  ]);
  const loaded = new Set(['r1']);
  it('classifies by gate type, never by rule name', () => {
    expect(securityKind(f('1', { gate_execution_id: 'ge-sca', rule_id: 'gitleaks.aws' }), gateTypes, loaded)).toBe('dependency');
    expect(securityKind(f('2', { gate_execution_id: 'ge-sbom' }), gateTypes, loaded)).toBe('dependency');
    expect(securityKind(f('3', { gate_execution_id: 'ge-sec' }), gateTypes, loaded)).toBe('secrets');
    expect(securityKind(f('4', { gate_execution_id: 'ge-sast' }), gateTypes, loaded)).toBe('code');
    expect(securityKind(f('5', { gate_execution_id: 'ge-lint' }), gateTypes, loaded)).toBe('other_gate');
    // Отчёт run загружен, gate не найден — не безопасность; не загружен — неизвестно.
    expect(securityKind(f('6', { gate_execution_id: 'ge-missing' }), gateTypes, loaded)).toBe('other_gate');
    expect(securityKind(f('7', { gate_execution_id: 'ge-x', run_id: 'r2', rule_id: 'osv.CVE-1' }), gateTypes, loaded)).toBe('unknown');
    expect(securityKind(f('8', { source: 'agent_review' }), gateTypes, loaded)).toBe('not_gate');
    expect(securityKind(f('9'), gateTypes, loaded)).toBe('unknown');
  });

  it('distinct run ids keep first-seen order and the limit', () => {
    expect(distinctRunIds([f('1', { run_id: 'a' }), f('2', { run_id: 'b' }), f('3', { run_id: 'a' }), f('4', { run_id: 'c' })], 2)).toEqual(['a', 'b']);
  });
});

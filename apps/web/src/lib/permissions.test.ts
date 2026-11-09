import { describe, expect, it } from 'vitest';
import { can, canTriageFinding } from './permissions';

describe('permissions catalogue (API_DESIGN §5.1)', () => {
  it('finding:triage — все роли кроме viewer', () => {
    for (const role of ['owner', 'admin', 'developer', 'reviewer']) expect(can(role, 'finding:triage')).toBe(true);
    expect(can('viewer', 'finding:triage')).toBe(false);
    expect(can('auditor', 'finding:triage')).toBe(false);
    expect(can(null, 'finding:triage')).toBe(false);
  });

  it('finding:triage зависит от to_status (§5.5): ACCEPTED_RISK — только owner/admin', () => {
    expect(canTriageFinding('owner', 'ACCEPTED_RISK')).toBe(true);
    expect(canTriageFinding('admin', 'ACCEPTED_RISK')).toBe(true);
    expect(canTriageFinding('developer', 'ACCEPTED_RISK')).toBe(false);
    expect(canTriageFinding('reviewer', 'ACCEPTED_RISK')).toBe(false);
    expect(canTriageFinding('reviewer', 'FALSE_POSITIVE')).toBe(true);
    expect(canTriageFinding('developer', 'OPEN')).toBe(true);
    expect(canTriageFinding('viewer', 'ACKNOWLEDGED')).toBe(false);
    expect(canTriageFinding(undefined, 'OPEN')).toBe(false);
  });

  it('compliance:review — reviewer и выше', () => {
    for (const role of ['owner', 'admin', 'developer', 'reviewer']) expect(can(role, 'compliance:review')).toBe(true);
    expect(can('viewer', 'compliance:review')).toBe(false);
    expect(can(undefined, 'compliance:review')).toBe(false);
  });

  it('notification:read / notification:update (B8, §13.2) — любая роль workspace (только свои уведомления)', () => {
    for (const role of ['owner', 'admin', 'developer', 'reviewer', 'viewer']) {
      expect(can(role, 'notification:read')).toBe(true);
      expect(can(role, 'notification:update')).toBe(true);
    }
    expect(can('auditor', 'notification:read')).toBe(false);
    expect(can(null, 'notification:update')).toBe(false);
  });
});

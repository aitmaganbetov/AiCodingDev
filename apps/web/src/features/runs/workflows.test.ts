import { describe, expect, it } from 'vitest';
import { modeForFilter, statusesForFilter } from './WorkspaceRunsPage';
import { durationParts, workflowDurationSeconds, workflowStage } from './workflowModel';

describe('workflows model (F5)', () => {
  it('stage follows the run FSM; special statuses keep the reached stage', () => {
    expect(workflowStage({ status: 'RUNNING', started_at: 'x', finished_at: null })).toEqual({ index: 3, stage: 'RUNNING', special: false });
    expect(workflowStage({ status: 'COMPLETED', started_at: 'x', finished_at: 'y' })).toEqual({ index: 5, stage: 'COMPLETED', special: false });
    expect(workflowStage({ status: 'AWAITING_APPROVAL', started_at: null, finished_at: null })).toEqual({ index: 2, stage: 'PLANNED', special: true });
    expect(workflowStage({ status: 'PAUSED', started_at: '2026-11-08T08:00:00Z', finished_at: null })).toMatchObject({ stage: 'RUNNING', special: true });
    expect(workflowStage({ status: 'FAILED', started_at: '2026-11-08T08:00:00Z', finished_at: '2026-11-08T09:00:00Z' })).toMatchObject({
      stage: 'COMPLETED',
      special: true,
    });
    expect(workflowStage({ status: 'CANCELLED', started_at: null, finished_at: null })).toMatchObject({ index: 0, special: true });
  });

  it('duration: null when not started, live until finished', () => {
    const now = Date.parse('2026-11-08T09:00:00Z');
    expect(workflowDurationSeconds({ started_at: null, finished_at: null }, now)).toBeNull();
    expect(workflowDurationSeconds({ started_at: '2026-11-08T08:00:00Z', finished_at: null }, now)).toBe(3600);
    expect(workflowDurationSeconds({ started_at: '2026-11-08T08:00:00Z', finished_at: '2026-11-08T08:02:05Z' }, now)).toBe(125);
    expect(durationParts(125)).toEqual({ h: 0, m: 2, s: 5 });
    expect(durationParts(3725)).toEqual({ h: 1, m: 2, s: null });
  });

  it('filters: status presets and mode from URL', () => {
    expect(statusesForFilter('finished')).toEqual(['COMPLETED', 'FAILED', 'CANCELLED']);
    expect(statusesForFilter('nope')).toBeUndefined();
    expect(modeForFilter('AUDIT')).toBe('AUDIT');
    expect(modeForFilter('hack')).toBeUndefined();
  });
});

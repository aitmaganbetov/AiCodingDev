import { describe, expect, it } from 'vitest';
import type { Directive } from '../../api/directives';
import { directiveSteps } from './directiveModel';

type D = Pick<Directive, 'status' | 'impact_status' | 'requires_confirmation' | 'delivery' | 'completion'>;
const base: D = {
  status: 'IMPACT_ANALYSIS_PENDING',
  impact_status: 'PENDING',
  requires_confirmation: false,
  delivery: { state: 'NOT_DISPATCHED', acknowledgements: [] },
  completion: { state: 'NOT_STARTED', resulting_task_ids: [] },
};
const states = (d: D) => Object.fromEntries(directiveSteps(d).map((s) => [s.id, s.state]));

describe('directiveSteps', () => {
  it('analysis in progress', () => {
    expect(states(base)).toEqual({ accepted: 'done', impact: 'current', confirmation: 'pending', delivered: 'pending', acknowledged: 'pending', completed: 'pending' });
  });

  it('awaiting confirmation after impact preview', () => {
    expect(states({ ...base, status: 'AWAITING_CONFIRMATION', impact_status: 'READY', requires_confirmation: true })).toMatchObject({
      impact: 'done',
      confirmation: 'current',
      delivered: 'pending',
    });
  });

  it('delivered ≠ acknowledged ≠ completed', () => {
    const dispatched: D = { ...base, status: 'DISPATCHED', impact_status: 'READY', requires_confirmation: true, delivery: { state: 'DISPATCHED', acknowledgements: [] } };
    expect(states(dispatched)).toMatchObject({ confirmation: 'done', delivered: 'done', acknowledged: 'current', completed: 'pending' });
    const acked: D = { ...dispatched, status: 'ACKNOWLEDGED', delivery: { state: 'ACKNOWLEDGED', acknowledgements: [] }, completion: { state: 'IN_PROGRESS', resulting_task_ids: [] } };
    expect(states(acked)).toMatchObject({ delivered: 'done', acknowledged: 'done', completed: 'current' });
    expect(states({ ...acked, status: 'COMPLETED', completion: { state: 'COMPLETED', resulting_task_ids: [] } }).completed).toBe('done');
  });

  it('no confirmation needed is shown as skipped; cancellation marks the current step', () => {
    expect(states({ ...base, status: 'DISPATCHED', impact_status: 'NOT_REQUIRED', delivery: { state: 'DISPATCHED', acknowledgements: [] } }).confirmation).toBe('skipped');
    expect(states({ ...base, status: 'CANCELLED', impact_status: 'READY', requires_confirmation: true })).toMatchObject({
      impact: 'done',
      confirmation: 'failed',
      delivered: 'pending',
      completed: 'pending',
    });
  });
});

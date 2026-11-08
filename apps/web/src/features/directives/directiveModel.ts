/**
 * Этапы обработки поручения для UI (FR-CHAT-04/05): «принято» (202) ≠ «доставлено агентам»
 * (delivery) ≠ «подтверждено агентами» (acknowledgements) ≠ «выполнено» (completion).
 */
import type { Directive, DirectiveStatus } from '../../api/directives';

export type StepId = 'accepted' | 'impact' | 'confirmation' | 'delivered' | 'acknowledged' | 'completed';
export type StepState = 'done' | 'current' | 'pending' | 'skipped' | 'failed';
export interface Step {
  id: StepId;
  state: StepState;
}

const AFTER_CONFIRM: readonly DirectiveStatus[] = ['CONFIRMED', 'CHECKPOINTING', 'REPLANNING', 'DISPATCHED', 'ACKNOWLEDGED', 'COMPLETED'];
const PREPARING: readonly DirectiveStatus[] = ['CONFIRMED', 'CHECKPOINTING', 'REPLANNING'];
const STOPPED: readonly DirectiveStatus[] = ['REJECTED', 'CANCELLED', 'FAILED'];

export function directiveSteps(d: Pick<Directive, 'status' | 'impact_status' | 'requires_confirmation' | 'delivery' | 'completion'>): Step[] {
  const impact: StepState =
    d.impact_status === 'READY' || d.impact_status === 'NOT_REQUIRED' ? 'done' : d.impact_status === 'FAILED' ? 'failed' : 'current';
  let confirmation: StepState;
  if (d.status === 'AWAITING_CONFIRMATION') confirmation = 'current';
  else if (AFTER_CONFIRM.includes(d.status)) confirmation = d.requires_confirmation ? 'done' : 'skipped';
  else confirmation = impact === 'done' && !d.requires_confirmation ? 'skipped' : 'pending';

  const dispatched = d.delivery.state !== 'NOT_DISPATCHED';
  const delivered: StepState = dispatched ? 'done' : PREPARING.includes(d.status) ? 'current' : 'pending';
  const acknowledged: StepState =
    d.delivery.state === 'ACKNOWLEDGED' ? 'done' : dispatched ? 'current' : 'pending';
  const completed: StepState =
    d.completion.state === 'COMPLETED'
      ? 'done'
      : d.completion.state === 'FAILED' || d.completion.state === 'CANCELLED'
        ? 'failed'
        : d.completion.state === 'IN_PROGRESS'
          ? 'current'
          : 'pending';

  const steps: Step[] = [
    { id: 'accepted', state: 'done' },
    { id: 'impact', state: impact },
    { id: 'confirmation', state: confirmation },
    { id: 'delivered', state: delivered },
    { id: 'acknowledged', state: acknowledged },
    { id: 'completed', state: completed },
  ];
  // Поручение остановлено (отозвано/отклонено/ошибка): первый незавершённый этап — «прервано», дальше — не начаты.
  if (STOPPED.includes(d.status)) {
    let marked = false;
    for (const s of steps) {
      if (s.state === 'done' || s.state === 'skipped') continue;
      if (!marked) {
        s.state = 'failed';
        marked = true;
      } else if (s.state !== 'failed') s.state = 'pending';
    }
  } else {
    // Ровно один «текущий» этап: всё после первого current/failed — pending.
    let seen = false;
    for (const s of steps) {
      if (seen && s.state === 'current') s.state = 'pending';
      if (s.state === 'current' || s.state === 'failed') seen = true;
    }
  }
  return steps;
}

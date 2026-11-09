import type { TFunction } from 'i18next';
import type { RunEvent } from '../../api/runEvents';

/**
 * Однострочные описания событий run (журнал событий Mission Control, Task Detail, лента Dashboard).
 * Поля payload — недоверенные данные: выводятся только как текст.
 */
function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : typeof v === 'number' ? String(v) : '';
}

function enumLabel(t: TFunction, group: string, value: unknown): string {
  const s = str(value);
  return s ? t(`enums.${group}.${s}`, { defaultValue: s }) : '';
}

/** Однострочное описание события ленты; неизвестный тип — только его название. */
export function describeEvent(t: TFunction, e: RunEvent, taskTitle: (id: string) => string | undefined): string {
  const p = e.payload;
  switch (e.type) {
    case 'run.state_changed': {
      const text = t('event.transition', {
        from: enumLabel(t, 'run_status', p.from_status),
        to: enumLabel(t, 'run_status', p.to_status),
      });
      return p.blocked_reason ? `${text} (${enumLabel(t, 'blocked_reason', p.blocked_reason)})` : text;
    }
    case 'task.state_changed': {
      const text = t('event.taskTransition', {
        task: str(p.title) || taskTitle(str(p.task_id)) || enumLabel(t, 'agent_role', p.agent_role),
        from: enumLabel(t, 'task_status', p.from_status),
        to: enumLabel(t, 'task_status', p.to_status),
      });
      return p.blocked_reason ? `${text} (${enumLabel(t, 'blocked_reason', p.blocked_reason)})` : text;
    }
    case 'agent.message':
      // from/to — ACP-участники {role, provider?} (sse-event.schema.json).
      return t('event.agentMessage', {
        from: enumLabel(t, 'agent_role', isRecord(p.from) ? p.from.role : p.from),
        to: enumLabel(t, 'agent_role', isRecord(p.to) ? p.to.role : p.to),
        summary: str(p.summary),
      });
    case 'cli.output': {
      // Коалесцированное событие (≤ 20/с на run): dropped_chunks — фрагменты, не вошедшие в text.
      const dropped = typeof p.dropped_chunks === 'number' && p.dropped_chunks > 0 ? t('event.cliDropped', { count: p.dropped_chunks }) : '';
      return [str(p.provider), enumLabel(t, 'agent_role', p.agent_role), str(p.text) || str(p.cli_event_type), dropped]
        .filter(Boolean)
        .join(' · ');
    }
    case 'approval.requested':
      return str(p.summary);
    case 'approval.decided':
      return t('event.approvalDecided', { action: str(p.action_type), status: str(p.status) });
    case 'gate.completed':
      return t('event.gate', { gate: str(p.gate_type), status: str(p.status) });
    case 'run.completed':
      return t('event.runCompleted', {
        status: enumLabel(t, 'run_status', p.final_status),
        passed: str(p.required_gates_passed),
        total: str(p.required_gates_total),
      });
    case 'directive.state_changed':
      return t('event.transition', { from: str(p.from_status), to: str(p.to_status) });
    case 'directive.impact_ready':
      return str(p.summary);
    case 'chat.message':
      return str(p.body);
    default:
      return '';
  }
}

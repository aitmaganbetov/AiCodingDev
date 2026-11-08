import type { TFunction } from 'i18next';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { IdempotentIntent } from '../../api/client';
import { DIRECTIVE_CANCELLABLE, directiveKeys, directivesApi, type Directive } from '../../api/directives';
import { Badge, Callout, StatusBadge, Timestamp, type Tone } from '../../components/ui';
import { problemMessage } from '../../lib/problem';
import { roleLabel } from '../runs/labels';
import { directiveSteps, type StepState } from './directiveModel';

const STEP_TONE: Record<StepState, Tone> = { done: 'success', current: 'info', pending: 'neutral', skipped: 'neutral', failed: 'danger' };
const ACK_TONE: Record<string, Tone> = { ACKNOWLEDGED: 'success', DELIVERED: 'info', PENDING: 'neutral', FAILED: 'danger' };

interface Props {
  directive: Directive;
  taskTitle: (taskId: string) => string | undefined;
  taskHref: (taskId: string) => string;
  approvalsHref: string;
  canAct: boolean;
  /** Уровень заголовков внутри (h3 в списке, h3 в диалоге). */
  headingLevel?: 3 | 4;
}

export function directiveTargetLabel(t: TFunction, d: Directive, taskTitle: (id: string) => string | undefined) {
  const target = d.target;
  if (target.type === 'role' && target.role) return t('directives.target.roleValue', { role: roleLabel(t, target.role) });
  if (target.type === 'task' && target.task_id)
    return t('directives.target.taskValue', { task: taskTitle(target.task_id) ?? target.task_id.slice(0, 8) });
  if (target.type === 'approval') return t('directives.target.approvalValue');
  return t('directives.target.runValue');
}

/** Ход поручения: статус обработки, impact preview, доставка/подтверждение агентами, выполнение; подтверждение/отзыв. */
export function DirectiveDetails({ directive: d, taskTitle, taskHref, approvalsHref, canAct, headingLevel = 3 }: Props) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const intent = useRef(new IdempotentIntent());
  const [confirmCancel, setConfirmCancel] = useState(false);
  const H = `h${headingLevel}` as const;
  const uid = useId();
  const action = useMutation({
    mutationFn: (kind: 'confirm' | 'cancel') => {
      const key = intent.current.keyFor({ id: d.id, kind, version: d.version });
      return kind === 'confirm'
        ? directivesApi.confirm(d.run_id, d.id, d.version, key)
        : directivesApi.cancel(d.run_id, d.id, d.version, key);
    },
    onSuccess: (next) => {
      intent.current.complete();
      setConfirmCancel(false);
      queryClient.setQueryData(directiveKeys.detail(d.run_id, d.id), next);
    },
    // 409/412: состояние ушло вперёд — перечитать.
    onSettled: () => void queryClient.invalidateQueries({ queryKey: directiveKeys.all(d.run_id) }),
  });

  const steps = directiveSteps(d);
  const awaiting = d.status === 'AWAITING_CONFIRMATION';
  const critical = awaiting && Boolean(d.approval_id);
  const cancellable = DIRECTIVE_CANCELLABLE.includes(d.status);
  const acks = d.delivery.acknowledgements;
  const acked = acks.filter((a) => a.status === 'ACKNOWLEDGED').length;

  return (
    <div className="flex flex-col gap-3 text-sm" data-testid="directive-details" data-directive-id={d.id} data-status={d.status}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-fg-muted">{t('directives.processing')}</span>
        <StatusBadge kind="directive" status={d.status} labelKey={`directives.enums.status.${d.status}`} />
      </div>

      <section aria-label={t('directives.stepsLabel')}>
        <ol className="flex flex-col gap-1" data-testid="directive-steps">
          {steps.map((s) => (
            <li key={s.id} className="flex flex-wrap items-center gap-2" data-step={s.id} data-state={s.state}>
              <Badge tone={STEP_TONE[s.state]}>{t(`directives.stepState.${s.state}`)}</Badge>
              <span>{t(`directives.step.${s.id}`)}</span>
              {s.id === 'acknowledged' && acks.length > 0 ? (
                <span className="text-fg-muted">{t('directives.ackCount', { done: acked, total: acks.length })}</span>
              ) : null}
            </li>
          ))}
        </ol>
      </section>

      <section aria-labelledby={`${uid}-impact`}>
        <H id={`${uid}-impact`} className="mb-1 font-semibold">
          {t('directives.impact.title')}{' '}
          <span className="font-normal text-fg-muted">({t(`directives.enums.impact_status.${d.impact_status}`, { defaultValue: d.impact_status })})</span>
        </H>
        {d.impact_summary ? <p className="mb-1 whitespace-pre-wrap break-words">{d.impact_summary}</p> : null}
        {d.impact_status === 'PENDING' ? (
          <p className="text-fg-muted">{t('directives.impact.pending')}</p>
        ) : d.impacts.length === 0 ? (
          <p className="text-fg-muted">{t('directives.impact.none')}</p>
        ) : (
          <ul className="flex flex-col gap-1" data-testid="directive-impacts">
            {d.impacts.map((imp, i) => (
              <li key={`${imp.task_id}-${i}`} className="flex flex-wrap items-baseline gap-2">
                <Badge tone={imp.impact_type === 'NONE' || imp.impact_type === 'CONTEXT_ONLY' ? 'neutral' : 'warning'}>
                  {t(`directives.enums.impact_type.${imp.impact_type}`, { defaultValue: imp.impact_type })}
                </Badge>
                <Link to={taskHref(imp.task_id)} className="link">
                  {taskTitle(imp.task_id) ?? t('run.unknownTask', { id: imp.task_id.slice(0, 8) })}
                </Link>
                {imp.resolution ? <span className="text-fg-muted">{imp.resolution}</span> : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby={`${uid}-delivery`}>
        <H id={`${uid}-delivery`} className="mb-1 font-semibold">
          {t('directives.delivery.title')}:{' '}
          <span className="font-normal" data-testid="directive-delivery" data-state={d.delivery.state}>
            {t(`directives.enums.delivery.${d.delivery.state}`, { defaultValue: d.delivery.state })}
          </span>
        </H>
        {acks.length > 0 ? (
          <ul className="flex flex-col gap-1">
            {acks.map((a) => (
              <li key={a.recipient_role} className="flex flex-wrap items-center gap-2">
                <span>{roleLabel(t, a.recipient_role)}</span>
                <Badge tone={ACK_TONE[a.status] ?? 'neutral'}>{t(`directives.enums.ack.${a.status}`, { defaultValue: a.status })}</Badge>
                {a.acknowledged_at ? <Timestamp value={a.acknowledged_at} format="time" className="text-fg-muted" /> : null}
              </li>
            ))}
          </ul>
        ) : null}
      </section>

      <section aria-labelledby={`${uid}-completion`}>
        <H id={`${uid}-completion`} className="mb-1 font-semibold">
          {t('directives.completion.title')}:{' '}
          <span className="font-normal" data-testid="directive-completion" data-state={d.completion.state}>
            {t(`directives.enums.completion.${d.completion.state}`, { defaultValue: d.completion.state })}
          </span>
        </H>
        {d.completion.resulting_task_ids.length > 0 ? (
          <p>
            {t('directives.completion.tasks')}{' '}
            {d.completion.resulting_task_ids.map((id, i) => (
              <span key={id}>
                {i > 0 ? ', ' : ''}
                <Link to={taskHref(id)} className="link">
                  {taskTitle(id) ?? t('run.unknownTask', { id: id.slice(0, 8) })}
                </Link>
              </span>
            ))}
          </p>
        ) : null}
      </section>

      {critical ? (
        <Callout tone="warning" title={t('directives.critical.title')}>
          {t('directives.critical.description')}{' '}
          <Link to={approvalsHref} className="link">
            {t('directives.critical.link')}
          </Link>
        </Callout>
      ) : null}

      {canAct && (awaiting || cancellable) ? (
        <div className="flex flex-col gap-2">
          {awaiting && !critical ? <p className="font-medium">{t('directives.confirmPrompt')}</p> : null}
          <div className="flex flex-wrap gap-3">
            {awaiting && !critical ? (
              <button type="button" className="btn-primary" disabled={action.isPending} onClick={() => action.mutate('confirm')}>
                {t('directives.confirm')}
              </button>
            ) : null}
            {cancellable && !confirmCancel ? (
              <button type="button" className="btn-secondary" disabled={action.isPending} onClick={() => setConfirmCancel(true)}>
                {t('directives.cancel')}
              </button>
            ) : null}
          </div>
          {confirmCancel && cancellable ? (
            <div className="flex flex-wrap items-center gap-3 rounded-md border border-warning-border bg-warning-soft p-3" role="alert">
              <p className="text-warning-fg">{t('directives.cancelConfirm')}</p>
              <button type="button" className="btn-primary" disabled={action.isPending} onClick={() => action.mutate('cancel')}>
                {t('directives.cancelYes')}
              </button>
              <button type="button" className="btn-secondary" onClick={() => setConfirmCancel(false)}>
                {t('directives.cancelNo')}
              </button>
            </div>
          ) : null}
        </div>
      ) : null}
      <p role="status" className="text-fg-muted">
        {action.isPending ? t('directives.sending') : action.isSuccess ? t(`directives.done.${action.variables}`) : ''}
      </p>
      {action.isError ? (
        <p role="alert" className="font-medium text-danger-text">
          {problemMessage(t, action.error)}
        </p>
      ) : null}
    </div>
  );
}

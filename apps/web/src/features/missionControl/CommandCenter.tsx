/**
 * User Command Center (F4, план §3.3, §6.3): поручение команде агентов → существующий поток директив
 * (createDirective с `expected_run_version`, 202 = «принято») → impact preview → подтверждение →
 * статусы «доставлено → подтверждено агентами → выполнено» (DirectiveTrackContent). Цель — все агенты
 * run, роль или задача. Быстрые действия — типы директив: вопрос (ASK), новая задача (NEW_TASK),
 * изменение (CHANGE_REQUEST), повторное ревью (REVIEW), пауза (PAUSE), продолжить (RESUME), вернуть на
 * доработку (CHANGE_REQUEST по задаче). Критические — подтверждение в диалоге до отправки; step-up,
 * если сервер его требует (`useStepUp`). Черновик текста сохраняется на устройстве (WCAG 3.3.7).
 */
import { useQuery } from '@tanstack/react-query';
import { useEffect, useId, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { directiveKeys, directivesApi, directiveTypesFor, type DirectiveCreateRequest, type DirectiveType } from '../../api/directives';
import type { AgentRole, Run, TaskSummary } from '../../api/runs';
import { useStepUp } from '../../auth/StepUp';
import { FormAlert } from '../../components/form';
import { RUN_PAUSABLE } from '../../components/StatusBadge';
import { Dialog, DialogClose, DialogContent, Icon, LoadingState, Toolbar, cx, type IconName } from '../../components/ui';
import { can } from '../../lib/permissions';
import { problemMessage } from '../../lib/problem';
import { AGENT_ROLES } from '../chat/chatModel';
import { DirectiveTrackContent, useCreateDirective } from '../directives/DirectiveDialog';
import { roleLabel } from '../runs/labels';

const MESSAGE_MAX = 8000;
type Target = 'run' | 'role' | 'task';

export const QUICK_ACTIONS = ['ask', 'newTask', 'change', 'rereview', 'pause', 'resume', 'rework'] as const;
export type QuickAction = (typeof QUICK_ACTIONS)[number];
/** Быстрое действие → тип директивы и цель по умолчанию. */
export const QUICK_ACTION_MAP: Record<QuickAction, { type: DirectiveType; target: Target; icon: IconName }> = {
  ask: { type: 'ASK', target: 'run', icon: 'help' },
  newTask: { type: 'NEW_TASK', target: 'run', icon: 'plus' },
  change: { type: 'CHANGE_REQUEST', target: 'run', icon: 'workflow' },
  rereview: { type: 'REVIEW', target: 'task', icon: 'roleReviewer' },
  pause: { type: 'PAUSE', target: 'run', icon: 'statusPaused' },
  resume: { type: 'RESUME', target: 'run', icon: 'play' },
  rework: { type: 'CHANGE_REQUEST', target: 'task', icon: 'refresh' },
};

/**
 * Требует подтверждения до отправки: остановка/пауза run, решения по approvals и изменения без
 * предварительного анализа влияния (с анализом подтверждение — по impact preview после него).
 */
export function needsPreConfirm(type: DirectiveType, preview: boolean): boolean {
  if (type === 'STOP' || type === 'PAUSE' || type === 'APPROVE' || type === 'REJECT') return true;
  return (type === 'CHANGE_REQUEST' || type === 'NEW_TASK') && !preview;
}

const draftKey = (runId: string) => `aicd.draft.mission-control.${runId}`;
function readDraft(runId: string): string {
  try {
    return window.localStorage.getItem(draftKey(runId)) ?? '';
  } catch {
    return '';
  }
}
function writeDraft(runId: string, text: string): void {
  try {
    if (text) window.localStorage.setItem(draftKey(runId), text);
    else window.localStorage.removeItem(draftKey(runId));
  } catch {
    /* хранилище недоступно — черновик живёт до перезагрузки */
  }
}

interface Props {
  run: Run;
  role: string;
  workspaceId: string;
  tasks: readonly TaskSummary[];
  selectedTask: TaskSummary | null;
  taskHref: (taskId: string) => string;
}

export function CommandCenter({ run, role, workspaceId, tasks, selectedTask, taskHref }: Props) {
  const { t } = useTranslation();
  const id = useId();
  const types = directiveTypesFor(role);
  const canCreate = can(role, 'directive:create') && types.length > 0;
  const [type, setType] = useState<DirectiveType>(types[0] ?? 'ASK');
  const [action, setAction] = useState<QuickAction | null>(null);
  const [target, setTarget] = useState<Target>('run');
  const [targetRole, setTargetRole] = useState<AgentRole | ''>('');
  const [targetTask, setTargetTask] = useState('');
  const [approvalId, setApprovalId] = useState('');
  const [message, setMessage] = useState(() => readDraft(run.id));
  const [preview, setPreview] = useState(true);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<DirectiveCreateRequest | null>(null);
  const [tracked, setTracked] = useState<{ id: string; type: DirectiveType } | null>(null);
  const [trackOpen, setTrackOpen] = useState(false);
  const stepUp = useStepUp();
  const needsApproval = type === 'APPROVE' || type === 'REJECT';
  const titles = new Map(tasks.map((x) => [x.id, x.title]));
  const taskTitle = (taskId: string) => titles.get(taskId);

  useEffect(() => writeDraft(run.id, message), [run.id, message]);

  const approvals = useQuery({
    queryKey: directiveKeys.approvals(run.id),
    queryFn: ({ signal }) => directivesApi.pendingApprovals(workspaceId, run.id, signal),
    enabled: canCreate && needsApproval,
  });

  const create = useCreateDirective(
    run,
    (accepted, body) => {
      setTracked({ id: accepted.directive_id, type: body.directive_type });
      setTrackOpen(true);
      setMessage('');
      setAction(null);
    },
    stepUp.run,
  );

  if (!canCreate) {
    return (
      <section aria-labelledby={`${id}-title`} className="flex flex-col gap-1 rounded-lg border border-border bg-surface p-3" data-testid="command-center">
        <h2 id={`${id}-title`} className="text-base font-semibold">
          {t('missionControl.command.title')}
        </h2>
        <p className="text-sm text-fg-muted">{t('directives.readOnly')}</p>
      </section>
    );
  }

  const pickAction = (a: QuickAction) => {
    const def = QUICK_ACTION_MAP[a];
    setAction(a);
    setType(def.type);
    setTarget(def.target);
    if (def.target === 'task') setTargetTask(selectedTask?.id ?? targetTask);
    setErrors({});
    requestAnimationFrame(() => document.getElementById(`${id}-message`)?.focus());
  };

  const actionDisabled = (a: QuickAction) => {
    const def = QUICK_ACTION_MAP[a];
    if (!types.includes(def.type)) return true;
    if (a === 'pause') return !RUN_PAUSABLE.includes(run.status);
    if (a === 'resume') return run.status !== 'PAUSED';
    if (def.target === 'task') return tasks.length === 0;
    return false;
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (create.isPending) return;
    const next: Record<string, string> = {};
    if (!message.trim()) next.message = t('directives.form.messageRequired');
    if (needsApproval && !approvalId) next.approval = t('directives.form.approvalRequired');
    if (!needsApproval && target === 'role' && !targetRole) next.role = t('directives.form.roleRequired');
    if (!needsApproval && target === 'task' && !targetTask) next.task = t('directives.form.taskRequired');
    setErrors(next);
    if (Object.keys(next).length > 0) {
      document.getElementById(`${id}-${Object.keys(next)[0] ?? 'message'}`)?.focus();
      return;
    }
    const body: DirectiveCreateRequest = {
      target: needsApproval
        ? { type: 'approval', approval_id: approvalId }
        : target === 'role'
          ? { type: 'role', role: targetRole as AgentRole }
          : target === 'task'
            ? { type: 'task', task_id: targetTask }
            : { type: 'run' },
      directive_type: type,
      message: message.trim(),
      expected_run_version: run.version,
      requires_impact_preview: preview,
    };
    if (needsPreConfirm(type, preview)) setPending(body);
    else create.mutate(body);
  };

  const field = (name: string) => ({
    id: `${id}-${name}`,
    'aria-invalid': Boolean(errors[name]),
    'aria-describedby': errors[name] ? `${id}-${name}-error` : undefined,
  });
  const fieldError = (name: string) =>
    errors[name] ? (
      <p id={`${id}-${name}-error`} className="text-sm font-medium text-danger-text">
        {errors[name]}
      </p>
    ) : null;
  const targetLabel = (b: DirectiveCreateRequest) =>
    b.target.type === 'role' && b.target.role
      ? t('directives.target.roleValue', { role: roleLabel(t, b.target.role) })
      : b.target.type === 'task' && b.target.task_id
        ? t('directives.target.taskValue', { task: taskTitle(b.target.task_id) ?? b.target.task_id.slice(0, 8) })
        : b.target.type === 'approval'
          ? t('directives.target.approvalValue')
          : t('directives.target.runValue');

  return (
    <section aria-labelledby={`${id}-title`} className="flex flex-col gap-2 rounded-lg border border-border bg-surface p-3" data-testid="command-center">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id={`${id}-title`} className="text-base font-semibold">
          {t('missionControl.command.title')}
        </h2>
        {tracked ? (
          <button type="button" className="btn-ghost btn-sm" onClick={() => setTrackOpen(true)}>
            <Icon name="history" className="h-4 w-4" />
            {t('missionControl.command.lastDirective', { type: t(`directives.enums.type.${tracked.type}`) })}
          </button>
        ) : null}
      </div>
      <Toolbar label={t('missionControl.command.quickActions')} className="flex flex-wrap gap-1">
        {QUICK_ACTIONS.map((a) => (
          <button
            key={a}
            type="button"
            className={cx('btn-sm', action === a ? 'btn-primary' : 'btn-secondary')}
            aria-pressed={action === a}
            disabled={actionDisabled(a)}
            onClick={() => pickAction(a)}
            data-action={a}
          >
            <Icon name={QUICK_ACTION_MAP[a].icon} className="h-4 w-4" />
            {t(`missionControl.command.action.${a}`)}
          </button>
        ))}
      </Toolbar>
      <form onSubmit={submit} noValidate className="flex flex-col gap-2">
        <div className="flex flex-wrap items-end gap-2">
          <div className="flex min-w-[10rem] flex-1 flex-col gap-1 sm:flex-none">
            <label htmlFor={`${id}-type`} className="text-xs font-medium text-fg-muted">
              {t('directives.form.type')}
            </label>
            <select
              id={`${id}-type`}
              className="select min-h-target text-sm"
              value={type}
              aria-describedby={`${id}-type-hint`}
              onChange={(e) => {
                setType(e.target.value as DirectiveType);
                setAction(null);
              }}
            >
              {types.map((x) => (
                <option key={x} value={x}>
                  {t(`directives.enums.type.${x}`)}
                </option>
              ))}
            </select>
          </div>
          {needsApproval ? (
            <div className="flex min-w-[12rem] flex-1 flex-col gap-1">
              <label htmlFor={`${id}-approval`} className="text-xs font-medium text-fg-muted">
                {t('directives.form.approval')}
              </label>
              {approvals.isPending ? (
                <LoadingState inline label={t('directives.form.approvalsLoading')} />
              ) : approvals.isError ? (
                <FormAlert messages={[problemMessage(t, approvals.error)]} />
              ) : (
                <select {...field('approval')} className="select min-h-target text-sm" value={approvalId} onChange={(e) => setApprovalId(e.target.value)}>
                  <option value="">{approvals.data.items.length === 0 ? t('directives.form.noApprovals') : t('directives.form.chooseApproval')}</option>
                  {approvals.data.items.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.summary}
                    </option>
                  ))}
                </select>
              )}
              {fieldError('approval')}
            </div>
          ) : (
            <>
              <fieldset className="flex flex-col gap-1">
                <legend className="text-xs font-medium text-fg-muted">{t('missionControl.command.target')}</legend>
                <div className="flex flex-wrap gap-x-3 gap-y-1">
                  {(['run', 'role', 'task'] as const).map((tt) => (
                    <div key={tt} className="flex min-h-target items-center gap-1.5">
                      <input
                        type="radio"
                        id={`${id}-target-${tt}`}
                        name={`${id}-target`}
                        className="h-4 w-4 accent-[rgb(var(--color-accent))]"
                        checked={target === tt}
                        onChange={() => {
                          setTarget(tt);
                          if (tt === 'task' && !targetTask && selectedTask) setTargetTask(selectedTask.id);
                        }}
                      />
                      <label htmlFor={`${id}-target-${tt}`} className="text-sm">
                        {t(`missionControl.command.targetOption.${tt}`)}
                      </label>
                    </div>
                  ))}
                </div>
              </fieldset>
              {target === 'role' ? (
                <div className="flex min-w-[10rem] flex-col gap-1">
                  <label htmlFor={`${id}-role`} className="text-xs font-medium text-fg-muted">
                    {t('directives.form.role')}
                  </label>
                  <select {...field('role')} className="select min-h-target text-sm" value={targetRole} onChange={(e) => setTargetRole(e.target.value as AgentRole | '')}>
                    <option value="">{t('chat.filters.chooseRole')}</option>
                    {AGENT_ROLES.map((r) => (
                      <option key={r} value={r}>
                        {roleLabel(t, r)}
                      </option>
                    ))}
                  </select>
                  {fieldError('role')}
                </div>
              ) : null}
              {target === 'task' ? (
                <div className="flex min-w-[10rem] max-w-full flex-col gap-1">
                  <label htmlFor={`${id}-task`} className="text-xs font-medium text-fg-muted">
                    {t('directives.form.task')}
                  </label>
                  <select {...field('task')} className="select min-h-target max-w-[18rem] text-sm" value={targetTask} onChange={(e) => setTargetTask(e.target.value)}>
                    <option value="">{t('chat.filters.chooseTask')}</option>
                    {tasks.map((x) => (
                      <option key={x.id} value={x.id}>
                        {x.title}
                      </option>
                    ))}
                  </select>
                  {fieldError('task')}
                </div>
              ) : null}
            </>
          )}
        </div>
        <p id={`${id}-type-hint`} className="text-xs text-fg-muted">
          {t(`directives.typeHint.${type}`)}
        </p>
        <div className="flex flex-col gap-1">
          <label htmlFor={`${id}-message`} className="text-sm font-medium">
            {t('directives.form.message')}
          </label>
          <textarea
            {...field('message')}
            className="input min-h-[72px] resize-y"
            value={message}
            maxLength={MESSAGE_MAX}
            placeholder={t('missionControl.command.placeholder')}
            onChange={(e) => setMessage(e.target.value)}
          />
          {fieldError('message')}
        </div>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-start gap-2">
            <input
              id={`${id}-preview`}
              type="checkbox"
              className="mt-1 h-4 w-4 accent-[rgb(var(--color-accent))]"
              checked={preview}
              aria-describedby={`${id}-preview-hint`}
              onChange={(e) => setPreview(e.target.checked)}
            />
            <div>
              <label htmlFor={`${id}-preview`} className="text-sm">
                {t('directives.form.preview')}
              </label>
              <p id={`${id}-preview-hint`} className="text-xs text-fg-muted">
                {t('missionControl.command.previewHint', { version: run.version })}
              </p>
            </div>
          </div>
          {/* Не disabled во время отправки: фокус остаётся на кнопке и вернётся на неё после диалога хода. */}
          <button type="submit" className="btn-primary" aria-disabled={create.isPending}>
            <Icon name="enter" className="h-4 w-4" />
            {create.isPending ? t('directives.form.submitting') : t('directives.form.submit')}
          </button>
        </div>
        {create.isError ? <FormAlert messages={[problemMessage(t, create.error)]} /> : null}
        <p role="status" className="text-xs text-fg-muted">
          {create.isPending ? t('directives.sending') : ''}
        </p>
      </form>
      {stepUp.dialog}

      <Dialog open={pending !== null} onOpenChange={(open) => !open && setPending(null)}>
        {pending ? (
          <DialogContent
            title={t('missionControl.command.confirmTitle', { type: t(`directives.enums.type.${pending.directive_type}`) })}
            description={t('missionControl.command.confirmDescription')}
            footer={
              <>
                <DialogClose className="btn-secondary">{t('action.cancel')}</DialogClose>
                <button
                  type="button"
                  className="btn-primary"
                  disabled={create.isPending}
                  onClick={() => {
                    create.mutate(pending);
                    setPending(null);
                  }}
                >
                  {t('missionControl.command.confirmSend')}
                </button>
              </>
            }
          >
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
              <dt className="text-fg-muted">{t('directives.form.type')}</dt>
              <dd>{t(`directives.enums.type.${pending.directive_type}`)}</dd>
              <dt className="text-fg-muted">{t('missionControl.command.target')}</dt>
              <dd>{targetLabel(pending)}</dd>
              <dt className="text-fg-muted">{t('directives.form.message')}</dt>
              <dd className="whitespace-pre-wrap break-words">{pending.message}</dd>
            </dl>
            <p className="mt-3 text-sm text-fg-muted">{t(`directives.typeHint.${pending.directive_type}`)}</p>
          </DialogContent>
        ) : null}
      </Dialog>

      <Dialog open={trackOpen && tracked !== null} onOpenChange={setTrackOpen}>
        {trackOpen && tracked ? (
          <DirectiveTrackContent
            runId={run.id}
            directiveId={tracked.id}
            type={tracked.type}
            taskTitle={taskTitle}
            taskHref={taskHref}
            approvalsHref={`/w/${workspaceId}/approvals?run_id=${encodeURIComponent(run.id)}`}
            canConfirm={can(role, 'directive:confirm')}
          />
        ) : null}
      </Dialog>
    </section>
  );
}

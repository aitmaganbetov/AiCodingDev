import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useId, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { IdempotentIntent, isApiError } from '../../api/client';
import {
  DIRECTIVE_ANALYZING,
  directiveKeys,
  directivesApi,
  directiveTypesFor,
  type DirectiveCreateRequest,
  type DirectiveTargetType,
  type DirectiveType,
} from '../../api/directives';
import { runKeys, type AgentRole, type Run, type TaskSummary } from '../../api/runs';
import { FormAlert } from '../../components/form';
import { DialogClose, DialogContent, LoadingState } from '../../components/ui';
import { problemMessage } from '../../lib/problem';
import { AGENT_ROLES } from '../chat/chatModel';
import { roleLabel } from '../runs/labels';
import { DirectiveDetails } from './DirectiveDetails';

const MESSAGE_MAX = 8000;

interface Props {
  run: Run;
  role: string;
  workspaceId: string;
  tasks: readonly TaskSummary[];
  taskHref: (taskId: string) => string;
  approvalsHref: string;
  canConfirm: boolean;
}

/**
 * Новое поручение (ТЗ §8.2): форма → 202 (принято) → анализ влияния → impact preview →
 * подтверждение → доставка/подтверждение агентами → выполнение. Содержимое диалога (DialogContent);
 * Dialog/Trigger — у вызывающего.
 */
export function DirectiveDialogContent({ run, role, workspaceId, tasks, taskHref, approvalsHref, canConfirm }: Props) {
  const { t } = useTranslation();
  const id = useId();
  const queryClient = useQueryClient();
  const intent = useRef(new IdempotentIntent());
  const types = directiveTypesFor(role);
  const [type, setType] = useState<DirectiveType>(types[0] ?? 'ASK');
  const [targetType, setTargetType] = useState<Exclude<DirectiveTargetType, 'approval'>>('run');
  const [targetRole, setTargetRole] = useState<AgentRole | ''>('');
  const [targetTask, setTargetTask] = useState('');
  const [approvalId, setApprovalId] = useState('');
  const [message, setMessage] = useState('');
  const [preview, setPreview] = useState(true);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [directiveId, setDirectiveId] = useState<string | null>(null);
  const needsApproval = type === 'APPROVE' || type === 'REJECT';
  const titles = new Map(tasks.map((x) => [x.id, x.title]));
  const taskTitle = (taskId: string) => titles.get(taskId);

  const approvals = useQuery({
    queryKey: directiveKeys.approvals(run.id),
    queryFn: ({ signal }) => directivesApi.pendingApprovals(workspaceId, run.id, signal),
    enabled: needsApproval && directiveId === null,
  });

  const create = useMutation({
    mutationFn: (body: DirectiveCreateRequest) => directivesApi.create(run.id, body, intent.current.keyFor(body)),
    onSuccess: (accepted) => {
      intent.current.complete();
      setDirectiveId(accepted.directive_id);
      void queryClient.invalidateQueries({ queryKey: directiveKeys.list(run.id) });
    },
    onError: (e) => {
      // Run ушёл вперёд (409 version_mismatch): перечитать версию — следующая отправка пойдёт с новой.
      if (isApiError(e) && (e.code === 'version_mismatch' || e.status === 409)) {
        void queryClient.invalidateQueries({ queryKey: runKeys.detail(run.id), exact: true });
      }
    },
  });

  const directive = useQuery({
    queryKey: directiveKeys.detail(run.id, directiveId ?? ''),
    queryFn: ({ signal }) => directivesApi.get(run.id, directiveId ?? '', signal),
    enabled: directiveId !== null,
    // Основной канал — SSE (directive.*); опрос — подстраховка, пока идёт анализ влияния.
    refetchInterval: (q) => (q.state.data && DIRECTIVE_ANALYZING.includes(q.state.data.status) ? 3000 : false),
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const next: Record<string, string> = {};
    if (!message.trim()) next.message = t('directives.form.messageRequired');
    if (needsApproval && !approvalId) next.approval = t('directives.form.approvalRequired');
    if (!needsApproval && targetType === 'role' && !targetRole) next.role = t('directives.form.roleRequired');
    if (!needsApproval && targetType === 'task' && !targetTask) next.task = t('directives.form.taskRequired');
    setErrors(next);
    if (Object.keys(next).length > 0) {
      const first = Object.keys(next)[0];
      document.getElementById(`${id}-${first}`)?.focus();
      return;
    }
    const target: DirectiveCreateRequest['target'] = needsApproval
      ? { type: 'approval', approval_id: approvalId }
      : targetType === 'role'
        ? { type: 'role', role: targetRole as AgentRole }
        : targetType === 'task'
          ? { type: 'task', task_id: targetTask }
          : { type: 'run' };
    create.mutate({
      target,
      directive_type: type,
      message: message.trim(),
      expected_run_version: run.version,
      requires_impact_preview: preview,
    });
  };

  if (directiveId !== null) {
    return (
      <DialogContent
        title={t('directives.dialog.trackTitle', { type: t(`directives.enums.type.${type}`) })}
        description={t('directives.dialog.trackDescription')}
        size="lg"
        footer={<DialogClose className="btn-secondary">{t('action.close')}</DialogClose>}
      >
        {directive.isPending ? (
          <LoadingState label={t('directives.loadingOne')} />
        ) : directive.isError ? (
          <FormAlert messages={[problemMessage(t, directive.error)]} />
        ) : (
          <DirectiveDetails
            directive={directive.data}
            taskTitle={taskTitle}
            taskHref={taskHref}
            approvalsHref={approvalsHref}
            canAct={canConfirm}
            headingLevel={3}
          />
        )}
      </DialogContent>
    );
  }

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
  const formId = `${id}-form`;

  return (
    <DialogContent
      title={t('directives.dialog.title')}
      description={t('directives.dialog.description')}
      size="lg"
      footer={
        <>
          <DialogClose className="btn-secondary">{t('action.cancel')}</DialogClose>
          <button type="submit" form={formId} className="btn-primary" disabled={create.isPending}>
            {create.isPending ? t('directives.form.submitting') : t('directives.form.submit')}
          </button>
        </>
      }
    >
      <form id={formId} onSubmit={submit} noValidate className="flex flex-col gap-4">
        <div className="flex flex-col gap-1">
          <label htmlFor={`${id}-type`} className="text-sm font-medium">
            {t('directives.form.type')}
          </label>
          <select
            id={`${id}-type`}
            className="input min-h-touch"
            value={type}
            aria-describedby={`${id}-type-hint`}
            onChange={(e) => setType(e.target.value as DirectiveType)}
          >
            {types.map((x) => (
              <option key={x} value={x}>
                {t(`directives.enums.type.${x}`)}
              </option>
            ))}
          </select>
          <p id={`${id}-type-hint`} className="text-sm text-fg-muted">
            {t(`directives.typeHint.${type}`)}
          </p>
        </div>

        {needsApproval ? (
          <div className="flex flex-col gap-1">
            <label htmlFor={`${id}-approval`} className="text-sm font-medium">
              {t('directives.form.approval')}
            </label>
            {approvals.isPending ? (
              <LoadingState inline label={t('directives.form.approvalsLoading')} />
            ) : approvals.isError ? (
              <FormAlert messages={[problemMessage(t, approvals.error)]} />
            ) : (
              <select {...field('approval')} className="input min-h-touch" value={approvalId} onChange={(e) => setApprovalId(e.target.value)}>
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
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-sm font-medium">{t('directives.form.target')}</legend>
            {(['run', 'role', 'task'] as const).map((tt) => (
              <div key={tt} className="flex items-center gap-2">
                <input
                  type="radio"
                  id={`${id}-target-${tt}`}
                  name={`${id}-target`}
                  className="h-4 w-4 accent-[rgb(var(--color-accent))]"
                  checked={targetType === tt}
                  onChange={() => setTargetType(tt)}
                />
                <label htmlFor={`${id}-target-${tt}`} className="text-sm">
                  {t(`directives.target.${tt}`)}
                </label>
              </div>
            ))}
            {targetType === 'role' ? (
              <div className="flex flex-col gap-1">
                <label htmlFor={`${id}-role`} className="text-sm font-medium">
                  {t('directives.form.role')}
                </label>
                <select {...field('role')} className="input min-h-touch" value={targetRole} onChange={(e) => setTargetRole(e.target.value as AgentRole | '')}>
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
            {targetType === 'task' ? (
              <div className="flex flex-col gap-1">
                <label htmlFor={`${id}-task`} className="text-sm font-medium">
                  {t('directives.form.task')}
                </label>
                <select {...field('task')} className="input min-h-touch" value={targetTask} onChange={(e) => setTargetTask(e.target.value)}>
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
          </fieldset>
        )}

        <div className="flex flex-col gap-1">
          <label htmlFor={`${id}-message`} className="text-sm font-medium">
            {t('directives.form.message')}
          </label>
          <textarea
            {...field('message')}
            className="input min-h-[120px]"
            value={message}
            maxLength={MESSAGE_MAX}
            onChange={(e) => setMessage(e.target.value)}
          />
          {fieldError('message')}
        </div>

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
            <p id={`${id}-preview-hint`} className="text-sm text-fg-muted">
              {t('directives.form.previewHint')}
            </p>
          </div>
        </div>
        <p className="text-sm text-fg-muted">{t('directives.form.version', { version: run.version })}</p>
        {create.isError ? <FormAlert messages={[problemMessage(t, create.error)]} /> : null}
      </form>
    </DialogContent>
  );
}

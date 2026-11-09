/**
 * Параметры рабочего пространства (OQ-45 (f)): GET/PATCH /workspaces/{id}.
 *
 * If-Match строится из ETag последнего ответа сервера (API_DESIGN §9), а при его отсутствии —
 * из поля `version`. Название меняют owner/admin; политику самоутверждения — только owner
 * (сервер отвечает 403 остальным). 412 — данные устарели: форма перечитывает workspace.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useId, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { isApiError, versionFromEtag, type ApiResponse } from '../../api/client';
import { workspaceKeys, workspacesApi, type Workspace, type WorkspaceRole, type WorkspaceUpdateRequest } from '../../api/workspaces';
import { sessionQueryKey } from '../../auth/session';
import { useStepUp } from '../../auth/StepUp';
import { FormAlert, TextField, focusFirstInvalid } from '../../components/form';
import { ErrorState, LoadingState } from '../../components/states';
import { Button, Callout, Card, useToast } from '../../components/ui';
import { can } from '../../lib/permissions';
import { mapProblemToFields } from '../../lib/problem';

export const WORKSPACE_NAME_MAX = 200;

export interface WorkspaceDraft {
  name: string;
  selfApproval: boolean;
}

/** Merge-patch из черновика: только изменённые поля; политики — только если их можно менять. */
export function buildWorkspacePatch(
  current: Workspace,
  draft: WorkspaceDraft,
  canPolicy: boolean,
): { error: 'required' | 'max' | null; patch: WorkspaceUpdateRequest | null } {
  const name = draft.name.trim();
  if (!name) return { error: 'required', patch: null };
  if (name.length > WORKSPACE_NAME_MAX) return { error: 'max', patch: null };
  const patch: WorkspaceUpdateRequest = {};
  if (name !== current.name) patch.name = name;
  const currentSelf = current.settings?.allow_single_member_self_approval === true;
  if (canPolicy && draft.selfApproval !== currentSelf) patch.settings = { allow_single_member_self_approval: draft.selfApproval };
  return { error: null, patch: Object.keys(patch).length > 0 ? patch : null };
}

function versionOf(r: ApiResponse<Workspace>): number {
  return versionFromEtag(r.etag) ?? r.data.version ?? 0;
}

function draftOf(ws: Workspace): WorkspaceDraft {
  return { name: ws.name, selfApproval: ws.settings?.allow_single_member_self_approval === true };
}

function WorkspaceForm({ loaded, role, onSaved }: { loaded: ApiResponse<Workspace>; role: WorkspaceRole; onSaved: (r: ApiResponse<Workspace>) => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const queryClient = useQueryClient();
  const uid = useId();
  const formRef = useRef<HTMLFormElement>(null);
  const canPolicy = can(role, 'workspace:policy');
  // База для If-Match и сравнения; новая версия с сервера (после сохранения или 412) сбрасывает черновик.
  const [base, setBase] = useState<ApiResponse<Workspace>>(loaded);
  const [draft, setDraft] = useState<WorkspaceDraft>(() => draftOf(loaded.data));
  if (versionOf(loaded) > versionOf(base)) {
    setBase(loaded);
    setDraft(draftOf(loaded.data));
  }
  const ws = base.data;
  const [nameError, setNameError] = useState<string | undefined>(undefined);
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const [info, setInfo] = useState<string | null>(null);

  // Политика (settings) — критическое действие: при 403 step_up_required — диалог пароля и повтор (SC-IAM-08).
  const stepUp = useStepUp();
  const save = useMutation({
    mutationFn: (patch: WorkspaceUpdateRequest) =>
      stepUp.run(() => workspacesApi.update(ws.id, patch, { etag: base.etag, version: base.etag ? undefined : ws.version })),
    onSuccess: async (updated) => {
      setBase(updated);
      setDraft(draftOf(updated.data));
      onSaved(updated);
      setInfo(t('settings.workspace.saved'));
      toast({ title: t('settings.workspace.saved'), tone: 'success' });
      await queryClient.invalidateQueries({ queryKey: sessionQueryKey });
      await queryClient.invalidateQueries({ queryKey: workspaceKeys.all });
    },
    onError: async (error) => {
      if (isApiError(error) && error.status === 412) {
        setFormErrors([t('settings.workspace.stale')]);
        await queryClient.invalidateQueries({ queryKey: workspaceKeys.detail(ws.id) });
        return;
      }
      const mapped = mapProblemToFields(t, error, ['name'] as const);
      setNameError(mapped.fields.name);
      setFormErrors(mapped.form);
      requestAnimationFrame(() => focusFirstInvalid(formRef.current));
    },
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    setInfo(null);
    setFormErrors([]);
    const { error, patch } = buildWorkspacePatch(ws, draft, canPolicy);
    setNameError(error === 'required' ? t('validation.required') : error === 'max' ? t('validation.maxLength', { max: WORKSPACE_NAME_MAX }) : undefined);
    if (error) {
      requestAnimationFrame(() => focusFirstInvalid(formRef.current));
      return;
    }
    if (!patch) {
      setInfo(t('settings.workspace.noChanges'));
      return;
    }
    save.mutate(patch);
  };

  return (
    <form ref={formRef} noValidate onSubmit={onSubmit} className="flex flex-col gap-4" aria-label={t('settings.workspace.formLabel')}>
      <FormAlert messages={formErrors} />
      <TextField
        id={`${uid}-name`}
        label={t('settings.workspace.name')}
        maxLength={WORKSPACE_NAME_MAX}
        autoComplete="off"
        value={draft.name}
        onChange={(e) => setDraft({ ...draft, name: e.target.value })}
        error={nameError}
      />
      <fieldset className="flex flex-col gap-1">
        <legend className="mb-1 text-sm font-medium">{t('settings.workspace.policies')}</legend>
        <div className="flex items-start gap-2">
          <input
            id={`${uid}-self`}
            type="checkbox"
            className="mt-1 h-4 w-4 accent-[rgb(var(--color-accent))]"
            checked={draft.selfApproval}
            disabled={!canPolicy}
            aria-describedby={`${uid}-self-hint`}
            onChange={(e) => setDraft({ ...draft, selfApproval: e.target.checked })}
          />
          <label htmlFor={`${uid}-self`} className="text-sm">
            {t('settings.workspace.selfApproval')}
          </label>
        </div>
        <p id={`${uid}-self-hint`} className="text-sm text-fg-muted">
          {canPolicy ? t('settings.workspace.selfApprovalHint') : t('settings.workspace.policyOwnerOnly')}
        </p>
      </fieldset>
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" variant="primary" loading={save.isPending}>
          {t('settings.workspace.save')}
        </Button>
        <p role="status" className="text-sm text-fg-muted">
          {info}
        </p>
      </div>
      {stepUp.dialog}
    </form>
  );
}

export function WorkspacePanel({ workspaceId, role }: { workspaceId: string; role: WorkspaceRole }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: workspaceKeys.detail(workspaceId),
    queryFn: ({ signal }) => workspacesApi.get(workspaceId, signal),
  });
  const canEdit = can(role, 'workspace:update');
  let body;
  if (query.isPending) body = <LoadingState label={t('settings.workspace.loading')} />;
  else if (query.isError) body = <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  else {
    const ws = query.data.data;
    body = (
      <>
        <dl className="mb-4 grid grid-cols-[auto,1fr] gap-x-4 gap-y-1 text-sm">
          {!canEdit ? (
            <>
              <dt className="text-fg-muted">{t('settings.workspace.name')}</dt>
              <dd className="break-words">{ws.name}</dd>
            </>
          ) : null}
          <dt className="text-fg-muted">{t('settings.workspace.slug')}</dt>
          <dd className="font-mono">{ws.slug}</dd>
          <dt className="text-fg-muted">{t('settings.workspace.yourRole')}</dt>
          <dd>{t(`enums.workspace_role.${role}`, { defaultValue: role })}</dd>
          {!canEdit ? (
            <>
              <dt className="text-fg-muted">{t('settings.workspace.selfApproval')}</dt>
              <dd>{ws.settings?.allow_single_member_self_approval === true ? t('settings.workspace.on') : t('settings.workspace.off')}</dd>
            </>
          ) : null}
        </dl>
        {canEdit ? (
          <WorkspaceForm
            key={ws.id}
            loaded={query.data}
            role={role}
            onSaved={(r) => queryClient.setQueryData(workspaceKeys.detail(workspaceId), r)}
          />
        ) : (
          <Callout tone="neutral">{t('settings.workspace.readOnly')}</Callout>
        )}
      </>
    );
  }
  return (
    <Card as="section" id="workspace-info" title={t('settings.workspace.title')}>
      {body}
    </Card>
  );
}

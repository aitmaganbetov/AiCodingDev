import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams } from 'react-router';
import { IdempotentIntent } from '../../api/client';
import { projectKeys, projectsApi } from '../../api/projects';
import {
  AGENT_ROLES,
  rationaleRequired,
  standardsApi,
  standardsKeys,
  type AgentRole,
  type ControlApplicability,
  type ControlApprovalState,
  type ProjectControl,
  type ProjectControlApprovalRequest,
} from '../../api/standards';
import { FormAlert, RadioGroup, TextAreaField } from '../../components/form';
import { LoadMore } from '../../components/LoadMore';
import { PageHeading } from '../../components/PageHeading';
import { ErrorState, LoadingState } from '../../components/states';
import { Badge, Callout } from '../../components/ui/Badge';
import { Button } from '../../components/ui/Button';
import { Dialog, DialogClose, DialogContent } from '../../components/ui/Dialog';
import { Table, type Column } from '../../components/ui/Table';
import { Timestamp } from '../../components/ui/Time';
import { useToast } from '../../components/ui/Toast';
import { can } from '../../lib/permissions';
import { mapProblemToFields } from '../../lib/problem';
import { useCurrentWorkspace } from '../../workspace';
import { useWorkspaceRole } from '../quality/shared';
import { ComplianceDisclaimer } from './ComplianceDisclaimer';
import { filterControls, type ControlFilters } from './model';

export const CONTROLS_PAGE_SIZE = 100;
export const RATIONALE_MAX = 4000;
const APPROVAL_STATES: readonly ControlApprovalState[] = ['draft', 'approved', 'missing'];
const APPLICABILITY: readonly ControlApplicability[] = ['applicable', 'tailored', 'not_applicable'];
type BlockingChoice = 'default' | 'true' | 'false';
type ApprovalField = 'applicability' | 'rationale' | 'agent_role' | 'blocking';

/** Блокирующий статус: эффективное значение + откуда оно (явное значение проекта или умолчание платформы). */
export function BlockingCell({ control }: { control: ProjectControl }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col items-start gap-1" data-testid="blocking">
      <Badge tone={control.effective_blocking ? 'danger' : 'neutral'}>
        {control.effective_blocking ? t('compliance.controls.blocks') : t('compliance.controls.notBlocks')}
      </Badge>
      <span className="text-xs text-fg-muted">
        {control.blocking === null ? t('compliance.controls.platformDefault') : t('compliance.controls.projectExplicit')}
      </span>
    </div>
  );
}

function ApprovalDialog({ projectId, control, onClose }: { projectId: string; control: ProjectControl; onClose: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const queryClient = useQueryClient();
  const intent = useRef(new IdempotentIntent());
  const [applicability, setApplicability] = useState<ControlApplicability>(control.proposed_applicability ?? 'applicable');
  const [rationale, setRationale] = useState(control.rationale ?? '');
  const [role, setRole] = useState<AgentRole | ''>(control.agent_role ?? '');
  const [blocking, setBlocking] = useState<BlockingChoice>(control.blocking === null ? 'default' : control.blocking ? 'true' : 'false');
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<ApprovalField, string>>>({});
  const [formErrors, setFormErrors] = useState<string[]>([]);

  const approve = useMutation({
    mutationFn: (body: ProjectControlApprovalRequest) =>
      standardsApi.approve(projectId, control.control_id, body, intent.current.keyFor({ control: control.control_id, ...body })),
    onSuccess: (updated) => {
      intent.current.complete();
      void queryClient.invalidateQueries({ queryKey: standardsKeys.all(projectId) });
      toast({ title: t('compliance.controls.approved', { control: updated.control_code }), tone: 'success' });
      onClose();
    },
    onError: (error) => {
      const mapped = mapProblemToFields(t, error, ['applicability', 'rationale', 'agent_role', 'blocking'] as const);
      setFieldErrors(mapped.fields);
      setFormErrors(mapped.form);
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const trimmed = rationale.trim();
    const errors: Partial<Record<ApprovalField, string>> = {};
    if (rationaleRequired(applicability) && !trimmed) errors.rationale = t('compliance.controls.rationaleRequired');
    else if (trimmed.length > RATIONALE_MAX) errors.rationale = t('validation.maxLength', { max: RATIONALE_MAX });
    setFieldErrors(errors);
    setFormErrors([]);
    if (Object.keys(errors).length > 0) {
      requestAnimationFrame(() => document.getElementById('control-rationale')?.focus());
      return;
    }
    const body: ProjectControlApprovalRequest = {
      applicability,
      ...(trimmed ? { rationale: trimmed } : {}),
      ...(role ? { agent_role: role } : {}),
      blocking: blocking === 'default' ? null : blocking === 'true',
    };
    approve.mutate(body);
  };

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent size="lg" title={t('compliance.controls.dialogTitle', { control: control.control_code })} description={control.description}>
        <form onSubmit={submit} noValidate className="flex flex-col gap-4">
          <FormAlert messages={formErrors} />
          <RadioGroup
            name="control-applicability"
            legend={t('compliance.controls.applicability')}
            value={applicability}
            onChange={setApplicability}
            error={fieldErrors.applicability}
            options={APPLICABILITY.map((a) => ({ value: a, label: t(`compliance.enums.proposed.${a}`) }))}
          />
          <TextAreaField
            id="control-rationale"
            label={t('compliance.controls.rationale')}
            hint={rationaleRequired(applicability) ? t('compliance.controls.rationaleHintRequired') : t('compliance.controls.rationaleHint')}
            error={fieldErrors.rationale}
            value={rationale}
            maxLength={RATIONALE_MAX}
            required={rationaleRequired(applicability)}
            onChange={(e) => setRationale(e.target.value)}
          />
          <div className="flex flex-col gap-1">
            <label htmlFor="control-role" className="text-sm font-medium">
              {t('compliance.controls.role')}
            </label>
            <select id="control-role" className="input" value={role} onChange={(e) => setRole(e.target.value as AgentRole | '')}>
              <option value="">
                {t('compliance.controls.roleDefault', {
                  role: control.default_agent_role ? t(`enums.agent_role.${control.default_agent_role}`) : '—',
                })}
              </option>
              {AGENT_ROLES.map((r) => (
                <option key={r} value={r}>
                  {t(`enums.agent_role.${r}`, { defaultValue: r })}
                </option>
              ))}
            </select>
            {fieldErrors.agent_role ? <p className="text-sm font-medium text-danger-text">{fieldErrors.agent_role}</p> : null}
          </div>
          <RadioGroup
            name="control-blocking"
            legend={t('compliance.controls.blocking')}
            value={blocking}
            onChange={setBlocking}
            error={fieldErrors.blocking}
            options={[
              { value: 'default', label: t('compliance.controls.blockingDefault'), description: t('compliance.controls.blockingDefaultHint') },
              { value: 'true', label: t('compliance.controls.blocks') },
              { value: 'false', label: t('compliance.controls.notBlocks') },
            ]}
          />
          <div className="flex flex-wrap justify-end gap-3">
            <DialogClose asChild>
              <Button>{t('action.cancel')}</Button>
            </DialogClose>
            <Button type="submit" variant="primary" loading={approve.isPending}>
              {t('compliance.controls.submit')}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function ProjectControlsPage() {
  const { t } = useTranslation();
  const { projectId = '' } = useParams();
  const { workspaceId } = useCurrentWorkspace();
  const role = useWorkspaceRole();
  const canApprove = can(role, 'project:update');
  const [approvalState, setApprovalState] = useState<ControlApprovalState | ''>('');
  const [editing, setEditing] = useState<ProjectControl | null>(null);
  const project = useQuery({ queryKey: projectKeys.detail(projectId), queryFn: ({ signal }) => projectsApi.get(projectId, signal) });
  const query = approvalState ? { approval_state: approvalState } : {};
  const controls = useInfiniteQuery({
    queryKey: standardsKeys.controls(projectId, query),
    queryFn: ({ pageParam, signal }) =>
      standardsApi.controls(projectId, { ...query, limit: CONTROLS_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const first = controls.data?.pages[0];
  const all = useMemo(() => controls.data?.pages.flatMap((p) => p.items) ?? [], [controls.data]);
  const [filters, setFilters] = useState<ControlFilters>({ standard: '', applicability: '', blocking: '', q: '' });
  const rows = useMemo(() => filterControls(all, filters), [all, filters]);
  const standards = useMemo(() => [...new Set(all.map((c) => c.standard_code))].sort(), [all]);
  const setFilter = <K extends keyof ControlFilters>(key: K, value: ControlFilters[K]) => setFilters((f) => ({ ...f, [key]: value }));

  const columns: Column<ProjectControl>[] = [
    {
      id: 'standard',
      header: t('compliance.col.standard'),
      cell: (c) => (
        <span>
          {c.standard_code} <span className="text-fg-muted">({c.edition})</span>
          {c.standard_title ? <span className="block text-xs text-fg-muted">{c.standard_title}</span> : null}
        </span>
      ),
      sortValue: (c) => c.standard_code,
    },
    {
      id: 'control',
      header: t('compliance.col.control'),
      rowHeader: true,
      cell: (c) => (
        <span>
          <span className="font-mono text-xs">{c.control_code}</span> <span className="text-fg-muted">§ {c.clause_ref}</span>
          <span className="block text-xs">{c.description}</span>
        </span>
      ),
      sortValue: (c) => c.control_code,
    },
    {
      id: 'applicability',
      header: t('compliance.col.applicability'),
      cell: (c) => (
        <div className="flex flex-col items-start gap-1">
          <Badge tone={c.approval_state === 'approved' ? 'success' : 'warning'}>{t(`compliance.enums.approval_state.${c.approval_state}`)}</Badge>
          <span className="text-xs">
            {c.proposed_applicability
              ? t(`compliance.enums.proposed.${c.proposed_applicability}`)
              : t(`compliance.enums.applicability.${c.applicability}`, { defaultValue: c.applicability })}
          </span>
          {c.rationale ? <span className="whitespace-pre-line break-words text-xs text-fg-muted">{c.rationale}</span> : null}
        </div>
      ),
    },
    {
      id: 'role',
      header: t('compliance.col.role'),
      cell: (c) => {
        const r = c.agent_role ?? c.default_agent_role;
        return r ? t(`enums.agent_role.${r}`, { defaultValue: r }) : '—';
      },
    },
    {
      id: 'check',
      header: t('compliance.col.check'),
      cell: (c) => (
        <span>
          {t(`compliance.enums.method.${c.verification_method}`)}
          {c.gate_type ? <span className="block font-mono text-xs text-fg-muted">{c.gate_type}</span> : null}
        </span>
      ),
    },
    { id: 'blocking', header: t('compliance.controls.blocking'), cell: (c) => <BlockingCell control={c} />, sortValue: (c) => (c.effective_blocking ? 0 : 1) },
    {
      id: 'approvedBy',
      header: t('compliance.controls.approvedBy'),
      cell: (c) =>
        c.approved_by ? (
          <span className="text-xs">
            {c.approved_by.display_name}
            {c.approved_at ? (
              <span className="block text-fg-muted">
                <Timestamp value={c.approved_at} />
              </span>
            ) : null}
          </span>
        ) : (
          '—'
        ),
    },
    ...(canApprove
      ? [
          {
            id: 'actions',
            header: t('compliance.col.actions'),
            cell: (c: ProjectControl) => (
              <Button size="sm" onClick={() => setEditing(c)}>
                {c.approval_state === 'approved' ? t('compliance.controls.revise') : t('compliance.controls.approve')}
                <span className="sr-only">: {c.control_code}</span>
              </Button>
            ),
          },
        ]
      : []),
  ];

  if (!workspaceId) return null;
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={project.data ? t('compliance.controls.titleWithProject', { project: project.data.name }) : t('compliance.controls.title')} />
      <ComplianceDisclaimer className="mb-4" />
      <div className="mb-4 flex flex-col gap-3">
        <p className="text-sm text-fg-muted">{t('compliance.controls.description')}</p>
        {first?.quality_profile ? (
          <p className="text-sm">
            {t('quality.profile')}: {t(`enums.quality_profile.${first.quality_profile}`)}
          </p>
        ) : null}
        {first?.disclaimer ? (
          <Callout tone="info">
            <p data-testid="compliance-disclaimer">{first.disclaimer}</p>
          </Callout>
        ) : null}
        {!canApprove ? <p className="text-sm text-fg-muted">{t('compliance.controls.readOnly')}</p> : null}
        <div role="group" aria-label={t('compliance.index.controlFilters')} className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <div className="flex min-w-0 flex-col gap-1">
            <label htmlFor="controls-approval-state" className="text-sm font-medium">
              {t('compliance.controls.approvalState')}
            </label>
            <select
              id="controls-approval-state"
              className="input"
              value={approvalState}
              onChange={(e) => setApprovalState(e.target.value as ControlApprovalState | '')}
            >
              <option value="">{t('quality.finding.any')}</option>
              {APPROVAL_STATES.map((s) => (
                <option key={s} value={s}>
                  {t(`compliance.enums.approval_state.${s}`)}
                </option>
              ))}
            </select>
          </div>
          <div className="flex min-w-0 flex-col gap-1">
            <label htmlFor="controls-standard" className="text-sm font-medium">
              {t('compliance.col.standard')}
            </label>
            <select id="controls-standard" className="input" value={filters.standard} onChange={(e) => setFilter('standard', e.target.value)}>
              <option value="">{t('quality.finding.any')}</option>
              {standards.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
          <div className="flex min-w-0 flex-col gap-1">
            <label htmlFor="controls-q" className="text-sm font-medium">
              {t('compliance.index.controlSearch')}
            </label>
            <input id="controls-q" type="search" className="input" value={filters.q} onChange={(e) => setFilter('q', e.target.value)} />
          </div>
          <div className="flex min-w-0 flex-col gap-1">
            <label htmlFor="controls-applicability" className="text-sm font-medium">
              {t('compliance.col.applicability')}
            </label>
            <select id="controls-applicability" className="input" value={filters.applicability} onChange={(e) => setFilter('applicability', e.target.value)}>
              <option value="">{t('quality.finding.any')}</option>
              {(['APPLICABLE', 'NOT_APPLICABLE', 'PENDING_REVIEW'] as const).map((a) => (
                <option key={a} value={a}>
                  {t(`compliance.enums.applicability.${a}`)}
                </option>
              ))}
            </select>
          </div>
          <div className="flex min-w-0 flex-col gap-1">
            <label htmlFor="controls-blocking" className="text-sm font-medium">
              {t('compliance.controls.blocking')}
            </label>
            <select
              id="controls-blocking"
              className="input"
              value={filters.blocking}
              onChange={(e) => setFilter('blocking', e.target.value as ControlFilters['blocking'])}
            >
              <option value="">{t('quality.finding.any')}</option>
              <option value="yes">{t('compliance.controls.blocks')}</option>
              <option value="no">{t('compliance.controls.notBlocks')}</option>
            </select>
          </div>
        </div>
      </div>
      {controls.isPending ? (
        <LoadingState label={t('compliance.controls.loading')} />
      ) : controls.isError && rows.length === 0 ? (
        <ErrorState error={controls.error} onRetry={() => void controls.refetch()} />
      ) : (
        <>
          <Table
            caption={t('compliance.controls.caption')}
            columns={columns}
            rows={rows}
            rowKey={(c) => c.control_id}
            rowProps={(c) => ({ 'data-control': c.control_code })}
            empty={t('compliance.controls.empty')}
            minWidthClass="min-w-[64rem]"
          />
          <LoadMore
            shown={rows.length}
            hasNextPage={controls.hasNextPage}
            isFetchingNextPage={controls.isFetchingNextPage}
            nextPageError={controls.isFetchNextPageError ? controls.error : null}
            onLoadMore={() => void controls.fetchNextPage()}
          />
        </>
      )}
      {editing ? <ApprovalDialog projectId={projectId} control={editing} onClose={() => setEditing(null)} /> : null}
    </section>
  );
}

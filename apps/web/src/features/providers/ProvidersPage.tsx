import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query';
import { useEffect, useId, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { analyticsApi, analyticsKeys, type AnalyticsSummary } from '../../api/analytics';
import { IdempotentIntent } from '../../api/client';
import {
  providerKeys,
  providersApi,
  type AuthStatus,
  type Provider,
  type ProviderAvailability,
  type ProviderInstallation,
  type ProviderProbeStatus,
  type ProviderSettings,
  type ProviderSettingsUpdateRequest,
} from '../../api/providers';
import { FormAlert, TextField } from '../../components/form';
import { PageHeading } from '../../components/PageHeading';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import { Badge, Button, Callout, Card, CodeBlock, Icon, RelativeTime, Table, Timestamp, useToast, type Column, type IconName, type Tone } from '../../components/ui';
import { CHIP_STATE_ICON, CHIP_STATE_TONE, providerChipModel } from '../../layout/providerStatus';
import { can } from '../../lib/permissions';
import { mapProblemToFields, problemMessage } from '../../lib/problem';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { AGENT_ROLES, MAX_PARALLEL, TIMEOUT_SECONDS, buildSettingsPatch, draftFrom, type SettingsDraft, type SettingsField } from './settingsForm';
import { useStepUp } from '../../auth/StepUp';
import { dashboardWindow } from '../dashboard/model';
import { ExecutionHistory } from './ExecutionHistory';
import { ProviderUsage } from './ProviderUsage';

const AUTH_TONE: Record<AuthStatus, Tone> = {
  AUTHENTICATED: 'success',
  AUTH_REQUIRED: 'warning',
  EXPIRED: 'warning',
  ERROR: 'danger',
  UNKNOWN: 'neutral',
};
const AVAILABILITY_TONE: Record<ProviderAvailability, Tone> = {
  AVAILABLE: 'success',
  DEGRADED: 'warning',
  RATE_LIMITED: 'warning',
  UNAVAILABLE: 'danger',
};
const NEEDS_LOGIN: readonly AuthStatus[] = ['AUTH_REQUIRED', 'EXPIRED'];

function InstallationsTable({ provider }: { provider: Provider }) {
  const { t } = useTranslation();
  const columns: Column<ProviderInstallation>[] = [
    { id: 'host', header: t('providers.col.host'), rowHeader: true, cell: (i) => <span className="break-all">{i.host}</span>, sortValue: (i) => i.host },
    { id: 'version', header: t('providers.col.version'), cell: (i) => <span className="font-mono">{i.detected_version ?? '—'}</span> },
    {
      id: 'compatibility',
      header: t('providers.col.compatibility'),
      cell: (i) => (
        <Badge tone={i.compatibility === 'SUPPORTED' ? 'success' : i.compatibility === 'UNSUPPORTED' ? 'danger' : 'neutral'}>
          {t(`providers.enums.compatibility.${i.compatibility}`, { defaultValue: i.compatibility })}
        </Badge>
      ),
    },
    {
      id: 'auth',
      header: t('providers.col.auth'),
      cell: (i) => (
        <Badge tone={AUTH_TONE[i.auth_status] ?? 'neutral'} data-auth-status={i.auth_status}>
          {t(`providers.enums.auth_status.${i.auth_status}`, { defaultValue: i.auth_status })}
        </Badge>
      ),
    },
    {
      id: 'availability',
      header: t('providers.col.availability'),
      cell: (i) => (
        <Badge tone={AVAILABILITY_TONE[i.availability] ?? 'neutral'}>
          {t(`providers.enums.availability.${i.availability}`, { defaultValue: i.availability })}
        </Badge>
      ),
    },
    {
      id: 'error',
      header: t('providers.col.lastError'),
      cell: (i) =>
        i.last_error_category ? t(`providers.enums.cli_error.${i.last_error_category}`, { defaultValue: i.last_error_category }) : '—',
    },
    { id: 'checked', header: t('providers.col.checkedAt'), cell: (i) => (i.checked_at ? <Timestamp value={i.checked_at} /> : t('providers.neverChecked')) },
  ];
  return (
    <Table
      caption={t('providers.installationsCaption', { name: provider.display_name })}
      columns={columns}
      rows={provider.installations}
      rowKey={(i) => i.installation_id}
      empty={t('providers.noInstallations')}
      minWidthClass="min-w-[48rem]"
    />
  );
}

function Capabilities({ provider }: { provider: Provider }) {
  const { t } = useTranslation();
  const withCaps = provider.installations.filter((i) => Object.keys(i.capabilities).length > 0);
  if (withCaps.length === 0) return <p className="text-sm text-fg-muted">{t('providers.noCapabilities')}</p>;
  return (
    <div className="flex flex-col gap-3">
      {withCaps.map((i) => (
        <details key={i.installation_id} className="rounded-md border border-border">
          <summary className="min-h-touch cursor-pointer px-3 py-2 text-sm font-medium">{t('providers.capabilitiesOf', { host: i.host })}</summary>
          <div className="px-3 pb-3">
            <CodeBlock code={JSON.stringify(i.capabilities, null, 2)} label={t('providers.capabilitiesLabel', { host: i.host })} language="json" />
          </div>
        </details>
      ))}
    </div>
  );
}

function SettingsView({ settings }: { settings: ProviderSettings }) {
  const { t } = useTranslation();
  return (
    <dl className="grid grid-cols-[auto,1fr] gap-x-4 gap-y-1 text-sm">
      <dt className="text-fg-muted">{t('providers.settings.enabled')}</dt>
      <dd>{settings.enabled ? t('providers.yes') : t('providers.no')}</dd>
      <dt className="text-fg-muted">{t('providers.settings.maxParallel')}</dt>
      <dd>{settings.max_parallel_sessions}</dd>
      <dt className="text-fg-muted">{t('providers.settings.timeout')}</dt>
      <dd>{t('providers.settings.seconds', { count: settings.default_timeout_seconds })}</dd>
      <dt className="text-fg-muted">{t('providers.settings.roles')}</dt>
      <dd>
        {settings.allowed_roles.length > 0
          ? settings.allowed_roles.map((r) => t(`enums.agent_role.${r}`, { defaultValue: r })).join(', ')
          : t('providers.settings.noRoles')}
      </dd>
    </dl>
  );
}

function SettingsForm({ workspaceId, provider }: { workspaceId: string; provider: Provider }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const toast = useToast();
  const uid = useId();
  const formRef = useRef<HTMLFormElement>(null);
  // База для If-Match и сравнения: ответ PATCH сразу становится новой версией (до перечитывания списка).
  const [settings, setSettings] = useState<ProviderSettings>(provider.workspace_settings);
  const [draft, setDraft] = useState<SettingsDraft>(() => draftFrom(provider.workspace_settings));
  const incoming = provider.workspace_settings;
  if (incoming.settings_version > settings.settings_version) {
    setSettings(incoming);
    setDraft(draftFrom(incoming));
  }
  const [errors, setErrors] = useState<Partial<Record<SettingsField, string>>>({});
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const [info, setInfo] = useState<string | null>(null);

  // Политика провайдера в workspace — step-up (SC-IAM-08): при 403 step_up_required — диалог пароля и повтор.
  const stepUp = useStepUp();
  const save = useMutation({
    mutationFn: (patch: ProviderSettingsUpdateRequest) =>
      stepUp.run(() => providersApi.updateSettings(workspaceId, provider.provider_id, patch, settings.settings_version)),
    onSuccess: async (updated) => {
      setSettings(updated);
      setDraft(draftFrom(updated));
      setInfo(t('providers.settings.saved'));
      toast({ title: t('providers.settings.saved'), tone: 'success' });
      await queryClient.invalidateQueries({ queryKey: providerKeys.list(workspaceId) });
    },
    onError: (error) => {
      const mapped = mapProblemToFields(t, error, ['enabled', 'max_parallel_sessions', 'default_timeout_seconds', 'allowed_roles'] as const);
      setErrors(mapped.fields);
      setFormErrors(mapped.form);
    },
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    setInfo(null);
    setFormErrors([]);
    const { errors: v, patch } = buildSettingsPatch(settings, draft);
    const messages: Partial<Record<SettingsField, string>> = {};
    if (v.max_parallel_sessions) messages.max_parallel_sessions = t('providers.settings.rangeError', MAX_PARALLEL);
    if (v.default_timeout_seconds) messages.default_timeout_seconds = t('providers.settings.rangeError', TIMEOUT_SECONDS);
    setErrors(messages);
    if (Object.keys(messages).length > 0) {
      requestAnimationFrame(() => formRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus());
      return;
    }
    if (!patch) {
      setInfo(t('providers.settings.noChanges'));
      return;
    }
    save.mutate(patch);
  };

  const rolesLegend = `${uid}-roles`;
  return (
    <form ref={formRef} noValidate onSubmit={onSubmit} className="flex flex-col gap-4" aria-label={t('providers.settings.formLabel', { name: provider.display_name })}>
      <FormAlert messages={formErrors} />
      <div className="flex items-center gap-2">
        <input
          id={`${uid}-enabled`}
          type="checkbox"
          className="h-4 w-4 accent-[rgb(var(--color-accent))]"
          checked={draft.enabled}
          onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })}
        />
        <label htmlFor={`${uid}-enabled`} className="text-sm">
          {t('providers.settings.enabledLabel')}
        </label>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <TextField
          id={`${uid}-parallel`}
          label={t('providers.settings.maxParallel')}
          hint={t('providers.settings.rangeHint', MAX_PARALLEL)}
          inputMode="numeric"
          value={draft.maxParallel}
          onChange={(e) => setDraft({ ...draft, maxParallel: e.target.value })}
          error={errors.max_parallel_sessions}
        />
        <TextField
          id={`${uid}-timeout`}
          label={t('providers.settings.timeoutSeconds')}
          hint={t('providers.settings.rangeHint', TIMEOUT_SECONDS)}
          inputMode="numeric"
          value={draft.timeout}
          onChange={(e) => setDraft({ ...draft, timeout: e.target.value })}
          error={errors.default_timeout_seconds}
        />
      </div>
      <fieldset aria-describedby={errors.allowed_roles ? `${rolesLegend}-error` : undefined}>
        <legend className="mb-2 text-sm font-medium">{t('providers.settings.roles')}</legend>
        <div className="grid gap-2 sm:grid-cols-3">
          {AGENT_ROLES.map((role) => {
            const id = `${uid}-role-${role}`;
            return (
              <div key={role} className="flex items-center gap-2">
                <input
                  id={id}
                  type="checkbox"
                  className="h-4 w-4 accent-[rgb(var(--color-accent))]"
                  checked={draft.roles.includes(role)}
                  onChange={(e) =>
                    setDraft({ ...draft, roles: e.target.checked ? [...draft.roles, role] : draft.roles.filter((r) => r !== role) })
                  }
                />
                <label htmlFor={id} className="text-sm">
                  {t(`enums.agent_role.${role}`, { defaultValue: role })}
                </label>
              </div>
            );
          })}
        </div>
        {errors.allowed_roles ? (
          <p id={`${rolesLegend}-error`} className="mt-1 text-sm font-medium text-danger-text">
            {errors.allowed_roles}
          </p>
        ) : null}
      </fieldset>
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" variant="primary" loading={save.isPending}>
          {t('providers.settings.save')}
        </Button>
        <p role="status" className="text-sm text-fg-muted">
          {info}
        </p>
      </div>
      {stepUp.dialog}
    </form>
  );
}

/** Интервал опроса статуса запроса проверки (ProbeAccepted.status_url), пока он PENDING. */
export const PROBE_POLL_MS = 2_000;
const PROBE_FINAL: readonly ProviderProbeStatus['status'][] = ['COMPLETED', 'FAILED', 'EXPIRED'];

function ProbeAction({ workspaceId, provider }: { workspaceId: string; provider: Provider }) {
  const { t } = useTranslation();
  const toast = useToast();
  const queryClient = useQueryClient();
  const intent = useRef(new IdempotentIntent());
  const [error, setError] = useState<string | null>(null);
  const [requestId, setRequestId] = useState<string | null>(null);
  const probe = useMutation({
    mutationFn: () => providersApi.probe(workspaceId, provider.provider_id, intent.current.keyFor({ provider: provider.provider_id })),
    onSuccess: (accepted) => {
      intent.current.complete();
      setError(null);
      setRequestId(accepted.probe_request_id);
      toast({ title: t('providers.probeAccepted', { name: provider.display_name }), tone: 'info' });
    },
    onError: (e) => setError(problemMessage(t, e)),
  });
  // OQ-45 (c): статус запроса опрашивается до финального состояния; затем перечитывается список установок.
  const status = useQuery({
    queryKey: providerKeys.probe(workspaceId, provider.provider_id, requestId ?? ''),
    queryFn: ({ signal }) => providersApi.probeStatus(workspaceId, provider.provider_id, requestId ?? '', signal),
    enabled: requestId !== null,
    refetchInterval: (q) => (q.state.data && PROBE_FINAL.includes(q.state.data.status) ? false : PROBE_POLL_MS),
  });
  const final = status.data && PROBE_FINAL.includes(status.data.status) ? status.data.status : null;
  useEffect(() => {
    if (final) void queryClient.invalidateQueries({ queryKey: providerKeys.list(workspaceId) });
  }, [final, queryClient, workspaceId]);

  let text: string | null = error;
  let tone: 'ok' | 'error' = error ? 'error' : 'ok';
  if (!error && requestId) {
    const s = status.data;
    if (status.isError) {
      text = problemMessage(t, status.error);
      tone = 'error';
    } else if (!s || s.status === 'PENDING') text = t('providers.probeStatus.PENDING', { name: provider.display_name });
    else if (s.status === 'COMPLETED')
      text = t('providers.probeStatus.COMPLETED', {
        host: s.installation?.host ?? '—',
        auth: t(`providers.enums.auth_status.${s.installation?.auth_status ?? 'UNKNOWN'}`),
      });
    else {
      tone = 'error';
      text = t(`providers.probeStatus.${s.status}`, {
        error: s.error_category ? t(`providers.enums.cli_error.${s.error_category}`, { defaultValue: s.error_category }) : '—',
      });
    }
  }
  return (
    <div className="flex flex-col items-start gap-1">
      <Button
        onClick={() => probe.mutate()}
        loading={probe.isPending || (requestId !== null && !final && !status.isError)}
        aria-describedby={`probe-hint-${provider.provider_id}`}
      >
        {t('providers.probe')}
      </Button>
      <p id={`probe-hint-${provider.provider_id}`} className="text-xs text-fg-muted">
        {t('providers.probeHint')}
      </p>
      <p
        role="status"
        data-probe-status={status.data?.status ?? (requestId ? 'PENDING' : undefined)}
        className={tone === 'error' ? 'text-sm font-medium text-danger-text' : 'text-sm text-fg-muted'}
      >
        {text}
      </p>
    </div>
  );
}

function SectionTitle({ id, icon, children }: { id: string; icon: IconName; children: ReactNode }) {
  return (
    <h3 id={id} className="mb-2 flex items-center gap-2 font-semibold text-fg">
      <Icon name={icon} className="h-4 w-4 text-fg-muted" />
      {children}
    </h3>
  );
}

function ProviderCard({
  workspaceId,
  provider,
  role,
  analytics,
  window,
}: {
  workspaceId: string;
  provider: Provider;
  role: string;
  analytics: UseQueryResult<AnalyticsSummary>;
  window: { from: string; to: string };
}) {
  const { t } = useTranslation();
  const needsLogin = provider.installations.filter((i) => NEEDS_LOGIN.includes(i.auth_status));
  const untested = provider.installations.filter((i) => i.compatibility === 'UNTESTED');
  const unsupported = provider.installations.filter((i) => i.compatibility === 'UNSUPPORTED');
  const chip = providerChipModel(provider);
  const canProbe = can(role, 'provider:probe');
  const canEdit = can(role, 'provider:settings');
  const sid = `provider-${provider.code}`;
  return (
    <li>
      <Card
        as="article"
        id={sid}
        title={provider.display_name}
        actions={canProbe ? <ProbeAction workspaceId={workspaceId} provider={provider} /> : undefined}
      >
        <div className="mb-4 flex flex-wrap items-center gap-2" data-testid={`provider-state-${provider.code}`} data-state={chip.state}>
          <Badge tone={CHIP_STATE_TONE[chip.state]} icon={CHIP_STATE_ICON[chip.state]}>
            {t('providers.status.label')}: {t(`shell.cli.state.${chip.state}`)}
          </Badge>
          <Badge tone="neutral">{provider.code}</Badge>
          <Badge tone={provider.enabled_globally ? 'success' : 'danger'}>
            {provider.enabled_globally ? t('providers.enabledGlobally') : t('providers.disabledGlobally')}
          </Badge>
          <Badge tone={provider.workspace_settings.enabled ? 'success' : 'neutral'}>
            {provider.workspace_settings.enabled ? t('providers.enabledInWorkspace') : t('providers.disabledInWorkspace')}
          </Badge>
          <span className="text-xs text-fg-muted">
            {t('providers.status.checked')}: {chip.checkedAt ? <RelativeTime value={chip.checkedAt} /> : t('providers.neverChecked')}
          </span>
        </div>
        {needsLogin.length > 0 ? (
          <Callout tone="warning" title={t('providers.loginRequiredTitle')} className="mb-4">
            <p>{t('providers.loginRequired', { hosts: needsLogin.map((i) => i.host).join(', ') })}</p>
            <p className="mt-1">{t('providers.noSecrets')}</p>
          </Callout>
        ) : null}
        {unsupported.length > 0 ? (
          <Callout tone="danger" title={t('providers.unsupportedTitle')} className="mb-4">
            {t('providers.unsupported', { hosts: unsupported.map((i) => `${i.host} (${i.detected_version ?? '—'})`).join(', ') })}
          </Callout>
        ) : null}
        {untested.length > 0 ? (
          <Callout tone="warning" title={t('providers.untestedTitle')} className="mb-4">
            {t('providers.untested', { hosts: untested.map((i) => `${i.host} (${i.detected_version ?? '—'})`).join(', ') })}
          </Callout>
        ) : null}
        <div className="grid min-w-0 grid-cols-1 gap-6 2xl:grid-cols-2">
          <div className="flex min-w-0 flex-col gap-6">
            <section aria-labelledby={`${sid}-overview`} className="min-w-0">
              <SectionTitle id={`${sid}-overview`} icon="terminal">
                {t('providers.sections.overview')}
              </SectionTitle>
              <InstallationsTable provider={provider} />
              <h4 className="mb-2 mt-4 text-sm font-semibold text-fg">{t('providers.capabilities')}</h4>
              <Capabilities provider={provider} />
            </section>
            <section aria-labelledby={`${sid}-routing`} className="min-w-0">
              <SectionTitle id={`${sid}-routing`} icon="settings">
                {t('providers.sections.routing')}
              </SectionTitle>
              {canEdit ? <SettingsForm workspaceId={workspaceId} provider={provider} /> : <SettingsView settings={provider.workspace_settings} />}
            </section>
          </div>
          <div className="flex min-w-0 flex-col gap-6">
            <section aria-labelledby={`${sid}-usage`} className="min-w-0">
              <SectionTitle id={`${sid}-usage`} icon="analytics">
                {t('providers.sections.usage')}
              </SectionTitle>
              <ProviderUsage provider={provider} analytics={analytics} window={window} />
            </section>
            <section aria-labelledby={`${sid}-history`} className="min-w-0">
              <SectionTitle id={`${sid}-history`} icon="history">
                {t('providers.sections.history')}
              </SectionTitle>
              <ExecutionHistory workspaceId={workspaceId} provider={provider} />
            </section>
          </div>
        </div>
      </Card>
    </li>
  );
}

/**
 * Настройки › CLI-провайдеры (P7-09 → F5, план §3.9): по каждому провайдеру — установки (версия,
 * совместимость с предупреждением UNTESTED, авторизация — только статус и подсказка официального входа
 * CLI, доступность, последняя ошибка, «проверено N назад»), перепроверка, измеренное использование
 * (analytics; токены — только сообщённые CLI, без квот), история исполнений (B1, виртуализация),
 * маршрутизация (безопасные настройки с step-up).
 */
export function ProvidersPage() {
  const { t } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();
  const query = useQuery({
    queryKey: providerKeys.list(workspaceId ?? ''),
    queryFn: ({ signal }) => providersApi.list(workspaceId ?? '', signal),
    enabled: Boolean(workspaceId && membership),
  });
  const window = useMemo(() => dashboardWindow(), []);
  const analytics = useQuery({
    queryKey: analyticsKeys.summary(workspaceId ?? '', window),
    queryFn: ({ signal }) => analyticsApi.summary(workspaceId ?? '', window, signal),
    enabled: Boolean(workspaceId && membership),
    staleTime: 60_000,
  });
  if (!workspaceId || !membership) return <WorkspaceNotFound />;

  let body;
  if (query.isPending) body = <LoadingState label={t('providers.loading')} />;
  else if (query.isError) body = <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  else if (query.data.items.length === 0) body = <EmptyState icon="terminal" title={t('providers.emptyTitle')} description={t('providers.emptyDescription')} />;
  else {
    body = (
      <ul className="flex flex-col gap-6" aria-label={t('providers.listLabel')}>
        {query.data.items.map((p) => (
          <ProviderCard
            key={`${workspaceId}-${p.provider_id}`}
            workspaceId={workspaceId}
            provider={p}
            role={membership.role}
            analytics={analytics}
            window={window}
          />
        ))}
      </ul>
    );
  }
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('providers.title')}>
        <Button
          icon="refresh"
          onClick={() => {
            void query.refetch();
            void analytics.refetch();
          }}
          loading={query.isFetching && !query.isPending}
        >
          {t('providers.refresh')}
        </Button>
      </PageHeading>
      <Callout tone="info" className="mb-6">
        {t('providers.intro')}
      </Callout>
      {body}
    </section>
  );
}

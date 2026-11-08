import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useId, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { IdempotentIntent } from '../../api/client';
import {
  providerKeys,
  providersApi,
  type AuthStatus,
  type Provider,
  type ProviderAvailability,
  type ProviderInstallation,
  type ProviderSettings,
  type ProviderSettingsUpdateRequest,
} from '../../api/providers';
import { FormAlert, TextField } from '../../components/form';
import { PageHeading } from '../../components/PageHeading';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import { Badge, Button, Callout, Card, CodeBlock, Table, Timestamp, useToast, type Column, type Tone } from '../../components/ui';
import { can } from '../../lib/permissions';
import { mapProblemToFields, problemMessage } from '../../lib/problem';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { AGENT_ROLES, MAX_PARALLEL, TIMEOUT_SECONDS, buildSettingsPatch, draftFrom, type SettingsDraft, type SettingsField } from './settingsForm';

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

  const save = useMutation({
    mutationFn: (patch: ProviderSettingsUpdateRequest) =>
      providersApi.updateSettings(workspaceId, provider.provider_id, patch, settings.settings_version),
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
    </form>
  );
}

function ProbeAction({ workspaceId, provider }: { workspaceId: string; provider: Provider }) {
  const { t } = useTranslation();
  const toast = useToast();
  const queryClient = useQueryClient();
  const intent = useRef(new IdempotentIntent());
  const [status, setStatus] = useState<{ tone: 'ok' | 'error'; text: string } | null>(null);
  const probe = useMutation({
    mutationFn: () => providersApi.probe(workspaceId, provider.provider_id, intent.current.keyFor({ provider: provider.provider_id })),
    onSuccess: async () => {
      intent.current.complete();
      const text = t('providers.probeAccepted', { name: provider.display_name });
      setStatus({ tone: 'ok', text });
      toast({ title: text, tone: 'info' });
      await queryClient.invalidateQueries({ queryKey: providerKeys.list(workspaceId) });
    },
    onError: (error) => setStatus({ tone: 'error', text: problemMessage(t, error) }),
  });
  return (
    <div className="flex flex-col items-start gap-1">
      <Button onClick={() => probe.mutate()} loading={probe.isPending} aria-describedby={`probe-hint-${provider.provider_id}`}>
        {t('providers.probe')}
      </Button>
      <p id={`probe-hint-${provider.provider_id}`} className="text-xs text-fg-muted">
        {t('providers.probeHint')}
      </p>
      <p role="status" className={status?.tone === 'error' ? 'text-sm font-medium text-danger-text' : 'text-sm text-fg-muted'}>
        {status?.text}
      </p>
    </div>
  );
}

function ProviderCard({ workspaceId, provider, role }: { workspaceId: string; provider: Provider; role: string }) {
  const { t } = useTranslation();
  const needsLogin = provider.installations.filter((i) => NEEDS_LOGIN.includes(i.auth_status));
  const canProbe = can(role, 'provider:probe');
  const canEdit = can(role, 'provider:settings');
  return (
    <li>
      <Card
        as="article"
        id={`provider-${provider.code}`}
        title={provider.display_name}
        actions={canProbe ? <ProbeAction workspaceId={workspaceId} provider={provider} /> : undefined}
      >
        <div className="mb-4 flex flex-wrap gap-2">
          <Badge tone="neutral">{provider.code}</Badge>
          <Badge tone={provider.enabled_globally ? 'success' : 'danger'}>
            {provider.enabled_globally ? t('providers.enabledGlobally') : t('providers.disabledGlobally')}
          </Badge>
          <Badge tone={provider.workspace_settings.enabled ? 'success' : 'neutral'}>
            {provider.workspace_settings.enabled ? t('providers.enabledInWorkspace') : t('providers.disabledInWorkspace')}
          </Badge>
        </div>
        {needsLogin.length > 0 ? (
          <Callout tone="warning" title={t('providers.loginRequiredTitle')} className="mb-4">
            <p>{t('providers.loginRequired', { hosts: needsLogin.map((i) => i.host).join(', ') })}</p>
            <p className="mt-1">{t('providers.noSecrets')}</p>
          </Callout>
        ) : null}
        <h3 className="mb-2 font-semibold">{t('providers.installations')}</h3>
        <InstallationsTable provider={provider} />
        <h3 className="mb-2 mt-4 font-semibold">{t('providers.capabilities')}</h3>
        <Capabilities provider={provider} />
        <h3 className="mb-2 mt-4 font-semibold">{t('providers.settings.title')}</h3>
        {canEdit ? <SettingsForm workspaceId={workspaceId} provider={provider} /> : <SettingsView settings={provider.workspace_settings} />}
      </Card>
    </li>
  );
}

/** P7-09 CLI Providers: health, авторизация (только статус), capabilities, probe, безопасные настройки. */
export function ProvidersPage() {
  const { t } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();
  const query = useQuery({
    queryKey: providerKeys.list(workspaceId ?? ''),
    queryFn: ({ signal }) => providersApi.list(workspaceId ?? '', signal),
    enabled: Boolean(workspaceId && membership),
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
          <ProviderCard key={`${workspaceId}-${p.provider_id}`} workspaceId={workspaceId} provider={p} role={membership.role} />
        ))}
      </ul>
    );
  }
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('providers.title')}>
        <Button onClick={() => void query.refetch()} loading={query.isFetching && !query.isPending}>
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

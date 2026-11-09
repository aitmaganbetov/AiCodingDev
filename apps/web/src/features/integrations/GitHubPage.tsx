/**
 * Интеграции → GitHub App (P8-02, OQ-47 (2)).
 *
 * 1. «Подключить GitHub App» (github:manage): `startGitHubInstallation` выдаёт state (HMAC, привязан к пользователю
 *    и workspace, однократный) и URL установки; workspace запоминается в sessionStorage, браузер уходит на GitHub.
 * 2. GitHub возвращает на Setup URL App (`/integrations/github/setup?installation_id&state&setup_action`, вне
 *    workspace) — `GitHubSetupRedirect` переводит на эту страницу workspace с теми же параметрами.
 * 3. Страница один раз вызывает `connectGitHubInstallation` (201, установка PENDING + verification_id), убирает
 *    параметры из адреса и показывает проверку воркером (PENDING → SUCCEEDED / FAILED, установка ACTIVE / REVOKED).
 * 4. Список установок и отключение (DELETE, github:manage).
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { IdempotentIntent, isApiError } from '../../api/client';
import { githubApi, githubKeys, type GitHubInstallation } from '../../api/github';
import { useSession } from '../../auth/session';
import { FormAlert } from '../../components/form';
import { PageHeading } from '../../components/PageHeading';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import {
  Badge,
  Button,
  Callout,
  Card,
  Dialog,
  DialogClose,
  DialogContent,
  DialogTrigger,
  Table,
  Timestamp,
  type Column,
  type Tone,
} from '../../components/ui';
import { can } from '../../lib/permissions';
import { problemMessage } from '../../lib/problem';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { VerificationStatus } from './VerificationStatus';

/** sessionStorage: workspace незавершённой установки по state (Setup URL App не содержит workspace). */
export const SETUP_STORAGE_KEY = 'aicd.github.setup';

export function rememberSetup(workspaceId: string, state: string): void {
  try {
    window.sessionStorage.setItem(SETUP_STORAGE_KEY, JSON.stringify({ workspaceId, state }));
  } catch {
    /* хранилище недоступно — возврат определит workspace по единственному членству */
  }
}

export function recallSetup(state: string): string | null {
  try {
    const raw = window.sessionStorage.getItem(SETUP_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { workspaceId?: unknown; state?: unknown };
    return parsed.state === state && typeof parsed.workspaceId === 'string' ? parsed.workspaceId : null;
  } catch {
    return null;
  }
}

const STATUS_TONE: Record<GitHubInstallation['status'], Tone> = {
  PENDING: 'info',
  ACTIVE: 'success',
  SUSPENDED: 'warning',
  REVOKED: 'neutral',
};

function accountLabel(t: (k: string) => string, inst: GitHubInstallation): string {
  return inst.status === 'PENDING' ? t('integrations.github.pendingAccount') : inst.account_login;
}

function DisconnectDialog({ workspaceId, inst, onResult }: { workspaceId: string; inst: GitHubInstallation; onResult: (m: string) => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const name = `${accountLabel(t, inst)} (${inst.installation_id})`;
  const disconnect = useMutation({
    mutationFn: () => githubApi.disconnectInstallation(workspaceId, inst.id),
    onSuccess: async () => {
      setOpen(false);
      onResult(t('integrations.github.disconnected', { name }));
      await queryClient.invalidateQueries({ queryKey: githubKeys.installations(workspaceId) });
    },
    onError: (error) => setErrors([problemMessage(t, error)]),
  });
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) setErrors([]);
      }}
    >
      <DialogTrigger asChild>
        <Button size="sm" variant="danger" aria-label={t('integrations.github.disconnectOf', { name })}>
          {t('integrations.github.disconnect')}
        </Button>
      </DialogTrigger>
      <DialogContent
        title={t('integrations.github.disconnectTitle', { name })}
        description={t('integrations.github.disconnectDescription')}
        footer={
          <>
            <DialogClose asChild>
              <Button>{t('action.cancel')}</Button>
            </DialogClose>
            <Button variant="danger" loading={disconnect.isPending} onClick={() => disconnect.mutate()}>
              {t('integrations.github.disconnectConfirm')}
            </Button>
          </>
        }
      >
        <FormAlert messages={errors} />
      </DialogContent>
    </Dialog>
  );
}

/** Возврат с GitHub: один вызов connect на пару (installation_id, state), затем статус проверки. */
function useSetupCallback(workspaceId: string, canManage: boolean) {
  const [params, setParams] = useSearchParams();
  const queryClient = useQueryClient();
  const intent = useRef(new IdempotentIntent());
  const started = useRef(false);
  // Параметры возврата с GitHub фиксируются при открытии страницы (затем убираются из адреса).
  const [callback] = useState(() => ({
    present: params.has('installation_id') || params.has('state'),
    installationId: Number(params.get('installation_id')),
    state: params.get('state') ?? '',
    setupAction: params.get('setup_action'),
  }));
  const valid = callback.present && canManage && Boolean(callback.state) && Number.isInteger(callback.installationId) && callback.installationId >= 1;

  const connect = useMutation({
    mutationFn: (body: { installation_id: number; state: string; setup_action?: 'install' | 'update' }) =>
      githubApi.connectInstallation(workspaceId, body, intent.current.keyFor(body)),
    onSuccess: async () => {
      intent.current.complete();
      await queryClient.invalidateQueries({ queryKey: githubKeys.installations(workspaceId) });
    },
  });
  const { mutate } = connect;

  useEffect(() => {
    if (!callback.present || started.current) return;
    started.current = true;
    // Параметры возврата одноразовые: обновление страницы не повторяет подключение.
    setParams({}, { replace: true });
    if (!valid) return;
    const { setupAction } = callback;
    mutate({
      installation_id: callback.installationId,
      state: callback.state,
      ...(setupAction === 'install' || setupAction === 'update' ? { setup_action: setupAction } : {}),
    });
  }, [callback, valid, setParams, mutate]);

  return {
    connecting: connect.isPending,
    verificationId: connect.data?.verification_id ?? null,
    registered: connect.data?.installation_id ?? null,
    error: connect.error,
    invalid: callback.present && !valid,
  };
}

export function GitHubPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { workspaceId, membership } = useCurrentWorkspace();
  const canManage = can(membership?.role, 'github:manage');
  const [notice, setNotice] = useState<string | null>(null);
  const callback = useSetupCallback(workspaceId ?? '', canManage);
  const query = useQuery({
    queryKey: githubKeys.installations(workspaceId ?? ''),
    queryFn: ({ signal }) => githubApi.installations(workspaceId ?? '', signal),
    enabled: Boolean(workspaceId && membership),
  });
  const start = useMutation({
    mutationFn: () => githubApi.startInstallation(workspaceId ?? ''),
    onSuccess: (setup) => {
      rememberSetup(workspaceId ?? '', setup.state);
      window.location.assign(setup.install_url);
    },
  });
  const onVerified = useCallback(
    () => void queryClient.invalidateQueries({ queryKey: githubKeys.installations(workspaceId ?? '') }),
    [queryClient, workspaceId],
  );

  if (!workspaceId || !membership) return <WorkspaceNotFound />;

  const columns: Column<GitHubInstallation>[] = [
    {
      id: 'account',
      header: t('integrations.github.account'),
      rowHeader: true,
      cell: (i) => <span className={i.status === 'PENDING' ? 'text-fg-muted' : 'font-medium'}>{accountLabel(t, i)}</span>,
      sortValue: (i) => i.account_login,
    },
    { id: 'id', header: t('integrations.github.installationId'), cell: (i) => <span className="font-mono">{i.installation_id}</span> },
    {
      id: 'selection',
      header: t('integrations.github.selection'),
      cell: (i) => t(`integrations.github.enums.selection.${i.repository_selection}`, { defaultValue: i.repository_selection }),
    },
    {
      id: 'status',
      header: t('integrations.github.status'),
      cell: (i) => <Badge tone={STATUS_TONE[i.status] ?? 'neutral'}>{t(`integrations.github.enums.status.${i.status}`, { defaultValue: i.status })}</Badge>,
      sortValue: (i) => i.status,
    },
    { id: 'created', header: t('integrations.github.connectedAt'), cell: (i) => <Timestamp value={i.created_at} />, sortValue: (i) => i.created_at },
  ];
  if (canManage) {
    columns.push({
      id: 'actions',
      header: t('integrations.github.actions'),
      cell: (i) => (i.status === 'REVOKED' ? null : <DisconnectDialog workspaceId={workspaceId} inst={i} onResult={setNotice} />),
    });
  }

  let body;
  if (query.isPending) body = <LoadingState label={t('integrations.github.loading')} />;
  else if (query.isError) body = <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  else if (query.data.items.length === 0) {
    body = (
      <EmptyState
        headingLevel={3}
        icon="layers"
        title={t('integrations.github.emptyTitle')}
        description={canManage ? t('integrations.github.emptyDescription') : t('integrations.github.emptyDescriptionReadOnly')}
      />
    );
  } else {
    body = <Table caption={t('integrations.github.listLabel')} columns={columns} rows={query.data.items} rowKey={(i) => i.id} />;
  }

  const startError = start.error;
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-6">
      <PageHeading title={t('integrations.title')} />
      <Card as="section" id="github-app" title={t('integrations.github.title')}>
        <div className="flex flex-col gap-4">
          <p className="text-sm text-fg-muted">{t('integrations.github.description')}</p>
          {canManage ? (
            <div>
              <Button variant="primary" icon="layers" loading={start.isPending} onClick={() => start.mutate()}>
                {start.isPending ? t('integrations.github.connecting') : t('integrations.github.connect')}
              </Button>
            </div>
          ) : (
            <Callout tone="neutral">{t('integrations.github.manageOnly')}</Callout>
          )}
          {startError ? (
            <FormAlert
              messages={[isApiError(startError) && startError.status === 503 ? t('integrations.github.notConfigured') : problemMessage(t, startError)]}
            />
          ) : null}
          {callback.connecting ? <LoadingState inline label={t('integrations.github.callbackConnecting')} /> : null}
          {callback.invalid ? <FormAlert messages={[t('integrations.github.callbackInvalid')]} /> : null}
          {callback.error ? <FormAlert messages={[problemMessage(t, callback.error)]} /> : null}
          {callback.registered !== null ? (
            <p className="text-sm">{t('integrations.github.callbackRegistered', { id: callback.registered })}</p>
          ) : null}
          {callback.verificationId ? (
            <VerificationStatus workspaceId={workspaceId} verificationId={callback.verificationId} onSettled={onVerified} />
          ) : null}
          {notice ? (
            <p role="status" className="rounded-md border border-success-border bg-success-soft p-3 text-sm text-success-fg">
              {notice}
            </p>
          ) : null}
          {body}
        </div>
      </Card>
    </section>
  );
}

/** Setup URL GitHub App (вне workspace): переход на страницу интеграций нужного workspace с параметрами возврата. */
export function GitHubSetupRedirect() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const { data: session } = useSession();
  const state = params.get('state') ?? '';
  const workspaces = session?.workspaces ?? [];
  const remembered = recallSetup(state);
  const target =
    remembered && workspaces.some((w) => w.workspace_id === remembered)
      ? remembered
      : workspaces.length === 1
        ? (workspaces[0]?.workspace_id ?? null)
        : null;
  useEffect(() => {
    if (target) navigate(`/w/${target}/integrations?${params.toString()}`, { replace: true });
  }, [target, params, navigate]);
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-4">
      <PageHeading title={t('integrations.github.setupTitle')} />
      {target || session === undefined ? (
        <LoadingState label={t('integrations.github.setupRedirecting')} />
      ) : (
        <Callout tone="warning">
          {t('integrations.github.setupNoWorkspace')}{' '}
          <Link className="text-link underline" to="/workspaces">
            {t('nav.workspaces')}
          </Link>
        </Callout>
      )}
    </section>
  );
}

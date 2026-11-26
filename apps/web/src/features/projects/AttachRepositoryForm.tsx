/**
 * Привязка репозитория к проекту (FR-PRJ-02, FR-PRJ-03).
 *
 * Форма показывает, что разрешено, вместо того чтобы просить угадать:
 *  * локальный каталог — выбор из репозиториев, найденных в allowlist-корнях сервера (`listRepositoryRoots`);
 *  * GitHub — выбор из репозиториев, доступных установке GitHub App (`listGitHubInstallationRepositories`).
 *
 * Список репозиториев установки читает воркер (ключ App есть только у него, OQ-47 (2)) и кладёт в кэш:
 * пустой или устаревший кэш — не ошибка, форма предлагает «Обновить» и ждёт нового `synced_at`.
 * Ручной ввод остаётся всегда: списки — подсказка, а не ограничение. Сервер в любом случае проверяет
 * путь в корнях и доступ установки к репозиторию заново.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { IdempotentIntent } from '../../api/client';
import { githubApi, githubKeys } from '../../api/github';
import { projectKeys, projectsApi, type RepositoryAttachRequest, type RepositoryProvider } from '../../api/projects';
import { FormAlert, RadioGroup, SelectField, TextField, focusFirstInvalid } from '../../components/form';
import { Button, Callout, Timestamp } from '../../components/ui';
import { mapProblemToFields, problemMessage } from '../../lib/problem';
import { useCurrentWorkspace } from '../../workspace';

type Field = 'provider' | 'remote_url' | 'local_path' | 'default_branch';
const FIELDS = ['provider', 'remote_url', 'local_path', 'default_branch'] as const;
const PROVIDERS = ['local', 'github'] as const satisfies readonly RepositoryProvider[];
/** RepositoryAttachRequest: remote_url ≤ 2048, local_path ≤ 4096, default_branch ≤ 255. */
export const REMOTE_URL_MAX = 2048;
export const LOCAL_PATH_MAX = 4096;
export const BRANCH_MAX = 255;
export const GITHUB_URL_PATTERN = /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/?$/;
/** Значение пункта «ввести вручную» в списках выбора. Не может совпасть с ref каталога или full_name. */
const MANUAL = '\u0000manual';
/** Сколько раз перечитать список после «Обновить», прежде чем перестать ждать воркер. */
const MAX_SYNC_POLLS = 12;
const REFRESH_POLL_MS = 1_500;

/** Клиентская проверка формы привязки репозитория (сервер повторяет её и проверяет allowlist корней). */
export function validateAttach(
  input: { provider: RepositoryProvider; remoteUrl: string; localPath: string; branch: string },
  t: (key: string, opts?: Record<string, unknown>) => string,
): Partial<Record<Field, string>> {
  const errors: Partial<Record<Field, string>> = {};
  if (input.provider === 'github') {
    if (!input.remoteUrl) errors.remote_url = t('validation.required');
    else if (input.remoteUrl.length > REMOTE_URL_MAX) errors.remote_url = t('validation.maxLength', { max: REMOTE_URL_MAX });
    else if (!GITHUB_URL_PATTERN.test(input.remoteUrl)) errors.remote_url = t('projects.repo.remoteUrlInvalid');
  }
  // GitHub-репозиторий тоже требует локальную рабочую копию из allowlist-корней (OQ-47 (6)).
  if (!input.localPath) errors.local_path = t('validation.required');
  else if (input.localPath.length > LOCAL_PATH_MAX) errors.local_path = t('validation.maxLength', { max: LOCAL_PATH_MAX });
  if (input.branch.length > BRANCH_MAX) errors.default_branch = t('validation.maxLength', { max: BRANCH_MAX });
  return errors;
}

export function AttachRepositoryForm({
  projectId,
  onDone,
}: {
  projectId: string;
  /** `verificationId` — provider=github: доступ проверяет воркер (202 GitHubVerification). */
  onDone: (message: string, verificationId?: string) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { workspaceId } = useCurrentWorkspace();
  const formRef = useRef<HTMLFormElement>(null);
  const intent = useRef(new IdempotentIntent());
  const [provider, setProvider] = useState<RepositoryProvider>('local');
  const [remoteUrl, setRemoteUrl] = useState('');
  const [localPath, setLocalPath] = useState('');
  const [branch, setBranch] = useState('');
  const [manualRemote, setManualRemote] = useState(false);
  const [manualLocal, setManualLocal] = useState(false);
  /** Рабочей копии ещё нет: воркер склонирует репозиторий в указанный каталог (create_local_copy). */
  const [createCopy, setCreateCopy] = useState(false);
  const [installationId, setInstallationId] = useState('');
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<Field, string>>>({});
  const [formErrors, setFormErrors] = useState<string[]>([]);

  // --- что разрешено: корни сервера --------------------------------------------------------------
  const roots = useQuery({
    queryKey: projectKeys.repositoryRoots(workspaceId ?? ''),
    queryFn: ({ signal }) => projectsApi.repositoryRoots(workspaceId as string, signal),
    enabled: Boolean(workspaceId),
    staleTime: 60_000,
  });
  const rootEntries = (roots.data?.items ?? []).flatMap((root) => root.repositories);
  const rootsTruncated = (roots.data?.items ?? []).some((root) => root.truncated);

  // --- что разрешено: установки GitHub App и их состав --------------------------------------------
  const installations = useQuery({
    queryKey: githubKeys.installations(workspaceId ?? ''),
    queryFn: ({ signal }) => githubApi.installations(workspaceId as string, signal),
    enabled: Boolean(workspaceId) && provider === 'github',
    staleTime: 60_000,
  });
  const active = (installations.data?.items ?? []).filter((i) => i.status === 'ACTIVE');
  const selectedInstallation = active.find((i) => i.id === installationId) ?? active[0];
  const selectedInstallationId = selectedInstallation?.id ?? '';

  const repositoriesKey = githubKeys.installationRepositories(workspaceId ?? '', selectedInstallationId);
  // Чтение списка — отдельный запрос воркеру, поэтому после «Обновить» форма ждёт его результат опросом.
  // Счётчик удачных чтений на момент запроса ограничивает ожидание: воркер мог не ответить вовсе.
  const pollBaseline = useRef(0);
  const refresh = useMutation({
    mutationFn: () => githubApi.refreshRepositories(workspaceId as string, selectedInstallationId),
    onSuccess: () => {
      pollBaseline.current = queryClient.getQueryState(repositoriesKey)?.dataUpdateCount ?? 0;
    },
  });
  const repositories = useQuery({
    queryKey: repositoriesKey,
    queryFn: ({ signal }) => githubApi.installationRepositories(workspaceId as string, selectedInstallationId, signal),
    enabled: Boolean(workspaceId) && provider === 'github' && Boolean(selectedInstallationId),
    // Опрос идёт, пока сервер отдаёт устаревший список, и не дольше MAX_SYNC_POLLS чтений.
    refetchInterval: (query) =>
      refresh.isSuccess &&
      query.state.data?.stale !== false &&
      query.state.dataUpdateCount - pollBaseline.current < MAX_SYNC_POLLS
        ? REFRESH_POLL_MS
        : false,
  });
  const repoOptions = repositories.data?.items ?? [];
  // Ошибка чтения или запроса обновления: без неё форма молча показала бы пустое поле ввода.
  const listError = repositories.isError ? repositories.error : refresh.isError ? refresh.error : null;
  const waitingForSync = refresh.isPending || (refresh.isSuccess && repositories.data?.stale !== false && repositories.isFetching);

  // Кэша ещё нет (установка подключена давно, Redis очищен) — попросить список один раз сами,
  // чтобы оператор увидел выбор, а не пустое поле.
  const autoRefreshed = useRef<string | null>(null);
  useEffect(() => {
    if (provider !== 'github' || !selectedInstallationId || !repositories.isSuccess) return;
    if (repositories.data.synced_at !== null || autoRefreshed.current === selectedInstallationId) return;
    autoRefreshed.current = selectedInstallationId;
    refresh.mutate();
  }, [provider, selectedInstallationId, repositories.isSuccess, repositories.data?.synced_at, refresh]);

  const attach = useMutation({
    mutationFn: (body: RepositoryAttachRequest) => projectsApi.attachRepository(projectId, body, intent.current.keyFor(body)),
    onSuccess: async (result) => {
      intent.current.complete();
      await queryClient.invalidateQueries({ queryKey: projectKeys.repositories(projectId) });
      // 202 (github): репозиторий появится после проверки доступа воркером (GitHubVerification).
      if ('kind' in result) onDone(t('projects.repo.verificationQueued'), result.id);
      else onDone(t('projects.repo.attached'));
    },
    onError: (error) => {
      const mapped = mapProblemToFields(t, error, FIELDS);
      setFieldErrors(mapped.fields);
      setFormErrors(mapped.form);
      requestAnimationFrame(() => focusFirstInvalid(formRef.current));
    },
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const input = { provider, remoteUrl: remoteUrl.trim(), localPath: localPath.trim(), branch: branch.trim() };
    const errors = validateAttach(input, t);
    setFieldErrors(errors);
    setFormErrors([]);
    if (Object.keys(errors).length > 0) {
      requestAnimationFrame(() => focusFirstInvalid(formRef.current));
      return;
    }
    const body: RepositoryAttachRequest = {
      provider,
      ...(provider === 'github' ? { remote_url: input.remoteUrl, ...(createCopy ? { create_local_copy: true } : {}) } : {}),
      local_path: input.localPath,
      ...(input.branch ? { default_branch: input.branch } : {}),
    };
    attach.mutate(body);
  };

  const pickRepository = (value: string) => {
    if (value === MANUAL) {
      setManualRemote(true);
      setRemoteUrl('');
      return;
    }
    const option = repoOptions.find((r) => r.full_name === value);
    setRemoteUrl(option?.remote_url ?? '');
    // Ветка по умолчанию известна из GitHub — подставляем, пока оператор её не менял.
    if (option?.default_branch && !branch) setBranch(option.default_branch);
    if (createCopy && option) setLocalPath(clonePathFor(option.name));
  };

  /** Каталог для новой копии: имя репозитория в первом разрешённом корне — его и предлагаем. */
  function clonePathFor(repoName: string): string {
    const root = roots.data?.items[0]?.name;
    return root ? `${root}/${repoName}` : repoName;
  }

  const toggleCreateCopy = (next: boolean) => {
    setCreateCopy(next);
    const chosen = repoOptions.find((r) => r.remote_url === remoteUrl);
    // Путь к существующей копии и путь будущей копии — разные вещи: не переносим одно в другое.
    setLocalPath(next && chosen ? clonePathFor(chosen.name) : '');
  };

  const pickLocalPath = (value: string) => {
    if (value === MANUAL) {
      setManualLocal(true);
      setLocalPath('');
      return;
    }
    setLocalPath(value);
  };

  const showRepositoryPicker = provider === 'github' && !manualRemote && repoOptions.length > 0;
  const cloning = provider === 'github' && createCopy;
  const showLocalPicker = !cloning && !manualLocal && rootEntries.length > 0;
  const rootsSummary = (roots.data?.items ?? []).map((r) => `${r.name} (${r.path})`).join(', ');

  return (
    <form ref={formRef} noValidate onSubmit={onSubmit} className="flex flex-col gap-4" aria-label={t('projects.repo.attachTitle')}>
      <FormAlert messages={formErrors} />
      <RadioGroup
        name="repository-provider"
        legend={t('projects.repo.provider')}
        value={provider}
        onChange={setProvider}
        error={fieldErrors.provider}
        options={PROVIDERS.map((v) => ({
          value: v,
          label: t(`projects.enums.repository_provider.${v}`),
          description: t(`projects.repo.providerHint.${v}`),
        }))}
      />

      {provider === 'github' ? (
        <>
          {active.length === 0 && installations.isSuccess ? (
            <Callout tone="warning" title={t('projects.repo.noInstallationTitle')}>
              {t('projects.repo.noInstallation')}
            </Callout>
          ) : null}
          {/* Список не прочитан из-за ошибки — показать причину, иначе пустое поле выглядит как поломка формы. */}
          {listError ? (
            <Callout tone="danger" title={t('projects.repo.listError')}>
              {problemMessage(t, listError)} {t('projects.repo.pickerErrorFallback')}
            </Callout>
          ) : null}
          {active.length > 1 ? (
            <SelectField
              id="repo-installation"
              label={t('projects.repo.installation')}
              hint={t('projects.repo.installationHint')}
              value={selectedInstallationId}
              onChange={(e) => setInstallationId(e.target.value)}
              options={active.map((i) => ({ value: i.id, label: i.account_login }))}
            />
          ) : null}
          {showRepositoryPicker ? (
            <SelectField
              id="repo-remote-choice"
              label={t('projects.repo.remoteChoice')}
              hint={t('projects.repo.remoteChoiceHint', { count: repositories.data?.total ?? repoOptions.length })}
              placeholder={t('projects.repo.remoteChoicePlaceholder')}
              value={repoOptions.find((r) => r.remote_url === remoteUrl)?.full_name ?? ''}
              onChange={(e) => pickRepository(e.target.value)}
              error={fieldErrors.remote_url}
              options={[
                ...repoOptions.map((r) => ({
                  value: r.full_name,
                  // Уже привязанный репозиторий выбрать нельзя: привязка упала бы conflict'ом после проверки.
                  disabled: r.attached,
                  label: [
                    r.full_name,
                    r.private ? t('projects.repo.repoPrivate') : null,
                    r.archived ? t('projects.repo.repoArchived') : null,
                    r.attached ? t('projects.repo.alreadyAttached') : null,
                  ]
                    .filter(Boolean)
                    .join(' · '),
                })),
                { value: MANUAL, label: t('projects.repo.enterManually') },
              ]}
            />
          ) : (
            <TextField
              id="repo-remote-url"
              label={t('projects.repo.remoteUrl')}
              hint={t('projects.repo.remoteUrlHint')}
              inputMode="url"
              autoComplete="off"
              maxLength={REMOTE_URL_MAX}
              value={remoteUrl}
              onChange={(e) => setRemoteUrl(e.target.value)}
              error={fieldErrors.remote_url}
            />
          )}
          {selectedInstallationId ? (
            <p className="flex flex-wrap items-center gap-2 text-sm text-fg-muted">
              {repositories.data?.synced_at ? (
                <span>
                  {t('projects.repo.syncedAt')} <Timestamp value={repositories.data.synced_at} />
                  {repositories.data.truncated ? ` · ${t('projects.repo.listTruncated')}` : ''}
                </span>
              ) : (
                <span>{waitingForSync ? t('projects.repo.listLoading') : t('projects.repo.listEmpty')}</span>
              )}
              <Button
                type="button"
                variant="ghost"
                onClick={() => refresh.mutate()}
                loading={waitingForSync}
                disabled={!selectedInstallationId}
              >
                {t('projects.repo.refreshList')}
              </Button>
              {manualRemote && repoOptions.length > 0 ? (
                <Button type="button" variant="ghost" onClick={() => setManualRemote(false)}>
                  {t('projects.repo.backToList')}
                </Button>
              ) : null}
            </p>
          ) : null}
        </>
      ) : null}

      {provider === 'github' ? (
        <div className="flex items-start gap-2">
          <input
            type="checkbox"
            id="repo-create-copy"
            className="checkbox mt-1"
            checked={createCopy}
            onChange={(e) => toggleCreateCopy(e.target.checked)}
            aria-describedby="repo-create-copy-hint"
          />
          <div>
            <label htmlFor="repo-create-copy" className="text-sm text-fg">
              {t('projects.repo.createCopy')}
            </label>
            <p id="repo-create-copy-hint" className="text-sm text-fg-muted">
              {t('projects.repo.createCopyHint')}
            </p>
          </div>
        </div>
      ) : null}

      {cloning ? (
        <TextField
          id="repo-clone-path"
          label={t('projects.repo.clonePath')}
          hint={rootsSummary ? t('projects.repo.clonePathHint', { roots: rootsSummary }) : t('projects.repo.clonePathHintNoRoots')}
          autoComplete="off"
          maxLength={LOCAL_PATH_MAX}
          value={localPath}
          onChange={(e) => setLocalPath(e.target.value)}
          error={fieldErrors.local_path}
        />
      ) : showLocalPicker ? (
        <SelectField
          id="repo-local-choice"
          label={t('projects.repo.localPath')}
          hint={
            provider === 'github'
              ? t('projects.repo.localChoiceHintGithub', { roots: rootsSummary })
              : t('projects.repo.localChoiceHint', { roots: rootsSummary })
          }
          placeholder={t('projects.repo.localChoicePlaceholder')}
          value={rootEntries.some((entry) => entry.ref === localPath) ? localPath : ''}
          onChange={(e) => pickLocalPath(e.target.value)}
          error={fieldErrors.local_path}
          options={[
            ...rootEntries.map((entry) => ({
              value: entry.ref,
              // Каталог занят другим репозиторием workspace (uq_repositories_local_root) — предлагать нечего.
              disabled: entry.attached,
              label: `${entry.ref} — ${entry.path}${entry.attached ? ` · ${t('projects.repo.alreadyAttached')}` : ''}`,
            })),
            { value: MANUAL, label: t('projects.repo.enterManually') },
          ]}
        />
      ) : (
        <TextField
          id="repo-local-path"
          label={t('projects.repo.localPath')}
          hint={
            rootsSummary
              ? t('projects.repo.localPathHintRoots', { roots: rootsSummary })
              : provider === 'github'
                ? t('projects.repo.localPathHintGithub')
                : t('projects.repo.localPathHint')
          }
          autoComplete="off"
          maxLength={LOCAL_PATH_MAX}
          value={localPath}
          onChange={(e) => setLocalPath(e.target.value)}
          error={fieldErrors.local_path}
        />
      )}
      {rootsTruncated ? <p className="text-sm text-fg-muted">{t('projects.repo.rootsTruncated')}</p> : null}
      {!cloning && manualLocal && rootEntries.length > 0 ? (
        <div>
          <Button type="button" variant="ghost" onClick={() => setManualLocal(false)}>
            {t('projects.repo.backToList')}
          </Button>
        </div>
      ) : null}
      {roots.isSuccess && (roots.data.items ?? []).length === 0 ? (
        <Callout tone="warning" title={t('projects.repo.noRootsTitle')}>
          {t('projects.repo.noRoots')}
        </Callout>
      ) : null}
      {roots.isError ? (
        <Callout tone="danger" title={t('projects.repo.rootsError')}>
          {problemMessage(t, roots.error)} {t('projects.repo.pickerErrorFallback')}
        </Callout>
      ) : null}

      <TextField
        id="repo-branch"
        label={t('projects.repo.defaultBranch')}
        hint={t('projects.repo.defaultBranchHint')}
        autoComplete="off"
        maxLength={BRANCH_MAX}
        value={branch}
        onChange={(e) => setBranch(e.target.value)}
        error={fieldErrors.default_branch}
      />
      <div>
        <Button type="submit" variant="primary" loading={attach.isPending}>
          {attach.isPending ? t('projects.repo.attaching') : t('projects.repo.attach')}
        </Button>
      </div>
    </form>
  );
}

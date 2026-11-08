import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { IdempotentIntent } from '../../api/client';
import { projectKeys, projectsApi, type RepositoryAttachRequest, type RepositoryProvider } from '../../api/projects';
import { FormAlert, RadioGroup, TextField, focusFirstInvalid } from '../../components/form';
import { Button } from '../../components/ui';
import { mapProblemToFields } from '../../lib/problem';

type Field = 'provider' | 'remote_url' | 'local_path' | 'default_branch';
const FIELDS = ['provider', 'remote_url', 'local_path', 'default_branch'] as const;
const PROVIDERS = ['local', 'github'] as const satisfies readonly RepositoryProvider[];
/** RepositoryAttachRequest: remote_url ≤ 2048, local_path ≤ 4096, default_branch ≤ 255. */
export const REMOTE_URL_MAX = 2048;
export const LOCAL_PATH_MAX = 4096;
export const BRANCH_MAX = 255;
export const GITHUB_URL_PATTERN = /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/?$/;

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
  } else if (!input.localPath) errors.local_path = t('validation.required');
  else if (input.localPath.length > LOCAL_PATH_MAX) errors.local_path = t('validation.maxLength', { max: LOCAL_PATH_MAX });
  if (input.branch.length > BRANCH_MAX) errors.default_branch = t('validation.maxLength', { max: BRANCH_MAX });
  return errors;
}

export function AttachRepositoryForm({ projectId, onDone }: { projectId: string; onDone: (message: string) => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const formRef = useRef<HTMLFormElement>(null);
  const intent = useRef(new IdempotentIntent());
  const [provider, setProvider] = useState<RepositoryProvider>('local');
  const [remoteUrl, setRemoteUrl] = useState('');
  const [localPath, setLocalPath] = useState('');
  const [branch, setBranch] = useState('');
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<Field, string>>>({});
  const [formErrors, setFormErrors] = useState<string[]>([]);

  const attach = useMutation({
    mutationFn: (body: RepositoryAttachRequest) => projectsApi.attachRepository(projectId, body, intent.current.keyFor(body)),
    onSuccess: async () => {
      intent.current.complete();
      await queryClient.invalidateQueries({ queryKey: projectKeys.repositories(projectId) });
      onDone(t('projects.repo.attached'));
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
      ...(provider === 'github' ? { remote_url: input.remoteUrl } : { local_path: input.localPath }),
      ...(input.branch ? { default_branch: input.branch } : {}),
    };
    attach.mutate(body);
  };

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
      ) : (
        <TextField
          id="repo-local-path"
          label={t('projects.repo.localPath')}
          hint={t('projects.repo.localPathHint')}
          autoComplete="off"
          maxLength={LOCAL_PATH_MAX}
          value={localPath}
          onChange={(e) => setLocalPath(e.target.value)}
          error={fieldErrors.local_path}
        />
      )}
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

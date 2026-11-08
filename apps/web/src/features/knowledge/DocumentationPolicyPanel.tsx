import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { knowledgeApi, knowledgeKeys, type DocumentationPolicy } from '../../api/knowledge';
import { projectKeys, type DocumentationMode } from '../../api/projects';
import { FormAlert, RadioGroup } from '../../components/form';
import { ErrorState, LoadingState } from '../../components/states';
import { Button, Card } from '../../components/ui';
import { problemMessage } from '../../lib/problem';
import { ProjectSelect } from './ProjectSelect';

export const DOC_MODES = ['OFF', 'SMART', 'STRICT'] as const satisfies readonly DocumentationMode[];

function PolicyForm({ policy, canEdit }: { policy: DocumentationPolicy; canEdit: boolean }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [base, setBase] = useState(policy);
  const [mode, setMode] = useState<DocumentationMode>(policy.documentation_mode);
  const [status, setStatus] = useState<string | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  if (policy.settings_version > base.settings_version) {
    setBase(policy);
    setMode(policy.documentation_mode);
  }
  const save = useMutation({
    mutationFn: (next: DocumentationMode) => knowledgeApi.updatePolicy(base.project_id, { documentation_mode: next }, base.settings_version),
    onSuccess: async (updated) => {
      setBase(updated);
      setMode(updated.documentation_mode);
      setStatus(t('knowledge.policy.saved', { mode: t(`enums.documentation_mode.${updated.documentation_mode}`) }));
      queryClient.setQueryData(knowledgeKeys.policy(updated.project_id), updated);
      await queryClient.invalidateQueries({ queryKey: projectKeys.detail(updated.project_id) });
    },
    onError: (error) => setErrors([problemMessage(t, error)]),
  });
  const options = DOC_MODES.map((v) => ({
    value: v,
    label: t(`enums.documentation_mode.${v}`),
    description: t(`projects.documentationModeHint.${v}`),
  }));

  if (!canEdit) {
    return (
      <p>
        {t('knowledge.policy.current')}: <strong>{t(`enums.documentation_mode.${base.documentation_mode}`)}</strong>
        <span className="mt-1 block text-sm text-fg-muted">{t('knowledge.policy.readOnly')}</span>
      </p>
    );
  }
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    setErrors([]);
    setStatus(null);
    if (mode === base.documentation_mode) {
      setStatus(t('knowledge.policy.noChanges'));
      return;
    }
    save.mutate(mode);
  };
  return (
    <form noValidate onSubmit={onSubmit} className="flex flex-col gap-4">
      <FormAlert messages={errors} />
      <RadioGroup name="documentation-policy" legend={t('knowledge.policy.mode')} value={mode} onChange={setMode} options={options} />
      <p className="text-sm text-fg-muted">{t('knowledge.policy.effect')}</p>
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" variant="primary" loading={save.isPending}>
          {t('knowledge.policy.save')}
        </Button>
        <p role="status" className="text-sm text-fg-muted">
          {status}
        </p>
      </div>
    </form>
  );
}

function PolicyLoader({ projectId, canEdit }: { projectId: string; canEdit: boolean }) {
  const { t } = useTranslation();
  const query = useQuery({
    queryKey: knowledgeKeys.policy(projectId),
    queryFn: ({ signal }) => knowledgeApi.getPolicy(projectId, signal),
  });
  if (query.isPending) return <LoadingState inline label={t('knowledge.policy.loading')} />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  return <PolicyForm key={projectId} policy={query.data} canEdit={canEdit} />;
}

/** Режим документации проекта OFF/SMART/STRICT (GET/PATCH documentation-policy, If-Match settings_version). */
export function DocumentationPolicyPanel({ workspaceId, canEdit }: { workspaceId: string; canEdit: boolean }) {
  const { t } = useTranslation();
  const [projectId, setProjectId] = useState('');
  return (
    <Card as="section" id="documentation-policy" title={t('knowledge.policy.title')}>
      <div className="flex flex-col gap-4">
        <p className="text-sm text-fg-muted">{t('knowledge.policy.description')}</p>
        <div className="max-w-md">
          <ProjectSelect id="policy-project" workspaceId={workspaceId} value={projectId} onChange={setProjectId} label={t('knowledge.policy.project')} />
        </div>
        {projectId ? <PolicyLoader projectId={projectId} canEdit={canEdit} /> : null}
      </div>
    </Card>
  );
}

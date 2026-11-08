import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { IdempotentIntent } from '../../api/client';
import {
  knowledgeApi,
  knowledgeKeys,
  type DocumentationRefreshPolicy,
  type DocumentationSourceCreateRequest,
  type DocumentationSourceType,
  type DocumentationTrustLevel,
} from '../../api/knowledge';
import { projectKeys, projectsApi } from '../../api/projects';
import { FormAlert, RadioGroup, TextAreaField, TextField, focusFirstInvalid } from '../../components/form';
import { ErrorState, LoadingState } from '../../components/states';
import { Button } from '../../components/ui';
import { mapProblemToFields } from '../../lib/problem';
import { ProjectSelect } from './ProjectSelect';

export const SOURCE_TYPES = ['url', 'git', 'file'] as const satisfies readonly DocumentationSourceType[];
export const TRUST_LEVELS = ['official', 'internal', 'community'] as const satisfies readonly DocumentationTrustLevel[];
export const REFRESH_POLICIES = ['manual', 'scheduled'] as const satisfies readonly DocumentationRefreshPolicy[];
export const URI_MAX = 2048;
export const LICENSE_MAX = 2000;

type Field = 'project' | 'technology_version_id' | 'source_type' | 'uri_or_file_ref' | 'trust_level' | 'license_info' | 'refresh_policy';
const SERVER_FIELDS = ['technology_version_id', 'source_type', 'uri_or_file_ref', 'trust_level', 'license_info', 'refresh_policy'] as const;

/** https-URL для url; для git — https:// или git@; file — ссылка на загруженный artifact (непустая строка). */
export function validateSourceUri(type: DocumentationSourceType, uri: string): 'required' | 'max' | 'https' | 'git' | null {
  if (!uri) return 'required';
  if (uri.length > URI_MAX) return 'max';
  if (type === 'url' && !/^https:\/\/[^\s/]+\S*$/.test(uri)) return 'https';
  if (type === 'git' && !/^(https:\/\/|git@)\S+$/.test(uri)) return 'git';
  return null;
}

function TechnologySelect({ projectId, value, onChange, error }: { projectId: string; value: string; onChange: (v: string) => void; error?: string | undefined }) {
  const { t } = useTranslation();
  const query = useQuery({
    queryKey: [...projectKeys.technologies(projectId), 'select'] as const,
    queryFn: ({ signal }) => projectsApi.technologies(projectId, { limit: 200 }, signal),
  });
  if (query.isPending) return <LoadingState inline label={t('knowledge.register.techLoading')} />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  const errorId = error ? 'source-technology-error' : undefined;
  if (query.data.items.length === 0) return <p className="text-sm text-fg-muted">{t('knowledge.register.noTechnologies')}</p>;
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor="source-technology" className="text-sm font-medium">
        {t('knowledge.register.technology')}
      </label>
      <select
        id="source-technology"
        className="input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-invalid={Boolean(error)}
        aria-describedby={errorId}
      >
        <option value="">{t('knowledge.register.selectTechnology')}</option>
        {query.data.items.map((x) => (
          <option key={x.technology_version_id} value={x.technology_version_id}>
            {x.name} {x.version_label} ({x.ecosystem})
          </option>
        ))}
      </select>
      {error ? (
        <p id={errorId} className="text-sm font-medium text-danger-text">
          {error}
        </p>
      ) : null}
    </div>
  );
}

export function RegisterSourceForm({ workspaceId, onDone }: { workspaceId: string; onDone: (message: string) => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const formRef = useRef<HTMLFormElement>(null);
  const intent = useRef(new IdempotentIntent());
  const [projectId, setProjectId] = useState('');
  const [technology, setTechnology] = useState('');
  const [sourceType, setSourceType] = useState<DocumentationSourceType>('url');
  const [uri, setUri] = useState('');
  const [trust, setTrust] = useState<DocumentationTrustLevel>('official');
  const [license, setLicense] = useState('');
  const [refreshPolicy, setRefreshPolicy] = useState<DocumentationRefreshPolicy>('manual');
  const [enabled, setEnabled] = useState(true);
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<Field, string>>>({});
  const [formErrors, setFormErrors] = useState<string[]>([]);

  const register = useMutation({
    mutationFn: (body: DocumentationSourceCreateRequest) => knowledgeApi.registerSource(workspaceId, body, intent.current.keyFor(body)),
    onSuccess: async () => {
      intent.current.complete();
      await queryClient.invalidateQueries({ queryKey: knowledgeKeys.allSources(workspaceId) });
      onDone(t('knowledge.register.done'));
    },
    onError: (error) => {
      const mapped = mapProblemToFields(t, error, SERVER_FIELDS);
      setFieldErrors(mapped.fields);
      setFormErrors(mapped.form);
      requestAnimationFrame(() => focusFirstInvalid(formRef.current));
    },
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const errors: Partial<Record<Field, string>> = {};
    const trimmedUri = uri.trim();
    const trimmedLicense = license.trim();
    if (!projectId) errors.project = t('validation.required');
    else if (!technology) errors.technology_version_id = t('validation.required');
    const uriError = validateSourceUri(sourceType, trimmedUri);
    if (uriError === 'required') errors.uri_or_file_ref = t('validation.required');
    else if (uriError === 'max') errors.uri_or_file_ref = t('validation.maxLength', { max: URI_MAX });
    else if (uriError) errors.uri_or_file_ref = t(`knowledge.register.uriInvalid.${uriError}`);
    if (trimmedLicense.length > LICENSE_MAX) errors.license_info = t('validation.maxLength', { max: LICENSE_MAX });
    setFieldErrors(errors);
    setFormErrors([]);
    if (Object.keys(errors).length > 0) {
      requestAnimationFrame(() => focusFirstInvalid(formRef.current));
      return;
    }
    register.mutate({
      technology_version_id: technology,
      source_type: sourceType,
      uri_or_file_ref: trimmedUri,
      trust_level: trust,
      refresh_policy: refreshPolicy,
      enabled,
      ...(trimmedLicense ? { license_info: trimmedLicense } : {}),
    });
  };

  return (
    <form ref={formRef} noValidate onSubmit={onSubmit} className="flex flex-col gap-4" aria-label={t('knowledge.register.title')}>
      <FormAlert messages={formErrors} />
      <ProjectSelect
        id="source-project"
        workspaceId={workspaceId}
        value={projectId}
        onChange={(v) => {
          setProjectId(v);
          setTechnology('');
        }}
        label={t('knowledge.register.project')}
        error={fieldErrors.project}
      />
      {projectId ? <TechnologySelect projectId={projectId} value={technology} onChange={setTechnology} error={fieldErrors.technology_version_id} /> : null}
      <RadioGroup
        name="source-type"
        legend={t('knowledge.register.sourceType')}
        value={sourceType}
        onChange={setSourceType}
        error={fieldErrors.source_type}
        options={SOURCE_TYPES.map((v) => ({ value: v, label: t(`knowledge.enums.source_type.${v}`), description: t(`knowledge.register.sourceTypeHint.${v}`) }))}
      />
      <TextField
        id="source-uri"
        label={t('knowledge.register.uri')}
        autoComplete="off"
        maxLength={URI_MAX}
        value={uri}
        onChange={(e) => setUri(e.target.value)}
        error={fieldErrors.uri_or_file_ref}
      />
      <RadioGroup
        name="source-trust"
        legend={t('knowledge.register.trust')}
        value={trust}
        onChange={setTrust}
        error={fieldErrors.trust_level}
        options={TRUST_LEVELS.map((v) => ({ value: v, label: t(`knowledge.enums.trust_level.${v}`) }))}
      />
      <RadioGroup
        name="source-refresh"
        legend={t('knowledge.register.refreshPolicy')}
        value={refreshPolicy}
        onChange={setRefreshPolicy}
        error={fieldErrors.refresh_policy}
        options={REFRESH_POLICIES.map((v) => ({ value: v, label: t(`knowledge.enums.refresh_policy.${v}`) }))}
      />
      <TextAreaField
        id="source-license"
        label={t('knowledge.register.license')}
        hint={t('knowledge.register.licenseHint')}
        maxLength={LICENSE_MAX}
        value={license}
        onChange={(e) => setLicense(e.target.value)}
        error={fieldErrors.license_info}
      />
      <div className="flex items-center gap-2">
        <input
          id="source-enabled"
          type="checkbox"
          className="h-4 w-4 accent-[rgb(var(--color-accent))]"
          checked={enabled}
          onChange={(e) => setEnabled(e.target.checked)}
        />
        <label htmlFor="source-enabled" className="text-sm">
          {t('knowledge.register.enabled')}
        </label>
      </div>
      <p className="text-sm text-fg-muted">{t('knowledge.register.noDownload')}</p>
      <div>
        <Button type="submit" variant="primary" loading={register.isPending}>
          {t('knowledge.register.submit')}
        </Button>
      </div>
    </form>
  );
}

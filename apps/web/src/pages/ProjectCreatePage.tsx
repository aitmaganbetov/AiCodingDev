import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useNavigate } from 'react-router';
import {
  api,
  IdempotentIntent,
  type DocumentationMode,
  type ProjectCreateRequest,
  type QualityProfile,
} from '../api/client';
import { FormAlert, RadioGroup, TextAreaField, TextField, focusFirstInvalid } from '../components/form';
import { PageHeading } from '../components/PageHeading';
import { can } from '../lib/permissions';
import { mapProblemToFields } from '../lib/problem';
import { PROJECT_DESCRIPTION_MAX, PROJECT_NAME_MAX } from '../lib/validation';
import { useCurrentWorkspace } from '../workspace';
import { WorkspaceNotFound } from './NotFoundPage';
import { projectsQueryKey } from './ProjectsPage';

type Field = 'name' | 'description' | 'documentation_mode' | 'quality_profile';
const FIELDS = ['name', 'description', 'documentation_mode', 'quality_profile'] as const;
const DOC_MODES = ['OFF', 'SMART', 'STRICT'] as const satisfies readonly DocumentationMode[];
const QUALITY_PROFILES = ['basic', 'strict', 'critical'] as const satisfies readonly QualityProfile[];

export function ProjectCreatePage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { workspaceId, membership } = useCurrentWorkspace();
  const formRef = useRef<HTMLFormElement>(null);
  const intent = useRef(new IdempotentIntent());
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [documentationMode, setDocumentationMode] = useState<DocumentationMode>('SMART');
  const [qualityProfile, setQualityProfile] = useState<QualityProfile>('strict');
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<Field, string>>>({});
  const [formErrors, setFormErrors] = useState<string[]>([]);

  const create = useMutation({
    mutationFn: ({ ws, body }: { ws: string; body: ProjectCreateRequest }) =>
      api.createProject(ws, body, intent.current.keyFor({ ws, body })),
    onSuccess: async (project, { ws }) => {
      intent.current.complete();
      await queryClient.invalidateQueries({ queryKey: projectsQueryKey(ws) });
      navigate(`/w/${ws}/projects`, { state: { createdProject: project.name } });
    },
    onError: (error) => {
      const mapped = mapProblemToFields(t, error, FIELDS);
      setFieldErrors(mapped.fields);
      setFormErrors(mapped.form);
      requestAnimationFrame(() => focusFirstInvalid(formRef.current));
    },
  });

  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  const listPath = `/w/${workspaceId}/projects`;

  if (!can(membership.role, 'project:create')) {
    return (
      <section aria-labelledby="page-title" className="max-w-xl">
        <PageHeading title={t('projects.createTitle')} />
        <p className="mb-4 text-slate-700">{t('projects.noPermission')}</p>
        <Link to={listPath} className="btn-secondary">
          {t('projects.backToList')}
        </Link>
      </section>
    );
  }

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const errors: Partial<Record<Field, string>> = {};
    const trimmedName = name.trim();
    const trimmedDescription = description.trim();
    if (!trimmedName) errors.name = t('validation.required');
    else if (trimmedName.length > PROJECT_NAME_MAX) errors.name = t('validation.maxLength', { max: PROJECT_NAME_MAX });
    if (trimmedDescription.length > PROJECT_DESCRIPTION_MAX) {
      errors.description = t('validation.maxLength', { max: PROJECT_DESCRIPTION_MAX });
    }
    setFieldErrors(errors);
    setFormErrors([]);
    if (Object.keys(errors).length > 0) {
      requestAnimationFrame(() => focusFirstInvalid(formRef.current));
      return;
    }
    const body: ProjectCreateRequest = {
      name: trimmedName,
      documentation_mode: documentationMode,
      quality_profile: qualityProfile,
      ...(trimmedDescription ? { description: trimmedDescription } : {}),
    };
    create.mutate({ ws: workspaceId, body });
  };

  return (
    <section aria-labelledby="page-title" className="max-w-xl">
      <PageHeading title={t('projects.createTitle')} />
      <form ref={formRef} noValidate onSubmit={onSubmit} className="card flex flex-col gap-5">
        <FormAlert messages={formErrors} />
        <TextField
          id="project-name"
          label={t('projects.name')}
          name="name"
          required
          autoComplete="off"
          maxLength={PROJECT_NAME_MAX}
          value={name}
          onChange={(e) => setName(e.target.value)}
          error={fieldErrors.name}
        />
        <TextAreaField
          id="project-description"
          label={t('projects.description')}
          name="description"
          maxLength={PROJECT_DESCRIPTION_MAX}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          hint={t('projects.descriptionHint', { max: PROJECT_DESCRIPTION_MAX })}
          error={fieldErrors.description}
        />
        <RadioGroup
          name="documentation_mode"
          legend={t('projects.documentationMode')}
          value={documentationMode}
          onChange={setDocumentationMode}
          error={fieldErrors.documentation_mode}
          options={DOC_MODES.map((v) => ({
            value: v,
            label: t(`enums.documentation_mode.${v}`),
            description: t(`projects.documentationModeHint.${v}`),
          }))}
        />
        <RadioGroup
          name="quality_profile"
          legend={t('projects.qualityProfile')}
          value={qualityProfile}
          onChange={setQualityProfile}
          error={fieldErrors.quality_profile}
          options={QUALITY_PROFILES.map((v) => ({
            value: v,
            label: t(`enums.quality_profile.${v}`),
            description: t(`projects.qualityProfileHint.${v}`),
          }))}
        />
        <div className="flex flex-wrap gap-3">
          <button type="submit" className="btn-primary" disabled={create.isPending}>
            {create.isPending ? t('action.creating') : t('action.create')}
          </button>
          <Link to={listPath} className="btn-secondary">
            {t('action.cancel')}
          </Link>
        </div>
      </form>
    </section>
  );
}

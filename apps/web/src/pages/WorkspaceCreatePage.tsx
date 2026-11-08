import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useNavigate } from 'react-router';
import { IdempotentIntent, isApiError } from '../api/client';
import { workspaceKeys, workspacesApi, type WorkspaceCreateRequest } from '../api/workspaces';
import { sessionQueryKey } from '../auth/session';
import { FormAlert, TextField, focusFirstInvalid } from '../components/form';
import { PageHeading } from '../components/PageHeading';
import { mapProblemToFields } from '../lib/problem';
import { WORKSPACE_NAME_MAX, WORKSPACE_SLUG_PATTERN } from '../lib/validation';

type Field = 'name' | 'slug';
const FIELDS = ['name', 'slug'] as const;

export function validateWorkspace(
  values: { name: string; slug: string },
  t: (key: string, opts?: Record<string, unknown>) => string,
): Partial<Record<Field, string>> {
  const errors: Partial<Record<Field, string>> = {};
  const name = values.name.trim();
  if (!name) errors.name = t('validation.required');
  else if (name.length > WORKSPACE_NAME_MAX) errors.name = t('validation.maxLength', { max: WORKSPACE_NAME_MAX });
  if (!values.slug) errors.slug = t('validation.required');
  else if (!WORKSPACE_SLUG_PATTERN.test(values.slug)) errors.slug = t('workspaces.slugInvalid');
  return errors;
}

export function WorkspaceCreatePage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const formRef = useRef<HTMLFormElement>(null);
  const intent = useRef(new IdempotentIntent());
  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<Field, string>>>({});
  const [formErrors, setFormErrors] = useState<string[]>([]);

  const create = useMutation({
    mutationFn: (body: WorkspaceCreateRequest) => workspacesApi.create(body, intent.current.keyFor(body)),
    onSuccess: async (ws) => {
      intent.current.complete();
      // Роль в новом workspace приходит в членствах сессии — обновляем её до перехода.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: sessionQueryKey }),
        queryClient.invalidateQueries({ queryKey: workspaceKeys.all }),
      ]);
      navigate(`/w/${ws.id}/projects`, { state: { createdWorkspace: ws.name } });
    },
    onError: (error) => {
      const mapped = mapProblemToFields(t, error, FIELDS);
      if (isApiError(error) && error.code === 'conflict' && error.errors.length === 0) {
        mapped.form = [];
        mapped.fields.slug = t('workspaces.slugTaken');
      }
      setFieldErrors(mapped.fields);
      setFormErrors(mapped.form);
      requestAnimationFrame(() => focusFirstInvalid(formRef.current));
    },
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const errors = validateWorkspace({ name, slug }, t);
    setFieldErrors(errors);
    setFormErrors([]);
    if (Object.keys(errors).length > 0) {
      requestAnimationFrame(() => focusFirstInvalid(formRef.current));
      return;
    }
    create.mutate({ name: name.trim(), slug });
  };

  return (
    <section aria-labelledby="page-title" className="max-w-xl">
      <PageHeading title={t('workspaces.createTitle')} />
      <form ref={formRef} noValidate onSubmit={onSubmit} className="card flex flex-col gap-4">
        <FormAlert messages={formErrors} />
        <TextField
          id="workspace-name"
          label={t('workspaces.name')}
          name="name"
          required
          autoComplete="off"
          maxLength={WORKSPACE_NAME_MAX}
          value={name}
          onChange={(e) => setName(e.target.value)}
          error={fieldErrors.name}
        />
        <TextField
          id="workspace-slug"
          label={t('workspaces.slug')}
          name="slug"
          required
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          maxLength={63}
          value={slug}
          onChange={(e) => setSlug(e.target.value)}
          hint={t('workspaces.slugHint')}
          error={fieldErrors.slug}
        />
        <div className="flex flex-wrap gap-3">
          <button type="submit" className="btn-primary" disabled={create.isPending}>
            {create.isPending ? t('action.creating') : t('action.create')}
          </button>
          <Link to="/workspaces" className="btn-secondary">
            {t('action.cancel')}
          </Link>
        </div>
      </form>
    </section>
  );
}

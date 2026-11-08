import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Navigate, useNavigate, useSearchParams } from 'react-router';
import { authApi } from '../api/auth';
import { isApiError } from '../api/client';
import { safeNextPath, storeSession, useSession, wasLoggedOut } from '../auth/session';
import { FormAlert, TextField, focusFirstInvalid } from '../components/form';
import { PageHeading } from '../components/PageHeading';
import { LoadingState } from '../components/states';
import { EMAIL_MAX, PASSWORD_MAX } from '../lib/validation';
import { mapProblemToFields, problemMessage } from '../lib/problem';

type Field = 'email' | 'password';

function landingPath(next: string | null): string {
  return safeNextPath(next) ?? '/';
}

export function LoginPage() {
  const { t } = useTranslation();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const session = useSession();
  const formRef = useRef<HTMLFormElement>(null);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<Field, string>>>({});
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const [loggedOut] = useState(wasLoggedOut);

  const login = useMutation({
    mutationFn: authApi.login,
    onSuccess: (s) => {
      storeSession(queryClient, s);
      navigate(landingPath(params.get('next')), { replace: true });
    },
    onError: (error) => {
      if (isApiError(error) && error.status === 401) {
        // Сервер не уточняет, что именно неверно (API_DESIGN §3) — и UI тоже.
        setFormErrors([t('auth.invalidCredentials')]);
        setPassword('');
        return;
      }
      if (isApiError(error) && error.code === 'validation_error') {
        const mapped = mapProblemToFields(t, error, ['email', 'password'] as const);
        setFieldErrors(mapped.fields);
        setFormErrors(mapped.form);
        requestAnimationFrame(() => focusFirstInvalid(formRef.current));
        return;
      }
      setFormErrors([problemMessage(t, error)]);
    },
  });

  if (session.isPending) return <LoadingState />;
  if (session.data && !login.isPending) return <Navigate to={landingPath(params.get('next'))} replace />;

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const errors: Partial<Record<Field, string>> = {};
    if (!email.trim()) errors.email = t('validation.required');
    if (!password) errors.password = t('validation.required');
    setFieldErrors(errors);
    setFormErrors([]);
    if (Object.keys(errors).length > 0) {
      requestAnimationFrame(() => focusFirstInvalid(formRef.current));
      return;
    }
    login.mutate({ email: email.trim(), password });
  };


  return (
    <main id="main" tabIndex={-1} className="mx-auto flex min-h-screen w-full max-w-md flex-col justify-center px-4 py-10">
      <p className="mb-2 text-lg font-semibold text-fg">{t('app.name')}</p>
      <PageHeading title={t('auth.loginTitle')} />
      {loggedOut ? (
        <p role="status" className="mb-4 rounded-md bg-surface-muted p-3 text-sm text-fg">
          {t('auth.loggedOut')}
        </p>
      ) : null}
      <form ref={formRef} noValidate onSubmit={onSubmit} className="card flex flex-col gap-4">
        <FormAlert messages={formErrors} />
        <TextField
          id="login-email"
          label={t('auth.email')}
          type="email"
          name="email"
          autoComplete="username"
          inputMode="email"
          required
          maxLength={EMAIL_MAX}
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          error={fieldErrors.email}
        />
        <TextField
          id="login-password"
          label={t('auth.password')}
          type="password"
          name="password"
          autoComplete="current-password"
          required
          maxLength={PASSWORD_MAX}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          error={fieldErrors.password}
        />
        <button type="submit" className="btn-primary" disabled={login.isPending}>
          {login.isPending ? t('auth.signingIn') : t('auth.signIn')}
        </button>
      </form>
    </main>
  );
}

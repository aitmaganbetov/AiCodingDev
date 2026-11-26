/**
 * Device-code вход CLI-провайдера из UI (P2-01, SECURITY_ARCHITECTURE §6.2, cli-auth.md).
 *
 * Экран не принимает ни пароля, ни API-ключа, ни токена и не показывает поля для них: платформа их не
 * получает (ТЗ §1, ADR-0003). «Войти» просит worker-exec запустить официальную команду входа CLI на хосте
 * исполнения; UI показывает только то, что напечатал сам CLI — ссылку и код устройства, — а оператор
 * подтверждает вход на сайте провайдера. Токен пишет сам CLI в свой credential store.
 *
 * Состояния сессии опрашиваются так же, как статус probe (`LoginAccepted.status_url`): PENDING → CHALLENGE →
 * AUTHENTICATED | FAILED | CANCELLED | EXPIRED. Доступно только роли owner и после step-up re-auth.
 *
 * Единственное поле ввода на экране — код авторизации, который провайдер показывает оператору в браузере
 * (Claude Code: «Paste code here if prompted >»); оно появляется, когда CLI об этом попросил
 * (`awaiting_code`). Это не токен и не ключ: одноразовый код OAuth/PKCE, который платформа не хранит, а
 * передаёт в stdin процесса CLI на хосте исполнения (см. `aicd_platform.login_input`).
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IdempotentIntent } from '../../api/client';
import { providerKeys, providersApi, type Provider, type ProviderLoginState } from '../../api/providers';
import { useStepUp } from '../../auth/StepUp';
import { Badge, Button, Callout, CodeBlock, Timestamp, useToast } from '../../components/ui';
import { problemMessage } from '../../lib/problem';

const LOGIN_POLL_MS = 2000;
const LOGIN_FINAL: readonly ProviderLoginState[] = ['AUTHENTICATED', 'FAILED', 'CANCELLED', 'EXPIRED'];

export function LoginAction({ workspaceId, provider }: { workspaceId: string; provider: Provider }) {
  const { t } = useTranslation();
  const toast = useToast();
  const queryClient = useQueryClient();
  const stepUp = useStepUp();
  const intent = useRef(new IdempotentIntent());
  const cancelIntent = useRef(new IdempotentIntent());
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [code, setCode] = useState('');
  const codeIntent = useRef(new IdempotentIntent());

  const start = useMutation({
    mutationFn: () =>
      stepUp.run(() =>
        providersApi.login(workspaceId, provider.provider_id, intent.current.keyFor({ provider: provider.provider_id })),
      ),
    onSuccess: (accepted) => {
      intent.current.complete();
      setError(null);
      setSessionId(accepted.login_session_id);
      toast({ title: t('providers.login.started', { name: provider.display_name }), tone: 'info' });
    },
    onError: (e) => setError(problemMessage(t, e)),
  });

  const status = useQuery({
    queryKey: providerKeys.login(workspaceId, provider.provider_id, sessionId ?? ''),
    queryFn: ({ signal }) => providersApi.loginStatus(workspaceId, provider.provider_id, sessionId ?? '', signal),
    enabled: sessionId !== null,
    refetchInterval: (q) => (q.state.data && LOGIN_FINAL.includes(q.state.data.state) ? false : LOGIN_POLL_MS),
  });

  const cancel = useMutation({
    mutationFn: () =>
      providersApi.cancelLogin(
        workspaceId,
        provider.provider_id,
        sessionId ?? '',
        cancelIntent.current.keyFor({ session: sessionId ?? '' }),
      ),
    onSuccess: () => {
      cancelIntent.current.complete();
      void status.refetch();
    },
    onError: (e) => setError(problemMessage(t, e)),
  });

  const submitCode = useMutation({
    mutationFn: (value: string) =>
      providersApi.submitLoginCode(
        workspaceId,
        provider.provider_id,
        sessionId ?? '',
        value,
        codeIntent.current.keyFor({ session: sessionId ?? '', code: value }),
      ),
    onSuccess: () => {
      codeIntent.current.complete();
      setCode('');
      setError(null);
      toast({ title: t('providers.login.code.sent'), tone: 'info' });
      void status.refetch();
    },
    onError: (e) => setError(problemMessage(t, e)),
  });

  const session = status.data ?? null;
  const final = session && LOGIN_FINAL.includes(session.state) ? session.state : null;
  // Вход меняет auth_status установки: после завершения перечитываем список провайдеров.
  useEffect(() => {
    if (final) void queryClient.invalidateQueries({ queryKey: providerKeys.list(workspaceId) });
  }, [final, queryClient, workspaceId]);
  useEffect(() => {
    if (final === 'AUTHENTICATED') toast({ title: t('providers.login.state.AUTHENTICATED'), tone: 'success' });
  }, [final, t, toast]);

  const active = sessionId !== null && final === null;
  const problem = error ?? (status.isError ? problemMessage(t, status.error) : null);

  return (
    <div className="flex min-w-0 flex-col items-start gap-2" data-testid={`provider-login-${provider.code}`}>
      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={() => start.mutate()} loading={start.isPending || active} disabled={active}>
          {t('providers.login.action')}
        </Button>
        {active ? (
          <Button variant="secondary" onClick={() => cancel.mutate()} loading={cancel.isPending}>
            {t('providers.login.cancel')}
          </Button>
        ) : null}
        {session ? (
          <Badge
            tone={session.state === 'AUTHENTICATED' ? 'success' : final ? 'danger' : 'warning'}
            data-login-state={session.state}
          >
            {t(`providers.login.state.${session.state}`, { defaultValue: session.state })}
          </Badge>
        ) : null}
      </div>
      <p className="text-xs text-fg-muted">{t('providers.login.hint')}</p>
      {problem ? (
        <p role="alert" className="text-sm font-medium text-danger-text">
          {problem}
        </p>
      ) : null}
      {session && session.state === 'CHALLENGE' && session.verification_url ? (
        <Callout tone="info" title={t('providers.login.challengeTitle')} className="w-full">
          <ol className="list-decimal space-y-2 pl-5">
            <li>
              {t('providers.login.step.open')}{' '}
              <a className="link break-all" href={session.verification_url} target="_blank" rel="noreferrer noopener">
                {session.verification_url}
              </a>
            </li>
            {session.user_code ? (
              <li>
                {t('providers.login.step.enterCode')}
                <CodeBlock
                  className="mt-1"
                  code={session.user_code}
                  label={t('providers.login.step.enterCode')}
                  wrap
                />
              </li>
            ) : null}
            <li>{t('providers.login.step.wait')}</li>
          </ol>
          <p className="mt-2 text-xs text-fg-muted">
            {t('providers.login.expiresAt')}: <Timestamp value={session.expires_at} />
          </p>
        </Callout>
      ) : null}
      {session && session.awaiting_code ? (
        <Callout tone="info" title={t('providers.login.code.title')} className="w-full">
          <p>{t('providers.login.code.hint')}</p>
          <form
            className="mt-2 flex flex-wrap items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (code.trim() === '') {
                setError(t('providers.login.code.empty'));
                return;
              }
              submitCode.mutate(code.trim());
            }}
          >
            <label className="flex min-w-0 flex-1 flex-col gap-1 text-sm">
              <span className="text-fg-muted">{t('providers.login.code.label')}</span>
              <input
                className="input min-h-target font-mono text-sm"
                name="login-code"
                data-testid="login-code-input"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                placeholder={t('providers.login.code.placeholder')}
                maxLength={1024}
                autoComplete="off"
                autoCorrect="off"
                spellCheck={false}
              />
            </label>
            <Button type="submit" loading={submitCode.isPending} data-testid="login-code-submit">
              {t('providers.login.code.submit')}
            </Button>
          </form>
        </Callout>
      ) : null}
      {session && session.state === 'PENDING' ? (
        <p role="status" className="text-sm text-fg-muted">
          {t('providers.login.state.PENDING')}
        </p>
      ) : null}
      {session && final && final !== 'AUTHENTICATED' ? (
        <Callout tone="danger" title={t(`providers.login.state.${final}`)} className="w-full">
          <p>
            {session.error_category
              ? t(`providers.enums.cli_error.${session.error_category}`, { defaultValue: session.error_category })
              : '—'}
          </p>
          {session.error_category === 'CLI_NOT_INSTALLED' ? <p className="mt-1">{t('providers.login.notInstalled')}</p> : null}
        </Callout>
      ) : null}
      {session && session.output_tail ? (
        <details className="w-full">
          <summary className="min-h-target cursor-pointer text-sm text-fg-muted">{t('providers.login.output')}</summary>
          <CodeBlock className="mt-1" code={session.output_tail} label={t('providers.login.output')} wrap />
          <p className="mt-1 text-xs text-fg-muted">{t('providers.login.outputScrubbed')}</p>
        </details>
      ) : null}
      {stepUp.dialog}
    </div>
  );
}

/**
 * Step-up re-auth (SC-IAM-08, SECURITY_ARCHITECTURE §3.2): операции критического набора (политики workspace и
 * провайдеров, управление участниками, утверждение критических approvals) сервер отклоняет 403
 * `step_up_required`, если пароль в сессии вводился больше 10 минут назад.
 *
 * `useStepUp()` оборачивает вызов API: при `step_up_required` открывается диалог ввода пароля
 * (`POST /auth/reauthenticate`), после подтверждения сессия ротируется (новый csrf_token сохраняется) и тот же
 * вызов повторяется — с тем же Idempotency-Key/If-Match (намерение пользователя не изменилось). Отмена диалога
 * возвращает исходную ошибку вызывающему (форма показывает её как обычную ошибку). Пароль хранится только в
 * состоянии поля и очищается при закрытии.
 */
import { useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { authApi } from '../api/auth';
import { isApiError } from '../api/client';
import { FormAlert, TextField } from '../components/form';
import { Button, Dialog, DialogContent } from '../components/ui';
import { problemMessage } from '../lib/problem';
import { storeSession } from './session';

export function isStepUpRequired(error: unknown): boolean {
  return isApiError(error) && error.code === 'step_up_required';
}

interface Pending {
  retry: () => Promise<unknown>;
  resolve: (value: unknown) => void;
  reject: (reason: unknown) => void;
  error: unknown;
}

export interface StepUp {
  /** Выполнить вызов; при `step_up_required` — запросить пароль и повторить. */
  run: <T>(call: () => Promise<T>) => Promise<T>;
  /** Диалог — отрисовать в дереве компонента, вызывающего `run`. */
  dialog: ReactNode;
}

export function useStepUp(): StepUp {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const uid = useId();
  const pending = useRef<Pending | null>(null);
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState('');
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  // Фокус — в поле пароля (Radix по умолчанию фокусирует первую кнопку диалога — «Закрыть»).
  useEffect(() => {
    if (!open) return;
    const frame = requestAnimationFrame(() => document.getElementById(`${uid}-password`)?.focus());
    return () => cancelAnimationFrame(frame);
  }, [open, uid]);

  const run = useCallback(async <T,>(call: () => Promise<T>): Promise<T> => {
    try {
      return await call();
    } catch (error) {
      if (!isStepUpRequired(error)) throw error;
      return new Promise<T>((resolve, reject) => {
        pending.current = { retry: call, resolve: resolve as (value: unknown) => void, reject, error };
        setPassword('');
        setErrors([]);
        setOpen(true);
      });
    }
  }, []);

  const close = () => {
    const p = pending.current;
    pending.current = null;
    setOpen(false);
    setPassword('');
    setErrors([]);
    p?.reject(p.error);
  };

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    // Диалог может быть отрисован внутри формы вызывающего: событие React всплывает по дереву компонентов (через портал).
    e.stopPropagation();
    if (!password) {
      setErrors([t('validation.required')]);
      return;
    }
    setBusy(true);
    try {
      const session = await authApi.reauthenticate({ password });
      storeSession(queryClient, session);
    } catch (error) {
      setBusy(false);
      setErrors([isApiError(error) && error.code === 'reauthentication_failed' ? t('settings.stepUp.wrongPassword') : problemMessage(t, error)]);
      return;
    }
    setBusy(false);
    const p = pending.current;
    pending.current = null;
    setOpen(false);
    setPassword('');
    if (p) p.retry().then(p.resolve, p.reject);
  };

  const dialog = (
    <Dialog open={open} onOpenChange={(next) => (next ? setOpen(true) : close())}>
      <DialogContent
        size="sm"
        title={t('settings.stepUp.title')}
        description={t('settings.stepUp.description')}
        footer={
          <>
            <Button onClick={close}>{t('settings.stepUp.cancel')}</Button>
            <Button type="submit" form={`${uid}-form`} variant="primary" loading={busy}>
              {t('settings.stepUp.confirm')}
            </Button>
          </>
        }
      >
        <form id={`${uid}-form`} noValidate onSubmit={(e) => void onSubmit(e)} className="flex flex-col gap-3">
          <FormAlert messages={errors} />
          <TextField
            id={`${uid}-password`}
            label={t('settings.stepUp.password')}
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </form>
      </DialogContent>
    </Dialog>
  );

  return { run, dialog };
}

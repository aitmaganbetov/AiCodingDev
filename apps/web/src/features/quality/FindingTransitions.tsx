/**
 * Решение человека по находке (FSM `transitionFinding`, DATABASE_DESIGN §18.3): кнопки допустимых
 * переходов с учётом роли (RBAC — только UX, решает сервер) и профиля качества + диалог с обоснованием.
 * Используется карточкой находки (`FindingPage`) и Центром безопасности (F7).
 * If-Match — версия, которую видел человек; Idempotency-Key — одно намерение. Если сервер потребует
 * повторный ввод пароля (`403 step_up_required`), запрос повторяется после step-up с тем же ключом.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { IdempotentIntent, isApiError } from '../../api/client';
import {
  acceptedRiskBlockedByProfile,
  allowedFindingTransitions,
  canTransitionFinding,
  findingKeys,
  findingsApi,
  type Finding,
  type FindingTargetStatus,
} from '../../api/findings';
import { qualityKeys } from '../../api/quality';
import { useStepUp } from '../../auth/StepUp';
import { FormAlert, TextAreaField } from '../../components/form';
import { Button } from '../../components/ui/Button';
import { Dialog, DialogClose, DialogContent } from '../../components/ui/Dialog';
import { useToast } from '../../components/ui/Toast';
import { problemMessage } from '../../lib/problem';
import { useWorkspaceRole } from './shared';

export const REASON_MAX = 4000;

function TransitionDialog({
  finding,
  to,
  open,
  onOpenChange,
  onDone,
}: {
  finding: Finding;
  to: FindingTargetStatus;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone?: ((updated: Finding) => void) | undefined;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const queryClient = useQueryClient();
  const stepUp = useStepUp();
  const intent = useRef(new IdempotentIntent());
  const [reason, setReason] = useState('');
  const [fieldError, setFieldError] = useState<string | undefined>();
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const version = finding.version ?? 0;

  const mutation = useMutation({
    mutationFn: (body: { to_status: FindingTargetStatus; reason: string }) => {
      const key = intent.current.keyFor({ id: finding.id, version, ...body });
      return stepUp.run(() => findingsApi.transition(finding.id, body, key, version));
    },
    onSuccess: (updated) => {
      intent.current.complete();
      queryClient.setQueryData(findingKeys.detail(finding.id), updated);
      // Списки run и workspace (Центр безопасности, Dashboard) — общий префикс ['findings'].
      void queryClient.invalidateQueries({ queryKey: ['findings'] });
      void queryClient.invalidateQueries({ queryKey: qualityKeys.run(finding.run_id ?? '') });
      toast({ title: t('quality.transition.done', { status: t(`quality.enums.finding_status.${updated.status}`) }), tone: 'success' });
      onDone?.(updated);
      onOpenChange(false);
    },
    onError: (error) => {
      // 409 (FSM ушла вперёд) / 412 (версия устарела): показать причину и перечитать находку.
      if (isApiError(error) && (error.status === 409 || error.status === 412)) {
        void queryClient.invalidateQueries({ queryKey: findingKeys.detail(finding.id) });
        void queryClient.invalidateQueries({ queryKey: ['findings'] });
      }
      const fe = isApiError(error) ? error.errors.find((e) => e.pointer === '/reason') : undefined;
      setFieldError(fe ? t(`fieldError.${fe.code}`, { defaultValue: fe.message ?? t('fieldError.invalid') }) : undefined);
      setFormErrors(fe ? [] : [problemMessage(t, error)]);
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const trimmed = reason.trim();
    if (!trimmed) {
      setFieldError(t('validation.required'));
      return;
    }
    if (trimmed.length > REASON_MAX) {
      setFieldError(t('validation.maxLength', { max: REASON_MAX }));
      return;
    }
    setFieldError(undefined);
    setFormErrors([]);
    mutation.mutate({ to_status: to, reason: trimmed });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title={t('quality.transition.dialogTitle', { status: t(`quality.enums.finding_status.${to}`) })}
        description={t(`quality.transition.hint.${to}`)}
      >
        <form onSubmit={submit} noValidate className="flex flex-col gap-4">
          <FormAlert messages={formErrors} />
          <TextAreaField
            id="finding-reason"
            label={t('quality.transition.reason')}
            hint={t('quality.transition.reasonHint')}
            error={fieldError}
            value={reason}
            maxLength={REASON_MAX}
            required
            onChange={(e) => setReason(e.target.value)}
          />
          <div className="flex flex-wrap justify-end gap-3">
            <DialogClose asChild>
              <Button>{t('action.cancel')}</Button>
            </DialogClose>
            <Button type="submit" variant={to === 'ACCEPTED_RISK' ? 'danger' : 'primary'} loading={mutation.isPending}>
              {t(`quality.transition.to.${to}`)}
            </Button>
          </div>
          {stepUp.dialog}
        </form>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Кнопки решений по находке. `profile` — профиль качества run (critical запрещает принимать
 * критический риск); undefined — неизвестен, ограничение проверит сервер.
 */
export function FindingTransitionActions({
  finding,
  profile,
  onDone,
  idPrefix = 'finding',
}: {
  finding: Finding;
  profile: string | undefined;
  onDone?: (updated: Finding) => void;
  idPrefix?: string;
}) {
  const { t } = useTranslation();
  const role = useWorkspaceRole();
  const [target, setTarget] = useState<FindingTargetStatus | null>(null);
  const allowed = allowedFindingTransitions(finding.status);

  if (allowed.length === 0) return <p className="text-sm text-fg-muted">{t('quality.transition.engineOnly')}</p>;
  const permitted = allowed.filter((to) => canTransitionFinding(role, to));
  if (permitted.length === 0) return <p className="text-sm text-fg-muted">{t('quality.transition.noRole')}</p>;
  if (finding.version === undefined) return <p className="text-sm text-fg-muted">{t('quality.transition.noVersion')}</p>;
  const blockedId = `${idPrefix}-accepted-risk-blocked`;

  return (
    <div className="flex flex-col gap-2">
      <div role="group" aria-label={t('quality.transition.group')} className="flex flex-wrap gap-3">
        {permitted.map((to) => {
          const blocked = to === 'ACCEPTED_RISK' && acceptedRiskBlockedByProfile(finding.severity, profile);
          return (
            <Button
              key={to}
              variant={to === 'ACCEPTED_RISK' ? 'danger' : 'secondary'}
              disabled={blocked}
              aria-describedby={blocked ? blockedId : undefined}
              onClick={() => setTarget(to)}
            >
              {t(`quality.transition.to.${to}`)}
            </Button>
          );
        })}
      </div>
      {permitted.includes('ACCEPTED_RISK') && acceptedRiskBlockedByProfile(finding.severity, profile) ? (
        <p id={blockedId} className="text-sm text-fg-muted">
          {t('quality.transition.profileBlocked')}
        </p>
      ) : null}
      {allowed.length > permitted.length ? <p className="text-sm text-fg-muted">{t('quality.transition.someNeedAdmin')}</p> : null}
      {target ? (
        <TransitionDialog key={target} finding={finding} to={target} open onOpenChange={(o) => !o && setTarget(null)} onDone={onDone} />
      ) : null}
    </div>
  );
}

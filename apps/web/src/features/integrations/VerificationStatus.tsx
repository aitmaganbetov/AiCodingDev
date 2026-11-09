/**
 * Статус проверки GitHub на воркере (getGitHubVerification, OQ-47 (2)): опрос, пока PENDING; итог — один раз
 * через `onSettled` (вызывающий обновляет список установок/репозиториев).
 */
import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { githubApi, githubKeys, type GitHubVerification } from '../../api/github';
import { ErrorState, LoadingState } from '../../components/states';

/** Интервал опроса проверки, пока она PENDING (воркер обычно отвечает за секунды). */
export const VERIFICATION_POLL_MS = 1500;

export function verificationMessage(t: TFunction, v: GitHubVerification): string {
  if (v.status === 'PENDING') return t('integrations.verification.PENDING');
  if (v.status === 'SUCCEEDED') return t(`integrations.verification.SUCCEEDED_${v.kind}`);
  const code = v.error?.code ?? 'unknown';
  const reason = t(`integrations.verification.errors.${code}`, {
    defaultValue: t('integrations.verification.errors.unknown', { code }),
    detail: v.error?.detail ?? '',
  });
  return t('integrations.verification.FAILED', { reason });
}

const TONE = {
  PENDING: 'border-info-border bg-info-soft text-info-fg',
  SUCCEEDED: 'border-success-border bg-success-soft text-success-fg',
  FAILED: 'border-danger-border bg-danger-soft text-danger-fg',
} as const;

export function VerificationStatus({
  workspaceId,
  verificationId,
  onSettled,
}: {
  workspaceId: string;
  verificationId: string;
  onSettled?: (v: GitHubVerification) => void;
}) {
  const { t } = useTranslation();
  const query = useQuery({
    queryKey: githubKeys.verification(workspaceId, verificationId),
    queryFn: ({ signal }) => githubApi.verification(workspaceId, verificationId, signal),
    refetchInterval: (q) => (q.state.data?.status === 'PENDING' ? VERIFICATION_POLL_MS : false),
  });
  const settled = useRef(false);
  const data = query.data;
  useEffect(() => {
    if (data && data.status !== 'PENDING' && !settled.current) {
      settled.current = true;
      onSettled?.(data);
    }
  }, [data, onSettled]);
  if (query.isPending) return <LoadingState inline label={t('integrations.verification.PENDING')} />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  return (
    <p role="status" aria-live="polite" className={`rounded-md border p-3 text-sm ${TONE[query.data.status]}`} data-status={query.data.status}>
      {verificationMessage(t, query.data)}
    </p>
  );
}

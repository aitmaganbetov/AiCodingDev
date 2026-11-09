import type { UseQueryResult } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import type { AnalyticsSummary } from '../../api/analytics';
import type { Provider } from '../../api/providers';
import { TileError } from '../../components/ui';
import { formatDate } from '../../lib/format';
import { DASHBOARD_WINDOW_DAYS } from '../dashboard/model';
import { formatPercent } from '../dashboard/format';

type ProviderStats = AnalyticsSummary['providers'][number];

/** Строка analytics.providers провайдера; отсутствие строки — «нет исполнений в окне», а не нули. */
export function providerStats(summary: AnalyticsSummary | undefined, code: string): ProviderStats | null {
  return summary?.providers.find((p) => p.provider === code) ?? null;
}

/** Токены — только достоверно сообщённые CLI (tokens.total null ⇒ «CLI не сообщает»). */
export function tokensText(t: (key: string, o?: Record<string, unknown>) => string, s: ProviderStats): string {
  const { total, source, executions_reported } = s.tokens;
  if (total === null || source === 'not_exposed_by_cli') return t('providers.usage.tokensNotExposed');
  const value = total.toLocaleString('ru-RU');
  if (source === 'mixed') return t('providers.usage.tokensMixed', { total: value, reported: executions_reported, executions: s.executions });
  return t('providers.usage.tokensReported', { total: value });
}

/**
 * Измеренное использование провайдера за окно analytics (план §3.9): исполнения, исходы, длительность,
 * ошибки по категориям, токены — только если CLI их сообщил. Квоты/лимиты/остаток подписки не показываются.
 */
export function ProviderUsage({ provider, analytics, window }: { provider: Provider; analytics: UseQueryResult<AnalyticsSummary>; window: { from: string; to: string } }) {
  const { t, i18n } = useTranslation();
  const source = t('providers.usage.source', {
    days: DASHBOARD_WINDOW_DAYS,
    from: formatDate(window.from, i18n.language),
    to: formatDate(window.to, i18n.language),
  });
  if (analytics.isPending)
    return (
      <div aria-busy="true">
        <span className="sr-only">{t('providers.loading')}</span>
        <span aria-hidden="true" className="skeleton block h-24 w-full" />
      </div>
    );
  if (analytics.isError) return <TileError error={analytics.error} onRetry={() => void analytics.refetch()} />;
  const s = providerStats(analytics.data, provider.code);
  const na = t('providers.usage.notMeasured');
  const failures = s ? Object.entries(s.failures_by_category).filter(([, n]) => n > 0) : [];
  return (
    <div className="flex min-w-0 flex-col gap-3" data-testid={`usage-${provider.code}`}>
      {s ? (
        <>
          <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
            {(
              [
                ['executions', s.executions],
                ['collected', s.collected],
                ['successRate', formatPercent(s.success_rate) ?? na],
                ['avgDuration', s.avg_duration_seconds === null ? na : t('providers.usage.seconds', { value: Math.round(s.avg_duration_seconds) })],
                ['errors', s.errors],
                ['rateLimited', s.rate_limited],
                ['authRequired', s.auth_required],
                ['inProgress', s.in_progress],
              ] as const
            ).map(([key, value]) => (
              <div key={key} className="flex min-w-0 flex-col">
                <dt className="text-xs text-fg-muted">{t(`providers.usage.${key}`)}</dt>
                <dd className="font-semibold tabular-nums text-fg" data-metric={key}>
                  {value}
                </dd>
              </div>
            ))}
            <div className="col-span-2 flex min-w-0 flex-col sm:col-span-3">
              <dt className="text-xs text-fg-muted">{t('providers.usage.tokens')}</dt>
              <dd className="text-fg" data-metric="tokens">
                {tokensText(t, s)}
              </dd>
            </div>
          </dl>
          <div>
            <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-fg-muted">{t('providers.usage.failures')}</h4>
            {failures.length === 0 ? (
              <p className="text-sm text-fg-muted">{t('providers.usage.noFailures')}</p>
            ) : (
              <ul className="flex flex-wrap gap-1.5 text-xs">
                {failures.map(([code, n]) => (
                  <li key={code} className="rounded-full border border-danger-border bg-danger-soft px-2 py-0.5 text-danger-fg">
                    {t(`providers.enums.cli_error.${code}`, { defaultValue: code })}: <span className="tabular-nums">{n}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      ) : (
        <p className="text-sm text-fg-muted" data-testid="usage-none">
          {t('providers.usage.none')}
        </p>
      )}
      <p className="text-xs text-fg-subtle">{source}</p>
    </div>
  );
}

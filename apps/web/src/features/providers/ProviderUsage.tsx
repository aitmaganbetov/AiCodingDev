import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { analyticsApi, analyticsKeys, type AnalyticsSummary } from '../../api/analytics';
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

/** Скользящие окна расхода токенов: последний час и последние 7 дней. */
export const TOKEN_WINDOWS = [
  { key: 'hour', hours: 1 },
  { key: 'week', hours: 24 * 7 },
] as const;
const MINUTE_MS = 60_000;

/**
 * Окно «последние N часов»: конец — начало следующей минуты (ключ кэша стабилен в пределах минуты,
 * а только что завершившееся исполнение уже попадает в окно).
 */
export function recentWindow(hours: number, now: number): { from: string; to: string } {
  const end = Math.floor(now / MINUTE_MS) * MINUTE_MS + MINUTE_MS;
  return { from: new Date(end - hours * 3_600_000).toISOString(), to: new Date(end).toISOString() };
}

function TokenWindow({ workspaceId, code, hours, label, now }: { workspaceId: string; code: string; hours: number; label: string; now: number }) {
  const { t } = useTranslation();
  const window = recentWindow(hours, now);
  const query = useQuery({
    queryKey: analyticsKeys.summary(workspaceId, window),
    queryFn: ({ signal }) => analyticsApi.summary(workspaceId, window, signal),
    // Прежнее значение остаётся на экране, пока перечитывается окно следующей минуты.
    placeholderData: (previous) => previous,
  });
  const s = providerStats(query.data, code);
  return (
    <div className="flex min-w-0 flex-col rounded-md border border-border p-3" data-window={hours}>
      <dt className="text-xs text-fg-muted">{label}</dt>
      <dd className="text-fg">
        {query.isPending ? (
          <span aria-hidden="true" className="skeleton block h-5 w-24" />
        ) : query.isError ? (
          <span className="text-sm text-danger-text">{t('providers.usage.windowError')}</span>
        ) : s ? (
          <>
            <span className="block font-semibold tabular-nums" data-metric="tokens">
              {tokensText(t, s)}
            </span>
            <span className="block text-xs text-fg-muted">{t('providers.usage.windowExecutions', { count: s.executions })}</span>
          </>
        ) : (
          <span className="text-sm text-fg-muted">{t('providers.usage.windowNone')}</span>
        )}
      </dd>
    </div>
  );
}

/**
 * Расход токенов провайдера за последний час и за 7 дней — измеренный платформой по исполнениям её задач
 * (только то, что сообщил сам CLI). Это не остаток лимита подписки: его CLI платформе не сообщает, а
 * запросы вне платформы (свой терминал, другие устройства) сюда не попадают — об этом сказано в подписи.
 */
export function TokenWindows({ workspaceId, provider }: { workspaceId: string; provider: Provider }) {
  const { t } = useTranslation();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), MINUTE_MS);
    return () => clearInterval(timer);
  }, []);
  return (
    <div className="flex flex-col gap-2" data-testid={`token-windows-${provider.code}`}>
      <dl className="grid gap-3 sm:grid-cols-2">
        {TOKEN_WINDOWS.map((w) => (
          <TokenWindow key={w.key} workspaceId={workspaceId} code={provider.code} hours={w.hours} label={t(`providers.usage.window.${w.key}`)} now={now} />
        ))}
      </dl>
      <p className="text-xs text-fg-subtle">{t('providers.usage.windowNote')}</p>
    </div>
  );
}

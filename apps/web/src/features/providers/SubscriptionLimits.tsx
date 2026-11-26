/**
 * Лимиты подписки CLI (сессия, неделя): доля израсходованного окна и время сброса — как их сообщил сам CLI.
 *
 * Платформа эти числа не вычисляет и к API провайдера не обращается: Claude Code печатает их в своём выводе
 * (при каждом исполнении задачи и по запросу «Обновить» — один минимальный запрос CLI, он расходует
 * подписку); Codex отвечает о них через `codex app-server` без запроса к модели и сообщает ещё и кредиты.
 * Подписка расходуется и вне платформы, поэтому значение — наблюдение на момент «обновлено», а не гарантия.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { IdempotentIntent } from '../../api/client';
import { providerKeys, providersApi, type Provider, type SubscriptionLimits as Limits, type SubscriptionLimitWindow } from '../../api/providers';
import { Button } from '../../components/ui';
import { formatDateTime, formatRelative } from '../../lib/format';
import { can } from '../../lib/permissions';
import { problemMessage } from '../../lib/problem';

/** Провайдеры, CLI которых сообщает лимиты подписки. */
export const LIMITS_READABLE: readonly string[] = ['claude', 'codex'];
/** Чтение лимитов делает запрос к модели и расходует подписку (Claude Code); у Codex оно бесплатно. */
export const LIMITS_COSTLY: readonly string[] = ['claude'];
const POLL_MS = 2_000;
const FINAL: readonly string[] = ['COMPLETED', 'FAILED', 'EXPIRED'];

/** Самое свежее наблюдение среди установок провайдера. */
export function latestLimits(provider: Pick<Provider, 'installations'>): Limits | null {
  let latest: Limits | null = null;
  for (const installation of provider.installations) {
    const observed = installation.subscription_limits ?? null;
    if (observed && (!latest || Date.parse(observed.observed_at) > Date.parse(latest.observed_at))) latest = observed;
  }
  return latest;
}

/** Подпись окна: «Сессия · 5 ч», «Неделя», «Неделя · Opus»; незнакомое окно показывается как есть. */
export function windowLabel(t: TFunction, kind: string): string {
  if (kind === 'five_hour') return t('providers.limits.window.five_hour');
  if (kind === 'seven_day') return t('providers.limits.window.seven_day');
  if (kind.startsWith('seven_day_')) {
    const model = kind.slice('seven_day_'.length).replaceAll('_', ' ');
    return t('providers.limits.window.seven_day_model', { model: model.charAt(0).toUpperCase() + model.slice(1) });
  }
  const minutes = /^window_(\d+)m$/.exec(kind)?.[1];
  if (minutes) return t('providers.limits.window.window_other', { kind: untilReset(t, new Date(Number(minutes) * 60_000).toISOString(), 0) ?? kind });
  return kind;
}

/** «2 ч 18 мин», «4 дн 15 ч»; null — время сброса неизвестно или уже прошло. */
export function untilReset(t: TFunction, resetsAt: string | null, now: number): string | null {
  if (!resetsAt) return null;
  const minutes = Math.floor((Date.parse(resetsAt) - now) / 60_000);
  if (!Number.isFinite(minutes) || minutes <= 0) return null;
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  if (days > 0) return t('providers.limits.daysHours', { days, hours });
  if (hours > 0) return t('providers.limits.hoursMinutes', { hours, minutes: minutes % 60 });
  return t('providers.limits.minutes', { minutes });
}

function WindowRow({ window, now }: { window: SubscriptionLimitWindow; now: number }) {
  const { t, i18n } = useTranslation();
  const used = Math.round(window.used * 100);
  const reset = untilReset(t, window.resets_at, now);
  const label = windowLabel(t, window.kind);
  const tone = used >= 90 ? 'bg-danger' : used >= 70 ? 'bg-warning' : 'bg-accent';
  return (
    <li className="flex flex-col gap-1" data-window={window.kind}>
      <div className="flex items-baseline justify-between gap-2 text-sm">
        <span>{label}</span>
        <span className="font-semibold tabular-nums" data-metric="used">
          {used}%
        </span>
      </div>
      <div
        role="progressbar"
        aria-label={t('providers.limits.barLabel', { window: label })}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={used}
        className="h-1.5 w-full overflow-hidden rounded-full bg-surface-muted"
      >
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${used}%` }} />
      </div>
      <p className="text-xs text-fg-muted">
        {t('providers.limits.left', { percent: 100 - used })}
        {reset && window.resets_at
          ? ` · ${t('providers.limits.resetIn', { time: reset, at: formatDateTime(window.resets_at, i18n.language) })}`
          : window.resets_at
            ? ` · ${t('providers.limits.resetPassed')}`
            : ''}
      </p>
    </li>
  );
}

export function SubscriptionLimits({ workspaceId, provider, role }: { workspaceId: string; provider: Provider; role: string }) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const intent = useRef(new IdempotentIntent());
  const [requestId, setRequestId] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, []);
  const readable = LIMITS_READABLE.includes(provider.code);
  const limits = latestLimits(provider);

  const refresh = useMutation({
    mutationFn: () => providersApi.refreshLimits(workspaceId, provider.provider_id, intent.current.keyFor({ limits: provider.provider_id })),
    onSuccess: (accepted) => {
      intent.current.complete();
      setRequestId(accepted.probe_request_id);
    },
  });
  // Обновление — запрос проверки на worker-exec: ждём его итог, затем перечитываем список провайдеров.
  const status = useQuery({
    queryKey: providerKeys.probe(workspaceId, provider.provider_id, requestId ?? ''),
    queryFn: ({ signal }) => providersApi.probeStatus(workspaceId, provider.provider_id, requestId ?? '', signal),
    enabled: requestId !== null,
    refetchInterval: (q) => (q.state.data && FINAL.includes(q.state.data.status) ? false : POLL_MS),
  });
  const final = status.data && FINAL.includes(status.data.status) ? status.data.status : null;
  useEffect(() => {
    if (final) void queryClient.invalidateQueries({ queryKey: providerKeys.list(workspaceId) });
  }, [final, queryClient, workspaceId]);
  const waiting = refresh.isPending || (requestId !== null && !final && !status.isError);

  return (
    <div className="flex flex-col gap-3" data-testid={`limits-${provider.code}`}>
      {!readable ? (
        <p className="text-sm text-fg-muted">{t('providers.limits.notReported', { name: provider.display_name })}</p>
      ) : limits ? (
        <>
          <ul className="flex flex-col gap-3">
            {limits.windows.map((w) => (
              <WindowRow key={w.kind} window={w} now={now} />
            ))}
          </ul>
          {limits.credits ? (
            <p className="flex items-baseline justify-between gap-2 text-sm" data-testid={`credits-${provider.code}`}>
              <span>{t('providers.limits.credits')}</span>
              <span className="font-semibold tabular-nums">
                {limits.credits.unlimited
                  ? t('providers.limits.creditsUnlimited')
                  : (limits.credits.balance ?? t('providers.limits.creditsUnknown'))}
              </span>
            </p>
          ) : null}
          <p className="text-xs text-fg-subtle">
            {t('providers.limits.observed', { ago: formatRelative(limits.observed_at, i18n.language, now) })} · {t('providers.limits.note')}
          </p>
        </>
      ) : (
        <p className="text-sm text-fg-muted">{t('providers.limits.none')}</p>
      )}
      {readable && can(role, 'provider:probe') ? (
        <div className="flex flex-wrap items-center gap-3">
          <Button type="button" variant="secondary" onClick={() => refresh.mutate()} loading={waiting}>
            {t('providers.limits.refresh')}
          </Button>
          <p className="text-xs text-fg-muted">
            {t(LIMITS_COSTLY.includes(provider.code) ? 'providers.limits.refreshHint' : 'providers.limits.refreshHintFree')}
          </p>
        </div>
      ) : null}
      {refresh.isError || status.isError || final === 'FAILED' || final === 'EXPIRED' ? (
        <p role="alert" className="text-sm font-medium text-danger-text">
          {refresh.isError ? problemMessage(t, refresh.error) : status.isError ? problemMessage(t, status.error) : t('providers.limits.refreshFailed')}
        </p>
      ) : null}
    </div>
  );
}

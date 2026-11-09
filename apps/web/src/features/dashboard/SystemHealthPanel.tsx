import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import type { SystemHealthStatus } from '../../api/system';
import { Badge, RelativeTime, Tile, type IconName } from '../../components/ui';
import { formatDateTime } from '../../lib/format';
import { useSystemHealth } from './data';
import { HEALTH_TONE, healthStatus } from './model';

const HEALTH_ICON: Record<SystemHealthStatus, IconName> = { ok: 'statusSuccess', degraded: 'alert', down: 'statusFailed', unknown: 'help' };

function HealthBadge({ status }: { status: string }) {
  const { t } = useTranslation();
  const s = healthStatus(status);
  return (
    <Badge tone={HEALTH_TONE[s]} icon={HEALTH_ICON[s]} data-health={s}>
      {t(`dashboard.health.status.${s}`)}
    </Badge>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <h3 className="text-sm font-semibold text-fg">{title}</h3>
      {children}
    </div>
  );
}

/**
 * Состояние системы (B2 GET /system/health): только для ролей с system:health (owner/admin) —
 * для остальных компонент не рендерится и запрос не выполняется. «Неизвестно» остаётся
 * «неизвестно» (не измерено ≠ работает); null-разделы ответа — «не измерено».
 */
export function SystemHealthPanel({ workspaceId, role }: { workspaceId: string; role: string }) {
  const { t, i18n } = useTranslation();
  const q = useSystemHealth(workspaceId, role);
  const h = q.data;
  return (
    <Tile
      id="dashboard-health"
      icon="system"
      headingLevel={2}
      title={t('dashboard.health.title')}
      className="xl:col-span-12"
      loading={q.isPending}
      error={q.error}
      onRetry={() => void q.refetch()}
      source={t('dashboard.health.unknownNote')}
      actions={h ? <HealthBadge status={h.status} /> : undefined}
    >
      <p className="-mt-2 text-sm text-fg-muted">{t('dashboard.health.subtitle')}</p>
      {h ? (
        <>
          <p className="text-xs text-fg-muted">
            {t('dashboard.health.checked')} <RelativeTime value={h.checked_at} /> · {t('dashboard.health.cacheTtl', { ttl: h.cache_ttl_seconds })}
          </p>
          <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-4">
            <Section title={t('dashboard.health.components')}>
              {h.components.length === 0 ? (
                <p className="text-sm text-fg-muted">{t('dashboard.health.notMeasured')}</p>
              ) : (
                <ul className="flex flex-col gap-2" aria-label={t('dashboard.health.components')}>
                  {h.components.map((c) => (
                    <li key={c.name} className="flex flex-col gap-0.5 text-sm" data-component={c.name}>
                      <span className="flex flex-wrap items-center justify-between gap-2">
                        <span className="font-medium text-fg">{t(`dashboard.health.component.${c.name}`, { defaultValue: c.name })}</span>
                        <HealthBadge status={c.status} />
                      </span>
                      {c.latency_ms !== null || c.detail ? (
                        <span className="break-words text-xs text-fg-muted">
                          {[c.latency_ms !== null ? t('dashboard.health.latency', { ms: c.latency_ms }) : null, c.detail].filter(Boolean).join(' · ')}
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
            </Section>
            <Section title={t('dashboard.health.workers')}>
              {h.worker_kinds === null ? (
                <p className="text-sm text-fg-muted">{t('dashboard.health.notMeasured')}</p>
              ) : (
                <ul className="flex flex-col gap-2" aria-label={t('dashboard.health.workers')}>
                  {h.worker_kinds.map((w) => (
                    <li key={w.kind} className="flex flex-col gap-0.5 text-sm" data-worker-kind={w.kind}>
                      <span className="flex flex-wrap items-center justify-between gap-2">
                        <span className="font-medium text-fg">{t(`dashboard.health.workerKind.${w.kind}`, { defaultValue: w.kind })}</span>
                        <HealthBadge status={w.status} />
                      </span>
                      <span className="text-xs text-fg-muted">
                        {w.expected
                          ? t('dashboard.health.workerLine', {
                              active: w.active_hosts,
                              hosts: w.hosts,
                              age:
                                w.freshest_heartbeat_age_seconds === null
                                  ? t('dashboard.health.workerAgeUnknown')
                                  : t('dashboard.health.workerAge', { s: Math.round(w.freshest_heartbeat_age_seconds) }),
                            })
                          : t('dashboard.health.notExpected')}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </Section>
            <Section title={t('dashboard.health.queues')}>
              {h.queues === null ? (
                <p className="text-sm text-fg-muted">{t('dashboard.health.notMeasured')}</p>
              ) : (
                <ul className="flex flex-col gap-2" aria-label={t('dashboard.health.queues')}>
                  {h.queues.map((qh) => (
                    <li key={qh.queue} className="flex flex-col gap-0.5 text-sm" data-queue={qh.queue}>
                      <span className="break-all font-mono text-code-sm text-fg">{qh.queue}</span>
                      <span className="text-xs text-fg-muted">
                        {t('dashboard.health.queueLine', { ready: qh.ready, delayed: qh.delayed, dead: qh.dead_letters })}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </Section>
            <Section title={t('dashboard.health.backups')}>
              {h.backups === null ? (
                <p className="text-sm text-fg-muted">{t('dashboard.health.notMeasured')}</p>
              ) : (
                <ul className="flex flex-col gap-2" aria-label={t('dashboard.health.backups')}>
                  {h.backups.map((b) => (
                    <li key={b.component} className="flex flex-col gap-0.5 text-sm" data-backup={b.component}>
                      <span className="flex flex-wrap items-center justify-between gap-2">
                        <span className="break-all font-mono text-code-sm text-fg">{b.component}</span>
                        <Badge
                          tone={b.last_status === 'success' && !b.stale ? 'success' : b.last_status === 'failure' ? 'danger' : b.stale ? 'warning' : 'neutral'}
                          icon={b.last_status === 'success' && !b.stale ? 'statusSuccess' : b.last_status === 'failure' ? 'statusFailed' : b.stale ? 'alert' : 'help'}
                        >
                          {t(`dashboard.health.backupStatus.${b.last_status}`)}
                          {b.stale ? `, ${t('dashboard.health.stale')}` : ''}
                        </Badge>
                      </span>
                      <span className="text-xs text-fg-muted">
                        {b.last_success_at
                          ? t('dashboard.health.backupLine', { at: formatDateTime(b.last_success_at, i18n.language) })
                          : t('dashboard.health.backupNever')}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </Section>
          </div>
        </>
      ) : null}
    </Tile>
  );
}

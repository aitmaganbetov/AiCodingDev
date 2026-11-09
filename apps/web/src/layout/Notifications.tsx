import { useEffect, useRef, useState } from 'react';
import type { TFunction } from 'i18next';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import type { StreamEvent } from '../api/sse';
import { Icon, type IconName } from '../components/ui/Icon';
import { Popover, PopoverContent, PopoverTrigger } from '../components/ui/Popover';
import { RelativeTime } from '../components/ui/Time';
import { cx } from '../components/ui/cx';
import { useCurrentWorkspace } from '../workspace';
import { usePendingApprovals, useWorkspaceNotifications } from './shellData';

function str(v: unknown): string | null {
  return typeof v === 'string' && v.length > 0 ? v : null;
}

/** Текст, иконка и ссылка уведомления из события потока workspace (только поля контракта). */
export function describeNotification(t: TFunction, workspaceId: string, e: StreamEvent): { text: string; href: string; icon: IconName } {
  const p = e.payload;
  const runHref = `/w/${workspaceId}/mission-control?run=${e.run_id}`;
  switch (e.type) {
    case 'approval.requested': {
      const id = str(p.approval_id);
      return {
        text: t('shell.notifications.approvalRequested', { summary: str(p.summary) ?? t(`approvals.enums.action.${String(p.action_type)}`, { defaultValue: String(p.action_type) }) }),
        href: id ? `/w/${workspaceId}/approvals/${id}` : `/w/${workspaceId}/approvals`,
        icon: 'approval',
      };
    }
    case 'approval.decided': {
      const id = str(p.approval_id);
      return {
        text: t('shell.notifications.approvalDecided', {
          action: t(`approvals.enums.action.${String(p.action_type)}`, { defaultValue: String(p.action_type) }),
          status: t(`approvals.enums.status.${String(p.status)}`, { defaultValue: String(p.status) }),
        }),
        href: id ? `/w/${workspaceId}/approvals/${id}` : `/w/${workspaceId}/approvals`,
        icon: 'check',
      };
    }
    case 'run.completed':
      return {
        text: t('shell.notifications.runCompleted', { status: t(`enums.run_status.${String(p.final_status)}`, { defaultValue: String(p.final_status) }) }),
        href: runHref,
        icon: 'statusSuccess',
      };
    case 'run.state_changed':
      return {
        text: t('shell.notifications.runStateChanged', { status: t(`enums.run_status.${String(p.to_status)}`, { defaultValue: String(p.to_status) }) }),
        href: runHref,
        icon: 'activity',
      };
    default:
      return { text: e.type, href: runHref, icon: 'info' };
  }
}

/**
 * Уведомления (план §3.1): события потока workspace (`GET /workspaces/{id}/events`: approvals и
 * статусы runs) + число approvals, ожидающих моего решения. Отметка «прочитано» хранится только в
 * этом браузере (серверных уведомлений пока нет — B8); это указано в панели.
 */
export function NotificationsBell() {
  const { t } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();
  const { events, unread, state, markAllRead } = useWorkspaceNotifications(workspaceId && membership ? workspaceId : null);
  const approvals = usePendingApprovals(workspaceId, membership?.role);
  const [open, setOpen] = useState(false);
  // Вежливое объявление только при росте числа непрочитанных (не на каждое событие).
  const [announce, setAnnounce] = useState('');
  const prevUnread = useRef(unread);
  useEffect(() => {
    if (unread > prevUnread.current) setAnnounce(t('shell.notifications.announce', { count: unread }));
    prevUnread.current = unread;
  }, [t, unread]);

  if (!workspaceId || !membership) return null;
  const list = [...events].reverse();
  const label = unread > 0 ? t('shell.notifications.labelUnread', { count: unread }) : t('shell.notifications.label');

  return (
    <>
      <span role="status" className="sr-only">
        {announce}
      </span>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger className="btn-ghost relative min-w-touch px-2" aria-label={label} data-testid="notifications-trigger">
          <Icon name="bell" />
          {unread > 0 ? (
            <span
              aria-hidden="true"
              data-testid="notifications-count"
              className="absolute right-1 top-1 min-w-[1.125rem] rounded-full bg-accent px-1 text-center text-[0.6875rem] font-semibold leading-[1.125rem] text-accent-fg tabular-nums"
            >
              {unread > 99 ? '99+' : unread}
            </span>
          ) : null}
        </PopoverTrigger>
        <PopoverContent label={t('shell.notifications.title')} align="end" className="w-[min(24rem,calc(100vw-1rem))] p-0">
          <div className="flex items-center gap-2 border-b border-border px-3 py-2">
            <h2 className="mr-auto text-base font-semibold">{t('shell.notifications.title')}</h2>
            <button type="button" className="btn-ghost btn-sm" onClick={markAllRead} disabled={unread === 0}>
              {t('shell.notifications.markRead')}
            </button>
          </div>
          {approvals.count !== null && approvals.count > 0 ? (
            <Link
              to={`/w/${workspaceId}/approvals?mine=1`}
              onClick={() => setOpen(false)}
              className="flex min-h-touch items-center gap-2 border-b border-border bg-warning-soft px-3 text-sm font-medium text-warning-fg no-underline"
              data-testid="notifications-approvals"
            >
              <Icon name="approval" className="h-4 w-4" />
              {t('shell.notifications.pendingApprovals', { count: approvals.count, more: approvals.more ? '+' : '' })}
            </Link>
          ) : null}
          {list.length === 0 ? (
            <p className="px-3 py-4 text-sm text-fg-muted" data-testid="notifications-empty">
              {t('shell.notifications.empty')}
            </p>
          ) : (
            <ul className="max-h-[50vh] overflow-y-auto" aria-label={t('shell.notifications.listLabel')}>
              {list.map((e) => {
                const n = describeNotification(t, workspaceId, e);
                return (
                  <li key={e.event_id} className="border-b border-border last:border-b-0">
                    <Link
                      to={n.href}
                      onClick={() => setOpen(false)}
                      className={cx('flex gap-2 px-3 py-2 text-sm text-fg no-underline hover:bg-surface-muted')}
                      data-testid="notification-item"
                    >
                      <Icon name={n.icon} className="mt-0.5 h-4 w-4 shrink-0 text-fg-muted" />
                      <span className="flex min-w-0 flex-col">
                        <span className="break-words">{n.text}</span>
                        <RelativeTime value={e.timestamp} className="text-xs text-fg-muted" />
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          )}
          <p className="border-t border-border px-3 py-2 text-xs text-fg-muted">
            {state === 'shared' ? t('shell.notifications.sharedStream') : null} {t('shell.notifications.deviceNote')}
          </p>
        </PopoverContent>
      </Popover>
    </>
  );
}

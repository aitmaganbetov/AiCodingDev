import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { notificationHref, type Notification, type NotificationSeverity } from '../api/notifications';
import { Icon, type IconName } from '../components/ui/Icon';
import { Popover, PopoverContent, PopoverTrigger } from '../components/ui/Popover';
import { RelativeTime } from '../components/ui/Time';
import { cx } from '../components/ui/cx';
import { problemMessage } from '../lib/problem';
import { useCurrentWorkspace } from '../workspace';
import { useShellNotifications } from './NotificationsData';
import { usePendingApprovals } from './shellData';

const SEVERITY_ICON: Record<NotificationSeverity, { icon: IconName; className: string }> = {
  info: { icon: 'info', className: 'text-fg-muted' },
  success: { icon: 'statusSuccess', className: 'text-success-fg' },
  warning: { icon: 'alert', className: 'text-warning-fg' },
  error: { icon: 'statusFailed', className: 'text-danger-fg' },
};

function severityIcon(severity: string): { icon: IconName; className: string } {
  return SEVERITY_ICON[severity as NotificationSeverity] ?? SEVERITY_ICON.info;
}

function NotificationItem({
  workspaceId,
  n,
  canUpdate,
  disabled,
  onOpen,
  onMarkRead,
}: {
  workspaceId: string;
  n: Notification;
  canUpdate: boolean;
  disabled: boolean;
  onOpen: () => void;
  onMarkRead: () => void;
}) {
  const { t } = useTranslation();
  const sev = severityIcon(n.severity);
  return (
    <li className={cx('flex items-start border-b border-border last:border-b-0', n.read ? 'border-l-2 border-l-transparent' : 'border-l-2 border-l-accent')} data-testid="notification-item" data-read={n.read ? 'true' : 'false'}>
      <Link
        to={notificationHref(workspaceId, n)}
        onClick={() => {
          if (!n.read && canUpdate) onMarkRead();
          onOpen();
        }}
        className="flex min-w-0 flex-1 gap-2 px-3 py-2 text-sm text-fg no-underline hover:bg-surface-muted"
      >
        <Icon name={sev.icon} className={cx('mt-0.5 h-4 w-4 shrink-0', sev.className)} />
        <span className="flex min-w-0 flex-col">
          <span className={cx('break-words', !n.read && 'font-semibold')}>
            {!n.read ? <span className="sr-only">{t('shell.notifications.unreadMark')} </span> : null}
            {n.title}
          </span>
          {n.body ? <span className="break-words text-fg-muted">{n.body}</span> : null}
          <RelativeTime value={n.created_at} className="text-xs text-fg-muted" />
        </span>
      </Link>
      {!n.read && canUpdate ? (
        <button
          type="button"
          className="btn-ghost btn-sm m-1 min-w-touch shrink-0 px-2"
          onClick={onMarkRead}
          disabled={disabled}
          aria-label={t('shell.notifications.markOne', { title: n.title })}
          title={t('shell.notifications.markOneShort')}
        >
          <Icon name="check" className="h-4 w-4" />
        </button>
      ) : null}
    </li>
  );
}

/**
 * Уведомления (план §3.1, UI redesign B8): персональные уведомления с сервера (`GET /notifications`,
 * `GET /notifications/unread-count`), live — кадры `notification.created` потока workspace, отметка
 * «прочитано» — `POST /notifications/mark-read` (общая для всех устройств), плюс число approvals,
 * ожидающих моего решения.
 */
export function NotificationsBell() {
  const { t } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();
  const [open, setOpen] = useState(false);
  const n = useShellNotifications(workspaceId && membership ? workspaceId : null, membership?.role, { listOpen: open });
  const approvals = usePendingApprovals(workspaceId, membership?.role);
  const unread = n.unread ?? 0;
  // Вежливое объявление только при росте числа непрочитанных (не на каждое событие).
  const [announce, setAnnounce] = useState('');
  const prevUnread = useRef<number | null>(null);
  useEffect(() => {
    if (prevUnread.current !== null && unread > prevUnread.current) setAnnounce(t('shell.notifications.announce', { count: unread }));
    prevUnread.current = n.unread === null ? prevUnread.current : unread;
  }, [t, unread, n.unread]);

  if (!workspaceId || !membership) return null;
  const label = unread > 0 ? t('shell.notifications.labelUnread', { count: unread }) : t('shell.notifications.label');
  const items = n.items ?? [];

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
            {n.canUpdate ? (
              <button type="button" className="btn-ghost btn-sm" onClick={n.markAllRead} disabled={unread === 0 || n.marking}>
                {t('shell.notifications.markRead')}
              </button>
            ) : null}
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
          {n.markError ? (
            <p role="alert" className="border-b border-border px-3 py-2 text-sm text-danger-fg" data-testid="notifications-mark-error">
              {t('shell.notifications.markFailed', { reason: problemMessage(t, n.markError) })}
            </p>
          ) : null}
          {n.listError ? (
            <div role="alert" className="flex flex-col gap-2 px-3 py-4 text-sm" data-testid="notifications-error">
              <span>{t('shell.notifications.loadFailed', { reason: problemMessage(t, n.listError) })}</span>
              <button type="button" className="btn-secondary btn-sm self-start" onClick={n.refetchList}>
                {t('shell.notifications.retry')}
              </button>
            </div>
          ) : n.listPending && n.items === undefined ? (
            <p className="px-3 py-4 text-sm text-fg-muted" aria-busy="true" data-testid="notifications-loading">
              {t('shell.notifications.loading')}
            </p>
          ) : items.length === 0 ? (
            <p className="px-3 py-4 text-sm text-fg-muted" data-testid="notifications-empty">
              {t('shell.notifications.empty')}
            </p>
          ) : (
            <ul className="max-h-[50vh] overflow-y-auto" aria-label={t('shell.notifications.listLabel')}>
              {items.map((item) => (
                <NotificationItem
                  key={item.id}
                  workspaceId={workspaceId}
                  n={item}
                  canUpdate={n.canUpdate}
                  disabled={n.marking}
                  onOpen={() => setOpen(false)}
                  onMarkRead={() => n.markRead([item.id])}
                />
              ))}
            </ul>
          )}
          {n.hasMore || n.state === 'shared' ? (
            <p className="border-t border-border px-3 py-2 text-xs text-fg-muted">
              {n.hasMore ? t('shell.notifications.latestOnly', { count: items.length }) : null}{' '}
              {n.state === 'shared' ? t('shell.notifications.sharedStream') : null}
            </p>
          ) : null}
        </PopoverContent>
      </Popover>
    </>
  );
}

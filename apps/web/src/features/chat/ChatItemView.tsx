import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import type { AgentRole } from '../../api/runs';
import { AgentAvatar, Badge, StatusBadge, Timestamp, cx } from '../../components/ui';
import { providerLabel, roleLabel } from '../runs/labels';
import type { TimelineItem } from './chatModel';

interface Props {
  item: TimelineItem;
  taskTitle: (taskId: string) => string | undefined;
  taskHref: (taskId: string, tab?: string) => string;
  onMention: (role: AgentRole) => void;
}

const REF_TAB: Record<string, string> = { diff: 'diff', evidence: 'evidence', test_report: 'evidence', artifact: 'evidence', gate_execution: 'evidence', task: 'overview' };

/**
 * Элемент ленты. Тело (`body`) — санитизированный, но недоверенный текст агента/CLI/пользователя:
 * выводится только как текст (React экранирует), без HTML/markdown и без ссылок из содержимого.
 */
export function ChatItemView({ item, taskTitle, taskHref, onMention }: Props) {
  const { t } = useTranslation();
  const sender =
    item.sender_type === 'user'
      ? (item.sender?.user?.display_name ?? t('chat.sender.user'))
      : item.sender_type === 'agent'
        ? [roleLabel(t, item.sender?.role) || t('chat.sender.agent'), providerLabel(t, item.sender?.provider)].filter(Boolean).join(' · ')
        : t('chat.sender.system');
  const headingId = `chat-item-${item.id}`;
  return (
    <article
      aria-labelledby={headingId}
      // Новое live-сообщение появляется мягко (opacity, 120 мс; при reduced motion — без анимации).
      className={cx('flex flex-col gap-1.5 px-3 py-3 text-sm', item.live && 'animate-fade-in')}
      data-testid="chat-item"
      data-item-id={item.id}
      data-item-type={item.item_type}
      data-live={item.live ? 'true' : undefined}
    >
      <header className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <AgentAvatar agentRole={item.sender_type === 'agent' ? item.sender?.role : item.sender_type === 'user' ? 'user' : 'system'} size="sm" />
        <h3 id={headingId} className="font-semibold text-fg">
          {sender}
          {item.recipient_role ? (
            <span className="font-normal text-fg-muted"> {t('chat.to', { role: roleLabel(t, item.recipient_role) })}</span>
          ) : null}
        </h3>
        {item.acp_type ? <Badge tone="info">{t(`chat.enums.acp_type.${item.acp_type}`, { defaultValue: item.acp_type })}</Badge> : null}
        <Badge tone="neutral">{t(`chat.enums.item_type.${item.item_type}`, { defaultValue: item.item_type })}</Badge>
        <Timestamp value={item.created_at} format="time" className="text-xs text-fg-muted" />
        {item.live ? <span className="text-xs text-fg-muted">{t('chat.live')}</span> : null}
      </header>
      <p className="whitespace-pre-wrap break-words text-fg" data-testid="chat-item-body">
        {item.body}
      </p>
      {(item.mentions?.length ?? 0) > 0 || item.directive_status || item.approval_status || item.task_id || (item.refs?.length ?? 0) > 0 ? (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {(item.mentions ?? []).map((role) => (
            <button
              key={role}
              type="button"
              className="rounded-full border border-accent/40 bg-accent-soft px-2 py-0.5 font-medium text-accent-soft-fg hover:underline"
              onClick={() => onMention(role)}
              aria-label={t('chat.mentionFilter', { role: roleLabel(t, role) })}
            >
              @{roleLabel(t, role)}
            </button>
          ))}
          {item.directive_status ? (
            <span className="inline-flex items-center gap-1">
              {t('chat.directiveStatus')}{' '}
              <StatusBadge kind="directive" status={item.directive_status} labelKey={`directives.enums.status.${item.directive_status}`} />
            </span>
          ) : null}
          {item.approval_status ? (
            <span className="inline-flex items-center gap-1">
              {t('chat.approvalStatus')}{' '}
              <StatusBadge kind="approval" status={item.approval_status} labelKey={`chat.enums.approval_status.${item.approval_status}`} />
            </span>
          ) : null}
          {item.task_id ? (
            <Link to={taskHref(item.task_id)} className="link">
              {t('chat.taskLink', { task: taskTitle(item.task_id) ?? t('run.unknownTask', { id: item.task_id.slice(0, 8) }) })}
            </Link>
          ) : null}
          {(item.refs ?? []).map((ref) =>
            (item.task_id && ref.type !== 'approval') || ref.type === 'task' ? (
              <Link key={`${ref.type}-${ref.id}`} to={taskHref(ref.type === 'task' ? ref.id : (item.task_id ?? ''), REF_TAB[ref.type])} className="link">
                {t(`chat.enums.ref.${ref.type}`, { defaultValue: ref.type })}
              </Link>
            ) : (
              <span key={`${ref.type}-${ref.id}`} className="text-fg-muted">
                {t(`chat.enums.ref.${ref.type}`, { defaultValue: ref.type })} {ref.id.slice(0, 8)}
              </span>
            ),
          )}
        </div>
      ) : null}
    </article>
  );
}

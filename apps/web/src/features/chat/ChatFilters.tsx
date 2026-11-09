import { useId, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { CHAT_ITEM_TYPES, CHAT_STREAMS, type ChatFilters, type ChatItemType, type ChatThread } from '../../api/chat';
import type { AgentRole, TaskSummary } from '../../api/runs';
import { roleLabel } from '../runs/labels';
import { AGENT_ROLES, CHAT_Q_MAX } from './chatModel';

interface Props {
  filters: ChatFilters;
  onChange: (next: ChatFilters) => void;
  tasks: readonly TaskSummary[];
  threads: readonly ChatThread[];
}

/** Потоки (FR-CHAT-01), фильтры по роли/задаче/типу/ветке и полнотекстовый поиск (FR-CHAT-06). */
export function ChatFiltersPanel({ filters, onChange, tasks, threads }: Props) {
  const { t } = useTranslation();
  const id = useId();
  const [q, setQ] = useState(filters.q ?? '');
  const [prevQ, setPrevQ] = useState(filters.q);
  if (prevQ !== filters.q) {
    setPrevQ(filters.q);
    setQ(filters.q ?? '');
  }
  const taskTitle = new Map(tasks.map((x) => [x.id, x.title]));
  const threadLabel = (th: ChatThread) =>
    th.scope === 'role' && th.target_role
      ? t('chat.thread.role', { role: roleLabel(t, th.target_role) })
      : th.scope === 'task' && th.task_id
        ? t('chat.thread.task', { task: taskTitle.get(th.task_id) ?? th.task_id.slice(0, 8) })
        : t('chat.thread.run');
  const submit = (e: FormEvent) => {
    e.preventDefault();
    onChange({ ...filters, q: q.trim() || undefined });
  };
  const toggleType = (type: ChatItemType, on: boolean) => {
    const set = new Set(filters.item_type ?? []);
    if (on) set.add(type);
    else set.delete(type);
    const list = CHAT_ITEM_TYPES.filter((x) => set.has(x));
    onChange({ ...filters, item_type: list.length > 0 ? list : undefined });
  };
  const select = 'input min-h-touch';
  return (
    <section aria-labelledby={`${id}-title`} className="flex flex-col gap-4">
      <h2 id={`${id}-title`} className="text-base font-semibold">
        {t('chat.filters.title')}
      </h2>
      <form role="search" aria-label={t('chat.filters.search')} onSubmit={submit} className="flex flex-col gap-2">
        <label htmlFor={`${id}-q`} className="text-sm font-medium">
          {t('chat.filters.search')}
        </label>
        <div className="flex gap-2">
          <input
            id={`${id}-q`}
            type="search"
            className="input min-h-touch"
            value={q}
            maxLength={CHAT_Q_MAX}
            onChange={(e) => setQ(e.target.value)}
          />
          <button type="submit" className="btn-secondary">
            {t('chat.filters.find')}
          </button>
        </div>
      </form>
      <div className="flex flex-col gap-1">
        <label htmlFor={`${id}-stream`} className="text-sm font-medium">
          {t('chat.filters.stream')}
        </label>
        <select
          id={`${id}-stream`}
          className={select}
          value={filters.stream}
          onChange={(e) => onChange({ ...filters, stream: e.target.value as ChatFilters['stream'] })}
        >
          {CHAT_STREAMS.map((s) => (
            <option key={s} value={s}>
              {t(`chat.enums.stream.${s}`)}
            </option>
          ))}
        </select>
      </div>
      {filters.stream === 'role' ? (
        <div className="flex flex-col gap-1">
          <label htmlFor={`${id}-role`} className="text-sm font-medium">
            {t('chat.filters.role')}
          </label>
          <select
            id={`${id}-role`}
            className={select}
            value={filters.role ?? ''}
            aria-invalid={!filters.role}
            onChange={(e) => onChange({ ...filters, role: (e.target.value || undefined) as AgentRole | undefined })}
          >
            <option value="">{t('chat.filters.chooseRole')}</option>
            {AGENT_ROLES.map((r) => (
              <option key={r} value={r}>
                {roleLabel(t, r)}
              </option>
            ))}
          </select>
        </div>
      ) : null}
      {filters.stream === 'task' ? (
        <div className="flex flex-col gap-1">
          <label htmlFor={`${id}-task`} className="text-sm font-medium">
            {t('chat.filters.task')}
          </label>
          <select
            id={`${id}-task`}
            className={select}
            value={filters.task_id ?? ''}
            aria-invalid={!filters.task_id}
            onChange={(e) => onChange({ ...filters, task_id: e.target.value || undefined })}
          >
            <option value="">{t('chat.filters.chooseTask')}</option>
            {tasks.map((x) => (
              <option key={x.id} value={x.id}>
                {x.title}
              </option>
            ))}
          </select>
        </div>
      ) : null}
      {threads.length > 0 ? (
        <div className="flex flex-col gap-1">
          <label htmlFor={`${id}-thread`} className="text-sm font-medium">
            {t('chat.filters.thread')}
          </label>
          <select
            id={`${id}-thread`}
            className={select}
            value={filters.thread_id ?? ''}
            onChange={(e) => onChange({ ...filters, thread_id: e.target.value || undefined })}
          >
            <option value="">{t('chat.filters.allThreads')}</option>
            {threads.map((th) => (
              <option key={th.id} value={th.id}>
                {threadLabel(th)}
              </option>
            ))}
          </select>
        </div>
      ) : null}
      <fieldset className="flex flex-col gap-1">
        <legend className="mb-1 text-sm font-medium">{t('chat.filters.types')}</legend>
        {CHAT_ITEM_TYPES.map((type) => (
          <div key={type} className="flex items-center gap-2">
            <input
              id={`${id}-type-${type}`}
              type="checkbox"
              className="h-4 w-4 accent-[rgb(var(--color-accent))]"
              checked={filters.item_type?.includes(type) ?? false}
              onChange={(e) => toggleType(type, e.target.checked)}
            />
            <label htmlFor={`${id}-type-${type}`} className="text-sm">
              {t(`chat.enums.item_type.${type}`)}
            </label>
          </div>
        ))}
        <p className="text-xs text-fg-muted">{t('chat.filters.typesHint')}</p>
      </fieldset>
      <button type="button" className="btn-ghost self-start" onClick={() => onChange({ stream: 'all_agents' })}>
        {t('chat.filters.reset')}
      </button>
    </section>
  );
}

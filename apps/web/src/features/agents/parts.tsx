import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import type { AgentProfile } from '../../api/agents';
import type { CliProvider } from '../../api/runs';
import { Badge, Icon } from '../../components/ui';
import { formatPercent } from '../dashboard/format';
import { taskLinks, type RoleActivity, type RoleState, type RoleStats } from './model';

/** Подпись провайдера CLI (Codex CLI / Claude Code CLI). */
export function useProviderLabel(): (code: CliProvider | string) => string {
  const { t } = useTranslation();
  return (code) => t(`agents.provider.${code}`, { defaultValue: code });
}

/**
 * Активность роли — только из задач (LEASED/RUNNING). Не «онлайн»: роль — логический профиль.
 * Тон нейтральный/информационный (статусные зелёный/красный не используются для ролей).
 */
export function RoleStateBadge({ state, activity }: { state: RoleState; activity: RoleActivity | undefined }) {
  const { t } = useTranslation();
  if (state === 'loading')
    return (
      <span className="inline-flex items-center gap-2 text-sm text-fg-muted" aria-busy="true">
        <span aria-hidden="true" className="skeleton h-5 w-28" />
        <span className="sr-only">{t('agents.state.loading')}</span>
      </span>
    );
  const count = activity?.tasks.length ?? 0;
  const text =
    state === 'active'
      ? activity?.more
        ? t('agents.state.activeMore', { count })
        : t('agents.state.active', { count })
      : state === 'idle'
        ? t('agents.state.idle')
        : t('agents.state.unknown');
  return (
    <Badge tone={state === 'active' ? 'info' : 'neutral'} icon={state === 'active' ? 'activity' : state === 'idle' ? 'statusNeutral' : 'help'} data-role-state={state}>
      {text}
    </Badge>
  );
}

export function ProviderChipsList({ providers, emptyText }: { providers: readonly string[]; emptyText: string }) {
  const label = useProviderLabel();
  if (providers.length === 0)
    return (
      <Badge tone="warning" icon="alert">
        {emptyText}
      </Badge>
    );
  return (
    <ul className="flex flex-wrap gap-1.5">
      {providers.map((p) => (
        <li key={p} className="inline-flex items-center gap-1 rounded-full border border-border bg-surface-muted px-2 py-0.5 text-xs text-fg">
          <Icon name="terminal" className="h-3.5 w-3.5 text-fg-muted" />
          {label(p)}
        </li>
      ))}
    </ul>
  );
}

/** Результаты роли за окно analytics; нет строки by_role — «нет попыток», а не нули. */
export function RoleResults({ stats, pending, error }: { stats: RoleStats | null; pending: boolean; error: boolean }) {
  const { t } = useTranslation();
  if (pending) return <span aria-hidden="true" className="skeleton block h-10 w-full" />;
  if (error) return <p className="text-sm text-fg-muted">{t('agents.notMeasured')}</p>;
  if (!stats) return <p className="text-sm text-fg-muted" data-testid="role-no-attempts">{t('agents.card.noAttempts')}</p>;
  const verified = formatPercent(stats.verified_completion_rate);
  return (
    <dl className="grid grid-cols-3 gap-2 text-sm" data-testid="role-results">
      <div className="flex min-w-0 flex-col">
        <dt className="text-xs text-fg-muted">{t('agents.card.attempts')}</dt>
        <dd className="font-semibold tabular-nums text-fg">{stats.attempts}</dd>
      </div>
      <div className="flex min-w-0 flex-col">
        <dt className="text-xs text-fg-muted">{t('agents.card.succeeded')}</dt>
        <dd className="font-semibold tabular-nums text-fg">{stats.succeeded}</dd>
      </div>
      <div className="flex min-w-0 flex-col">
        <dt className="text-xs text-fg-muted">{t('agents.card.verified')}</dt>
        <dd className="font-semibold tabular-nums text-fg">{verified ?? t('agents.notMeasured')}</dd>
      </div>
    </dl>
  );
}

/** Краткие права профиля: запись, сеть, уровни. */
export function ProfileFacts({ profile }: { profile: AgentProfile }) {
  const { t } = useTranslation();
  return (
    <dl className="grid grid-cols-[auto,1fr] gap-x-3 gap-y-0.5 text-xs">
      <dt className="text-fg-muted">{t('agents.detail.writeScope')}</dt>
      <dd className="text-fg">{t(`agents.enums.write_scope.${profile.permissions.write_scope}`)}</dd>
      <dt className="text-fg-muted">{t('agents.detail.network')}</dt>
      <dd className="text-fg">{t(`agents.enums.network.${profile.permissions.network}`)}</dd>
      <dt className="text-fg-muted">{t('agents.detail.complexity')}</dt>
      <dd className="font-mono text-fg">{t('agents.detail.complexityValue', { min: profile.complexity_band.min, max: profile.complexity_band.max })}</dd>
    </dl>
  );
}

/** Текущие задачи роли: ссылки в Mission Control с выбранной задачей. */
export function CurrentTasks({ workspaceId, activity, limit }: { workspaceId: string; activity: RoleActivity | undefined; limit: number }) {
  const { t } = useTranslation();
  const tasks = activity?.tasks ?? [];
  if (tasks.length === 0) return <p className="text-sm text-fg-muted">{t('agents.card.noCurrentTasks')}</p>;
  const shown = tasks.slice(0, limit);
  const rest = tasks.length - shown.length;
  return (
    <ul className="flex flex-col gap-1 text-sm">
      {shown.map((task) => (
        <li key={task.id} className="flex min-w-0 items-center gap-2">
          <Icon name="statusRunning" className="h-4 w-4 shrink-0 text-info" />
          <Link to={taskLinks(workspaceId, task).mission} className="link min-w-0 truncate">
            {task.title}
          </Link>
        </li>
      ))}
      {rest > 0 || activity?.more ? (
        <li className="text-xs text-fg-muted">{t('agents.card.moreTasks', { count: rest })}{activity?.more ? '+' : ''}</li>
      ) : null}
    </ul>
  );
}

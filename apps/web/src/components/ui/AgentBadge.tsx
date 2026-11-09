import { useTranslation } from 'react-i18next';
import { agentRoleMeta, type AgentFamily } from '../../lib/agentRoles';
import { cx } from './cx';
import { Icon } from './Icon';

type Family = AgentFamily | 'system';

/** Цвет семейства — вторичный признак (глиф и код — основные); пары проверены tokens.test.ts. */
const FAMILY_CLASS: Record<Family, string> = {
  discover: 'bg-role-discover-soft text-role-discover',
  build: 'bg-role-build-soft text-role-build',
  verify: 'bg-role-verify-soft text-role-verify',
  sustain: 'bg-role-sustain-soft text-role-sustain',
  system: 'bg-role-system-soft text-role-system',
};

const SIZE = { sm: 'h-6 w-6 rounded', md: 'h-8 w-8 rounded-md' } as const;
const ICON_SIZE = { sm: 'h-4 w-4', md: 'h-5 w-5' } as const;

/**
 * Аватар роли агента (§5.7): квадрат с глифом Lucide на soft-фоне семейства. Декоративен —
 * доступное имя даёт AgentBadge (или текст рядом). Любое значение вне `AgentRole` — «Система».
 */
export function AgentAvatar({
  agentRole,
  size = 'md',
  className,
}: {
  agentRole: string | null | undefined;
  size?: keyof typeof SIZE;
  className?: string;
}) {
  const meta = agentRoleMeta(agentRole);
  const family: Family = meta?.family ?? 'system';
  return (
    <span
      aria-hidden="true"
      className={cx('inline-flex shrink-0 items-center justify-center', SIZE[size], FAMILY_CLASS[family], className)}
      data-agent-family={family}
    >
      <Icon name={meta?.icon ?? 'system'} className={ICON_SIZE[size]} />
    </span>
  );
}

/**
 * Бейдж роли: аватар + код (моно) + название; провайдер-исполнитель — отдельным чипом.
 * Онлайн-статусов у ролей нет (роли — логические профили, ТЗ §4).
 */
export function AgentBadge({
  agentRole,
  showName = true,
  provider,
  size = 'sm',
  className,
}: {
  /** Значение `AgentRole` либо иное (orchestrator, router…) → «Система». */
  agentRole: string | null | undefined;
  showName?: boolean;
  /** Подпись исполнителя («Codex CLI», «Claude Code CLI»), если известен. */
  provider?: string | null | undefined;
  size?: keyof typeof SIZE;
  className?: string;
}) {
  const { t } = useTranslation();
  const meta = agentRoleMeta(agentRole);
  const name = meta ? t(`enums.agent_role.${agentRole}`) : t('ds.agent.system');
  return (
    <span
      className={cx('inline-flex max-w-full items-center gap-2 text-sm text-fg', className)}
      data-agent-role={meta ? (agentRole ?? undefined) : 'system'}
      title={meta ? undefined : t('ds.agent.systemHint')}
    >
      <AgentAvatar agentRole={agentRole} size={size} />
      <span className="font-mono text-code-sm font-semibold text-fg-muted">{meta?.code ?? 'SYS'}</span>
      {showName ? <span className="truncate">{name}</span> : <span className="sr-only">{name}</span>}
      {provider ? (
        <span className="inline-flex items-center gap-1 rounded-full border border-border bg-surface-muted px-2 py-0.5 text-xs text-fg-muted">
          <Icon name="terminal" className="h-3.5 w-3.5" />
          {provider}
        </span>
      ) : null}
    </span>
  );
}

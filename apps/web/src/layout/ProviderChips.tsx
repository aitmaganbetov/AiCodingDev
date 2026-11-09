import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import type { CliProvider } from '../api/runs';
import { TONE_CLASS } from '../components/ui/Badge';
import { Icon } from '../components/ui/Icon';
import { Tooltip } from '../components/ui/Tooltip';
import { cx } from '../components/ui/cx';
import { formatDateTime, formatRelative } from '../lib/format';
import { useCurrentWorkspace } from '../workspace';
import { CHIP_STATE_ICON, CHIP_STATE_TONE, providerChipModel, type ProviderChipModel } from './providerStatus';
import { useProvidersStatus } from './shellData';

/** Провайдеры v1 (ТЗ §5): текстовые метки без товарных знаков (план §5.6). */
const CHIPS: readonly { code: CliProvider; short: string }[] = [
  { code: 'codex', short: 'CX' },
  { code: 'claude', short: 'CC' },
];

function ChipTooltip({ name, model }: { name: string; model: ProviderChipModel }) {
  const { t, i18n } = useTranslation();
  return (
    <div className="flex max-w-[18rem] flex-col gap-1 py-0.5 text-left">
      <span className="font-semibold">
        {name}: {t(`shell.cli.state.${model.state}`)}
      </span>
      {model.installations.length === 0 ? <span>{t('shell.cli.noInstallations')}</span> : null}
      {model.installations.slice(0, 3).map((i) => (
        <span key={i.installation_id} className="font-normal">
          {t('shell.cli.installation', {
            host: i.host,
            version: i.detected_version ?? t('shell.cli.versionUnknown'),
            compatibility: t(`providers.enums.compatibility.${i.compatibility}`, { defaultValue: i.compatibility }),
          })}
          {i.last_error_category ? ` · ${t('shell.cli.lastError', { code: i.last_error_category })}` : ''}
        </span>
      ))}
      <span className="font-normal">
        {model.checkedAt
          ? t('shell.cli.checked', { relative: formatRelative(model.checkedAt, i18n.language), absolute: formatDateTime(model.checkedAt, i18n.language) })
          : t('shell.cli.neverChecked')}
      </span>
      <span className="font-normal">{t('shell.cli.noQuota')}</span>
    </div>
  );
}

/**
 * Чипы статуса Codex CLI / Claude Code CLI (план §3.1): из `listProviders` — вход (auth_status),
 * доступность, совместимость, время последней проверки; опрос 60 с. Ссылка — Настройки › CLI-провайдеры.
 * Нет данных ≠ «готов». `compact` — только точка-иконка и код (узкая шапка, 768 px).
 */
export function ProviderChips({ compact = false, onNavigate }: { compact?: boolean; onNavigate?: () => void }) {
  const { t } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();
  const query = useProvidersStatus(workspaceId, membership?.role);
  if (!workspaceId || !membership) return null;
  const href = `/w/${workspaceId}/settings/providers`;

  if (query.isPending) {
    return (
      <div className="flex items-center gap-2" aria-busy="true" data-testid="cli-chips-loading">
        <span className="sr-only">{t('shell.cli.loading')}</span>
        {CHIPS.map((c) => (
          <span key={c.code} aria-hidden="true" className={cx('skeleton h-8 rounded-full', compact ? 'w-12' : 'w-36')} />
        ))}
      </div>
    );
  }

  return (
    <ul className="flex items-center gap-2" aria-label={t('shell.cli.label')} data-testid="cli-chips">
      {CHIPS.map((c) => {
        const name = t(`shell.cli.name.${c.code}`);
        const model: ProviderChipModel = query.isError
          ? { state: 'unknown', installations: [], checkedAt: null }
          : providerChipModel(query.data.items.find((p) => p.code === c.code));
        const stateLabel = query.isError ? t('shell.cli.loadFailed') : t(`shell.cli.state.${model.state}`);
        const tone = CHIP_STATE_TONE[model.state];
        return (
          <li key={c.code}>
            <Tooltip content={query.isError ? `${name}: ${stateLabel}` : <ChipTooltip name={name} model={model} />} side="bottom">
              <Link
                to={href}
                onClick={onNavigate}
                data-provider={c.code}
                data-state={query.isError ? 'error' : model.state}
                className={cx(
                  'inline-flex min-h-[2rem] items-center gap-1.5 rounded-full border px-2.5 text-xs font-semibold no-underline transition-colors duration-instant hover:brightness-110',
                  TONE_CLASS[tone],
                  model.state === 'auth_required' && 'pattern-hatch',
                )}
              >
                <Icon name={query.isError ? 'unplug' : CHIP_STATE_ICON[model.state]} className="h-3.5 w-3.5 shrink-0" />
                {compact ? (
                  <>
                    <span aria-hidden="true" className="font-mono">
                      {c.short}
                    </span>
                    <span className="sr-only">
                      {name} · {stateLabel}
                    </span>
                  </>
                ) : (
                  <span className="whitespace-nowrap">
                    {name} · {stateLabel}
                  </span>
                )}
              </Link>
            </Tooltip>
          </li>
        );
      })}
    </ul>
  );
}

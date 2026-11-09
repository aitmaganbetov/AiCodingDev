import * as RadixDialog from '@radix-ui/react-dialog';
import { useQuery } from '@tanstack/react-query';
import { Command } from 'cmdk';
import { useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';
import { isApiError } from '../api/client';
import { projectKeys, projectsApi } from '../api/projects';
import { runKeys, runsApi } from '../api/runs';
import { Icon, type IconName } from '../components/ui/Icon';
import { Kbd } from '../components/ui/Kbd';
import { cx } from '../components/ui/cx';
import { focusMovedElsewhere } from '../components/ui/focus';
import { can } from '../lib/permissions';
import { THEMES, useTheme } from '../lib/theme';
import { useCurrentWorkspace } from '../workspace';
import { useWorkspaceSearch } from './GlobalSearch';
import { SEARCH_TYPE_ICON, searchResultHref } from './searchModel';
import { useNavEntries } from './Sidebar';
import { SWITCHER_PROJECTS_LIMIT } from './ProjectSwitcher';

export const PALETTE_RECENT_RUNS = 8;

const THEME_ICON = { dark: 'moon', light: 'sun', system: 'monitor' } as const;

/** Подразделы Настроек (D-3) — тоже цели навигации палитры. */
const SETTINGS_SECTIONS: readonly { path: string; labelKey: string; icon: IconName; permission?: 'audit:read' }[] = [
  { path: 'settings/providers', labelKey: 'settings.sections.providers', icon: 'terminal' },
  { path: 'settings/audit', labelKey: 'settings.sections.audit', icon: 'audit', permission: 'audit:read' },
  { path: 'settings/appearance', labelKey: 'settings.sections.appearance', icon: 'palette' },
];

function Item({ value, keywords, icon, children, hint, onSelect, forceMount }: {
  value: string;
  keywords?: string[];
  icon: IconName;
  children: ReactNode;
  hint?: ReactNode;
  onSelect: () => void;
  forceMount?: boolean;
}) {
  return (
    <Command.Item
      value={value}
      keywords={keywords}
      onSelect={onSelect}
      forceMount={forceMount}
      className="mx-1 flex min-h-[2.75rem] cursor-pointer items-center gap-3 rounded-md px-3 py-2 text-sm text-fg data-[selected=true]:bg-accent-soft data-[selected=true]:text-accent-soft-fg"
    >
      <Icon name={icon} className="h-4 w-4 shrink-0" />
      <span className="min-w-0 flex-1 truncate">{children}</span>
      {hint ? <span className="shrink-0 text-xs opacity-80">{hint}</span> : null}
    </Command.Item>
  );
}

const GROUP_CLASS =
  'py-1 [&_[cmdk-group-heading]]:px-4 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:text-xs [&_[cmdk-group-heading]]:font-semibold [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wide [&_[cmdk-group-heading]]:text-fg-subtle';

/**
 * Command Palette (Ctrl/Cmd+K, D-5: `cmdk` 1.1.1, загружается лениво). Только навигация и
 * безопасные локальные действия (тема, справка, сворачивание навигации) + поиск по workspace
 * (`GET /search`), недавние запуски и проекты. Никаких shell-команд (план §4.2).
 * Доступность: Radix Dialog (ловушка фокуса, Esc, возврат фокуса), cmdk — combobox + listbox.
 */
export default function CommandPalette({
  open,
  onOpenChange,
  onShowShortcuts,
  onToggleSidebar,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onShowShortcuts: () => void;
  onToggleSidebar: (() => void) | null;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { workspaceId, membership } = useCurrentWorkspace();
  const entries = useNavEntries();
  const [theme, setTheme] = useTheme();
  const [search, setSearch] = useState('');
  const opener = useRef<Element | null>(null);
  const inWorkspace = Boolean(workspaceId && membership);
  const role = membership?.role;

  const runs = useQuery({
    queryKey: [...runKeys.list(workspaceId ?? ''), 'palette'] as const,
    queryFn: ({ signal }) => runsApi.list(workspaceId ?? '', { limit: PALETTE_RECENT_RUNS }, signal),
    enabled: open && inWorkspace && can(role, 'run:read'),
  });
  const projects = useQuery({
    queryKey: projectKeys.summary(workspaceId ?? ''),
    queryFn: ({ signal }) => projectsApi.list(workspaceId ?? '', { limit: SWITCHER_PROJECTS_LIMIT }, signal),
    enabled: open && inWorkspace && can(role, 'project:read'),
  });
  const { q, query: searchQuery, pendingDebounce } = useWorkspaceSearch(search, open && inWorkspace);
  const projectName = new Map((projects.data?.items ?? []).map((p) => [p.id, p.name]));

  const run = (fn: () => void) => {
    onOpenChange(false);
    setSearch('');
    fn();
  };
  const go = (to: string) => run(() => navigate(to));
  const w = `/w/${workspaceId ?? ''}`;

  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 z-overlay bg-overlay/60 animate-fade-in" />
        <RadixDialog.Content
          aria-describedby={undefined}
          onOpenAutoFocus={() => {
            opener.current = document.activeElement;
          }}
          onCloseAutoFocus={(e) => {
            if (focusMovedElsewhere()) {
              e.preventDefault();
              return;
            }
            const el = opener.current;
            if (el instanceof HTMLElement && el !== document.body && el.isConnected) {
              e.preventDefault();
              el.focus();
            }
          }}
          className="glass fixed left-1/2 top-[10vh] z-modal flex max-h-[min(80vh,40rem)] w-[min(40rem,calc(100vw-1rem))] -translate-x-1/2 flex-col overflow-hidden rounded-xl text-fg shadow-e3 animate-fade-in"
          data-testid="command-palette"
        >
          <RadixDialog.Title className="sr-only">{t('shell.palette.title')}</RadixDialog.Title>
          <Command label={t('shell.palette.title')} loop className="flex min-h-0 flex-1 flex-col">
            <div className="flex items-center gap-2 border-b border-border px-4">
              <Icon name="search" className="h-4 w-4 shrink-0 text-fg-muted" />
              <Command.Input
                value={search}
                onValueChange={setSearch}
                placeholder={t('shell.palette.placeholder')}
                aria-label={t('shell.palette.inputLabel')}
                className="min-h-[3rem] w-full bg-transparent text-base text-fg outline-none placeholder:text-fg-subtle"
              />
              <Kbd>Esc</Kbd>
            </div>
            <Command.List label={t('shell.palette.results')} className="min-h-0 flex-1 overflow-y-auto py-1">
              <Command.Empty className="px-4 py-6 text-sm text-fg-muted">{t('shell.palette.empty')}</Command.Empty>

              {inWorkspace && q ? (
                <Command.Group heading={t('shell.palette.groupSearch')} forceMount className={GROUP_CLASS}>
                  {searchQuery.isError ? (
                    <Command.Item disabled forceMount value="search-error" className="mx-1 px-3 py-2 text-sm text-danger-text">
                      {isApiError(searchQuery.error) && searchQuery.error.status === 429 ? t('shell.search.rateLimited') : t('shell.search.error')}
                    </Command.Item>
                  ) : (pendingDebounce || searchQuery.isFetching) && !searchQuery.data ? (
                    <Command.Loading label={t('shell.search.loading')}>
                      <div className="mx-4 my-2 h-8 rounded skeleton" aria-hidden="true" />
                    </Command.Loading>
                  ) : searchQuery.data && searchQuery.data.items.length === 0 ? (
                    <Command.Item disabled forceMount value="search-empty" className="mx-1 px-3 py-2 text-sm text-fg-muted">
                      {t('shell.search.noResults', { q })}
                    </Command.Item>
                  ) : (
                    (searchQuery.data?.items ?? []).map((r) => (
                      <Item
                        key={`${r.type}-${r.id}`}
                        value={`search ${r.type} ${r.id}`}
                        keywords={[q, r.title]}
                        forceMount
                        icon={SEARCH_TYPE_ICON[r.type]}
                        hint={t(`shell.search.types.${r.type}`)}
                        onSelect={() => go(searchResultHref(workspaceId ?? '', r))}
                      >
                        {r.title}
                      </Item>
                    ))
                  )}
                </Command.Group>
              ) : null}

              {inWorkspace ? (
                <Command.Group heading={t('shell.palette.groupNavigation')} className={GROUP_CLASS}>
                  {entries.map((e) => (
                    <Item
                      key={e.id}
                      value={`nav ${e.id} ${t(e.labelKey)}`}
                      icon={e.icon}
                      hint={e.shortcut ? `g ${e.shortcut}` : undefined}
                      onSelect={() => go(`${w}/${e.path}`)}
                    >
                      {t('shell.palette.goTo', { section: t(e.labelKey) })}
                    </Item>
                  ))}
                  {SETTINGS_SECTIONS.filter((s) => !s.permission || can(role, s.permission)).map((s) => (
                    <Item key={s.path} value={`nav ${s.path} ${t(s.labelKey)}`} icon={s.icon} onSelect={() => go(`${w}/${s.path}`)}>
                      {t('shell.palette.goTo', { section: `${t('nav.settings')} › ${t(s.labelKey)}` })}
                    </Item>
                  ))}
                </Command.Group>
              ) : null}
              <Command.Group heading={inWorkspace ? undefined : t('shell.palette.groupNavigation')} className={GROUP_CLASS}>
                <Item value="nav workspaces" icon="layers" onSelect={() => go('/workspaces')}>
                  {t('shell.palette.goTo', { section: t('nav.workspaces') })}
                </Item>
              </Command.Group>

              {inWorkspace && can(role, 'run:read') ? (
                <Command.Group heading={t('shell.palette.groupRuns')} className={GROUP_CLASS}>
                  {runs.isPending ? (
                    <Command.Loading label={t('state.loading')}>
                      <div className="mx-4 my-2 h-8 rounded skeleton" aria-hidden="true" />
                    </Command.Loading>
                  ) : runs.isError ? (
                    <Command.Item disabled value="runs-error" className="mx-1 px-3 py-2 text-sm text-danger-text">
                      {t('shell.palette.runsError')}
                    </Command.Item>
                  ) : (
                    runs.data.items.map((r) => (
                      <Item
                        key={r.id}
                        value={`run ${r.id} ${r.objective}`}
                        keywords={[projectName.get(r.project_id) ?? '']}
                        icon="workflow"
                        hint={t(`enums.run_status.${r.status}`, { defaultValue: r.status })}
                        onSelect={() => go(`${w}/projects/${r.project_id}/runs/${r.id}`)}
                      >
                        {r.objective}
                      </Item>
                    ))
                  )}
                </Command.Group>
              ) : null}

              {inWorkspace && can(role, 'project:read') ? (
                <Command.Group heading={t('shell.palette.groupProjects')} className={GROUP_CLASS}>
                  {projects.isPending ? (
                    <Command.Loading label={t('state.loading')}>
                      <div className="mx-4 my-2 h-8 rounded skeleton" aria-hidden="true" />
                    </Command.Loading>
                  ) : projects.isError ? (
                    <Command.Item disabled value="projects-error" className="mx-1 px-3 py-2 text-sm text-danger-text">
                      {t('shell.project.error')}
                    </Command.Item>
                  ) : (
                    projects.data.items.map((p) => (
                      <Item key={p.id} value={`project ${p.id} ${p.name}`} icon="folder" onSelect={() => go(`${w}/projects/${p.id}`)}>
                        {p.name}
                      </Item>
                    ))
                  )}
                </Command.Group>
              ) : null}

              <Command.Group heading={t('shell.palette.groupActions')} className={GROUP_CLASS}>
                {THEMES.map((th) => (
                  <Item
                    key={th}
                    value={`theme ${th} ${t(`theme.${th}`)}`}
                    keywords={[t('theme.label')]}
                    icon={THEME_ICON[th]}
                    hint={theme === th ? t('shell.palette.current') : undefined}
                    onSelect={() => run(() => setTheme(th))}
                  >
                    {t('shell.palette.theme', { theme: t(`theme.${th}`) })}
                  </Item>
                ))}
                {onToggleSidebar ? (
                  <Item value="action sidebar" icon="panelLeftClose" onSelect={() => run(onToggleSidebar)}>
                    {t('shell.palette.toggleSidebar')}
                  </Item>
                ) : null}
                <Item value="action shortcuts" icon="keyboard" hint="?" onSelect={() => run(onShowShortcuts)}>
                  {t('shell.palette.shortcuts')}
                </Item>
              </Command.Group>
            </Command.List>
            <div className={cx('flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-border px-4 py-2 text-xs text-fg-muted')}>
              <span>
                <Kbd>↑</Kbd> <Kbd>↓</Kbd> {t('shell.palette.hintMove')}
              </span>
              <span>
                <Kbd>Enter</Kbd> {t('shell.palette.hintSelect')}
              </span>
              <span>{t('shell.palette.hintSafe')}</span>
            </div>
          </Command>
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}

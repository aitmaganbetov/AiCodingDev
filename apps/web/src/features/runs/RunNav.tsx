import { useTranslation } from 'react-i18next';
import { NavLink } from 'react-router';
import { cx } from '../../components/ui';

/** Разделы одного run: обзор (лента), граф задач, Live Agent Chat — у каждого свой URL. */
export function RunNav({ workspaceId, projectId, runId }: { workspaceId: string; projectId: string; runId: string }) {
  const { t } = useTranslation();
  const base = `/w/${workspaceId}/projects/${projectId}/runs/${runId}`;
  const items = [
    { to: base, label: t('runs.nav.overview'), end: true },
    { to: `${base}/graph`, label: t('runs.nav.graph'), end: false },
    { to: `${base}/chat`, label: t('runs.nav.chat'), end: false },
  ];
  return (
    <nav aria-label={t('runs.nav.label')} className="mb-6 border-b border-border">
      <ul className="-mb-px flex flex-wrap gap-1">
        {items.map((item) => (
          <li key={item.to}>
            <NavLink
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cx(
                  'inline-flex min-h-touch items-center border-b-2 px-3 text-sm font-medium',
                  isActive ? 'border-accent text-fg' : 'border-transparent text-fg-muted hover:text-fg',
                )
              }
            >
              {item.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}

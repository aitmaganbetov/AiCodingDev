import { useTranslation } from 'react-i18next';
import { NavLink } from 'react-router';
import { cx } from '../../components/ui';
import { can, type Permission } from '../../lib/permissions';
import { useCurrentWorkspace } from '../../workspace';

interface Item {
  to: string;
  label: string;
  end: boolean;
  permission?: Permission;
}

/**
 * Разделы одного run: обзор (лента), граф задач, Live Agent Chat — у каждого свой URL;
 * затем переходы к экранам других областей по этому run: качество, соответствие, одобрения.
 */
export function RunNav({ workspaceId, projectId, runId }: { workspaceId: string; projectId: string; runId: string }) {
  const { t } = useTranslation();
  const role = useCurrentWorkspace().membership?.role;
  const ws = `/w/${workspaceId}`;
  const base = `${ws}/projects/${projectId}/runs/${runId}`;
  const items: Item[] = [
    { to: base, label: t('runs.nav.overview'), end: true },
    { to: `${base}/graph`, label: t('runs.nav.graph'), end: false },
    { to: `${base}/chat`, label: t('runs.nav.chat'), end: false },
    { to: `${ws}/quality/runs/${runId}`, label: t('runs.nav.quality'), end: false, permission: 'quality:read' },
    { to: `${ws}/compliance/runs/${runId}`, label: t('runs.nav.compliance'), end: false, permission: 'quality:read' },
    { to: `${ws}/approvals?run_id=${encodeURIComponent(runId)}`, label: t('runs.nav.approvals'), end: false, permission: 'approval:read' },
  ];
  return (
    <nav aria-label={t('runs.nav.label')} className="mb-6 border-b border-border">
      <ul className="-mb-px flex flex-wrap gap-1">
        {items
          .filter((item) => !item.permission || can(role, item.permission))
          .map((item) => (
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

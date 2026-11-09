import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { can, type Permission } from '../../lib/permissions';
import { useCurrentWorkspace } from '../../workspace';

interface Item {
  to: string;
  label: string;
  permission?: Permission;
}

/**
 * Переходы экрана задачи (Task Detail) к её run: AI Mission Control (с той же задачей в Agent Inspector;
 * обзор run, граф и Live Agent Chat с F4 — панели Mission Control), затем экраны других областей по
 * этому run: качество, соответствие, одобрения.
 */
export function RunNav({ workspaceId, runId, taskId }: { workspaceId: string; projectId: string; runId: string; taskId?: string }) {
  const { t } = useTranslation();
  const role = useCurrentWorkspace().membership?.role;
  const ws = `/w/${workspaceId}`;
  const mc = new URLSearchParams({ run: runId, ...(taskId ? { task: taskId } : {}) });
  const items: Item[] = [
    { to: `${ws}/mission-control?${mc.toString()}`, label: t('runs.nav.missionControl') },
    { to: `${ws}/quality/runs/${runId}`, label: t('runs.nav.quality'), permission: 'quality:read' },
    { to: `${ws}/compliance/runs/${runId}`, label: t('runs.nav.compliance'), permission: 'quality:read' },
    { to: `${ws}/approvals?run_id=${encodeURIComponent(runId)}`, label: t('runs.nav.approvals'), permission: 'approval:read' },
  ];
  return (
    <nav aria-label={t('runs.nav.label')} className="mb-6 border-b border-border">
      <ul className="-mb-px flex flex-wrap gap-1">
        {items
          .filter((item) => !item.permission || can(role, item.permission))
          .map((item) => (
            <li key={item.to}>
              <Link
                to={item.to}
                className="inline-flex min-h-touch items-center border-b-2 border-transparent px-3 text-sm font-medium text-fg-muted hover:text-fg"
              >
                {item.label}
              </Link>
            </li>
          ))}
      </ul>
    </nav>
  );
}

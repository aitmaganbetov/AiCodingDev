import { useTranslation } from 'react-i18next';
import { PageHeading } from '../../components/PageHeading';
import { ButtonLink } from '../../components/ui';
import { can } from '../../lib/permissions';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { MembersPanel } from './MembersPanel';
import { WorkspacePanel } from './WorkspacePanel';

/** P7-13 Settings: рабочее пространство (название, политики — OQ-45 (f)) и участники (роли RBAC). */
export function SettingsPage() {
  const { t } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();
  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-6">
      <PageHeading title={t('settings.title')}>
        {can(membership.role, 'audit:read') ? (
          <ButtonLink to={`/w/${workspaceId}/audit`} icon="audit">
            {t('settings.toAudit')}
          </ButtonLink>
        ) : null}
      </PageHeading>
      <WorkspacePanel key={`ws-${workspaceId}`} workspaceId={workspaceId} role={membership.role} />
      <MembersPanel key={workspaceId} workspaceId={workspaceId} canManage={can(membership.role, 'member:manage')} />
    </section>
  );
}

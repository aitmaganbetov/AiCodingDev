import { useTranslation } from 'react-i18next';
import { PageHeading } from '../../components/PageHeading';
import { ButtonLink, Callout, Card } from '../../components/ui';
import { can } from '../../lib/permissions';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { MembersPanel } from './MembersPanel';

/** P7-13 Settings: рабочее пространство и участники (роли RBAC). */
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
      <Card as="section" id="workspace-info" title={t('settings.workspace.title')}>
        <dl className="grid grid-cols-[auto,1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-fg-muted">{t('settings.workspace.name')}</dt>
          <dd className="break-words">{membership.name}</dd>
          <dt className="text-fg-muted">{t('settings.workspace.slug')}</dt>
          <dd className="font-mono">{membership.slug}</dd>
          <dt className="text-fg-muted">{t('settings.workspace.yourRole')}</dt>
          <dd>{t(`enums.workspace_role.${membership.role}`, { defaultValue: membership.role })}</dd>
        </dl>
        <Callout tone="neutral" className="mt-4" title={t('settings.workspace.gapTitle')}>
          {t('settings.workspace.gap')}
        </Callout>
      </Card>
      <MembersPanel key={workspaceId} workspaceId={workspaceId} canManage={can(membership.role, 'member:manage')} />
    </section>
  );
}

import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { PageHeading } from '../components/PageHeading';

export function NotFoundPage() {
  const { t } = useTranslation();
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('notFound.title')} />
      <p className="mb-4 text-slate-700">{t('notFound.description')}</p>
      <Link to="/" className="btn-secondary">
        {t('notFound.home')}
      </Link>
    </section>
  );
}

/** Нет членства — тот же ответ, что и для несуществующего workspace (API_DESIGN §4). */
export function WorkspaceNotFound() {
  const { t } = useTranslation();
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('notFound.workspaceTitle')} />
      <p className="mb-4 text-slate-700">{t('notFound.workspaceDescription')}</p>
      <Link to="/workspaces" className="btn-secondary">
        {t('layout.allWorkspaces')}
      </Link>
    </section>
  );
}

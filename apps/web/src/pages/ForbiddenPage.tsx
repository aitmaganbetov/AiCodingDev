import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { PageHeading } from '../components/PageHeading';
import { ForbiddenState } from '../components/states';

/** 403: роль не имеет доступа к разделу (UI-проверка или ответ сервера forbidden). */
export function ForbiddenPage({ description }: { description?: string }) {
  const { t } = useTranslation();
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('state.forbiddenTitle')} />
      <ForbiddenState
        {...(description ? { description } : {})}
        action={
          <Link to="/" className="btn-secondary">
            {t('notFound.home')}
          </Link>
        }
      />
    </section>
  );
}

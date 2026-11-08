import { useTranslation } from 'react-i18next';
import { PageHeading } from '../components/PageHeading';
import { UnderConstructionState } from '../components/states';

/** Заглушка экрана Phase 7: заголовок области + «Раздел в разработке». Заменяется реализацией области. */
export function AreaPlaceholderPage({ titleKey }: { titleKey: string }) {
  const { t } = useTranslation();
  const title = t(titleKey);
  return (
    <section aria-labelledby="page-title" data-testid="area-placeholder">
      <PageHeading title={title} />
      <UnderConstructionState section={title} />
    </section>
  );
}

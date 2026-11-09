import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { PageHeading } from '../components/PageHeading';
import { UnderConstructionState } from '../components/states';
import { Icon } from '../components/ui/Icon';
import { useCurrentWorkspace } from '../workspace';

export interface RelatedLink {
  /** Путь относительно /w/:workspaceId/. */
  to: string;
  labelKey: string;
}

/**
 * Раздел, экран которого появится в следующей фазе редизайна: заголовок + «Раздел в разработке»
 * (фаза плана) + ссылки на существующие экраны с теми же данными. Без демо-содержимого.
 */
export function AreaPlaceholderPage({
  titleKey,
  phase,
  descriptionKey,
  related = [],
}: {
  titleKey: string;
  /** Фаза плана редизайна, в которой появится экран (F4…F8). */
  phase?: string;
  descriptionKey?: string;
  related?: RelatedLink[];
}) {
  const { t } = useTranslation();
  const { workspaceId } = useCurrentWorkspace();
  const title = t(titleKey);
  return (
    <section aria-labelledby="page-title" data-testid="area-placeholder" className="flex max-w-3xl flex-col gap-4">
      <PageHeading title={title} />
      <UnderConstructionState section={title} />
      {phase || descriptionKey ? (
        <div className="flex flex-col gap-2 text-fg-muted">
          {descriptionKey ? <p>{t(descriptionKey)}</p> : null}
          {phase ? <p className="text-sm">{t('shell.pending.phase', { phase })}</p> : null}
        </div>
      ) : null}
      {related.length > 0 && workspaceId ? (
        <nav aria-label={t('shell.pending.relatedLabel')}>
          <h2 className="mb-2 text-base font-semibold text-fg">{t('shell.pending.related')}</h2>
          <ul className="flex flex-wrap gap-3">
            {related.map((r) => (
              <li key={r.to}>
                <Link to={`/w/${workspaceId}/${r.to}`} className="btn-secondary">
                  {t(r.labelKey)}
                  <Icon name="chevronRight" className="h-4 w-4" />
                </Link>
              </li>
            ))}
          </ul>
        </nav>
      ) : null}
    </section>
  );
}

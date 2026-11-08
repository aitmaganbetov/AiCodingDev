import { useTranslation } from 'react-i18next';
import { problemMessage } from '../lib/problem';

interface Props {
  shown: number;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  nextPageError: unknown;
  onLoadMore: () => void;
}

/** Cursor-пагинация (API_DESIGN §7): общего количества нет, только «Загрузить ещё». */
export function LoadMore({ shown, hasNextPage, isFetchingNextPage, nextPageError, onLoadMore }: Props) {
  const { t } = useTranslation();
  return (
    <div className="mt-4 flex flex-col items-start gap-2">
      <p role="status" aria-live="polite" className="text-sm text-slate-700">
        {t('list.shown', { count: shown })}
      </p>
      {nextPageError ? (
        <p role="alert" className="text-sm font-medium text-red-700">
          {problemMessage(t, nextPageError)}
        </p>
      ) : null}
      {hasNextPage ? (
        <button type="button" className="btn-secondary" onClick={onLoadMore} disabled={isFetchingNextPage}>
          {isFetchingNextPage ? t('state.loading') : t('list.loadMore')}
        </button>
      ) : null}
    </div>
  );
}

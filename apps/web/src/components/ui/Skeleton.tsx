import { cx } from './cx';

/**
 * Заглушка-скелет для области, контент которой ещё грузится. Декоративна (aria-hidden): о загрузке
 * сообщает LoadingState / aria-busy контейнера. Пульсация — только без prefers-reduced-motion.
 */
export function Skeleton({ lines = 3, className }: { lines?: number; className?: string }) {
  return (
    <div aria-hidden="true" className={cx('flex flex-col gap-2', className)} data-testid="skeleton">
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className={cx('skeleton h-4', i === lines - 1 ? 'w-2/3' : 'w-full')} />
      ))}
    </div>
  );
}

/** Прямоугольный скелет произвольной формы (плитка, аватар, график). */
export function SkeletonBlock({ className }: { className?: string }) {
  return <div aria-hidden="true" className={cx('skeleton', className ?? 'h-24 w-full')} />;
}

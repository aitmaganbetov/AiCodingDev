import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { formatDateTime, formatRelative, type DateTimeFormat } from '../../lib/format';

/** Абсолютное время в локали пользователя (машиночитаемое значение — в dateTime). */
export function Timestamp({ value, format = 'datetime', className }: { value: string; format?: DateTimeFormat; className?: string }) {
  const { i18n } = useTranslation();
  return (
    <time dateTime={value} className={className}>
      {formatDateTime(value, i18n.language, format)}
    </time>
  );
}

/**
 * Относительное время («5 минут назад»), обновляется раз в 30 с. Точное время — в title
 * и в скрытом тексте для скринридеров (title сам по себе клавиатуре недоступен).
 */
export function RelativeTime({ value, className, refreshMs = 30_000 }: { value: string; className?: string; refreshMs?: number }) {
  const { t, i18n } = useTranslation();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), refreshMs);
    return () => clearInterval(id);
  }, [refreshMs]);
  const absolute = formatDateTime(value, i18n.language, 'datetime');
  return (
    <time dateTime={value} title={absolute} className={className}>
      <span aria-hidden="true">{formatRelative(value, i18n.language, now)}</span>
      <span className="sr-only">{t('ui.absoluteTime', { time: absolute })}</span>
    </time>
  );
}

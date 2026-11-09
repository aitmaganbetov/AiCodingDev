import { useEffect, useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { cx } from './cx';
import { Icon } from './Icon';

interface CodeBlockProps {
  code: string;
  /** Подпись блока (имя файла, команда); по умолчанию «Код». */
  label?: string;
  /** Язык для data-атрибута/будущей подсветки. */
  language?: string;
  /** Переносить строки вместо горизонтальной прокрутки. */
  wrap?: boolean;
  /** Максимальная высота (Tailwind-класс), дальше — вертикальная прокрутка. */
  maxHeightClass?: string;
  copyable?: boolean;
  className?: string;
}

/**
 * Блок кода/вывода CLI. Текст — недоверенные данные: рендерится только как текст (без HTML).
 * Прокручиваемая область фокусируема (axe scrollable-region-focusable), кнопка копирования
 * сообщает результат через live region.
 */
export function CodeBlock({ code, label, language, wrap = false, maxHeightClass = 'max-h-96', copyable = true, className }: CodeBlockProps) {
  const { t } = useTranslation();
  const captionId = useId();
  const [copied, setCopied] = useState<'ok' | 'fail' | null>(null);
  useEffect(() => {
    if (!copied) return undefined;
    const id = setTimeout(() => setCopied(null), 2000);
    return () => clearTimeout(id);
  }, [copied]);
  const caption = label ?? t('ui.code');
  const focusable = 0;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied('ok');
    } catch {
      setCopied('fail');
    }
  };

  return (
    <figure className={cx('overflow-hidden rounded-md border border-border bg-surface-muted', className)} data-language={language}>
      <figcaption className="flex min-h-[2.5rem] items-center justify-between gap-2 border-b border-border px-3 text-xs font-medium text-fg-muted">
        <span id={captionId} className="truncate font-mono">
          {caption}
        </span>
        {copyable ? (
          <span className="flex items-center gap-2">
            <span role="status" className="text-xs">
              {copied === 'ok' ? t('action.copied') : copied === 'fail' ? t('action.copyFailed') : ''}
            </span>
            <button type="button" className="btn-ghost btn-sm min-h-[2rem] px-2" onClick={() => void copy()}>
              <Icon name="copy" className="h-4 w-4" />
              {t('action.copy')}
            </button>
          </span>
        ) : null}
      </figcaption>
      <pre
        tabIndex={focusable}
        aria-labelledby={captionId}
        className={cx('overflow-auto p-3 text-sm leading-relaxed text-fg', maxHeightClass, wrap ? 'whitespace-pre-wrap break-words' : 'whitespace-pre')}
      >
        <code>{code}</code>
      </pre>
    </figure>
  );
}

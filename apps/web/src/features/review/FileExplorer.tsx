/**
 * File Explorer ревью: изменённые файлы diff (из артефакта git_diff) со статусом, +/− и числом открытых
 * обсуждений. Клавиатура: один Tab-стоп (roving tabindex), ↑/↓/Home/End — переход и выбор файла.
 */
import { useRef, type KeyboardEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { cx } from '../../components/ui';
import type { DiffFile } from '../tasks/diffModel';

export function FileExplorer({
  files,
  selected,
  openCounts,
  onSelect,
}: {
  files: readonly DiffFile[];
  selected: number;
  openCounts: readonly number[];
  onSelect: (index: number) => void;
}) {
  const { t } = useTranslation();
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const move = (index: number) => {
    onSelect(index);
    refs.current[index]?.focus();
  };
  const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    const last = files.length - 1;
    const next = e.key === 'ArrowDown' ? Math.min(last, i + 1) : e.key === 'ArrowUp' ? Math.max(0, i - 1) : e.key === 'Home' ? 0 : e.key === 'End' ? last : null;
    if (next === null) return;
    e.preventDefault();
    move(next);
  };
  return (
    <nav aria-label={t('review.files.label', { count: files.length })}>
      <p className="mb-2 text-xs text-fg-muted" id="review-files-hint">
        {t('review.files.hint')}
      </p>
      <ul className="flex flex-col gap-1" data-testid="review-files">
        {files.map((f, i) => {
          const open = openCounts[i] ?? 0;
          return (
            <li key={`${f.path}-${i}`}>
              <button
                ref={(el) => {
                  refs.current[i] = el;
                }}
                type="button"
                tabIndex={i === selected ? 0 : -1}
                aria-current={i === selected ? 'true' : undefined}
                aria-describedby="review-files-hint"
                onClick={() => onSelect(i)}
                onKeyDown={(e) => onKeyDown(e, i)}
                className={cx(
                  'flex min-h-touch w-full min-w-0 flex-col items-start rounded-md border px-2 py-1 text-left text-sm',
                  i === selected ? 'border-accent bg-accent-soft text-accent-soft-fg' : 'border-border bg-surface hover:bg-surface-muted',
                )}
                data-path={f.path}
              >
                <span className="w-full break-all font-mono text-xs">{f.path}</span>
                <span className="text-xs">
                  {t(`tasks.diff.status.${f.status}`)} · {t('tasks.diff.stats', { add: f.additions, del: f.deletions })}
                  {open > 0 ? ` · ${t('review.files.open', { count: open })}` : ''}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

/**
 * Текстовый (unified) вид diff файла для ревью: номера строк обеих сторон, знак изменения текстом, треды под
 * строкой якоря, маркеры находок. Номер строки — кнопка «комментировать» (клавиатура: Tab → Enter). Код и
 * комментарии — недоверенный текст, выводятся только как текст. Длинные строки переносятся (без горизонтальной
 * прокрутки страницы на 375 px).
 */
import { Fragment, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import type { Finding } from '../../api/findings';
import type { ReviewSide } from '../../api/reviews';
import { Icon, cx } from '../../components/ui';
import { anchorKey, rowTarget, type DiffRow, type FileThreads } from './reviewModel';

const ROW_TONE: Record<DiffRow['kind'], string> = {
  add: 'bg-success-soft text-success-fg',
  del: 'bg-danger-soft text-danger-fg',
  context: 'text-fg',
  hunk: 'bg-surface-muted text-fg-muted',
  note: 'text-fg-muted',
};

export function DiffTextView({
  path,
  rows,
  threads,
  findingsByLine,
  canComment,
  onComment,
  draftKey,
  form,
  renderThread,
}: {
  path: string;
  rows: readonly DiffRow[];
  threads: FileThreads;
  /** Находки новой версии файла по номеру строки. */
  findingsByLine: ReadonlyMap<number, readonly Finding[]>;
  canComment: boolean;
  onComment: (side: ReviewSide, line: number) => void;
  /** `side:line` строки, под которой открыта форма нового комментария. */
  draftKey: string | null;
  form: ReactNode;
  renderThread: (threadId: string) => ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <div className="overflow-hidden rounded-md border border-border bg-surface" data-testid="diff-text">
      <table className="w-full table-fixed border-collapse text-xs">
        <caption className="sr-only">{t('review.diff.tableCaption', { path })}</caption>
        <colgroup>
          <col className="w-12" />
          <col className="w-12" />
          <col className="w-6" />
          <col />
        </colgroup>
        <thead className="sr-only">
          <tr>
            <th scope="col">{t('review.diff.colBase')}</th>
            <th scope="col">{t('review.diff.colHead')}</th>
            <th scope="col">{t('review.diff.colChange')}</th>
            <th scope="col">{t('review.diff.colCode')}</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {rows.map((row, i) => {
            const target = rowTarget(row);
            const key = target ? anchorKey(target.side, target.line) : null;
            const rowThreads = key ? (threads.byRow.get(key) ?? []) : [];
            const findings = row.newLine !== null && row.kind !== 'del' ? (findingsByLine.get(row.newLine) ?? []) : [];
            const showForm = key !== null && key === draftKey;
            const lineCell = (side: ReviewSide, n: number | null) => {
              if (n === null) return null;
              if (canComment && target && target.side === side && target.line === n) {
                return (
                  <button
                    type="button"
                    className="min-h-target w-full rounded-sm px-1 text-right underline-offset-2 hover:bg-surface-raised hover:underline"
                    aria-label={t('review.diff.commentLine', { line: n, side: t(`review.side.${side}`) })}
                    data-line={`${side}:${n}`}
                    onClick={() => onComment(side, n)}
                  >
                    {n}
                  </button>
                );
              }
              return <span className="block px-1 text-right">{n}</span>;
            };
            const numberTone = row.kind === 'context' ? 'text-fg-muted' : undefined;
            return (
              <Fragment key={i}>
                <tr className={cx('align-top', ROW_TONE[row.kind])} data-kind={row.kind} data-anchor={key ?? undefined}>
                  <td className={cx('select-none border-r border-border', numberTone)}>{row.kind === 'hunk' || row.kind === 'note' ? null : lineCell('base', row.oldLine)}</td>
                  <td className={cx('select-none border-r border-border', numberTone)}>{row.kind === 'hunk' || row.kind === 'note' ? null : lineCell('head', row.newLine)}</td>
                  <td className="select-none text-center">
                    {row.kind === 'add' ? (
                      <>
                        <span aria-hidden="true">+</span>
                        <span className="sr-only">{t('review.diff.added')}</span>
                      </>
                    ) : row.kind === 'del' ? (
                      <>
                        <span aria-hidden="true">−</span>
                        <span className="sr-only">{t('review.diff.removed')}</span>
                      </>
                    ) : null}
                  </td>
                  <td className="whitespace-pre-wrap break-all px-2 py-0.5">
                    {row.text}
                    {rowThreads.length > 0 ? (
                      <span className="ml-2 inline-flex items-center gap-1 font-sans text-[11px] font-semibold" data-testid="row-thread-marker">
                        <Icon name="message" className="h-3 w-3" />
                        <span className="sr-only">{t('review.diff.threadsHere', { count: rowThreads.length })}</span>
                      </span>
                    ) : null}
                    {findings.map((f) => (
                      <span key={f.id} className="ml-2 inline-flex items-center gap-1 font-sans text-[11px] font-semibold" data-testid="row-finding-marker">
                        <Icon name="alert" className="h-3 w-3" />
                        <span>{t('review.diff.finding', { rule: f.rule_id })}</span>
                      </span>
                    ))}
                  </td>
                </tr>
                {rowThreads.length > 0 || showForm ? (
                  <tr>
                    <td colSpan={4} className="bg-canvas-secondary p-2 font-sans">
                      <div className="flex flex-col gap-2">
                        {rowThreads.map((th) => (
                          <Fragment key={th.id}>{renderThread(th.id)}</Fragment>
                        ))}
                        {showForm ? form : null}
                      </div>
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

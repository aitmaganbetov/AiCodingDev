/**
 * Repository Explorer (FR-PRJ-05): дерево файлов и просмотр файла по commit.
 *
 * Содержимое файлов и имена из репозитория — недоверенные данные: выводятся только как текст
 * (React экранирует; без HTML/markdown, без подсветки, ссылки из содержимого не создаются).
 */
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useId, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { projectKeys, projectsApi, type Repository, type RepositoryCommitSummary, type RepositoryTreeEntry } from '../../api/projects';
import { TextField } from '../../components/form';
import { Badge, Button, Callout, ErrorState, Icon, LoadMore, LoadingState } from '../../components/ui';

export const TREE_PAGE_SIZE = 100;
/** Сколько последних commit выбранной ветки предлагает селектор (сервер отдаёт до 1000 по курсору). */
export const RECENT_COMMITS = 30;
export const BRANCHES_PAGE_SIZE = 200;
/** Ввод commit: hex SHA (7–64) или имя ветки — та же грамматика, что проверяет сервер (422 иначе). */
const COMMIT_RE = /^(?:[0-9a-f]{7,64}|(?!-)(?!.*\.\.)(?!.*@\{)(?!.*\/\/)[A-Za-z0-9._/-]{1,255})$/;

/** Выбор commit: '' — HEAD выбранной ветки, 'index' — снимок индекса, 'custom' — ввод, иначе — SHA из истории. */
type CommitChoice = '' | 'index' | 'custom' | (string & {});

/** Подпись commit в селекторе: короткий SHA и тема (тема — недоверенный текст, выводится как текст). */
export function commitLabel(c: RepositoryCommitSummary): string {
  const subject = c.subject.length > 60 ? `${c.subject.slice(0, 59)}…` : c.subject;
  return subject ? `${c.sha.slice(0, 12)} — ${subject}` : c.sha.slice(0, 12);
}
const SCROLL_FOCUSABLE = 0;

export function formatBytes(n: number, locale: string): string {
  const units = ['B', 'KB', 'MB', 'GB'];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(v)} ${units[i]}`;
}

/** Номера строк для текста файла (завершающий перевод строки не порождает пустую строку). */
export function lineNumbers(content: string): string {
  const count = content === '' ? 0 : content.split('\n').length - (content.endsWith('\n') ? 1 : 0);
  return Array.from({ length: count }, (_, i) => String(i + 1)).join('\n');
}

function parentPath(path: string): string {
  const i = path.lastIndexOf('/');
  return i < 0 ? '' : path.slice(0, i);
}

function FileView({ projectId, repositoryId, commit, path }: { projectId: string; repositoryId: string; commit: string; path: string }) {
  const { t, i18n } = useTranslation();
  const captionId = useId();
  const query = useQuery({
    queryKey: projectKeys.file(projectId, repositoryId, commit, path),
    queryFn: ({ signal }) => projectsApi.file(projectId, repositoryId, { path, ...(commit ? { commit } : {}) }, signal),
  });
  if (query.isPending) return <LoadingState label={t('projects.explorer.fileLoading')} />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  const f = query.data;
  const content = f.content ?? '';
  return (
    <section aria-labelledby={captionId} className="flex min-w-0 flex-col gap-2" data-testid="repository-file">
      <div className="flex flex-wrap items-center gap-2">
        <h4 id={captionId} className="break-all font-mono text-sm font-semibold">
          {f.path}
        </h4>
        <span className="text-sm text-fg-muted">{formatBytes(f.size, i18n.language)}</span>
        {f.type === 'symlink' ? <Badge tone="info">{t('projects.explorer.type.symlink')}</Badge> : null}
      </div>
      {f.is_binary ? (
        <Callout tone="neutral" title={t('projects.explorer.binaryTitle')}>
          {t('projects.explorer.binary')}
        </Callout>
      ) : (
        <>
          {f.truncated ? (
            <Callout tone="warning" title={t('projects.explorer.truncatedTitle')}>
              {t('projects.explorer.truncated', { shown: formatBytes(f.max_bytes, i18n.language), size: formatBytes(f.size, i18n.language) })}
            </Callout>
          ) : null}
          {content === '' ? (
            <p className="text-sm text-fg-muted">{t('projects.explorer.emptyFile')}</p>
          ) : (
            <div
              // Прокручиваемая область фокусируема с клавиатуры (axe scrollable-region-focusable), как в CodeBlock.
              tabIndex={SCROLL_FOCUSABLE}
              role="region"
              aria-label={t('projects.explorer.contentLabel', { path: f.path })}
              className="max-h-[32rem] overflow-auto rounded-md border border-border bg-surface-muted text-sm leading-relaxed"
              data-testid="repository-file-content"
            >
              <div className="flex min-w-max">
                <pre aria-hidden="true" className="select-none border-r border-border px-3 py-3 text-right text-fg-muted" data-testid="line-numbers">
                  {lineNumbers(content)}
                </pre>
                <pre className="whitespace-pre px-3 py-3 text-fg">
                  <code>{content}</code>
                </pre>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function EntryRow({ entry, selected, onOpen }: { entry: RepositoryTreeEntry; selected: boolean; onOpen: (e: RepositoryTreeEntry) => void }) {
  const { t, i18n } = useTranslation();
  const typeLabel = t(`projects.explorer.type.${entry.type}`, { defaultValue: entry.type });
  const label = (
    <>
      {entry.type === 'directory' ? <Icon name="folder" className="h-4 w-4 shrink-0" aria-hidden="true" /> : null}
      <span className="break-all font-mono">{entry.name}</span>
      <span className="sr-only">, {typeLabel}</span>
    </>
  );
  return (
    <li className="flex items-center justify-between gap-3 border-b border-border py-1 last:border-b-0" data-entry-type={entry.type}>
      {entry.type === 'submodule' ? (
        <span className="flex min-h-[2rem] items-center gap-2 text-fg-muted">
          {label}
          <Badge>{typeLabel}</Badge>
        </span>
      ) : (
        <button
          type="button"
          className="flex min-h-[2rem] items-center gap-2 text-left text-link underline-offset-2 hover:underline"
          aria-current={selected ? 'true' : undefined}
          onClick={() => onOpen(entry)}
        >
          {label}
          {entry.type === 'symlink' ? <Badge tone="info">{typeLabel}</Badge> : null}
        </button>
      )}
      <span className="shrink-0 text-xs text-fg-muted">{entry.size != null && entry.type !== 'directory' ? formatBytes(entry.size, i18n.language) : ''}</span>
    </li>
  );
}

/** Дерево файлов репозитория с выбором commit и просмотром файла как текста с номерами строк. */
export function RepositoryExplorer({ projectId, repo, initialBranch }: { projectId: string; repo: Repository; initialBranch?: string | undefined }) {
  const { t } = useTranslation();
  const uid = useId();
  const snapshot = repo.latest_index_snapshot ?? null;
  // F6: «Открыть файлы ветки» со вкладки «Ветки» (вызывающий меняет key при смене ветки).
  const [branch, setBranch] = useState(initialBranch || repo.default_branch);
  const [choice, setChoice] = useState<CommitChoice>('');
  const [draft, setDraft] = useState('');
  const [draftError, setDraftError] = useState<string | undefined>(undefined);
  const [custom, setCustom] = useState('');
  const [path, setPath] = useState('');
  const [file, setFile] = useState<string | null>(null);
  // HEAD ветки по умолчанию — без параметра (сервер разрешает сам, с fallback на снимок индекса).
  const branchRef = branch === repo.default_branch ? '' : branch;
  const commit =
    choice === '' ? branchRef : choice === 'index' ? (snapshot?.commit_sha ?? '') : choice === 'custom' ? custom : choice;
  const mode = choice === 'custom' ? 'custom' : 'other';

  // OQ-45 (c): ветки и последние commit выбранной ветки — вместо ручного ввода SHA.
  const branches = useQuery({
    queryKey: projectKeys.branches(projectId, repo.id),
    queryFn: ({ signal }) => projectsApi.branches(projectId, repo.id, { limit: BRANCHES_PAGE_SIZE }, signal),
    staleTime: 30_000,
  });
  const recent = useQuery({
    queryKey: projectKeys.commits(projectId, repo.id, branch),
    queryFn: ({ signal }) => projectsApi.commits(projectId, repo.id, { limit: RECENT_COMMITS, ...(branchRef ? { commit: branchRef } : {}) }, signal),
    staleTime: 30_000,
  });
  const branchNames = branches.data?.items.map((b) => b.name) ?? [];
  if (!branchNames.includes(repo.default_branch)) branchNames.unshift(repo.default_branch);
  if (!branchNames.includes(branch)) branchNames.push(branch);
  const recentCommits = recent.data?.items ?? [];

  const tree = useInfiniteQuery({
    queryKey: projectKeys.tree(projectId, repo.id, commit, path),
    queryFn: ({ pageParam, signal }) =>
      projectsApi.tree(
        projectId,
        repo.id,
        { limit: TREE_PAGE_SIZE, ...(path ? { path } : {}), ...(commit ? { commit } : {}), ...(pageParam ? { cursor: pageParam } : {}) },
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: mode !== 'custom' || custom !== '',
  });
  const entries = tree.data?.pages.flatMap((p) => p.items) ?? [];
  // Файл читается по разрешённому SHA: дерево и файл согласованы, даже если ветка сдвинулась.
  const resolved = tree.data?.pages[0]?.commit_sha ?? null;

  const navigate = (next: string) => {
    setPath(next);
    setFile(null);
  };
  const onOpen = (e: RepositoryTreeEntry) => (e.type === 'directory' ? navigate(e.path) : setFile(e.path));
  const onBranchChange = (next: string) => {
    setBranch(next);
    setChoice('');
    setFile(null);
  };
  const onChoiceChange = (next: CommitChoice) => {
    setChoice(next);
    setFile(null);
    if (next !== 'custom') setDraftError(undefined);
  };
  const applyCustom = (e: FormEvent) => {
    e.preventDefault();
    const value = draft.trim();
    if (!COMMIT_RE.test(value)) {
      setDraftError(t('projects.explorer.commitInvalid'));
      return;
    }
    setDraftError(undefined);
    setCustom(value);
    setFile(null);
  };

  const crumbs = path ? path.split('/') : [];
  return (
    <div className="flex flex-col gap-3" data-testid="repository-explorer">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <label htmlFor={`${uid}-branch`} className="text-sm font-medium text-fg">
            {t('projects.explorer.branch')}
          </label>
          <select id={`${uid}-branch`} className="input" value={branch} onChange={(e) => onBranchChange(e.target.value)} data-testid="explorer-branch">
            {branchNames.map((name) => (
              <option key={name} value={name}>
                {name === repo.default_branch ? t('projects.explorer.branchDefault', { branch: name }) : name}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor={`${uid}-commit`} className="text-sm font-medium text-fg">
            {t('projects.explorer.commit')}
          </label>
          <select id={`${uid}-commit`} className="input max-w-[28rem]" value={choice} onChange={(e) => onChoiceChange(e.target.value)} data-testid="explorer-commit">
            <option value="">{t('projects.explorer.commitDefault', { branch })}</option>
            {recentCommits.length > 0 ? (
              <optgroup label={t('projects.explorer.recentCommits', { branch })}>
                {recentCommits.map((c) => (
                  <option key={c.sha} value={c.sha}>
                    {commitLabel(c)}
                  </option>
                ))}
              </optgroup>
            ) : null}
            {snapshot ? <option value="index">{t('projects.explorer.commitIndex', { sha: snapshot.commit_sha.slice(0, 12) })}</option> : null}
            <option value="custom">{t('projects.explorer.commitCustom')}</option>
          </select>
        </div>
        {branches.isError || recent.isError ? (
          <p className="text-sm text-fg-muted" role="status">
            {t('projects.explorer.historyUnavailable')}
          </p>
        ) : null}
        {mode === 'custom' ? (
          <form className="flex flex-wrap items-end gap-2" onSubmit={applyCustom} noValidate aria-label={t('projects.explorer.commitCustom')}>
            <TextField
              id={`${uid}-commit-custom`}
              label={t('projects.explorer.commitInput')}
              hint={t('projects.explorer.commitInputHint')}
              autoComplete="off"
              spellCheck={false}
              maxLength={255}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              error={draftError}
            />
            <Button type="submit" variant="secondary">
              {t('projects.explorer.commitApply')}
            </Button>
          </form>
        ) : null}
        {resolved ? (
          <p className="text-sm text-fg-muted" data-testid="explorer-resolved-commit">
            {t('projects.explorer.resolved')}{' '}
            <code className="font-mono text-fg" title={resolved}>
              {resolved.slice(0, 12)}
            </code>
          </p>
        ) : null}
      </div>

      <nav aria-label={t('projects.explorer.breadcrumbs')}>
        <ol className="flex flex-wrap items-center gap-1 text-sm">
          <li>
            {path ? (
              <button type="button" className="text-link underline" onClick={() => navigate('')}>
                {t('projects.explorer.root')}
              </button>
            ) : (
              <span aria-current="location" className="font-semibold">
                {t('projects.explorer.root')}
              </span>
            )}
          </li>
          {crumbs.map((name, i) => {
            const target = crumbs.slice(0, i + 1).join('/');
            const last = i === crumbs.length - 1;
            return (
              <li key={target} className="flex items-center gap-1">
                <span aria-hidden="true" className="text-fg-muted">
                  /
                </span>
                {last ? (
                  <span aria-current="location" className="break-all font-mono font-semibold">
                    {name}
                  </span>
                ) : (
                  <button type="button" className="break-all font-mono text-link underline" onClick={() => navigate(target)}>
                    {name}
                  </button>
                )}
              </li>
            );
          })}
        </ol>
      </nav>

      <div className="grid gap-4 lg:grid-cols-[minmax(14rem,1fr),2fr]">
        <div className="min-w-0">
          {mode === 'custom' && custom === '' ? (
            <p className="text-sm text-fg-muted">{t('projects.explorer.commitPrompt')}</p>
          ) : tree.isPending ? (
            <LoadingState label={t('projects.explorer.treeLoading')} />
          ) : tree.isError && entries.length === 0 ? (
            <ErrorState error={tree.error} onRetry={() => void tree.refetch()} />
          ) : (
            <>
              {path ? (
                <button type="button" className="mb-1 text-sm text-link underline" onClick={() => navigate(parentPath(path))}>
                  {t('projects.explorer.up')}
                </button>
              ) : null}
              {entries.length === 0 ? (
                <p className="text-sm text-fg-muted">{t('projects.explorer.emptyDir')}</p>
              ) : (
                <ul aria-label={t('projects.explorer.listLabel', { path: path || '/' })} className="text-sm" data-testid="repository-tree">
                  {entries.map((e) => (
                    <EntryRow key={e.path} entry={e} selected={file === e.path} onOpen={onOpen} />
                  ))}
                </ul>
              )}
              {tree.hasNextPage || tree.isFetchNextPageError ? (
                <LoadMore
                  shown={entries.length}
                  hasNextPage={tree.hasNextPage}
                  isFetchingNextPage={tree.isFetchingNextPage}
                  nextPageError={tree.isFetchNextPageError ? tree.error : null}
                  onLoadMore={() => void tree.fetchNextPage()}
                />
              ) : null}
            </>
          )}
        </div>
        <div className="min-w-0">
          {file && resolved ? (
            <FileView projectId={projectId} repositoryId={repo.id} commit={resolved} path={file} />
          ) : (
            <p className="text-sm text-fg-muted">{t('projects.explorer.selectFile')}</p>
          )}
        </div>
      </div>
    </div>
  );
}

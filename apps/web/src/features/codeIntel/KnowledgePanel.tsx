/**
 * Knowledge Explorer: стек проекта (`listTechnologies`) и источники документации workspace по версиям
 * этого стека (`listDocumentationSources`, последний снимок: статус и время загрузки), режим
 * документации проекта (`getDocumentationPolicy`). Только чтение; регистрация/обновление источников —
 * экран «Документация».
 */
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { knowledgeApi, knowledgeKeys, type DocumentationSource } from '../../api/knowledge';
import { projectKeys, projectsApi, type Technology } from '../../api/projects';
import { Badge, ErrorState, Skeleton, Table, Timestamp, type Column } from '../../components/ui';

export const KNOWLEDGE_TECH_LIMIT = 100;
export const KNOWLEDGE_SOURCES_LIMIT = 200;

function SourceList({ sources }: { sources: readonly DocumentationSource[] }) {
  const { t } = useTranslation();
  if (sources.length === 0) return <span className="text-fg-muted">{t('codeIntel.knowledge.noSources')}</span>;
  return (
    <ul className="flex flex-col gap-1">
      {sources.map((s) => (
        <li key={s.id} className="flex flex-col text-xs" data-source-id={s.id}>
          <span className="break-all font-mono text-sm">{s.uri_or_file_ref}</span>
          <span className="flex flex-wrap items-center gap-1 text-fg-muted">
            <span>{t(`knowledge.enums.source_type.${s.source_type}`, { defaultValue: s.source_type })}</span>·
            <span>{t(`knowledge.enums.trust_level.${s.trust_level}`, { defaultValue: s.trust_level })}</span>
            {!s.enabled ? <Badge tone="neutral">{t('codeIntel.knowledge.disabled')}</Badge> : null}
            {s.latest_snapshot ? (
              <span>
                · {t('codeIntel.knowledge.snapshot', { status: s.latest_snapshot.status })} <Timestamp value={s.latest_snapshot.fetched_at} />
              </span>
            ) : (
              <span>· {t('codeIntel.knowledge.notFetched')}</span>
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function KnowledgePanel({ workspaceId, projectId }: { workspaceId: string; projectId: string }) {
  const { t } = useTranslation();
  const technologies = useQuery({
    queryKey: [...projectKeys.technologies(projectId), KNOWLEDGE_TECH_LIMIT] as const,
    queryFn: ({ signal }) => projectsApi.technologies(projectId, { limit: KNOWLEDGE_TECH_LIMIT }, signal),
  });
  const sources = useQuery({
    queryKey: [...knowledgeKeys.allSources(workspaceId), 'code-intel', KNOWLEDGE_SOURCES_LIMIT] as const,
    queryFn: ({ signal }) => knowledgeApi.listSources(workspaceId, { limit: KNOWLEDGE_SOURCES_LIMIT }, signal),
  });
  const policy = useQuery({ queryKey: knowledgeKeys.policy(projectId), queryFn: ({ signal }) => knowledgeApi.getPolicy(projectId, signal) });

  const byVersion = new Map<string, DocumentationSource[]>();
  for (const s of sources.data?.items ?? []) byVersion.set(s.technology_version_id, [...(byVersion.get(s.technology_version_id) ?? []), s]);

  const columns: Column<Technology>[] = [
    {
      id: 'name',
      header: t('codeIntel.knowledge.col.technology'),
      rowHeader: true,
      sortValue: (x) => x.name,
      cell: (x) => (
        <span className="flex flex-col">
          <span className="font-semibold">{x.name}</span>
          <span className="text-xs text-fg-muted">{x.ecosystem}</span>
        </span>
      ),
    },
    {
      id: 'version',
      header: t('codeIntel.knowledge.col.version'),
      sortValue: (x) => x.version_label,
      cell: (x) => (
        <span className="flex flex-col">
          <span className="font-mono">{x.version_label}</span>
          {x.support_status ? <span className="text-xs text-fg-muted">{x.support_status}</span> : null}
        </span>
      ),
    },
    {
      id: 'detected',
      header: t('codeIntel.knowledge.col.detected'),
      cell: (x) =>
        x.detected_from ? <span className="break-all font-mono text-xs">{x.detected_from}</span> : <span className="text-fg-muted">{t('codeIntel.knowledge.manual')}</span>,
    },
    {
      id: 'sources',
      header: t('codeIntel.knowledge.col.sources'),
      sortValue: (x) => byVersion.get(x.technology_version_id)?.length ?? 0,
      cell: (x) => (sources.isSuccess ? <SourceList sources={byVersion.get(x.technology_version_id) ?? []} /> : <span className="text-fg-muted">—</span>),
    },
  ];

  return (
    <div className="flex flex-col gap-4" data-testid="knowledge-panel">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm">
          {t('codeIntel.knowledge.mode')}{' '}
          {policy.isPending ? (
            <span className="text-fg-muted">…</span>
          ) : policy.isError ? (
            <span className="text-fg-muted">{t('codeIntel.knowledge.modeUnavailable')}</span>
          ) : (
            <Badge tone="info">{t(`enums.documentation_mode.${policy.data.documentation_mode}`, { defaultValue: policy.data.documentation_mode })}</Badge>
          )}
        </p>
        <Link className="link" to={`/w/${workspaceId}/knowledge`}>
          {t('codeIntel.knowledge.toDocs')}
        </Link>
      </div>
      {sources.isError ? <ErrorState title={t('codeIntel.knowledge.sourcesError')} error={sources.error} onRetry={() => void sources.refetch()} /> : null}
      {technologies.isPending ? (
        <Skeleton lines={4} />
      ) : technologies.isError ? (
        <ErrorState error={technologies.error} onRetry={() => void technologies.refetch()} />
      ) : technologies.data.items.length === 0 ? (
        <p className="text-sm text-fg-muted">{t('codeIntel.knowledge.noStack')}</p>
      ) : (
        <Table caption={t('codeIntel.knowledge.caption')} columns={columns} rows={technologies.data.items} rowKey={(x) => x.technology_version_id} density="compact" minWidthClass="min-w-[44rem]" />
      )}
      {technologies.data?.next_cursor ? <p className="text-sm text-fg-muted">{t('codeIntel.knowledge.more', { count: technologies.data.items.length })}</p> : null}
      {sources.data?.next_cursor ? <p className="text-sm text-fg-muted">{t('codeIntel.knowledge.sourcesMore', { count: sources.data.items.length })}</p> : null}
    </div>
  );
}

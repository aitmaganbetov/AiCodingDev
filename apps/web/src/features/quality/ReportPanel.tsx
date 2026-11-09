/**
 * Отчёт run (getRunReport): JSON и тот же отчёт в text/markdown (OQ-45 (d)) — сервер формирует и
 * экранирует содержимое, UI только сохраняет файл. Используется экранами качества и соответствия.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { artifactDownloadHref, qualityApi, type ArtifactRef } from '../../api/quality';
import { Callout } from '../../components/ui/Badge';
import { Button } from '../../components/ui/Button';
import { Timestamp } from '../../components/ui/Time';
import { problemMessage } from '../../lib/problem';
import { Field, FieldList } from './shared';

/** Сохранение текста в файл через object URL (без внешних зависимостей). */
function saveFile(content: string, type: string, filename: string): void {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function saveJson(data: unknown, filename: string): void {
  saveFile(JSON.stringify(data, null, 2), 'application/json', filename);
}

type ReportFormat = 'json' | 'markdown';

export function ReportPanel({ runId }: { runId: string }) {
  const { t } = useTranslation();
  const [state, setState] = useState<{
    loading: ReportFormat | null;
    error: unknown;
    artifact: ArtifactRef | null;
    generatedAt: string | null;
    disclaimer: string | null;
    markdownReady: boolean;
  }>({ loading: null, error: null, artifact: null, generatedAt: null, disclaimer: null, markdownReady: false });
  // OQ-45 (d): тот же отчёт в text/markdown (Accept) — сервер рендерит и экранирует содержимое; UI только сохраняет файл.
  const downloadMarkdown = async () => {
    setState((s) => ({ ...s, loading: 'markdown', error: null }));
    try {
      const report = await qualityApi.runReportMarkdown(runId);
      saveFile(report.text, 'text/markdown;charset=utf-8', report.filename ?? `run-${runId}-report.md`);
      setState((s) => ({ ...s, loading: null, markdownReady: true }));
    } catch (error) {
      setState((s) => ({ ...s, loading: null, error }));
    }
  };
  const download = async () => {
    setState((s) => ({ ...s, loading: 'json', error: null }));
    try {
      const report = await qualityApi.runReport(runId);
      saveJson(report, `run-${runId}-report.json`);
      setState((s) => ({
        ...s,
        loading: null,
        error: null,
        artifact: report.report_artifact ?? null,
        generatedAt: report.generated_at,
        disclaimer: report.disclaimer ?? null,
      }));
    } catch (error) {
      setState((s) => ({ ...s, loading: null, error }));
    }
  };
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-fg-muted">{t('quality.report.description')}</p>
      <div className="flex flex-wrap gap-3">
        <Button variant="primary" loading={state.loading === 'json'} disabled={state.loading !== null} onClick={() => void download()}>
          {t('quality.report.download')}
        </Button>
        <Button loading={state.loading === 'markdown'} disabled={state.loading !== null} onClick={() => void downloadMarkdown()}>
          {t('quality.report.downloadMarkdown')}
        </Button>
      </div>
      <p role="status" className="text-sm text-fg-muted">
        {state.generatedAt ? (
          <>
            {t('quality.report.ready')} <Timestamp value={state.generatedAt} />
          </>
        ) : null}
        {state.markdownReady ? <span className="ml-2">{t('quality.report.markdownReady')}</span> : null}
      </p>
      {state.error ? (
        <p role="alert" className="text-sm font-medium text-danger-text">
          {problemMessage(t, state.error)}
        </p>
      ) : null}
      {state.disclaimer ? <Callout tone="info">{state.disclaimer}</Callout> : null}
      {state.artifact ? (
        <FieldList label={t('quality.report.artifact')}>
          <Field label={t('quality.report.artifact')}>
            <a className="font-medium text-link underline" href={artifactDownloadHref(state.artifact.id)}>
              {t('quality.report.artifactDownload')}
            </a>
          </Field>
          <Field label={t('quality.evidence.sha256')}>
            <code className="break-all font-mono text-xs">{state.artifact.sha256}</code>
          </Field>
        </FieldList>
      ) : null}
    </div>
  );
}

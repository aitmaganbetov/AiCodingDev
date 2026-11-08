/**
 * Evidence & Quality (API_DESIGN §11, QUALITY_GATES): сводка gates/verdict run, evidence и отчёт run.
 * Находки — src/api/findings.ts; матрица соответствия — src/api/compliance.ts.
 */
import { API_BASE, request, requestText, seg, type JsonResponse, type Query, type Schemas, type TextResponse } from './client';

export type QualityReport = Schemas['QualityReport'];
export type QualityVerdict = QualityReport['verdict'];
export type GateExecution = Schemas['GateExecution'];
export type GateExecutionStatus = Schemas['GateExecutionStatus'];
export type Evidence = Schemas['Evidence'];
export type EvidencePage = Schemas['EvidencePage'];
export type EvidenceVerificationStatus = Schemas['EvidenceVerificationStatus'];
export type ArtifactRef = Schemas['ArtifactRef'];
export type RunReport = Schemas['RunReport'];
export type TaskEvidenceQuery = Query<'listTaskEvidence'>;
export type RunEvidenceQuery = Query<'listRunEvidence'>;

export const qualityKeys = {
  run: (runId: string) => ['quality', runId] as const,
  report: (runId: string) => ['quality', runId, 'report'] as const,
  evidence: (evidenceId: string) => ['evidence', evidenceId] as const,
  taskEvidence: (taskId: string, query: TaskEvidenceQuery = {}) => ['evidence', 'task', taskId, query] as const,
  runEvidence: (runId: string, query: RunEvidenceQuery = {}) => ['evidence', 'run', runId, query] as const,
};

/**
 * QUALITY_GATES §5 / ТЗ §14: PASS обязательного gate засчитывается только при статусе PASS.
 * SKIPPED и ERROR — не PASS (AC-11), NOT_APPLICABLE — не требуется.
 */
export function gateCountsAsPass(status: GateExecutionStatus | (string & {})): boolean {
  return status === 'PASS';
}

/** Статусы, которые легко принять за «пройдено», но которые им не являются (выделяются в UI). */
export const GATE_NOT_PASS_LOOKALIKE: readonly string[] = ['SKIPPED', 'ERROR'];

export const qualityApi = {
  runQuality(runId: string, signal?: AbortSignal): Promise<QualityReport> {
    return request({ method: 'GET', path: `/runs/${seg(runId)}/quality`, signal });
  },

  /** Полный отчёт run (application/json). */
  runReport(runId: string, signal?: AbortSignal): Promise<RunReport> {
    return request({ method: 'GET', path: `/runs/${seg(runId)}/report`, signal });
  },

  /** Тот же отчёт в text/markdown (content negotiation по Accept, API_DESIGN §1). */
  runReportMarkdown(runId: string, signal?: AbortSignal): Promise<TextResponse> {
    return requestText({ method: 'GET', path: `/runs/${seg(runId)}/report`, accept: 'text/markdown', signal });
  },

  /** OQ-45 (c): все evidence run (keyset по времени создания), фильтры статуса/задачи/вида. */
  runEvidence(runId: string, query: RunEvidenceQuery = {}, signal?: AbortSignal): Promise<JsonResponse<'listRunEvidence', 200>> {
    return request({ method: 'GET', path: `/runs/${seg(runId)}/evidence`, query, signal });
  },

  evidence(evidenceId: string, signal?: AbortSignal): Promise<Evidence> {
    return request({ method: 'GET', path: `/evidence/${seg(evidenceId)}`, signal });
  },

  taskEvidence(taskId: string, query: TaskEvidenceQuery = {}, signal?: AbortSignal): Promise<JsonResponse<'listTaskEvidence', 200>> {
    return request({ method: 'GET', path: `/tasks/${seg(taskId)}/evidence`, query, signal });
  },
};

/** Ссылка скачивания artifact (GET /artifacts/{id}/download — потоковая отдача через API). */
export function artifactDownloadHref(artifactId: string): string {
  return `${API_BASE}/artifacts/${seg(artifactId)}/download`;
}

/** Task Detail (FR-CHAT-06): задача, попытки, ACP-сообщения, evidence, gate executions, артефакты. */
import { API_BASE, ApiError, parseProblem, request, seg, type Query, type Schemas } from './client';

export type Task = Schemas['Task'];
export type TaskAttempt = Schemas['TaskAttempt'];
export type TaskAttemptPage = Schemas['TaskAttemptPage'];
export type AcpMessage = Schemas['AcpMessage'];
export type AcpMessagePage = Schemas['AcpMessagePage'];
export type Evidence = Schemas['Evidence'];
export type EvidencePage = Schemas['EvidencePage'];
export type ArtifactRef = Schemas['ArtifactRef'];
export type GateExecution = Schemas['GateExecution'];
export type QualityReport = Schemas['QualityReport'];

export const taskKeys = {
  detail: (taskId: string) => ['task', taskId] as const,
  attempts: (taskId: string) => ['task', taskId, 'attempts'] as const,
  messages: (taskId: string) => ['task', taskId, 'messages'] as const,
  evidence: (taskId: string) => ['task', taskId, 'evidence'] as const,
  artifact: (artifactId: string) => ['artifact', artifactId, 'text'] as const,
  quality: (runId: string) => ['run', runId, 'quality'] as const,
};

/** Больше этого артефакт в браузере не открываем (только скачивание). */
export const MAX_INLINE_ARTIFACT_BYTES = 2 * 1024 * 1024;

/** Путь скачивания артефакта (downloadArtifact): строится из id, а не из данных ответа. */
export function artifactDownloadPath(artifactId: string): string {
  return `${API_BASE}/artifacts/${seg(artifactId)}/download`;
}

export const tasksApi = {
  get(taskId: string, signal?: AbortSignal): Promise<Task> {
    return request({ method: 'GET', path: `/tasks/${seg(taskId)}`, signal });
  },

  attempts(taskId: string, query: Query<'listTaskAttempts'> = {}, signal?: AbortSignal): Promise<TaskAttemptPage> {
    return request({ method: 'GET', path: `/tasks/${seg(taskId)}/attempts`, query, signal });
  },

  messages(taskId: string, query: Query<'listTaskMessages'> = {}, signal?: AbortSignal): Promise<AcpMessagePage> {
    return request({ method: 'GET', path: `/tasks/${seg(taskId)}/messages`, query, signal });
  },

  evidence(taskId: string, query: Query<'listTaskEvidence'> = {}, signal?: AbortSignal): Promise<EvidencePage> {
    return request({ method: 'GET', path: `/tasks/${seg(taskId)}/evidence`, query, signal });
  },

  /** Отчёт качества run: gate executions (фильтруются по task_id на клиенте). */
  quality(runId: string, signal?: AbortSignal): Promise<QualityReport> {
    return request({ method: 'GET', path: `/runs/${seg(runId)}/quality`, signal });
  },

  /**
   * Текст артефакта (git_diff) через downloadArtifact: 302 на подписанный URL (fetch следует
   * редиректу) либо потоковая отдача API. Содержимое — недоверенные данные, только как текст.
   */
  async artifactText(artifactId: string, signal?: AbortSignal): Promise<string> {
    let res: Response;
    try {
      res = await fetch(artifactDownloadPath(artifactId), {
        method: 'GET',
        credentials: 'same-origin',
        headers: { Accept: 'text/plain, text/x-diff, application/octet-stream, application/problem+json' },
        signal: signal ?? null,
      });
    } catch (cause) {
      if (signal?.aborted) throw cause;
      throw new ApiError({ status: 0, code: 'network_error', cause });
    }
    const text = await res.text();
    if (res.ok) return text;
    let body: unknown;
    try {
      body = JSON.parse(text) as unknown;
    } catch {
      body = undefined;
    }
    throw parseProblem(res.status, body, res.headers.get('Retry-After'));
  },
};

/**
 * GitHub-интеграция (P8-02…P8-05, OQ-47 (2)): установка GitHub App, проверки на воркере, pull requests.
 *
 * Процесс API не держит ключ App: подключение установки возвращает 201 с установкой в статусе PENDING и
 * `verification_id`, привязка GitHub-репозитория — 202 GitHubVerification; итог проверки — getGitHubVerification.
 */
import { request, seg, type JsonBody, type JsonResponse, type Query, type Schemas } from './client';

export type GitHubInstallation = Schemas['GitHubInstallation'];
export type GitHubVerification = Schemas['GitHubVerification'];
export type GitHubInstallationSetup = Schemas['GitHubInstallationSetup'];
export type GitHubInstallationConnectRequest = JsonBody<'connectGitHubInstallation'>;
export type PullRequest = Schemas['PullRequest'];
export type PullRequestState = Schemas['PullRequestState'];
export type PullRequestCreateRequest = JsonBody<'requestPullRequest'>;
export type PullRequestMergeRequest = JsonBody<'requestPullRequestMerge'>;
export type MergeMethod = NonNullable<PullRequestMergeRequest['merge_method']>;
export type ListPullRequestsQuery = Query<'listPullRequests'>;

export const githubKeys = {
  installations: (workspaceId: string) => ['github', workspaceId, 'installations'] as const,
  verification: (workspaceId: string, verificationId: string) => ['github', workspaceId, 'verification', verificationId] as const,
  pullRequests: (projectId: string, runId?: string) => ['github', 'pull-requests', projectId, runId ?? null] as const,
  pullRequestsAll: (projectId: string) => ['github', 'pull-requests', projectId] as const,
};

export const githubApi = {
  /** state (HMAC, 10 мин, однократный) и URL установки App на GitHub; 503 — App не настроен. */
  startInstallation(workspaceId: string): Promise<JsonResponse<'startGitHubInstallation', 200>> {
    return request({ method: 'POST', path: '/integrations/github/installations/setup', workspaceId });
  },

  /** 201: установка в статусе PENDING; проверку через GitHub API выполняет воркер (`verification_id`). */
  connectInstallation(
    workspaceId: string,
    body: GitHubInstallationConnectRequest,
    idempotencyKey: string,
  ): Promise<JsonResponse<'connectGitHubInstallation', 201>> {
    return request({ method: 'POST', path: '/integrations/github/installations', body, workspaceId, idempotencyKey });
  },

  installations(workspaceId: string, signal?: AbortSignal): Promise<JsonResponse<'listGitHubInstallations', 200>> {
    return request({ method: 'GET', path: '/integrations/github/installations', workspaceId, signal });
  },

  disconnectInstallation(workspaceId: string, id: string): Promise<void> {
    return request({ method: 'DELETE', path: `/integrations/github/installations/${seg(id)}`, workspaceId });
  },

  verification(workspaceId: string, verificationId: string, signal?: AbortSignal): Promise<JsonResponse<'getGitHubVerification', 200>> {
    return request({ method: 'GET', path: `/integrations/github/verifications/${seg(verificationId)}`, workspaceId, signal });
  },

  pullRequests(projectId: string, query: ListPullRequestsQuery = {}, signal?: AbortSignal): Promise<JsonResponse<'listPullRequests', 200>> {
    return request({ method: 'GET', path: `/projects/${seg(projectId)}/pull-requests`, query, signal });
  },

  /** 202: PR откроет воркер через ledger; 403 policy_denied + approval_id — изменения CI-конфигурации требуют MANUAL_REVIEW. */
  requestPullRequest(runId: string, body: PullRequestCreateRequest, idempotencyKey: string): Promise<JsonResponse<'requestPullRequest', 202>> {
    return request({ method: 'POST', path: `/runs/${seg(runId)}/pull-requests`, body, idempotencyKey });
  },

  /** 202: approval MERGE (four-eyes); merge выполнит воркер после утверждения. */
  requestMerge(
    projectId: string,
    pullRequestId: string,
    body: PullRequestMergeRequest,
    idempotencyKey: string,
  ): Promise<JsonResponse<'requestPullRequestMerge', 202>> {
    return request({
      method: 'POST',
      path: `/projects/${seg(projectId)}/pull-requests/${seg(pullRequestId)}/merge-request`,
      body,
      idempotencyKey,
    });
  },
};

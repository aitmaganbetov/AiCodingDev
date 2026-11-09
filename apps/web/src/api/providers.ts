/**
 * CLI-провайдеры (API_DESIGN §15): только health, capabilities и безопасные workspace-настройки.
 * API не выполняет login и не принимает credentials — авторизация CLI делается штатной командой
 * CLI на хосте (runbook), UI лишь отражает auth_status.
 */
import { request, seg, type JsonBody, type JsonResponse, type Query, type Schemas } from './client';

export type Provider = Schemas['Provider'];
export type ProviderInstallation = Schemas['ProviderInstallation'];
export type ProviderSettings = Schemas['ProviderSettings'];
export type ProviderSettingsUpdateRequest = JsonBody<'updateProviderSettings'>;
export type AgentRole = Schemas['AgentRole'];
export type AuthStatus = Schemas['AuthStatus'];
export type ProviderAvailability = Schemas['ProviderAvailability'];
export type ProbeAccepted = Schemas['ProbeAccepted'];
export type ProviderProbeStatus = Schemas['ProviderProbeStatus'];
export type ProviderExecution = Schemas['ProviderExecution'];
export type ProviderExecutionPage = Schemas['ProviderExecutionPage'];
export type ExecutionStatus = Schemas['ExecutionStatus'];
export type ProviderExecutionsQuery = Query<'listProviderExecutions'>;

export const providerKeys = {
  list: (workspaceId: string) => ['providers', workspaceId] as const,
  probe: (workspaceId: string, providerId: string, probeRequestId: string) =>
    ['providers', workspaceId, providerId, 'probe', probeRequestId] as const,
  /** История исполнений CLI провайдера (B1, G-03). */
  executions: (workspaceId: string, providerId: string, query: ProviderExecutionsQuery = {}) =>
    ['providers', workspaceId, providerId, 'executions', query] as const,
};

export const providersApi = {
  list(workspaceId: string, signal?: AbortSignal): Promise<JsonResponse<'listProviders', 200>> {
    return request({ method: 'GET', path: '/providers', workspaceId, signal });
  },

  /** 202: перепроверка поставлена на worker-exec (лимит 6/час на workspace). */
  probe(workspaceId: string, providerId: string, idempotencyKey: string): Promise<JsonResponse<'probeProvider', 202>> {
    return request({ method: 'POST', path: `/providers/${seg(providerId)}/probe`, workspaceId, idempotencyKey });
  },

  /** Статус запроса проверки (ProbeAccepted.status_url): PENDING → COMPLETED | FAILED | EXPIRED. */
  probeStatus(
    workspaceId: string,
    providerId: string,
    probeRequestId: string,
    signal?: AbortSignal,
  ): Promise<JsonResponse<'getProviderProbe', 200>> {
    return request({ method: 'GET', path: `/providers/${seg(providerId)}/probes/${seg(probeRequestId)}`, workspaceId, signal });
  },

  /**
   * Исполнения CLI провайдера (cli_executions, B1): статус, код выхода, длительность, категория ошибки,
   * ссылки на задачу/попытку/run. Без вывода CLI, команд, путей хоста и учётных данных; новые сверху.
   */
  executions(
    workspaceId: string,
    providerId: string,
    query: ProviderExecutionsQuery = {},
    signal?: AbortSignal,
  ): Promise<ProviderExecutionPage> {
    return request({ method: 'GET', path: `/providers/${seg(providerId)}/executions`, query, workspaceId, signal });
  },

  updateSettings(
    workspaceId: string,
    providerId: string,
    body: ProviderSettingsUpdateRequest,
    settingsVersion: number,
  ): Promise<JsonResponse<'updateProviderSettings', 200>> {
    return request({ method: 'PATCH', path: `/providers/${seg(providerId)}/settings`, body, workspaceId, ifMatchVersion: settingsVersion });
  },
};

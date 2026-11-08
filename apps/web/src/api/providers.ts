/**
 * CLI-провайдеры (API_DESIGN §15): только health, capabilities и безопасные workspace-настройки.
 * API не выполняет login и не принимает credentials — авторизация CLI делается штатной командой
 * CLI на хосте (runbook), UI лишь отражает auth_status.
 */
import { request, seg, type JsonBody, type JsonResponse, type Schemas } from './client';

export type Provider = Schemas['Provider'];
export type ProviderInstallation = Schemas['ProviderInstallation'];
export type ProviderSettings = Schemas['ProviderSettings'];
export type ProviderSettingsUpdateRequest = JsonBody<'updateProviderSettings'>;
export type AgentRole = Schemas['AgentRole'];
export type AuthStatus = Schemas['AuthStatus'];
export type ProviderAvailability = Schemas['ProviderAvailability'];

export const providerKeys = {
  list: (workspaceId: string) => ['providers', workspaceId] as const,
};

export const providersApi = {
  list(workspaceId: string, signal?: AbortSignal): Promise<JsonResponse<'listProviders', 200>> {
    return request({ method: 'GET', path: '/providers', workspaceId, signal });
  },

  /** 202: перепроверка поставлена на worker-exec (лимит 6/час на workspace). */
  probe(workspaceId: string, providerId: string, idempotencyKey: string): Promise<JsonResponse<'probeProvider', 202>> {
    return request({ method: 'POST', path: `/providers/${seg(providerId)}/probe`, workspaceId, idempotencyKey });
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

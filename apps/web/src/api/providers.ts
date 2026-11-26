/**
 * CLI-провайдеры (API_DESIGN §15): health, capabilities, безопасные workspace-настройки и device-code вход.
 *
 * API не принимает credentials и не проксирует OAuth (ТЗ §1, ADR-0003): вход выполняет официальный CLI на
 * хосте исполнения, а UI показывает лишь публичную часть device-code потока — ссылку и код, которые оператор
 * вводит сам на сайте провайдера. Токен платформа не видит: его пишет сам CLI в свой credential store.
 */
import { request, seg, type JsonBody, type JsonResponse, type Query, type Schemas } from './client';

export type Provider = Schemas['Provider'];
export type SubscriptionLimits = Schemas['SubscriptionLimits'];
export type SubscriptionLimitWindow = Schemas['SubscriptionLimitWindow'];
export type ProviderInstallation = Schemas['ProviderInstallation'];
export type ProviderSettings = Schemas['ProviderSettings'];
export type ProviderSettingsUpdateRequest = JsonBody<'updateProviderSettings'>;
export type AgentRole = Schemas['AgentRole'];
export type AuthStatus = Schemas['AuthStatus'];
export type ProviderAvailability = Schemas['ProviderAvailability'];
export type ProbeAccepted = Schemas['ProbeAccepted'];
export type ProviderProbeStatus = Schemas['ProviderProbeStatus'];
export type LoginAccepted = Schemas['LoginAccepted'];
export type ProviderLoginStatus = Schemas['ProviderLoginStatus'];
export type ProviderLoginState = ProviderLoginStatus['state'];
export type ProviderLoginCodeRequest = JsonBody<'submitProviderLoginCode'>;
export type ProviderExecution = Schemas['ProviderExecution'];
export type ProviderExecutionPage = Schemas['ProviderExecutionPage'];
export type ExecutionStatus = Schemas['ExecutionStatus'];
export type ProviderExecutionsQuery = Query<'listProviderExecutions'>;

export const providerKeys = {
  list: (workspaceId: string) => ['providers', workspaceId] as const,
  probe: (workspaceId: string, providerId: string, probeRequestId: string) =>
    ['providers', workspaceId, providerId, 'probe', probeRequestId] as const,
  login: (workspaceId: string, providerId: string, loginSessionId: string) =>
    ['providers', workspaceId, providerId, 'login', loginSessionId] as const,
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

  /** 202: проверка установки плюс один минимальный запрос CLI — он и сообщает лимиты подписки (тратит подписку). */
  refreshLimits(workspaceId: string, providerId: string, idempotencyKey: string): Promise<JsonResponse<'refreshProviderLimits', 202>> {
    return request({ method: 'POST', path: `/providers/${seg(providerId)}/limits/refresh`, workspaceId, idempotencyKey });
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
   * 202: сессия device-code входа создана, worker-exec запускает официальную команду входа CLI.
   * Требует роли owner и свежей re-аутентификации (step-up). Активная сессия уже есть ⇒ 409.
   */
  login(workspaceId: string, providerId: string, idempotencyKey: string): Promise<JsonResponse<'loginProvider', 202>> {
    return request({ method: 'POST', path: `/providers/${seg(providerId)}/login`, workspaceId, idempotencyKey });
  },

  /** Состояние сессии входа (LoginAccepted.status_url): PENDING → CHALLENGE → AUTHENTICATED | FAILED | CANCELLED | EXPIRED. */
  loginStatus(
    workspaceId: string,
    providerId: string,
    loginSessionId: string,
    signal?: AbortSignal,
  ): Promise<JsonResponse<'getProviderLogin', 200>> {
    return request({ method: 'GET', path: `/providers/${seg(providerId)}/logins/${seg(loginSessionId)}`, workspaceId, signal });
  },

  /**
   * 202: код авторизации из браузера оператора передан процессу входа CLI (Claude Code: «Paste code here»).
   *
   * Платформа код не хранит: он уходит в stdin процесса на хосте исполнения и нигде не сохраняется — ни в БД,
   * ни в журнале аудита (там только факт), ни в ответе. Обменять его может только сам CLI (OAuth/PKCE).
   * Показывать поле ввода следует, пока `ProviderLoginStatus.awaiting_code` = true. Сессия завершена ⇒ 409.
   */
  submitLoginCode(
    workspaceId: string,
    providerId: string,
    loginSessionId: string,
    code: string,
    idempotencyKey: string,
  ): Promise<JsonResponse<'submitProviderLoginCode', 202>> {
    return request({
      method: 'POST',
      path: `/providers/${seg(providerId)}/logins/${seg(loginSessionId)}/code`,
      workspaceId,
      idempotencyKey,
      body: { code },
    });
  },

  /** Отмена сессии входа: worker-exec снимает процесс CLI. Идемпотентно. */
  cancelLogin(
    workspaceId: string,
    providerId: string,
    loginSessionId: string,
    idempotencyKey: string,
  ): Promise<JsonResponse<'cancelProviderLogin', 202>> {
    return request({
      method: 'POST',
      path: `/providers/${seg(providerId)}/logins/${seg(loginSessionId)}/cancel`,
      workspaceId,
      idempotencyKey,
    });
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

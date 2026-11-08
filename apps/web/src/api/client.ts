/**
 * Типизированный fetch-клиент Internal REST API v1.
 *
 * Правила (docs/api/API_DESIGN.md):
 *  - §3: cookie-сессия (credentials: same-origin) + X-CSRF-Token на небезопасных методах;
 *  - §4: X-Workspace-Id для коллекций без родительского ресурса (GET/POST /projects);
 *  - §6: ошибки RFC 7807 application/problem+json → ApiError;
 *  - §8: Idempotency-Key на POST-командах, один ключ на «намерение пользователя»,
 *        переиспользуется при повторах.
 * Типы запросов/ответов берутся только из сгенерированного schema.d.ts (ADR-0013).
 *
 * Здесь только ядро транспорта. Операции — в модулях областей `src/api/<area>.ts`
 * (auth, workspaces, projects, runs, …): новая область = новый файл, client.ts не меняется.
 */
import type { components, operations } from './schema';

export const API_BASE = '/api/v1';
export const CSRF_COOKIE = 'aicd_csrf';

export type Schemas = components['schemas'];
export type Problem = Schemas['Problem'];
export type FieldError = Schemas['FieldError'];

/**
 * Помощники типов для модулей областей (src/api/<area>.ts): тело запроса, тело ответа
 * и query-параметры операции по её operationId из сгенерированного schema.d.ts.
 */
export type JsonBody<Op extends keyof operations> = NonNullable<operations[Op]['requestBody']> extends {
  content: { 'application/json': infer B };
}
  ? B
  : never;
export type JsonResponse<Op extends keyof operations, S extends keyof operations[Op]['responses']> =
  operations[Op]['responses'][S] extends { content: { 'application/json': infer B } } ? B : never;
export type Query<Op extends keyof operations> = NonNullable<operations[Op]['parameters']['query']>;

/** Сегмент пути из идентификатора (encodeURIComponent): `/runs/${seg(id)}`. */
export function seg(id: string): string {
  return encodeURIComponent(id);
}

/** Коды, которые клиент порождает сам (их нет в контракте). */
export type ClientErrorCode = 'network_error' | 'unknown_error';
/** Неизвестные коды сервера допустимы (API_DESIGN §16: толерантность к расширению enum). */
export type ApiErrorCode = Problem['code'] | ClientErrorCode | (string & {});

export class ApiError extends Error {
  readonly status: number;
  readonly code: ApiErrorCode;
  readonly title: string | undefined;
  readonly detail: string | undefined;
  readonly errors: FieldError[];
  readonly requestId: string | undefined;
  readonly retryAfterSeconds: number | undefined;
  readonly problem: Problem | undefined;

  constructor(init: {
    status: number;
    code: ApiErrorCode;
    title?: string | undefined;
    detail?: string | undefined;
    errors?: FieldError[] | undefined;
    requestId?: string | undefined;
    retryAfterSeconds?: number | undefined;
    problem?: Problem | undefined;
    cause?: unknown;
  }) {
    super(init.detail ?? init.title ?? init.code, { cause: init.cause });
    this.name = 'ApiError';
    this.status = init.status;
    this.code = init.code;
    this.title = init.title;
    this.detail = init.detail;
    this.errors = init.errors ?? [];
    this.requestId = init.requestId;
    this.retryAfterSeconds = init.retryAfterSeconds;
    this.problem = init.problem;
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function parseFieldErrors(v: unknown): FieldError[] {
  if (!Array.isArray(v)) return [];
  const out: FieldError[] = [];
  for (const item of v) {
    if (isRecord(item) && typeof item.pointer === 'string' && typeof item.code === 'string') {
      const fe: FieldError = { pointer: item.pointer, code: item.code };
      if (typeof item.message === 'string') fe.message = item.message;
      out.push(fe);
    }
  }
  return out;
}

/** Разбор тела ответа об ошибке. Не-problem ответы (прокси, HTML) дают unknown_error. */
export function parseProblem(status: number, body: unknown, retryAfterHeader?: string | null): ApiError {
  const headerRetry = retryAfterHeader != null && /^\d+$/.test(retryAfterHeader) ? Number(retryAfterHeader) : undefined;
  if (isRecord(body) && typeof body.code === 'string') {
    const problem = body as unknown as Problem;
    return new ApiError({
      status: typeof body.status === 'number' ? body.status : status,
      code: body.code,
      title: typeof body.title === 'string' ? body.title : undefined,
      detail: typeof body.detail === 'string' ? body.detail : undefined,
      errors: parseFieldErrors(body.errors),
      requestId: typeof body.request_id === 'string' ? body.request_id : undefined,
      retryAfterSeconds: typeof body.retry_after_seconds === 'number' ? body.retry_after_seconds : headerRetry,
      problem,
    });
  }
  return new ApiError({ status, code: 'unknown_error', retryAfterSeconds: headerRetry });
}

// ---------------------------------------------------------------------------
// CSRF и обработка 401
// ---------------------------------------------------------------------------

let csrfToken: string | null = null;

/** Токен из SessionInfo.csrf_token (приоритетнее cookie). null — сбросить. */
export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

export function readCsrfCookie(cookie: string = typeof document === 'undefined' ? '' : document.cookie): string | null {
  for (const part of cookie.split(';')) {
    const idx = part.indexOf('=');
    if (idx < 0) continue;
    if (part.slice(0, idx).trim() === CSRF_COOKIE) {
      const raw = part.slice(idx + 1).trim();
      try {
        return decodeURIComponent(raw) || null;
      } catch {
        return raw || null;
      }
    }
  }
  return null;
}

export function currentCsrfToken(): string | null {
  return csrfToken ?? readCsrfCookie();
}

type UnauthorizedHandler = (error: ApiError) => void;
let unauthorizedHandler: UnauthorizedHandler = () => {
  if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
    window.location.assign('/login');
  }
};

/** Обработчик 401 (по умолчанию — переход на /login). Возвращает функцию восстановления. */
export function setUnauthorizedHandler(handler: UnauthorizedHandler): () => void {
  const prev = unauthorizedHandler;
  unauthorizedHandler = handler;
  return () => {
    unauthorizedHandler = prev;
  };
}

// ---------------------------------------------------------------------------
// Idempotency-Key
// ---------------------------------------------------------------------------

export function createIdempotencyKey(): string {
  return crypto.randomUUID();
}

function stableStringify(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value)
      .sort()
      .filter((k) => value[k] !== undefined)
      .map((k) => `${JSON.stringify(k)}:${stableStringify(value[k])}`)
      .join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}

/**
 * «Намерение пользователя» (API_DESIGN §8): повторная отправка того же тела
 * (после сетевой ошибки, 5xx, двойного клика) идёт с тем же ключом; изменённое
 * тело — новое намерение и новый ключ; после успеха ключ сбрасывается.
 */
export class IdempotentIntent {
  private key: string | null = null;
  private fingerprint: string | null = null;

  keyFor(body: unknown): string {
    const fp = stableStringify(body);
    if (this.key === null || fp !== this.fingerprint) {
      this.key = createIdempotencyKey();
      this.fingerprint = fp;
    }
    return this.key;
  }

  complete(): void {
    this.key = null;
    this.fingerprint = null;
  }
}

// ---------------------------------------------------------------------------
// Транспорт
// ---------------------------------------------------------------------------

export const clientConfig = {
  /** Пауза перед автоматическим повтором (мс); в тестах уменьшается. */
  retryDelayMs: 400,
  /** Максимум попыток для безопасных повторов (GET или POST с Idempotency-Key). */
  maxAttempts: 3,
};

type Method = 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE';

export interface RequestOptions {
  method: Method;
  path: string;
  query?: Record<string, string | number | readonly string[] | undefined | null> | undefined;
  body?: unknown;
  idempotencyKey?: string | undefined;
  workspaceId?: string | undefined;
  /** Optimistic locking (API_DESIGN §9): If-Match: "v<version>". */
  ifMatchVersion?: number | undefined;
  /** false — не отправлять X-CSRF-Token (только POST /auth/login). */
  csrf?: boolean;
  /** false — 401 не вызывает переход на вход (login, session, logout). */
  redirectOn401?: boolean;
  signal?: AbortSignal | undefined;
}

function buildUrl(path: string, query: RequestOptions['query']): string {
  const params = new URLSearchParams();
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (Array.isArray(v)) for (const item of v) params.append(k, String(item));
      else if (v !== undefined && v !== null && v !== '') params.set(k, String(v));
    }
  }
  const qs = params.toString();
  return `${API_BASE}${path}${qs ? `?${qs}` : ''}`;
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => {
      clearTimeout(t);
      reject(signal.reason);
    });
  });
}

async function readBody(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

export async function request<T>(opts: RequestOptions): Promise<T> {
  const unsafe = opts.method !== 'GET';
  const headers: Record<string, string> = { Accept: 'application/json, application/problem+json' };
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  if (unsafe && opts.csrf !== false) {
    const token = currentCsrfToken();
    if (token) headers['X-CSRF-Token'] = token;
  }
  if (opts.idempotencyKey) headers['Idempotency-Key'] = opts.idempotencyKey;
  if (opts.workspaceId) headers['X-Workspace-Id'] = opts.workspaceId;
  if (opts.ifMatchVersion !== undefined) headers['If-Match'] = `"v${opts.ifMatchVersion}"`;

  // Повторять можно только то, что безопасно: GET или POST с Idempotency-Key.
  const retryable = !unsafe || Boolean(opts.idempotencyKey);
  const url = buildUrl(opts.path, opts.query);
  const init: RequestInit = {
    method: opts.method,
    headers,
    credentials: 'same-origin',
    signal: opts.signal ?? null,
  };
  if (opts.body !== undefined) init.body = JSON.stringify(opts.body);

  for (let attempt = 1; ; attempt++) {
    let res: Response;
    try {
      res = await fetch(url, init);
    } catch (cause) {
      if (opts.signal?.aborted) throw cause;
      if (retryable && attempt < clientConfig.maxAttempts) {
        await sleep(clientConfig.retryDelayMs * attempt, opts.signal);
        continue;
      }
      throw new ApiError({ status: 0, code: 'network_error', cause });
    }

    if (res.ok) {
      if (res.status === 204) return undefined as T;
      return (await readBody(res)) as T;
    }

    const error = parseProblem(res.status, await readBody(res), res.headers.get('Retry-After'));
    if (
      retryable &&
      attempt < clientConfig.maxAttempts &&
      (error.code === 'idempotency_key_in_flight' || (!unsafe && res.status === 503))
    ) {
      const waitMs = Math.min((error.retryAfterSeconds ?? 0) * 1000, 5000) || clientConfig.retryDelayMs * attempt;
      await sleep(waitMs, opts.signal);
      continue;
    }
    if (res.status === 401 && opts.redirectOn401 !== false) unauthorizedHandler(error);
    throw error;
  }
}

/**
 * Открывает поток text/event-stream (GET /runs/{id}/events). Возвращает Response с телом-потоком;
 * не-2xx ответ разбирается в ApiError (401 — через общий обработчик). Повторы — забота вызывающего
 * (см. src/api/sse.ts): только он знает последний seq.
 */
export async function openEventStream(opts: {
  path: string;
  query?: RequestOptions['query'];
  lastEventId?: string | undefined;
  signal: AbortSignal;
}): Promise<Response> {
  const headers: Record<string, string> = { Accept: 'text/event-stream' };
  if (opts.lastEventId !== undefined) headers['Last-Event-ID'] = opts.lastEventId;
  let res: Response;
  try {
    res = await fetch(buildUrl(opts.path, opts.query), {
      method: 'GET',
      headers,
      credentials: 'same-origin',
      cache: 'no-store',
      signal: opts.signal,
    });
  } catch (cause) {
    if (opts.signal.aborted) throw cause;
    throw new ApiError({ status: 0, code: 'network_error', cause });
  }
  if (res.ok) return res;
  const error = parseProblem(res.status, await readBody(res), res.headers.get('Retry-After'));
  if (res.status === 401) unauthorizedHandler(error);
  throw error;
}

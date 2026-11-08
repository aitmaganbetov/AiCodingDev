/**
 * Валидация тел запросов/ответов моков по packages/contracts/openapi.yaml.
 * Схемы берутся из самого контракта (OpenAPI 3.1 = JSON Schema 2020-12),
 * поэтому моки не могут незаметно разойтись с контрактом.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import Ajv2020, { type ValidateFunction } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { parse } from 'yaml';

const SPEC_PATH = resolve(dirname(fileURLToPath(import.meta.url)), '../../../../packages/contracts/openapi.yaml');
const SPEC_ID = 'urn:aicd:openapi';

type Json = Record<string, unknown>;

const spec = parse(readFileSync(SPEC_PATH, 'utf8')) as Json & { paths: Record<string, Record<string, Json>> };

const ajv = new Ajv2020({ strict: false, allErrors: true, validateSchema: false });
addFormats(ajv);
ajv.addFormat('int64', true);
ajv.addFormat('int32', true);
ajv.addSchema({ ...spec, $id: SPEC_ID });

const METHODS = ['get', 'post', 'put', 'patch', 'delete'] as const;

interface OperationRef {
  path: string;
  method: (typeof METHODS)[number];
  op: Json;
}

function findOperation(operationId: string): OperationRef {
  for (const [path, item] of Object.entries(spec.paths)) {
    for (const method of METHODS) {
      const op = item[method] as Json | undefined;
      if (op?.operationId === operationId) return { path, method, op };
    }
  }
  throw new Error(`operationId ${operationId} not found in contract`);
}

const escape = (s: string) => s.replace(/~/g, '~0').replace(/\//g, '~1');

function resolveLocal(ref: string): { value: Json; pointer: string } {
  const pointer = ref.replace(/^#/, '');
  let cur: unknown = spec;
  for (const raw of pointer.split('/').slice(1)) {
    const key = raw.replace(/~1/g, '/').replace(/~0/g, '~');
    cur = (cur as Json)[key];
  }
  return { value: cur as Json, pointer };
}

const cache = new Map<string, ValidateFunction>();
function compilePointer(pointer: string): ValidateFunction {
  let fn = cache.get(pointer);
  if (!fn) {
    fn = ajv.compile({ $ref: `${SPEC_ID}#${pointer}` });
    cache.set(pointer, fn);
  }
  return fn;
}

function format(fn: ValidateFunction): string {
  return ajv.errorsText(fn.errors, { separator: '; ' });
}

/** Ошибка валидации тела ответа или null. */
/** `mediaType` — представление ответа (content negotiation, например text/markdown отчёта run); по умолчанию первое. */
export function validateResponse(operationId: string, status: number, body: unknown, mediaType?: string): string | null {
  const { path, method, op } = findOperation(operationId);
  const responses = op.responses as Json;
  let base = `/paths/${escape(path)}/${method}/responses/${status}`;
  let response = responses[String(status)] as Json | undefined;
  if (!response) return `${operationId}: status ${status} is not declared in the contract`;
  if (typeof response.$ref === 'string') {
    const r = resolveLocal(response.$ref);
    response = r.value;
    base = r.pointer;
  }
  const content = response.content as Json | undefined;
  if (!content) return body === undefined ? null : `${operationId} ${status}: contract declares no body`;
  if (mediaType !== undefined && !(mediaType in content)) return `${operationId} ${status}: media type ${mediaType} is not declared`;
  const chosen = mediaType ?? Object.keys(content)[0];
  if (!chosen) return null;
  const fn = compilePointer(`${base}/content/${escape(chosen)}/schema`);
  return fn(body) ? null : `${operationId} ${status} response: ${format(fn)}`;
}

export function responseMediaType(operationId: string, status: number): string | null {
  const { op } = findOperation(operationId);
  let response = (op.responses as Json)[String(status)] as Json | undefined;
  if (response && typeof response.$ref === 'string') response = resolveLocal(response.$ref).value;
  const content = response?.content as Json | undefined;
  return content ? (Object.keys(content)[0] ?? null) : null;
}

/** Ошибка валидации тела запроса (что отправил UI) или null. */
export function validateRequest(operationId: string, body: unknown): string | null {
  const { path, method, op } = findOperation(operationId);
  const requestBody = op.requestBody as Json | undefined;
  if (!requestBody) return body === undefined ? null : `${operationId}: contract declares no request body`;
  if (body === undefined) return requestBody.required === true ? `${operationId}: request body is required` : null;
  const fn = compilePointer(`/paths/${escape(path)}/${method}/requestBody/content/application~1json/schema`);
  return fn(body) ? null : `${operationId} request: ${format(fn)}`;
}

/** Параметры-заголовки операции (Idempotency-Key, X-Workspace-Id) из контракта. */
export function requiredHeaders(operationId: string): { name: string; schema: Json }[] {
  const { op } = findOperation(operationId);
  const params = (op.parameters as Json[] | undefined) ?? [];
  return params
    .map((p) => (typeof p.$ref === 'string' ? resolveLocal(p.$ref).value : p))
    .filter((p) => p.in === 'header' && p.required === true)
    .map((p) => ({ name: String(p.name), schema: p.schema as Json }));
}

export function validateHeader(schema: Json, value: string): boolean {
  return ajv.validate(schema, value);
}

/** Требует ли операция CSRF (глобальная security со схемой csrfHeader). */
export function requiresCsrf(operationId: string): boolean {
  const { op } = findOperation(operationId);
  const security = (op.security ?? spec.security) as Json[] | undefined;
  return Boolean(security?.some((s) => 'csrfHeader' in s));
}

const SSE_SCHEMA_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../../../packages/contracts/schemas/events/1.0/sse-event.schema.json',
);
let sseValidator: ValidateFunction | null = null;

/**
 * Ошибка валидации SSE-конверта (кадр `data:` потока streamRunEvents) или null. Схема —
 * нормативная `schemas/events/1.0/sse-event.schema.json` (EVENT_SCHEMAS §3, по ней пишет сервер).
 */
export function validateSseEvent(envelope: unknown): string | null {
  sseValidator ??= ajv.compile(JSON.parse(readFileSync(SSE_SCHEMA_PATH, 'utf8')) as Json);
  return sseValidator(envelope) ? null : `SSE event: ${format(sseValidator)}`;
}

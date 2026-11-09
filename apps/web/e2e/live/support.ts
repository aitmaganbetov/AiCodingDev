import { expect, test, type Page } from '@playwright/test';

/**
 * Обвязка live E2E (P7-14): настоящий стенд tests/support/web_live_stack.py — API + воркеры
 * (outbox/orchestrator/exec) + PostgreSQL + Redis + S3 + Fake CLI, SPA и API на одном origin.
 * Управляющий HTTP стенда (AICD_E2E_LIVE_CONTROL_URL) — только темп сценария и сбои инфраструктуры:
 * запуск/остановка воркера исполнения, жёсткий обрыв процесса API. Backend не подменяется.
 */
export const LIVE_URL = process.env.AICD_E2E_LIVE_URL;
export const CONTROL_URL = process.env.AICD_E2E_LIVE_CONTROL_URL;

export interface LiveFixture {
  email: string;
  password: string;
  workspace_id: string;
  project_id: string;
}

export interface LiveProject {
  project_id: string;
  project_name: string;
  workspace_id: string;
}

/** Пропуск spec, если стенд не задан (как live smoke). */
export function requireStack(): void {
  test.skip(!LIVE_URL || !CONTROL_URL, 'AICD_E2E_LIVE_URL / AICD_E2E_LIVE_CONTROL_URL не заданы — live E2E со стендом пропущены');
}

async function call<T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${CONTROL_URL}${path}`, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = (await res.json()) as T & { error?: string };
  if (!res.ok) throw new Error(`stack control ${method} ${path}: ${res.status} ${data.error ?? ''}`);
  return data;
}

export const stack = {
  fixture: () => call<LiveFixture>('GET', '/fixture'),
  /** Новый проект с локальным git-репозиторием (изоляция сценариев друг от друга). */
  project: (name: string) => call<LiveProject>('POST', '/project', { name }),
  /** Воркер исполнения: остановка (задачи остаются READY в очереди) / запуск — темп сценария. */
  stopExec: () => call('POST', '/exec/stop'),
  startExec: () => call('POST', '/exec/start'),
  /** Процесс API: SIGKILL (обрыв SSE на стороне сервера) / запуск. */
  killApi: () => call('POST', '/api/kill'),
  startApi: () => call('POST', '/api/start'),
};

export async function loginLive(page: Page, fx: LiveFixture): Promise<void> {
  await page.goto('/login');
  await page.getByLabel('Электронная почта').fill(fx.email);
  await page.getByLabel('Пароль').fill(fx.password);
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page).not.toHaveURL(/\/login/);
}

/**
 * Запуск DEVELOP run через UI проекта; возвращает id run. С F4 после запуска открывается AI Mission Control
 * (`/w/:ws/mission-control?run=<id>`: прежний путь run перенаправляется туда).
 */
export async function startDevelopRun(page: Page, project: LiveProject, objective: string): Promise<string> {
  await page.goto(`/w/${project.workspace_id}/projects/${project.project_id}/runs`);
  await expect(page.getByRole('heading', { level: 1, name: `Запуски · ${project.project_name}` })).toBeVisible();
  await page.getByLabel('Режим').selectOption('DEVELOP');
  await page.getByLabel('Цель').fill(objective);
  await page.getByRole('button', { name: 'Запустить' }).click();
  await expect(page).toHaveURL(new RegExp(`/w/${project.workspace_id}/mission-control\\?run=[0-9a-f-]{36}`));
  const runId = new URL(page.url()).searchParams.get('run') ?? '';
  expect(runId).toMatch(/^[0-9a-f-]{36}$/);
  return runId;
}

/** Прежний адрес экрана run (обзор/граф/чат перенаправляются в Mission Control; Task Detail — `${runBase}/tasks/:id`). */
export function runBase(project: LiveProject, runId: string): string {
  return `/w/${project.workspace_id}/projects/${project.project_id}/runs/${runId}`;
}

/** AI Mission Control выбранного run (`view`: conversation | cli | system | directives | evidence). */
export function missionControl(project: LiveProject, runId: string, query: Record<string, string> = {}): string {
  return `/w/${project.workspace_id}/mission-control?${new URLSearchParams({ run: runId, ...query }).toString()}`;
}

/** Статус run на Run view (dd «Статус»), ждёт текста без перезагрузки — обновления приходят по SSE. */
export async function expectRunStatus(page: Page, label: string, timeout = 60_000): Promise<void> {
  await expect(page.getByTestId('run-status')).toHaveText(label, { timeout });
}

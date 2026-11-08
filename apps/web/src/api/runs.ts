/** Runs и задачи run (API_DESIGN §11, §13). Поток событий — src/api/runEvents.ts. */
import { request, seg, type JsonBody, type JsonResponse, type Query, type Schemas } from './client';

export type RunMode = Schemas['RunMode'];
export type RunStatus = Schemas['RunStatus'];
export type RunSummary = Schemas['RunSummary'];
export type Run = Schemas['Run'];
export type RunPage = Schemas['RunPage'];
export type TaskStatus = Schemas['TaskStatus'];
export type TaskSummary = Schemas['TaskSummary'];
export type TaskGraph = Schemas['TaskGraph'];
export type TaskDependency = Schemas['TaskDependency'];
export type AgentRole = Schemas['AgentRole'];
export type CliProvider = Schemas['CliProvider'];
export type ActivityEvent = Schemas['ActivityEvent'];
export type ActivityPage = Schemas['ActivityPage'];
export type RunActivityQuery = Query<'getRunActivity'>;
export type RunCommand = Schemas['RunCommandAccepted']['command'];
export type RunCreateRequest = JsonBody<'createRun'>;
export type ListRunsQuery = Query<'listRuns'>;
export type RunTasksQuery = Query<'getRunTasks'>;

/** Ключи TanStack Query: инвалидация по событиям SSE из любого экрана. */
export const runKeys = {
  list: (workspaceId: string, projectId?: string) => ['runs', workspaceId, projectId ?? null] as const,
  detail: (runId: string) => ['run', runId] as const,
  tasks: (runId: string) => ['run', runId, 'tasks'] as const,
  activity: (runId: string, taskId?: string) => ['run', runId, 'activity', taskId ?? null] as const,
};

export const runsApi = {
  list(workspaceId: string, query: ListRunsQuery = {}, signal?: AbortSignal): Promise<RunPage> {
    return request({ method: 'GET', path: '/runs', query, workspaceId, signal });
  },

  create(body: RunCreateRequest, idempotencyKey: string): Promise<JsonResponse<'createRun', 201>> {
    return request({ method: 'POST', path: '/runs', body, idempotencyKey });
  },

  get(runId: string, signal?: AbortSignal): Promise<Run> {
    return request({ method: 'GET', path: `/runs/${seg(runId)}`, signal });
  },

  tasks(runId: string, query: RunTasksQuery = {}, signal?: AbortSignal): Promise<TaskGraph> {
    return request({ method: 'GET', path: `/runs/${seg(runId)}/tasks`, query, signal });
  },

  /** История событий run (run_event_log): дополняет SSE-ленту прошлым, фильтр по задаче/типу. */
  activity(runId: string, query: RunActivityQuery = {}, signal?: AbortSignal): Promise<ActivityPage> {
    return request({ method: 'GET', path: `/runs/${seg(runId)}/activity`, query, signal });
  },

  /** pause | resume | cancel (202: команда принята; переход публикуется событием run.state_changed). */
  command(runId: string, command: RunCommand, idempotencyKey: string, ifMatchVersion?: number): Promise<JsonResponse<'pauseRun', 202>> {
    return request({ method: 'POST', path: `/runs/${seg(runId)}/${command}`, idempotencyKey, ifMatchVersion });
  },
};

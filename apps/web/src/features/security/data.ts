/**
 * Запросы Центра безопасности (F7): находки workspace (`GET /findings`, B1) постранично; отчёты
 * качества run (`getRunQuality`) — для типа gate каждой находки и evidence проверок безопасности;
 * статусы evidence этих gates (`getEvidence`) — для правила D-8. Подгрузка отчётов ограничена
 * первыми `RUN_REPORTS_LIMIT` run выборки (N+1 под контролем); остальное честно помечается
 * как «не классифицировано».
 */
import { useInfiniteQuery, useQueries } from '@tanstack/react-query';
import { useMemo } from 'react';
import { findingKeys, findingsApi, type ListWorkspaceFindingsQuery } from '../../api/findings';
import { qualityApi, qualityKeys, type EvidenceVerificationStatus, type GateExecution } from '../../api/quality';
import { SECURITY_GATE_TYPES } from './model';

export const FINDINGS_PAGE_SIZE = 100;
export const RUN_REPORTS_LIMIT = 10;
export const SECURITY_EVIDENCE_LIMIT = 20;

export function useWorkspaceFindings(workspaceId: string, query: ListWorkspaceFindingsQuery) {
  return useInfiniteQuery({
    queryKey: findingKeys.workspace(workspaceId, query),
    queryFn: ({ pageParam, signal }) =>
      findingsApi.listWorkspace(workspaceId, { ...query, limit: FINDINGS_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

export interface RunGateIndex {
  /** gate_execution_id → gate_type. */
  gateTypes: Map<string, string>;
  /** run, чьи отчёты качества загружены. */
  loadedRuns: Set<string>;
  /** Gates безопасности загруженных run (SAST/SCA/SECRETS/SBOM/IAC/SIGN). */
  securityGates: (GateExecution & { run_id: string })[];
  pending: boolean;
  errors: number;
}

export function useRunGateIndex(runIds: readonly string[]): RunGateIndex {
  const results = useQueries({
    queries: runIds.map((id) => ({
      queryKey: qualityKeys.run(id),
      queryFn: ({ signal }: { signal: AbortSignal }) => qualityApi.runQuality(id, signal),
    })),
  });
  const dataKey = results.map((r) => r.dataUpdatedAt).join(',');
  const pending = results.some((r) => r.isPending);
  const errors = results.filter((r) => r.isError).length;
  return useMemo(() => {
    const gateTypes = new Map<string, string>();
    const loadedRuns = new Set<string>();
    const securityGates: RunGateIndex['securityGates'] = [];
    for (const r of results) {
      if (!r.data) continue;
      loadedRuns.add(r.data.run_id);
      for (const g of r.data.gate_executions) {
        gateTypes.set(g.id, g.gate_type);
        if (SECURITY_GATE_TYPES.includes(g.gate_type)) securityGates.push({ ...g, run_id: r.data.run_id });
      }
    }
    return { gateTypes, loadedRuns, securityGates, pending, errors };
    // results — новый массив на каждый рендер; пересчёт — по времени обновления данных.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataKey, pending, errors]);
}

/** Статусы evidence (getEvidence) для первых `SECURITY_EVIDENCE_LIMIT` id. */
export function useEvidenceStatuses(evidenceIds: readonly string[]): Map<string, EvidenceVerificationStatus> {
  const ids = evidenceIds.slice(0, SECURITY_EVIDENCE_LIMIT);
  const results = useQueries({
    queries: ids.map((id) => ({
      queryKey: qualityKeys.evidence(id),
      queryFn: ({ signal }: { signal: AbortSignal }) => qualityApi.evidence(id, signal),
    })),
  });
  const key = results.map((r) => `${r.data?.id ?? ''}:${r.data?.verification_status ?? ''}`).join(',');
  return useMemo(() => {
    const out = new Map<string, EvidenceVerificationStatus>();
    for (const r of results) if (r.data) out.set(r.data.id, r.data.verification_status);
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
}

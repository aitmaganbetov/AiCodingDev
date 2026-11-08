import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useRef } from 'react';
import { runKeys, runsApi } from '../../api/runs';
import { RUN_TERMINAL } from '../../components/StatusBadge';

/** Run (getRun) + `isTerminal` для потока событий: после завершения run поток не переоткрывается. */
export function useRunDetail(runId: string, enabled = true) {
  const run = useQuery({
    queryKey: runKeys.detail(runId),
    queryFn: ({ signal }) => runsApi.get(runId, signal),
    enabled: Boolean(runId) && enabled,
  });
  const statusRef = useRef<string | undefined>(undefined);
  useEffect(() => {
    statusRef.current = run.data?.status;
  }, [run.data?.status]);
  const isTerminal = useCallback(() => RUN_TERMINAL.includes(statusRef.current ?? ''), []);
  return { run, isTerminal };
}

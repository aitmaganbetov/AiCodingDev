import { useQuery } from '@tanstack/react-query';
import { useCallback } from 'react';
import { qualityApi, type Evidence, type EvidenceVerificationStatus } from '../../api/quality';

/** Не более стольких страниц evidence run (по 200) для сопоставления статусов — затем «не загружено». */
export const RUN_EVIDENCE_MAX_PAGES = 5;
const PAGE = 200;

/**
 * Статусы проверки evidence run (listRunEvidence, OQ-45 (c)) для правила D-8: PASS gate засчитывается
 * только при evidence VERIFIED. Evidence вне загруженной выборки — «неизвестно» (undefined), и тогда
 * PASS показывается как «Недостаточно доказательств», а не «Пройдено».
 */
export function useRunEvidenceStatus(runId: string | null | undefined) {
  const query = useQuery({
    queryKey: ['evidence', 'run', runId ?? '', 'statuses'] as const,
    enabled: Boolean(runId),
    queryFn: async ({ signal }) => {
      const items: Evidence[] = [];
      let cursor: string | null = null;
      let pages = 0;
      do {
        const page = await qualityApi.runEvidence(runId ?? '', { limit: PAGE, ...(cursor ? { cursor } : {}) }, signal);
        items.push(...page.items);
        cursor = page.next_cursor;
        pages += 1;
      } while (cursor && pages < RUN_EVIDENCE_MAX_PAGES);
      return { statuses: new Map(items.map((e) => [e.id, e.verification_status] as const)), complete: !cursor };
    },
  });
  const statuses = query.data?.statuses;
  const statusOf = useCallback((evidenceId: string): EvidenceVerificationStatus | undefined => statuses?.get(evidenceId), [statuses]);
  return { query, statusOf, complete: query.data?.complete ?? false };
}

/**
 * Модель Code Review Workspace (F6, план §3.5; ADR-0015): строки diff для текстового вида, размещение тредов
 * по строкам, проверка якоря, классификация gates, доступность вердиктов. Чистые функции — unit-тесты рядом.
 */
import type { Approval } from '../../api/approvals';
import type { Evidence } from '../../api/quality';
import type { ReviewSide, ReviewThread, ReviewVerdict } from '../../api/reviews';
import { REVIEW_ANCHOR_MAX_LINES, REVIEW_BODY_MAX } from '../../api/reviews';
import type { GateExecution } from '../../api/tasks';
import { canSubmitReview } from '../../lib/permissions';

// ---------------------------------------------------------------------------
// Строки unified diff одного файла (текстовый вид)
// ---------------------------------------------------------------------------

export type DiffRowKind = 'hunk' | 'context' | 'add' | 'del' | 'note';

export interface DiffRow {
  kind: DiffRowKind;
  /** Номер строки базовой версии (del/context). */
  oldLine: number | null;
  /** Номер строки новой версии (add/context). */
  newLine: number | null;
  text: string;
}

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/;

/**
 * Строки секции diff файла (`DiffFile.raw`): заголовки hunk и строки с номерами обеих сторон. Строки hunk
 * учитываются по счётчикам заголовка `@@` (как в `parseUnifiedDiff`), поэтому код вида `--- x` не путается
 * с заголовком файла.
 */
export function diffRows(raw: string): DiffRow[] {
  const rows: DiffRow[] = [];
  let oldNo = 0;
  let newNo = 0;
  let oldLeft = 0;
  let newLeft = 0;
  for (const line of raw.replace(/\r\n/g, '\n').split('\n')) {
    if (oldLeft > 0 || newLeft > 0) {
      const sign = line[0];
      if (sign === '\\') {
        rows.push({ kind: 'note', oldLine: null, newLine: null, text: line });
        continue;
      }
      if (sign === '+') {
        rows.push({ kind: 'add', oldLine: null, newLine: newNo, text: line.slice(1) });
        newNo++;
        newLeft--;
        continue;
      }
      if (sign === '-') {
        rows.push({ kind: 'del', oldLine: oldNo, newLine: null, text: line.slice(1) });
        oldNo++;
        oldLeft--;
        continue;
      }
      if (sign === ' ' || line === '') {
        rows.push({ kind: 'context', oldLine: oldNo, newLine: newNo, text: line.slice(1) });
        oldNo++;
        newNo++;
        oldLeft--;
        newLeft--;
        continue;
      }
      oldLeft = 0;
      newLeft = 0;
    }
    const m = HUNK.exec(line);
    if (m) {
      oldNo = Number(m[1]);
      newNo = Number(m[3]);
      oldLeft = m[2] === undefined ? 1 : Number(m[2]);
      newLeft = m[4] === undefined ? 1 : Number(m[4]);
      rows.push({ kind: 'hunk', oldLine: null, newLine: null, text: line });
    } else if (line.startsWith('\\')) {
      rows.push({ kind: 'note', oldLine: null, newLine: null, text: line });
    }
  }
  return rows;
}

/** Строка, к которой можно привязать комментарий: добавленная/контекст — новая версия, удалённая — базовая. */
export function rowTarget(row: DiffRow): { side: ReviewSide; line: number } | null {
  if (row.kind === 'del' && row.oldLine !== null) return { side: 'base', line: row.oldLine };
  if ((row.kind === 'add' || row.kind === 'context') && row.newLine !== null) return { side: 'head', line: row.newLine };
  return null;
}

export const anchorKey = (side: ReviewSide, line: number) => `${side}:${line}`;

// ---------------------------------------------------------------------------
// Треды: размещение по файлу/строкам просматриваемого diff
// ---------------------------------------------------------------------------

export interface FileThreads {
  /** Треды этого commit, которые стоят под строкой diff (ключ — `side:end_line`). */
  byRow: Map<string, ReviewThread[]>;
  /** Треды этого commit на строках вне показанных hunk. */
  unplaced: ReviewThread[];
  /** Треды файла к другому commit (устарели относительно просматриваемого diff). */
  otherCommit: ReviewThread[];
  all: ReviewThread[];
}

export function placeThreads(threads: readonly ReviewThread[], path: string, headSha: string | null, rows: readonly DiffRow[]): FileThreads {
  const keys = new Set<string>();
  for (const r of rows) {
    const target = rowTarget(r);
    if (target) keys.add(anchorKey(target.side, target.line));
  }
  const byRow = new Map<string, ReviewThread[]>();
  const unplaced: ReviewThread[] = [];
  const otherCommit: ReviewThread[] = [];
  const all = threads.filter((th) => th.anchor.file_path === path);
  for (const th of all) {
    if (!headSha || th.anchor.commit_sha !== headSha) {
      otherCommit.push(th);
      continue;
    }
    const key = anchorKey(th.anchor.side, th.anchor.end_line);
    if (keys.has(key)) byRow.set(key, [...(byRow.get(key) ?? []), th]);
    else unplaced.push(th);
  }
  return { byRow, unplaced, otherCommit, all };
}

/** Порядок обхода тредов файла (n/p): по стороне и строке, затем по времени создания. */
export function threadOrder(threads: readonly ReviewThread[]): ReviewThread[] {
  return [...threads].sort(
    (a, b) =>
      a.anchor.file_path.localeCompare(b.anchor.file_path) ||
      a.anchor.end_line - b.anchor.end_line ||
      a.anchor.side.localeCompare(b.anchor.side) ||
      a.created_at.localeCompare(b.created_at),
  );
}

/** Открытые треды файла (счётчик в списке файлов). */
export function openThreadCount(threads: readonly ReviewThread[], path: string): number {
  return threads.filter((th) => th.anchor.file_path === path && th.status === 'OPEN').length;
}

/** Индекс соседнего элемента по кругу (клавиатурная навигация). */
export function cycleIndex(length: number, current: number, delta: 1 | -1): number {
  if (length <= 0) return -1;
  if (current < 0) return delta > 0 ? 0 : length - 1;
  return (current + delta + length) % length;
}

// ---------------------------------------------------------------------------
// Якорь нового треда
// ---------------------------------------------------------------------------

export interface AnchorDraft {
  side: ReviewSide;
  startLine: number;
  endLine: number;
}

export type AnchorError = 'lines' | 'range' | 'tooLong' | 'baseUnknown' | 'body' | 'bodyTooLong';

/** Путь относительно корня репозитория: без ведущего `/`, сегментов `.`/`..`, `//`, `\` и управляющих символов. */
export function isSafeRelativePath(path: string): boolean {
  if (!path || path.length > 1024 || path.startsWith('/') || path.endsWith('/')) return false;
  for (const ch of path) {
    const code = ch.charCodeAt(0);
    if (ch === '\\' || code < 0x20 || code === 0x7f) return false;
  }
  return path.split('/').every((part) => part !== '' && part !== '.' && part !== '..');
}

/** Проверка формы нового треда (сервер повторяет её, 422). */
export function validateThreadDraft(draft: AnchorDraft, body: string, baseSha: string | null): AnchorError[] {
  const errors: AnchorError[] = [];
  if (!Number.isInteger(draft.startLine) || !Number.isInteger(draft.endLine) || draft.startLine < 1 || draft.endLine < 1) errors.push('lines');
  else if (draft.endLine < draft.startLine) errors.push('range');
  else if (draft.endLine - draft.startLine + 1 > REVIEW_ANCHOR_MAX_LINES) errors.push('tooLong');
  if (draft.side === 'base' && !baseSha) errors.push('baseUnknown');
  const text = body.trim();
  if (!text) errors.push('body');
  else if (text.length > REVIEW_BODY_MAX) errors.push('bodyTooLong');
  return errors;
}

// ---------------------------------------------------------------------------
// Diff-артефакты и репозиторий
// ---------------------------------------------------------------------------

/** git_diff evidence run/задачи: новые первыми. */
export function diffEvidence(items: readonly Evidence[], taskId: string | null): Evidence[] {
  return items
    .filter((e) => e.artifact.kind === 'git_diff' || e.kind === 'git_diff')
    .filter((e) => !taskId || e.task_id === taskId)
    .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id));
}

// ---------------------------------------------------------------------------
// Проверки (тесты, безопасность)
// ---------------------------------------------------------------------------

export type GateCategory = 'tests' | 'security' | 'other';

const SECURITY = /SAST|SCA|DAST|SECRET|IAC|LICEN[CS]E|VULN|SECURITY|CONTAINER|DEPENDENC/i;
const TESTS = /TEST|COVERAGE|E2E/i;

export function gateCategory(gateType: string): GateCategory {
  if (SECURITY.test(gateType)) return 'security';
  if (TESTS.test(gateType)) return 'tests';
  return 'other';
}

export function gatesFor(gates: readonly GateExecution[], taskId: string | null, category: GateCategory): GateExecution[] {
  return gates.filter((g) => (!taskId || g.task_id === taskId) && gateCategory(g.gate_type) === category);
}

// ---------------------------------------------------------------------------
// Отправка ревью
// ---------------------------------------------------------------------------

export type VerdictBlock = 'role' | 'noApproval' | 'cannotDecide' | null;

export interface VerdictOption {
  verdict: ReviewVerdict;
  /** null — доступен; иначе причина недоступности (показывается текстом). */
  blocked: VerdictBlock;
}

/** Решаемые approvals run (PENDING). `can_decide` сервера учитывает роль по action_type и four-eyes. */
export function decidableApprovals(approvals: readonly Approval[]): Approval[] {
  return approvals.filter((a) => a.status === 'PENDING' && a.can_decide);
}

/**
 * Вердикты по роли (§5.6) и данным: APPROVE существует только при PENDING approval этого run (ADR-0015 §3);
 * REQUEST_CHANGES — developer+ (как поручение CHANGE_REQUEST).
 */
export function verdictOptions(role: string | null | undefined, pending: readonly Approval[]): VerdictOption[] {
  const approve: VerdictBlock = !canSubmitReview(role, 'APPROVE')
    ? 'role'
    : pending.filter((a) => a.status === 'PENDING').length === 0
      ? 'noApproval'
      : decidableApprovals(pending).length === 0
        ? 'cannotDecide'
        : null;
  return [
    { verdict: 'COMMENT', blocked: canSubmitReview(role, 'COMMENT') ? null : 'role' },
    { verdict: 'APPROVE', blocked: approve },
    { verdict: 'REQUEST_CHANGES', blocked: canSubmitReview(role, 'REQUEST_CHANGES') ? null : 'role' },
  ];
}

/** Треды для REQUEST_CHANGES по умолчанию — открытые треды run (лимит контракта 50). */
export function defaultSubmitThreads(threads: readonly ReviewThread[], limit = 50): string[] {
  return threads
    .filter((th) => th.status === 'OPEN')
    .slice(0, limit)
    .map((th) => th.id);
}

/** Короткая ссылка на якорь: `path:12–14`. */
export function anchorLabel(th: Pick<ReviewThread, 'anchor'>): string {
  const { file_path, start_line, end_line } = th.anchor;
  return start_line === end_line ? `${file_path}:${start_line}` : `${file_path}:${start_line}–${end_line}`;
}

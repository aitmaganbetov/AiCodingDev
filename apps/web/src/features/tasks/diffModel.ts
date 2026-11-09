/**
 * Разбор unified diff (git diff) для read-only diff viewer. API отдаёт diff только как артефакт
 * `git_diff` (evidence задачи), а не версии файлов целиком: обе стороны восстанавливаются из
 * hunks, неизвестные строки между hunks — пустые (номера строк совпадают с файлом, а Monaco
 * сворачивает неизменённые области).
 */
export interface DiffFile {
  oldPath: string | null;
  newPath: string | null;
  /** Отображаемый путь (новый, для удалённого — старый). */
  path: string;
  status: 'added' | 'deleted' | 'modified' | 'renamed';
  binary: boolean;
  additions: number;
  deletions: number;
  original: string;
  modified: string;
  /** Исходный текст секции diff этого файла (текстовый вид). */
  raw: string;
}

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

function stripPrefix(p: string): string | null {
  const v = p.replace(/\t.*$/, '').trim().replace(/^"(.*)"$/, '$1');
  if (v === '/dev/null') return null;
  return v.replace(/^[ab]\//, '');
}

interface Draft {
  oldPath: string | null;
  newPath: string | null;
  git: boolean;
  renamed: boolean;
  created: boolean;
  deleted: boolean;
  binary: boolean;
  hunks: number;
  additions: number;
  deletions: number;
  original: string[];
  modified: string[];
  raw: string[];
}

function draft(git: boolean): Draft {
  return {
    oldPath: null,
    newPath: null,
    git,
    renamed: false,
    created: false,
    deleted: false,
    binary: false,
    hunks: 0,
    additions: 0,
    deletions: 0,
    original: [],
    modified: [],
    raw: [],
  };
}

function finish(d: Draft): DiffFile | null {
  const oldPath = d.created ? null : d.oldPath;
  const newPath = d.deleted ? null : d.newPath;
  const path = newPath ?? oldPath ?? '';
  if (!path) return null;
  while (d.raw.length > 0 && d.raw.at(-1) === '') d.raw.pop();
  return {
    oldPath,
    newPath,
    path,
    status: oldPath === null ? 'added' : newPath === null ? 'deleted' : d.renamed || oldPath !== newPath ? 'renamed' : 'modified',
    binary: d.binary,
    additions: d.additions,
    deletions: d.deletions,
    original: d.original.join('\n'),
    modified: d.modified.join('\n'),
    raw: d.raw.join('\n'),
  };
}

/** Однопроходный разбор: строки hunk учитываются по счётчикам заголовка `@@`, поэтому строки кода вида `--- x` не путаются с заголовками. */
export function parseUnifiedDiff(text: string): DiffFile[] {
  const lines = text.replace(/\r\n/g, '\n').split('\n');
  const files: DiffFile[] = [];
  let cur: Draft | null = null;
  let oldLeft = 0;
  let newLeft = 0;
  const flush = () => {
    const f = cur ? finish(cur) : null;
    if (f) files.push(f);
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] ?? '';
    // Строка hunk начинается с ' ', '+', '-' или '\\'; иное (например, следующий `diff --git`
    // при неверных счётчиках) завершает hunk.
    if (cur && (oldLeft > 0 || newLeft > 0) && line !== '' && !' +-\\'.includes(line[0] ?? '')) {
      oldLeft = 0;
      newLeft = 0;
    }
    if (cur && (oldLeft > 0 || newLeft > 0)) {
      cur.raw.push(line);
      if (line.startsWith('\\')) continue; // \ No newline at end of file
      const sign = line[0];
      const body = line.slice(1);
      if (sign === '+') {
        cur.modified.push(body);
        cur.additions++;
        newLeft--;
      } else if (sign === '-') {
        cur.original.push(body);
        cur.deletions++;
        oldLeft--;
      } else {
        cur.original.push(body);
        cur.modified.push(body);
        oldLeft--;
        newLeft--;
      }
      continue;
    }
    if (line.startsWith('diff --git ')) {
      flush();
      cur = draft(true);
      const m = /^diff --git "?a\/(.+?)"? "?b\/(.+?)"?$/.exec(line);
      cur.oldPath = m?.[1] ?? null;
      cur.newPath = m?.[2] ?? null;
      cur.raw.push(line);
      continue;
    }
    if (line.startsWith('--- ') && (lines[i + 1] ?? '').startsWith('+++ ')) {
      // Без заголовка git каждая пара ---/+++ начинает новый файл.
      if (!cur || !cur.git || cur.hunks > 0) {
        flush();
        cur = draft(false);
      }
      cur.oldPath = stripPrefix(line.slice(4));
      cur.raw.push(line);
      continue;
    }
    if (!cur) continue;
    cur.raw.push(line);
    const hunk = HUNK.exec(line);
    if (hunk) {
      cur.hunks++;
      oldLeft = hunk[2] === undefined ? 1 : Number(hunk[2]);
      newLeft = hunk[4] === undefined ? 1 : Number(hunk[4]);
      const oldStart = Number(hunk[1]);
      const newStart = Number(hunk[3]);
      // Пустые строки до начала hunk: номера строк обеих сторон совпадают с файлами.
      while (cur.original.length < oldStart - 1) cur.original.push('');
      while (cur.modified.length < newStart - 1) cur.modified.push('');
    } else if (line.startsWith('+++ ')) cur.newPath = stripPrefix(line.slice(4));
    else if (line.startsWith('rename from ')) {
      cur.oldPath = line.slice(12);
      cur.renamed = true;
    } else if (line.startsWith('rename to ')) {
      cur.newPath = line.slice(10);
      cur.renamed = true;
    } else if (line.startsWith('new file mode')) cur.created = true;
    else if (line.startsWith('deleted file mode')) cur.deleted = true;
    else if (line.startsWith('Binary files ') || line === 'GIT binary patch') cur.binary = true;
  }
  flush();
  return files;
}

/** Язык Monaco по расширению (только для подсветки, без загрузки языковых сервисов). */
export function languageForPath(path: string): string {
  const ext = path.slice(path.lastIndexOf('.') + 1).toLowerCase();
  const map: Record<string, string> = {
    ts: 'typescript',
    tsx: 'typescript',
    js: 'javascript',
    jsx: 'javascript',
    mjs: 'javascript',
    py: 'python',
    json: 'json',
    md: 'markdown',
    yml: 'yaml',
    yaml: 'yaml',
    css: 'css',
    html: 'html',
    sql: 'sql',
    sh: 'shell',
    go: 'go',
    java: 'java',
    rs: 'rust',
  };
  return map[ext] ?? 'plaintext';
}

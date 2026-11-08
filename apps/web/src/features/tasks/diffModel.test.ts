import { describe, expect, it } from 'vitest';
import { languageForPath, parseUnifiedDiff } from './diffModel';

const GIT = `diff --git a/src/export.ts b/src/export.ts
index 1111111..2222222 100644
--- a/src/export.ts
+++ b/src/export.ts
@@ -2,3 +2,4 @@ import x
 line2
-old3
+new3
+added4
 line4
@@ -10,1 +11,1 @@
--- not a header
+++ also not a header
diff --git a/README.md b/README.md
new file mode 100644
--- /dev/null
+++ b/README.md
@@ -0,0 +1,2 @@
+# Title
+text
\\ No newline at end of file
diff --git a/old.txt b/new.txt
similarity index 100%
rename from old.txt
rename to new.txt
diff --git a/img.png b/img.png
Binary files a/img.png and b/img.png differ
`;

describe('parseUnifiedDiff', () => {
  const files = parseUnifiedDiff(GIT);

  it('splits files and detects status', () => {
    expect(files.map((f) => [f.path, f.status, f.binary])).toEqual([
      ['src/export.ts', 'modified', false],
      ['README.md', 'added', false],
      ['new.txt', 'renamed', false],
      ['img.png', 'modified', true],
    ]);
  });

  it('rebuilds both sides from hunks with real line numbers; hunk lines that look like headers stay content', () => {
    const f = files[0]!;
    expect(f.additions).toBe(3);
    expect(f.deletions).toBe(2);
    const original = f.original.split('\n');
    const modified = f.modified.split('\n');
    expect(original[0]).toBe('');
    expect(original.slice(1, 4)).toEqual(['line2', 'old3', 'line4']);
    expect(modified.slice(1, 5)).toEqual(['line2', 'new3', 'added4', 'line4']);
    expect(original[9]).toBe('-- not a header');
    expect(modified[10]).toBe('++ also not a header');
    expect(f.raw.startsWith('diff --git a/src/export.ts')).toBe(true);
  });

  it('new file: empty original; "no newline" marker is ignored', () => {
    const f = files[1]!;
    expect(f.oldPath).toBeNull();
    expect(f.original).toBe('');
    expect(f.modified).toBe('# Title\ntext');
  });

  it('plain unified diff without git headers', () => {
    const plain = parseUnifiedDiff('--- a/x.py\t2026-01-01\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n--- y.py\n+++ y.py\n@@ -1 +1 @@\n-c\n+d\n');
    expect(plain.map((f) => [f.path, f.original, f.modified])).toEqual([
      ['x.py', 'a', 'b'],
      ['y.py', 'c', 'd'],
    ]);
  });

  it('returns nothing for non-diff text and maps languages', () => {
    expect(parseUnifiedDiff('hello world')).toEqual([]);
    expect(languageForPath('a/b.tsx')).toBe('typescript');
    expect(languageForPath('Makefile')).toBe('plaintext');
  });
});

describe('parseUnifiedDiff robustness', () => {
  it('ends a hunk early when counts overstate it', () => {
    const files = parseUnifiedDiff('diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1,5 +1,5 @@\n-x\n+y\ndiff --git a/b b/b\n--- a/b\n+++ b/b\n@@ -1 +1 @@\n-p\n+q\n');
    expect(files.map((f) => [f.path, f.status, f.modified])).toEqual([
      ['a', 'modified', 'y'],
      ['b', 'modified', 'q'],
    ]);
  });
});

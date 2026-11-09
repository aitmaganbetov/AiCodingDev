/**
 * Read-only diff editor на Monaco (ADR-0014). Модуль грузится лениво (React.lazy): Monaco и его
 * worker не входят в начальный бандл. Monaco берётся из локального пакета monaco-editor
 * (loader.config({ monaco })) — без загрузки с CDN; worker — отдельный файл того же origin (CSP 'self').
 */
import { DiffEditor, loader, type DiffOnMount } from '@monaco-editor/react';
import * as monaco from 'monaco-editor/editor/editor.api';
import EditorWorker from 'monaco-editor/editor/editor.worker?worker';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { LoadingState } from '../../components/ui';
import './monacoMarkers.css';

declare global {
  interface Window {
    MonacoEnvironment?: { getWorker: (workerId: string, label: string) => Worker };
  }
}

window.MonacoEnvironment = { getWorker: () => new EditorWorker() };
loader.config({ monaco });

function prefersDark(): boolean {
  const forced = document.documentElement.dataset.theme;
  if (forced === 'dark') return true;
  if (forced === 'light') return false;
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false;
}

/** Тема Monaco следует теме приложения (data-theme / prefers-color-scheme). */
function useMonacoTheme(): 'vs' | 'vs-dark' {
  const [dark, setDark] = useState(prefersDark);
  useEffect(() => {
    const update = () => setDark(prefersDark());
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    const mq = window.matchMedia?.('(prefers-color-scheme: dark)');
    mq?.addEventListener('change', update);
    return () => {
      observer.disconnect();
      mq?.removeEventListener('change', update);
    };
  }, []);
  return dark ? 'vs-dark' : 'vs';
}

export interface MonacoDiffProps {
  original: string;
  modified: string;
  language: string;
  /** Уникальный путь моделей (смена файла не смешивает модели). */
  modelPath: string;
  ariaLabel: string;
  sideBySide: boolean;
  /** Code Review (F6): маркеры строк — треды (полоса и фон) и находки (точка) в поле глифов. */
  markers?: readonly MonacoLineMarker[];
  /** Прокрутить к строке стороны и выделить её (смена `nonce` повторяет переход). */
  reveal?: { side: 'base' | 'head'; line: number; nonce: number } | null;
  /** Выделение строк в любой из сторон (клавиатура/мышь) — якорь нового комментария. */
  onSelectionChange?: (selection: MonacoLineSelection) => void;
}

export interface MonacoLineMarker {
  side: 'base' | 'head';
  line: number;
  kind: 'thread' | 'finding';
}

export interface MonacoLineSelection {
  side: 'base' | 'head';
  startLine: number;
  endLine: number;
}

type DiffEditorInstance = Parameters<DiffOnMount>[0];

export default function MonacoDiff({ original, modified, language, modelPath, ariaLabel, sideBySide, markers, reveal, onSelectionChange }: MonacoDiffProps) {
  const { t } = useTranslation();
  const theme = useMonacoTheme();
  const [editor, setEditor] = useState<DiffEditorInstance | null>(null);
  const selectionRef = useRef(onSelectionChange);
  useEffect(() => {
    selectionRef.current = onSelectionChange;
  });

  useEffect(() => {
    if (!editor) return undefined;
    const subs = (['base', 'head'] as const).map((side) => {
      const ed = side === 'base' ? editor.getOriginalEditor() : editor.getModifiedEditor();
      return ed.onDidChangeCursorSelection((e) => {
        const a = e.selection.startLineNumber;
        const b = e.selection.endLineNumber;
        selectionRef.current?.({ side, startLine: Math.min(a, b), endLine: Math.max(a, b) });
      });
    });
    return () => subs.forEach((d) => d.dispose());
  }, [editor]);

  useEffect(() => {
    if (!editor || !markers) return undefined;
    const collections = (['base', 'head'] as const).map((side) => {
      const ed = side === 'base' ? editor.getOriginalEditor() : editor.getModifiedEditor();
      return ed.createDecorationsCollection(
        markers
          .filter((m) => m.side === side)
          .map((m) => ({
            range: new monaco.Range(m.line, 1, m.line, 1),
            options:
              m.kind === 'thread'
                ? { isWholeLine: true, className: 'aicd-line-thread', glyphMarginClassName: 'aicd-glyph-thread' }
                : { glyphMarginClassName: 'aicd-glyph-finding' },
          })),
      );
    });
    return () => collections.forEach((c) => c.clear());
  }, [editor, markers, original, modified]);

  const revealSide = reveal?.side;
  const revealLine = reveal?.line;
  const revealNonce = reveal?.nonce;
  useEffect(() => {
    if (!editor || !revealSide || !revealLine) return;
    const ed = revealSide === 'base' ? editor.getOriginalEditor() : editor.getModifiedEditor();
    ed.revealLineInCenter(revealLine);
    ed.setSelection(new monaco.Range(revealLine, 1, revealLine, 1));
  }, [editor, revealSide, revealLine, revealNonce]);

  return (
    <div className="h-[60vh] min-h-[20rem] overflow-hidden rounded-md border border-border" data-testid="monaco-diff">
      <DiffEditor
        original={original}
        modified={modified}
        language={language}
        originalModelPath={`original/${modelPath}`}
        modifiedModelPath={`modified/${modelPath}`}
        theme={theme}
        loading={<LoadingState label={t('tasks.diff.editorLoading')} />}
        onMount={(ed) => setEditor(ed)}
        options={{
          readOnly: true,
          originalEditable: false,
          domReadOnly: true,
          renderSideBySide: sideBySide,
          useInlineViewWhenSpaceIsLimited: true,
          hideUnchangedRegions: { enabled: true, contextLineCount: 3, minimumLineCount: 3, revealLineCount: 20 },
          ariaLabel,
          accessibilitySupport: 'auto',
          minimap: { enabled: false },
          scrollBeyondLastLine: false,
          automaticLayout: true,
          renderOverviewRuler: false,
          wordWrap: 'off',
          links: false,
          contextmenu: false,
          glyphMargin: Boolean(markers),
          fontSize: 13,
        }}
      />
    </div>
  );
}

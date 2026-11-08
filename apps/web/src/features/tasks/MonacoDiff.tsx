/**
 * Read-only diff editor на Monaco (ADR-0014). Модуль грузится лениво (React.lazy): Monaco и его
 * worker не входят в начальный бандл. Monaco берётся из локального пакета monaco-editor
 * (loader.config({ monaco })) — без загрузки с CDN; worker — отдельный файл того же origin (CSP 'self').
 */
import { DiffEditor, loader } from '@monaco-editor/react';
import * as monaco from 'monaco-editor/editor/editor.api';
import EditorWorker from 'monaco-editor/editor/editor.worker?worker';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { LoadingState } from '../../components/ui';

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
}

export default function MonacoDiff({ original, modified, language, modelPath, ariaLabel, sideBySide }: MonacoDiffProps) {
  const { t } = useTranslation();
  const theme = useMonacoTheme();
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
          fontSize: 13,
        }}
      />
    </div>
  );
}

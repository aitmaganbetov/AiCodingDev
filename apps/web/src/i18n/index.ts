import i18n, { type ResourceLanguage } from 'i18next';
import { initReactI18next } from 'react-i18next';

export const DEFAULT_LOCALE = 'ru';
export const DEFAULT_NS = 'common';

/**
 * Ресурсы разбиты по областям: `src/i18n/<area>.json` = namespace `<area>` (ru).
 * Новый файл подхватывается автоматически (import.meta.glob) — index.ts править не нужно.
 *
 * Соглашение (см. README «i18n»): ключи каждого файла лежат под группой с именем области
 * (`approvals.json` → `{ "approvals": { … } }`), поэтому пути ключей глобально уникальны и
 * `t('approvals.title')` работает из любого компонента (fallbackNS), а
 * `useTranslation('approvals')` / `t('approvals:approvals.title')` — по желанию.
 * Исторические группы (runs.json: run, event; common.json: enums, problem…) сохранены.
 *
 * Бюджет начального JS (F4, план §4.3): в начальный бандл входят только строки оболочки —
 * `EAGER_NAMESPACES` (common, nav, ds — кит `components/ui`). Остальные файлы — отдельные чанки:
 * экран загружает свои namespace до первого рендера (`lazyPage(…, ns)` в `src/routes/lazyPage.tsx`),
 * `SHELL_DEFERRED_NAMESPACES` (подписи перечислений в уведомлениях и чипах CLI, всегда с defaultValue)
 * догружаются в фоне после старта. Полнота объявлений проверяется тестом `routes/namespaces.test.ts`.
 */
export const EAGER_NAMESPACES = ['common', 'nav', 'ds'] as const;
export const SHELL_DEFERRED_NAMESPACES = ['providers'] as const;

const eager = import.meta.glob<{ default: ResourceLanguage }>(['./common.json', './nav.json', './ds.json'], { eager: true });
const deferred = import.meta.glob<{ default: ResourceLanguage }>(['./*.json', '!./common.json', '!./nav.json', '!./ds.json']);

const nsOf = (path: string) => /\.\/([\w-]+)\.json$/.exec(path)?.[1];

export const NAMESPACES: string[] = [...Object.keys(eager), ...Object.keys(deferred)]
  .map(nsOf)
  .filter((ns): ns is string => Boolean(ns))
  .sort((a, b) => (a === DEFAULT_NS ? -1 : b === DEFAULT_NS ? 1 : a.localeCompare(b)));

const resources: ResourceLanguage = {};
for (const [path, mod] of Object.entries(eager)) {
  const ns = nsOf(path);
  if (ns) resources[ns] = mod.default;
}

void i18n.use(initReactI18next).init({
  resources: { [DEFAULT_LOCALE]: resources },
  ns: Object.keys(resources),
  defaultNS: DEFAULT_NS,
  // Ключи без префикса ищутся во всех namespace (в том числе догруженных позже).
  fallbackNS: NAMESPACES.filter((ns) => ns !== DEFAULT_NS),
  partialBundledLanguages: true,
  lng: DEFAULT_LOCALE,
  fallbackLng: DEFAULT_LOCALE,
  supportedLngs: [DEFAULT_LOCALE],
  interpolation: { escapeValue: false },
  returnNull: false,
  initAsync: false,
  // Компоненты, уже показанные до догрузки namespace (оболочка), перерисовываются после неё.
  react: { bindI18nStore: 'added' },
});

const loading = new Map<string, Promise<void>>();

/** Загружен ли namespace (строки доступны синхронно). */
export function isNamespaceLoaded(ns: string): boolean {
  return i18n.hasResourceBundle(DEFAULT_LOCALE, ns);
}

/**
 * Догрузить namespace областей (отдельные чанки). Повторный вызов не повторяет загрузку;
 * неизвестное имя — ошибка разработчика (исключение), чтобы опечатка не скрыла строки.
 */
export function loadNamespaces(names: readonly string[]): Promise<void> {
  const pending = names
    .filter((ns) => !isNamespaceLoaded(ns))
    .map((ns) => {
      let p = loading.get(ns);
      if (!p) {
        const load = deferred[`./${ns}.json`];
        if (!load) return Promise.reject(new Error(`i18n: unknown namespace "${ns}"`));
        p = load().then(
          (mod) => {
            i18n.addResourceBundle(DEFAULT_LOCALE, ns, mod.default, true, false);
          },
          (error: unknown) => {
            loading.delete(ns);
            throw error;
          },
        );
        loading.set(ns, p);
      }
      return p;
    });
  return pending.length > 0 ? Promise.all(pending).then(() => undefined) : Promise.resolve();
}

/** Все namespace (unit-тесты, справочник дизайн-системы). */
export function loadAllNamespaces(): Promise<void> {
  return loadNamespaces(NAMESPACES.filter((ns) => !isNamespaceLoaded(ns)));
}

/** Фоновая догрузка строк оболочки после первого рендера (уведомления, чипы CLI). */
export function preloadShellNamespaces(): void {
  void loadNamespaces(SHELL_DEFERRED_NAMESPACES).catch(() => undefined);
}

if (typeof document !== 'undefined') {
  document.documentElement.lang = i18n.language;
  i18n.on('languageChanged', (lng) => {
    document.documentElement.lang = lng;
  });
}

export default i18n;

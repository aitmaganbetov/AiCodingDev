import i18n, { type Resource, type ResourceLanguage } from 'i18next';
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
 */
const modules = import.meta.glob<{ default: ResourceLanguage }>('./*.json', { eager: true });

export const NAMESPACES: string[] = [];
const ru: Resource[string] = {};
for (const [path, mod] of Object.entries(modules)) {
  const ns = /\.\/([\w-]+)\.json$/.exec(path)?.[1];
  if (!ns) continue;
  NAMESPACES.push(ns);
  ru[ns] = mod.default;
}
NAMESPACES.sort((a, b) => (a === DEFAULT_NS ? -1 : b === DEFAULT_NS ? 1 : a.localeCompare(b)));

void i18n.use(initReactI18next).init({
  resources: { [DEFAULT_LOCALE]: ru },
  ns: NAMESPACES,
  defaultNS: DEFAULT_NS,
  fallbackNS: NAMESPACES.filter((ns) => ns !== DEFAULT_NS),
  lng: DEFAULT_LOCALE,
  fallbackLng: DEFAULT_LOCALE,
  supportedLngs: [DEFAULT_LOCALE],
  interpolation: { escapeValue: false },
  returnNull: false,
  initAsync: false,
});

if (typeof document !== 'undefined') {
  document.documentElement.lang = i18n.language;
  i18n.on('languageChanged', (lng) => {
    document.documentElement.lang = lng;
  });
}

export default i18n;

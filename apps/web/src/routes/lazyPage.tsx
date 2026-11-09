import { lazy, type ComponentType, type LazyExoticComponent } from 'react';
import { loadNamespaces } from '../i18n';

/** Загрузчики всех ленивых экранов (для предзагрузки в тестовой обвязке, `preloadLazyPages`). */
const loaders: Array<() => Promise<unknown>> = [];

/**
 * Ленивый экран области (бюджет начального JS, план §4.3): модуль страницы и строки её namespace
 * (`src/i18n/<area>.json`) загружаются параллельно, экран рендерится, когда готово и то и другое
 * (Suspense — в `AppRoutes`), поэтому ключи никогда не показываются «сырыми».
 *
 * `namespaces` — все области, чьи ключи использует модуль вместе с импортами (common/nav/ds уже
 * загружены). Полноту проверяет `routes/namespaces.test.ts` (статический обход импортов).
 * Формат вызова — `lazyPage(() => import('…'), 'ExportName', ['ns', …])` в одну конструкцию
 * (тест разбирает исходник route-файла).
 */
export function lazyPage<M extends Record<string, unknown>, K extends keyof M & string>(
  load: () => Promise<M>,
  exportName: K,
  namespaces: readonly string[],
): LazyExoticComponent<M[K] extends ComponentType<infer P> ? ComponentType<P> : never> {
  type C = M[K] extends ComponentType<infer P> ? ComponentType<P> : never;
  const loadAll = () => Promise.all([load(), loadNamespaces(namespaces)]);
  loaders.push(loadAll);
  return lazy(async () => {
    const [mod] = await loadAll();
    return { default: mod[exportName] as C };
  });
}

/**
 * Загружает модули и namespace всех ленивых экранов, объявленных к этому моменту (реестр
 * заполняется при импорте route-модулей). Используется тестовой обвязкой (`src/test/utils.tsx`):
 * первый импорт модуля под нагрузкой (трансформация Vite) может занимать секунды, и без
 * предзагрузки это время уходило бы в таймауты `findBy*` конкретного теста. В приложении не
 * вызывается — экраны остаются ленивыми (бюджет начального JS).
 */
export async function preloadLazyPages(): Promise<void> {
  await Promise.all(loaders.map((load) => load()));
}

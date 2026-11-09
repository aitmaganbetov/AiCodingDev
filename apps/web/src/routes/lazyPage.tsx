import { lazy, type ComponentType, type LazyExoticComponent } from 'react';
import { loadNamespaces } from '../i18n';

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
  return lazy(async () => {
    const [mod] = await Promise.all([load(), loadNamespaces(namespaces)]);
    return { default: mod[exportName] as C };
  });
}

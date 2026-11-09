import AxeBuilder from '@axe-core/playwright';
import { expect, type Page } from '@playwright/test';

/** Теги axe-core: WCAG 2.0/2.1/2.2 уровней A и AA (D-1 — цель WCAG 2.2 AA). */
export const AXE_WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22a', 'wcag22aa'] as const;

/**
 * Ждёт завершения конечных анимаций (появление слоёв — opacity): контраст проверяется по итоговому
 * кадру. Бесконечные индикаторы (спиннер, скелет, пульсация узла) и приостановленные анимации не
 * ждём — их `finished` не наступает; верхняя граница ожидания — длительность самой длинной анимации
 * (токены motion ≤ 250 мс) с запасом, чтобы под нагрузкой (троттлинг кадров) проверка не зависала.
 */
export async function settleAnimations(page: Page): Promise<void> {
  await page.evaluate(
    () =>
      new Promise<void>((resolve) => {
        const pending = document
          .getAnimations()
          .filter((a) => a.playState === 'running' && a.effect?.getComputedTiming().iterations !== Infinity)
          .map((a) => a.finished.then(() => undefined, () => undefined));
        void Promise.all(pending).then(() => resolve());
        setTimeout(resolve, 2_000);
      }),
  );
}

/** WCAG 2.2 A/AA (включая 2.0/2.1) через axe-core: нарушений уровня serious/critical быть не должно. */
export async function expectNoSeriousA11yViolations(page: Page, screen: string): Promise<void> {
  await settleAnimations(page);
  const results = await new AxeBuilder({ page }).withTags([...AXE_WCAG_TAGS]).analyze();
  const blocking = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
  expect(
    blocking.map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`),
    `axe violations on "${screen}"`,
  ).toEqual([]);
}

/** Нет горизонтальной прокрутки страницы (responsive до 360px). */
export async function expectNoHorizontalScroll(page: Page): Promise<void> {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
}

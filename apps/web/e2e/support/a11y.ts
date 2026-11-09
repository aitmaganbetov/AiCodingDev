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

type Violation = Awaited<ReturnType<AxeBuilder['analyze']>>['violations'][number];

/**
 * axe `target-size` (2.5.8) считает цель «частично закрытой», если при текущей прокрутке её край
 * находится под sticky верхней панелью. Это положение прокрутки, а не размер цели: такие узлы
 * проверяются повторно после прокрутки к цели (с учётом scroll-padding, 2.4.11) — остаются только
 * цели, недостаточные и в полностью видимом положении.
 */
async function recheckObscuredTargets(page: Page, violations: Violation[]): Promise<Violation[]> {
  const out: Violation[] = [];
  for (const v of violations) {
    if (v.id !== 'target-size') {
      out.push(v);
      continue;
    }
    const nodes: Violation['nodes'] = [];
    for (const node of v.nodes) {
      const selector = node.target.join(' ');
      if (!/obscured/.test(node.failureSummary ?? '') || typeof node.target[0] !== 'string' || node.target.length !== 1) {
        nodes.push(node);
        continue;
      }
      const y = await page.evaluate(() => window.scrollY);
      await page.locator(selector).first().evaluate((el) => el.scrollIntoView({ block: 'center' }));
      const again = await new AxeBuilder({ page }).withRules(['target-size']).include(selector).analyze();
      await page.evaluate((top) => window.scrollTo(0, top), y);
      // Узел засчитывается только если правило реально выполнено для него (селектор нашёл цель).
      if (again.violations.length > 0 || !again.passes.some((p) => p.id === 'target-size')) nodes.push(node);
    }
    if (nodes.length > 0) out.push({ ...v, nodes });
  }
  return out;
}

/** WCAG 2.2 A/AA (включая 2.0/2.1) через axe-core: нарушений уровня serious/critical быть не должно. */
export async function expectNoSeriousA11yViolations(page: Page, screen: string): Promise<void> {
  await settleAnimations(page);
  const results = await new AxeBuilder({ page }).withTags([...AXE_WCAG_TAGS]).analyze();
  const blocking = (await recheckObscuredTargets(page, results.violations)).filter((v) => v.impact === 'serious' || v.impact === 'critical');
  expect(
    blocking.map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => `${n.target.join(' ')} — ${(n.failureSummary ?? '').replace(/\s+/g, ' ').slice(0, 300)}`).join('; ')}`),
    `axe violations on "${screen}"`,
  ).toEqual([]);
}

/** Нет горизонтальной прокрутки страницы (responsive до 360px). */
export async function expectNoHorizontalScroll(page: Page): Promise<void> {
  // После setViewportSize раскладка меняется асинхронно (media query → перерисовка React): опрос до
  // устойчивого результата; постоянное переполнение по-прежнему падает по таймауту expect.
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth))
    .toBeLessThanOrEqual(0);
}

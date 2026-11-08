import { expect, type Locator, type Page } from '@playwright/test';
import { PASSWORD, type MockApi } from './mockApi';

export async function login(page: Page, api: MockApi): Promise<void> {
  await page.goto('/login');
  await page.getByLabel('Электронная почта').fill(api.user.email);
  await page.getByLabel('Пароль').fill(PASSWORD);
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page).not.toHaveURL(/\/login/);
}

/** Начать последовательную навигацию с начала документа (как после загрузки страницы). */
export async function focusDocumentStart(page: Page): Promise<void> {
  await page.evaluate(() => {
    (document.activeElement as HTMLElement | null)?.blur();
    document.body.tabIndex = -1;
    document.body.focus();
    document.body.removeAttribute('tabindex');
  });
}

/** Только клавиатура: Tab (или Shift+Tab), пока фокус не окажется на целевом элементе. */
export async function tabTo(page: Page, target: Locator, { backwards = false, max = 30 } = {}): Promise<void> {
  for (let i = 0; i < max; i++) {
    if (await target.evaluate((el) => el === document.activeElement)) return;
    await page.keyboard.press(backwards ? 'Shift+Tab' : 'Tab');
  }
  await expect(target).toBeFocused();
}

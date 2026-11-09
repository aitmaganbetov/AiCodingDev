/** Форматирование чисел Dashboard (чистые функции). */

/** Доля 0..1 → «87,5 %»; null/NaN → null (нет данных, а не 0 %). */
export function formatPercent(value: number | null | undefined): string | null {
  if (value === null || value === undefined || Number.isNaN(value)) return null;
  return `${(value * 100).toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%`;
}

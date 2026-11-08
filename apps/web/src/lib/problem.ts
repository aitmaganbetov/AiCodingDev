import type { TFunction } from 'i18next';
import { isApiError } from '../api/client';

/** Человекочитаемое сообщение об ошибке API (по коду problem, с fallback на detail). */
export function problemMessage(t: TFunction, error: unknown): string {
  if (!isApiError(error)) return t('problem.unknown_error');
  return t(`problem.${error.code}`, {
    defaultValue: error.detail ?? error.title ?? t('problem.unknown_error'),
  });
}

export interface MappedErrors<F extends string> {
  fields: Partial<Record<F, string>>;
  form: string[];
}

/**
 * Раскладывает problem.errors[] по полям формы по JSON Pointer (`/name` → name).
 * Ошибки с неизвестным pointer и сама ошибка без errors[] уходят в form-level.
 */
export function mapProblemToFields<F extends string>(t: TFunction, error: unknown, fields: readonly F[]): MappedErrors<F> {
  const result: MappedErrors<F> = { fields: {}, form: [] };
  if (!isApiError(error) || error.errors.length === 0) {
    result.form.push(problemMessage(t, error));
    return result;
  }
  for (const fe of error.errors) {
    const segment = fe.pointer.replace(/^#/, '').split('/').filter(Boolean)[0];
    const field = fields.find((f) => f === segment);
    const message = t(`fieldError.${fe.code}`, { defaultValue: fe.message ?? t('fieldError.invalid') });
    if (field && result.fields[field] === undefined) result.fields[field] = message;
    else if (!field) result.form.push(message);
  }
  return result;
}

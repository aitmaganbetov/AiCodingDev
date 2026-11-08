import type { InputHTMLAttributes, ReactNode, TextareaHTMLAttributes } from 'react';

interface FieldShellProps {
  id: string;
  label: string;
  hint?: string | undefined;
  error?: string | undefined;
  children: (aria: { 'aria-invalid': boolean; 'aria-describedby'?: string }) => ReactNode;
}

function FieldShell({ id, label, hint, error, children }: FieldShellProps) {
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(' ') || undefined;
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium text-slate-900">
        {label}
      </label>
      {children({ 'aria-invalid': Boolean(error), ...(describedBy ? { 'aria-describedby': describedBy } : {}) })}
      {hint ? (
        <p id={hintId} className="text-sm text-slate-600">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} className="text-sm font-medium text-red-700">
          {error}
        </p>
      ) : null}
    </div>
  );
}

type TextFieldProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'id'> & {
  id: string;
  label: string;
  hint?: string | undefined;
  error?: string | undefined;
};

export function TextField({ id, label, hint, error, ...input }: TextFieldProps) {
  return (
    <FieldShell id={id} label={label} hint={hint} error={error}>
      {(aria) => <input id={id} className="input" {...aria} {...input} />}
    </FieldShell>
  );
}

type TextAreaFieldProps = Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'id'> & {
  id: string;
  label: string;
  hint?: string | undefined;
  error?: string | undefined;
};

export function TextAreaField({ id, label, hint, error, ...textarea }: TextAreaFieldProps) {
  return (
    <FieldShell id={id} label={label} hint={hint} error={error}>
      {(aria) => <textarea id={id} className="input min-h-[96px]" {...aria} {...textarea} />}
    </FieldShell>
  );
}

interface RadioGroupProps<V extends string> {
  name: string;
  legend: string;
  value: V;
  options: readonly { value: V; label: string; description?: string }[];
  onChange: (value: V) => void;
  error?: string | undefined;
}

export function RadioGroup<V extends string>({ name, legend, value, options, onChange, error }: RadioGroupProps<V>) {
  const errorId = error ? `${name}-error` : undefined;
  return (
    <fieldset className="flex flex-col gap-2" aria-describedby={errorId} aria-invalid={error ? true : undefined}>
      <legend className="mb-1 text-sm font-medium text-slate-900">{legend}</legend>
      {options.map((opt) => {
        const id = `${name}-${opt.value}`;
        return (
          <div key={opt.value} className="flex items-start gap-2">
            <input
              type="radio"
              id={id}
              name={name}
              value={opt.value}
              checked={value === opt.value}
              onChange={() => onChange(opt.value)}
              className="mt-1 h-4 w-4 accent-blue-700"
              aria-describedby={opt.description ? `${id}-desc` : undefined}
            />
            <div>
              <label htmlFor={id} className="text-sm text-slate-900">
                {opt.label}
              </label>
              {opt.description ? (
                <p id={`${id}-desc`} className="text-sm text-slate-600">
                  {opt.description}
                </p>
              ) : null}
            </div>
          </div>
        );
      })}
      {error ? (
        <p id={errorId} className="text-sm font-medium text-red-700">
          {error}
        </p>
      ) : null}
    </fieldset>
  );
}

export function FormAlert({ messages }: { messages: string[] }) {
  if (messages.length === 0) return null;
  return (
    <div role="alert" className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900" data-testid="form-alert">
      {messages.length === 1 ? (
        <p>{messages[0]}</p>
      ) : (
        <ul className="list-disc pl-5">
          {messages.map((m) => (
            <li key={m}>{m}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Фокус на первое поле с ошибкой после неуспешной отправки. */
export function focusFirstInvalid(form: HTMLFormElement | null): void {
  const el = form?.querySelector<HTMLElement>('[aria-invalid="true"]');
  if (el instanceof HTMLFieldSetElement) {
    el.querySelector<HTMLInputElement>('input:checked, input')?.focus();
  } else {
    el?.focus();
  }
}

import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import { Dialog, DialogContent } from '../components/ui/Dialog';
import { Kbd } from '../components/ui/Kbd';
import { setShortcutsEnabled, useShortcutsEnabled } from '../lib/shortcuts';
import type { NavEntry } from '../routes';

/** Справка по сочетаниям клавиш (`?` или кнопка в шапке) + отключение односимвольных сочетаний (WCAG 2.1.4). */
export function ShortcutsDialog({ open, onOpenChange, entries }: { open: boolean; onOpenChange: (open: boolean) => void; entries: NavEntry[] }) {
  const { t } = useTranslation();
  const enabled = useShortcutsEnabled();
  const toggleId = useId();
  const rows: { keys: string[]; action: string }[] = [
    { keys: ['?'], action: t('shortcuts.help') },
    ...entries.filter((e) => e.shortcut).map((e) => ({ keys: ['g', e.shortcut as string], action: t('shortcuts.goTo', { section: t(e.labelKey) }) })),
    { keys: ['Tab'], action: t('shortcuts.skip') },
    { keys: ['Esc'], action: t('shortcuts.closeDialog') },
  ];
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title={t('shortcuts.title')} description={t('shortcuts.description')} size="md">
        <div className="mb-3 flex items-center gap-2">
          <input
            id={toggleId}
            type="checkbox"
            className="h-5 w-5 accent-[rgb(var(--color-accent))]"
            checked={enabled}
            onChange={(e) => setShortcutsEnabled(e.target.checked)}
          />
          <label htmlFor={toggleId} className="text-sm">
            {t('shortcuts.enable')}
          </label>
        </div>
        <table className="w-full text-left text-sm">
          <caption className="sr-only">{t('shortcuts.caption')}</caption>
          <thead className="text-fg-muted">
            <tr>
              <th scope="col" className="py-1 pr-3 font-semibold">
                {t('shortcuts.keys')}
              </th>
              <th scope="col" className="py-1 font-semibold">
                {t('shortcuts.action')}
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.action} className="border-t border-border">
                <td className="whitespace-nowrap py-2 pr-3">
                  {row.keys.map((k, i) => (
                    <span key={k}>
                      {i > 0 ? <span className="px-1 text-fg-muted">{t('shortcuts.then')}</span> : null}
                      <Kbd>{k}</Kbd>
                    </span>
                  ))}
                </td>
                <td className="py-2">{row.action}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </DialogContent>
    </Dialog>
  );
}

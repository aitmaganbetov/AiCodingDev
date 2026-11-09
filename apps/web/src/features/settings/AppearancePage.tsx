import { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { PageHeading } from '../../components/PageHeading';
import { Button } from '../../components/ui/Button';
import { Card } from '../../components/ui/Card';
import { resetLayout, useSidebarMode, type SidebarMode } from '../../lib/layoutPrefs';
import { THEMES, useTheme, type ThemePreference } from '../../lib/theme';

const SIDEBAR_MODES: readonly SidebarMode[] = ['auto', 'expanded', 'collapsed'];

/**
 * Настройки › Внешний вид (план §3.1, §6.1): тема, режим боковой навигации и сброс сохранённой
 * раскладки (свёрнутость, контекстная панель, размеры панелей экранов). Всё хранится только в этом
 * браузере — так и подписано.
 */
export default function AppearancePage() {
  const { t } = useTranslation();
  const [theme, setTheme] = useTheme();
  const [sidebar, setSidebar] = useSidebarMode();
  const [resetDone, setResetDone] = useState(false);
  const themeId = useId();
  const sidebarId = useId();
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-6">
      <PageHeading title={t('settings.appearance.title')} />
      <p className="text-fg-muted">{t('settings.appearance.deviceNote')}</p>
      <Card>
        <fieldset aria-describedby={`${themeId}-hint`}>
          <legend className="text-base font-semibold text-fg">{t('theme.label')}</legend>
          <p id={`${themeId}-hint`} className="mb-3 text-sm text-fg-muted">
            {t('settings.appearance.themeHint')}
          </p>
          <div className="flex flex-col gap-2">
            {THEMES.map((th) => (
              <label key={th} className="flex min-h-touch items-center gap-3">
                <input type="radio" className="checkbox" name={`${themeId}-theme`} value={th} checked={theme === th} onChange={() => setTheme(th as ThemePreference)} />
                {t(`theme.${th}`)}
              </label>
            ))}
          </div>
        </fieldset>
      </Card>
      <Card>
        <fieldset>
          <legend className="text-base font-semibold text-fg">{t('settings.appearance.sidebar')}</legend>
          <div className="mt-3 flex flex-col gap-2">
            {SIDEBAR_MODES.map((m) => (
              <label key={m} className="flex min-h-touch items-center gap-3">
                <input type="radio" className="checkbox" name={`${sidebarId}-sidebar`} value={m} checked={sidebar === m} onChange={() => setSidebar(m)} />
                {t(`settings.appearance.sidebarModes.${m}`)}
              </label>
            ))}
          </div>
        </fieldset>
      </Card>
      <Card>
        <h2 className="text-base font-semibold text-fg">{t('settings.appearance.layoutTitle')}</h2>
        <p className="mt-1 text-sm text-fg-muted">{t('settings.appearance.layoutHint')}</p>
        <Button
          className="mt-3"
          icon="refresh"
          onClick={() => {
            resetLayout();
            setResetDone(true);
          }}
        >
          {t('settings.appearance.resetLayout')}
        </Button>
        <p role="status" className="mt-2 text-sm text-fg-muted">
          {resetDone ? t('settings.appearance.layoutReset') : ''}
        </p>
      </Card>
    </section>
  );
}

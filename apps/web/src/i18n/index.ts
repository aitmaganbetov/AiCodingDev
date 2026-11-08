import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import ru from './ru.json';

export const DEFAULT_LOCALE = 'ru';

// Все строки UI — ключи из ru.json; новая локаль добавляется ещё одним ресурсом.
void i18n.use(initReactI18next).init({
  resources: { ru: { translation: ru } },
  lng: DEFAULT_LOCALE,
  fallbackLng: DEFAULT_LOCALE,
  supportedLngs: [DEFAULT_LOCALE],
  interpolation: { escapeValue: false },
  returnNull: false,
  initAsync: false,
});

if (typeof document !== 'undefined') {
  document.documentElement.lang = i18n.language;
  i18n.on('languageChanged', (lng) => {
    document.documentElement.lang = lng;
  });
}

export default i18n;

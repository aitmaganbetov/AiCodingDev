import { useTranslation } from 'react-i18next';
import { Callout } from '../../components/ui/Badge';

/**
 * Постоянный баннер «не сертификация» (ТЗ §13, план §3.8): автоматические проверки и вердикты
 * платформы — не официальная сертификация и не заключение аудитора. Текст сервера (`disclaimer`
 * ответа) показывается отдельно там, где он пришёл.
 */
export function ComplianceDisclaimer({ className }: { className?: string }) {
  const { t } = useTranslation();
  return (
    <div role="note" aria-label={t('compliance.disclaimer.title')} className={className} data-testid="certification-disclaimer">
      <Callout tone="warning" icon="alert" title={t('compliance.disclaimer.title')}>
        <p>{t('compliance.disclaimer.text')}</p>
      </Callout>
    </div>
  );
}

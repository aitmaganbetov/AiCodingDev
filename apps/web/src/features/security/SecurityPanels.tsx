/**
 * Плитки Центра безопасности (F7): серьёзность, статусы исправления, CWE/контроли, риски
 * зависимостей (SCA/SBOM), секреты (SECRETS), evidence проверок безопасности. Все числа —
 * по загруженной выборке `GET /findings` (подпись выборки видна; «N+», если есть следующие страницы).
 */
import { useTranslation } from 'react-i18next';
import { FINDING_SEVERITIES, FINDING_STATUSES, type Finding } from '../../api/findings';
import type { EvidenceVerificationStatus } from '../../api/quality';
import { StatusBadge } from '../../components/StatusBadge';
import { Meter } from '../../components/ui/Charts';
import { Tile } from '../../components/ui/Tile';
import { SeverityBadge, VerificationBadge } from '../../components/ui/VerificationBadge';
import { gateVerificationState } from '../../lib/verification';
import { TextLink, workspacePath } from '../quality/shared';
import type { RunGateIndex } from './data';
import { cweMappings, controlMappings, severityDistribution, statusDistribution, UNRESOLVED_STATUSES, type MappingCount } from './model';

const SEVERITY_TONE = { critical: 'danger', high: 'danger', medium: 'warning', low: 'info', info: 'accent' } as const;

export interface Sample {
  items: Finding[];
  more: boolean;
}

function SampleNote({ sample }: { sample: Sample }) {
  const { t } = useTranslation();
  return <>{t(sample.more ? 'security.sample.partial' : 'security.sample.full', { count: sample.items.length })}</>;
}

export function SeverityTile({ sample, loading }: { sample: Sample; loading: boolean }) {
  const { t } = useTranslation();
  const dist = severityDistribution(sample.items);
  const max = Math.max(1, ...Object.values(dist));
  return (
    <Tile id="security-severity" icon="severityHigh" headingLevel={2} title={t('security.severity.title')} loading={loading} source={<SampleNote sample={sample} />}>
      <ul className="flex flex-col gap-2" aria-label={t('security.severity.title')}>
        {FINDING_SEVERITIES.map((s) => (
          <li key={s} data-severity-count={s}>
            <Meter
              label={<SeverityBadge severity={s} />}
              ratio={dist[s] / max}
              tone={SEVERITY_TONE[s]}
              valueText={sample.more && dist[s] > 0 ? t('ds.metric.more', { value: dist[s] }) : dist[s].toLocaleString('ru-RU')}
            />
          </li>
        ))}
      </ul>
    </Tile>
  );
}

export function RemediationTile({ sample, loading }: { sample: Sample; loading: boolean }) {
  const { t } = useTranslation();
  const dist = statusDistribution(sample.items);
  const unresolved = UNRESOLVED_STATUSES.reduce((a, s) => a + dist[s], 0);
  return (
    <Tile id="security-remediation" icon="listChecks" headingLevel={2} title={t('security.remediation.title')} loading={loading} source={<SampleNote sample={sample} />}>
      <p className="text-sm text-fg" data-testid="security-unresolved">
        {t('security.remediation.unresolved', { count: unresolved })}
      </p>
      <ul className="flex flex-col gap-1.5 text-sm" aria-label={t('security.remediation.title')}>
        {FINDING_STATUSES.map((s) => (
          <li key={s} className="flex flex-wrap items-center justify-between gap-2" data-status-count={s}>
            <StatusBadge kind="finding" status={s} labelKey={`quality.enums.finding_status.${s}`} />
            <span className="tabular-nums text-fg-muted">{dist[s].toLocaleString('ru-RU')}</span>
          </li>
        ))}
      </ul>
      <p className="text-xs text-fg-muted">{t('security.remediation.note')}</p>
    </Tile>
  );
}

function MappingList({
  items,
  label,
  active,
  onPick,
  testId,
}: {
  items: MappingCount[];
  label: string;
  active: string;
  onPick: (key: string) => void;
  testId: string;
}) {
  const { t } = useTranslation();
  if (items.length === 0) return <p className="text-sm text-fg-muted">{t('security.mappings.none')}</p>;
  return (
    <ul className="flex flex-wrap gap-2" aria-label={label} data-testid={testId}>
      {items.slice(0, 24).map((m) => (
        <li key={m.key}>
          <button
            type="button"
            className="btn-secondary btn-sm"
            aria-pressed={active === m.key}
            onClick={() => onPick(active === m.key ? '' : m.key)}
            data-mapping={m.key}
          >
            <span className="font-mono">{m.key}</span>
            <span className="tabular-nums text-fg-muted">
              {m.count}
              <span className="sr-only">{t('security.mappings.countSr', { count: m.count, unresolved: m.unresolved })}</span>
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

export function MappingsTile({
  sample,
  loading,
  cwe,
  control,
  onCwe,
  onControl,
}: {
  sample: Sample;
  loading: boolean;
  cwe: string;
  control: string;
  onCwe: (v: string) => void;
  onControl: (v: string) => void;
}) {
  const { t } = useTranslation();
  const withoutCwe = sample.items.filter((f) => !f.cwe_id).length;
  return (
    <Tile id="security-mappings" icon="layers" headingLevel={2} title={t('security.mappings.title')} loading={loading} source={<SampleNote sample={sample} />}>
      <h3 className="text-sm font-semibold">{t('security.mappings.cwe')}</h3>
      <MappingList items={cweMappings(sample.items)} label={t('security.mappings.cwe')} active={cwe} onPick={onCwe} testId="cwe-mappings" />
      {withoutCwe > 0 ? <p className="text-xs text-fg-muted">{t('security.mappings.withoutCwe', { count: withoutCwe })}</p> : null}
      <h3 className="text-sm font-semibold">{t('security.mappings.controls')}</h3>
      <MappingList items={controlMappings(sample.items)} label={t('security.mappings.controls')} active={control} onPick={onControl} testId="control-mappings" />
      <p className="text-xs text-fg-muted">{t('security.mappings.note')}</p>
    </Tile>
  );
}

function FindingLinks({ items, workspaceId, empty, testId }: { items: Finding[]; workspaceId: string; empty: string; testId: string }) {
  const { t } = useTranslation();
  if (items.length === 0) return <p className="text-sm text-fg-muted">{empty}</p>;
  return (
    <ul className="flex flex-col gap-2 text-sm" data-testid={testId}>
      {items.slice(0, 8).map((f) => (
        <li key={f.id} className="flex min-w-0 flex-wrap items-center gap-2" data-finding-id={f.id}>
          <SeverityBadge severity={f.severity} />
          <TextLink to={workspacePath(workspaceId, `quality/findings/${f.id}`)}>
            <span className="break-words">{f.title ?? f.rule_id}</span>
          </TextLink>
          <span className="break-all font-mono text-xs text-fg-muted">
            {f.file_path ? (f.line ? `${f.file_path}:${f.line}` : f.file_path) : f.rule_id}
          </span>
          <StatusBadge kind="finding" status={f.status} labelKey={`quality.enums.finding_status.${f.status}`} />
        </li>
      ))}
      {items.length > 8 ? <li className="text-xs text-fg-muted">{t('security.kind.more', { count: items.length - 8 })}</li> : null}
    </ul>
  );
}

export function KindTile({
  kind,
  items,
  unclassified,
  loading,
  workspaceId,
}: {
  kind: 'dependency' | 'secrets';
  items: Finding[];
  unclassified: number;
  loading: boolean;
  workspaceId: string;
}) {
  const { t } = useTranslation();
  const unresolved = items.filter((f) => UNRESOLVED_STATUSES.includes(f.status)).length;
  return (
    <Tile
      id={`security-${kind}`}
      icon={kind === 'secrets' ? 'lock' : 'layers'}
      headingLevel={2}
      title={t(`security.kind.${kind}.title`)}
      loading={loading}
      source={t(`security.kind.${kind}.source`)}
    >
      <p className="text-sm text-fg" data-testid={`kind-${kind}-count`} data-count={items.length}>
        {t('security.kind.count', { count: items.length, unresolved })}
      </p>
      <FindingLinks items={items} workspaceId={workspaceId} empty={t(`security.kind.${kind}.empty`)} testId={`kind-${kind}-list`} />
      {kind === 'secrets' ? <p className="text-xs text-fg-muted">{t('security.kind.secrets.redacted')}</p> : null}
      {unclassified > 0 ? (
        <p className="text-xs text-fg-muted" data-testid={`kind-${kind}-unclassified`}>
          {t('security.kind.unclassified', { count: unclassified })}
        </p>
      ) : null}
    </Tile>
  );
}

export function SecurityEvidenceTile({
  index,
  statuses,
  workspaceId,
  runCount,
}: {
  index: RunGateIndex;
  statuses: Map<string, EvidenceVerificationStatus>;
  workspaceId: string;
  runCount: number;
}) {
  const { t } = useTranslation();
  const gates = index.securityGates;
  return (
    <Tile
      id="security-evidence"
      icon="verifyPass"
      headingLevel={2}
      title={t('security.evidence.title')}
      loading={index.pending && gates.length === 0}
      source={t('security.evidence.source', { count: runCount })}
    >
      {gates.length === 0 ? (
        <p className="text-sm text-fg-muted">{t('security.evidence.empty')}</p>
      ) : (
        <ul className="flex flex-col gap-2 text-sm" aria-label={t('security.evidence.title')} data-testid="security-evidence-list">
          {gates.slice(0, 20).map((g) => (
            <li key={g.id} className="flex min-w-0 flex-wrap items-center gap-2" data-gate={g.gate_type}>
              <span className="font-mono text-xs">{g.gate_type}</span>
              <VerificationBadge result={gateVerificationState(g, g.evidence_id ? statuses.get(g.evidence_id) : undefined)} />
              {g.evidence_id ? (
                <TextLink to={workspacePath(workspaceId, `quality/evidence/${g.evidence_id}`)} srSuffix={g.gate_type}>
                  {t('quality.gate.openEvidence')}
                </TextLink>
              ) : (
                <span className="text-xs text-fg-muted">{t('quality.gate.noEvidence')}</span>
              )}
              <TextLink to={workspacePath(workspaceId, `quality/runs/${g.run_id}`)} srSuffix={g.gate_type}>
                {t('security.evidence.openRun')}
              </TextLink>
            </li>
          ))}
        </ul>
      )}
      {index.errors > 0 ? <p className="text-xs text-danger-text">{t('security.evidence.reportErrors', { count: index.errors })}</p> : null}
    </Tile>
  );
}

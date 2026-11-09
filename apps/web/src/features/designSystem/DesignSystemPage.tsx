import { useMemo, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { PageHeading } from '../../components/PageHeading';
import {
  AgentBadge,
  Button,
  Callout,
  Card,
  DataUnavailable,
  Dialog,
  DialogClose,
  DialogContent,
  DialogTrigger,
  IconButton,
  Kbd,
  Logo,
  LogoMark,
  Menu,
  MenuContent,
  MenuItem,
  MenuTrigger,
  Popover,
  PopoverContent,
  PopoverTrigger,
  SeverityBadge,
  Skeleton,
  StatusBadge,
  Table,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  VerificationBadge,
  VirtualList,
  useToast,
  type Column,
  type TableDensity,
} from '../../components/ui';
import { ResizableGroup, ResizableHandle, ResizablePanel, useResizableGroup } from '../../components/ui/Resizable';
import { AGENT_ROLE_ORDER } from '../../lib/agentRoles';
import { THEMES, useTheme } from '../../lib/theme';
import { gateVerificationState } from '../../lib/verification';

/**
 * Справочник дизайн-системы (F1) — только для разработки UI: маршрут `/w/:id/settings/design-system`
 * не входит в навигацию. Все данные на странице — MOCK (примеры компонентов), не данные системы.
 */
export default function DesignSystemPage() {
  const { t } = useTranslation();
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-8">
      <PageHeading title={t('ds.page.title')} />
      <Callout tone="info" title={t('ds.page.mockTitle')}>
        {t('ds.page.mockNote')}
      </Callout>
      <ThemeSection />
      <ColorsSection />
      <TypographySection />
      <ButtonsSection />
      <StatusSection />
      <AgentsSection />
      <SurfacesSection />
      <OverlaysSection />
      <FormsSection />
      <DataSection />
    </section>
  );
}

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section aria-labelledby={`ds-${id}`} className="flex flex-col gap-3">
      <h2 id={`ds-${id}`} className="text-xl font-semibold">
        {title}
      </h2>
      {children}
    </section>
  );
}

function ThemeSection() {
  const { t } = useTranslation();
  const [theme, setTheme] = useTheme();
  return (
    <Section id="theme" title={t('theme.label')}>
      <fieldset className="flex flex-wrap gap-4">
        <legend className="sr-only">{t('theme.label')}</legend>
        {THEMES.map((th) => (
          <label key={th} className="inline-flex min-h-touch items-center gap-2 text-sm">
            <input type="radio" className="checkbox" name="ds-theme" value={th} checked={theme === th} onChange={() => setTheme(th)} />
            {t(`theme.${th}`)}
          </label>
        ))}
      </fieldset>
      <div className="flex flex-wrap items-center gap-6">
        <Logo className="text-xl" markClassName="h-10 w-10" />
        <span className="rounded-lg bg-[#080B14] p-3">
          <LogoMark variant="dark" title={t('ds.page.logoDark')} className="h-12 w-12" />
        </span>
        <span className="rounded-lg bg-[#F6F8FC] p-3">
          <LogoMark variant="light" title={t('ds.page.logoLight')} className="h-12 w-12" />
        </span>
      </div>
    </Section>
  );
}

const SWATCHES = [
  ['canvas', 'bg-canvas'],
  ['canvas-secondary', 'bg-canvas-secondary'],
  ['surface', 'bg-surface'],
  ['surface-raised', 'bg-surface-raised'],
  ['accent', 'bg-accent'],
  ['secondary', 'bg-secondary'],
  ['highlight', 'bg-highlight'],
  ['border-active', 'bg-border-active'],
  ['success', 'bg-success'],
  ['warning', 'bg-warning'],
  ['danger', 'bg-danger'],
  ['info', 'bg-info'],
] as const;

function ColorsSection() {
  const { t } = useTranslation();
  return (
    <Section id="colors" title={t('ds.page.colors')}>
      <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        {SWATCHES.map(([name, cls]) => (
          <li key={name} className="flex items-center gap-2 text-sm">
            <span aria-hidden="true" className={`h-8 w-8 shrink-0 rounded-md border border-border ${cls}`} />
            <code className="text-code-sm">{name}</code>
          </li>
        ))}
      </ul>
      <div aria-hidden="true" className="h-2 rounded-full bg-gradient-brand" />
      <p className="text-sm text-fg-muted">{t('ds.page.gradientNote')}</p>
    </Section>
  );
}

function TypographySection() {
  const { t } = useTranslation();
  return (
    <Section id="type" title={t('ds.page.typography')}>
      <p className="text-display">Display 30/36 — Inter Variable</p>
      <p className="text-2xl font-semibold">H1 24/32 · Обзор рабочего пространства</p>
      <p className="text-xl font-semibold">H2 20/28 · Последние запуски</p>
      <p className="text-base">Body 16/24 — основной текст форм и описаний.</p>
      <p className="text-sm text-fg-muted">Body-dense 14/20 — таблицы, панели, списки.</p>
      <p className="text-xs text-fg-subtle">Caption 12/16 — мета, бейджи.</p>
      <p className="text-metric tabular-nums">1 284</p>
      <p className="font-mono text-code">$ pnpm e2e — JetBrains Mono 13/20 · 0O 1lI</p>
    </Section>
  );
}

function ButtonsSection() {
  const { t } = useTranslation();
  return (
    <Section id="buttons" title={t('ds.page.buttons')}>
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="primary" icon="play">
          Primary
        </Button>
        <Button variant="secondary">Secondary</Button>
        <Button variant="outline">Outline</Button>
        <Button variant="ghost">Ghost</Button>
        <Button variant="danger">Danger</Button>
        <Button variant="primary" loading>
          Loading
        </Button>
        <Button variant="secondary" disabled>
          Disabled
        </Button>
        <Button variant="primary" size="sm">
          Small
        </Button>
        <Button variant="primary" size="lg">
          Large
        </Button>
        <IconButton icon="settings" label={t('ds.page.iconButton')} />
      </div>
    </Section>
  );
}

function StatusSection() {
  const { t } = useTranslation();
  const states = [
    gateVerificationState({ status: 'PASS', evidence_id: 'mock' }, 'VERIFIED'),
    gateVerificationState({ status: 'FAIL', evidence_id: null }),
    gateVerificationState({ status: 'ERROR', evidence_id: null }),
    gateVerificationState({ status: 'SKIPPED', evidence_id: null }),
    gateVerificationState({ status: 'PASS', evidence_id: null }),
    gateVerificationState({ status: 'RUNNING', evidence_id: null }),
  ];
  return (
    <Section id="status" title={t('ds.page.status')}>
      <div className="flex flex-wrap gap-2">
        {states.map((r, i) => (
          <VerificationBadge key={i} result={r} showSource />
        ))}
      </div>
      <div className="flex flex-wrap gap-2">
        {(['RUNNING', 'COMPLETED', 'PAUSED', 'BLOCKED', 'FAILED', 'CANCELLED'] as const).map((s) => (
          <StatusBadge key={s} kind="run" status={s} />
        ))}
      </div>
      <div className="flex flex-wrap gap-2">
        {(['critical', 'high', 'medium', 'low', 'info'] as const).map((s) => (
          <SeverityBadge key={s} severity={s} />
        ))}
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        <Callout tone="success" title="Success">
          {t('ds.page.calloutText')}
        </Callout>
        <Callout tone="danger" title="Danger">
          {t('ds.page.calloutText')}
        </Callout>
      </div>
    </Section>
  );
}

function AgentsSection() {
  const { t } = useTranslation();
  return (
    <Section id="agents" title={t('ds.page.agents')}>
      <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {AGENT_ROLE_ORDER.map((r) => (
          <li key={r}>
            <AgentBadge agentRole={r} />
          </li>
        ))}
        <li>
          <AgentBadge agentRole="orchestrator" />
        </li>
        <li>
          <AgentBadge agentRole="developer" provider="Codex CLI" />
        </li>
      </ul>
    </Section>
  );
}

function SurfacesSection() {
  const { t } = useTranslation();
  return (
    <Section id="surfaces" title={t('ds.page.surfaces')}>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Card title="Default" headingLevel={3}>
          <p className="text-sm text-fg-muted">{t('ds.page.cardText')}</p>
        </Card>
        <Card title="Raised" headingLevel={3} variant="raised">
          <p className="text-sm text-fg-muted">{t('ds.page.cardText')}</p>
        </Card>
        <Card title="Glass" headingLevel={3} variant="glass">
          <p className="text-sm text-fg-muted">{t('ds.page.cardText')}</p>
        </Card>
        <Card title="Featured" headingLevel={3} variant="featured">
          <p className="text-sm text-fg-muted">{t('ds.page.cardText')}</p>
        </Card>
      </div>
      <DataUnavailable reason={t('ds.page.gapReason')} operation="GET /api/v1/system/health" />
      <Card title={t('ds.page.skeleton')} headingLevel={3}>
        <Skeleton lines={3} />
      </Card>
    </Section>
  );
}

function OverlaysSection() {
  const { t } = useTranslation();
  const toast = useToast();
  return (
    <Section id="overlays" title={t('ds.page.overlays')}>
      <div className="flex flex-wrap items-center gap-3">
        <Dialog>
          <DialogTrigger className="btn-secondary">{t('ds.page.openDialog')}</DialogTrigger>
          <DialogContent title={t('ds.page.dialogTitle')} footer={<DialogClose className="btn-primary">OK</DialogClose>}>
            <p>{t('ds.page.cardText')}</p>
          </DialogContent>
        </Dialog>
        <Menu>
          <MenuTrigger className="btn-secondary">{t('ds.page.openMenu')}</MenuTrigger>
          <MenuContent align="start">
            <MenuItem>{t('ds.page.menuItem', { n: 1 })}</MenuItem>
            <MenuItem>{t('ds.page.menuItem', { n: 2 })}</MenuItem>
          </MenuContent>
        </Menu>
        <Popover>
          <PopoverTrigger className="btn-secondary">{t('ds.page.openPopover')}</PopoverTrigger>
          <PopoverContent label={t('ds.page.openPopover')}>
            <p className="p-2 text-sm text-fg-muted">{t('ds.page.cardText')}</p>
          </PopoverContent>
        </Popover>
        <Button onClick={() => toast({ title: t('ds.page.toastTitle'), tone: 'success' })}>{t('ds.page.showToast')}</Button>
        <span className="text-sm text-fg-muted">
          <Kbd>Ctrl</Kbd> + <Kbd>K</Kbd>
        </span>
      </div>
      <Tabs defaultValue="a">
        <TabsList label={t('ds.page.tabs')} variant="pills">
          <TabsTrigger value="a">{t('ds.page.tab', { n: 1 })}</TabsTrigger>
          <TabsTrigger value="b">{t('ds.page.tab', { n: 2 })}</TabsTrigger>
        </TabsList>
        <TabsContent value="a">
          <p className="text-sm">{t('ds.page.tab', { n: 1 })}</p>
        </TabsContent>
        <TabsContent value="b">
          <p className="text-sm">{t('ds.page.tab', { n: 2 })}</p>
        </TabsContent>
      </Tabs>
    </Section>
  );
}

function FormsSection() {
  const { t } = useTranslation();
  return (
    <Section id="forms" title={t('ds.page.forms')}>
      <div className="grid gap-4 md:grid-cols-3">
        <label className="flex flex-col gap-1 text-sm font-medium">
          {t('ds.page.input')}
          <input className="input" placeholder={t('ds.page.placeholder')} />
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          {t('ds.page.select')}
          <select className="input" defaultValue="a">
            <option value="a">A</option>
            <option value="b">B</option>
          </select>
        </label>
        <label className="inline-flex min-h-touch items-center gap-2 self-end text-sm">
          <input type="checkbox" className="checkbox" defaultChecked />
          {t('ds.page.checkbox')}
        </label>
      </div>
    </Section>
  );
}

interface Row {
  id: string;
  name: string;
  count: number;
}

function DataSection() {
  const { t } = useTranslation();
  const [density, setDensity] = useState<TableDensity>('comfortable');
  const rows = useMemo<Row[]>(() => Array.from({ length: 5 }, (_, i) => ({ id: `r${i}`, name: `MOCK ${i + 1}`, count: (i + 1) * 7 })), []);
  const columns: Column<Row>[] = [
    { id: 'name', header: t('ds.page.colName'), cell: (r) => r.name, sortValue: (r) => r.name, rowHeader: true },
    { id: 'count', header: t('ds.page.colCount'), cell: (r) => r.count, sortValue: (r) => r.count, className: 'tabular-nums' },
  ];
  const lines = useMemo(() => Array.from({ length: 10_000 }, (_, i) => ({ id: `l${i}`, text: `[MOCK ${String(i + 1).padStart(5, '0')}] cli: строка вывода` })), []);
  return (
    <Section id="data" title={t('ds.page.data')}>
      <fieldset className="flex flex-wrap gap-4">
        <legend className="mb-1 text-sm font-medium">{t('ds.table.density')}</legend>
        {(['comfortable', 'compact'] as const).map((d) => (
          <label key={d} className="inline-flex min-h-touch items-center gap-2 text-sm">
            <input type="radio" className="checkbox" name="ds-density" checked={density === d} onChange={() => setDensity(d)} />
            {t(`ds.table.${d}`)}
          </label>
        ))}
      </fieldset>
      <Table caption={t('ds.page.tableCaption')} columns={columns} rows={rows} rowKey={(r) => r.id} density={density} minWidthClass="min-w-0" />
      <VirtualList
        kind="feed"
        followOutput
        label={t('ds.page.virtualLabel')}
        items={lines}
        getKey={(l) => l.id}
        renderItem={(l) => <span className="font-mono text-code-sm">{l.text}</span>}
        estimateSize={37}
        className="h-64"
      />
      <ResizableDemo />
    </Section>
  );
}

function ResizableDemo() {
  const { t } = useTranslation();
  const left = t('ds.page.panelLeft');
  const right = t('ds.page.panelRight');
  return (
    <div className="h-48 overflow-hidden rounded-lg border border-border" data-testid="resizable-demo">
      <ResizableGroup id="design-system-demo" defaultLayout={{ 'ds-left': 40, 'ds-right': 60 }}>
        <ResizablePanel id="ds-left" defaultSize="40" minSize="20">
          <div className="h-full bg-canvas-secondary p-3 text-sm">{left}</div>
        </ResizablePanel>
        <ResizableHandle before={left} after={right} />
        <ResizablePanel id="ds-right" defaultSize="60" minSize="20">
          <div className="flex h-full flex-col items-start gap-2 bg-surface p-3 text-sm">
            {right}
            <ResetLayoutButton />
          </div>
        </ResizablePanel>
      </ResizableGroup>
    </div>
  );
}

function ResetLayoutButton() {
  const { t } = useTranslation();
  const { reset } = useResizableGroup();
  return (
    <Button size="sm" icon="refresh" onClick={reset}>
      {t('ds.resizable.reset')}
    </Button>
  );
}

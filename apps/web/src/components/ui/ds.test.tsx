import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AGENT_ROLE_META, AGENT_ROLE_ORDER, agentRoleMeta } from '../../lib/agentRoles';
import { gateVerificationState } from '../../lib/verification';
import { StatusBadge } from '../StatusBadge';
import { AgentAvatar, AgentBadge } from './AgentBadge';
import { Badge, Callout } from './Badge';
import { Card } from './Card';
import { DataUnavailable } from './DataUnavailable';
import { Icon, ICON_NAMES } from './Icon';
import { Logo, LogoMark } from './Logo';
import { LAYOUT_STORAGE_PREFIX, ResizableGroup, ResizableHandle, ResizablePanel, safeLayoutStorage } from './Resizable';
import { Table, type Column } from './Table';
import { SeverityBadge, VerificationBadge } from './VerificationBadge';
import { VirtualList } from './VirtualList';

describe('Icon (Lucide)', () => {
  it('renders every mapped name as a decorative, non-focusable SVG', () => {
    const { container } = render(
      <div>
        {ICON_NAMES.map((n) => (
          <Icon key={n} name={n} />
        ))}
      </div>,
    );
    const svgs = container.querySelectorAll('svg');
    expect(svgs).toHaveLength(ICON_NAMES.length);
    for (const svg of svgs) {
      expect(svg).toHaveAttribute('aria-hidden', 'true');
      expect(svg).toHaveAttribute('focusable', 'false');
      expect(svg.getAttribute('stroke-width')).toBe('1.75');
    }
  });

  it('keeps the legacy names of the 21 custom icons', () => {
    for (const legacy of ['dashboard', 'folder', 'play', 'approval', 'terminal', 'quality', 'compliance', 'book', 'audit', 'layers', 'menu', 'close', 'keyboard', 'check', 'copy', 'alert', 'info', 'lock', 'construction', 'user', 'settings']) {
      expect(ICON_NAMES).toContain(legacy);
    }
  });
});

describe('status and verification badges (never color-only)', () => {
  it('StatusBadge keeps its label and adds a status icon', () => {
    const { container } = render(<StatusBadge kind="run" status="BLOCKED" />);
    expect(screen.getByText('Заблокирован', { exact: false })).toBeInTheDocument();
    expect(container.querySelector('[data-icon="verifyBlocked"]')).not.toBeNull();
  });

  it('VerificationBadge shows icon + label + pattern for each of the 5 states and the contract status for AT', () => {
    const cases = [
      [gateVerificationState({ status: 'PASS', evidence_id: 'e' }, 'VERIFIED'), 'Пройдено', 'verifyPass'],
      [gateVerificationState({ status: 'FAIL', evidence_id: null }), 'Не пройдено', 'verifyFail'],
      [gateVerificationState({ status: 'ERROR', evidence_id: null }), 'Заблокировано', 'verifyBlocked'],
      [gateVerificationState({ status: 'SKIPPED', evidence_id: null }), 'Не запускалось', 'verifyNotRun'],
      [gateVerificationState({ status: 'PASS', evidence_id: null }), 'Недостаточно доказательств', 'verifyInsufficient'],
    ] as const;
    for (const [result, label, icon] of cases) {
      const { container, unmount } = render(<VerificationBadge result={result} />);
      const badge = container.firstElementChild as HTMLElement;
      expect(badge).toHaveTextContent(label);
      expect(badge).toHaveAttribute('data-verification', result.state);
      expect(badge.querySelector(`[data-icon="${icon}"]`)).not.toBeNull();
      expect(badge).toHaveAttribute('title', `Статус контракта: ${result.source}`);
      expect(badge).toHaveTextContent(`Статус контракта: ${result.source}`);
      unmount();
    }
    const { container } = render(<VerificationBadge result={gateVerificationState({ status: 'ERROR', evidence_id: null })} />);
    expect(container.firstElementChild).toHaveClass('pattern-hatch');
  });

  it('SeverityBadge always shows the text label', () => {
    render(<SeverityBadge severity="critical" />);
    expect(screen.getByText('Критическая')).toBeInTheDocument();
  });

  it('Badge and Callout render optional icons', () => {
    const { container } = render(
      <>
        <Badge tone="success" icon="statusSuccess">
          ok
        </Badge>
        <Callout tone="warning">внимание</Callout>
      </>,
    );
    expect(container.querySelector('[data-icon="statusSuccess"]')).not.toBeNull();
    expect(container.querySelector('[data-icon="alert"]')).not.toBeNull();
  });
});

describe('AgentBadge', () => {
  it('maps all 12 AgentRole values to distinct glyphs and codes', () => {
    expect(AGENT_ROLE_ORDER).toHaveLength(12);
    const icons = new Set(AGENT_ROLE_ORDER.map((r) => AGENT_ROLE_META[r].icon));
    const codes = new Set(AGENT_ROLE_ORDER.map((r) => AGENT_ROLE_META[r].code));
    expect(icons.size).toBe(12);
    expect(codes.size).toBe(12);
  });

  it('shows glyph + code + name; non-role values (orchestrator, user) are shown as «Система»', () => {
    const { container } = render(
      <>
        <AgentBadge agentRole="developer" provider="Codex CLI" />
        <AgentBadge agentRole="orchestrator" />
      </>,
    );
    const [dev, sys] = container.querySelectorAll('[data-agent-role]');
    expect(dev).toHaveTextContent('DEVРазработчикCodex CLI');
    expect(dev!.querySelector('[data-icon="roleDeveloper"]')).not.toBeNull();
    expect(sys).toHaveAttribute('data-agent-role', 'system');
    expect(sys).toHaveTextContent('SYSСистема');
    expect(agentRoleMeta('router')).toBeNull();
  });

  it('AgentAvatar is decorative; name stays available to AT when hidden visually', () => {
    const { container } = render(
      <>
        <AgentAvatar agentRole="qa" />
        <AgentBadge agentRole="security" showName={false} />
      </>,
    );
    expect(container.querySelector('[data-agent-family="verify"]')).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByText('Безопасность')).toHaveClass('sr-only');
  });
});

describe('DataUnavailable', () => {
  it('explains the reason and the missing API operation, never a number', () => {
    render(<DataUnavailable reason="Нет операции чтения состояния системы." operation="GET /system/health" docsHref="https://example.test/gap" />);
    const box = screen.getByTestId('data-unavailable');
    expect(within(box).getByRole('heading', { name: 'Нет источника данных' })).toBeInTheDocument();
    expect(box).toHaveTextContent('Требуется операция API: GET /system/health');
    expect(box.textContent).not.toMatch(/\b0\b/);
    expect(within(box).getByRole('link', { name: /Подробнее/ })).toHaveAttribute('rel', 'noreferrer noopener');
  });
});

describe('Card, Logo, Table density', () => {
  it('Card glass/featured variants', () => {
    const { container } = render(
      <>
        <Card variant="glass">a</Card>
        <Card variant="featured">b</Card>
      </>,
    );
    expect(container.querySelector('[data-variant="glass"]')).toHaveClass('glass');
    expect(container.querySelector('[data-variant="featured"]')).toHaveClass('ring-brand');
  });

  it('LogoMark is decorative without title and an img with title; Logo keeps the plain-text name', () => {
    const { container } = render(
      <>
        <LogoMark />
        <LogoMark title="AiCodingDev" />
        <Logo />
      </>,
    );
    expect(container.querySelectorAll('svg')[0]).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByRole('img', { name: 'AiCodingDev' })).toBeInTheDocument();
    expect(container.lastElementChild).toHaveTextContent('AiCodingDev');
  });

  it('Table compact density', () => {
    const columns: Column<{ id: string }>[] = [{ id: 'id', header: 'ID', cell: (r) => r.id }];
    render(<Table caption="t" columns={columns} rows={[{ id: 'x' }]} rowKey={(r) => r.id} density="compact" />);
    expect(screen.getByRole('table')).toHaveAttribute('data-density', 'compact');
    expect(screen.getByRole('cell', { name: 'x' })).toHaveClass('px-2', 'py-1');
  });
});

describe('VirtualList', () => {
  // jsdom не считает раскладку: окно прокрутки 400 px, строки по 40 px.
  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (this: HTMLElement) {
      return this.dataset.testid === 'virtual-list' ? 400 : 40;
    });
    vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(600);
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(400);
  });
  afterEach(() => vi.restoreAllMocks());
  const items = Array.from({ length: 1000 }, (_, i) => ({ id: `e${i}`, text: `Событие ${i + 1}` }));

  it('renders only a window of rows, each with aria-posinset/aria-setsize; arrows/Home/End move focus', async () => {
    const user = userEvent.setup();
    render(<VirtualList items={items} getKey={(x) => x.id} renderItem={(x) => x.text} label="Лента" estimateSize={40} initialHeight={400} />);
    const list = screen.getByRole('list', { name: 'Лента' });
    const rendered = within(list).getAllByRole('listitem');
    expect(rendered.length).toBeGreaterThan(0);
    expect(rendered.length).toBeLessThan(100);
    expect(rendered[0]).toHaveAttribute('aria-posinset', '1');
    expect(rendered[0]).toHaveAttribute('aria-setsize', '1000');

    list.focus();
    await user.keyboard('{ArrowDown}');
    expect(document.activeElement).toHaveAttribute('aria-posinset', '1');
    await user.keyboard('{ArrowDown}');
    expect(document.activeElement).toHaveAttribute('aria-posinset', '2');
    await user.keyboard('{ArrowUp}');
    expect(document.activeElement).toHaveAttribute('aria-posinset', '1');
    // End/PageDown прокручивают окно (scrollToIndex) — в jsdom прокрутки нет, проверяется в e2e.
  });

  it('feed kind uses articles and aria-busy', () => {
    render(<VirtualList kind="feed" busy items={items.slice(0, 3)} getKey={(x) => x.id} renderItem={(x) => x.text} label="CLI" />);
    const feed = screen.getByRole('feed', { name: 'CLI' });
    expect(feed).toHaveAttribute('aria-busy', 'true');
    expect(within(feed).getAllByRole('article')).toHaveLength(3);
  });
});

describe('Resizable', () => {
  it('renders keyboard-focusable separators with an accessible name', () => {
    render(
      <div style={{ width: 800, height: 400 }}>
        <ResizableGroup id="test-layout" defaultLayout={{ a: 40, b: 60 }}>
          <ResizablePanel id="a" defaultSize="40">
            A
          </ResizablePanel>
          <ResizableHandle before="Навигатор" after="Лента" />
          <ResizablePanel id="b" defaultSize="60">
            B
          </ResizablePanel>
        </ResizableGroup>
      </div>,
    );
    const sep = screen.getByRole('separator', { name: 'Изменить размер: Навигатор / Лента' });
    expect(sep).toHaveAttribute('tabindex', '0');
    // Ресайз с клавиатуры требует реальной раскладки — проверяется в e2e (design-system.spec.ts).
  });

  it('layout storage never throws when localStorage is unavailable', () => {
    const original = Object.getOwnPropertyDescriptor(window, 'localStorage')!;
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get() {
        throw new Error('SecurityError');
      },
    });
    try {
      expect(safeLayoutStorage.getItem(`${LAYOUT_STORAGE_PREFIX}x`)).toBeNull();
      expect(() => safeLayoutStorage.setItem(`${LAYOUT_STORAGE_PREFIX}x`, '{}')).not.toThrow();
    } finally {
      Object.defineProperty(window, 'localStorage', original);
    }
  });
});

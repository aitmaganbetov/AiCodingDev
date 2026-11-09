import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { formatRelative } from '../../lib/format';
import { Button, IconButton } from './Button';
import { CodeBlock } from './CodeBlock';
import { Dialog, DialogContent, DialogTrigger } from './Dialog';
import { Menu, MenuContent, MenuItem, MenuTrigger } from './DropdownMenu';
import { Table, type Column } from './Table';
import { Tabs, TabsContent, TabsList, TabsTrigger } from './Tabs';
import { RelativeTime, Timestamp } from './Time';
import { ToastProvider, useToast } from './Toast';
import { Toolbar } from './Toolbar';
import { TooltipProvider } from './Tooltip';

interface Row {
  id: string;
  name: string;
  size: number | null;
}
const rows: Row[] = [
  { id: '1', name: 'бета', size: 20 },
  { id: '2', name: 'Альфа', size: null },
  { id: '3', name: 'гамма 10', size: 3 },
];
const columns: Column<Row>[] = [
  { id: 'name', header: 'Имя', cell: (r) => r.name, sortValue: (r) => r.name, rowHeader: true },
  { id: 'size', header: 'Размер', cell: (r) => r.size ?? '—', sortValue: (r) => r.size },
  { id: 'id', header: 'ID', cell: (r) => r.id },
];

const names = () =>
  within(screen.getByRole('table'))
    .getAllByRole('rowheader')
    .map((c) => c.textContent);

describe('Table', () => {
  it('sorts by clicking header buttons, exposes aria-sort and announces the sort in the caption', async () => {
    const user = userEvent.setup();
    render(<Table caption="Файлы" columns={columns} rows={rows} rowKey={(r) => r.id} />);
    expect(screen.getByRole('table', { name: 'Файлы' })).toBeInTheDocument();
    expect(names()).toEqual(['бета', 'Альфа', 'гамма 10']);
    expect(screen.queryByRole('button', { name: /ID/ })).not.toBeInTheDocument(); // не сортируемый

    await user.click(screen.getByRole('button', { name: /Имя/ }));
    expect(names()).toEqual(['Альфа', 'бета', 'гамма 10']);
    expect(screen.getByRole('columnheader', { name: /Имя/ })).toHaveAttribute('aria-sort', 'ascending');
    expect(screen.getByRole('table')).toHaveAccessibleName(/Отсортировано: «Имя», по возрастанию/);

    await user.click(screen.getByRole('button', { name: /Имя/ }));
    expect(names()).toEqual(['гамма 10', 'бета', 'Альфа']);
    expect(screen.getByRole('columnheader', { name: /Имя/ })).toHaveAttribute('aria-sort', 'descending');

    await user.click(screen.getByRole('button', { name: /Размер/ }));
    expect(names()).toEqual(['гамма 10', 'бета', 'Альфа']); // null — в конце
    expect(screen.getByRole('columnheader', { name: /Имя/ })).not.toHaveAttribute('aria-sort');
  });

  it('controlled (server) sorting calls onSortChange and keeps row order; empty table shows a message', async () => {
    const user = userEvent.setup();
    const onSortChange = vi.fn();
    const { rerender } = render(
      <Table caption="Файлы" columns={columns} rows={rows} rowKey={(r) => r.id} sort={{ columnId: 'size', direction: 'desc' }} onSortChange={onSortChange} />,
    );
    await user.click(screen.getByRole('button', { name: /Размер/ }));
    expect(onSortChange).toHaveBeenCalledWith({ columnId: 'size', direction: 'asc' });
    expect(names()).toEqual(['бета', 'Альфа', 'гамма 10']);
    rerender(<Table caption="Файлы" columns={columns} rows={[]} rowKey={(r) => r.id} empty="Файлов нет" />);
    expect(screen.getByText('Файлов нет')).toBeInTheDocument();
  });
});

describe('Toolbar', () => {
  it('has a single tab stop and moves focus with arrow keys / Home / End', async () => {
    const user = userEvent.setup();
    render(
      <>
        <Toolbar label="Действия">
          <Button>Первая</Button>
          <Button>Вторая</Button>
          <Button>Третья</Button>
        </Toolbar>
        <button type="button">После</button>
      </>,
    );
    expect(screen.getByRole('toolbar', { name: 'Действия' })).toBeInTheDocument();
    await user.tab();
    expect(screen.getByRole('button', { name: 'Первая' })).toHaveFocus();
    await user.keyboard('{ArrowRight}');
    expect(screen.getByRole('button', { name: 'Вторая' })).toHaveFocus();
    await user.keyboard('{End}');
    expect(screen.getByRole('button', { name: 'Третья' })).toHaveFocus();
    await user.keyboard('{ArrowRight}');
    expect(screen.getByRole('button', { name: 'Первая' })).toHaveFocus();
    await user.keyboard('{ArrowLeft}');
    await user.tab();
    expect(screen.getByRole('button', { name: 'После' })).toHaveFocus();
    await user.tab({ shift: true });
    expect(screen.getByRole('button', { name: 'Третья' })).toHaveFocus();
  });
});

describe('Dialog, Menu, Tabs, Tooltip, Toast (Radix wrappers)', () => {
  it('dialog traps focus, closes on Escape and returns focus to the trigger', async () => {
    const user = userEvent.setup();
    render(
      <Dialog>
        <DialogTrigger className="btn-secondary">Открыть</DialogTrigger>
        <DialogContent title="Подтверждение" description="Описание">
          <button type="button">Внутри</button>
        </DialogContent>
      </Dialog>,
    );
    await user.click(screen.getByRole('button', { name: 'Открыть' }));
    const dialog = screen.getByRole('dialog', { name: 'Подтверждение' });
    expect(dialog).toHaveAccessibleDescription('Описание');
    expect(within(dialog).getByRole('button', { name: 'Закрыть' })).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Открыть' })).toHaveFocus();
  });

  it('menu: keyboard opens and selects items', async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <Menu>
        <MenuTrigger className="btn-secondary">Действия</MenuTrigger>
        <MenuContent>
          <MenuItem onSelect={onSelect}>Удалить</MenuItem>
        </MenuContent>
      </Menu>,
    );
    screen.getByRole('button', { name: 'Действия' }).focus();
    await user.keyboard('{Enter}');
    await user.click(await screen.findByRole('menuitem', { name: 'Удалить' }));
    expect(onSelect).toHaveBeenCalled();
  });

  it('tabs switch panels with arrow keys', async () => {
    const user = userEvent.setup();
    render(
      <Tabs defaultValue="a">
        <TabsList label="Разделы">
          <TabsTrigger value="a">Граф</TabsTrigger>
          <TabsTrigger value="b">Чат</TabsTrigger>
        </TabsList>
        <TabsContent value="a">Панель графа</TabsContent>
        <TabsContent value="b">Панель чата</TabsContent>
      </Tabs>,
    );
    expect(screen.getByRole('tablist', { name: 'Разделы' })).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'Граф' }));
    await user.keyboard('{ArrowRight}');
    expect(screen.getByRole('tab', { name: 'Чат' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tabpanel')).toHaveTextContent('Панель чата');
  });

  it('icon button has an accessible name; toast is announced', async () => {
    const user = userEvent.setup();
    function Notify() {
      const toast = useToast();
      return <Button onClick={() => toast({ title: 'Команда принята', tone: 'success' })}>Отправить</Button>;
    }
    render(
      <TooltipProvider>
        <ToastProvider>
          <IconButton icon="settings" label="Настройки панели" />
          <Notify />
        </ToastProvider>
      </TooltipProvider>,
    );
    expect(screen.getByRole('button', { name: 'Настройки панели' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Отправить' }));
    expect(await screen.findByTestId('toast')).toHaveTextContent('Команда принята');
    expect(screen.getByRole('region', { name: /Уведомления \(F8\)/ })).toBeInTheDocument();
  });
});

describe('CodeBlock, Timestamp, RelativeTime, Button', () => {
  it('code block renders text only (no HTML), is a focusable scroll region and copies to the clipboard', async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    render(<CodeBlock label="stdout" code={'<img src=x onerror=alert(1)>\nok'} />);
    expect(document.querySelector('img')).toBeNull();
    const pre = screen.getByLabelText('stdout');
    expect(pre.tagName).toBe('PRE');
    expect(pre).toHaveAttribute('tabindex', '0');
    await user.click(screen.getByRole('button', { name: 'Копировать' }));
    expect(writeText).toHaveBeenCalledWith('<img src=x onerror=alert(1)>\nok');
    expect(await screen.findByRole('status')).toHaveTextContent('Скопировано');
  });

  it('formats absolute and relative time in the ru locale', () => {
    const now = Date.parse('2026-11-08T12:00:00Z');
    expect(formatRelative('2026-11-08T11:55:00Z', 'ru', now)).toBe('5 минут назад');
    expect(formatRelative('2026-11-08T11:59:50Z', 'ru', now)).toBe('сейчас');
    expect(formatRelative('2026-11-07T12:00:00Z', 'ru', now)).toBe('вчера');
    expect(formatRelative('2026-11-08T14:00:00Z', 'ru', now)).toBe('через 2 часа');
    render(
      <>
        <Timestamp value="2026-11-08T09:30:00Z" format="date" />
        <RelativeTime value={new Date(Date.now() - 3 * 3600_000).toISOString()} />
      </>,
    );
    expect(screen.getByText('8 нояб. 2026 г.')).toHaveAttribute('datetime', '2026-11-08T09:30:00Z');
    expect(screen.getByText('3 часа назад')).toBeInTheDocument();
    expect(screen.getByText(/^Точное время:/)).toHaveClass('sr-only');
  });

  it('loading button is disabled and busy', () => {
    render(
      <MemoryRouter>
        <Button loading variant="primary">
          Сохранить
        </Button>
      </MemoryRouter>,
    );
    const b = screen.getByRole('button', { name: 'Сохранить' });
    expect(b).toBeDisabled();
    expect(b).toHaveAttribute('aria-busy', 'true');
  });
});

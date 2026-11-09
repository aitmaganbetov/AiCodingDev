import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import '../../i18n';
import { ChatItemView } from './ChatItemView';

describe('ChatItemView', () => {
  it('renders agent content as text only (no HTML), with type, sender, recipient and mentions', async () => {
    const onMention = vi.fn();
    const { container } = render(
      <MemoryRouter>
        <ChatItemView
          item={{
            id: 'i1',
            item_type: 'agent_message',
            sender_type: 'agent',
            sender: { role: 'architect', provider: 'claude', user: null },
            recipient_role: 'developer',
            message_kind: 'agent_relay',
            acp_type: 'HANDOFF',
            body: '<img src=x onerror="window.pwned=1"> **жирный** [ссылка](javascript:alert(1))',
            mentions: ['qa'],
            task_id: 't1',
            created_at: '2026-11-08T09:00:00Z',
          }}
          taskTitle={() => 'Реализовать экспорт'}
          taskHref={(id) => `/tasks/${id}`}
          onMention={onMention}
        />
      </MemoryRouter>,
    );
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelectorAll('a')).toHaveLength(1); // только ссылка на задачу, не из содержимого
    expect(screen.getByTestId('chat-item-body').textContent).toContain('<img src=x onerror="window.pwned=1"> **жирный**');
    expect(screen.getByRole('heading', { level: 3 }).textContent).toBe('Архитектор · Claude → Разработчик');
    expect(screen.getByText('Передача задачи')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Задача: Реализовать экспорт' })).toHaveAttribute('href', '/tasks/t1');
    await userEvent.click(screen.getByRole('button', { name: 'Показать поток роли «QA»' }));
    expect(onMention).toHaveBeenCalledWith('qa');
  });
});

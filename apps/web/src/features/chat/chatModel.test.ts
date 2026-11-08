import { describe, expect, it } from 'vitest';
import type { ChatItem } from '../../api/chat';
import { envelope } from '../../test/sse';
import {
  chatQuery,
  filtersComplete,
  filtersFromParams,
  liveItemFromEvent,
  matchesFilters,
  mergeTimeline,
  paramsFromFilters,
  parseMentions,
} from './chatModel';

function item(id: string, created_at: string, init: Partial<ChatItem> = {}): ChatItem {
  return { id, item_type: 'agent_message', sender_type: 'agent', message_kind: 'agent_relay', body: id, created_at, ...init };
}

describe('chat filters', () => {
  it('round-trips through URL params and drops invalid values', () => {
    const f = filtersFromParams(new URLSearchParams('stream=role&role=reviewer&types=agent_message,bogus&q=тест'));
    expect(f).toEqual({ stream: 'role', role: 'reviewer', task_id: undefined, thread_id: undefined, q: 'тест', item_type: ['agent_message'] });
    expect(filtersFromParams(paramsFromFilters(f))).toEqual(f);
    expect(filtersFromParams(new URLSearchParams('stream=nope&role=hacker')).stream).toBe('all_agents');
    expect(filtersFromParams(new URLSearchParams('role=hacker')).role).toBeUndefined();
  });

  it('requires role/task for their streams and sends them only there', () => {
    expect(filtersComplete({ stream: 'role' })).toBe(false);
    expect(filtersComplete({ stream: 'task', task_id: 't' })).toBe(true);
    expect(chatQuery({ stream: 'all_agents', role: 'qa', task_id: 't' }, null, 50)).toEqual({ stream: 'all_agents', order: 'desc', limit: 50 });
    expect(chatQuery({ stream: 'task', task_id: 't', q: 'x' }, 'c1', 10)).toEqual({ stream: 'task', task_id: 't', q: 'x', order: 'desc', limit: 10, cursor: 'c1' });
  });
});

describe('live items', () => {
  it('maps chat.message to a timeline item and ignores other events', () => {
    const e = envelope(7, 'chat.message');
    e.payload = {
      chat_message_id: 'm1',
      thread_id: 'th',
      scope: 'RUN',
      message_kind: 'AGENT_MESSAGE',
      sender_type: 'agent',
      sender_role: 'architect',
      provider: 'claude',
      target_role: 'developer',
      mentions: ['qa', 'nobody'],
      body: '<img src=x onerror=alert(1)>',
      links: [{ kind: 'diff', id: 'd1' }, { kind: 'gate_execution', id: 'g1' }],
      created_at: '2026-11-08T09:00:00Z',
    };
    const live = liveItemFromEvent(e)!;
    expect(live).toMatchObject({
      id: 'm1',
      item_type: 'agent_message',
      sender: { role: 'architect', provider: 'claude' },
      recipient_role: 'developer',
      mentions: ['qa'],
      refs: [{ type: 'diff', id: 'd1' }],
      body: '<img src=x onerror=alert(1)>',
      seq: 7,
      live: true,
    });
    expect(liveItemFromEvent(envelope(8, 'agent.message'))).toBeNull();
  });

  it('applies stream, type, thread and search filters client-side', () => {
    const it1 = item('a', '2026-11-08T09:00:00Z', { sender: { role: 'developer', provider: 'codex' }, thread_id: 't1', body: 'Готов экспорт CSV' });
    expect(matchesFilters(it1, { stream: 'codex' })).toBe(true);
    expect(matchesFilters(it1, { stream: 'claude' })).toBe(false);
    expect(matchesFilters(it1, { stream: 'role', role: 'developer' })).toBe(true);
    expect(matchesFilters(it1, { stream: 'role', role: 'qa' })).toBe(false);
    expect(matchesFilters(it1, { stream: 'all_agents', thread_id: 't2' })).toBe(false);
    expect(matchesFilters(it1, { stream: 'all_agents', item_type: ['directive'] })).toBe(false);
    expect(matchesFilters(it1, { stream: 'all_agents', q: 'экспорт csv' })).toBe(true);
    expect(matchesFilters(it1, { stream: 'approvals' })).toBe(false);
  });

  it('merges REST (desc) with live items chronologically without duplicates', () => {
    const rest = [item('b', '2026-11-08T09:02:00Z'), item('a', '2026-11-08T09:01:00Z')];
    const live = [{ ...item('b', '2026-11-08T09:02:00Z'), live: true }, { ...item('c', '2026-11-08T09:03:00Z'), live: true }, { ...item('c', '2026-11-08T09:03:00Z'), live: true }];
    const merged = mergeTimeline(rest, live);
    expect(merged.map((x) => x.id)).toEqual(['a', 'b', 'c']);
    expect(merged[1]!.live).toBeUndefined();
  });

  it('parses @role mentions', () => {
    expect(parseMentions('@developer и @qa, посмотрите; mail@developer — нет; @unknown')).toEqual(['developer', 'qa']);
  });
});

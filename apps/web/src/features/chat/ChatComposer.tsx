import { useMutation } from '@tanstack/react-query';
import { useId, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { chatApi } from '../../api/chat';
import { IdempotentIntent } from '../../api/client';
import type { AgentRole } from '../../api/runs';
import { FormAlert } from '../../components/form';
import { Popover, PopoverContent, PopoverTrigger } from '../../components/ui';
import { problemMessage } from '../../lib/problem';
import { roleLabel } from '../runs/labels';
import { AGENT_ROLES, CHAT_BODY_MAX, MAX_MENTIONS, parseMentions } from './chatModel';

interface Props {
  runId: string;
  threadId: string | undefined;
  onPosted: () => void;
}

/**
 * Комментарий в ленту run с упоминаниями ролей (`@developer` в тексте или выбором из списка).
 * Комментарий агентам не доставляется (ChatMessageCreateRequest) — для воздействия есть поручения.
 */
export function ChatComposer({ runId, threadId, onPosted }: Props) {
  const { t } = useTranslation();
  const id = useId();
  const intent = useRef(new IdempotentIntent());
  const [body, setBody] = useState('');
  const [picked, setPicked] = useState<AgentRole[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);
  const mentions = [...new Set([...picked, ...parseMentions(body)])].slice(0, MAX_MENTIONS);

  const post = useMutation({
    mutationFn: () => {
      const payload = { body: body.trim(), ...(threadId ? { thread_id: threadId } : {}), ...(mentions.length > 0 ? { mentions } : {}) };
      return chatApi.post(runId, payload, intent.current.keyFor({ runId, ...payload }));
    },
    onSuccess: () => {
      intent.current.complete();
      setBody('');
      setPicked([]);
      setSent(true);
      onPosted();
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSent(false);
    if (!body.trim()) {
      setError(t('chat.composer.empty'));
      return;
    }
    setError(null);
    post.mutate();
  };
  const toggle = (role: AgentRole, on: boolean) => setPicked((prev) => (on ? [...prev, role] : prev.filter((r) => r !== role)));

  return (
    <form onSubmit={submit} className="card flex flex-col gap-3" aria-labelledby={`${id}-title`} noValidate>
      <h2 id={`${id}-title`} className="text-base font-semibold">
        {t('chat.composer.title')}
      </h2>
      <div className="flex flex-col gap-1">
        <label htmlFor={`${id}-body`} className="text-sm font-medium">
          {t('chat.composer.label')}
        </label>
        <textarea
          id={`${id}-body`}
          className="input min-h-[96px]"
          value={body}
          maxLength={CHAT_BODY_MAX}
          aria-invalid={Boolean(error)}
          aria-describedby={`${id}-hint${error ? ` ${id}-error` : ''}`}
          onChange={(e) => setBody(e.target.value)}
        />
        <p id={`${id}-hint`} className="text-sm text-fg-muted">
          {t('chat.composer.hint')}
        </p>
        {error ? (
          <p id={`${id}-error`} className="text-sm font-medium text-danger-text">
            {error}
          </p>
        ) : null}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Popover>
          <PopoverTrigger className="btn-secondary btn-sm">{t('chat.composer.mention')}</PopoverTrigger>
          <PopoverContent label={t('chat.composer.mentionTitle')}>
            <fieldset className="flex flex-col gap-1">
              <legend className="mb-1 text-sm font-medium">{t('chat.composer.mentionTitle')}</legend>
              {AGENT_ROLES.map((role) => (
                <div key={role} className="flex items-center gap-2">
                  <input
                    id={`${id}-m-${role}`}
                    type="checkbox"
                    className="h-4 w-4 accent-[rgb(var(--color-accent))]"
                    checked={mentions.includes(role)}
                    disabled={!picked.includes(role) && mentions.includes(role)}
                    onChange={(e) => toggle(role, e.target.checked)}
                  />
                  <label htmlFor={`${id}-m-${role}`} className="text-sm">
                    {roleLabel(t, role)} <span className="font-mono text-xs text-fg-muted">@{role}</span>
                  </label>
                </div>
              ))}
            </fieldset>
          </PopoverContent>
        </Popover>
        {mentions.length > 0 ? (
          <p className="text-sm text-fg-muted" data-testid="composer-mentions">
            {t('chat.composer.mentions', { roles: mentions.map((r) => roleLabel(t, r)).join(', ') })}
          </p>
        ) : null}
      </div>
      {post.isError ? <FormAlert messages={[problemMessage(t, post.error)]} /> : null}
      <div className="flex flex-wrap items-center gap-3">
        <button type="submit" className="btn-primary" disabled={post.isPending}>
          {post.isPending ? t('chat.composer.sending') : t('chat.composer.send')}
        </button>
        <p role="status" className="text-sm text-fg-muted">
          {sent ? t('chat.composer.sent') : ''}
        </p>
      </div>
    </form>
  );
}

import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { IdempotentIntent } from '../../api/client';
import { memberKeys, membersApi, type Member, type MemberCreateRequest } from '../../api/settings';
import type { WorkspaceRole } from '../../api/workspaces';
import { sessionQueryKey, useSession } from '../../auth/session';
import { FormAlert, RadioGroup, TextField, focusFirstInvalid } from '../../components/form';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import { Badge, Button, Dialog, DialogClose, DialogContent, DialogTrigger, LoadMore, Table, Timestamp, type Column } from '../../components/ui';
import { mapProblemToFields, problemMessage } from '../../lib/problem';
import { EMAIL_MAX } from '../../lib/validation';
import { useStepUp } from '../../auth/StepUp';

export const MEMBERS_PAGE_SIZE = 50;
export const ROLES = ['owner', 'admin', 'developer', 'reviewer', 'viewer'] as const satisfies readonly WorkspaceRole[];
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function roleOptions(t: (k: string) => string) {
  return ROLES.map((r) => ({ value: r, label: t(`enums.workspace_role.${r}`), description: t(`settings.roleHint.${r}`) }));
}

function AddMemberForm({ workspaceId, onDone }: { workspaceId: string; onDone: (message: string) => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const formRef = useRef<HTMLFormElement>(null);
  const intent = useRef(new IdempotentIntent());
  const [email, setEmail] = useState('');
  const [role, setRole] = useState<WorkspaceRole>('developer');
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<'email' | 'role', string>>>({});
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const stepUp = useStepUp();
  const add = useMutation({
    mutationFn: (body: MemberCreateRequest) => stepUp.run(() => membersApi.add(workspaceId, body, intent.current.keyFor(body))),
    onSuccess: async (member) => {
      intent.current.complete();
      await queryClient.invalidateQueries({ queryKey: memberKeys.list(workspaceId) });
      onDone(t('settings.members.added', { name: member.user.display_name }));
    },
    onError: (error) => {
      const mapped = mapProblemToFields(t, error, ['email', 'role'] as const);
      setFieldErrors(mapped.fields);
      setFormErrors(mapped.form);
      requestAnimationFrame(() => focusFirstInvalid(formRef.current));
    },
  });
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const value = email.trim();
    const errors: Partial<Record<'email' | 'role', string>> = {};
    if (!value) errors.email = t('validation.required');
    else if (value.length > EMAIL_MAX || !EMAIL_PATTERN.test(value)) errors.email = t('settings.members.emailInvalid');
    setFieldErrors(errors);
    setFormErrors([]);
    if (Object.keys(errors).length > 0) {
      requestAnimationFrame(() => focusFirstInvalid(formRef.current));
      return;
    }
    add.mutate({ email: value, role });
  };
  return (
    <form ref={formRef} noValidate onSubmit={onSubmit} className="flex flex-col gap-4" aria-label={t('settings.members.addTitle')}>
      <FormAlert messages={formErrors} />
      <TextField
        id="member-email"
        type="email"
        label={t('settings.members.email')}
        hint={t('settings.members.emailHint')}
        autoComplete="off"
        maxLength={EMAIL_MAX}
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        error={fieldErrors.email}
      />
      <RadioGroup name="member-role" legend={t('settings.members.role')} value={role} onChange={setRole} options={roleOptions(t)} error={fieldErrors.role} />
      <div>
        <Button type="submit" variant="primary" loading={add.isPending}>
          {t('settings.members.add')}
        </Button>
      </div>
      {stepUp.dialog}
    </form>
  );
}

function ChangeRoleDialog({ workspaceId, member, onResult }: { workspaceId: string; member: Member; onResult: (text: string, ok: boolean) => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [role, setRole] = useState<WorkspaceRole>(member.role);
  const [errors, setErrors] = useState<string[]>([]);
  const stepUp = useStepUp();
  const update = useMutation({
    mutationFn: (next: WorkspaceRole) => stepUp.run(() => membersApi.update(workspaceId, member.user.id, { role: next }, member.version)),
    onSuccess: async (updated) => {
      setOpen(false);
      onResult(t('settings.members.roleChanged', { name: updated.user.display_name, role: t(`enums.workspace_role.${updated.role}`) }), true);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: memberKeys.list(workspaceId) }),
        queryClient.invalidateQueries({ queryKey: sessionQueryKey }),
      ]);
    },
    onError: (error) => setErrors([problemMessage(t, error)]),
  });
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) {
          setRole(member.role);
          setErrors([]);
        }
      }}
    >
      <DialogTrigger asChild>
        <Button size="sm" aria-label={t('settings.members.changeRoleOf', { name: member.user.display_name })}>
          {t('settings.members.changeRole')}
        </Button>
      </DialogTrigger>
      <DialogContent
        title={t('settings.members.changeRoleTitle', { name: member.user.display_name })}
        footer={
          <>
            <DialogClose asChild>
              <Button>{t('action.cancel')}</Button>
            </DialogClose>
            <Button variant="primary" loading={update.isPending} disabled={role === member.role} onClick={() => update.mutate(role)}>
              {t('settings.members.saveRole')}
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <FormAlert messages={errors} />
          <RadioGroup name={`role-${member.user.id}`} legend={t('settings.members.role')} value={role} onChange={setRole} options={roleOptions(t)} />
        </div>
        {stepUp.dialog}
      </DialogContent>
    </Dialog>
  );
}

function RemoveMemberDialog({ workspaceId, member, onResult }: { workspaceId: string; member: Member; onResult: (text: string, ok: boolean) => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const stepUp = useStepUp();
  const remove = useMutation({
    mutationFn: () => stepUp.run(() => membersApi.remove(workspaceId, member.user.id, member.version)),
    onSuccess: async () => {
      setOpen(false);
      onResult(t('settings.members.removed', { name: member.user.display_name }), true);
      await queryClient.invalidateQueries({ queryKey: memberKeys.list(workspaceId) });
    },
    onError: (error) => setErrors([problemMessage(t, error)]),
  });
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) setErrors([]);
      }}
    >
      <DialogTrigger asChild>
        <Button size="sm" variant="danger" aria-label={t('settings.members.removeOf', { name: member.user.display_name })}>
          {t('settings.members.remove')}
        </Button>
      </DialogTrigger>
      <DialogContent
        title={t('settings.members.removeTitle', { name: member.user.display_name })}
        description={t('settings.members.removeDescription')}
        footer={
          <>
            <DialogClose asChild>
              <Button>{t('action.cancel')}</Button>
            </DialogClose>
            <Button variant="danger" loading={remove.isPending} onClick={() => remove.mutate()}>
              {t('settings.members.removeConfirm')}
            </Button>
          </>
        }
      >
        <FormAlert messages={errors} />
        <p className="break-all text-sm">{member.email}</p>
        {stepUp.dialog}
      </DialogContent>
    </Dialog>
  );
}

/** Участники workspace: список (member:read), добавление/смена роли/удаление (member:manage — owner). */
export function MembersPanel({ workspaceId, canManage }: { workspaceId: string; canManage: boolean }) {
  const { t } = useTranslation();
  const { data: session } = useSession();
  const [addOpen, setAddOpen] = useState(false);
  const [notice, setNotice] = useState<{ text: string; ok: boolean } | null>(null);
  const query = useInfiniteQuery({
    queryKey: memberKeys.list(workspaceId),
    queryFn: ({ pageParam, signal }) =>
      membersApi.list(workspaceId, { limit: MEMBERS_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = query.data?.pages.flatMap((p) => p.items) ?? [];
  const onResult = (text: string, ok: boolean) => setNotice({ text, ok });

  const columns: Column<Member>[] = [
    {
      id: 'name',
      header: t('settings.members.name'),
      rowHeader: true,
      cell: (m) => (
        <span className="flex flex-wrap items-center gap-2">
          {m.user.display_name}
          {m.user.id === session?.user.id ? <Badge tone="info">{t('settings.members.you')}</Badge> : null}
        </span>
      ),
      sortValue: (m) => m.user.display_name,
    },
    { id: 'email', header: t('settings.members.email'), cell: (m) => <span className="break-all">{m.email}</span>, sortValue: (m) => m.email },
    { id: 'role', header: t('settings.members.role'), cell: (m) => t(`enums.workspace_role.${m.role}`, { defaultValue: m.role }), sortValue: (m) => ROLES.indexOf(m.role as (typeof ROLES)[number]) },
    { id: 'joined', header: t('settings.members.joined'), cell: (m) => <Timestamp value={m.joined_at} format="date" />, sortValue: (m) => m.joined_at },
    ...(canManage
      ? [
          {
            id: 'actions',
            header: t('settings.members.actions'),
            cell: (m: Member) => (
              <span className="flex flex-wrap gap-2">
                <ChangeRoleDialog workspaceId={workspaceId} member={m} onResult={onResult} />
                <RemoveMemberDialog workspaceId={workspaceId} member={m} onResult={onResult} />
              </span>
            ),
          },
        ]
      : []),
  ];

  let body;
  if (query.isPending) body = <LoadingState label={t('settings.members.loading')} />;
  else if (query.isError && items.length === 0) body = <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  else if (items.length === 0) body = <EmptyState headingLevel={3} title={t('settings.members.empty')} />;
  else {
    body = (
      <>
        <Table
          caption={t('settings.members.title')}
          columns={columns}
          rows={items}
          rowKey={(m) => m.user.id}
          defaultSort={{ columnId: 'role', direction: 'asc' }}
          minWidthClass="min-w-[48rem]"
        />
        <LoadMore
          shown={items.length}
          hasNextPage={query.hasNextPage}
          isFetchingNextPage={query.isFetchingNextPage}
          nextPageError={query.isFetchNextPageError ? query.error : null}
          onLoadMore={() => void query.fetchNextPage()}
        />
      </>
    );
  }

  return (
    <section aria-labelledby="members-title" className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="members-title" className="text-lg font-semibold">
          {t('settings.members.title')}
        </h2>
        {canManage ? (
          <Dialog open={addOpen} onOpenChange={setAddOpen}>
            <DialogTrigger asChild>
              <Button variant="primary" icon="user">
                {t('settings.members.addOpen')}
              </Button>
            </DialogTrigger>
            <DialogContent title={t('settings.members.addTitle')} description={t('settings.members.addDescription')}>
              <AddMemberForm
                workspaceId={workspaceId}
                onDone={(text) => {
                  setAddOpen(false);
                  setNotice({ text, ok: true });
                }}
              />
            </DialogContent>
          </Dialog>
        ) : null}
      </div>
      {!canManage ? <p className="text-sm text-fg-muted">{t('settings.members.readOnly')}</p> : null}
      <p role="status" className={notice && !notice.ok ? 'text-sm font-medium text-danger-text' : 'text-sm text-success-fg'}>
        {notice?.text}
      </p>
      {body}
    </section>
  );
}

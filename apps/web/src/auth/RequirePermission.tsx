import type { ReactNode } from 'react';
import { can, type Permission } from '../lib/permissions';
import { ForbiddenPage } from '../pages/ForbiddenPage';
import { useCurrentWorkspace } from '../workspace';

/** Показывает страницу «Недостаточно прав», если у роли в текущем workspace нет права (только UX). */
export function RequirePermission({ permission, children }: { permission: Permission; children: ReactNode }) {
  const { membership } = useCurrentWorkspace();
  if (!can(membership?.role, permission)) return <ForbiddenPage />;
  return <>{children}</>;
}

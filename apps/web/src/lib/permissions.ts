import type { WorkspaceRole } from '../api/client';

/**
 * Подмножество каталога прав (API_DESIGN §5.1), используемое web shell.
 * Только для отображения: авторизацию всегда выполняет сервер (403 forbidden).
 */
export type Permission = 'project:create' | 'workspace:create';

const MATRIX: Record<Permission, readonly WorkspaceRole[]> = {
  'project:create': ['owner', 'admin'],
  'workspace:create': ['owner', 'admin', 'developer', 'reviewer', 'viewer'],
};

/** Неизвестная роль (расширение enum) трактуется как отсутствие права. */
export function can(role: string | null | undefined, permission: Permission): boolean {
  if (!role) return false;
  return (MATRIX[permission] as readonly string[]).includes(role);
}

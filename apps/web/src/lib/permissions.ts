import type { WorkspaceRole } from '../api/workspaces';

/**
 * Каталог прав API_DESIGN §5.1 (полный, чтобы экранам Phase 7 не нужно было править этот файл).
 * Только для отображения: авторизацию всегда выполняет сервер (403 forbidden).
 * `directive:create` для reviewer ограничен типами (§5.4), `approval:decide` — правилами §5.3:
 * точное решение принимает сервер, здесь — «может ли роль в принципе».
 */
export type Permission =
  | 'workspace:create'
  | 'member:read'
  | 'member:manage'
  | 'project:read'
  | 'project:create'
  | 'project:update'
  | 'repository:attach'
  | 'run:read'
  | 'run:create'
  | 'run:control'
  | 'chat:read'
  | 'chat:write'
  | 'directive:create'
  | 'directive:confirm'
  | 'evidence:read'
  | 'quality:read'
  | 'approval:read'
  | 'approval:decide'
  | 'provider:read'
  | 'provider:probe'
  | 'provider:settings'
  | 'documentation:read'
  | 'documentation:register'
  | 'documentation:refresh'
  | 'artifact:download'
  | 'artifact:download_raw'
  | 'audit:read';

const ALL: readonly WorkspaceRole[] = ['owner', 'admin', 'developer', 'reviewer', 'viewer'];
const ADMINS: readonly WorkspaceRole[] = ['owner', 'admin'];
const WRITERS: readonly WorkspaceRole[] = ['owner', 'admin', 'developer'];
const PARTICIPANTS: readonly WorkspaceRole[] = ['owner', 'admin', 'developer', 'reviewer'];

const MATRIX: Record<Permission, readonly WorkspaceRole[]> = {
  'workspace:create': ALL,
  'member:read': ALL,
  'member:manage': ['owner'],
  'project:read': ALL,
  'project:create': ADMINS,
  'project:update': ADMINS,
  'repository:attach': ADMINS,
  'run:read': ALL,
  'run:create': WRITERS,
  'run:control': WRITERS,
  'chat:read': ALL,
  'chat:write': PARTICIPANTS,
  'directive:create': PARTICIPANTS,
  'directive:confirm': PARTICIPANTS,
  'evidence:read': ALL,
  'quality:read': ALL,
  'approval:read': ALL,
  'approval:decide': PARTICIPANTS,
  'provider:read': ALL,
  'provider:probe': ADMINS,
  'provider:settings': ADMINS,
  'documentation:read': ALL,
  'documentation:register': ADMINS,
  'documentation:refresh': WRITERS,
  'artifact:download': ALL,
  'artifact:download_raw': ADMINS,
  'audit:read': ADMINS,
};

/** Неизвестная роль (расширение enum) трактуется как отсутствие права. */
export function can(role: string | null | undefined, permission: Permission): boolean {
  if (!role) return false;
  return (MATRIX[permission] as readonly string[]).includes(role);
}

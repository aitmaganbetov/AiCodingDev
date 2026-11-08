/** Ограничения из packages/contracts/openapi.yaml (WorkspaceCreateRequest, ProjectCreateRequest). */
export const WORKSPACE_SLUG_PATTERN = /^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$/;
export const WORKSPACE_NAME_MAX = 200;
export const PROJECT_NAME_MAX = 200;
export const PROJECT_DESCRIPTION_MAX = 4000;
export const EMAIL_MAX = 320;
export const PASSWORD_MAX = 1024;

const LAST_WORKSPACE_KEY = 'aicd.lastWorkspaceId';

/** Только пользовательское удобство: хранилище может быть недоступно. */
export function rememberWorkspace(id: string): void {
  try {
    window.localStorage.setItem(LAST_WORKSPACE_KEY, id);
  } catch {
    /* ignore */
  }
}

export function lastWorkspace(): string | null {
  try {
    return window.localStorage.getItem(LAST_WORKSPACE_KEY);
  } catch {
    return null;
  }
}

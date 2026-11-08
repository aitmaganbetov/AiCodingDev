export function get(url: string): Promise<unknown> {
  return fetch(url);
}

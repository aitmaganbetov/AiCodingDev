import type { Id } from "./ids.js";
import { z } from "zod";
export { helper } from "../util";

/** Пользователь. */
export interface User {
  id: Id;
  name: string;
}

export type Role = "admin" | "viewer";

export enum Status {
  Active,
  Blocked,
}

export const DEFAULT_ROLE: Role = "viewer";
export const schema = z.object({});

export abstract class Repository<T extends { id: Id }> {
  protected abstract table: string;

  abstract find(id: Id): Promise<T | undefined>;

  /** Сохранить. */
  async save(item: T): Promise<void> {
    await Promise.resolve(item);
  }
}

export function isAdmin(user: User, role: Role = "viewer"): boolean {
  return role === "admin" && user.name.length > 0;
}

declare function ambient(x: number): void;

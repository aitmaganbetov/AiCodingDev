import React from "react";
import { get } from "./http.js";
export * from "./types";
import { missing } from "./nope";

export function load(): Promise<unknown> {
  return get("/api");
}

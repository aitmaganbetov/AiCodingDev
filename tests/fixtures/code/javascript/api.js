import { request } from "./http";
import config from "../config.js";
const fs = require("fs");
export * from "./types";

export const API_ROOT = "/api";
let counter = 0;
const handler = async (req) => req;

/**
 * Клиент API.
 * @param base базовый URL
 */
export class ApiClient {
  constructor(base) {
    this.base = base;
  }

  async get(path) {
    return request(this.base + path);
  }

  static create() {
    return new ApiClient(API_ROOT);
  }
}

export function* ids() {
  yield counter++;
}

function loadPlugin(name) {
  return import("./plugins/" + name);
}

export default function main() {
  return import("./lazy");
}

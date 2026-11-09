#!/usr/bin/env node
/**
 * Бюджет бандла (план §4.3, F9): начальный JS (entry + modulepreload из dist/index.html) ≤ 260 kB gzip.
 * Печатает таблицу: начальные файлы, ленивые чанки (по убыванию), CSS и шрифты. Код выхода 1 — бюджет
 * превышен. Запуск после `pnpm build`: `pnpm size` (или `node scripts/bundle-size.mjs [dist]`).
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { gzipSync } from 'node:zlib';

const DIST = process.argv[2] ?? 'dist';
const BUDGET_KB = 260;

const kb = (bytes) => bytes / 1000;
const fmt = (bytes) => `${kb(bytes).toFixed(1)} kB`;
const gz = (file) => gzipSync(readFileSync(file), { level: 9 }).length;

const html = readFileSync(join(DIST, 'index.html'), 'utf8');
const refs = (re) => [...html.matchAll(re)].map((m) => m[1].replace(/^\//, ''));
const initialJs = [...new Set([...refs(/<script[^>]+type="module"[^>]+src="([^"]+)"/g), ...refs(/<link[^>]+rel="modulepreload"[^>]+href="([^"]+)"/g)])];
const initialCss = refs(/<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"/g);

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}
const files = walk(DIST).map((path) => relative(DIST, path));
const lazyJs = files.filter((f) => f.endsWith('.js') && !initialJs.includes(f));
const fonts = files.filter((f) => /\.woff2$/.test(f));

const row = (f) => ({ file: f, raw: statSync(join(DIST, f)).size, gzip: gz(join(DIST, f)) });
const initial = initialJs.map(row);
const css = initialCss.map(row);
const lazy = lazyJs.map(row).sort((a, b) => b.gzip - a.gzip);
const sum = (rows, key) => rows.reduce((acc, r) => acc + r[key], 0);

const table = (title, rows) => {
  console.log(`\n${title}`);
  console.log('| Файл | raw | gzip |\n|---|---:|---:|');
  for (const r of rows) console.log(`| ${r.file} | ${fmt(r.raw)} | ${fmt(r.gzip)} |`);
  console.log(`| **Итого** | ${fmt(sum(rows, 'raw'))} | **${fmt(sum(rows, 'gzip'))}** |`);
};

table('Начальный JS (entry + modulepreload)', initial);
table('Начальный CSS', css);
table(`Ленивые JS-чанки (${lazy.length})`, lazy);
const fontBytes = fonts.reduce((acc, f) => acc + statSync(join(DIST, f)).size, 0);
console.log(`\nШрифты woff2: ${fonts.length} файлов, ${fmt(fontBytes)} (загружаются по unicode-range)`);

const initialGzip = sum(initial, 'gzip');
const ok = kb(initialGzip) <= BUDGET_KB;
console.log(`\nБюджет начального JS: ${fmt(initialGzip)} gzip из ${BUDGET_KB} kB — ${ok ? 'OK' : 'ПРЕВЫШЕН'}`);
process.exit(ok ? 0 : 1);

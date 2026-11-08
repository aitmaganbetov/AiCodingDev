// Генерирует src/api/schema.d.ts из единственного источника контракта
// (packages/contracts/openapi.yaml, ADR-0013). С флагом --check генерирует во
// временный файл и завершается с ошибкой, если результат отличается от
// закоммиченного (drift check для CI).
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const spec = resolve(root, '../../packages/contracts/openapi.yaml');
const target = resolve(root, 'src/api/schema.d.ts');
const cli = resolve(root, 'node_modules/openapi-typescript/bin/cli.js');
const check = process.argv.includes('--check');

function generate(out) {
  const res = spawnSync(process.execPath, [cli, spec, '--output', out], {
    cwd: root,
    stdio: ['ignore', 'inherit', 'inherit'],
  });
  if (res.status !== 0) {
    console.error(`openapi-typescript failed (exit ${res.status ?? 'signal'})`);
    process.exit(res.status ?? 1);
  }
}

if (!check) {
  generate(target);
  process.exit(0);
}

const dir = mkdtempSync(join(tmpdir(), 'aicd-api-'));
try {
  const tmp = join(dir, 'schema.d.ts');
  generate(tmp);
  let committed = '';
  try {
    committed = readFileSync(target, 'utf8');
  } catch {
    console.error(`check:api: ${target} не найден — выполните pnpm gen:api`);
    process.exit(1);
  }
  if (readFileSync(tmp, 'utf8') !== committed) {
    console.error('check:api: src/api/schema.d.ts устарел относительно packages/contracts/openapi.yaml — выполните pnpm gen:api');
    process.exit(1);
  }
  console.log('check:api: src/api/schema.d.ts соответствует контракту');
} finally {
  rmSync(dir, { recursive: true, force: true });
}

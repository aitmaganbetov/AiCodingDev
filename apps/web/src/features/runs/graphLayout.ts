/**
 * Послойная раскладка DAG без внешних зависимостей: слой = длина самого длинного пути от
 * корня (задачи без зависимостей), порядок внутри слоя — порядок задач в плане.
 * Циклы (некорректный план) не зацикливают раскладку: ребро, замыкающее цикл, игнорируется.
 */
export interface LayoutInput {
  id: string;
  dependsOn: readonly string[];
}

export interface LayoutResult {
  /** Слой (колонка) каждой задачи. */
  layer: Map<string, number>;
  position: Map<string, { x: number; y: number }>;
  layers: string[][];
}

export const NODE_WIDTH = 256;
export const NODE_HEIGHT = 112;
const GAP_X = 72;
const GAP_Y = 24;

export function layoutDag(items: readonly LayoutInput[]): LayoutResult {
  const ids = new Set(items.map((i) => i.id));
  const deps = new Map(items.map((i) => [i.id, i.dependsOn.filter((d) => ids.has(d) && d !== i.id)]));
  const layer = new Map<string, number>();
  const visiting = new Set<string>();

  const depth = (id: string): number => {
    const known = layer.get(id);
    if (known !== undefined) return known;
    if (visiting.has(id)) return 0;
    visiting.add(id);
    let d = 0;
    for (const dep of deps.get(id) ?? []) if (!visiting.has(dep)) d = Math.max(d, depth(dep) + 1);
    visiting.delete(id);
    layer.set(id, d);
    return d;
  };
  for (const i of items) depth(i.id);

  const layers: string[][] = [];
  for (const i of items) {
    const l = layer.get(i.id) ?? 0;
    (layers[l] ??= []).push(i.id);
  }
  const position = new Map<string, { x: number; y: number }>();
  layers.forEach((col, x) => {
    col.forEach((id, y) => position.set(id, { x: x * (NODE_WIDTH + GAP_X), y: y * (NODE_HEIGHT + GAP_Y) }));
  });
  return { layer, position, layers: layers.map((l) => l ?? []) };
}

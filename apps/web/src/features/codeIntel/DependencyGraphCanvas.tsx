/**
 * Холст графа зависимостей (React Flow, ленивый чанк вместе со стилями — в начальный бандл не входит).
 * Узлы не перетаскиваются; масштаб/панорама/«вписать» — кнопки Controls, колесо и перетаскивание фона.
 * Ссылки «Открыть файл» внутри узлов доступны с клавиатуры; полная доступная альтернатива — вид «Список».
 * Внешние узлы (неразрешённые импорты, пакеты) — пунктирная рамка, значок и подпись «внешний».
 */
import '@xyflow/react/dist/style.css';
import '../runs/graph.css';
import { Controls, Handle, Position, ReactFlow, type AriaLabelConfig, type Edge, type Node, type NodeProps } from '@xyflow/react';
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import type { DependencyEdge } from '../../api/codeIntel';
import { Icon, cx } from '../../components/ui';
import { DEP_NODE_HEIGHT, DEP_NODE_WIDTH, nodeLabel, type LaidOutNode } from './model';

type DepNodeData = { item: LaidOutNode; href: string | null };
type DepNode = Node<DepNodeData, 'dep'>;

function splitPath(label: string): { dir: string; base: string } {
  const i = label.lastIndexOf('/');
  return i < 0 ? { dir: '', base: label } : { dir: label.slice(0, i + 1), base: label.slice(i + 1) };
}

function DepNodeView({ data }: NodeProps<DepNode>) {
  const { t } = useTranslation();
  const { item, href } = data;
  const n = item.node;
  const external = n.kind === 'external';
  const label = nodeLabel(n);
  const { dir, base } = splitPath(label);
  return (
    <div
      className={cx(
        'relative flex flex-col justify-center gap-0.5 rounded-lg border-2 bg-surface-raised px-2 py-1 text-left text-xs text-fg shadow-e1',
        item.side === 'root' ? 'border-accent' : external ? 'border-dashed border-border-strong bg-canvas-secondary' : 'border-border-strong',
      )}
      style={{ width: DEP_NODE_WIDTH, minHeight: DEP_NODE_HEIGHT - 8 }}
      data-node-id={n.id}
      data-kind={n.kind}
      data-side={item.side}
      title={label}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <span className="flex items-center gap-1 font-semibold">
        <Icon name={external ? 'externalLink' : item.side === 'root' ? 'missionControl' : 'code'} className="h-3.5 w-3.5 shrink-0" />
        <span className="truncate font-mono">{base}</span>
      </span>
      {dir ? <span className="truncate font-mono text-fg-muted">{dir}</span> : null}
      <span className="flex flex-wrap items-center gap-1 text-fg-muted">
        {item.side === 'root' ? <span className="font-semibold text-fg">{t('codeIntel.graph.root')}</span> : null}
        {external ? <span className="font-semibold text-fg">{t('codeIntel.graph.external')}</span> : null}
        {n.is_test ? <span>{t('codeIntel.graph.test')}</span> : null}
        {n.language ? <span>{n.language}</span> : null}
        {href ? (
          <Link to={href} className="link nodrag ml-auto">
            {t('codeIntel.graph.openShort')}
            <span className="sr-only">: {label}</span>
          </Link>
        ) : null}
      </span>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}

const NODE_TYPES = { dep: DepNodeView };

export interface DependencyGraphCanvasProps {
  items: readonly LaidOutNode[];
  edges: readonly DependencyEdge[];
  fileHref: (path: string) => string;
  label: string;
}

export default function DependencyGraphCanvas({ items, edges, fileHref, label }: DependencyGraphCanvasProps) {
  const { t } = useTranslation();
  const byId = useMemo(() => new Map(items.map((i) => [i.node.id, i])), [items]);
  const nodes = useMemo<DepNode[]>(
    () =>
      items.map((item) => ({
        id: item.node.id,
        type: 'dep',
        position: { x: item.x, y: item.y },
        data: { item, href: item.node.kind === 'file' && item.node.path ? fileHref(item.node.path) : null },
        draggable: false,
        selectable: false,
        focusable: false,
      })),
    [items, fileHref],
  );
  const flowEdges = useMemo<Edge[]>(
    () =>
      edges
        .filter((e) => byId.has(e.source) && byId.has(e.target))
        .map((e, i) => {
          const target = byId.get(e.target);
          const source = byId.get(e.source);
          return {
            id: `${e.source}->${e.target}:${e.edge_type}:${i}`,
            source: e.source,
            target: e.target,
            focusable: false,
            selectable: false,
            className: target?.node.kind === 'external' ? 'aicd-edge-waiting' : e.edge_type === 'tests' ? 'aicd-edge-active' : undefined,
            ariaLabel: t('codeIntel.graph.edge', {
              from: source ? nodeLabel(source.node) : e.source,
              to: target ? nodeLabel(target.node) : e.target,
              type: t(`codeIntel.edgeType.${e.edge_type}`, { defaultValue: e.edge_type }),
            }),
          } satisfies Edge;
        }),
    [edges, byId, t],
  );
  const ariaLabelConfig = useMemo<Partial<AriaLabelConfig>>(
    () => ({
      'controls.ariaLabel': t('codeIntel.graph.controls.label'),
      'controls.zoomIn.ariaLabel': t('codeIntel.graph.controls.zoomIn'),
      'controls.zoomOut.ariaLabel': t('codeIntel.graph.controls.zoomOut'),
      'controls.fitView.ariaLabel': t('codeIntel.graph.controls.fitView'),
      'controls.interactive.ariaLabel': t('codeIntel.graph.controls.interactive'),
      'minimap.ariaLabel': t('codeIntel.graph.controls.minimap'),
      'handle.ariaLabel': t('codeIntel.graph.controls.handle'),
    }),
    [t],
  );
  return (
    <div className="aicd-graph h-[60vh] min-h-[22rem] overflow-hidden rounded-lg border border-border" data-testid="dependency-graph-canvas">
      <ReactFlow<DepNode, Edge>
        aria-label={label}
        nodes={nodes}
        edges={flowEdges}
        nodeTypes={NODE_TYPES}
        nodesDraggable={false}
        nodesConnectable={false}
        nodesFocusable={false}
        edgesFocusable={false}
        elementsSelectable={false}
        disableKeyboardA11y
        ariaLabelConfig={ariaLabelConfig}
        fitView
        minZoom={0.15}
        maxZoom={1.5}
        proOptions={{ hideAttribution: false }}
      >
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

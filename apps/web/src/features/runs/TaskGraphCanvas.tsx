/**
 * Холст графа задач (@xyflow/react, ADR-0014). Загружается лениво (React.lazy) вместе со
 * стилями React Flow — в начальный бандл не попадает. Узлы не перетаскиваются и не
 * выделяются; в каждом узле — ссылка на Task Detail (клавиатура: Tab по ссылкам). Полностью
 * доступная альтернатива того же графа — вид «Список» (RunGraphPage).
 */
import '@xyflow/react/dist/style.css';
import './graph.css';
import {
  Controls,
  Handle,
  Position,
  ReactFlow,
  type AriaLabelConfig,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react';
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useNavigate } from 'react-router';
import type { TaskDependency, TaskSummary } from '../../api/runs';
import { StatusBadge } from '../../components/StatusBadge';
import { cx } from '../../components/ui';
import { layoutDag, NODE_HEIGHT, NODE_WIDTH } from './graphLayout';
import { executorLabel, roleLabel, TASK_ACTIVE } from './labels';

type TaskNodeData = { task: TaskSummary; href: string };
type TaskNode = Node<TaskNodeData, 'task'>;

function TaskNodeView({ data }: NodeProps<TaskNode>) {
  const { t } = useTranslation();
  const { task, href } = data;
  const active = TASK_ACTIVE.includes(task.status);
  return (
    <div
      className={cx(
        'flex flex-col gap-1 rounded-md border bg-surface-raised p-2 text-left text-sm text-fg shadow-sm',
        active ? 'border-accent ring-2 ring-accent/40' : 'border-border-strong',
      )}
      style={{ width: NODE_WIDTH, minHeight: NODE_HEIGHT - 16 }}
      data-task-id={task.id}
      data-status={task.status}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <Link to={href} className="link line-clamp-2 font-semibold">
        {task.title}
      </Link>
      <div className="flex flex-wrap items-center gap-1">
        <StatusBadge kind="task" status={task.status} />
        <span className="text-xs text-fg-muted">{roleLabel(t, task.agent_role)}</span>
      </div>
      <p className="text-xs text-fg-muted">
        {active
          ? t('runGraph.node.executor', { executor: executorLabel(t, task.agent_role, task.current_provider) })
          : t('runGraph.node.attempts', { count: task.attempt_count, max: task.max_attempts })}
      </p>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}

const NODE_TYPES = { task: TaskNodeView };

export interface TaskGraphCanvasProps {
  tasks: readonly TaskSummary[];
  edges: readonly TaskDependency[];
  taskHref: (taskId: string) => string;
  label: string;
}

export default function TaskGraphCanvas({ tasks, edges, taskHref, label }: TaskGraphCanvasProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const layout = useMemo(() => layoutDag(tasks.map((x) => ({ id: x.id, dependsOn: x.depends_on }))), [tasks]);
  const byId = useMemo(() => new Map(tasks.map((x) => [x.id, x])), [tasks]);

  const nodes = useMemo<TaskNode[]>(
    () =>
      tasks.map((task) => ({
        id: task.id,
        type: 'task',
        position: layout.position.get(task.id) ?? { x: 0, y: 0 },
        data: { task, href: taskHref(task.id) },
        draggable: false,
        selectable: false,
        focusable: false,
        ariaLabel: undefined,
      })),
    [tasks, layout, taskHref],
  );

  const flowEdges = useMemo<Edge[]>(
    () =>
      edges
        .filter((e) => byId.has(e.task_id) && byId.has(e.depends_on_task_id))
        .map((e) => {
          const target = byId.get(e.task_id);
          const source = byId.get(e.depends_on_task_id);
          const active = target ? TASK_ACTIVE.includes(target.status) : false;
          return {
            id: `${e.depends_on_task_id}->${e.task_id}`,
            source: e.depends_on_task_id,
            target: e.task_id,
            focusable: false,
            selectable: false,
            className: active ? 'aicd-edge-active' : undefined,
            ariaLabel: t('runGraph.edge', { from: source?.title ?? '', to: target?.title ?? '' }),
          } satisfies Edge;
        }),
    [edges, byId, t],
  );

  const ariaLabelConfig = useMemo<Partial<AriaLabelConfig>>(
    () => ({
      'controls.ariaLabel': t('runGraph.controls.label'),
      'controls.zoomIn.ariaLabel': t('runGraph.controls.zoomIn'),
      'controls.zoomOut.ariaLabel': t('runGraph.controls.zoomOut'),
      'controls.fitView.ariaLabel': t('runGraph.controls.fitView'),
      'controls.interactive.ariaLabel': t('runGraph.controls.interactive'),
      'minimap.ariaLabel': t('runGraph.controls.minimap'),
      'handle.ariaLabel': t('runGraph.controls.handle'),
    }),
    [t],
  );

  return (
    <div className="aicd-graph h-[60vh] min-h-[22rem] overflow-hidden rounded-lg border border-border" data-testid="run-graph-canvas">
      <ReactFlow<TaskNode, Edge>
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
        minZoom={0.2}
        maxZoom={1.5}
        onNodeClick={(_e, node) => void navigate(node.data.href)}
        proOptions={{ hideAttribution: false }}
      >
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

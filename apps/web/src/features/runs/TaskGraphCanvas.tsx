/**
 * Холст графа задач (@xyflow/react, ADR-0014) — Workflow Navigator AI Mission Control (F4). Загружается
 * лениво (React.lazy) вместе со стилями React Flow — в начальный бандл не попадает. Узлы не
 * перетаскиваются; клик по узлу или кнопка-заголовок (Tab по узлам) выбирает задачу для Agent Inspector,
 * ссылка «Карточка» ведёт в Task Detail. Полностью доступная альтернатива — вид «Список» навигатора.
 * Узел: роль (AgentBadge) и CLI-провайдер, статус, прогресс только по подтверждённым проверкам,
 * ошибки/предупреждения, evidence и тесты (если данные загружены — иначе не показываются),
 * ожидание зависимостей/очередь. Активный узел — рамка и мягкая пульсация (нет при reduced motion).
 */
import '@xyflow/react/dist/style.css';
import './graph.css';
import { Controls, Handle, Position, ReactFlow, type AriaLabelConfig, type Edge, type Node, type NodeProps } from '@xyflow/react';
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import type { TaskDependency, TaskSummary } from '../../api/runs';
import { StatusBadge } from '../../components/StatusBadge';
import { AgentAvatar, Icon, cx } from '../../components/ui';
import { agentRoleMeta } from '../../lib/agentRoles';
import type { TaskIndicators } from '../missionControl/feedModel';
import { layoutDag, NODE_HEIGHT, NODE_WIDTH } from './graphLayout';
import { enumLabel, providerLabel, roleLabel, TASK_ACTIVE } from './labels';

type TaskNodeData = {
  task: TaskSummary;
  href: string;
  selected: boolean;
  indicators: TaskIndicators | undefined;
  waiting: string[];
  onSelect: ((taskId: string) => void) | undefined;
};
type TaskNode = Node<TaskNodeData, 'task'>;

function TaskNodeView({ data }: NodeProps<TaskNode>) {
  const { t } = useTranslation();
  const { task, href, selected, indicators: ind, waiting, onSelect } = data;
  const active = TASK_ACTIVE.includes(task.status);
  const code = agentRoleMeta(task.agent_role)?.code ?? 'SYS';
  const provider = providerLabel(t, task.current_provider);
  const warnings: string[] = [];
  if (task.blocked_reason) warnings.push(enumLabel(t, 'blocked_reason', task.blocked_reason));
  if (ind && ind.failed > 0) warnings.push(t('missionControl.node.failedGates', { count: ind.failed }));
  if (ind && ind.blocked > 0) warnings.push(t('missionControl.node.blockedGates', { count: ind.blocked }));
  if (task.status === 'PENDING' && waiting.length > 0) warnings.push(t('missionControl.node.waiting', { count: waiting.length }));
  if (task.status === 'READY') warnings.push(t('missionControl.node.queued'));
  const percent = ind && ind.gates > 0 ? Math.round((ind.verified / ind.gates) * 100) : null;
  return (
    <div
      className={cx(
        'aicd-node relative flex flex-col gap-1.5 rounded-lg border bg-surface-raised p-2 text-left text-sm text-fg shadow-e1',
        active ? 'aicd-node-active border-accent' : task.status === 'FAILED' || task.status === 'BLOCKED' ? 'border-danger' : 'border-border-strong',
        selected && 'ring-2 ring-focus ring-offset-2 ring-offset-surface',
      )}
      style={{ width: NODE_WIDTH, minHeight: NODE_HEIGHT - 20 }}
      data-task-id={task.id}
      data-status={task.status}
      data-selected={selected ? 'true' : undefined}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <div className="flex items-start gap-2">
        <AgentAvatar agentRole={task.agent_role} size="sm" />
        <div className="min-w-0 flex-1">
          {onSelect ? (
            <button
              type="button"
              className="nodrag line-clamp-2 w-full break-words rounded-sm text-left font-semibold text-fg hover:underline"
              aria-pressed={selected}
              onClick={(e) => {
                e.stopPropagation();
                onSelect(task.id);
              }}
            >
              {task.title}
            </button>
          ) : (
            <Link to={href} className="link line-clamp-2 font-semibold">
              {task.title}
            </Link>
          )}
          <p className="text-xs text-fg-muted">
            <span className="font-mono font-semibold">{code}</span> {roleLabel(t, task.agent_role)}
            {provider ? <span> · {provider}</span> : null}
          </p>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-1">
        <StatusBadge kind="task" status={task.status} />
        <span className="text-xs text-fg-muted">{t('runGraph.node.attempts', { count: task.attempt_count, max: task.max_attempts })}</span>
      </div>
      {ind && ind.gates > 0 && percent !== null ? (
        <div className="flex flex-col gap-0.5" data-testid="node-progress">
          <span className="text-xs text-fg-muted">{t('missionControl.node.verified', { verified: ind.verified, total: ind.gates })}</span>
          <span className="block h-1.5 overflow-hidden rounded-full bg-surface-muted" aria-hidden="true">
            <span className="block h-full rounded-full bg-success" style={{ width: `${percent}%` }} />
          </span>
        </div>
      ) : null}
      {ind && (ind.evidence || ind.tests.total > 0) ? (
        <div className="flex flex-wrap gap-1 text-xs" data-testid="node-evidence">
          {ind.evidence ? (
            <span className="inline-flex items-center gap-1 rounded-full border border-border px-1.5 py-0.5 text-fg-muted">
              <Icon name="verifyPass" className="h-3 w-3" />
              {t('missionControl.node.evidence', { verified: ind.evidence.verified, total: ind.evidence.total })}
            </span>
          ) : null}
          {ind.tests.total > 0 ? (
            <span className="inline-flex items-center gap-1 rounded-full border border-border px-1.5 py-0.5 text-fg-muted">
              <Icon name="listChecks" className="h-3 w-3" />
              {t('missionControl.node.tests', { verified: ind.tests.verified, total: ind.tests.total })}
            </span>
          ) : null}
        </div>
      ) : null}
      {warnings.length > 0 ? (
        <ul className="flex flex-col gap-0.5 text-xs" data-testid="node-warnings">
          {warnings.map((w) => (
            <li key={w} className="flex items-start gap-1 text-fg">
              <Icon name="alert" className="mt-0.5 h-3 w-3 shrink-0 text-warning" />
              <span className="break-words">{w}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {onSelect ? (
        <Link to={href} className="link nodrag self-start text-xs">
          {t('missionControl.node.openTask')}
          <span className="sr-only">: {task.title}</span>
        </Link>
      ) : null}
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
  selectedId?: string | null;
  onSelect?: (taskId: string) => void;
  indicators?: ReadonlyMap<string, TaskIndicators>;
  className?: string;
}

export default function TaskGraphCanvas({ tasks, edges, taskHref, label, selectedId = null, onSelect, indicators, className }: TaskGraphCanvasProps) {
  const { t } = useTranslation();
  const layout = useMemo(() => layoutDag(tasks.map((x) => ({ id: x.id, dependsOn: x.depends_on }))), [tasks]);
  const byId = useMemo(() => new Map(tasks.map((x) => [x.id, x])), [tasks]);
  const statusById = useMemo(() => new Map(tasks.map((x) => [x.id, x.status as string])), [tasks]);

  const nodes = useMemo<TaskNode[]>(
    () =>
      tasks.map((task) => ({
        id: task.id,
        type: 'task',
        position: layout.position.get(task.id) ?? { x: 0, y: 0 },
        data: {
          task,
          href: taskHref(task.id),
          selected: task.id === selectedId,
          indicators: indicators?.get(task.id),
          waiting: task.status === 'PENDING' ? task.depends_on.filter((d) => statusById.get(d) !== 'SUCCEEDED') : [],
          onSelect,
        },
        draggable: false,
        selectable: false,
        focusable: false,
        ariaLabel: undefined,
      })),
    [tasks, layout, taskHref, selectedId, indicators, statusById, onSelect],
  );

  const flowEdges = useMemo<Edge[]>(
    () =>
      edges
        .filter((e) => byId.has(e.task_id) && byId.has(e.depends_on_task_id))
        .map((e) => {
          const target = byId.get(e.task_id);
          const source = byId.get(e.depends_on_task_id);
          const active = target ? TASK_ACTIVE.includes(target.status) : false;
          const blocked = target?.status === 'PENDING' && source?.status !== 'SUCCEEDED';
          return {
            id: `${e.depends_on_task_id}->${e.task_id}`,
            source: e.depends_on_task_id,
            target: e.task_id,
            focusable: false,
            selectable: false,
            className: active ? 'aicd-edge-active' : blocked ? 'aicd-edge-waiting' : undefined,
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
    <div className={cx('aicd-graph overflow-hidden rounded-lg border border-border', className ?? 'h-[60vh] min-h-[22rem]')} data-testid="run-graph-canvas">
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
        onNodeClick={(_e, node) => node.data.onSelect?.(node.id)}
        proOptions={{ hideAttribution: false }}
      >
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

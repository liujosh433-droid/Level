"use client";

import { useMemo } from "react";
import {
  Background,
  Controls,
  MarkerType,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
  Handle,
  Position,
} from "@xyflow/react";
import dagre from "@dagrejs/dagre";
import "@xyflow/react/dist/style.css";

import type { CareGraph, CareGraphNode } from "@/lib/api";
import styles from "./CareLoadGraph.module.css";

const FALLBACK: Record<string, string> = {
  you: "#3DB8A0",
  child: "#5B8EC9",
  elder: "#B87AA0",
  work: "#5A7A8C",
  recovery: "#6A9E78",
  logistics: "#A09060",
  helper: "#D4A05A",
  domain: "#8aa4b0",
};

type CareNodeData = {
  label: string;
  kind: string;
  color: string;
  shape: "star" | "circle";
  relationship?: string | null;
  event_count?: number;
  hint?: string | null;
};

function hexToRgba(hex: string, alpha: number): string {
  const h = hex.replace("#", "");
  if (h.length !== 6) return `rgba(138, 164, 176, ${alpha})`;
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function starPath(cx: number, cy: number, outerR: number, innerR: number): string {
  const points: string[] = [];
  for (let i = 0; i < 10; i++) {
    const r = i % 2 === 0 ? outerR : innerR;
    const angle = -Math.PI / 2 + (i * Math.PI) / 5;
    points.push(`${cx + r * Math.cos(angle)},${cy + r * Math.sin(angle)}`);
  }
  return `M ${points.join(" L ")} Z`;
}

function CarePersonNode({ data }: NodeProps<Node<CareNodeData>>) {
  const d = data;
  const color = d.color || FALLBACK.domain;
  const rel = (d.relationship || "").trim();
  const title = rel ? `${d.label} (${rel})` : d.label;

  return (
    <div className={styles.rfNode} title={title}>
      <Handle type="target" position={Position.Top} className={styles.handle} />
      <svg width={84} height={84} viewBox="0 0 84 84" aria-hidden>
        {d.shape === "star" ? (
          <path
            d={starPath(42, 42, 34, 14)}
            fill={hexToRgba(color, 0.32)}
            stroke={color}
            strokeWidth={2.5}
          />
        ) : (
          <circle
            cx={42}
            cy={42}
            r={32}
            fill={hexToRgba(color, 0.28)}
            stroke={color}
            strokeWidth={2.5}
          />
        )}
      </svg>
      <div className={styles.rfLabel}>
        <span className={styles.rfName}>{d.label}</span>
        {rel ? <span className={styles.rfRel}>{rel}</span> : null}
      </div>
      <Handle type="source" position={Position.Bottom} className={styles.handle} />
    </div>
  );
}

const nodeTypes = { carePerson: CarePersonNode };

function toFlowNode(n: CareGraphNode, shape: "star" | "circle"): Node {
  return {
    id: n.id,
    type: "carePerson",
    position: { x: 0, y: 0 },
    data: {
      label: n.label,
      kind: n.kind,
      color: n.color || FALLBACK[n.kind] || FALLBACK.domain,
      shape,
      relationship: n.relationship,
      event_count: n.event_count,
      hint: n.hint,
    } satisfies CareNodeData,
    sourcePosition: Position.Bottom,
    targetPosition: Position.Top,
  };
}

function layoutWithDagre(nodes: Node[], edges: Edge[]): Node[] {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({
    rankdir: "TB",
    nodesep: 40,
    ranksep: 56,
    marginx: 8,
    marginy: 4,
  });

  const width = 140;
  const height = 128;
  for (const n of nodes) {
    g.setNode(n.id, { width, height });
  }
  for (const e of edges) {
    g.setEdge(e.source, e.target);
  }
  dagre.layout(g);

  return nodes.map((n) => {
    const pos = g.node(n.id);
    return {
      ...n,
      position: {
        x: (pos?.x ?? 0) - width / 2,
        y: (pos?.y ?? 0) - height / 2,
      },
    };
  });
}

function buildFlowElements(graph: CareGraph): { nodes: Node[]; edges: Edge[] } {
  const roots =
    graph.roots && graph.roots.length > 0 ? graph.roots : [graph.center];
  const rootIds = new Set(roots.map((r) => r.id));
  // Dependents only in nodes; older payloads may still nest stars in nodes.
  const satellites = graph.nodes.filter((n) => !rootIds.has(n.id));

  const nodes: Node[] = [
    ...roots.map((r) =>
      toFlowNode(r, (r.shape || "star").toLowerCase() === "circle" ? "circle" : "star"),
    ),
    ...satellites.map((n) =>
      toFlowNode(n, (n.shape || "circle").toLowerCase() === "star" ? "star" : "circle"),
    ),
  ];

  const edges: Edge[] = graph.edges.map((e, i) => {
    const dashed = e.relation === "can_help";
    const color = e.color || FALLBACK.domain;
    return {
      id: `${e.from_id}-${e.to_id}-${e.relation}-${i}`,
      source: e.from_id,
      target: e.to_id,
      label: e.relation === "can_help" ? "helps" : undefined,
      style: {
        stroke: color,
        strokeWidth: dashed ? 1.75 : 2.25,
        strokeDasharray: dashed ? "6 4" : undefined,
      },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color,
        width: 16,
        height: 16,
      },
      animated: false,
    };
  });

  return { nodes: layoutWithDagre(nodes, edges), edges };
}

export function CareLoadGraph({
  graph,
  building = false,
}: {
  graph: CareGraph | null | undefined;
  building?: boolean;
}) {
  const empty =
    !graph ||
    ((!graph.roots || graph.roots.length === 0) &&
      graph.nodes.length === 0 &&
      !graph.center);

  const { nodes, edges } = useMemo(() => {
    if (!graph || empty) return { nodes: [] as Node[], edges: [] as Edge[] };
    // Need at least center or some nodes
    if (!graph.center && graph.nodes.length === 0) {
      return { nodes: [] as Node[], edges: [] as Edge[] };
    }
    return buildFlowElements(graph);
  }, [graph, empty]);

  if (!graph || nodes.length === 0) {
    return (
      <section className={styles.wrap} aria-label="Care responsibilities">
        <h2 className={styles.title}>Care load</h2>
        <p className={styles.empty}>
          {building
            ? "Reading your calendar to map who you care for — this usually takes a few seconds. Refresh if it stays empty."
            : "Connect calendar or tell Level who you care for — names appear when titles, past chats, or a note give them away."}
        </p>
      </section>
    );
  }

  const categories = graph.categories ?? [];
  const helperHints = (graph.roots ?? [])
    .concat(graph.nodes)
    .filter((n) => n.kind === "helper" && n.hint);

  return (
    <section className={styles.wrap} aria-label="Care responsibilities">
      <h2 className={styles.title}>Care load</h2>
      <p className={styles.lead}>
        Each star is a caregiver. Circles are people or loads in their care — Level
        labels relationships from your calendar and notes.
      </p>

      <div className={styles.flowShell}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          fitView
          fitViewOptions={{ padding: 0.14, maxZoom: 1.25, minZoom: 0.65 }}
          nodesDraggable
          nodesConnectable={false}
          elementsSelectable={false}
          panOnDrag
          zoomOnScroll={false}
          preventScrolling={false}
          proOptions={{ hideAttribution: true }}
          minZoom={0.6}
          maxZoom={1.4}
          defaultEdgeOptions={{
            style: { stroke: "rgba(168, 188, 198, 0.55)", strokeWidth: 2 },
          }}
        >
          <Background gap={20} size={0.7} color="rgba(138, 164, 176, 0.1)" />
          <Controls showInteractive={false} position="bottom-right" />
        </ReactFlow>
      </div>

      <p className={styles.shapeKey} aria-hidden="true">
        <span className={styles.shapeStar}>★</span> caregiver root{" "}
        <span className={styles.shapeCircle}>●</span> dependent / load
      </p>

      {categories.length > 0 ? (
        <ul className={styles.legend} aria-label="Care role colors from calendar">
          {categories.map((c) => (
            <li key={c.role_id}>
              <span className={styles.swatch} style={{ background: c.color }} />
              <span>
                {c.label}
                {c.event_count > 0 ? (
                  <span className={styles.count}> · {c.event_count}</span>
                ) : null}
              </span>
            </li>
          ))}
        </ul>
      ) : null}

      {helperHints.length > 0 ? (
        <ul className={styles.hints}>
          {helperHints.map((h) => (
            <li key={h.id}>
              <strong>{h.label}</strong>
              {h.hint ? ` — ${h.hint}` : ""}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

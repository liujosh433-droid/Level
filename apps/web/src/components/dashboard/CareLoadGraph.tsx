"use client";

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

function polar(cx: number, cy: number, r: number, angleRad: number) {
  return {
    x: cx + r * Math.cos(angleRad),
    y: cy + r * Math.sin(angleRad),
  };
}

function nodeColor(n: CareGraphNode): string {
  return n.color || FALLBACK[n.kind] || FALLBACK.domain;
}

function hexToRgba(hex: string, alpha: number): string {
  const h = hex.replace("#", "");
  if (h.length !== 6) return `rgba(138, 164, 176, ${alpha})`;
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/** Shorten a directed segment so arrowheads sit on the node rim, not the center. */
function edgeEndpoints(
  ax: number,
  ay: number,
  bx: number,
  by: number,
  startPad: number,
  endPad: number,
): { x1: number; y1: number; x2: number; y2: number } {
  const dx = bx - ax;
  const dy = by - ay;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;
  return {
    x1: ax + ux * startPad,
    y1: ay + uy * startPad,
    x2: bx - ux * endPad,
    y2: by - uy * endPad,
  };
}

function posFor(
  id: string,
  centerId: string,
  nodes: CareGraphNode[],
  cx: number,
  cy: number,
  radius: number,
): { x: number; y: number } {
  if (id === centerId) return { x: cx, y: cy };
  const idx = nodes.findIndex((n) => n.id === id);
  if (idx < 0) return { x: cx, y: cy };
  const n = Math.max(nodes.length, 1);
  const angle = -Math.PI / 2 + (idx / n) * 2 * Math.PI;
  return polar(cx, cy, radius, angle);
}

export function CareLoadGraph({
  graph,
}: {
  graph: CareGraph | null | undefined;
}) {
  if (!graph || graph.nodes.length === 0) {
    return (
      <section className={styles.wrap} aria-label="Care responsibilities">
        <h2 className={styles.title}>Care load</h2>
        <p className={styles.empty}>
          Connect calendar or tell Level who you care for — names appear when titles,
          past chats, or a note give them away.
        </p>
      </section>
    );
  }

  const w = 420;
  const h = 380;
  const cx = w / 2;
  const cy = h / 2 - 6;
  const radius = 128;
  const centerR = 34;
  const nodeR = 30;
  const center = graph.center;
  const nodes = graph.nodes;
  const categories = graph.categories ?? [];
  const helpers = nodes.filter((n) => n.kind === "helper" && n.hint);

  // Unique marker ids per edge color so arrowheads match the stroke.
  const markerColors = Array.from(
    new Set(graph.edges.map((e) => e.color || FALLBACK.domain)),
  );

  return (
    <section className={styles.wrap} aria-label="Care responsibilities">
      <h2 className={styles.title}>Care load</h2>
      <p className={styles.lead}>
        Calendar events grouped into care roles — arrows show what you’re holding and who
        can share the load.
      </p>
      <svg
        className={styles.svg}
        viewBox={`0 0 ${w} ${h}`}
        role="img"
        aria-label="Directed responsibilities graph colored by care role"
      >
        <defs>
          {markerColors.map((color) => {
            const id = `arrow-${color.replace("#", "")}`;
            return (
              <marker
                key={id}
                id={id}
                viewBox="0 0 12 12"
                refX="10"
                refY="6"
                markerWidth="8"
                markerHeight="8"
                orient="auto"
                markerUnits="userSpaceOnUse"
              >
                <path d="M 0 1 L 10 6 L 0 11 z" fill={color} />
              </marker>
            );
          })}
        </defs>

        {graph.edges.map((e) => {
          const a = posFor(e.from_id, center.id, nodes, cx, cy, radius);
          const b = posFor(e.to_id, center.id, nodes, cx, cy, radius);
          const startPad = e.from_id === center.id ? centerR : nodeR;
          const endPad = e.to_id === center.id ? centerR : nodeR;
          const { x1, y1, x2, y2 } = edgeEndpoints(a.x, a.y, b.x, b.y, startPad, endPad);
          const color = e.color || FALLBACK.domain;
          const markerId = `arrow-${color.replace("#", "")}`;
          const dashed = e.relation === "can_help";
          return (
            <line
              key={`${e.from_id}-${e.to_id}-${e.relation}`}
              x1={x1}
              y1={y1}
              x2={x2}
              y2={y2}
              stroke={color}
              strokeWidth={dashed ? 2 : 2.4}
              strokeOpacity={0.9}
              strokeDasharray={dashed ? "6 5" : undefined}
              markerEnd={`url(#${markerId})`}
            />
          );
        })}

        <g>
          <circle
            cx={cx}
            cy={cy}
            r={centerR}
            fill={hexToRgba(center.color || FALLBACK.you, 0.35)}
            stroke={center.color || FALLBACK.you}
            strokeWidth={2.5}
          />
          <text x={cx} y={cy + 5} textAnchor="middle" className={styles.nodeLabel}>
            {center.label}
          </text>
        </g>

        {nodes.map((n, idx) => {
          const angle = -Math.PI / 2 + (idx / Math.max(nodes.length, 1)) * 2 * Math.PI;
          const { x, y } = polar(cx, cy, radius, angle);
          const color = nodeColor(n);
          const label =
            n.label.length > 12 ? `${n.label.slice(0, 11)}…` : n.label;
          return (
            <g key={n.id}>
              <circle
                cx={x}
                cy={y}
                r={nodeR}
                fill={hexToRgba(color, 0.28)}
                stroke={color}
                strokeWidth={2.25}
              />
              <text x={x} y={y + (n.event_count ? 0 : 5)} textAnchor="middle" className={styles.nodeLabel}>
                {label}
              </text>
              {n.event_count ? (
                <text x={x} y={y + 14} textAnchor="middle" className={styles.nodeMeta}>
                  {n.event_count} evt{n.event_count === 1 ? "" : "s"}
                </text>
              ) : null}
            </g>
          );
        })}
      </svg>

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

      {helpers.length > 0 ? (
        <ul className={styles.hints}>
          {helpers.map((h) => (
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

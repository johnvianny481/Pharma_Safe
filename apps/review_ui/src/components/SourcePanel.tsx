/**
 * The source document next to the form, with every evidence span highlighted.
 *
 * Offsets index into the document text (as stored at intake), so a reviewer sees exactly
 * which words each AI value came from. The active span (the field being looked at) is
 * marked differently and scrolled into view.
 */
import { useEffect, useRef } from "react";

import type { SourceText } from "../api";

export interface Highlight {
  start: number;
  end: number;
  path: string;
}

interface Segment {
  start: number;
  end: number;
  paths: string[];
}

/** Split [from, to) into runs covered by the same set of highlights. */
export function segments(from: number, to: number, highlights: Highlight[]): Segment[] {
  const inside = highlights.filter((h) => h.end > from && h.start < to);
  const cuts = new Set([from, to]);
  for (const h of inside) {
    cuts.add(Math.max(h.start, from));
    cuts.add(Math.min(h.end, to));
  }
  const points = [...cuts].sort((a, b) => a - b);
  const out: Segment[] = [];
  for (let i = 0; i < points.length - 1; i++) {
    const start = points[i] as number;
    const end = points[i + 1] as number;
    const paths = inside.filter((h) => h.start <= start && h.end >= end).map((h) => h.path);
    out.push({ start, end, paths });
  }
  return out;
}

export function SourcePanel(props: {
  source: SourceText | null | undefined;
  highlights: Highlight[];
  active: string | null;
}) {
  const activeRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    activeRef.current?.scrollIntoView?.({ block: "center", behavior: "smooth" });
  }, [props.active]);

  if (props.source === undefined) return <p role="status">Loading the source document…</p>;
  if (props.source === null) {
    return <p>This case has no source text (entered by hand, or an unreadable scan).</p>;
  }
  const { text, report_start, report_end, filename, language } = props.source;
  let activeSeen = false;
  return (
    <section aria-labelledby="source-heading" className="source">
      <h2 id="source-heading">Source document</h2>
      <p className="hint">
        {filename ?? "Submitted text"} · language {language}. Highlighted words are the
        evidence for AI-proposed values.
      </p>
      <div className="source-text" tabIndex={0} aria-label="Source document text">
        {segments(report_start, report_end, props.highlights).map((seg) => {
          const chunk = text.slice(seg.start, seg.end);
          if (seg.paths.length === 0) return <span key={seg.start}>{chunk}</span>;
          const isActive = props.active !== null && seg.paths.includes(props.active);
          const first = isActive && !activeSeen;
          if (first) activeSeen = true;
          return (
            <mark
              key={seg.start}
              className={isActive ? "evidence evidence-active" : "evidence"}
              title={`Evidence for ${seg.paths.join(", ")}`}
              ref={first ? (el) => { activeRef.current = el; } : undefined}
            >
              {chunk}
            </mark>
          );
        })}
      </div>
    </section>
  );
} // prettier-ignore

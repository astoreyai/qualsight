import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type ComparisonRow, type MatcherErrors, type MatcherLeaf, type MatrixStatus } from "./api";

const POLICY = "EXCLUDE_FAILED_TRIALS";
const CONDITION_SHORT: Record<string, string> = {
  SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1: "shared crop (adaptive · largest · JPEG95)",
  SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1: "native crop (640 · highest · in-memory)",
};
const MATCHER_SHORT: Record<string, string> = {
  "adaface-ir101-local-candidate": "AdaFace IR-101",
  "elasticface-arc-r100-local-candidate": "ElasticFace-Arc R100",
  "transface-s-glint360k-local-candidate": "TransFace-S",
};

function shortHash(value: string, keep = 12): string {
  const marker = value.indexOf("sha256:");
  const digest = marker >= 0 ? value.slice(marker + 7) : value;
  return `${digest.slice(0, keep)}…`;
}
function pct(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)} %`;
}
function fileName(path: string): string {
  const parts = path.split("/");
  return parts[parts.length - 1] ?? path;
}

interface MatchersProps {
  projectId: string | null;
  datasetId: string | null;
  onMessage: (message: string) => void;
}

interface DetPoint { fmr: number; fnmr: number; threshold: number }

/** Verification workbench: matrix execution, six-leaf metrics table, DET overlay, error drilldown (QS-C22..C24). */
export function Matchers({ projectId, datasetId, onMessage }: MatchersProps) {
  const [protocolId, setProtocolId] = useState<string | null>(null);
  const [protocolSummary, setProtocolSummary] = useState<string>("");
  const [leaves, setLeaves] = useState<MatcherLeaf[]>([]);
  const [matrix, setMatrix] = useState<MatrixStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [detCurves, setDetCurves] = useState<Record<string, DetPoint[]>>({});
  const [selectedLeaf, setSelectedLeaf] = useState<MatcherLeaf | null>(null);
  const [errors, setErrors] = useState<MatcherErrors | null>(null);
  const [thresholdInput, setThresholdInput] = useState<string>("");

  const refresh = useCallback(() => {
    if (!projectId) {
      setLeaves([]);
      setProtocolId(null);
      return;
    }
    api.protocols(projectId).then((items) => {
      const first = items[0];
      setProtocolId(first ? first.protocol_id : null);
      setProtocolSummary(first ? `${first.name} · ${first.pair_count.toLocaleString()} pairs (${first.mated_count.toLocaleString()} mated)` : "");
    }).catch(() => setProtocolId(null));
    api.matcherLeaves(projectId).then(setLeaves).catch(() => setLeaves([]));
  }, [projectId]);

  useEffect(() => {
    refresh();
    setMatrix(null);
    setSelectedLeaf(null);
    setErrors(null);
    setDetCurves({});
  }, [refresh]);

  useEffect(() => {
    if (!matrix || ["completed", "failed"].includes(matrix.progress.phase)) return;
    const handle = window.setInterval(() => {
      api.matrix(matrix.matrix_id).then((status) => {
        setMatrix(status);
        if (["completed", "failed"].includes(status.progress.phase)) refresh();
      }).catch(() => undefined);
    }, 2000);
    return () => window.clearInterval(handle);
  }, [matrix, refresh]);

  // Load DET curves for the currently listed leaves (latest matrix only).
  const latestMatrixId = useMemo(() => (leaves.length ? leaves[leaves.length - 1]!.matrix_id : null), [leaves]);
  const shownLeaves = useMemo(() => leaves.filter((leaf) => leaf.matrix_id === latestMatrixId), [leaves, latestMatrixId]);
  useEffect(() => {
    let cancelled = false;
    Promise.all(
      shownLeaves.map((leaf) => api.matcherMetrics(leaf.run_id).then((body) => [leaf.leaf_id, body.metrics.by_policy[POLICY].det.points as DetPoint[]] as const)),
    )
      .then((entries) => {
        if (!cancelled) setDetCurves(Object.fromEntries(entries));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [shownLeaves]);

  const freeze = useCallback(async () => {
    if (!projectId || !datasetId) return;
    setBusy(true);
    try {
      const frozen = await api.freezeProtocol(projectId, datasetId);
      onMessage(`Frozen protocol ${shortHash(frozen.protocol_id)} with ${frozen.pair_count} pairs`);
      refresh();
    } catch (error) {
      onMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }, [projectId, datasetId, onMessage, refresh]);

  const startMatrix = useCallback(
    async (fixtureOnly: boolean) => {
      if (!projectId || !protocolId) return;
      setBusy(true);
      try {
        const started = await api.startMatrix(projectId, protocolId, fixtureOnly);
        onMessage(`Matrix ${shortHash(started.matrix_id)} started: ${started.pairs} pairs · ${started.samples} samples`);
        setMatrix({ matrix_id: started.matrix_id, progress: { phase: "queued", detail: "" }, leaves: [] });
      } catch (error) {
        onMessage(error instanceof Error ? error.message : String(error));
      } finally {
        setBusy(false);
      }
    },
    [projectId, protocolId, onMessage],
  );

  const showErrors = useCallback(
    async (leaf: MatcherLeaf, threshold?: number) => {
      setSelectedLeaf(leaf);
      try {
        setErrors(await api.matcherErrors(leaf.run_id, threshold));
      } catch (error) {
        onMessage(error instanceof Error ? error.message : String(error));
      }
    },
    [onMessage],
  );

  const grouped = useMemo(() => {
    const byMatcher: Record<string, Record<string, MatcherLeaf>> = {};
    for (const leaf of shownLeaves) {
      byMatcher[leaf.matcher_id] = byMatcher[leaf.matcher_id] ?? {};
      byMatcher[leaf.matcher_id]![leaf.condition_id] = leaf;
    }
    return byMatcher;
  }, [shownLeaves]);
  const conditions = Object.keys(CONDITION_SHORT);
  const colors = ["#38bdf8", "#0ea5e9", "#34d399", "#10b981", "#f59e0b", "#d97706"];

  if (!projectId) {
    return (
      <section className="panel matchers" aria-labelledby="matchers-heading" data-testid="matchers">
        <h2 id="matchers-heading">Verification · matcher × preprocessing matrix</h2>
        <p className="empty">Select a project with an imported LFW dataset.</p>
      </section>
    );
  }

  return (
    <section className="panel matchers" aria-labelledby="matchers-heading" data-testid="matchers">
      <h2 id="matchers-heading">Verification · matcher × preprocessing matrix</h2>
      <div className="controls-row">
        {protocolId ? (
          <span className="hint" data-testid="protocol-summary">{protocolSummary}</span>
        ) : (
          <button type="button" className="secondary" disabled={!datasetId || busy} onClick={() => void freeze()} data-testid="freeze-protocol">
            Freeze LFW View 2 protocol
          </button>
        )}
        <button type="button" className="secondary" disabled={!protocolId || busy || (matrix !== null && !["completed", "failed"].includes(matrix.progress.phase))} onClick={() => void startMatrix(true)} data-testid="matrix-fixture">
          Run matrix (fixture pairs)
        </button>
        <button type="button" className="primary" disabled={!protocolId || busy || (matrix !== null && !["completed", "failed"].includes(matrix.progress.phase))} onClick={() => void startMatrix(false)} data-testid="matrix-full">
          Run matrix (all 6,000 pairs)
        </button>
      </div>
      {matrix && (
        <p className="mono" data-testid="matrix-progress">
          matrix {shortHash(matrix.matrix_id)} · {matrix.progress.phase} {matrix.progress.detail} · {matrix.leaves.length}/6 leaves
        </p>
      )}
      {shownLeaves.length > 0 && (
        <div className="matrix-grid">
          <table className="matrix-table" data-testid="matrix-table">
            <thead>
              <tr>
                <th scope="col">Matcher</th>
                {conditions.map((condition) => (
                  <th key={condition} scope="col">{CONDITION_SHORT[condition]}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {Object.entries(grouped).map(([matcherId, byCondition]) => (
                <tr key={matcherId}>
                  <th scope="row">{MATCHER_SHORT[matcherId] ?? matcherId}</th>
                  {conditions.map((condition) => {
                    const leaf = byCondition[condition];
                    if (!leaf) return <td key={condition}>—</td>;
                    const point = leaf.operating_points[POLICY]?.find((p) => p.target_fmr === 0.001);
                    return (
                      <td key={condition}>
                        <button type="button" className={selectedLeaf?.run_id === leaf.run_id ? "cell selected" : "cell"} onClick={() => void showErrors(leaf)} data-testid={`leaf-${leaf.leaf_id}`}>
                          <span className="mono">{leaf.leaf_id}</span>
                          <span>EER {pct(leaf.eer[POLICY]?.eer)}</span>
                          <span>FNMR@FMR≤0.1 % {pct(point?.fnmr ?? null)}</span>
                          <span>10-fold acc {pct(leaf.view2_tenfold[POLICY]?.mean_accuracy)}</span>
                          <span className="sample-meta">
                            {leaf.scored_count}/{leaf.comparison_count} scored · {leaf.failed_count} failed
                          </span>
                        </button>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          <div>
            <svg className="det" viewBox="0 0 320 240" role="img" aria-label="DET curves" data-testid="det-plot">
              <rect x="40" y="10" width="270" height="200" className="det-bg" />
              {[0.001, 0.01, 0.1, 1].map((v) => (
                <g key={v}>
                  <line x1={40 + Math.log10(v * 1000) * 90} y1="10" x2={40 + Math.log10(v * 1000) * 90} y2="210" className="grid" />
                  <text x={40 + Math.log10(v * 1000) * 90} y="225" textAnchor="middle" className="axis">{v}</text>
                </g>
              ))}
              {[0.001, 0.01, 0.1, 1].map((v) => (
                <g key={`y${v}`}>
                  <line x1="40" y1={210 - Math.log10(v * 1000) * 66.6} x2="310" y2={210 - Math.log10(v * 1000) * 66.6} className="grid" />
                  <text x="36" y={213 - Math.log10(v * 1000) * 66.6} textAnchor="end" className="axis">{v}</text>
                </g>
              ))}
              {shownLeaves.map((leaf, index) => {
                const points = (detCurves[leaf.leaf_id] ?? []).filter((p) => p.fmr > 0 && p.fnmr > 0);
                const path = points
                  .map((p) => `${40 + Math.max(0, Math.log10(Math.max(p.fmr, 0.001) * 1000)) * 90},${210 - Math.max(0, Math.log10(Math.max(p.fnmr, 0.001) * 1000)) * 66.6}`)
                  .join(" ");
                return <polyline key={leaf.leaf_id} points={path} fill="none" stroke={colors[index % colors.length]} strokeWidth="1.5" strokeDasharray={leaf.condition_id.includes("INMEMORY") ? "4 2" : undefined} />;
              })}
              <text x="175" y="238" textAnchor="middle" className="axis">FMR (log)</text>
              <text x="12" y="110" textAnchor="middle" transform="rotate(-90 12 110)" className="axis">FNMR (log)</text>
            </svg>
            <ul className="legend">
              {shownLeaves.map((leaf, index) => (
                <li key={leaf.leaf_id}>
                  <span className="swatch" style={{ background: colors[index % colors.length] }} /> {leaf.leaf_id} · {MATCHER_SHORT[leaf.matcher_id] ?? leaf.matcher_id} · {leaf.condition_id.includes("INMEMORY") ? "native" : "shared"}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
      {selectedLeaf && errors && (
        <div className="errors" data-testid="matcher-errors">
          <h3>
            {selectedLeaf.leaf_id} errors at threshold {errors.threshold.toFixed(4)} ({errors.policy}; {errors.threshold_equality_rule}) · condition {errors.condition_id} · transform {errors.transform_id} · run{" "}
            <a href={`/api/runs/${errors.run_id}`} className="mono">{shortHash(errors.run_id, 14)}</a>
          </h3>
          <div className="controls-row">
            <input type="number" step="0.01" min="-1" max="1" placeholder="threshold" value={thresholdInput} onChange={(event) => setThresholdInput(event.target.value)} aria-label="threshold" />
            <button type="button" className="secondary" onClick={() => void showErrors(selectedLeaf, thresholdInput ? Number(thresholdInput) : undefined)}>Recompute</button>
            <span className="hint">
              {errors.counts.false_matches} false matches · {errors.counts.false_non_matches} false non-matches · {errors.counts.processing_failures} processing failures
            </span>
          </div>
          <ErrorList title="False matches (non-mated accepted)" rows={errors.false_matches} />
          <ErrorList title="False non-matches (mated rejected)" rows={errors.false_non_matches} />
          <ErrorList title="Processing failures (no score)" rows={errors.processing_failures} />
        </div>
      )}
    </section>
  );
}

function ErrorList({ title, rows }: { title: string; rows: ComparisonRow[] }) {
  if (rows.length === 0) return <p className="hint">{title}: none</p>;
  return (
    <div className="error-list">
      <h4>{title} ({rows.length})</h4>
      <ul>
        {rows.slice(0, 24).map((row) => (
          <li key={row.pair_id} className="pair" data-testid="error-pair">
            {row.links.left_image && <img src={row.links.left_image} alt={`left ${row.left_subject_id}`} />}
            {row.links.right_image && <img src={row.links.right_image} alt={`right ${row.right_subject_id}`} />}
            <div className="pair-facts">
              <span className="mono">{row.pair_id} · fold {row.fold} · {row.label}</span>
              <span>{row.left_subject_id} vs {row.right_subject_id}</span>
              <span className="mono">{row.score === null ? `${row.failure} (${row.failure_side})` : `score ${row.score.toFixed(4)}`}</span>
              <span className="links">
                <a href={row.links.left_sample}>{fileName(row.links.left_image ?? row.left_sample_id)}</a>
                <a href={row.links.right_sample}>{fileName(row.links.right_image ?? row.right_sample_id)}</a>
              </span>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

import { useEffect, useState } from "react";

/** QS-Y86/Y87/Z91/Z92: recorded match evidence for one compared pair.
 *
 * Shows the originals, the exact aligned 112x112 crops that were compared
 * (recomputed under the recorded frozen condition), the pair's score placed on
 * the Run's own genuine/impostor distributions with recorded thresholds and
 * their kinds, per-side recorded quality, and the association-only quality
 * attribution. The honesty statement travels with the panel.
 */

interface EvidenceSide {
  sample_id: string;
  subject_id: string | null;
  path: string | null;
  sha256: string | null;
  face_count: number | null;
  quality: {
    job_id: string;
    unified_quality_score: number | null;
    failed_measures: string[];
  } | null;
  links?: { image: string; crop: string; detection: string };
}

interface EvidenceOut {
  run_id: string;
  leaf_id: string;
  matcher_id: string;
  condition_id: string;
  provenance_marker: string;
  pair: { pair_id: string; label: string; fold: number | null; score: number | null; failure: string | null; failure_side: string | null };
  sides: { left: EvidenceSide; right: EvidenceSide };
  thresholds: Array<{ kind: string; name: string; threshold: number; policy: string }>;
  decision_rule: string;
  decisions: Array<{ name: string; threshold: number; decision: string }>;
  distributions: {
    bins: number;
    range: [number, number];
    genuine: { counts: number[]; edges: number[] };
    impostor: { counts: number[]; edges: number[] };
    genuine_count: number;
    impostor_count: number;
  };
  quality_attribution: {
    available: boolean;
    reason?: string;
    wording?: string;
    sides?: Record<string, Array<{ measure: string; scalar: number; population_median: number; below_median_by: number }>>;
  };
  statement: string;
}

function ScoreDistribution({ evidence }: { evidence: EvidenceOut }) {
  const { distributions, pair, thresholds } = evidence;
  const width = 460;
  const height = 120;
  const [lo, hi] = distributions.range;
  const span = hi - lo || 1;
  const maxCount = Math.max(1, ...distributions.genuine.counts, ...distributions.impostor.counts);
  const barWidth = width / distributions.bins;
  const x = (score: number) => ((score - lo) / span) * width;
  return (
    <svg
      viewBox={`0 0 ${width} ${height + 18}`}
      className="score-distribution"
      role="img"
      aria-label={`Pair score ${pair.score ?? "none"} on the Run's genuine and impostor score distributions`}
      data-testid="score-distribution"
    >
      {distributions.impostor.counts.map((count, index) => (
        <rect key={`i${index}`} x={index * barWidth} y={height - (count / maxCount) * height} width={barWidth} height={(count / maxCount) * height} className="impostor-bar" />
      ))}
      {distributions.genuine.counts.map((count, index) => (
        <rect key={`g${index}`} x={index * barWidth} y={height - (count / maxCount) * height} width={barWidth} height={(count / maxCount) * height} className="genuine-bar" />
      ))}
      {thresholds.map((item) => (
        <g key={item.name}>
          <line x1={x(item.threshold)} x2={x(item.threshold)} y1={0} y2={height} className="threshold-line" />
          <text x={Math.min(x(item.threshold) + 2, width - 60)} y={10} className="threshold-label">{item.name}</text>
        </g>
      ))}
      {pair.score !== null && (
        <g data-testid="pair-score-marker">
          <line x1={x(pair.score)} x2={x(pair.score)} y1={0} y2={height} className="pair-score-line" />
          <text x={Math.max(2, Math.min(x(pair.score) - 30, width - 80))} y={height + 14} className="pair-score-label">
            this pair {pair.score.toFixed(4)}
          </text>
        </g>
      )}
    </svg>
  );
}

export function PairEvidence({ projectId, runId, pairId, onClose }: { projectId: string; runId: string; pairId: string; onClose: () => void }) {
  const [evidence, setEvidence] = useState<EvidenceOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setEvidence(null);
    setError(null);
    fetch(`/api/projects/${encodeURIComponent(projectId)}/matcher-runs/${encodeURIComponent(runId)}/pairs/${encodeURIComponent(pairId)}/evidence`)
      .then(async (r) => {
        if (!r.ok) throw new Error(`${r.status}: ${(await r.json()).detail ?? r.statusText}`);
        return (await r.json()) as EvidenceOut;
      })
      .then(setEvidence)
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [projectId, runId, pairId]);

  if (error) return <p className="error" role="alert">{error}</p>;
  if (!evidence) return <p className="status" role="status">Loading recorded evidence…</p>;
  const externallyComputed = evidence.condition_id === "EXTERNALLY_COMPUTED";
  const publisherPrealigned = evidence.condition_id === "AUTHOR_PREALIGNED_ARCFACE112_INMEMORY_V1";
  return (
    <div className="pair-evidence" data-testid="pair-evidence">
      <div className="pair-evidence-head">
        <h4>
          Match evidence · {evidence.pair.pair_id} · {evidence.pair.label} · {evidence.leaf_id}{" "}
          <span className="mono">[{evidence.provenance_marker}]</span>
        </h4>
        <button type="button" className="secondary" onClick={onClose}>close</button>
      </div>
      <div className="pair-evidence-sides">
        {(["left", "right"] as const).map((sideName) => {
          const side = evidence.sides[sideName];
          return (
            <div key={sideName} className="pair-evidence-side" data-testid={`evidence-${sideName}`}>
              <h5>{sideName} · {side.subject_id ?? "subject identity unavailable"}</h5>
              {side.links && <img className="original" src={side.links.image} alt={`${sideName} recorded biometric sample ${side.sample_id}; ${side.subject_id ? `subject ${side.subject_id}` : "subject identity unavailable"}`} />}
              {side.links && !externallyComputed && (
                <figure className="crop">
                  <img src={side.links.crop} alt={`${sideName} aligned 112x112 crop as compared`} data-testid={`evidence-crop-${sideName}`} />
                  <figcaption>{publisherPrealigned ? "compared publisher-prealigned 112×112 image (hash-verified pass-through)" : `compared 112×112 crop (recomputed under ${evidence.condition_id})`}</figcaption>
                </figure>
              )}
              {externallyComputed && (
                <p className="hint">Externally computed Run: QualSight did not produce the compared templates, so no crop exists to show.</p>
              )}
              {side.quality ? (
                <p className="mono">
                  UQS {side.quality.unified_quality_score ?? "failed"} · failed measures{" "}
                  {side.quality.failed_measures.length === 0 ? "none" : side.quality.failed_measures.join(", ")}
                </p>
              ) : (
                <p className="hint">No recorded quality batch covers this sample.</p>
              )}
            </div>
          );
        })}
      </div>
      <div className="pair-evidence-score">
        <p className="mono">
          {evidence.pair.score === null
            ? `no score: ${evidence.pair.failure} (${evidence.pair.failure_side})`
            : `score ${evidence.pair.score.toFixed(6)} · rule ${evidence.decision_rule}`}
        </p>
        <ScoreDistribution evidence={evidence} />
        <table className="measures" data-testid="evidence-decisions">
          <caption>Recorded matcher-threshold decisions for the selected pair</caption>
          <thead>
            <tr><th scope="col">Recorded threshold (kind: MATCHER THRESHOLD)</th><th scope="col">Value</th><th scope="col">Decision</th></tr>
          </thead>
          <tbody>
            {evidence.decisions.map((decision) => (
              <tr key={decision.name}>
                <th scope="row">{decision.name}</th>
                <td className="mono">{decision.threshold.toFixed(6)}</td>
                <td className={decision.decision === "ACCEPT" ? "accept" : "reject"}>{decision.decision}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {evidence.quality_attribution.available && evidence.quality_attribution.sides && (
        <div className="pair-evidence-attribution" data-testid="evidence-attribution">
          <h5>Quality context (association, not causation)</h5>
          <p className="hint">{evidence.quality_attribution.wording}</p>
          <div className="pair-evidence-sides">
            {(["left", "right"] as const).map((sideName) => (
              <table key={sideName} className="measures">
                <caption>{sideName} sample quality measures below the batch median</caption>
                <thead>
                  <tr><th scope="col">{sideName}: measure</th><th scope="col">value</th><th scope="col">batch median</th></tr>
                </thead>
                <tbody>
                  {(evidence.quality_attribution.sides?.[sideName] ?? []).map((item) => (
                    <tr key={item.measure}>
                      <th scope="row">{item.measure}</th>
                      <td className="mono">{item.scalar}</td>
                      <td className="mono">{item.population_median}</td>
                    </tr>
                  ))}
                  {(evidence.quality_attribution.sides?.[sideName] ?? []).length === 0 && (
                    <tr><td colSpan={3} className="hint">no measure below the batch median</td></tr>
                  )}
                </tbody>
              </table>
            ))}
          </div>
        </div>
      )}
      <p className="hint provenance-statement" data-testid="evidence-statement">{evidence.statement}</p>
    </div>
  );
}

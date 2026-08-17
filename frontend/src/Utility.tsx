import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type JobOut, type MatcherLeaf, type PairQualityPolicy, type UtilityAnalysisOut, type UtilityPairRow, type UtilitySummary } from "./api";

const MATCHER_SHORT: Record<string, string> = {
  "adaface-ir101-local-candidate": "AdaFace IR-101",
  "elasticface-arc-r100-local-candidate": "ElasticFace-Arc R100",
  "transface-s-glint360k-local-candidate": "TransFace-S",
};
const COLORS = ["#7dd3fc", "#f9a8d4", "#86efac", "#fcd34d", "#c4b5fd", "#fca5a5"];

function shortHash(value: string, keep = 12): string {
  const marker = value.indexOf("sha256:");
  const digest = marker >= 0 ? value.slice(marker + 7) : value;
  return `${digest.slice(0, keep)}…`;
}
function pct(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)} %`;
}
function num(value: number | null | undefined, digits = 4): string {
  return value === null || value === undefined ? "—" : value.toFixed(digits);
}
function conditionShort(conditionId: string): string {
  return conditionId.includes("INMEMORY") ? "native" : "shared";
}

interface UtilityProps {
  projectId: string | null;
  onMessage: (message: string) => void;
}

interface CurvePoint { rejected_fraction: number; retained: number; error_rate: number | null; quality_cutoff: number | null }

/** Quality-utility workbench: ERC/AU-ERC/risk-coverage, uncertainty, error models, and threshold-policy analysis (QS-D30..D33). */
export function Utility({ projectId, onMessage }: UtilityProps) {
  const [policies, setPolicies] = useState<PairQualityPolicy[]>([]);
  const [leaves, setLeaves] = useState<MatcherLeaf[]>([]);
  const [jobs, setJobs] = useState<JobOut[]>([]);
  const [analyses, setAnalyses] = useState<UtilitySummary[]>([]);
  const [selectedLeaf, setSelectedLeaf] = useState<string>("");
  const [selectedJob, setSelectedJob] = useState<string>("");
  const [policy, setPolicy] = useState<string>("pair.quality.min");
  const [measure, setMeasure] = useState<string>("UnifiedQualityScore");
  const [targetFmr, setTargetFmr] = useState<string>("0.001");
  const [qualityThreshold, setQualityThreshold] = useState<string>("");
  const [replicates, setReplicates] = useState<string>("1000");
  const [running, setRunning] = useState<string | null>(null);
  const [detail, setDetail] = useState<UtilityAnalysisOut | null>(null);
  const [curves, setCurves] = useState<Record<string, { fnmr: CurvePoint[]; label: string }>>({});
  const [pairs, setPairs] = useState<{ total: number; rows: UtilityPairRow[] } | null>(null);
  const [pairFilter, setPairFilter] = useState<"rejected" | "prevented" | "removed">("rejected");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    if (!projectId) {
      setLeaves([]); setJobs([]); setAnalyses([]);
      return;
    }
    api.matcherLeaves(projectId).then((items) => {
      setLeaves(items);
      setSelectedLeaf((current) => current || (items[0]?.run_id ?? ""));
    }).catch(() => setLeaves([]));
    api.projectJobs(projectId).then((items) => {
      const completed = items.filter((job) => job.state === "COMPLETED" && job.run_id);
      setJobs(completed);
      setSelectedJob((current) => current || (completed[0]?.job_id ?? ""));
    }).catch(() => setJobs([]));
    api.utilityAnalyses(projectId).then(setAnalyses).catch(() => setAnalyses([]));
  }, [projectId]);

  useEffect(() => {
    api.pairQualityPolicies().then((body) => setPolicies(body.policies)).catch(() => setPolicies([]));
  }, []);
  useEffect(() => {
    refresh();
    setDetail(null);
    setPairs(null);
    setCurves({});
    // Matcher Runs and quality jobs are produced by the other panels; keep the pick lists current.
    const handle = window.setInterval(refresh, 4000);
    return () => window.clearInterval(handle);
  }, [refresh]);

  // Poll a running analysis.
  useEffect(() => {
    if (!running) return;
    const handle = window.setInterval(() => {
      api.utilityAnalysis(running).then((status) => {
        if (status.state === "COMPLETED") {
          setRunning(null);
          setDetail(status);
          onMessage(`Utility analysis ${shortHash(status.analysis_id)} recorded as Run ${shortHash(status.summary?.run_id ?? "")}`);
          refresh();
        } else if (status.state === "FAILED") {
          setRunning(null);
          onMessage(`Utility analysis failed: ${status.error ?? "unknown error"}`);
        }
      }).catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(handle);
  }, [running, refresh, onMessage]);

  // ERC overlay curves for every recorded analysis of the project (FNMR-ERC, mated units).
  useEffect(() => {
    let cancelled = false;
    Promise.all(
      analyses.map((item) => api.utilityAnalysis(item.analysis_id).then((body) => [item.analysis_id, { fnmr: (body.utility?.utility?.erc?.curves?.FNMR?.points ?? []) as CurvePoint[], label: `${item.leaf_id} · ${item.quality_measure} · ${item.pair_quality_policy.replace("pair.quality.", "")}` }] as const)),
    ).then((entries) => {
      if (!cancelled) setCurves(Object.fromEntries(entries));
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [analyses]);

  const start = useCallback(async () => {
    if (!projectId || !selectedLeaf || !selectedJob) return;
    setBusy(true);
    try {
      const started = await api.startUtilityAnalysis(projectId, {
        matcher_run_id: selectedLeaf,
        quality_job_id: selectedJob,
        quality_measure: measure,
        pair_quality_policy: policy,
        target_fmr: Number(targetFmr),
        quality_threshold: qualityThreshold ? Number(qualityThreshold) : null,
        bootstrap_replicates: Number(replicates),
      });
      if (started.already_recorded) {
        onMessage(`Analysis ${shortHash(started.analysis_id)} was already recorded; showing it`);
        setDetail(await api.utilityAnalysis(started.analysis_id));
      } else {
        setRunning(started.analysis_id);
        onMessage(`Utility analysis ${shortHash(started.analysis_id)} running`);
      }
    } catch (error) {
      onMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }, [projectId, selectedLeaf, selectedJob, measure, policy, targetFmr, qualityThreshold, replicates, onMessage]);

  const open = useCallback(async (analysisId: string) => {
    try {
      setDetail(await api.utilityAnalysis(analysisId));
      setPairs(null);
    } catch (error) {
      onMessage(error instanceof Error ? error.message : String(error));
    }
  }, [onMessage]);

  const loadPairs = useCallback(async (filter: "rejected" | "prevented" | "removed") => {
    if (!detail?.summary) return;
    setPairFilter(filter);
    const query = filter === "rejected" ? "retained=false&limit=60" : filter === "prevented" ? "retained=false&decision_error=true&limit=60" : "retained=false&decision_error=false&limit=60";
    try {
      setPairs(await api.utilityPairs(detail.analysis_id, query));
    } catch (error) {
      onMessage(error instanceof Error ? error.message : String(error));
    }
  }, [detail, onMessage]);

  const analysisIds = useMemo(() => analyses.map((item) => item.analysis_id), [analyses]);
  const utility = detail?.utility?.utility;
  const policyBlock = detail?.policy;

  if (!projectId) {
    return (
      <section className="panel utility" aria-labelledby="utility-heading" data-testid="utility">
        <h2 id="utility-heading">Quality → utility · ERC, risk-coverage, policy analysis</h2>
        <p className="empty">Select a project with recorded matcher Runs and a completed quality batch.</p>
      </section>
    );
  }

  return (
    <section className="panel utility" aria-labelledby="utility-heading" data-testid="utility">
      <h2 id="utility-heading">Quality → utility · ERC, risk-coverage, policy analysis</h2>
      <div className="controls-row utility-controls">
        <label>
          Matcher Run
          <select value={selectedLeaf} onChange={(event) => setSelectedLeaf(event.target.value)} data-testid="utility-leaf">
            {leaves.map((leaf) => (
              <option key={leaf.run_id} value={leaf.run_id}>{leaf.leaf_id} · {MATCHER_SHORT[leaf.matcher_id] ?? leaf.matcher_id} · {conditionShort(leaf.condition_id)}</option>
            ))}
          </select>
        </label>
        <label>
          Quality job
          <select value={selectedJob} onChange={(event) => setSelectedJob(event.target.value)} data-testid="utility-job">
            {jobs.map((job) => (
              <option key={job.job_id} value={job.job_id}>{shortHash(job.job_id)} · {job.assessed.toLocaleString()} measured</option>
            ))}
          </select>
        </label>
        <label>
          Measure
          <input value={measure} onChange={(event) => setMeasure(event.target.value)} data-testid="utility-measure" />
        </label>
        <label>
          Pair policy
          <select value={policy} onChange={(event) => setPolicy(event.target.value)} data-testid="utility-policy">
            {policies.map((item) => (
              <option key={item.id} value={item.id} title={item.definition}>{item.id.replace("pair.quality.", "")} v{item.version}</option>
            ))}
          </select>
        </label>
        <label>
          Target FMR
          <input value={targetFmr} onChange={(event) => setTargetFmr(event.target.value)} data-testid="utility-fmr" />
        </label>
        <label>
          Quality ≥
          <input value={qualityThreshold} placeholder="auto (20 % cut)" onChange={(event) => setQualityThreshold(event.target.value)} data-testid="utility-q" />
        </label>
        <label>
          Bootstrap
          <input value={replicates} onChange={(event) => setReplicates(event.target.value)} data-testid="utility-replicates" />
        </label>
        <button type="button" className="primary" disabled={busy || running !== null || !selectedLeaf || !selectedJob} onClick={() => void start()} data-testid="utility-run">
          {running ? "Running…" : "Run utility analysis"}
        </button>
      </div>

      <div className="utility-grid">
        <div>
          <table className="compact utility-table" data-testid="utility-table">
            <thead>
              <tr>
                <th>Analysis</th><th>Matcher · crop</th><th>Measure · policy</th><th>T (matcher)</th><th>Q (policy)</th><th>AU-ERC</th><th>pAU-ERC 0–20 %</th><th>FNMR before → after</th><th>Retained</th><th>Errors prevented</th>
              </tr>
            </thead>
            <tbody>
              {analyses.length === 0 && (
                <tr><td colSpan={10} className="hint">No utility analyses recorded yet.</td></tr>
              )}
              {analyses.map((item, index) => (
                <tr key={item.analysis_id} className={detail?.analysis_id === item.analysis_id ? "selected" : undefined}>
                  <td><button type="button" className="cell" onClick={() => void open(item.analysis_id)} data-testid={`analysis-${item.leaf_id}`}><span className="swatch" style={{ background: COLORS[index % COLORS.length] }} /><span className="mono">{shortHash(item.analysis_id, 10)}</span></button></td>
                  <td>{item.leaf_id} · {conditionShort(item.condition_id)}</td>
                  <td>{item.quality_measure} · {item.pair_quality_policy.replace("pair.quality.", "")}</td>
                  <td className="mono">{num(item.matcher_threshold)}</td>
                  <td className="mono">{item.quality_threshold}</td>
                  <td className="mono">{num(item.au_erc_fnmr, 5)}</td>
                  <td className="mono">{num(item.partial_au_erc_fnmr, 5)}</td>
                  <td className="mono">{pct(item.fnmr_before)} → {pct(item.fnmr_after)}</td>
                  <td className="mono">{pct(item.retention_rate, 1)}</td>
                  <td className="mono">{item.errors_prevented} (−{item.successful_comparisons_removed} good)</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div>
          <svg className="det" viewBox="0 0 320 240" role="img" aria-label="ERC overlay" data-testid="erc-plot">
            <rect x="40" y="10" width="270" height="200" className="det-bg" />
            {[0, 0.2, 0.4, 0.6, 0.8, 1].map((v) => (
              <g key={`x${v}`}>
                <line x1={40 + v * 270} y1="10" x2={40 + v * 270} y2="210" className="grid" />
                <text x={40 + v * 270} y="225" textAnchor="middle" className="axis">{v}</text>
              </g>
            ))}
            {(() => {
              const maxError = Math.max(0.01, ...Object.values(curves).flatMap((c) => c.fnmr.map((p) => p.error_rate ?? 0)));
              return [0, 0.25, 0.5, 0.75, 1].map((f) => (
                <g key={`y${f}`}>
                  <line x1="40" y1={210 - f * 200} x2="310" y2={210 - f * 200} className="grid" />
                  <text x="36" y={213 - f * 200} textAnchor="end" className="axis">{(f * maxError * 100).toFixed(1)}%</text>
                </g>
              ));
            })()}
            {(() => {
              const maxError = Math.max(0.01, ...Object.values(curves).flatMap((c) => c.fnmr.map((p) => p.error_rate ?? 0)));
              return analysisIds.map((id, index) => {
                const curve = curves[id];
                if (!curve || curve.fnmr.length === 0) return null;
                const points = curve.fnmr.filter((p) => p.error_rate !== null).map((p) => `${40 + p.rejected_fraction * 270},${210 - ((p.error_rate ?? 0) / maxError) * 200}`).join(" ");
                return <polyline key={id} points={points} fill="none" stroke={COLORS[index % COLORS.length]} strokeWidth={detail?.analysis_id === id ? 2.5 : 1.2} />;
              });
            })()}
            <text x="175" y="238" textAnchor="middle" className="axis">rejected fraction (worst pair quality first)</text>
            <text x="12" y="110" textAnchor="middle" transform="rotate(-90 12 110)" className="axis">FNMR of retained mated units</text>
          </svg>
          <p className="hint">FNMR-ERC per recorded analysis; matcher threshold fixed per analysis (never re-estimated along the curve). FMR-ERC is reported separately in the detail below.</p>
        </div>
      </div>

      {detail?.summary && utility && policyBlock && (
        <div className="errors utility-detail" data-testid="utility-detail">
          <h3>
            Analysis <span className="mono">{shortHash(detail.analysis_id, 14)}</span> · Run <a href={detail.summary.links.run} className="mono">{shortHash(detail.summary.run_id, 14)}</a> · {detail.summary.leaf_id} · {detail.summary.quality_measure} · {detail.summary.pair_quality_policy}
          </h3>
          <div className="utility-facts">
            <div>
              <h4>Thresholds (never conflated)</h4>
              <dl className="facts">
                <dt>MATCHER THRESHOLD</dt><dd className="mono">{num(policyBlock.thresholds.matcher.value, 6)} · {policyBlock.thresholds.matcher.provenance?.source}{policyBlock.thresholds.matcher.provenance?.target_fmr !== undefined ? ` (FMR ≤ ${policyBlock.thresholds.matcher.provenance.target_fmr})` : ""}</dd>
                <dt>QUALITY POLICY THRESHOLD</dt><dd className="mono">{policyBlock.thresholds.quality_policy.value} on {policyBlock.thresholds.quality_policy.quality_measure} · {policyBlock.thresholds.quality_policy.provenance?.source}</dd>
                <dt>Re-estimated during policy</dt><dd>{String(policyBlock.thresholds.matcher.reestimated_during_policy)}</dd>
                <dt>Uncertainty</dt><dd>{utility.uncertainty.method} · {utility.uncertainty.replicates} replicates · seed {utility.uncertainty.seed} · unit {utility.uncertainty.sampling_unit}</dd>
              </dl>
            </div>
            <div>
              <h4>Areas under the ERC (registry v1.1.0)</h4>
              <table className="compact">
                <thead><tr><th>Curve</th><th>Metric</th><th>Domain</th><th>Value</th><th>95 % CI (cluster bootstrap)</th><th>Units</th></tr></thead>
                <tbody>
                  {(["FNMR", "FMR"] as const).flatMap((kind) => (["au_erc", "partial_au_erc"] as const).map((name) => {
                    const block = utility.areas[kind][name];
                    return (
                      <tr key={`${kind}-${name}`}>
                        <td>{kind}-ERC</td><td className="mono">{block.definition[0]}@{block.definition[1]}</td><td className="mono">[{block.integration_domain[0]}, {block.integration_domain[1]}]</td>
                        <td className="mono">{num(block.value, 5)}</td>
                        <td className="mono">{block.uncertainty?.status === "ESTIMATED" ? `${num(block.uncertainty.ci_lower, 5)} – ${num(block.uncertainty.ci_upper, 5)}` : block.uncertainty?.status ?? "—"}</td>
                        <td className="mono">{block.comparison_units}</td>
                      </tr>
                    );
                  }))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="utility-facts">
            <div>
              <h4>Policy: quality ≥ {policyBlock.thresholds.quality_policy.value} at fixed matcher threshold</h4>
              <table className="compact" data-testid="policy-table">
                <tbody>
                  <tr><th>Retention rate</th><td className="mono">{pct(policyBlock.retention_rate, 2)} ({policyBlock.counts.retained} of {policyBlock.counts.usable_units})</td></tr>
                  <tr><th>Rejection rate</th><td className="mono">{pct(policyBlock.rejection_rate, 2)} ({policyBlock.counts.rejected} rejected)</td></tr>
                  <tr><th>FNMR before → after</th><td className="mono">{pct(policyBlock.before_policy.fnmr.rate)} → {pct(policyBlock.after_policy.fnmr.rate)} [{pct(policyBlock.after_policy.fnmr.ci_lower)}, {pct(policyBlock.after_policy.fnmr.ci_upper)}]</td></tr>
                  <tr><th>FMR before → after</th><td className="mono">{pct(policyBlock.before_policy.fmr.rate, 3)} → {pct(policyBlock.after_policy.fmr.rate, 3)}</td></tr>
                  <tr><th>Errors prevented</th><td className="mono">{policyBlock.impact.errors_prevented} ({policyBlock.impact.false_non_matches_prevented} FNM · {policyBlock.impact.false_matches_prevented} FM)</td></tr>
                  <tr><th>Successful comparisons removed</th><td className="mono">{policyBlock.impact.successful_comparisons_removed} ({policyBlock.impact.successful_mated_removed} mated · {policyBlock.impact.successful_non_mated_removed} non-mated)</td></tr>
                  <tr><th>Excluded</th><td className="mono">{policyBlock.counts.processing_failures_excluded} processing failures · {policyBlock.counts.missing_quality_excluded} missing quality</td></tr>
                </tbody>
              </table>
              <div className="controls-row">
                <button type="button" className={pairFilter === "rejected" && pairs ? "secondary selected" : "secondary"} onClick={() => void loadPairs("rejected")} data-testid="pairs-rejected">Rejected pairs</button>
                <button type="button" className={pairFilter === "prevented" && pairs ? "secondary selected" : "secondary"} onClick={() => void loadPairs("prevented")} data-testid="pairs-prevented">Errors prevented</button>
                <button type="button" className={pairFilter === "removed" && pairs ? "secondary selected" : "secondary"} onClick={() => void loadPairs("removed")} data-testid="pairs-removed">Good comparisons removed</button>
              </div>
            </div>
            <div>
              <h4>Quality-conditioned FNMR (bins recorded with the Run)</h4>
              <table className="compact" data-testid="conditioned-table">
                <thead><tr><th>Bin</th><th>Units</th><th>Errors</th><th>FNMR</th><th>95 % CI</th><th>Support</th></tr></thead>
                <tbody>
                  {utility.quality_conditioned.FNMR.bins.map((bin: any) => (
                    <tr key={bin.bin_index}>
                      <td className="mono">[{bin.quality_low}, {bin.quality_high}{bin.closed_high ? "]" : ")"}</td><td className="mono">{bin.trials}</td><td className="mono">{bin.events}</td>
                      <td className="mono">{pct(bin.rate)}</td><td className="mono">{bin.ci_lower === null ? "—" : `${pct(bin.ci_lower)} – ${pct(bin.ci_upper)}`}</td>
                      <td>{bin.supported ? "supported" : "unsupported precision"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="utility-facts">
            <div>
              <h4>Quality–error association (mated units)</h4>
              <dl className="facts">
                <dt>Spearman ρ (quality vs score)</dt><dd className="mono">{num(utility.association.FNMR.spearman_quality_vs_score?.rho)} [{num(utility.association.FNMR.spearman_quality_vs_score?.ci_lower)}, {num(utility.association.FNMR.spearman_quality_vs_score?.ci_upper)}] · {utility.association.FNMR.spearman_quality_vs_score?.ci_method}</dd>
                <dt>Kendall τ-b</dt><dd className="mono">{num(utility.association.FNMR.kendall_quality_vs_score?.tau)}</dd>
                <dt>Logistic FNM ~ quality</dt><dd className="mono">{utility.association.FNMR.logistic_error_on_quality?.status === "ESTIMATED" ? `OR per 10 units ${num(utility.association.FNMR.logistic_error_on_quality.odds_ratio_per_10_units, 3)} [${num(utility.association.FNMR.logistic_error_on_quality.odds_ratio_per_10_units_ci[0], 3)}, ${num(utility.association.FNMR.logistic_error_on_quality.odds_ratio_per_10_units_ci[1], 3)}] · ${utility.association.FNMR.logistic_error_on_quality.events} events` : `${utility.association.FNMR.logistic_error_on_quality?.status ?? "—"} (${utility.association.FNMR.logistic_error_on_quality?.support_rule ?? ""})`}</dd>
                <dt>Quality as FNM predictor</dt><dd className="mono">{utility.failure_prediction.FNMR.roc_auc !== undefined ? `ROC-AUC ${num(utility.failure_prediction.FNMR.roc_auc, 3)} · PR-AUC ${num(utility.failure_prediction.FNMR.pr_auc, 3)} · prevalence ${pct(utility.failure_prediction.FNMR.prevalence)}` : utility.failure_prediction.FNMR.status}</dd>
                {utility.failure_prediction.FNMR.at_quality_cutoff && (
                  <>
                    <dt>At Q cutoff {utility.failure_prediction.FNMR.at_quality_cutoff.cutoff}</dt>
                    <dd className="mono">sens {pct(utility.failure_prediction.FNMR.at_quality_cutoff.sensitivity.rate, 1)} · spec {pct(utility.failure_prediction.FNMR.at_quality_cutoff.specificity.rate, 1)} · prec {pct(utility.failure_prediction.FNMR.at_quality_cutoff.precision.rate, 1)}</dd>
                  </>
                )}
                {utility.failure_prediction.PROCESSING_FAILURE && (
                  <>
                    <dt>Quality vs processing failure</dt>
                    <dd className="mono">ROC-AUC {num(utility.failure_prediction.PROCESSING_FAILURE.roc_auc, 3)} · {utility.failure_prediction.PROCESSING_FAILURE.outcome_events} failures</dd>
                  </>
                )}
              </dl>
            </div>
            <div>
              <h4>Per-quality-measure impact (retention-matched cutoffs)</h4>
              <div className="measure-impact">
                <table className="compact" data-testid="measure-impact">
                  <thead><tr><th>Measure</th><th>Cutoff</th><th>Retention</th><th>Errors prevented</th><th>Good removed</th><th>FNMR after</th></tr></thead>
                  <tbody>
                    {[...(policyBlock.per_quality_measure_impact?.measures ?? [])].sort((a: any, b: any) => (b.errors_prevented ?? -1) - (a.errors_prevented ?? -1)).map((row: any) => (
                      <tr key={row.quality_measure}>
                        <td>{row.quality_measure}</td><td className="mono">{row.quality_cutoff ?? row.status}</td><td className="mono">{pct(row.retention_rate, 1)}</td><td className="mono">{row.errors_prevented ?? "—"}</td><td className="mono">{row.successful_comparisons_removed ?? "—"}</td><td className="mono">{pct(row.fnmr_after)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          {pairs && (
            <div className="error-list" data-testid="utility-pairs">
              <h4>{pairFilter === "rejected" ? "Rejected by the quality policy" : pairFilter === "prevented" ? "Errors prevented by the policy" : "Successful comparisons removed by the policy"} · {pairs.total} pairs (showing {pairs.rows.length})</h4>
              <ul>
                {pairs.rows.map((row) => (
                  <li key={row.pair_id} className="pair" data-testid="utility-pair">
                    {row.links.left_image && <img src={row.links.left_image} alt="" loading="lazy" />}
                    {row.links.right_image && <img src={row.links.right_image} alt="" loading="lazy" />}
                    <div className="pair-facts">
                      <span className="mono">{row.pair_id} · fold {row.fold} · {row.label}</span>
                      <span className="mono">q {row.quality_left ?? "—"} / {row.quality_right ?? "—"} → pair {row.pair_quality ?? "—"} · {row.score === null ? row.failure : `score ${row.score.toFixed(4)}`}{row.decision_error ? " · matcher error" : ""}</span>
                      <span className="links">
                        <a href={row.links.left_quality}>left quality</a>
                        <a href={row.links.right_quality}>right quality</a>
                        <a href={row.links.run}>run</a>
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

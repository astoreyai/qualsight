import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type CrossContrastSummary, type CrossMatcherOut, type CrossMatcherSummary, type CrossPairRow, type JobOut, type MatcherLeaf } from "./api";
import { PairEvidence } from "./PairEvidence";

const MATCHER_SHORT: Record<string, string> = {
  "adaface-ir101-local-candidate": "AdaFace IR-101",
  "elasticface-arc-r100-local-candidate": "ElasticFace-Arc R100",
  "transface-s-glint360k-local-candidate": "TransFace-S",
};
const CATEGORY_LABEL: Record<string, string> = {
  A_ONLY: "failed by A only",
  B_ONLY: "failed by B only",
  BOTH: "failed by both",
  NEITHER: "failed by neither",
  FAILURE_A: "processing failure in A",
  FAILURE_B: "processing failure in B",
  FAILURE_BOTH: "processing failure in both",
};

function shortHash(value: string, keep = 12): string {
  const marker = value.indexOf("sha256:");
  const digest = marker >= 0 ? value.slice(marker + 7) : value;
  return `${digest.slice(0, keep)}…`;
}
function num(value: number | null | undefined, digits = 4): string {
  return value === null || value === undefined ? "—" : value.toFixed(digits);
}
function pct(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)} %`;
}
function conditionShort(conditionId: string): string {
  return conditionId.includes("INMEMORY") ? "native" : "shared";
}

interface CrossMatcherProps {
  projectId: string | null;
  onMessage: (message: string) => void;
}

/** Cross-matcher workbench: fixed-condition matcher contrasts, fixed-matcher preprocessing contrasts, disagreement drilldown, annotations (QS-E40..E42). */
export function CrossMatcher({ projectId, onMessage }: CrossMatcherProps) {
  const [leaves, setLeaves] = useState<MatcherLeaf[]>([]);
  const [jobs, setJobs] = useState<JobOut[]>([]);
  const [analyses, setAnalyses] = useState<CrossMatcherSummary[]>([]);
  const [selectedMatrix, setSelectedMatrix] = useState<string>("");
  const [selectedJob, setSelectedJob] = useState<string>("");
  const [replicates, setReplicates] = useState<string>("1000");
  const [running, setRunning] = useState<string | null>(null);
  const [detail, setDetail] = useState<CrossMatcherOut | null>(null);
  const [contrast, setContrast] = useState<CrossContrastSummary | null>(null);
  const [category, setCategory] = useState<string>("A_ONLY");
  const [pairs, setPairs] = useState<{ total: number; rows: CrossPairRow[] } | null>(null);
  const [noteText, setNoteText] = useState<Record<string, string>>({});
  const [evidencePair, setEvidencePair] = useState<{ pair: string; run: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    if (!projectId) {
      setLeaves([]); setJobs([]); setAnalyses([]);
      return;
    }
    api.matcherLeaves(projectId).then((items) => {
      setLeaves(items);
      // Default to the fullest matrix (most executed leaves), newest as tie-break.
      const counts = new Map<string, number>();
      for (const leaf of items) {
        if (leaf.condition_id !== "EXTERNALLY_COMPUTED") counts.set(leaf.matrix_id, (counts.get(leaf.matrix_id) ?? 0) + 1);
      }
      const best = Array.from(counts.entries()).sort((a, b) => b[1] - a[1])[0]?.[0];
      setSelectedMatrix((current) => current || (best ?? ""));
    }).catch(() => setLeaves([]));
    api.projectJobs(projectId).then((items) => {
      const completed = items.filter((job) => job.state === "COMPLETED" && job.run_id);
      setJobs(completed);
    }).catch(() => setJobs([]));
    api.crossMatcherAnalyses(projectId).then(setAnalyses).catch(() => setAnalyses([]));
  }, [projectId]);

  const matrixDataset = useMemo(
    () => leaves.find((leaf) => leaf.matrix_id === selectedMatrix)?.dataset_id ?? null,
    [leaves, selectedMatrix],
  );
  const eligibleJobs = useMemo(
    () => jobs.filter((job) => !matrixDataset || !job.dataset_id || job.dataset_id === matrixDataset),
    [jobs, matrixDataset],
  );
  useEffect(() => {
    // The quality job must cover the matrix's own dataset (uploads and other
    // datasets' jobs are not comparable to these matcher Runs).
    setSelectedJob((current) =>
      current && eligibleJobs.some((job) => job.job_id === current) ? current : (eligibleJobs[0]?.job_id ?? ""),
    );
  }, [eligibleJobs]);

  useEffect(() => {
    refresh();
    setDetail(null); setContrast(null); setPairs(null);
    const handle = window.setInterval(refresh, 4000);
    return () => window.clearInterval(handle);
  }, [refresh]);

  useEffect(() => {
    if (!running) return;
    const handle = window.setInterval(() => {
      api.crossMatcher(running).then((status) => {
        if (status.state === "COMPLETED") {
          setRunning(null);
          setDetail(status);
          onMessage(`Cross-matcher analysis ${shortHash(status.analysis_id)} recorded as Run ${shortHash(status.summary?.run_id ?? "")}`);
          refresh();
        } else if (status.state === "FAILED") {
          setRunning(null);
          onMessage(`Cross-matcher analysis failed: ${status.error ?? "unknown error"}`);
        }
      }).catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(handle);
  }, [running, refresh, onMessage]);

  const start = useCallback(async () => {
    if (!projectId || !selectedMatrix || !selectedJob) return;
    setBusy(true);
    try {
      const started = await api.startCrossMatcher(projectId, { matrix_id: selectedMatrix, quality_job_id: selectedJob, bootstrap_replicates: Number(replicates) });
      if (started.already_recorded) {
        onMessage(`Cross-matcher analysis ${shortHash(started.analysis_id)} was already recorded; showing it`);
        setDetail(await api.crossMatcher(started.analysis_id));
      } else {
        setRunning(started.analysis_id);
        onMessage(`Cross-matcher analysis ${shortHash(started.analysis_id)} running`);
      }
    } catch (error) {
      onMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }, [projectId, selectedMatrix, selectedJob, replicates, onMessage]);

  const open = useCallback(async (analysisId: string) => {
    try {
      setDetail(await api.crossMatcher(analysisId));
      setContrast(null); setPairs(null);
    } catch (error) {
      onMessage(error instanceof Error ? error.message : String(error));
    }
  }, [onMessage]);

  const showPairs = useCallback(async (item: CrossContrastSummary, nextCategory: string) => {
    if (!detail) return;
    setContrast(item);
    setCategory(nextCategory);
    try {
      setPairs(await api.crossPairs(detail.analysis_id, item.contrast_id, nextCategory));
    } catch (error) {
      onMessage(error instanceof Error ? error.message : String(error));
    }
  }, [detail, onMessage]);

  const annotate = useCallback(async (row: CrossPairRow) => {
    if (!projectId || !detail || !contrast) return;
    const text = (noteText[row.pair_id] ?? "").trim();
    if (!text) return;
    try {
      await api.annotate(projectId, "PROTOCOL_PAIR", row.pair_id, text, { contrast_id: contrast.contrast_id, analysis_id: detail.analysis_id, category: row.category });
      setNoteText((current) => ({ ...current, [row.pair_id]: "" }));
      onMessage(`Annotation recorded for ${row.pair_id}`);
      setPairs(await api.crossPairs(detail.analysis_id, contrast.contrast_id, category));
    } catch (error) {
      onMessage(error instanceof Error ? error.message : String(error));
    }
  }, [projectId, detail, contrast, noteText, category, onMessage]);

  const matrices = Array.from(new Set(leaves.map((leaf) => leaf.matrix_id)));
  const fullContrast = detail?.document?.contrasts?.find((c: any) => c.contrast_id === contrast?.contrast_id);

  if (!projectId) {
    return (
      <section className="panel crossmatcher" aria-labelledby="crossmatcher-heading" data-testid="crossmatcher">
        <h2 id="crossmatcher-heading">Cross-matcher · fixed-condition and fixed-matcher contrasts</h2>
        <p className="empty">Select a project with a recorded matcher matrix and a completed quality batch.</p>
      </section>
    );
  }

  return (
    <section className="panel crossmatcher" aria-labelledby="crossmatcher-heading" data-testid="crossmatcher">
      <h2 id="crossmatcher-heading">Cross-matcher · fixed-condition and fixed-matcher contrasts</h2>
      <div className="controls-row utility-controls">
        <label>
          Matrix
          <select value={selectedMatrix} onChange={(event) => setSelectedMatrix(event.target.value)} data-testid="cross-matrix">
            {matrices.map((id) => (
              <option key={id} value={id}>{shortHash(id)} · {leaves.filter((leaf) => leaf.matrix_id === id).length === 1 ? "1 leaf" : `${leaves.filter((leaf) => leaf.matrix_id === id).length} leaves`}</option>
            ))}
          </select>
        </label>
        <label>
          Quality job
          <select value={selectedJob} onChange={(event) => setSelectedJob(event.target.value)} data-testid="cross-job">
            {eligibleJobs.map((job) => (
              <option key={job.job_id} value={job.job_id}>{shortHash(job.job_id)} · {job.assessed.toLocaleString()} measured</option>
            ))}
          </select>
        </label>
        <label>
          Bootstrap
          <input value={replicates} onChange={(event) => setReplicates(event.target.value)} data-testid="cross-replicates" />
        </label>
        <button type="button" className="primary" disabled={busy || running !== null || !selectedMatrix || !selectedJob} onClick={() => void start()} data-testid="cross-run">
          {running ? "Running…" : "Run contrasts"}
        </button>
        <span className="hint">Matcher contrasts hold the crop fixed; preprocessing contrasts hold the matcher fixed. Nothing is pooled across conditions.</span>
      </div>

      <table className="compact" data-testid="cross-table">
        <thead>
          <tr><th>Analysis</th><th>Matrix</th><th>Measure · policy</th><th>Target FMR</th><th>Contrasts</th><th>Run</th></tr>
        </thead>
        <tbody>
          {analyses.length === 0 && <tr><td colSpan={6} className="hint">No cross-matcher analyses recorded yet.</td></tr>}
          {analyses.map((item) => (
            <tr key={item.analysis_id} className={detail?.analysis_id === item.analysis_id ? "selected" : undefined}>
              <td><button type="button" className="cell" onClick={() => void open(item.analysis_id)} data-testid="cross-analysis"><span className="mono">{shortHash(item.analysis_id, 10)}</span></button></td>
              <td className="mono">{shortHash(item.matrix_id, 10)}</td>
              <td>{item.quality_measure} · {item.pair_quality_policy.replace("pair.quality.", "")}</td>
              <td className="mono">{item.target_fmr}</td>
              <td className="mono">{item.matcher_contrasts} matcher · {item.preprocessing_contrasts} preprocessing</td>
              <td><a href={item.links.run} className="mono">{shortHash(item.run_id, 10)}</a></td>
            </tr>
          ))}
        </tbody>
      </table>

      {detail?.summary && (
        <div className="errors" data-testid="cross-detail">
          <h3>Contrasts of analysis <span className="mono">{shortHash(detail.analysis_id, 14)}</span> · thresholds at each Run's own FMR ≤ {detail.summary.target_fmr} operating point</h3>
          <table className="compact" data-testid="contrast-table">
            <thead>
              <tr><th>Contrast</th><th>Kind · held fixed</th><th>A</th><th>B</th><th>FNM A only / B only / both / neither</th><th>Jaccard (FNM)</th><th>AU-ERC A − B (FNMR) [95 % paired CI]</th><th>Quality × matcher interaction</th><th>Pairs</th></tr>
            </thead>
            <tbody>
              {detail.summary.contrasts.map((item) => {
                const full = detail.document?.contrasts?.find((c: any) => c.contrast_id === item.contrast_id);
                const interaction = full?.quality_matcher_interaction;
                const term = interaction?.coefficients?.find((c: any) => c.term === "pair_quality_x_is_b");
                return (
                  <tr key={item.contrast_id} className={contrast?.contrast_id === item.contrast_id ? "selected" : undefined} data-testid="contrast-row">
                    <td className="mono">{item.contrast_id}</td>
                    <td>{item.kind === "MATCHER_CONTRAST_FIXED_CONDITION" ? `matcher · crop ${conditionShort(item.held_fixed.condition_id ?? "")}` : `preprocessing · ${MATCHER_SHORT[item.held_fixed.matcher_id ?? ""] ?? item.held_fixed.matcher_id}`}</td>
                    <td>{full ? `${MATCHER_SHORT[full.a.matcher_id] ?? full.a.matcher_id} · ${conditionShort(full.a.condition_id)} · T ${num(full.a.threshold)}` : item.a}</td>
                    <td>{full ? `${MATCHER_SHORT[full.b.matcher_id] ?? full.b.matcher_id} · ${conditionShort(full.b.condition_id)} · T ${num(full.b.threshold)}` : item.b}</td>
                    <td className="mono">{item.fnm_overlap.A_ONLY} / {item.fnm_overlap.B_ONLY} / {item.fnm_overlap.BOTH} / {item.fnm_overlap.NEITHER}</td>
                    <td className="mono">{num(item.fnm_jaccard, 3)}</td>
                    <td className="mono">{num(item.au_erc_diff_fnmr, 5)}{item.au_erc_diff_fnmr_ci && item.au_erc_diff_fnmr_ci[0] !== null ? ` [${num(item.au_erc_diff_fnmr_ci[0], 5)}, ${num(item.au_erc_diff_fnmr_ci[1], 5)}]` : ""}</td>
                    <td className="mono">{term ? `β ${num(term.coefficient, 4)} (p ${num(term.p_two_sided, 3)})` : item.interaction_status}</td>
                    <td>
                      <div className="controls-row">
                        {["A_ONLY", "B_ONLY", "BOTH", "NEITHER"].map((cat) => (
                          <button key={cat} type="button" className={contrast?.contrast_id === item.contrast_id && category === cat ? "secondary selected" : "secondary"} onClick={() => void showPairs(item, cat)} data-testid={`pairs-${cat}`}>{CATEGORY_LABEL[cat]}</button>
                        ))}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          {fullContrast && (
            <div className="utility-facts">
              <div>
                <h4>Threshold transferability · {fullContrast.contrast_id}</h4>
                <table className="compact">
                  <thead><tr><th>Direction</th><th>Threshold</th><th>FMR</th><th>FNMR</th><th>Own FNMR</th></tr></thead>
                  <tbody>
                    <tr><td>A's threshold on B</td><td className="mono">{num(fullContrast.threshold_transferability.a_threshold_on_b.threshold)}</td><td className="mono">{pct(fullContrast.threshold_transferability.a_threshold_on_b.fmr.rate, 3)}</td><td className="mono">{pct(fullContrast.threshold_transferability.a_threshold_on_b.fnmr.rate)}</td><td className="mono">{pct(fullContrast.threshold_transferability.a_threshold_on_b.b_own.fnmr.rate)}</td></tr>
                    <tr><td>B's threshold on A</td><td className="mono">{num(fullContrast.threshold_transferability.b_threshold_on_a.threshold)}</td><td className="mono">{pct(fullContrast.threshold_transferability.b_threshold_on_a.fmr.rate, 3)}</td><td className="mono">{pct(fullContrast.threshold_transferability.b_threshold_on_a.fnmr.rate)}</td><td className="mono">{pct(fullContrast.threshold_transferability.b_threshold_on_a.a_own.fnmr.rate)}</td></tr>
                  </tbody>
                </table>
                <dl className="facts">
                  <dt>Score agreement (mated, Spearman)</dt><dd className="mono">{num(fullContrast.score_agreement.spearman_scores_mated.rho, 3)}</dd>
                  <dt>Quality predicts FNM (ROC-AUC A / B)</dt><dd className="mono">{num(fullContrast.quality_rank_consistency.quality_predicts_a_false_non_match_roc_auc, 3)} / {num(fullContrast.quality_rank_consistency.quality_predicts_b_false_non_match_roc_auc, 3)}</dd>
                  <dt>Median pair quality of FNM (A / B / all mated)</dt><dd className="mono">{fullContrast.quality_rank_consistency.median_pair_quality_a_false_non_matches ?? "—"} / {fullContrast.quality_rank_consistency.median_pair_quality_b_false_non_matches ?? "—"} / {fullContrast.quality_rank_consistency.median_pair_quality_all_mated ?? "—"}</dd>
                  <dt>Processing failures A only / B only / both</dt><dd className="mono">{fullContrast.error_overlap.processing_failures.A_ONLY} / {fullContrast.error_overlap.processing_failures.B_ONLY} / {fullContrast.error_overlap.processing_failures.BOTH}</dd>
                </dl>
              </div>
              <div>
                <h4>Provenance</h4>
                <dl className="facts">
                  <dt>Run A</dt><dd><a href={`/api/runs/${fullContrast.a.run_id}`} className="mono">{shortHash(fullContrast.a.run_id, 14)}</a> · {fullContrast.a.leaf_id} · {fullContrast.a.condition_id}</dd>
                  <dt>Run B</dt><dd><a href={`/api/runs/${fullContrast.b.run_id}`} className="mono">{shortHash(fullContrast.b.run_id, 14)}</a> · {fullContrast.b.leaf_id} · {fullContrast.b.condition_id}</dd>
                  <dt>Analysis Run</dt><dd><a href={detail.summary.links.run} className="mono">{shortHash(detail.summary.run_id, 14)}</a></dd>
                  <dt>Threshold kinds</dt><dd>MATCHER_THRESHOLD per Run (recorded operating point); transferred thresholds labelled MATCHER_THRESHOLD_TRANSFERRED</dd>
                </dl>
              </div>
            </div>
          )}

          {pairs && contrast && (
            <div className="error-list" data-testid="cross-pairs">
              <h4>{contrast.contrast_id} · {CATEGORY_LABEL[category]} · {pairs.total} pairs (showing {pairs.rows.length})</h4>
              <ul>
                {pairs.rows.map((row) => (
                  <li key={row.pair_id} className="pair" data-testid="cross-pair">
                    {row.links.left_image && <img src={row.links.left_image} alt="" loading="lazy" />}
                    {row.links.right_image && <img src={row.links.right_image} alt="" loading="lazy" />}
                    <div className="pair-facts">
                      <span className="mono">{row.pair_id} · {row.label} · {CATEGORY_LABEL[row.category]}</span>
                      <span className="mono">A {row.score_a === null ? row.failure_a : row.score_a.toFixed(4)} · B {row.score_b === null ? row.failure_b : row.score_b.toFixed(4)} · q {row.quality_left ?? "—"}/{row.quality_right ?? "—"} → {row.pair_quality ?? "—"}</span>
                      <span className="links">
                        <a href={row.links.left_quality}>left quality</a>
                        <a href={row.links.right_quality}>right quality</a>
                        <a href={row.links.run_a}>run A</a>
                        <a href={row.links.run_b}>run B</a>
                        <button type="button" className="secondary" data-testid="cross-evidence-open" onClick={() => setEvidencePair(evidencePair?.pair === row.pair_id ? null : { pair: row.pair_id, run: row.links.run_a.split("/").pop() ?? "" })}>
                          {evidencePair?.pair === row.pair_id ? "hide evidence" : "match evidence (A)"}
                        </button>
                      </span>
                      {row.annotations.length > 0 && (
                        <ul className="annotations">
                          {row.annotations.map((note) => (
                            <li key={note.annotation_id}><span className="mono">{note.actor_id} · {note.created_at.slice(0, 19)}</span> — {note.text}</li>
                          ))}
                        </ul>
                      )}
                      <span className="controls-row">
                        <input value={noteText[row.pair_id] ?? ""} placeholder="annotate this pair" onChange={(event) => setNoteText((current) => ({ ...current, [row.pair_id]: event.target.value }))} data-testid="annotation-input" />
                        <button type="button" className="secondary" onClick={() => void annotate(row)} data-testid="annotation-save">Save</button>
                      </span>
                    </div>
                  {evidencePair?.pair === row.pair_id && (
                      <PairEvidence runId={evidencePair.run} pairId={row.pair_id} onClose={() => setEvidencePair(null)} />
                    )}
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

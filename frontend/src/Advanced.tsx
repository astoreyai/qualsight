import { useCallback, useEffect, useState } from "react";
import { api, type DegradationOut, type DegradationSummary, type DerivedSetOut, type IdentificationProtocolOut, type IdentificationSearchOut, type IdentificationSearchSummary, type JobOut } from "./api";

const MATCHERS = ["adaface-ir101-local-candidate", "elasticface-arc-r100-local-candidate", "transface-s-glint360k-local-candidate"];
const CONDITIONS = ["SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1", "SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1"];

function shortHash(value: string, keep = 12): string {
  const marker = value.indexOf("sha256:");
  const digest = marker >= 0 ? value.slice(marker + 7) : value;
  return `${digest.slice(0, keep)}…`;
}
function pct(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)} %`;
}
function num(value: number | null | undefined, digits = 3): string {
  return value === null || value === undefined ? "—" : value.toFixed(digits);
}

interface AdvancedProps {
  projectId: string | null;
  datasetId: string | null;
  onMessage: (message: string) => void;
}

/** Phase 6 advanced analysis: 1:N identification (QS-X60) and controlled degradation responses (QS-X61). */
export function Advanced({ projectId, datasetId, onMessage }: AdvancedProps) {
  const [protocols, setProtocols] = useState<IdentificationProtocolOut[]>([]);
  const [searches, setSearches] = useState<IdentificationSearchSummary[]>([]);
  const [derivedSets, setDerivedSets] = useState<DerivedSetOut[]>([]);
  const [degradations, setDegradations] = useState<DegradationSummary[]>([]);
  const [jobs, setJobs] = useState<JobOut[]>([]);
  const [selection, setSelection] = useState<"all" | "subset_fixture">("subset_fixture");
  const [matcher, setMatcher] = useState<string>(MATCHERS[0]!);
  const [condition, setCondition] = useState<string>(CONDITIONS[0]!);
  const [runningSearch, setRunningSearch] = useState<string | null>(null);
  const [searchDetail, setSearchDetail] = useState<IdentificationSearchOut | null>(null);
  const [candidates, setCandidates] = useState<any[] | null>(null);
  const [transformSpec, setTransformSpec] = useState<string>("jpeg:80,50,20; gaussian_blur:1,2; downscale:0.5");
  const [threshold, setThreshold] = useState<string>("0.8");
  const [runningDegradation, setRunningDegradation] = useState<string | null>(null);
  const [degradationDetail, setDegradationDetail] = useState<DegradationOut | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    if (!projectId) {
      setProtocols([]); setSearches([]); setDerivedSets([]); setDegradations([]); setJobs([]);
      return;
    }
    api.identificationProtocols(projectId).then(setProtocols).catch(() => setProtocols([]));
    api.identificationSearches(projectId).then(setSearches).catch(() => setSearches([]));
    api.derivedSets(projectId).then(setDerivedSets).catch(() => setDerivedSets([]));
    api.degradations(projectId).then(setDegradations).catch(() => setDegradations([]));
    api.projectJobs(projectId).then((items) => setJobs(items.filter((job) => job.state === "COMPLETED" && job.run_id))).catch(() => setJobs([]));
  }, [projectId]);

  useEffect(() => {
    refresh();
    setSearchDetail(null); setCandidates(null); setDegradationDetail(null);
    const handle = window.setInterval(refresh, 5000);
    return () => window.clearInterval(handle);
  }, [refresh]);

  useEffect(() => {
    if (!runningSearch) return;
    const handle = window.setInterval(() => {
      api.identificationSearch(runningSearch).then((status) => {
        if (status.state === "COMPLETED") { setRunningSearch(null); setSearchDetail(status); onMessage(`Identification search ${shortHash(status.search_id)} recorded as Run ${shortHash(status.run_id ?? "")}`); refresh(); }
        else if (status.state === "FAILED") { setRunningSearch(null); onMessage(`Identification search failed: ${status.error ?? "unknown"}`); }
      }).catch(() => undefined);
    }, 2000);
    return () => window.clearInterval(handle);
  }, [runningSearch, refresh, onMessage]);

  useEffect(() => {
    if (!runningDegradation) return;
    const handle = window.setInterval(() => {
      api.degradation(runningDegradation).then((status) => {
        if (status.state === "COMPLETED") { setRunningDegradation(null); setDegradationDetail(status); onMessage(`Degradation analysis ${shortHash(status.analysis_id)} recorded as Run ${shortHash(status.run_id ?? "")}`); refresh(); }
        else if (status.state === "FAILED") { setRunningDegradation(null); onMessage(`Degradation analysis failed: ${status.error ?? "unknown"}`); }
      }).catch(() => undefined);
    }, 2000);
    return () => window.clearInterval(handle);
  }, [runningDegradation, refresh, onMessage]);

  const guard = async (label: string, action: () => Promise<string>) => {
    setBusy(true);
    try { onMessage(await action()); refresh(); } catch (error) { onMessage(`${label}: ${error instanceof Error ? error.message : String(error)}`); } finally { setBusy(false); }
  };

  const freeze = () => guard("freeze", async () => {
    if (!projectId || !datasetId) throw new Error("no dataset");
    const record = await api.freezeIdentification(projectId, datasetId, selection, 20);
    return `Identification protocol ${shortHash(record.protocol_id)}: gallery ${record.gallery_count}, mated probes ${record.mated_probe_count}, non-mated probes ${record.nonmated_probe_count}`;
  });
  const search = (protocolId: string) => guard("search", async () => {
    if (!projectId) throw new Error("no project");
    // Probe-quality effects need the dataset-scale quality batch, not a single-photo upload job.
    const qualityJob = jobs.reduce<JobOut | null>((best, job) => (best === null || job.assessed > best.assessed ? job : best), null);
    const started = await api.startIdentificationSearch(projectId, protocolId, { matcher_id: matcher, condition_id: condition, quality_job_id: qualityJob?.job_id ?? null });
    setRunningSearch(started.search_id);
    return `Identification search ${shortHash(started.search_id)} running on the matcher device`;
  });
  const openSearch = (searchId: string) => guard("open", async () => {
    const status = await api.identificationSearch(searchId);
    setSearchDetail(status); setCandidates(null);
    return `Search ${shortHash(searchId)} loaded`;
  });
  const showCandidates = (kind: string) => guard("candidates", async () => {
    if (!searchDetail) throw new Error("no search");
    const body = await api.identificationCandidates(searchDetail.search_id, `probe_kind=${kind}&rank_le=5&limit=100`);
    setCandidates(body.rows);
    return `${body.total} candidate rows (${kind}, rank ≤ 5)`;
  });
  const derive = () => guard("transform", async () => {
    if (!projectId || !datasetId) throw new Error("no dataset");
    const transforms = transformSpec.split(";").map((part) => part.trim()).filter(Boolean).map((part) => {
      const [kind, params] = part.split(":");
      return { kind: (kind ?? "").trim(), parameters: (params ?? "").split(",").map((v) => Number(v.trim())).filter((v) => !Number.isNaN(v)) };
    });
    const record = await api.createTransformations(projectId, datasetId, { selection, transforms });
    return `Derived set ${shortHash(record.derived_set_id)}: ${record.derived_count} images (${record.levels} levels × ${record.source_count} sources); run a quality batch on dataset ${shortHash(record.derived_dataset_id)} next`;
  });
  const analyse = (set: DerivedSetOut) => guard("degradation", async () => {
    if (!projectId) throw new Error("no project");
    const current = (await api.projectJobs(projectId)).filter((job) => job.state === "COMPLETED" && job.run_id);
    const derivedJob = current.find((job) => job.dataset_id === set.derived_dataset_id) ?? null;
    const baseline = current.find((job) => job.dataset_id === set.source_dataset_id) ?? null;
    if (!derivedJob) throw new Error("run the quality batch over the derived dataset first (Dataset panel → derived dataset)");
    const started = await api.startDegradation(projectId, { derived_set_id: set.derived_set_id, quality_job_id: derivedJob.job_id, baseline_quality_job_id: baseline?.job_id ?? null, matcher_id: matcher, condition_id: condition, matcher_threshold: Number(threshold) });
    setRunningDegradation(started.analysis_id);
    return `Degradation analysis ${shortHash(started.analysis_id)} running`;
  });
  const openDegradation = (id: string) => guard("open", async () => { setDegradationDetail(await api.degradation(id)); return `Degradation ${shortHash(id)} loaded`; });
  const qualityOverDerived = (set: DerivedSetOut) => guard("batch", async () => {
    if (!projectId) throw new Error("no project");
    const job = await api.startBatch(projectId, set.derived_dataset_id, { kind: "all" }, 1, null);
    return `OFIQ batch ${shortHash(job.job_id)} started over ${set.derived_count} derived images; analyse when it completes`;
  });

  if (!projectId) {
    return (
      <section className="panel advanced" aria-labelledby="advanced-heading" data-testid="advanced">
        <h2 id="advanced-heading">Advanced analysis · 1:N identification · controlled degradation</h2>
        <p className="empty">Select a project with an imported dataset.</p>
      </section>
    );
  }
  const closed = searchDetail?.metrics?.closed_set?.EXCLUDE_FAILED_TRIALS;
  const openSet = searchDetail?.metrics?.open_set?.EXCLUDE_FAILED_TRIALS;
  return (
    <section className="panel advanced" aria-labelledby="advanced-heading" data-testid="advanced">
      <h2 id="advanced-heading">Advanced analysis · 1:N identification · controlled degradation</h2>
      <div className="controls-row utility-controls">
        <label>Selection<select value={selection} onChange={(e) => setSelection(e.target.value as "all" | "subset_fixture")} data-testid="adv-selection"><option value="subset_fixture">subset fixture</option><option value="all">all samples</option></select></label>
        <label>Matcher<select value={matcher} onChange={(e) => setMatcher(e.target.value)} data-testid="adv-matcher">{MATCHERS.map((m) => <option key={m} value={m}>{m}</option>)}</select></label>
        <label>Condition<select value={condition} onChange={(e) => setCondition(e.target.value)} data-testid="adv-condition">{CONDITIONS.map((c) => <option key={c} value={c}>{c.includes("INMEMORY") ? "native" : "shared"}</option>)}</select></label>
        <button type="button" className="secondary" disabled={!datasetId || busy} onClick={() => void freeze()} data-testid="ident-freeze">Freeze 1:N protocol</button>
      </div>
      <div className="utility-facts">
        <div>
          <h4>1:N protocols and searches</h4>
          <table className="compact" data-testid="ident-protocols"><tbody>
            {protocols.map((p) => (
              <tr key={p.protocol_id}><td className="mono">{shortHash(p.protocol_id, 10)}</td><td>{p.selection} · gallery {p.gallery_count} · mated {p.mated_probe_count} · non-mated {p.nonmated_probe_count} · depth {p.rank_depth}</td><td><button type="button" className="secondary" disabled={busy || runningSearch !== null} onClick={() => void search(p.protocol_id)} data-testid="ident-search">Search</button></td></tr>
            ))}
          </tbody></table>
          <table className="compact" data-testid="ident-searches"><thead><tr><th>Search</th><th>Matcher · crop · device</th><th>Gallery</th><th>Mated / non-mated</th><th>Rank-1</th><th>FNIR @ FPIR 1 %</th></tr></thead><tbody>
            {searches.map((s) => (
              <tr key={s.search_id}><td><button type="button" className="cell" onClick={() => void openSearch(s.search_id)} data-testid="ident-open"><span className="mono">{shortHash(s.search_id, 10)}</span></button></td><td>{s.matcher_id.split("-local")[0]} · {s.condition_id.includes("INMEMORY") ? "native" : "shared"} · {s.device}</td><td className="mono">{s.gallery_size}</td><td className="mono">{s.mated_searches} / {s.nonmated_searches}</td><td className="mono">{pct(s.rank_1)}</td><td className="mono">{pct(s.fnir_at_fpir_0_01)}</td></tr>
            ))}
          </tbody></table>
          {searchDetail?.metrics && (
            <div className="errors" data-testid="ident-detail">
              <h3>Search <span className="mono">{shortHash(searchDetail.search_id, 12)}</span> · Run <a href={`/api/runs/${searchDetail.run_id}`} className="mono">{shortHash(searchDetail.run_id ?? "", 12)}</a></h3>
              <div className="utility-facts">
                <div>
                  <h4>Closed set (mated searches)</h4>
                  <table className="compact"><tbody>
                    {Object.entries(closed?.rank_k ?? {}).map(([k, block]: [string, any]) => <tr key={k}><th>rank-{k}</th><td className="mono">{pct(block.rate)} [{pct(block.ci_lower)}, {pct(block.ci_upper)}] · {block.events}/{block.trials}</td></tr>)}
                  </tbody></table>
                  <svg className="det" viewBox="0 0 320 200" role="img" aria-label="CMC" data-testid="cmc-plot">
                    <rect x="40" y="10" width="270" height="160" className="det-bg" />
                    {closed?.cmc?.points && <polyline fill="none" stroke="#7dd3fc" strokeWidth={2} points={closed.cmc.points.map((p: any, i: number) => `${40 + (i / Math.max(1, closed.cmc.points.length - 1)) * 270},${170 - (p.rate ?? 0) * 160}`).join(" ")} />}
                    <text x="175" y="192" textAnchor="middle" className="axis">rank (1 … {closed?.cmc?.points?.length ?? 0})</text>
                    <text x="12" y="90" textAnchor="middle" transform="rotate(-90 12 90)" className="axis">identification rate</text>
                  </svg>
                  <p className="hint">True-mate rank distribution: {Object.entries(closed?.true_mate_rank_distribution ?? {}).slice(0, 8).map(([k, v]) => `${k}:${v}`).join(" · ")}</p>
                </div>
                <div>
                  <h4>Open set (FNIR at target FPIR; MATCHER THRESHOLD from non-mated searches)</h4>
                  <table className="compact"><thead><tr><th>Target FPIR</th><th>Threshold</th><th>FPIR</th><th>FNIR</th><th>TPIR</th><th>Support</th></tr></thead><tbody>
                    {(openSet?.operating_points ?? []).map((p: any) => <tr key={p.target_fpir}><td className="mono">{p.target_fpir}</td><td className="mono">{p.threshold === null ? p.reason : num(p.threshold, 4)}</td><td className="mono">{pct(p.fpir, 3)}</td><td className="mono">{pct(p.fnir)}</td><td className="mono">{pct(p.tpir)}</td><td>{p.supported ? "supported" : "unsupported precision"}</td></tr>)}
                  </tbody></table>
                  <h4>Gallery-size effect</h4>
                  <table className="compact"><thead><tr><th>Gallery</th><th>Mated searches</th><th>Rank-1</th><th>FNIR @ 1 %</th></tr></thead><tbody>
                    {(searchDetail.metrics.gallery_effects?.gallery_sizes ?? []).map((g: any) => <tr key={g.gallery_size}><td className="mono">{g.gallery_size}</td><td className="mono">{g.mated_searches}</td><td className="mono">{pct(g.rank_1)}</td><td className="mono">{pct(g.fnir_at_fpir_0_01)}</td></tr>)}
                  </tbody></table>
                  {searchDetail.metrics.probe_quality_effect && <p className="hint">Rank-1 by probe UQS bin: {searchDetail.metrics.probe_quality_effect.bins.map((b: any) => `[${b.quality_low},${b.quality_high}) ${pct(b.rank_1, 1)} (n=${b.mated_searches})`).join(" · ")}</p>}
                  <div className="controls-row"><button type="button" className="secondary" onClick={() => void showCandidates("MATED")} data-testid="ident-candidates">Mated candidate lists</button><button type="button" className="secondary" onClick={() => void showCandidates("NON_MATED")}>Non-mated candidate lists</button></div>
                  {candidates && <table className="compact" data-testid="ident-candidate-table"><thead><tr><th>Probe</th><th>Rank</th><th>Gallery</th><th>Score</th><th>Mate?</th></tr></thead><tbody>{candidates.slice(0, 40).map((row) => <tr key={`${row.probe_sample_id}-${row.rank}`}><td className="mono">{row.probe_subject_id}</td><td className="mono">{row.rank}</td><td className="mono">{row.gallery_subject_id}</td><td className="mono">{num(row.score, 4)}</td><td>{row.is_true_mate ? "true mate" : ""}</td></tr>)}</tbody></table>}
                </div>
              </div>
            </div>
          )}
        </div>
        <div>
          <h4>Controlled degradation (transform real inputs → OFIQ + matcher response)</h4>
          <div className="controls-row utility-controls">
            <label>Transforms<input value={transformSpec} onChange={(e) => setTransformSpec(e.target.value)} style={{ minWidth: 300 }} data-testid="transform-spec" /></label>
            <label>Matcher T<input value={threshold} onChange={(e) => setThreshold(e.target.value)} data-testid="degradation-threshold" /></label>
            <button type="button" className="secondary" disabled={!datasetId || busy} onClick={() => void derive()} data-testid="transform-run">Create derived set</button>
          </div>
          <table className="compact" data-testid="derived-sets"><tbody>
            {derivedSets.map((set) => (
              <tr key={set.derived_set_id}><td className="mono">{shortHash(set.derived_set_id, 10)}</td><td>{set.derived_count} images · {set.levels} levels × {set.source_count} sources · derived dataset {shortHash(set.derived_dataset_id, 8)}</td><td className="controls-row"><button type="button" className="secondary" disabled={busy} onClick={() => void qualityOverDerived(set)} data-testid="derived-batch">OFIQ over derived</button><button type="button" className="secondary" disabled={busy || runningDegradation !== null} onClick={() => void analyse(set)} data-testid="degradation-run">Analyse response</button></td></tr>
            ))}
          </tbody></table>
          <table className="compact" data-testid="degradations"><thead><tr><th>Analysis</th><th>Matcher</th><th>Transforms</th><th>Pairs</th></tr></thead><tbody>
            {degradations.map((d) => (
              <tr key={d.analysis_id}><td><button type="button" className="cell" onClick={() => void openDegradation(d.analysis_id)} data-testid="degradation-open"><span className="mono">{shortHash(d.analysis_id, 10)}</span></button></td><td>{d.matcher_id.split("-local")[0]} · T {num(d.matcher_threshold, 4)}</td><td>{d.transforms.join(", ")}</td><td className="mono">{d.mated_pairs} mated · {d.nonmated_pairs} non-mated</td></tr>
            ))}
          </tbody></table>
          {degradationDetail?.document && (
            <div className="errors" data-testid="degradation-detail">
              <h3>Degradation response · Run <a href={`/api/runs/${degradationDetail.run_id}`} className="mono">{shortHash(degradationDetail.run_id ?? "", 12)}</a> · MATCHER THRESHOLD {num(degradationDetail.document.matcher_threshold.value, 4)}</h3>
              {degradationDetail.document.responses.map((r: any) => (
                <table className="compact" key={r.transform} data-testid="degradation-table"><thead><tr><th>{r.transform} ({r.parameter_name})</th><th>UQS mean</th><th>Sharpness mean</th><th>Mated score mean</th><th>FNMR</th><th>FMR</th><th>Template failures</th></tr></thead><tbody>
                  <tr><td>baseline (originals)</td><td className="mono">{num(r.baseline.quality?.UnifiedQualityScore, 1)}</td><td className="mono">{num(r.baseline.quality?.Sharpness, 1)}</td><td className="mono">{num(r.baseline.mated_score_mean, 3)}</td><td className="mono">{pct(r.baseline.fnmr.rate)}</td><td className="mono">{pct(r.baseline.fmr.rate, 3)}</td><td>—</td></tr>
                  {r.levels.map((l: any) => <tr key={l.parameter}><td className="mono">{l.parameter}</td><td className="mono">{num(l.quality.UnifiedQualityScore, 1)}</td><td className="mono">{num(l.quality.Sharpness, 1)}</td><td className="mono">{num(l.mated_score_mean, 3)}</td><td className="mono">{pct(l.fnmr.rate)} [{pct(l.fnmr.ci_lower)}, {pct(l.fnmr.ci_upper)}]</td><td className="mono">{pct(l.fmr.rate, 3)}</td><td className="mono">{l.template_failures}</td></tr>)}
                  <tr><td colSpan={7} className="hint">FNMR slope/level {num(r.derived_summaries.fnmr.slope_per_level, 4)} · area {num(r.derived_summaries.fnmr.area_under_response, 4)} · crossing at {String(r.derived_summaries.fnmr.threshold_crossing.parameter ?? "—")} · failure onset level {String(r.derived_summaries.failure_onset.first_level_index ?? "—")} · UQS relative change {num(r.derived_summaries.unified_quality_score.relative_change_last_level, 3)}</td></tr>
                </tbody></table>
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

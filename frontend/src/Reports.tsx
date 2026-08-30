import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, type BundleOut, type DashboardOut, type FigureOut, type JobOut, type MatcherLeaf, type ReportOut, type ReproductionOut, type UtilitySummary } from "./api";

function shortHash(value: string, keep = 12): string {
  const marker = value.indexOf("sha256:");
  const digest = marker >= 0 ? value.slice(marker + 7) : value;
  return `${digest.slice(0, keep)}…`;
}

interface ReportsProps {
  projectId: string | null;
  datasetId: string | null;
  onMessage: (message: string) => void;
}

/** Figures, dashboards, reports, exports, bundles, and reproduction (QS-F50..F53). */
export function Reports({ projectId, datasetId, onMessage }: ReportsProps) {
  const contextKey = `${projectId ?? ""}\u001f${datasetId ?? ""}`;
  const [leaves, setLeaves] = useState<MatcherLeaf[]>([]);
  const [jobs, setJobs] = useState<JobOut[]>([]);
  const [analyses, setAnalyses] = useState<UtilitySummary[]>([]);
  const [figures, setFigures] = useState<FigureOut[]>([]);
  const [dashboards, setDashboards] = useState<DashboardOut[]>([]);
  const [reports, setReports] = useState<ReportOut[]>([]);
  const [bundles, setBundles] = useState<BundleOut[]>([]);
  const [reproductions, setReproductions] = useState<ReproductionOut[]>([]);
  const [selectedFigure, setSelectedFigure] = useState<FigureOut | null>(null);
  const [loadedDashboard, setLoadedDashboard] = useState<any | null>(null);
  const [imagePolicy, setImagePolicy] = useState<"EXCLUDE" | "INCLUDE_PERMITTED">("EXCLUDE");
  const [busy, setBusy] = useState<string | null>(null);
  const [feedPhase, setFeedPhase] = useState<"loading" | "ready" | "error">("loading");
  const [feedError, setFeedError] = useState<string | null>(null);
  const contextEpoch = useRef(0);
  const contextKeyRef = useRef(contextKey);
  const refreshSequence = useRef(0);

  const refresh = useCallback(async (showLoading = false) => {
    const epoch = contextEpoch.current;
    const requestContextKey = contextKey;
    const sequence = ++refreshSequence.current;
    if (!projectId) return;
    if (showLoading) setFeedPhase("loading");
    setFeedError(null);
    const results = await Promise.allSettled([
      api.matcherLeaves(projectId), api.projectJobs(projectId), api.utilityAnalyses(projectId), api.figures(projectId),
      api.dashboards(projectId), api.reports(projectId), api.bundles(projectId), api.reproductions(projectId),
    ]);
    if (contextEpoch.current !== epoch || contextKeyRef.current !== requestContextKey || refreshSequence.current !== sequence) return;
    const [leafResult, jobResult, analysisResult, figureResult, dashboardResult, reportResult, bundleResult, reproductionResult] = results;
    if (leafResult.status === "fulfilled") setLeaves(leafResult.value); else setLeaves([]);
    if (jobResult.status === "fulfilled") setJobs(jobResult.value.filter((job) => job.state === "COMPLETED" && job.run_id)); else setJobs([]);
    if (analysisResult.status === "fulfilled") setAnalyses(analysisResult.value); else setAnalyses([]);
    if (figureResult.status === "fulfilled") setFigures(figureResult.value); else setFigures([]);
    if (dashboardResult.status === "fulfilled") setDashboards(dashboardResult.value); else setDashboards([]);
    if (reportResult.status === "fulfilled") setReports(reportResult.value); else setReports([]);
    if (bundleResult.status === "fulfilled") setBundles(bundleResult.value); else setBundles([]);
    if (reproductionResult.status === "fulfilled") setReproductions(reproductionResult.value); else setReproductions([]);
    const names = ["matcher Runs", "quality jobs", "utility analyses", "figures", "dashboards", "reports", "bundles", "reproductions"];
    const failures = results.flatMap((result, index) => result.status === "rejected" ? [`${names[index]}: ${result.reason instanceof Error ? result.reason.message : String(result.reason)}`] : []);
    setFeedError(failures.length ? `Some report evidence is unavailable. ${failures.join(" · ")}` : null);
    setFeedPhase(failures.length === results.length ? "error" : "ready");
  }, [projectId, contextKey]);

  useLayoutEffect(() => {
    contextKeyRef.current = contextKey;
    contextEpoch.current += 1;
    refreshSequence.current += 1;
    setLeaves([]); setJobs([]); setAnalyses([]); setFigures([]); setDashboards([]); setReports([]); setBundles([]); setReproductions([]);
    setSelectedFigure(null); setLoadedDashboard(null);
    setBusy(null); setFeedError(null); setFeedPhase(projectId ? "loading" : "ready");
  }, [projectId, contextKey]);

  useEffect(() => {
    void refresh(true);
    const handle = window.setInterval(() => void refresh(false), 5000);
    return () => window.clearInterval(handle);
  }, [refresh]);

  const datasetLeaves = useMemo(
    () => datasetId ? leaves.filter((leaf) => leaf.dataset_id === datasetId) : [],
    [datasetId, leaves],
  );
  const datasetJobs = useMemo(
    () => datasetId ? jobs.filter((job) => job.dataset_id === datasetId) : [],
    [datasetId, jobs],
  );
  const datasetLeafRunIds = useMemo(() => new Set(datasetLeaves.map((leaf) => leaf.run_id)), [datasetLeaves]);
  const datasetJobIds = useMemo(() => new Set(datasetJobs.map((job) => job.job_id)), [datasetJobs]);
  const datasetAnalyses = useMemo(
    () => analyses.filter((analysis) => datasetLeafRunIds.has(analysis.matcher_run_id) && datasetJobIds.has(analysis.quality_job_id)),
    [analyses, datasetJobIds, datasetLeafRunIds],
  );
  const datasetRunIds = useMemo(() => new Set([
    ...datasetLeaves.map((leaf) => leaf.run_id),
    ...datasetJobs.flatMap((job) => job.run_id ? [job.run_id] : []),
    ...datasetAnalyses.map((analysis) => analysis.run_id),
  ]), [datasetAnalyses, datasetJobs, datasetLeaves]);
  const datasetFigures = useMemo(
    () => figures.filter((figure) => figure.source_run_ids.length > 0 && figure.source_run_ids.every((runId) => datasetRunIds.has(runId))),
    [datasetRunIds, figures],
  );
  const datasetDashboards = useMemo(
    () => dashboards.filter((dashboard) => dashboard.panel_run_ids.length > 0 && dashboard.panel_run_ids.every((runId) => datasetRunIds.has(runId))),
    [dashboards, datasetRunIds],
  );
  const datasetReports = useMemo(
    () => reports.filter((report) => report.dataset_id === datasetId),
    [datasetId, reports],
  );
  const datasetBundles = useMemo(
    () => bundles.filter((bundle) => datasetRunIds.has(bundle.run_id)),
    [bundles, datasetRunIds],
  );
  const datasetReproductions = useMemo(
    () => reproductions.filter((reproduction) => datasetRunIds.has(reproduction.original_run_id)),
    [datasetRunIds, reproductions],
  );
  const latestMatrix = datasetLeaves.length ? datasetLeaves[datasetLeaves.length - 1]!.matrix_id : null;
  // Prefer the largest completed batch from the active dataset; never mix project datasets.
  const latestJob = useMemo(
    () => datasetJobs.reduce<JobOut | null>((best, job) => (best === null || job.assessed > best.assessed ? job : best), null),
    [datasetJobs],
  );

  const run = useCallback(async (label: string, action: (epoch: number) => Promise<string>) => {
    const epoch = contextEpoch.current;
    setBusy(label);
    try {
      const message = await action(epoch);
      if (contextEpoch.current !== epoch) return;
      onMessage(message);
      void refresh();
    } catch (error) {
      if (contextEpoch.current === epoch) onMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (contextEpoch.current === epoch) setBusy(null);
    }
  }, [onMessage, refresh]);

  const makeFigure = (kind: string, parameters: Record<string, unknown>) => run(kind, async (epoch) => {
    if (!projectId) throw new Error("no project");
    const figure = await api.createFigure(projectId, kind, parameters);
    if (contextEpoch.current !== epoch) return "";
    setSelectedFigure(figure);
    return `Figure ${figure.kind} ${shortHash(figure.figure_id)} recorded (${figure.source_run_ids.length} source Runs)`;
  });

  const saveDashboard = () => run("dashboard", async (epoch) => {
    if (!projectId) throw new Error("no project");
    const panels: Array<{ kind: string; ref: string }> = datasetFigures.slice(-4).map((figure) => ({ kind: "figure", ref: figure.figure_id }));
    if (datasetLeaves.length) panels.push({ kind: "operating_points", ref: datasetLeaves[datasetLeaves.length - 1]!.run_id });
    if (datasetAnalyses.length) panels.push({ kind: "policy_table", ref: datasetAnalyses[datasetAnalyses.length - 1]!.analysis_id });
    if (!panels.length) throw new Error("no figures or runs to place on a dashboard");
    const saved = await api.saveDashboard(projectId, { name: "Console dashboard", template: "FIQA Evaluation", columns: 2, panels });
    const loaded = await api.dashboard(saved.dashboard_id);
    if (contextEpoch.current !== epoch) return "";
    setLoadedDashboard(loaded);
    return `Dashboard ${shortHash(saved.dashboard_id)} saved with ${panels.length} panels and reloaded (${loaded.layout_sha256 === saved.layout_sha256 ? "layout identical" : "layout differs"})`;
  });

  const openDashboard = (dashboardId: string) => run("dashboard-open", async (epoch) => {
    const loaded = await api.dashboard(dashboardId);
    if (contextEpoch.current !== epoch) return "";
    setLoadedDashboard(loaded);
    return `Dashboard ${shortHash(dashboardId)} reloaded: ${loaded.panels.length} panels`;
  });

  const buildReport = () => run("report", async () => {
    if (!projectId) throw new Error("no project");
    const spec: Record<string, unknown> = {
      title: "QualSight console report", executive_summary: "Report assembled from the recorded Runs of this project.", image_policy: imagePolicy,
      dataset_id: datasetId ?? undefined, quality_job_id: latestJob?.job_id, matrix_id: latestMatrix ?? undefined,
      protocol_id: datasetLeaves.length ? datasetLeaves[datasetLeaves.length - 1]!.protocol_id : undefined,
      utility_analysis_ids: datasetAnalyses.map((item) => item.analysis_id).slice(-6), figure_ids: datasetFigures.slice(-4).map((figure) => figure.figure_id),
      failure_examples: datasetAnalyses.length ? { analysis_id: datasetAnalyses[datasetAnalyses.length - 1]!.analysis_id, limit: 4 } : undefined,
    };
    Object.keys(spec).forEach((key) => spec[key] === undefined && delete spec[key]);
    const report = await api.createReport(projectId, spec);
    return `Report ${shortHash(report.report_id)} built (${report.images_embedded ?? 0} images embedded, ${report.images_withheld ?? 0} withheld)`;
  });

  const exportBundle = (runId: string) => run("bundle", async () => {
    if (!projectId) throw new Error("no project");
    const bundle = await api.createBundle(projectId, runId);
    return `Bundle ${shortHash(bundle.bundle_id)} exported (${bundle.size_bytes ?? "?"} bytes)`;
  });

  const reproduce = (bundle: BundleOut) => run("reproduce", async () => {
    const result = await api.reproduce(bundle.path);
    return `Reproduction of ${shortHash(bundle.run_id)}: ${result.state} (${result.compared_values} values compared, ${result.differences.length} differences)`;
  });

  if (!projectId) {
    return (
      <section className="panel reports" aria-labelledby="reports-heading" data-testid="reports">
        <h2 id="reports-heading">Figures · dashboards · reports · exports · reproduction</h2>
        <div className="empty-state">
          <p>Select a project with recorded Runs.</p>
          <a className="button primary" href="#/projects">Open projects</a>
        </div>
      </section>
    );
  }

  return (
    <section className="panel reports" aria-labelledby="reports-heading" data-testid="reports">
      <h2 id="reports-heading">Figures · dashboards · reports · exports · reproduction</h2>
      {feedPhase === "loading" && <p className="status" role="status">Loading recorded report evidence…</p>}
      {feedError && <p className="error" role="alert">{feedError} <button type="button" className="secondary" onClick={() => void refresh(true)}>Retry</button></p>}
      <p className="hint" id="report-prerequisites">DET and operating-point figures require a completed verification matrix. ERC figures require utility analysis. A quality histogram requires a completed batch. Disabled actions become available when their recorded prerequisite exists.</p>
      <div className="controls-row utility-controls">
        <button type="button" className="secondary" aria-describedby="report-prerequisites" title={!latestMatrix ? "Complete a verification matrix first" : busy ? `Wait for ${busy} to finish` : undefined} disabled={!latestMatrix || busy !== null} onClick={() => void makeFigure("det_overlay", { matrix_id: latestMatrix })} data-testid="figure-det">DET overlay</button>
        <button type="button" className="secondary" aria-describedby="report-prerequisites" title={!latestMatrix ? "Complete a verification matrix first" : busy ? `Wait for ${busy} to finish` : undefined} disabled={!latestMatrix || busy !== null} onClick={() => void makeFigure("operating_points", { matrix_id: latestMatrix })} data-testid="figure-op">Operating points</button>
        <button type="button" className="secondary" aria-describedby="report-prerequisites" title={!datasetAnalyses.length ? "Complete a utility analysis for the active dataset first" : busy ? `Wait for ${busy} to finish` : undefined} disabled={!datasetAnalyses.length || busy !== null} onClick={() => void makeFigure("erc_overlay", { analysis_ids: datasetAnalyses.map((item) => item.analysis_id).slice(-6) })} data-testid="figure-erc">ERC overlay</button>
        <button type="button" className="secondary" aria-describedby="report-prerequisites" title={!datasetAnalyses.length ? "Complete a utility analysis for the active dataset first" : busy ? `Wait for ${busy} to finish` : undefined} disabled={!datasetAnalyses.length || busy !== null} onClick={() => void makeFigure("quality_conditioned_fnmr", { analysis_id: datasetAnalyses[datasetAnalyses.length - 1]?.analysis_id })} data-testid="figure-cond">FNMR | quality bin</button>
        <button type="button" className="secondary" aria-describedby="report-prerequisites" title={!latestJob ? "Complete a quality batch first" : busy ? `Wait for ${busy} to finish` : undefined} disabled={!latestJob || busy !== null} onClick={() => void makeFigure("quality_histogram", { job_id: latestJob?.job_id })} data-testid="figure-hist">Quality histogram</button>
        <button type="button" className="secondary" aria-describedby="report-prerequisites" title={!datasetFigures.length && !datasetLeaves.length && !datasetAnalyses.length ? "Record evidence for the active dataset first" : busy ? `Wait for ${busy} to finish` : undefined} disabled={(!datasetFigures.length && !datasetLeaves.length && !datasetAnalyses.length) || busy !== null} onClick={() => void saveDashboard()} data-testid="dashboard-save">Save dashboard</button>
        <label>
          Report images
          <select value={imagePolicy} onChange={(event) => setImagePolicy(event.target.value as "EXCLUDE" | "INCLUDE_PERMITTED")} data-testid="report-image-policy">
            <option value="EXCLUDE">EXCLUDE</option>
            <option value="INCLUDE_PERMITTED">INCLUDE_PERMITTED</option>
          </select>
        </label>
        <button type="button" className="primary" aria-describedby="report-prerequisites" title={!datasetId ? "Select an active dataset first" : !latestMatrix && !latestJob ? "Complete a verification matrix or quality batch for the active dataset first" : busy ? `Wait for ${busy} to finish` : undefined} disabled={busy !== null || !datasetId || (!latestMatrix && !latestJob)} onClick={() => void buildReport()} data-testid="report-build">Build report (HTML + PDF)</button>
      </div>

      <div className="utility-facts">
        <div>
          <h3>Recorded figures for active dataset ({datasetFigures.length})</h3>
          <ul className="thumb-list" data-testid="figure-list">
            {datasetFigures.map((figure) => (
              <li key={figure.figure_id}>
                <button type="button" className="thumb" onClick={() => setSelectedFigure(figure)} data-testid="figure-thumb">
                  <img src={figure.files.png} alt={figure.title} loading="lazy" style={{ width: 120, height: 80, objectFit: "contain" }} />
                  <span className="sample-meta">{figure.kind}</span>
                </button>
              </li>
            ))}
          </ul>
          {selectedFigure && (
            <div className="errors" data-testid="figure-detail">
              <h3>{selectedFigure.title}</h3>
              <img src={selectedFigure.files.png} alt={selectedFigure.title} style={{ maxWidth: "100%" }} />
              <dl className="facts">
                <dt>Figure</dt><dd className="mono">{selectedFigure.figure_id}</dd>
                <dt>Source Runs</dt><dd className="mono">{selectedFigure.source_run_ids.map((id) => shortHash(id, 10)).join(", ")}</dd>
                <dt>Metric definitions</dt><dd className="mono">{selectedFigure.metric_definitions.map((d) => d.join("@")).join(", ") || "—"}</dd>
                <dt>Thresholds</dt><dd className="mono">{selectedFigure.thresholds.length} recorded (kind {selectedFigure.thresholds[0]?.kind ?? "—"})</dd>
                <dt>Export</dt><dd className="links"><a href={selectedFigure.files.png}>PNG</a><a href={selectedFigure.files.svg}>SVG</a><a href={selectedFigure.files.pdf}>PDF</a><a href={selectedFigure.files.csv}>CSV</a><a href={selectedFigure.files.provenance}>provenance</a></dd>
              </dl>
            </div>
          )}
        </div>
        <div>
          <h3>Active-dataset dashboards ({datasetDashboards.length}) · reports ({datasetReports.length})</h3>
          <table className="compact" data-testid="dashboard-table">
            <caption>Recorded dashboards for the active dataset</caption>
            <thead><tr><th scope="col">Dashboard</th></tr></thead>
            <tbody>
              {datasetDashboards.map((item) => (
                <tr key={item.dashboard_id}><th scope="row"><button type="button" className="cell" onClick={() => void openDashboard(item.dashboard_id)} data-testid="dashboard-open"><span className="mono">{shortHash(item.dashboard_id, 10)}</span> · {item.name} v{item.version} · {String(item.panels)} panels</button></th></tr>
              ))}
            </tbody>
          </table>
          {datasetDashboards.length === 0 && <p className="empty">No dashboard with fully recorded lineage belongs to the active dataset.</p>}
          {loadedDashboard && (
            <div className="errors" data-testid="dashboard-detail">
              <h3>{loadedDashboard.name} · v{loadedDashboard.version} · layout sha {shortHash(loadedDashboard.layout_sha256 ?? "", 10)}</h3>
              <div className="dashboard-grid" style={{ gridTemplateColumns: `repeat(${loadedDashboard.layout.columns}, 1fr)` }}>
                {loadedDashboard.panels.map((panel: any, index: number) => (
                  <div key={`${panel.kind}-${index}`} className="dashboard-panel" data-testid="dashboard-panel">
                    <strong>{panel.title}</strong>
                    {panel.kind === "figure" && <img src={panel.data.figure.files.png} alt={panel.title} style={{ maxWidth: "100%" }} />}
                    {panel.kind !== "figure" && <pre className="mono" style={{ maxHeight: 140, overflow: "auto" }}>{JSON.stringify(panel.data, null, 1).slice(0, 1200)}</pre>}
                    <span className="sample-meta">Runs: {panel.run_ids.map((id: string) => shortHash(id, 8)).join(", ") || "—"}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
          <table className="compact" data-testid="report-table">
            <caption>Recorded reports for the active dataset</caption>
            <thead><tr><th scope="col">Report</th><th scope="col">Title</th><th scope="col">Downloads</th></tr></thead>
            <tbody>
              {datasetReports.map((item) => (
                <tr key={item.report_id}>
                  <th scope="row" className="mono">{shortHash(item.report_id, 10)}</th>
                  <td>{item.title}</td>
                  <td className="links">
                    {(["html", "pdf", "json"] as const).map((format) => item.downloads[format].href
                      ? <a key={format} href={item.downloads[format].href ?? undefined} target={format === "html" ? "_blank" : undefined} rel={format === "html" ? "noreferrer" : undefined}>{format.toUpperCase()}</a>
                      : <span key={format} className="hint">{format.toUpperCase()} unavailable: {item.downloads[format].reason}</span>)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {datasetReports.length === 0 && <p className="empty">No report explicitly records the active dataset.</p>}
        </div>
      </div>

      <div className="utility-facts">
        <div>
          <h3>Exports (CSV · JSON · Parquet, with run identifiers)</h3>
          <p id="report-export-prerequisites" className="hint">Bundle export and reproduction are available when no other report operation is running.</p>
          <ul className="legend">
            {latestJob && <li>quality results · <a href={`/api/exports/quality_results?ref=${encodeURIComponent(latestJob.job_id)}&format=csv`}>CSV</a> <a href={`/api/exports/quality_results?ref=${encodeURIComponent(latestJob.job_id)}&format=parquet`}>Parquet</a></li>}
            {datasetLeaves.slice(-6).map((leaf) => (
              <li key={leaf.run_id}>{leaf.leaf_id} comparison records · <a href={`/api/exports/comparison_records?ref=${encodeURIComponent(leaf.run_id)}&format=csv`}>CSV</a> {leaf.condition_id !== "EXTERNALLY_COMPUTED" && (<><a href={`/api/exports/embeddings?ref=${encodeURIComponent(leaf.run_id)}&format=parquet`}>embeddings</a>{" "}</>)}<a href={`/api/exports/metric_tables?ref=${encodeURIComponent(leaf.run_id)}&format=json`}>metrics</a> · <button type="button" className="secondary" disabled={busy !== null} aria-describedby="report-export-prerequisites" title={busy ? `Wait for ${busy} to finish` : "Export this recorded Run as a reproducible bundle"} onClick={() => void exportBundle(leaf.run_id)} data-testid="bundle-export">bundle</button></li>
            ))}
            {datasetAnalyses.slice(-6).map((item) => (
              <li key={item.analysis_id}>{item.leaf_id} utility statistics · <a href={`/api/exports/statistical_results?ref=${encodeURIComponent(item.analysis_id)}&format=csv`}>CSV</a> · <button type="button" className="secondary" disabled={busy !== null} aria-describedby="report-export-prerequisites" title={busy ? `Wait for ${busy} to finish` : "Export this recorded analysis Run as a reproducible bundle"} onClick={() => void exportBundle(item.run_id)} data-testid="bundle-export-analysis">bundle</button></li>
            ))}
            <li>annotations · <a href={`/api/exports/annotations?ref=${encodeURIComponent(projectId)}&format=json`}>JSON</a></li>
          </ul>
        </div>
        <div>
          <h3>Active-dataset bundles ({datasetBundles.length}) · reproductions ({datasetReproductions.length})</h3>
          <table className="compact" data-testid="bundle-table">
            <caption>Recorded export bundles for the active dataset</caption>
            <thead><tr><th scope="col">Bundle</th><th scope="col">Source Run</th><th scope="col">Download</th><th scope="col">Reproduction</th></tr></thead>
            <tbody>
              {datasetBundles.map((bundle) => (
                <tr key={bundle.bundle_id}>
                  <th scope="row" className="mono">{shortHash(bundle.bundle_id, 10)}</th><td className="mono">run {shortHash(bundle.run_id, 10)}</td>
                  <td>{bundle.download.href ? <a href={bundle.download.href}>.qsexp</a> : <span className="hint">Download unavailable: {bundle.download.reason}</span>}</td>
                  <td><button type="button" className="secondary" disabled={busy !== null || bundle.download.state !== "VERIFIED"} aria-describedby={bundle.download.state === "VERIFIED" ? "report-export-prerequisites" : `bundle-unavailable-${bundle.bundle_id.replace(/[^a-zA-Z0-9_-]/g, "-")}`} title={bundle.download.state !== "VERIFIED" ? bundle.download.reason ?? "The bundle is unavailable" : busy ? `Wait for ${busy} to finish` : "Reproduce this recorded bundle and compare its values"} onClick={() => void reproduce(bundle)} data-testid="reproduce">Reproduce</button>{bundle.download.state !== "VERIFIED" && <span id={`bundle-unavailable-${bundle.bundle_id.replace(/[^a-zA-Z0-9_-]/g, "-")}`} className="hint">Reproduction unavailable until a verified bundle is regenerated.</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <table className="compact" data-testid="reproduction-table">
            <caption>Recorded bundle reproductions for the active dataset</caption>
            <thead><tr><th scope="col">Reproduction</th><th scope="col">Original Run</th><th scope="col">State</th><th scope="col">Comparison</th></tr></thead>
            <tbody>
              {datasetReproductions.map((item) => (
                <tr key={item.reproduction_id}><th scope="row" className="mono">{shortHash(item.reproduction_id, 10)}</th><td className="mono">run {shortHash(item.original_run_id, 10)}</td><td className={item.state === "EXACT" || item.state === "WITHIN_TOLERANCE" ? "status" : "error"} data-testid="reproduction-state">{item.state}</td><td className="mono">{item.compared_values} values · {item.differences.length} diffs</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

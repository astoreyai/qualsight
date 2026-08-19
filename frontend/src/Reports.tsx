import { useCallback, useEffect, useMemo, useState } from "react";
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

  const refresh = useCallback(() => {
    if (!projectId) {
      setLeaves([]); setJobs([]); setAnalyses([]); setFigures([]); setDashboards([]); setReports([]); setBundles([]); setReproductions([]);
      return;
    }
    api.matcherLeaves(projectId).then(setLeaves).catch(() => setLeaves([]));
    api.projectJobs(projectId).then((items) => setJobs(items.filter((job) => job.state === "COMPLETED" && job.run_id))).catch(() => setJobs([]));
    api.utilityAnalyses(projectId).then(setAnalyses).catch(() => setAnalyses([]));
    api.figures(projectId).then(setFigures).catch(() => setFigures([]));
    api.dashboards(projectId).then(setDashboards).catch(() => setDashboards([]));
    api.reports(projectId).then(setReports).catch(() => setReports([]));
    api.bundles(projectId).then(setBundles).catch(() => setBundles([]));
    api.reproductions(projectId).then(setReproductions).catch(() => setReproductions([]));
  }, [projectId]);

  useEffect(() => {
    refresh();
    setSelectedFigure(null); setLoadedDashboard(null);
    const handle = window.setInterval(refresh, 5000);
    return () => window.clearInterval(handle);
  }, [refresh]);

  const latestMatrix = leaves.length ? leaves[leaves.length - 1]!.matrix_id : null;
  // Prefer the dataset-scale batch over single-photo uploads: report quality
  // sections describe the dataset job with the most assessed samples (newest wins ties).
  const latestJob = useMemo(
    () => jobs.reduce<JobOut | null>((best, job) => (best === null || job.assessed > best.assessed ? job : best), null),
    [jobs],
  );

  const run = useCallback(async (label: string, action: () => Promise<string>) => {
    setBusy(label);
    try {
      onMessage(await action());
      refresh();
    } catch (error) {
      onMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(null);
    }
  }, [onMessage, refresh]);

  const makeFigure = (kind: string, parameters: Record<string, unknown>) => run(kind, async () => {
    if (!projectId) throw new Error("no project");
    const figure = await api.createFigure(projectId, kind, parameters);
    setSelectedFigure(figure);
    return `Figure ${figure.kind} ${shortHash(figure.figure_id)} recorded (${figure.source_run_ids.length} source Runs)`;
  });

  const saveDashboard = () => run("dashboard", async () => {
    if (!projectId) throw new Error("no project");
    const panels: Array<{ kind: string; ref: string }> = figures.slice(-4).map((figure) => ({ kind: "figure", ref: figure.figure_id }));
    if (leaves.length) panels.push({ kind: "operating_points", ref: leaves[leaves.length - 1]!.run_id });
    if (analyses.length) panels.push({ kind: "policy_table", ref: analyses[analyses.length - 1]!.analysis_id });
    if (!panels.length) throw new Error("no figures or runs to place on a dashboard");
    const saved = await api.saveDashboard(projectId, { name: "Console dashboard", template: "FIQA Evaluation", columns: 2, panels });
    const loaded = await api.dashboard(saved.dashboard_id);
    setLoadedDashboard(loaded);
    return `Dashboard ${shortHash(saved.dashboard_id)} saved with ${panels.length} panels and reloaded (${loaded.layout_sha256 === saved.layout_sha256 ? "layout identical" : "layout differs"})`;
  });

  const openDashboard = (dashboardId: string) => run("dashboard-open", async () => {
    const loaded = await api.dashboard(dashboardId);
    setLoadedDashboard(loaded);
    return `Dashboard ${shortHash(dashboardId)} reloaded: ${loaded.panels.length} panels`;
  });

  const buildReport = () => run("report", async () => {
    if (!projectId) throw new Error("no project");
    const spec: Record<string, unknown> = {
      title: "QualSight console report", executive_summary: "Report assembled from the recorded Runs of this project.", image_policy: imagePolicy,
      dataset_id: datasetId ?? undefined, quality_job_id: latestJob?.job_id, matrix_id: latestMatrix ?? undefined,
      protocol_id: leaves.length ? leaves[leaves.length - 1]!.protocol_id : undefined,
      utility_analysis_ids: analyses.map((item) => item.analysis_id).slice(-6), figure_ids: figures.slice(-4).map((figure) => figure.figure_id),
      failure_examples: analyses.length ? { analysis_id: analyses[analyses.length - 1]!.analysis_id, limit: 4 } : undefined,
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
        <p className="empty">Select a project with recorded Runs.</p>
      </section>
    );
  }

  return (
    <section className="panel reports" aria-labelledby="reports-heading" data-testid="reports">
      <h2 id="reports-heading">Figures · dashboards · reports · exports · reproduction</h2>
      <div className="controls-row utility-controls">
        <button type="button" className="secondary" disabled={!latestMatrix || busy !== null} onClick={() => void makeFigure("det_overlay", { matrix_id: latestMatrix })} data-testid="figure-det">DET overlay</button>
        <button type="button" className="secondary" disabled={!latestMatrix || busy !== null} onClick={() => void makeFigure("operating_points", { matrix_id: latestMatrix })} data-testid="figure-op">Operating points</button>
        <button type="button" className="secondary" disabled={!analyses.length || busy !== null} onClick={() => void makeFigure("erc_overlay", { analysis_ids: analyses.map((item) => item.analysis_id).slice(-6) })} data-testid="figure-erc">ERC overlay</button>
        <button type="button" className="secondary" disabled={!analyses.length || busy !== null} onClick={() => void makeFigure("quality_conditioned_fnmr", { analysis_id: analyses[analyses.length - 1]?.analysis_id })} data-testid="figure-cond">FNMR | quality bin</button>
        <button type="button" className="secondary" disabled={!latestJob || busy !== null} onClick={() => void makeFigure("quality_histogram", { job_id: latestJob?.job_id })} data-testid="figure-hist">Quality histogram</button>
        <button type="button" className="secondary" disabled={!figures.length || busy !== null} onClick={() => void saveDashboard()} data-testid="dashboard-save">Save dashboard</button>
        <label>
          Report images
          <select value={imagePolicy} onChange={(event) => setImagePolicy(event.target.value as "EXCLUDE" | "INCLUDE_PERMITTED")} data-testid="report-image-policy">
            <option value="EXCLUDE">EXCLUDE</option>
            <option value="INCLUDE_PERMITTED">INCLUDE_PERMITTED</option>
          </select>
        </label>
        <button type="button" className="primary" disabled={busy !== null || (!latestMatrix && !latestJob)} onClick={() => void buildReport()} data-testid="report-build">Build report (HTML + PDF)</button>
      </div>

      <div className="utility-facts">
        <div>
          <h4>Recorded figures ({figures.length})</h4>
          <ul className="thumb-list" data-testid="figure-list">
            {figures.map((figure) => (
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
          <h4>Dashboards ({dashboards.length}) · reports ({reports.length})</h4>
          <table className="compact" data-testid="dashboard-table">
            <tbody>
              {dashboards.map((item) => (
                <tr key={item.dashboard_id}><td><button type="button" className="cell" onClick={() => void openDashboard(item.dashboard_id)} data-testid="dashboard-open"><span className="mono">{shortHash(item.dashboard_id, 10)}</span> · {item.name} v{item.version} · {String(item.panels)} panels</button></td></tr>
              ))}
            </tbody>
          </table>
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
            <tbody>
              {reports.map((item) => (
                <tr key={item.report_id}><td className="mono">{shortHash(item.report_id, 10)}</td><td>{item.title}</td><td className="links"><a href={item.links.html} target="_blank" rel="noreferrer">HTML</a><a href={item.links.pdf}>PDF</a><a href={item.links.json}>JSON</a></td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="utility-facts">
        <div>
          <h4>Exports (CSV · JSON · Parquet, with run identifiers)</h4>
          <ul className="legend">
            {latestJob && <li>quality results · <a href={`/api/exports/quality_results?ref=${encodeURIComponent(latestJob.job_id)}&format=csv`}>CSV</a> <a href={`/api/exports/quality_results?ref=${encodeURIComponent(latestJob.job_id)}&format=parquet`}>Parquet</a></li>}
            {leaves.slice(-6).map((leaf) => (
              <li key={leaf.run_id}>{leaf.leaf_id} comparison records · <a href={`/api/exports/comparison_records?ref=${encodeURIComponent(leaf.run_id)}&format=csv`}>CSV</a> <a href={`/api/exports/embeddings?ref=${encodeURIComponent(leaf.run_id)}&format=parquet`}>embeddings</a> <a href={`/api/exports/metric_tables?ref=${encodeURIComponent(leaf.run_id)}&format=json`}>metrics</a> · <button type="button" className="secondary" disabled={busy !== null} onClick={() => void exportBundle(leaf.run_id)} data-testid="bundle-export">bundle</button></li>
            ))}
            {analyses.slice(-6).map((item) => (
              <li key={item.analysis_id}>{item.leaf_id} utility statistics · <a href={`/api/exports/statistical_results?ref=${encodeURIComponent(item.analysis_id)}&format=csv`}>CSV</a> · <button type="button" className="secondary" disabled={busy !== null} onClick={() => void exportBundle(item.run_id)} data-testid="bundle-export-analysis">bundle</button></li>
            ))}
            <li>annotations · <a href={`/api/exports/annotations?ref=${encodeURIComponent(projectId)}&format=json`}>JSON</a></li>
          </ul>
        </div>
        <div>
          <h4>Bundles ({bundles.length}) · reproductions ({reproductions.length})</h4>
          <table className="compact" data-testid="bundle-table">
            <tbody>
              {bundles.map((bundle) => (
                <tr key={bundle.bundle_id}>
                  <td className="mono">{shortHash(bundle.bundle_id, 10)}</td><td className="mono">run {shortHash(bundle.run_id, 10)}</td>
                  <td><a href={bundle.links.download}>.qsexp</a></td>
                  <td><button type="button" className="secondary" disabled={busy !== null} onClick={() => void reproduce(bundle)} data-testid="reproduce">Reproduce</button></td>
                </tr>
              ))}
            </tbody>
          </table>
          <table className="compact" data-testid="reproduction-table">
            <tbody>
              {reproductions.map((item) => (
                <tr key={item.reproduction_id}><td className="mono">{shortHash(item.reproduction_id, 10)}</td><td className="mono">run {shortHash(item.original_run_id, 10)}</td><td className={item.state === "EXACT" || item.state === "WITHIN_TOLERANCE" ? "status" : "error"} data-testid="reproduction-state">{item.state}</td><td className="mono">{item.compared_values} values · {item.differences.length} diffs</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

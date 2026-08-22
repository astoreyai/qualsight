import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  type DatasetOut,
  type DistributionOut,
  type DrilldownRow,
  type JobOut,
  type SampleQualityOut,
} from "./api";

function shortHash(value: string, keep = 12): string {
  const marker = value.indexOf("sha256:");
  const digest = marker >= 0 ? value.slice(marker + 7) : value;
  return `${digest.slice(0, keep)}…`;
}

function fileName(path: string): string {
  const parts = path.split("/");
  return parts[parts.length - 1] ?? path;
}

interface WorkbenchProps {
  projectId: string | null;
  onMessage: (message: string) => void;
  onDataset?: (datasetId: string | null) => void;
}

/** Dataset import + batch execution + quality explorer for the active project (QS-B11..B13). */
export function Workbench({ projectId, onMessage, onDataset }: WorkbenchProps) {
  const [datasets, setDatasets] = useState<DatasetOut[]>([]);
  const [statistics, setStatistics] = useState<Record<string, any> | null>(null);
  const [jobs, setJobs] = useState<JobOut[]>([]);
  const [activeJob, setActiveJob] = useState<JobOut | null>(null);
  const [selection, setSelection] = useState<"subset_fixture" | "all">("subset_fixture");
  const [busy, setBusy] = useState(false);
  const [distribution, setDistribution] = useState<DistributionOut | null>(null);
  const [explorerJobId, setExplorerJobId] = useState<string | null>(null);
  const [selectedBin, setSelectedBin] = useState<number | null>(null);
  const [drill, setDrill] = useState<{ total: number; rows: DrilldownRow[] } | null>(null);
  const [sampleQuality, setSampleQuality] = useState<SampleQualityOut | null>(null);

  const refresh = useCallback(() => {
    if (!projectId) {
      setDatasets([]);
      setJobs([]);
      return;
    }
    api.datasets(projectId).then(setDatasets).catch(() => setDatasets([]));
    api.projectJobs(projectId).then(setJobs).catch(() => setJobs([]));
  }, [projectId]);

  useEffect(() => {
    refresh();
    setStatistics(null);
    setDistribution(null);
    setDrill(null);
    setSampleQuality(null);
    setExplorerJobId(null);
  }, [refresh]);

  // The workbench dataset is the imported identity-labelled LFW dataset; the
  // content-addressed single-sample datasets created by ad-hoc assessments are
  // listed but are not batch inputs.
  const dataset = datasets.find((item) => item.manifest["dataset_resource_id"] === "lfw-funneled-local-candidate") ?? null;

  useEffect(() => {
    onDataset?.(dataset ? dataset.dataset_id : null);
    if (!dataset) return;
    api.datasetStatistics(dataset.dataset_id).then((body) => setStatistics(body.statistics)).catch(() => setStatistics(null));
  }, [dataset, onDataset]);

  // Poll the active job until it ends.
  useEffect(() => {
    if (!activeJob || (activeJob.ended_at && ["COMPLETED", "CANCELLED", "FAILED"].includes(activeJob.state))) return;
    const handle = window.setInterval(() => {
      api
        .job(activeJob.job_id)
        .then((job) => {
          setActiveJob(job);
          if (job.ended_at) refresh();
        })
        .catch(() => undefined);
    }, 1000);
    return () => window.clearInterval(handle);
  }, [activeJob, refresh]);

  const importLfw = useCallback(async () => {
    if (!projectId) return;
    setBusy(true);
    try {
      const imported = await api.importLfw(projectId, true);
      onMessage(`Imported ${imported.name}: ${imported.sample_count.toLocaleString()} samples · ${imported.subject_count} subjects · rights ${imported.rights_status}`);
      refresh();
    } catch (error) {
      onMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }, [projectId, onMessage, refresh]);

  const startBatch = useCallback(async () => {
    if (!projectId || !dataset) return;
    setBusy(true);
    try {
      // The batch scope must be approved by both the engine and the dataset's
      // resource; the server chooses the common scope when none is given.
      const job = await api.startBatch(projectId, dataset.dataset_id, { kind: selection }, 1, dataset.common_scopes_with_engine[0] ?? null);
      setActiveJob(job);
      onMessage(`Batch ${shortHash(job.job_id)} started over ${job.total} samples`);
    } catch (error) {
      onMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }, [projectId, dataset, selection, onMessage]);

  const cancelBatch = useCallback(async () => {
    if (!activeJob) return;
    try {
      setActiveJob(await api.cancelJob(activeJob.job_id));
    } catch (error) {
      onMessage(error instanceof Error ? error.message : String(error));
    }
  }, [activeJob, onMessage]);

  const openExplorer = useCallback(
    async (jobId: string) => {
      setExplorerJobId(jobId);
      setSelectedBin(null);
      setDrill(null);
      setSampleQuality(null);
      try {
        setDistribution(await api.distribution(jobId, "UnifiedQualityScore", "scalar", 20));
      } catch (error) {
        onMessage(error instanceof Error ? error.message : String(error));
      }
    },
    [onMessage],
  );

  const selectBin = useCallback(
    async (index: number) => {
      if (!distribution || !explorerJobId) return;
      const bin = distribution.bins[index];
      if (!bin) return;
      setSelectedBin(index);
      setSampleQuality(null);
      const low = Math.ceil(bin.low);
      const high = index === distribution.bins.length - 1 ? Math.floor(bin.high) : Math.ceil(bin.high) - 1;
      try {
        setDrill(await api.drilldown(explorerJobId, low, high));
      } catch (error) {
        onMessage(error instanceof Error ? error.message : String(error));
      }
    },
    [distribution, explorerJobId, onMessage],
  );

  const showFailures = useCallback(async () => {
    if (!explorerJobId) return;
    setSelectedBin(null);
    setSampleQuality(null);
    try {
      setDrill(await api.drilldownFailures(explorerJobId));
    } catch (error) {
      onMessage(error instanceof Error ? error.message : String(error));
    }
  }, [explorerJobId, onMessage]);

  const inspectSample = useCallback(
    async (sampleId: string) => {
      try {
        setSampleQuality(await api.sampleQuality(sampleId));
      } catch (error) {
        onMessage(error instanceof Error ? error.message : String(error));
      }
    },
    [onMessage],
  );

  const maxCount = useMemo(() => (distribution ? Math.max(1, ...distribution.bins.map((b) => b.count)) : 1), [distribution]);
  const completedJobs = jobs.filter((job) => job.state === "COMPLETED");
  const currentResult = sampleQuality?.results.find((r) => r.job_id === explorerJobId) ?? sampleQuality?.results[0] ?? null;

  if (!projectId) {
    return (
      <section className="panel workbench" aria-labelledby="workbench-heading" data-testid="workbench">
        <h2 id="workbench-heading">Dataset · Batch · Explorer</h2>
        <p className="empty">Select or create a project to import a dataset and run batch quality.</p>
      </section>
    );
  }

  return (
    <section className="panel workbench" aria-labelledby="workbench-heading" data-testid="workbench">
      <h2 id="workbench-heading">Dataset · Batch · Explorer</h2>
      <div className="workbench-grid">
        <div className="wb-column">
          <h3>Dataset</h3>
          {dataset ? (
            <dl className="facts" data-testid="dataset-facts">
              <dt>Name</dt>
              <dd>{dataset.name}</dd>
              <dt>Samples</dt>
              <dd>{dataset.sample_count.toLocaleString()} · {dataset.subject_count} subjects</dd>
              <dt>Rights</dt>
              <dd className="mono">{dataset.rights_status}</dd>
              <dt>Scope</dt>
              <dd className="mono">{dataset.common_scopes_with_engine.join(", ") || "no common scope with the engine"}</dd>
              <dt>Manifest</dt>
              <dd className="mono">{shortHash(dataset.manifest_sha256, 16)}</dd>
              {statistics && (
                <>
                  <dt>Pairs</dt>
                  <dd>
                    {statistics.evaluation_support.mated_comparisons.toLocaleString()} mated · {statistics.evaluation_support.nonmated_comparisons.toLocaleString()} non-mated
                  </dd>
                  <dt>Completeness</dt>
                  <dd>
                    missing {statistics.data_completeness.missing_files} · unreadable {statistics.data_completeness.unreadable_files} · hash mismatches {statistics.data_completeness.hash_mismatches} · dup hashes {statistics.data_completeness.duplicate_hashes}
                  </dd>
                </>
              )}
            </dl>
          ) : (
            <>
              {datasets.length > 0 && (
                <p className="hint">
                  {datasets.length} single-sample dataset(s) recorded by ad-hoc assessments; no identity-labelled dataset yet.
                </p>
              )}
              <p className="hint">Import the registered LFW funneled dataset (13,233 images, verified in place; never copied).</p>
              <button type="button" className="primary" data-testid="import-lfw" disabled={busy} onClick={() => void importLfw()}>
                {busy ? "Importing…" : "Import registered LFW"}
              </button>
            </>
          )}
        </div>

        <div className="wb-column">
          <h3>Batch quality</h3>
          {dataset ? (
            <>
              <div className="controls-row">
                <select value={selection} onChange={(event) => setSelection(event.target.value as "subset_fixture" | "all")} data-testid="batch-selection" aria-label="Batch selection">
                  <option value="subset_fixture">Registered subset fixture (fold-1 pairs)</option>
                  <option value="all">All samples ({dataset.sample_count.toLocaleString()})</option>
                </select>
                <button type="button" className="primary" data-testid="batch-start" disabled={busy || (activeJob !== null && !activeJob.ended_at)} onClick={() => void startBatch()}>
                  Run OFIQ batch
                </button>
              </div>
              {activeJob && (
                <div className="job-progress" data-testid="job-progress">
                  <div className="progress-track" aria-label="batch progress">
                    <div className="progress-fill" style={{ width: `${activeJob.total ? Math.min(100, (100 * (activeJob.assessed + activeJob.failed + activeJob.integrity_failures)) / activeJob.total) : 0}%` }} />
                  </div>
                  <p className="mono">
                    {activeJob.state} · {activeJob.assessed + activeJob.failed}/{activeJob.total} · failures {activeJob.failed} · not assessed {activeJob.not_assessed}
                  </p>
                  {activeJob.error && (
                    <p className="error" role="alert">
                      {activeJob.error}
                    </p>
                  )}
                  {!activeJob.ended_at && (
                    <button type="button" className="secondary danger" data-testid="batch-cancel" onClick={() => void cancelBatch()}>
                      Cancel
                    </button>
                  )}
                  {activeJob.run_id && (
                    <p className="mono">
                      run <a href={`/api/runs/${activeJob.run_id}`}>{shortHash(activeJob.run_id, 16)}</a>
                    </p>
                  )}
                </div>
              )}
              {jobs.length > 0 && (
                <ul className="job-list" data-testid="job-list">
                  {jobs.map((job) => (
                    <li key={job.job_id}>
                      <button type="button" className={explorerJobId === job.job_id ? "sample selected" : "sample"} disabled={job.state !== "COMPLETED"} onClick={() => void openExplorer(job.job_id)}>
                        <span className="sample-name">{job.state} · {job.assessed}/{job.total}</span>
                        <span className="sample-meta mono">{shortHash(job.job_id, 16)}</span>
                        <span className="sample-meta">{(job.started_at ?? "").replace("T", " ").slice(0, 19)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {completedJobs.length === 0 && <p className="hint">Completed jobs open in the explorer.</p>}
            </>
          ) : (
            <p className="empty">Import a dataset first.</p>
          )}
        </div>

        <div className="wb-column explorer" data-testid="explorer">
          <h3>Quality explorer</h3>
          {distribution ? (
            <>
              <p className="hint">
                UnifiedQualityScore (0–100) · {distribution.measured_rows_with_value} measured rows · outcomes{" "}
                {Object.entries(distribution.outcomes).map(([k, v]) => `${k}=${v}`).join(", ")} · run{" "}
                {distribution.run_id ? <a href={`/api/runs/${distribution.run_id}`}>{shortHash(distribution.run_id, 12)}</a> : "—"}
              </p>
              <svg className="histogram" viewBox="0 0 400 140" role="img" aria-label="UnifiedQualityScore distribution" data-testid="histogram">
                {distribution.bins.map((bin, index) => {
                  const height = (bin.count / maxCount) * 110;
                  const width = 400 / distribution.bins.length;
                  return (
                    <g key={bin.bin} onClick={() => void selectBin(index)} style={{ cursor: "pointer" }} data-testid={`bin-${index}`}>
                      <rect x={index * width + 1} y={120 - height} width={width - 2} height={height} className={selectedBin === index ? "bar-selected" : "bar-normal"} />
                      <text x={index * width + width / 2} y={132} textAnchor="middle" className="axis">
                        {Math.round(bin.low)}
                      </text>
                    </g>
                  );
                })}
              </svg>
              <div className="controls-row">
                <button type="button" className="secondary" onClick={() => void showFailures()} data-testid="show-failures">
                  Engine row failures ({distribution.outcomes.ENGINE_ROW_FAILURE ?? 0})
                </button>
              </div>
            </>
          ) : (
            <p className="empty">Select a completed job to explore its distribution.</p>
          )}
          {drill && (
            <div className="drilldown" data-testid="drilldown">
              <p className="hint">{drill.total} samples in selection (showing {drill.rows.length})</p>
              <ul className="thumb-list">
                {drill.rows.map((row) => (
                  <li key={row.sample_id}>
                    <button type="button" className="thumb" onClick={() => void inspectSample(row.sample_id)} data-testid="drill-sample">
                      <img src={row.links.image} alt={`Sample ${fileName(row.source_path)}`} />
                      <span className="mono">{row.uqs_scalar ?? "—"}</span>
                      <span className="sample-meta">{fileName(row.source_path)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {sampleQuality && currentResult && (
            <div className="sample-quality" data-testid="sample-quality">
              <h4>{fileName(sampleQuality.sample.source_path)}</h4>
              <dl className="facts">
                <dt>Subject</dt>
                <dd>{sampleQuality.sample.subject_id.value ?? "—"}</dd>
                <dt>SHA-256</dt>
                <dd className="mono">{shortHash(sampleQuality.sample.sha256, 20)}</dd>
                <dt>Outcome</dt>
                <dd>{currentResult.outcome}{currentResult.failure_reason ? ` — ${currentResult.failure_reason}` : ""}</dd>
                <dt>Run</dt>
                <dd>{currentResult.links.run ? <a href={currentResult.links.run} className="mono">{shortHash(currentResult.run_id ?? "", 16)}</a> : "—"}</dd>
                <dt>Audit</dt>
                <dd>{currentResult.links.audit ? <a href={currentResult.links.audit}>audit trail</a> : "—"}</dd>
              </dl>
              <table className="measures compact" data-testid="sample-measure-table">
                <thead>
                  <tr>
                    <th scope="col">Measure</th>
                    <th scope="col">Native</th>
                    <th scope="col">Component</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(currentResult.measures).map(([name, value]) => (
                    <tr key={name} className={value.failed ? "failed" : ""}>
                      <th scope="row">{name}</th>
                      <td className="mono">{value.native ?? "—"}</td>
                      <td className="mono">{value.scalar ?? "engine failure"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

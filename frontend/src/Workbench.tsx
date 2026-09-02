import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type DatasetOut,
  type DistributionOut,
  type DrilldownRow,
  type JobOut,
  type SampleQualityOut,
} from "./api";
import { EvidenceOpenButton, type EvidenceRequest } from "./EvidenceDetail";

function shortHash(value: string, keep = 12): string {
  const marker = value.indexOf("sha256:");
  const digest = marker >= 0 ? value.slice(marker + 7) : value;
  return `${digest.slice(0, keep)}…`;
}

function isTerminalJob(job: JobOut): boolean {
  return Boolean(job.ended_at && ["COMPLETED", "CANCELLED", "FAILED", "INTERRUPTED"].includes(job.state));
}

interface WorkbenchProps {
  projectId: string | null;
  onMessage: (message: string) => void;
  onDataset?: (dataset: DatasetOut | null) => void;
  onWorkflowTerminal?: () => void;
  onOpenEvidence: (request: EvidenceRequest) => void;
}

/** Dataset import + batch execution + quality explorer for the active project (QS-B11..B13). */
export function Workbench({ projectId, onMessage, onDataset, onWorkflowTerminal, onOpenEvidence }: WorkbenchProps) {
  const [datasets, setDatasets] = useState<DatasetOut[]>([]);
  const [datasetsProjectId, setDatasetsProjectId] = useState<string | null>(null);
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
  const [activeDatasetId, setActiveDatasetId] = useState<string | null>(() => window.localStorage.getItem("qualsight.activeDatasetId"));
  const [feedPhase, setFeedPhase] = useState<"loading" | "ready" | "error">("loading");
  const [feedError, setFeedError] = useState<string | null>(null);
  const [jobPollError, setJobPollError] = useState<string | null>(null);
  const [ownerDirectory, setOwnerDirectory] = useState("");
  const [ownerDirectoryName, setOwnerDirectoryName] = useState("");
  const [ownerManifest, setOwnerManifest] = useState("");
  const [ownerManifestName, setOwnerManifestName] = useState("");
  const [manifestSubjectColumn, setManifestSubjectColumn] = useState("");
  const [manifestPathColumn, setManifestPathColumn] = useState("");
  const [mappingName, setMappingName] = useState("");
  const projectEpoch = useRef(0);
  const currentProjectId = useRef<string | null>(projectId);
  const currentDatasetId = useRef<string | null>(null);
  const refreshSequence = useRef(0);
  const statisticsSequence = useRef(0);
  const jobPollSequence = useRef(0);
  const explorerSequence = useRef(0);
  const drillSequence = useRef(0);
  const sampleSequence = useRef(0);
  const datasetEpoch = useRef(0);
  const actionSequence = useRef(0);
  const notifiedTerminalJobs = useRef(new Set<string>());

  const notifyTerminal = useCallback((job: JobOut) => {
    if (!isTerminalJob(job) || notifiedTerminalJobs.current.has(job.job_id)) return;
    notifiedTerminalJobs.current.add(job.job_id);
    onWorkflowTerminal?.();
  }, [onWorkflowTerminal]);

  const actionContextIsCurrent = useCallback((
    requestedProjectId: string,
    projectRequestEpoch: number,
    requestedDatasetId: string | null,
    datasetRequestEpoch: number,
    actionRequestSequence: number,
  ) => (
    currentProjectId.current === requestedProjectId
    && projectEpoch.current === projectRequestEpoch
    && currentDatasetId.current === requestedDatasetId
    && datasetEpoch.current === datasetRequestEpoch
    && actionSequence.current === actionRequestSequence
  ), []);

  useEffect(() => {
    if (activeDatasetId) window.localStorage.setItem("qualsight.activeDatasetId", activeDatasetId);
    else window.localStorage.removeItem("qualsight.activeDatasetId");
  }, [activeDatasetId]);

  const refresh = useCallback(async () => {
    const requestedProjectId = projectId;
    if (currentProjectId.current !== requestedProjectId) return;
    const epoch = projectEpoch.current;
    const sequence = ++refreshSequence.current;
    if (!requestedProjectId) {
      setDatasets([]);
      setDatasetsProjectId(null);
      setJobs([]);
      setActiveDatasetId(null);
      setFeedError(null);
      setFeedPhase("ready");
      return;
    }
    setFeedPhase("loading");
    setFeedError(null);
    const [datasetsResult, jobsResult] = await Promise.allSettled([api.datasets(requestedProjectId), api.projectJobs(requestedProjectId)]);
    if (currentProjectId.current !== requestedProjectId || projectEpoch.current !== epoch || refreshSequence.current !== sequence) return;
    if (datasetsResult.status === "fulfilled") setDatasets(datasetsResult.value);
    else setDatasets([]);
    setDatasetsProjectId(requestedProjectId);
    if (jobsResult.status === "fulfilled") setJobs(jobsResult.value);
    else setJobs([]);
    const failures = [
      datasetsResult.status === "rejected" ? `datasets: ${datasetsResult.reason instanceof Error ? datasetsResult.reason.message : String(datasetsResult.reason)}` : null,
      jobsResult.status === "rejected" ? `quality jobs: ${jobsResult.reason instanceof Error ? jobsResult.reason.message : String(jobsResult.reason)}` : null,
    ].filter((value): value is string => value !== null);
    setFeedError(failures.length ? `Project data is incomplete. ${failures.join(" · ")}` : null);
    setFeedPhase(failures.length === 2 ? "error" : "ready");
  }, [projectId]);

  useLayoutEffect(() => {
    currentProjectId.current = projectId;
    projectEpoch.current += 1;
    refreshSequence.current += 1;
    statisticsSequence.current += 1;
    jobPollSequence.current += 1;
    explorerSequence.current += 1;
    drillSequence.current += 1;
    sampleSequence.current += 1;
    datasetEpoch.current += 1;
    actionSequence.current += 1;
    currentDatasetId.current = null;
    notifiedTerminalJobs.current.clear();
    setDatasets([]);
    setDatasetsProjectId(null);
    setJobs([]);
    setActiveDatasetId(null);
    setActiveJob(null);
    setJobPollError(null);
    setBusy(false);
    onDataset?.(null);
    void refresh();
    setStatistics(null);
    setDistribution(null);
    setDrill(null);
    setSampleQuality(null);
    setExplorerJobId(null);
  }, [projectId, refresh, onDataset]);

  const usableDatasets = useMemo(
    () => datasetsProjectId === projectId ? datasets.filter((item) => item.sample_count > 1) : [],
    [datasets, datasetsProjectId, projectId],
  );
  const dataset = usableDatasets.find((item) => item.dataset_id === activeDatasetId)
    ?? usableDatasets.find((item) => item.manifest["dataset_resource_id"] === "lfw-funneled-local-candidate")
    ?? usableDatasets[0]
    ?? null;
  const datasetId = dataset?.dataset_id ?? null;

  useLayoutEffect(() => {
    if (currentDatasetId.current === datasetId) return;
    currentDatasetId.current = datasetId;
    datasetEpoch.current += 1;
    actionSequence.current += 1;
    statisticsSequence.current += 1;
    jobPollSequence.current += 1;
    explorerSequence.current += 1;
    drillSequence.current += 1;
    sampleSequence.current += 1;
    setActiveJob(null);
    setJobPollError(null);
    setBusy(false);
    setStatistics(null);
    setDistribution(null);
    setExplorerJobId(null);
    setSelectedBin(null);
    setDrill(null);
    setSampleQuality(null);
    if (["OWNER_SUPPLIED_LOCAL_INPUT", "MANAGED_PREALIGNED_PAIR_DATASET"].includes(String(dataset?.manifest["source_kind"]))) setSelection("all");
    onDataset?.(dataset);
  }, [dataset, datasetId, onDataset]);

  const datasetJobs = useMemo(
    () => datasetId ? jobs.filter((job) => job.dataset_id === datasetId) : [],
    [datasetId, jobs],
  );

  useEffect(() => {
    if (dataset && dataset.dataset_id !== activeDatasetId) setActiveDatasetId(dataset.dataset_id);
  }, [dataset, activeDatasetId]);

  useEffect(() => {
    const projectRequestEpoch = projectEpoch.current;
    const datasetRequestEpoch = datasetEpoch.current;
    const sequence = ++statisticsSequence.current;
    setStatistics(null);
    if (!projectId || !dataset) return;
    if (dataset.manifest["source_kind"] === "OWNER_SUPPLIED_LOCAL_INPUT") setSelection("all");
    void api.datasetStatistics(projectId, dataset.dataset_id)
      .then((body) => {
        if (
          projectEpoch.current !== projectRequestEpoch
          || datasetEpoch.current !== datasetRequestEpoch
          || currentDatasetId.current !== dataset.dataset_id
          || statisticsSequence.current !== sequence
          || body.dataset_id !== dataset.dataset_id
        ) return;
        setStatistics(body.statistics);
      })
      .catch((error) => {
        if (
          projectEpoch.current !== projectRequestEpoch
          || datasetEpoch.current !== datasetRequestEpoch
          || currentDatasetId.current !== dataset.dataset_id
          || statisticsSequence.current !== sequence
        ) return;
        onMessage(error instanceof Error ? error.message : String(error));
      });
    return () => {
      statisticsSequence.current += 1;
    };
  }, [dataset, onMessage, projectId]);

  // Poll the active job until it ends.
  useEffect(() => {
    if (!projectId || !activeJob || isTerminalJob(activeJob) || currentDatasetId.current !== activeJob.dataset_id) return;
    const requestedProjectId = projectId;
    const projectRequestEpoch = projectEpoch.current;
    const datasetRequestEpoch = datasetEpoch.current;
    const requestedDatasetId = activeJob.dataset_id;
    const jobId = activeJob.job_id;
    let cancelled = false;
    let handle: number | null = null;
    const poll = () => {
      handle = window.setTimeout(() => {
        const sequence = ++jobPollSequence.current;
        void api
          .job(requestedProjectId, jobId)
          .then((job) => {
            if (
              cancelled
              || currentProjectId.current !== requestedProjectId
              || projectEpoch.current !== projectRequestEpoch
              || datasetEpoch.current !== datasetRequestEpoch
              || currentDatasetId.current !== requestedDatasetId
              || jobPollSequence.current !== sequence
              || job.job_id !== jobId
              || job.dataset_id !== requestedDatasetId
            ) return;
            setJobPollError(null);
            setActiveJob(job);
            if (isTerminalJob(job)) {
              void refresh();
              notifyTerminal(job);
            } else {
              poll();
            }
          })
          .catch((error) => {
            if (
              cancelled
              || currentProjectId.current !== requestedProjectId
              || projectEpoch.current !== projectRequestEpoch
              || datasetEpoch.current !== datasetRequestEpoch
              || currentDatasetId.current !== requestedDatasetId
              || jobPollSequence.current !== sequence
            ) return;
            setJobPollError(`Quality job status is temporarily unavailable; the last state may be stale. ${error instanceof Error ? error.message : String(error)}`);
            poll();
          });
      }, 1000);
    };
    poll();
    return () => {
      cancelled = true;
      if (handle !== null) window.clearTimeout(handle);
      jobPollSequence.current += 1;
    };
  }, [activeJob, notifyTerminal, projectId, refresh]);

  const importLfw = useCallback(async () => {
    if (!projectId) return;
    const requestedProjectId = projectId;
    const requestedDatasetId = currentDatasetId.current;
    const projectRequestEpoch = projectEpoch.current;
    const datasetRequestEpoch = datasetEpoch.current;
    const actionRequestSequence = ++actionSequence.current;
    setBusy(true);
    try {
      const imported = await api.importLfw(requestedProjectId, true);
      if (!actionContextIsCurrent(requestedProjectId, projectRequestEpoch, requestedDatasetId, datasetRequestEpoch, actionRequestSequence)) return;
      onMessage(`Imported ${imported.name}: ${imported.sample_count.toLocaleString()} samples · ${imported.subject_count} subjects · rights ${imported.rights_status}`);
      void refresh();
    } catch (error) {
      if (actionContextIsCurrent(requestedProjectId, projectRequestEpoch, requestedDatasetId, datasetRequestEpoch, actionRequestSequence)) onMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (actionContextIsCurrent(requestedProjectId, projectRequestEpoch, requestedDatasetId, datasetRequestEpoch, actionRequestSequence)) setBusy(false);
    }
  }, [actionContextIsCurrent, projectId, onMessage, refresh]);

  const importOwnerDirectory = useCallback(async () => {
    if (!projectId || !ownerDirectory || !ownerDirectoryName) return;
    const requestedProjectId = projectId;
    const requestedDatasetId = currentDatasetId.current;
    const projectRequestEpoch = projectEpoch.current;
    const datasetRequestEpoch = datasetEpoch.current;
    const actionRequestSequence = ++actionSequence.current;
    setBusy(true);
    try {
      const imported = await api.importOwnerDirectory(requestedProjectId, ownerDirectory, ownerDirectoryName);
      if (!actionContextIsCurrent(requestedProjectId, projectRequestEpoch, requestedDatasetId, datasetRequestEpoch, actionRequestSequence)) return;
      setActiveDatasetId(imported.dataset_id);
      onMessage(`Imported owner directory: ${imported.sample_count.toLocaleString()} real images · ${imported.subject_count.toLocaleString()} operator-labelled subjects · local private input`);
      void refresh();
    } catch (error) {
      if (actionContextIsCurrent(requestedProjectId, projectRequestEpoch, requestedDatasetId, datasetRequestEpoch, actionRequestSequence)) onMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (actionContextIsCurrent(requestedProjectId, projectRequestEpoch, requestedDatasetId, datasetRequestEpoch, actionRequestSequence)) setBusy(false);
    }
  }, [actionContextIsCurrent, projectId, ownerDirectory, ownerDirectoryName, onMessage, refresh]);

  const importOwnerManifest = useCallback(async () => {
    if (!projectId || !ownerManifest || !ownerManifestName || !manifestSubjectColumn || !manifestPathColumn || !mappingName) return;
    const requestedProjectId = projectId;
    const requestedDatasetId = currentDatasetId.current;
    const projectRequestEpoch = projectEpoch.current;
    const datasetRequestEpoch = datasetEpoch.current;
    const actionRequestSequence = ++actionSequence.current;
    setBusy(true);
    try {
      const imported = await api.importOwnerManifest(
        requestedProjectId,
        ownerManifest,
        ownerManifestName,
        { subject_id: manifestSubjectColumn, path: manifestPathColumn },
        mappingName,
      );
      if (!actionContextIsCurrent(requestedProjectId, projectRequestEpoch, requestedDatasetId, datasetRequestEpoch, actionRequestSequence)) return;
      setActiveDatasetId(imported.dataset_id);
      onMessage(`Imported owner manifest: ${imported.sample_count.toLocaleString()} real images · ${imported.subject_count.toLocaleString()} operator-labelled subjects · mapping ${mappingName}`);
      void refresh();
    } catch (error) {
      if (actionContextIsCurrent(requestedProjectId, projectRequestEpoch, requestedDatasetId, datasetRequestEpoch, actionRequestSequence)) onMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (actionContextIsCurrent(requestedProjectId, projectRequestEpoch, requestedDatasetId, datasetRequestEpoch, actionRequestSequence)) setBusy(false);
    }
  }, [actionContextIsCurrent, projectId, ownerManifest, ownerManifestName, manifestSubjectColumn, manifestPathColumn, mappingName, onMessage, refresh]);

  const startBatch = useCallback(async () => {
    if (!projectId || !dataset) return;
    const requestedProjectId = projectId;
    const requestedDatasetId = dataset.dataset_id;
    const projectRequestEpoch = projectEpoch.current;
    const datasetRequestEpoch = datasetEpoch.current;
    const actionRequestSequence = ++actionSequence.current;
    setBusy(true);
    try {
      // The batch scope must be approved by both the engine and the dataset's
      // resource; the server chooses the common scope when none is given.
      const job = await api.startBatch(requestedProjectId, requestedDatasetId, { kind: selection }, 1, dataset.common_scopes_with_engine[0] ?? null);
      if (
        currentProjectId.current !== requestedProjectId
        || projectEpoch.current !== projectRequestEpoch
        || currentDatasetId.current !== requestedDatasetId
        || datasetEpoch.current !== datasetRequestEpoch
        || actionSequence.current !== actionRequestSequence
        || job.dataset_id !== requestedDatasetId
      ) return;
      setActiveJob(job);
      onMessage(`Batch ${shortHash(job.job_id)} started over ${job.total} samples`);
      notifyTerminal(job);
    } catch (error) {
      if (
        currentProjectId.current === requestedProjectId
        && projectEpoch.current === projectRequestEpoch
        && currentDatasetId.current === requestedDatasetId
        && datasetEpoch.current === datasetRequestEpoch
        && actionSequence.current === actionRequestSequence
      ) onMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (
        currentProjectId.current === requestedProjectId
        && projectEpoch.current === projectRequestEpoch
        && currentDatasetId.current === requestedDatasetId
        && datasetEpoch.current === datasetRequestEpoch
        && actionSequence.current === actionRequestSequence
      ) setBusy(false);
    }
  }, [projectId, dataset, selection, onMessage, notifyTerminal]);

  const cancelBatch = useCallback(async () => {
    if (!projectId || !activeJob) return;
    const requestedProjectId = projectId;
    const requestedDatasetId = activeJob.dataset_id;
    const requestedJobId = activeJob.job_id;
    const projectRequestEpoch = projectEpoch.current;
    const datasetRequestEpoch = datasetEpoch.current;
    const actionRequestSequence = ++actionSequence.current;
    try {
      const job = await api.cancelJob(requestedProjectId, requestedJobId);
      if (
        currentProjectId.current !== requestedProjectId
        || projectEpoch.current !== projectRequestEpoch
        || currentDatasetId.current !== requestedDatasetId
        || datasetEpoch.current !== datasetRequestEpoch
        || actionSequence.current !== actionRequestSequence
        || job.job_id !== requestedJobId
        || job.dataset_id !== requestedDatasetId
      ) return;
      setActiveJob(job);
      if (isTerminalJob(job)) {
        void refresh();
        notifyTerminal(job);
      }
    } catch (error) {
      if (
        currentProjectId.current === requestedProjectId
        && projectEpoch.current === projectRequestEpoch
        && currentDatasetId.current === requestedDatasetId
        && datasetEpoch.current === datasetRequestEpoch
        && actionSequence.current === actionRequestSequence
      ) onMessage(error instanceof Error ? error.message : String(error));
    }
  }, [activeJob, projectId, notifyTerminal, onMessage, refresh]);

  const openExplorer = useCallback(
    async (jobId: string) => {
      const requestedDatasetId = currentDatasetId.current;
      const selectedJob = datasetJobs.find((job) => job.job_id === jobId && job.state === "COMPLETED");
      if (!projectId || !requestedDatasetId || !selectedJob) return;
      const projectRequestEpoch = projectEpoch.current;
      const datasetRequestEpoch = datasetEpoch.current;
      const sequence = ++explorerSequence.current;
      drillSequence.current += 1;
      sampleSequence.current += 1;
      setExplorerJobId(jobId);
      setActiveJob(selectedJob);
      setDistribution(null);
      setSelectedBin(null);
      setDrill(null);
      setSampleQuality(null);
      try {
        const nextDistribution = await api.distribution(projectId, jobId, "UnifiedQualityScore", "scalar", 20);
        if (
          projectEpoch.current !== projectRequestEpoch
          || datasetEpoch.current !== datasetRequestEpoch
          || currentDatasetId.current !== requestedDatasetId
          || explorerSequence.current !== sequence
        ) return;
        setDistribution(nextDistribution);
      } catch (error) {
        if (
          projectEpoch.current === projectRequestEpoch
          && datasetEpoch.current === datasetRequestEpoch
          && currentDatasetId.current === requestedDatasetId
          && explorerSequence.current === sequence
        ) onMessage(error instanceof Error ? error.message : String(error));
      }
    },
    [datasetJobs, onMessage, projectId],
  );

  const selectBin = useCallback(
    async (index: number) => {
      if (!distribution || !explorerJobId) return;
      const requestedDatasetId = currentDatasetId.current;
      if (!projectId || !requestedDatasetId || !datasetJobs.some((job) => job.job_id === explorerJobId)) return;
      const bin = distribution.bins[index];
      if (!bin) return;
      setSelectedBin(index);
      const projectRequestEpoch = projectEpoch.current;
      const datasetRequestEpoch = datasetEpoch.current;
      const sequence = ++drillSequence.current;
      sampleSequence.current += 1;
      setSampleQuality(null);
      const low = Math.ceil(bin.low);
      const high = index === distribution.bins.length - 1 ? Math.floor(bin.high) : Math.ceil(bin.high) - 1;
      try {
        const nextDrill = await api.drilldown(projectId, explorerJobId, low, high);
        if (
          projectEpoch.current !== projectRequestEpoch
          || datasetEpoch.current !== datasetRequestEpoch
          || currentDatasetId.current !== requestedDatasetId
          || drillSequence.current !== sequence
        ) return;
        setDrill(nextDrill);
      } catch (error) {
        if (
          projectEpoch.current === projectRequestEpoch
          && datasetEpoch.current === datasetRequestEpoch
          && currentDatasetId.current === requestedDatasetId
          && drillSequence.current === sequence
        ) onMessage(error instanceof Error ? error.message : String(error));
      }
    },
    [datasetJobs, distribution, explorerJobId, onMessage, projectId],
  );

  const showFailures = useCallback(async () => {
    if (!explorerJobId) return;
    const requestedDatasetId = currentDatasetId.current;
    if (!projectId || !requestedDatasetId || !datasetJobs.some((job) => job.job_id === explorerJobId)) return;
    const projectRequestEpoch = projectEpoch.current;
    const datasetRequestEpoch = datasetEpoch.current;
    const sequence = ++drillSequence.current;
    sampleSequence.current += 1;
    setSelectedBin(null);
    setSampleQuality(null);
    try {
      const nextDrill = await api.drilldownFailures(projectId, explorerJobId);
      if (
        projectEpoch.current !== projectRequestEpoch
        || datasetEpoch.current !== datasetRequestEpoch
        || currentDatasetId.current !== requestedDatasetId
        || drillSequence.current !== sequence
      ) return;
      setDrill(nextDrill);
    } catch (error) {
      if (
        projectEpoch.current === projectRequestEpoch
        && datasetEpoch.current === datasetRequestEpoch
        && currentDatasetId.current === requestedDatasetId
        && drillSequence.current === sequence
      ) onMessage(error instanceof Error ? error.message : String(error));
    }
  }, [datasetJobs, explorerJobId, onMessage, projectId]);

  const inspectSample = useCallback(
    async (sampleId: string) => {
      const requestedProjectId = currentProjectId.current;
      const requestedDatasetId = currentDatasetId.current;
      if (!requestedProjectId || !requestedDatasetId) return;
      const projectRequestEpoch = projectEpoch.current;
      const datasetRequestEpoch = datasetEpoch.current;
      const sequence = ++sampleSequence.current;
      try {
        const nextSample = await api.sampleQuality(requestedProjectId, sampleId);
        if (
          projectEpoch.current !== projectRequestEpoch
          || datasetEpoch.current !== datasetRequestEpoch
          || currentProjectId.current !== requestedProjectId
          || currentDatasetId.current !== requestedDatasetId
          || sampleSequence.current !== sequence
        ) return;
        setSampleQuality(nextSample);
      } catch (error) {
        if (
          projectEpoch.current === projectRequestEpoch
          && datasetEpoch.current === datasetRequestEpoch
          && currentProjectId.current === requestedProjectId
          && currentDatasetId.current === requestedDatasetId
          && sampleSequence.current === sequence
        ) onMessage(error instanceof Error ? error.message : String(error));
      }
    },
    [onMessage],
  );

  const maxCount = useMemo(() => (distribution ? Math.max(1, ...distribution.bins.map((b) => b.count)) : 1), [distribution]);
  const completedJobs = datasetJobs.filter((job) => job.state === "COMPLETED");
  const currentResult = sampleQuality?.results.find((r) => r.job_id === explorerJobId) ?? sampleQuality?.results[0] ?? null;

  if (!projectId) {
    return (
      <section className="panel workbench" aria-labelledby="workbench-heading" data-testid="workbench">
        <h2 id="workbench-heading">Dataset · Batch · Explorer</h2>
        <div className="empty-state">
          <p>Select or create a project to import a dataset and run batch quality.</p>
          <a className="button primary" href="#/projects">Open projects</a>
        </div>
      </section>
    );
  }

  return (
    <section className="panel workbench" aria-labelledby="workbench-heading" data-testid="workbench">
      <h2 id="workbench-heading">Dataset · Batch · Explorer</h2>
      {feedPhase === "loading" && <p className="status" role="status">Loading project datasets and quality jobs…</p>}
      {feedError && <p className="error" role="alert">{feedError} <button type="button" className="secondary" onClick={() => void refresh()}>Retry</button></p>}
      {jobPollError && <p className="error" role="alert">{jobPollError} <button type="button" className="secondary" onClick={() => { setJobPollError(null); void refresh(); }}>Retry status</button></p>}
      <div className="workbench-grid">
        <div className="wb-column">
          <h3>Dataset</h3>
          {usableDatasets.length > 0 && (
            <label className="field-stack" htmlFor="workbench-dataset">
              Active dataset
              <select id="workbench-dataset" value={dataset?.dataset_id ?? ""} onChange={(event) => setActiveDatasetId(event.target.value)}>
                {usableDatasets.map((item) => <option key={item.dataset_id} value={item.dataset_id}>{item.name} · {item.sample_count.toLocaleString()} samples{item.subject_count === null ? " · pair-labelled protocol" : ""}</option>)}
              </select>
            </label>
          )}
          {dataset ? (
            <dl className="facts" data-testid="dataset-facts">
              <dt>Name</dt>
              <dd>{dataset.name}</dd>
              <dt>Samples</dt>
              <dd>{dataset.sample_count.toLocaleString()} · {dataset.subject_count === null ? "subject identities unavailable" : `${dataset.subject_count.toLocaleString()} subjects`}</dd>
              <dt>Rights</dt>
              <dd className="mono">{dataset.rights_status}</dd>
              <dt>Scope</dt>
              <dd className="mono">{dataset.common_scopes_with_engine.join(", ") || "no common scope with the engine"}</dd>
              <dt>Manifest</dt>
              <dd className="mono">{shortHash(dataset.manifest_sha256, 16)}</dd>
              {statistics?.evaluation_support && statistics?.data_completeness && (
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
            <p className="hint">No executable multi-sample dataset is recorded in this project.</p>
          )}
          <details className="import-surface" open={!dataset}>
            <summary>Import real dataset</summary>
            <div className="import-stack">
              <p className="hint">Registered LFW is verified in place. Owner imports copy admitted real images into private local storage and preserve their source kind.</p>
              {usableDatasets.some((item) => item.manifest["dataset_resource_id"] === "lfw-funneled-local-candidate") ? (
                <span className="state-pill ok" data-testid="import-lfw">Registered LFW imported</span>
              ) : (
                <button type="button" className="secondary" data-testid="import-lfw" disabled={busy} title={busy ? "Wait for the current dataset operation to finish" : undefined} onClick={() => void importLfw()}>Import registered LFW</button>
              )}
              <fieldset>
                <legend>Owner directory</legend>
                <label className="field-stack">Dataset name<input value={ownerDirectoryName} onChange={(event) => setOwnerDirectoryName(event.target.value)} /></label>
                <label className="field-stack">Local directory path<input className="mono" value={ownerDirectory} onChange={(event) => setOwnerDirectory(event.target.value)} /></label>
                <p className="hint" id="owner-directory-requirements">Provide a dataset name and local directory. Each immediate subdirectory name becomes the operator-declared subject label.</p>
                <button type="button" className="secondary" aria-describedby="owner-directory-requirements" title={!ownerDirectoryName ? "Enter a dataset name" : !ownerDirectory ? "Enter a local directory path" : busy ? "Wait for the current dataset operation to finish" : undefined} disabled={busy || !ownerDirectory || !ownerDirectoryName} onClick={() => void importOwnerDirectory()}>Import owner directory</button>
              </fieldset>
              <fieldset>
                <legend>Owner CSV manifest</legend>
                <label className="field-stack">Dataset name<input value={ownerManifestName} onChange={(event) => setOwnerManifestName(event.target.value)} /></label>
                <label className="field-stack">Local CSV path<input className="mono" value={ownerManifest} onChange={(event) => setOwnerManifest(event.target.value)} /></label>
                <label className="field-stack">Subject ID column<input className="mono" value={manifestSubjectColumn} onChange={(event) => setManifestSubjectColumn(event.target.value)} /></label>
                <label className="field-stack">Image path column<input className="mono" value={manifestPathColumn} onChange={(event) => setManifestPathColumn(event.target.value)} /></label>
                <label className="field-stack">Reusable mapping name<input className="mono" value={mappingName} onChange={(event) => setMappingName(event.target.value)} /></label>
                <p className="hint" id="owner-manifest-requirements">All five fields are required. The saved mapping name makes the declared CSV column contract reusable.</p>
                <button type="button" className="secondary" aria-describedby="owner-manifest-requirements" title={busy ? "Wait for the current dataset operation to finish" : !ownerManifestName || !ownerManifest || !manifestSubjectColumn || !manifestPathColumn || !mappingName ? "Complete all owner-manifest fields" : undefined} disabled={busy || !ownerManifest || !ownerManifestName || !manifestSubjectColumn || !manifestPathColumn || !mappingName} onClick={() => void importOwnerManifest()}>Import owner manifest</button>
              </fieldset>
            </div>
          </details>
        </div>

        <div className="wb-column">
          <h3>Batch quality</h3>
          {dataset ? (
            <>
              <div className="controls-row">
                <select value={selection} onChange={(event) => setSelection(event.target.value as "subset_fixture" | "all")} data-testid="batch-selection" aria-label="Batch selection">
                  {dataset.manifest["dataset_resource_id"] === "lfw-funneled-local-candidate" && <option value="subset_fixture">Registered subset fixture (fold-1 pairs)</option>}
                  <option value="all">All samples ({dataset.sample_count.toLocaleString()})</option>
                </select>
                <button type="button" className="primary" data-testid="batch-start" title={busy ? "Wait for the current dataset operation to finish" : activeJob !== null && !activeJob.ended_at ? `Batch ${shortHash(activeJob.job_id)} is still ${activeJob.state.toLowerCase()}` : undefined} disabled={busy || (activeJob !== null && !activeJob.ended_at)} onClick={() => void startBatch()}>
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
                  {activeJob.run_id && projectId && (
                    <p className="mono">
                      run <EvidenceOpenButton testId="workbench-active-run-evidence" request={{ kind: "run", href: `/api/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(activeJob.run_id)}`, evidenceId: activeJob.run_id, projectId, label: `Recorded Run ${shortHash(activeJob.run_id, 16)}` }} onOpen={onOpenEvidence}>{shortHash(activeJob.run_id, 16)}</EvidenceOpenButton>
                    </p>
                  )}
                </div>
              )}
              {datasetJobs.length > 0 && (
                <ul className="job-list" data-testid="job-list">
                  {datasetJobs.map((job) => (
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
                {distribution.run_id && projectId ? <EvidenceOpenButton testId="workbench-distribution-run-evidence" request={{ kind: "run", href: `/api/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(distribution.run_id)}`, evidenceId: distribution.run_id, projectId, label: `Distribution Run ${shortHash(distribution.run_id, 12)}` }} onOpen={onOpenEvidence}>{shortHash(distribution.run_id, 12)}</EvidenceOpenButton> : "—"}
              </p>
              <svg className="histogram" viewBox="0 0 400 140" role="img" aria-label="UnifiedQualityScore distribution" data-testid="histogram">
                {distribution.bins.map((bin, index) => {
                  const height = (bin.count / maxCount) * 110;
                  const width = 400 / distribution.bins.length;
                  return (
                    <g
                      key={bin.bin}
                      role="button"
                      tabIndex={0}
                      aria-label={`Quality ${Math.round(bin.low)} to ${Math.round(bin.high)}: ${bin.count} samples`}
                      onClick={() => void selectBin(index)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          void selectBin(index);
                        }
                      }}
                      style={{ cursor: "pointer" }}
                      data-testid={`bin-${index}`}
                    >
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
                      <img src={row.links.image} alt={`Recorded sample ${shortHash(row.sample_id, 14)}`} />
                      <span className="mono">{row.uqs_scalar ?? "—"}</span>
                      <span className="sample-meta">sample {shortHash(row.sample_id, 14)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {sampleQuality && currentResult && (
            <div className="sample-quality" data-testid="sample-quality">
              <h4>{sampleQuality.sample.subject_id ?? `Sample ${shortHash(sampleQuality.sample.sample_id, 14)}`}</h4>
              <dl className="facts">
                <dt>Subject</dt>
                <dd>{sampleQuality.sample.subject_id ?? "Not recorded"}</dd>
                <dt>Media</dt>
                <dd>{sampleQuality.sample.media_type} · {sampleQuality.sample.size_bytes.toLocaleString()} bytes</dd>
                <dt>SHA-256</dt>
                <dd className="mono">{shortHash(sampleQuality.sample.sha256, 20)}</dd>
                <dt>Outcome</dt>
                <dd>{currentResult.outcome}{currentResult.failure_reason ? ` — ${currentResult.failure_reason}` : ""}</dd>
                <dt>Run</dt>
                <dd>{currentResult.links.run && currentResult.run_id && projectId ? <EvidenceOpenButton testId="workbench-sample-run-evidence" request={{ kind: "run", href: currentResult.links.run, evidenceId: currentResult.run_id, projectId, label: `Sample-quality Run ${shortHash(currentResult.run_id, 16)}` }} onOpen={onOpenEvidence} className="mono">{shortHash(currentResult.run_id, 16)}</EvidenceOpenButton> : "—"}</dd>
                <dt>Audit</dt>
                <dd>{currentResult.links.audit && currentResult.run_id && projectId ? <EvidenceOpenButton testId="workbench-run-audit-evidence" request={{ kind: "audit", href: currentResult.links.audit, evidenceId: currentResult.run_id, projectId, label: `Run audit ${shortHash(currentResult.run_id, 16)}` }} onOpen={onOpenEvidence}>audit trail</EvidenceOpenButton> : "—"}</dd>
              </dl>
              <table className="measures compact" data-testid="sample-measure-table">
                <caption>Recorded quality measures for the selected sample</caption>
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

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, type AddonCatalogOut, type DegradationOut, type DegradationSummary, type DerivedSetOut, type IdentificationProtocolOut, type IdentificationSearchOut, type IdentificationSearchSummary, type JobOut, type MatcherAddonOut, type PreprocessingAddonOut, type TransformAddonOut } from "./api";
import { numericParameterError } from "./parameterContract";
import { EvidenceOpenButton, type EvidenceRequest } from "./EvidenceDetail";

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

function finiteParameters(value: string): number[] | null {
  const parts = value.split(",").map((item) => item.trim());
  if (parts.length === 0 || parts.some((item) => item.length === 0)) return null;
  const values = parts.map(Number);
  return values.every(Number.isFinite) ? values : null;
}

function transformParameterError(item: TransformAddonOut, value: string): string | null {
  const constraint = item.parameter_contract;
  const parameters = finiteParameters(value);
  if (parameters === null) return `Enter one or more finite ${constraint.guidance}, separated by commas.`;
  const valid = parameters.every((parameter) => {
    if (constraint.value_type === "integer" && !Number.isInteger(parameter)) return false;
    if (constraint.minimum !== undefined && (constraint.minimum_exclusive ? parameter <= constraint.minimum : parameter < constraint.minimum)) return false;
    if (constraint.maximum !== undefined && (constraint.maximum_exclusive ? parameter >= constraint.maximum : parameter > constraint.maximum)) return false;
    return true;
  });
  return valid ? null : `Every value must be ${constraint.guidance}.`;
}

interface AdvancedProps {
  projectId: string | null;
  datasetId: string | null;
  identityAuthorityAvailable: boolean;
  datasetRepresentation: "SOURCE_IMAGE_REQUIRING_DETECTION" | "AUTHOR_PREALIGNED_ARCFACE112";
  onMessage: (message: string) => void;
  onWorkflowTerminal?: () => void;
  onOpenEvidence: (request: EvidenceRequest) => void;
}

/** Phase 6 advanced analysis: 1:N identification (QS-X60) and controlled degradation responses (QS-X61). */
export function Advanced({ projectId, datasetId, identityAuthorityAvailable, datasetRepresentation, onMessage, onWorkflowTerminal, onOpenEvidence }: AdvancedProps) {
  const contextKey = `${projectId ?? ""}\u001f${datasetId ?? ""}`;
  const [protocols, setProtocols] = useState<IdentificationProtocolOut[]>([]);
  const [searches, setSearches] = useState<IdentificationSearchSummary[]>([]);
  const [derivedSets, setDerivedSets] = useState<DerivedSetOut[]>([]);
  const [degradations, setDegradations] = useState<DegradationSummary[]>([]);
  const [jobs, setJobs] = useState<JobOut[]>([]);
  const [selection, setSelection] = useState<"all" | "subset_fixture">("subset_fixture");
  const [matcherOptions, setMatcherOptions] = useState<MatcherAddonOut[]>([]);
  const [profileOptions, setProfileOptions] = useState<PreprocessingAddonOut[]>([]);
  const [transformOptions, setTransformOptions] = useState<TransformAddonOut[]>([]);
  const [analysisContracts, setAnalysisContracts] = useState<AddonCatalogOut["analysis_parameters"] | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [feedError, setFeedError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [matcher, setMatcher] = useState<string>("");
  const [condition, setCondition] = useState<string>("");
  const [runningSearch, setRunningSearch] = useState<string | null>(null);
  const [searchDetail, setSearchDetail] = useState<IdentificationSearchOut | null>(null);
  const [candidates, setCandidates] = useState<any[] | null>(null);
  const [selectedTransforms, setSelectedTransforms] = useState<string[]>([]);
  const [transformParameters, setTransformParameters] = useState<Record<string, string>>({});
  const [threshold, setThreshold] = useState<string>("0.8");
  const [runningDegradation, setRunningDegradation] = useState<string | null>(null);
  const [degradationDetail, setDegradationDetail] = useState<DegradationOut | null>(null);
  const [busy, setBusy] = useState(false);
  const contextEpoch = useRef(0);
  const contextKeyRef = useRef(contextKey);
  const refreshSequence = useRef(0);
  const catalogSequence = useRef(0);
  const handledTerminalWorkflows = useRef(new Set<string>());

  const loadCatalog = useCallback(async () => {
    const sequence = ++catalogSequence.current;
    setCatalogLoading(true);
    setCatalogError(null);
    try {
      const catalog = await api.addons();
      if (catalogSequence.current !== sequence) return;
      setMatcherOptions(catalog.matchers);
      setProfileOptions(catalog.preprocessing.filter((item) => item.input_representation === datasetRepresentation && item.runtime_state === "READY"));
      setTransformOptions(catalog.transforms);
      setAnalysisContracts(catalog.analysis_parameters);
      setMatcher((current) => catalog.matchers.some((item) => item.matcher_id === current) ? current : (catalog.matchers[0]?.matcher_id ?? ""));
      setCondition((current) => catalog.preprocessing.some((item) => item.addon_id === current) ? current : (catalog.preprocessing[0]?.addon_id ?? ""));
      setSelectedTransforms((current) => current.filter((operator) => catalog.transforms.some((item) => item.operator === operator)));
      setCatalogError(null);
    } catch (error) {
      if (catalogSequence.current !== sequence) return;
      setCatalogError(error instanceof Error ? error.message : String(error));
    } finally {
      if (catalogSequence.current === sequence) setCatalogLoading(false);
    }
  }, [datasetRepresentation]);

  useEffect(() => {
    void loadCatalog();
    return () => {
      catalogSequence.current += 1;
    };
  }, [loadCatalog]);

  const refresh = useCallback(async () => {
    const epoch = contextEpoch.current;
    const requestContextKey = contextKey;
    const sequence = ++refreshSequence.current;
    if (!projectId || !datasetId) return;
    setFeedError(null);
    const results = await Promise.allSettled([
      api.identificationProtocols(projectId),
      api.identificationSearches(projectId),
      api.derivedSets(projectId),
      api.degradations(projectId),
      api.projectJobs(projectId),
    ]);
    if (contextEpoch.current !== epoch || contextKeyRef.current !== requestContextKey || refreshSequence.current !== sequence) return;
    const [protocolResult, searchResult, derivedResult, degradationResult, jobResult] = results;
    const errors: string[] = [];
    const scopedProtocols = protocolResult.status === "fulfilled"
      ? protocolResult.value.filter((protocol) => protocol.dataset_id === datasetId)
      : [];
    const protocolIds = new Set(scopedProtocols.map((protocol) => protocol.protocol_id));
    const scopedSearches = searchResult.status === "fulfilled"
      ? searchResult.value.filter((search) => protocolIds.has(search.protocol_id))
      : [];
    const scopedDerivedSets = derivedResult.status === "fulfilled"
      ? derivedResult.value.filter((set) => set.source_dataset_id === datasetId || set.derived_dataset_id === datasetId)
      : [];
    const derivedSetIds = new Set(scopedDerivedSets.map((set) => set.derived_set_id));
    const scopedDegradations = degradationResult.status === "fulfilled"
      ? degradationResult.value.filter((analysis) => derivedSetIds.has(analysis.derived_set_id))
      : [];
    const allowedJobDatasetIds = new Set<string>([datasetId]);
    scopedDerivedSets.forEach((set) => {
      if (set.source_dataset_id === datasetId) allowedJobDatasetIds.add(set.derived_dataset_id);
      if (set.derived_dataset_id === datasetId) allowedJobDatasetIds.add(set.source_dataset_id);
    });
    const scopedJobs = jobResult.status === "fulfilled"
      ? jobResult.value.filter((job) => job.state === "COMPLETED" && job.run_id && job.dataset_id !== null && job.dataset_id !== undefined && allowedJobDatasetIds.has(job.dataset_id))
      : [];

    setProtocols(scopedProtocols);
    setSearches(scopedSearches);
    setDerivedSets(scopedDerivedSets);
    setDegradations(scopedDegradations);
    setJobs(scopedJobs);

    if (protocolResult.status === "rejected") errors.push(`Identification protocol feed unavailable: ${protocolResult.reason instanceof Error ? protocolResult.reason.message : String(protocolResult.reason)}`);
    if (searchResult.status === "rejected") errors.push(`Identification search feed unavailable: ${searchResult.reason instanceof Error ? searchResult.reason.message : String(searchResult.reason)}`);
    else if (protocolResult.status === "rejected") errors.push("Identification searches are hidden because their protocol lineage could not be verified.");
    if (derivedResult.status === "rejected") errors.push(`Derived-set feed unavailable: ${derivedResult.reason instanceof Error ? derivedResult.reason.message : String(derivedResult.reason)}`);
    if (degradationResult.status === "rejected") errors.push(`Degradation feed unavailable: ${degradationResult.reason instanceof Error ? degradationResult.reason.message : String(degradationResult.reason)}`);
    else if (derivedResult.status === "rejected") errors.push("Degradation analyses are hidden because their derived-set lineage could not be verified.");
    if (jobResult.status === "rejected") errors.push(`Quality-job feed unavailable: ${jobResult.reason instanceof Error ? jobResult.reason.message : String(jobResult.reason)}`);
    setFeedError(errors.length ? errors.join(" · ") : null);
  }, [projectId, datasetId, contextKey]);

  useLayoutEffect(() => {
    contextKeyRef.current = contextKey;
    contextEpoch.current += 1;
    refreshSequence.current += 1;
    setProtocols([]); setSearches([]); setDerivedSets([]); setDegradations([]); setJobs([]);
    setRunningSearch(null); setRunningDegradation(null);
    setSearchDetail(null); setCandidates(null); setDegradationDetail(null);
    setPollError(null); setFeedError(null); setBusy(false);
  }, [contextKey]);

  useEffect(() => {
    void refresh();
    const handle = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(handle);
  }, [refresh]);

  useEffect(() => {
    if (!runningSearch || !projectId) return;
    const epoch = contextEpoch.current;
    const searchId = runningSearch;
    const handle = window.setInterval(() => {
      api.identificationSearch(projectId, searchId).then((status) => {
        if (contextEpoch.current !== epoch) return;
        setPollError(null);
        if (status.state === "COMPLETED" || status.state === "FAILED" || status.state === "INTERRUPTED") {
          const terminalKey = `identification:${searchId}`;
          if (handledTerminalWorkflows.current.has(terminalKey)) return;
          handledTerminalWorkflows.current.add(terminalKey);
          setRunningSearch(null);
          if (status.state === "COMPLETED") {
            setSearchDetail(status);
            onMessage(`Identification search ${shortHash(status.search_id)} recorded as Run ${shortHash(status.run_id ?? "")}`);
          } else {
            onMessage(`Identification search ${status.state === "INTERRUPTED" ? "was interrupted" : "failed"}: ${status.error ?? "unknown"}`);
          }
          onWorkflowTerminal?.();
          void refresh();
        }
      }).catch((error) => {
        if (contextEpoch.current === epoch) setPollError(`Identification status is temporarily unavailable; completion is not assumed. ${error instanceof Error ? error.message : String(error)}`);
      });
    }, 2000);
    return () => window.clearInterval(handle);
  }, [runningSearch, projectId, refresh, onMessage, onWorkflowTerminal]);

  useEffect(() => {
    if (!runningDegradation || !projectId) return;
    const epoch = contextEpoch.current;
    const analysisId = runningDegradation;
    const handle = window.setInterval(() => {
      api.degradation(projectId, analysisId).then((status) => {
        if (contextEpoch.current !== epoch) return;
        setPollError(null);
        if (status.state === "COMPLETED" || status.state === "FAILED" || status.state === "INTERRUPTED") {
          const terminalKey = `degradation:${analysisId}`;
          if (handledTerminalWorkflows.current.has(terminalKey)) return;
          handledTerminalWorkflows.current.add(terminalKey);
          setRunningDegradation(null);
          if (status.state === "COMPLETED") {
            setDegradationDetail(status);
            onMessage(`Degradation analysis ${shortHash(status.analysis_id)} recorded as Run ${shortHash(status.run_id ?? "")}`);
          } else {
            onMessage(`Degradation analysis ${status.state === "INTERRUPTED" ? "was interrupted" : "failed"}: ${status.error ?? "unknown"}`);
          }
          onWorkflowTerminal?.();
          void refresh();
        }
      }).catch((error) => {
        if (contextEpoch.current === epoch) setPollError(`Degradation status is temporarily unavailable; completion is not assumed. ${error instanceof Error ? error.message : String(error)}`);
      });
    }, 2000);
    return () => window.clearInterval(handle);
  }, [runningDegradation, projectId, refresh, onMessage, onWorkflowTerminal]);

  const guard = async (label: string, action: (epoch: number) => Promise<string>) => {
    const actionContextKey = contextKey;
    if (contextKeyRef.current !== actionContextKey) return;
    const epoch = contextEpoch.current;
    setBusy(true);
    try {
      const message = await action(epoch);
      if (contextEpoch.current !== epoch || contextKeyRef.current !== actionContextKey) return;
      onMessage(message);
      void refresh();
    } catch (error) {
      if (contextEpoch.current === epoch && contextKeyRef.current === actionContextKey) onMessage(`${label}: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      if (contextEpoch.current === epoch && contextKeyRef.current === actionContextKey) setBusy(false);
    }
  };

  const freeze = () => guard("freeze", async () => {
    if (!projectId || !datasetId) throw new Error("no dataset");
    const record = await api.freezeIdentification(projectId, datasetId, selection, 20);
    return `Identification protocol ${shortHash(record.protocol_id)}: gallery ${record.gallery_count}, mated probes ${record.mated_probe_count}, non-mated probes ${record.nonmated_probe_count}`;
  });
  const search = (protocolId: string) => guard("search", async (epoch) => {
    if (!projectId || !datasetId) throw new Error("no active dataset");
    if (!matcher || !condition) throw new Error("the admitted add-on catalog has no matcher/preprocessing selection");
    const protocol = protocols.find((item) => item.protocol_id === protocolId);
    if (!protocol || protocol.dataset_id !== datasetId) throw new Error("the selected identification protocol is not recorded for the active dataset");
    // Probe-quality effects may only use a completed batch from the protocol's dataset.
    const qualityJob = jobs
      .filter((job) => job.dataset_id === protocol.dataset_id)
      .reduce<JobOut | null>((best, job) => (best === null || job.assessed > best.assessed ? job : best), null);
    const started = await api.startIdentificationSearch(projectId, protocolId, { matcher_id: matcher, condition_id: condition, quality_job_id: qualityJob?.job_id ?? null });
    if (contextEpoch.current !== epoch) return "";
    setRunningSearch(started.search_id);
    return `Identification search ${shortHash(started.search_id)} running on the matcher device`;
  });
  const openSearch = (searchId: string) => guard("open", async (epoch) => {
    if (!projectId) throw new Error("no active project");
    if (!searches.some((search) => search.search_id === searchId && protocols.some((protocol) => protocol.protocol_id === search.protocol_id && protocol.dataset_id === datasetId))) {
      throw new Error("the selected identification search is not recorded for the active dataset");
    }
    const status = await api.identificationSearch(projectId, searchId);
    if (contextEpoch.current !== epoch) return "";
    setSearchDetail(status); setCandidates(null);
    return `Search ${shortHash(searchId)} loaded`;
  });
  const showCandidates = (kind: string) => guard("candidates", async (epoch) => {
    if (!projectId) throw new Error("no active project");
    if (!searchDetail) throw new Error("no search");
    const protocolId = searchDetail.summary?.protocol_id;
    if (!protocolId || !protocols.some((protocol) => protocol.protocol_id === protocolId && protocol.dataset_id === datasetId)) {
      throw new Error("the loaded identification search is not recorded for the active dataset");
    }
    const body = await api.identificationCandidates(projectId, searchDetail.search_id, `probe_kind=${kind}&rank_le=5&limit=100`);
    if (contextEpoch.current !== epoch) return "";
    setCandidates(body.rows);
    return `${body.total} candidate rows (${kind}, rank ≤ 5)`;
  });
  const derive = () => guard("transform", async () => {
    if (!projectId || !datasetId) throw new Error("no dataset");
    if (transformOptions.length === 0) throw new Error("the admitted add-on catalog has no controlled-degradation operators");
    const selected = transformOptions.filter((item) => selectedTransforms.includes(item.operator));
    if (selected.length === 0) throw new Error("select at least one admitted controlled-degradation operator");
    const transforms = selected.map((item) => {
      const validationError = transformParameterError(item, transformParameters[item.operator] ?? "");
      if (validationError) throw new Error(`${item.display_name}: ${validationError}`);
      const parameters = finiteParameters(transformParameters[item.operator] ?? "");
      if (parameters === null) throw new Error(`${item.display_name} parameters could not be parsed`);
      return { kind: item.operator, parameters };
    });
    const record = await api.createTransformations(projectId, datasetId, { selection, transforms });
    return `Derived set ${shortHash(record.derived_set_id)}: ${record.derived_count} images (${record.levels} levels × ${record.source_count} sources); run a quality batch on dataset ${shortHash(record.derived_dataset_id)} next`;
  });
  const analyse = (set: DerivedSetOut) => guard("degradation", async (epoch) => {
    if (!projectId || !datasetId) throw new Error("no active dataset");
    if (!matcher || !condition) throw new Error("the admitted add-on catalog has no matcher/preprocessing selection");
    const activeSet = derivedSets.find((item) => item.derived_set_id === set.derived_set_id && (item.source_dataset_id === datasetId || item.derived_dataset_id === datasetId));
    if (!activeSet) throw new Error("the selected derived set is not in the active dataset lineage");
    const matcherThreshold = Number(threshold);
    const thresholdError = numericParameterError(threshold, analysisContracts?.matcher_threshold ?? null, "Matcher threshold");
    if (thresholdError || !Number.isFinite(matcherThreshold)) throw new Error(thresholdError ?? "Matcher threshold must be a finite score value.");
    const current = (await api.projectJobs(projectId)).filter((job) => job.state === "COMPLETED" && job.run_id);
    const derivedJob = current.find((job) => job.dataset_id === activeSet.derived_dataset_id) ?? null;
    const baseline = current.find((job) => job.dataset_id === activeSet.source_dataset_id) ?? null;
    if (!derivedJob) throw new Error("run the quality batch over the derived dataset first (Dataset panel → derived dataset)");
    const started = await api.startDegradation(projectId, { derived_set_id: activeSet.derived_set_id, quality_job_id: derivedJob.job_id, baseline_quality_job_id: baseline?.job_id ?? null, matcher_id: matcher, condition_id: condition, matcher_threshold: matcherThreshold });
    if (contextEpoch.current !== epoch) return "";
    setRunningDegradation(started.analysis_id);
    return `Degradation analysis ${shortHash(started.analysis_id)} running`;
  });
  const openDegradation = (id: string) => guard("open", async (epoch) => {
    if (!projectId) throw new Error("no active project");
    if (!degradations.some((analysis) => analysis.analysis_id === id && derivedSets.some((set) => set.derived_set_id === analysis.derived_set_id))) {
      throw new Error("the selected degradation analysis is not in the active dataset lineage");
    }
    const detail = await api.degradation(projectId, id);
    if (contextEpoch.current !== epoch) return "";
    setDegradationDetail(detail);
    return `Degradation ${shortHash(id)} loaded`;
  });
  const qualityOverDerived = (set: DerivedSetOut) => guard("batch", async () => {
    if (!projectId || !datasetId) throw new Error("no active dataset");
    const activeSet = derivedSets.find((item) => item.derived_set_id === set.derived_set_id && (item.source_dataset_id === datasetId || item.derived_dataset_id === datasetId));
    if (!activeSet) throw new Error("the selected derived set is not in the active dataset lineage");
    const job = await api.startBatch(projectId, activeSet.derived_dataset_id, { kind: "all" }, 1, null);
    return `OFIQ batch ${shortHash(job.job_id)} started over ${activeSet.derived_count} derived images; analyse when it completes`;
  });

  const catalogHasOptions = matcherOptions.length > 0 && profileOptions.length > 0 && transformOptions.length > 0 && analysisContracts !== null;
  const catalogStatus = (
    <>
      {catalogLoading && <p className="status" role="status">{catalogHasOptions ? "Refreshing the admitted add-on catalog…" : "Loading the admitted add-on catalog…"}</p>}
      {catalogError && (
        <p className="error" role="alert">
          Add-on catalog unavailable: {catalogError}. {catalogHasOptions ? "Previously verified admitted options remain available." : "Catalog-dependent actions remain disabled."}{" "}
          <button type="button" className="secondary" disabled={catalogLoading} onClick={() => void loadCatalog()} data-testid="advanced-catalog-retry">{catalogLoading ? "Retrying catalog…" : "Retry add-on catalog"}</button>
        </p>
      )}
    </>
  );

  if (!projectId) {
    return (
      <section className="panel advanced" aria-labelledby="advanced-heading" data-testid="advanced">
        <h2 id="advanced-heading">Advanced analysis · 1:N identification · controlled degradation</h2>
        {catalogStatus}
        <div className="empty-state">
          <strong>Select a project to configure advanced analysis.</strong>
          <span>Identification and controlled degradation require an imported dataset recorded in a project.</span>
          <a className="button secondary" href="#/projects">Open projects</a>
        </div>
      </section>
    );
  }
  const closed = searchDetail?.metrics?.closed_set?.EXCLUDE_FAILED_TRIALS;
  const openSet = searchDetail?.metrics?.open_set?.EXCLUDE_FAILED_TRIALS;
  const selectedTransformOptions = transformOptions.filter((item) => selectedTransforms.includes(item.operator));
  const transformErrors = Object.fromEntries(selectedTransformOptions.map((item) => [item.operator, transformParameterError(item, transformParameters[item.operator] ?? "")]));
  const thresholdError = numericParameterError(threshold, analysisContracts?.matcher_threshold ?? null, "Matcher threshold");
  const freezeDisabledReason = busy
    ? "An operation is already in progress."
    : !datasetId
      ? "Select an active dataset before freezing a protocol."
      : !identityAuthorityAvailable
        ? "1:N identification requires recorded subject identities; this pair-labelled dataset does not publish them."
        : null;
  const searchDisabledReason = busy
    ? "An operation is already in progress."
    : !identityAuthorityAvailable
      ? "1:N identification is unavailable because the active dataset has no recorded subject identities."
    : runningSearch !== null
      ? "Wait for the running identification search to finish."
      : !matcher
        ? "Select a ready matcher from the admitted add-on catalog."
        : !condition
          ? "Select a ready preprocessing profile from the admitted add-on catalog."
          : null;
  const transformRunDisabledReason = busy
    ? "An operation is already in progress."
    : !datasetId
      ? "Select an active dataset before creating a derived set."
      : catalogLoading && !catalogHasOptions
        ? "Wait for the admitted transform catalog to load."
        : catalogError !== null && !catalogHasOptions
          ? "Retry the admitted transform catalog before creating a derived set."
          : transformOptions.length === 0
          ? "No controlled-degradation operators are admitted."
          : selectedTransformOptions.length === 0
            ? "Select at least one admitted transform operator."
            : selectedTransformOptions.map((item) => transformErrors[item.operator]).find((error) => error !== null) ?? null;
  const setIsInActiveLineage = (set: DerivedSetOut): boolean => datasetId !== null && (set.source_dataset_id === datasetId || set.derived_dataset_id === datasetId);
  const derivedBatchDisabledReason = (set: DerivedSetOut): string | null => busy
    ? "An operation is already in progress."
    : !setIsInActiveLineage(set)
      ? "This derived set is not in the active dataset lineage."
      : null;
  const degradationDisabledReason = (set: DerivedSetOut): string | null => busy
    ? "An operation is already in progress."
    : !setIsInActiveLineage(set)
      ? "This derived set is not in the active dataset lineage."
      : runningDegradation !== null
        ? "Wait for the running degradation analysis to finish."
        : !matcher
          ? "Select a ready matcher from the admitted add-on catalog."
          : !condition
            ? "Select a ready preprocessing profile from the admitted add-on catalog."
            : thresholdError
              ? thresholdError
              : !jobs.some((job) => job.dataset_id === set.derived_dataset_id)
                ? "Run OFIQ over this derived dataset and wait for that quality job to complete."
                : null;
  const matcherLabel = (matcherId: string) => matcherOptions.find((item) => item.matcher_id === matcherId)?.name ?? matcherId;
  const profileLabel = (profileId: string) => profileOptions.find((item) => item.addon_id === profileId)?.display_name ?? profileId;
  const searchRunId = searchDetail?.run_id ?? null;
  const degradationRunId = degradationDetail?.run_id ?? null;
  return (
    <section className="panel advanced" aria-labelledby="advanced-heading" data-testid="advanced">
      <h2 id="advanced-heading">Advanced analysis · 1:N identification · controlled degradation</h2>
      {catalogStatus}
      {feedError && <p className="error" role="alert">{feedError}</p>}
      {pollError && <p className="error" role="alert">{pollError}</p>}
      {!identityAuthorityAvailable && datasetId && <p className="hint">The active dataset has authoritative verification-pair labels but no subject identities. Pair verification and batch quality remain available; 1:N protocol creation and search are disabled.</p>}
      <div className="controls-row utility-controls">
        <label>Selection<select value={selection} onChange={(e) => setSelection(e.target.value as "all" | "subset_fixture")} data-testid="adv-selection"><option value="subset_fixture">subset fixture</option><option value="all">all samples</option></select></label>
        <label>Matcher<select value={matcher} onChange={(e) => setMatcher(e.target.value)} disabled={matcherOptions.length === 0} title={matcherOptions.length === 0 ? "No ready matcher is present in the admitted add-on catalog." : undefined} data-testid="adv-matcher">{matcherOptions.map((item) => <option key={item.matcher_id} value={item.matcher_id}>{item.name}</option>)}</select></label>
        <label>Preprocessing<select value={condition} onChange={(e) => setCondition(e.target.value)} disabled={profileOptions.length === 0} title={profileOptions.length === 0 ? "No ready preprocessing profile is present in the admitted add-on catalog." : undefined} data-testid="adv-condition">{profileOptions.map((item) => <option key={item.addon_id} value={item.addon_id}>{item.display_name}</option>)}</select></label>
        <button type="button" className="secondary" disabled={freezeDisabledReason !== null} aria-describedby="ident-freeze-help" title={freezeDisabledReason ?? "Freeze a reproducible 1:N protocol over the active dataset."} onClick={() => void freeze()} data-testid="ident-freeze">Freeze 1:N protocol</button>
        <span id="ident-freeze-help" className={freezeDisabledReason ? "error" : "hint"}>{freezeDisabledReason ?? "Ready to freeze the selected population into a recorded protocol."}</span>
      </div>
      <div className="utility-facts">
        <div>
          <h3>1:N protocols and searches</h3>
          <table className="compact" data-testid="ident-protocols">
            <caption>Frozen 1:N identification protocols</caption>
            <thead><tr><th scope="col">Protocol</th><th scope="col">Population and rank depth</th><th scope="col">Action</th></tr></thead>
            <tbody>
            {protocols.map((p) => (
              <tr key={p.protocol_id}><th scope="row" className="mono">{shortHash(p.protocol_id, 10)}</th><td>{p.selection} · gallery {p.gallery_count} · mated {p.mated_probe_count} · non-mated {p.nonmated_probe_count} · depth {p.rank_depth}</td><td><button type="button" className="secondary" disabled={searchDisabledReason !== null} aria-describedby="ident-search-help" title={searchDisabledReason ?? "Run this frozen protocol with the selected matcher and preprocessing profile."} onClick={() => void search(p.protocol_id)} data-testid="ident-search">Search</button></td></tr>
            ))}
          </tbody></table>
          <p id="ident-search-help" className={searchDisabledReason ? "error" : "hint"}>{searchDisabledReason ?? "Searches use the selected admitted matcher and preprocessing profile."}</p>
          <table className="compact" data-testid="ident-searches">
            <caption>Recorded 1:N identification searches</caption>
            <thead><tr><th scope="col">Search</th><th scope="col">Matcher · preprocessing · device</th><th scope="col">Gallery</th><th scope="col">Mated / non-mated</th><th scope="col">Rank-1</th><th scope="col">FNIR @ FPIR 1 %</th></tr></thead><tbody>
            {searches.map((s) => (
              <tr key={s.search_id}><th scope="row"><button type="button" className="cell" onClick={() => void openSearch(s.search_id)} data-testid="ident-open"><span className="mono">{shortHash(s.search_id, 10)}</span></button></th><td>{matcherLabel(s.matcher_id)} · {profileLabel(s.condition_id)} · {s.device}</td><td className="mono">{s.gallery_size}</td><td className="mono">{s.mated_searches} / {s.nonmated_searches}</td><td className="mono">{pct(s.rank_1)}</td><td className="mono">{pct(s.fnir_at_fpir_0_01)}</td></tr>
            ))}
          </tbody></table>
          {searchDetail?.metrics && (
            <div className="errors" data-testid="ident-detail">
              <h4>Search <span className="mono">{shortHash(searchDetail.search_id, 12)}</span> · Run {searchRunId ? <EvidenceOpenButton testId="advanced-search-run-evidence" request={{ kind: "run", href: `/api/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(searchRunId)}`, evidenceId: searchRunId, projectId, label: `Identification Run ${shortHash(searchRunId, 12)}` }} onOpen={onOpenEvidence} className="mono">{shortHash(searchRunId, 12)}</EvidenceOpenButton> : "Not recorded"}</h4>
              <div className="utility-facts">
                <div>
                  <h5>Closed set (mated searches)</h5>
                  <table className="compact"><caption>Closed-set rank results</caption><thead><tr><th scope="col">Rank</th><th scope="col">Identification rate and confidence interval</th></tr></thead><tbody>
                    {Object.entries(closed?.rank_k ?? {}).map(([k, block]: [string, any]) => <tr key={k}><th scope="row">rank-{k}</th><td className="mono">{pct(block.rate)} [{pct(block.ci_lower)}, {pct(block.ci_upper)}] · {block.events}/{block.trials}</td></tr>)}
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
                  <h5>Open set (FNIR at target FPIR; MATCHER THRESHOLD from non-mated searches)</h5>
                  <table className="compact"><caption>Open-set operating points</caption><thead><tr><th scope="col">Target FPIR</th><th scope="col">Threshold</th><th scope="col">FPIR</th><th scope="col">FNIR</th><th scope="col">TPIR</th><th scope="col">Support</th></tr></thead><tbody>
                    {(openSet?.operating_points ?? []).map((p: any) => <tr key={p.target_fpir}><th scope="row" className="mono">{p.target_fpir}</th><td className="mono">{p.threshold === null ? p.reason : num(p.threshold, 4)}</td><td className="mono">{pct(p.fpir, 3)}</td><td className="mono">{pct(p.fnir)}</td><td className="mono">{pct(p.tpir)}</td><td>{p.supported ? "supported" : "unsupported precision"}</td></tr>)}
                  </tbody></table>
                  <h5>Gallery-size effect</h5>
                  <table className="compact"><caption>Identification results by gallery size</caption><thead><tr><th scope="col">Gallery</th><th scope="col">Mated searches</th><th scope="col">Rank-1</th><th scope="col">FNIR @ 1 %</th></tr></thead><tbody>
                    {(searchDetail.metrics.gallery_effects?.gallery_sizes ?? []).map((g: any) => <tr key={g.gallery_size}><th scope="row" className="mono">{g.gallery_size}</th><td className="mono">{g.mated_searches}</td><td className="mono">{pct(g.rank_1)}</td><td className="mono">{pct(g.fnir_at_fpir_0_01)}</td></tr>)}
                  </tbody></table>
                  {searchDetail.metrics.probe_quality_effect && <p className="hint">Rank-1 by probe UQS bin: {searchDetail.metrics.probe_quality_effect.bins.map((b: any) => `[${b.quality_low},${b.quality_high}) ${pct(b.rank_1, 1)} (n=${b.mated_searches})`).join(" · ")}</p>}
                  <div className="controls-row"><button type="button" className="secondary" onClick={() => void showCandidates("MATED")} data-testid="ident-candidates">Mated candidate lists</button><button type="button" className="secondary" onClick={() => void showCandidates("NON_MATED")}>Non-mated candidate lists</button></div>
                  {candidates && <table className="compact" data-testid="ident-candidate-table"><caption>Candidate ranks for the selected probe population</caption><thead><tr><th scope="col">Probe</th><th scope="col">Rank</th><th scope="col">Gallery</th><th scope="col">Score</th><th scope="col">Mate?</th></tr></thead><tbody>{candidates.slice(0, 40).map((row) => <tr key={`${row.probe_sample_id}-${row.rank}`}><th scope="row" className="mono">{row.probe_subject_id}</th><td className="mono">{row.rank}</td><td className="mono">{row.gallery_subject_id}</td><td className="mono">{num(row.score, 4)}</td><td>{row.is_true_mate ? "true mate" : ""}</td></tr>)}</tbody></table>}
                </div>
              </div>
            </div>
          )}
        </div>
        <div>
          <h3>Controlled degradation (transform real inputs → OFIQ + matcher response)</h3>
          <fieldset className="addon-selection" disabled={busy || transformOptions.length === 0} data-testid="transform-operators">
            <legend>Admitted operators and parameter levels</legend>
            {transformOptions.map((item) => {
              const checkboxId = `transform-select-${item.operator}`;
              const parametersId = `transform-params-${item.operator}`;
              const parametersHelpId = `${parametersId}-help`;
              const parametersErrorId = `${parametersId}-error`;
              const selected = selectedTransforms.includes(item.operator);
              const parametersError = selected ? transformErrors[item.operator] : null;
              const constraint = item.parameter_contract;
              return (
                <div className="transform-control" key={item.addon_id}>
                  <label className="transform-toggle" htmlFor={checkboxId}>
                    <input
                      id={checkboxId}
                      type="checkbox"
                      checked={selected}
                      data-testid={checkboxId}
                      onChange={(event) => setSelectedTransforms((current) => event.target.checked ? [...current, item.operator] : current.filter((operator) => operator !== item.operator))}
                    />
                    {item.display_name}
                  </label>
                  <label className="transform-parameters" htmlFor={parametersId}>
                    <span>{item.parameter} values, comma-separated</span>
                    <input
                      id={parametersId}
                      type="text"
                      inputMode="decimal"
                      value={transformParameters[item.operator] ?? ""}
                      disabled={!selected}
                      aria-invalid={parametersError ? "true" : undefined}
                      aria-describedby={`${parametersHelpId}${parametersError ? ` ${parametersErrorId}` : ""}`}
                      data-testid={parametersId}
                      onChange={(event) => setTransformParameters((current) => ({ ...current, [item.operator]: event.target.value }))}
                    />
                    <span id={parametersHelpId} className="hint">Allowed units/domain: {constraint.guidance}.</span>
                    {parametersError && <span id={parametersErrorId} className="error" role="alert">{parametersError}</span>}
                  </label>
                </div>
              );
            })}
            <p className="hint">Select only the operators needed for this derived set and enter every parameter level explicitly.</p>
          </fieldset>
          <div className="controls-row utility-controls">
            <label>
              Matcher threshold (score)
              <input type="number" step="any" required value={threshold} onChange={(e) => setThreshold(e.target.value)} aria-invalid={thresholdError ? "true" : undefined} aria-describedby={`degradation-threshold-help${thresholdError ? " degradation-threshold-error" : ""}`} data-testid="degradation-threshold" />
              <span id="degradation-threshold-help" className="hint">{analysisContracts?.matcher_threshold.guidance ?? "Load the admitted scientific contract"}; acceptance is score greater than or equal to this threshold.</span>
              {thresholdError && <span id="degradation-threshold-error" className="error" role="alert">{thresholdError}</span>}
            </label>
            <button type="button" className="secondary" disabled={transformRunDisabledReason !== null} aria-describedby="transform-run-help" title={transformRunDisabledReason ?? "Create a provenance-recorded derived set from the selected real inputs."} onClick={() => void derive()} data-testid="transform-run">Create derived set</button>
            <span id="transform-run-help" className={transformRunDisabledReason ? "error" : "hint"}>{transformRunDisabledReason ?? "Ready to create a derived set with the selected valid transform levels."}</span>
          </div>
          <table className="compact" data-testid="derived-sets"><caption>Recorded derived datasets</caption><thead><tr><th scope="col">Derived set</th><th scope="col">Recorded lineage</th><th scope="col">Actions</th></tr></thead><tbody>
            {derivedSets.map((set) => (
              <tr key={set.derived_set_id}><th scope="row" className="mono">{shortHash(set.derived_set_id, 10)}</th><td>{set.derived_count} images · {set.levels} levels × {set.source_count} sources · derived dataset {shortHash(set.derived_dataset_id, 8)}</td><td className="controls-row"><button type="button" className="secondary" disabled={derivedBatchDisabledReason(set) !== null} title={derivedBatchDisabledReason(set) ?? "Run OFIQ over every recorded image in this derived dataset."} onClick={() => void qualityOverDerived(set)} data-testid="derived-batch">OFIQ over derived</button><button type="button" className="secondary" disabled={degradationDisabledReason(set) !== null} aria-describedby={`degradation-run-help-${set.derived_set_id}`} title={degradationDisabledReason(set) ?? "Analyse quality and matcher response across recorded degradation levels."} onClick={() => void analyse(set)} data-testid="degradation-run">Analyse response</button>{degradationDisabledReason(set) && <span id={`degradation-run-help-${set.derived_set_id}`} className="error">{degradationDisabledReason(set)}</span>}</td></tr>
            ))}
          </tbody></table>
          <table className="compact" data-testid="degradations"><caption>Recorded controlled-degradation analyses</caption><thead><tr><th scope="col">Analysis</th><th scope="col">Matcher · preprocessing</th><th scope="col">Transforms</th><th scope="col">Pairs</th></tr></thead><tbody>
            {degradations.map((d) => (
              <tr key={d.analysis_id}><th scope="row"><button type="button" className="cell" onClick={() => void openDegradation(d.analysis_id)} data-testid="degradation-open"><span className="mono">{shortHash(d.analysis_id, 10)}</span></button></th><td>{matcherLabel(d.matcher_id)} · {profileLabel(d.condition_id)} · T {num(d.matcher_threshold, 4)}</td><td>{d.transforms.join(", ")}</td><td className="mono">{d.mated_pairs} mated · {d.nonmated_pairs} non-mated</td></tr>
            ))}
          </tbody></table>
          {degradationDetail?.document && (
            <div className="errors" data-testid="degradation-detail">
              <h4>Degradation response · Run {degradationRunId ? <EvidenceOpenButton testId="advanced-degradation-run-evidence" request={{ kind: "run", href: `/api/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(degradationRunId)}`, evidenceId: degradationRunId, projectId, label: `Degradation Run ${shortHash(degradationRunId, 12)}` }} onOpen={onOpenEvidence} className="mono">{shortHash(degradationRunId, 12)}</EvidenceOpenButton> : "Not recorded"} · MATCHER THRESHOLD {num(degradationDetail.document.matcher_threshold.value, 4)}</h4>
              {degradationDetail.document.responses.map((r: any) => (
                <table className="compact" key={r.transform} data-testid="degradation-table"><caption>{r.transform} response by {r.parameter_name}</caption><thead><tr><th scope="col">{r.transform} ({r.parameter_name})</th><th scope="col">UQS mean</th><th scope="col">Sharpness mean</th><th scope="col">Mated score mean</th><th scope="col">FNMR</th><th scope="col">FMR</th><th scope="col">Template failures</th></tr></thead><tbody>
                  <tr><th scope="row">baseline (originals)</th><td className="mono">{num(r.baseline.quality?.UnifiedQualityScore, 1)}</td><td className="mono">{num(r.baseline.quality?.Sharpness, 1)}</td><td className="mono">{num(r.baseline.mated_score_mean, 3)}</td><td className="mono">{pct(r.baseline.fnmr.rate)}</td><td className="mono">{pct(r.baseline.fmr.rate, 3)}</td><td>—</td></tr>
                  {r.levels.map((l: any) => <tr key={l.parameter}><th scope="row" className="mono">{l.parameter}</th><td className="mono">{num(l.quality.UnifiedQualityScore, 1)}</td><td className="mono">{num(l.quality.Sharpness, 1)}</td><td className="mono">{num(l.mated_score_mean, 3)}</td><td className="mono">{pct(l.fnmr.rate)} [{pct(l.fnmr.ci_lower)}, {pct(l.fnmr.ci_upper)}]</td><td className="mono">{pct(l.fmr.rate, 3)}</td><td className="mono">{l.template_failures}</td></tr>)}
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

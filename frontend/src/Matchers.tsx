import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, type AddonCatalogOut, type ComparisonRow, type DatasetOut, type MatcherAddonOut, type MatcherErrors, type MatcherLeaf, type MatrixStatus, type PreprocessingAddonOut } from "./api";
import { PairEvidence } from "./PairEvidence";
import { EvidenceOpenButton, type EvidenceRequest } from "./EvidenceDetail";
import { numericParameterError } from "./parameterContract";

const POLICY = "EXCLUDE_FAILED_TRIALS";
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
  active: boolean;
  projectId: string | null;
  datasetId: string | null;
  onMessage: (message: string) => void;
  onWorkflowTerminal?: () => void;
  onContextChange?: (context: MatcherHeaderContext | null) => void;
  onOpenEvidence: (request: EvidenceRequest) => void;
}

export interface MatcherHeaderContext {
  projectId: string;
  datasetId: string;
  protocolId: string | null;
  protocolLabel: string | null;
  leafId: string | null;
  leafLabel: string | null;
  runId: string | null;
}

interface DetPoint { fmr: number; fnmr: number; threshold: number }

/** Verification workbench: matrix execution, six-leaf metrics table, DET overlay, error drilldown (QS-C22..C24). */
export function Matchers({ active, projectId, datasetId, onMessage, onWorkflowTerminal, onContextChange, onOpenEvidence }: MatchersProps) {
  const contextKey = `${projectId ?? ""}\u001f${datasetId ?? ""}`;
  const [protocolId, setProtocolId] = useState<string | null>(null);
  const [protocolSummary, setProtocolSummary] = useState<string>("");
  const [leaves, setLeaves] = useState<MatcherLeaf[]>([]);
  const [matrix, setMatrix] = useState<MatrixStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [detCurves, setDetCurves] = useState<Record<string, DetPoint[]>>({});
  const [selectedLeaf, setSelectedLeaf] = useState<MatcherLeaf | null>(null);
  const [errors, setErrors] = useState<MatcherErrors | null>(null);
  const [errorsLoading, setErrorsLoading] = useState(false);
  const [errorsError, setErrorsError] = useState<string | null>(null);
  const [thresholdInput, setThresholdInput] = useState<string>("");
  const [dataset, setDataset] = useState<DatasetOut | null>(null);
  const [ownerPairSeed, setOwnerPairSeed] = useState("");
  const [matcherOptions, setMatcherOptions] = useState<MatcherAddonOut[]>([]);
  const [profileOptions, setProfileOptions] = useState<PreprocessingAddonOut[]>([]);
  const [selectedMatchers, setSelectedMatchers] = useState<string[]>([]);
  const [selectedProfiles, setSelectedProfiles] = useState<string[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [analysisContracts, setAnalysisContracts] = useState<AddonCatalogOut["analysis_parameters"] | null>(null);
  const [feedLoading, setFeedLoading] = useState(true);
  const [feedError, setFeedError] = useState<string | null>(null);
  const [resolvedContextKey, setResolvedContextKey] = useState("");
  const [datasetLoading, setDatasetLoading] = useState(false);
  const [datasetError, setDatasetError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [externalResources, setExternalResources] = useState<Array<{ resource_id: string; name: string; state: string }>>([]);
  const [externalResource, setExternalResource] = useState<string>("");
  const [externalResourceLoading, setExternalResourceLoading] = useState(true);
  const [externalResourceError, setExternalResourceError] = useState<string | null>(null);
  const [expectedLeaves, setExpectedLeaves] = useState(0);
  const selectionInitialized = useRef(false);
  const scopeEpoch = useRef(0);
  const refreshSequence = useRef(0);
  const datasetSequence = useRef(0);
  const errorsSequence = useRef(0);
  const catalogSequence = useRef(0);
  const externalResourceSequence = useRef(0);
  const matrixPollSequence = useRef(0);
  const activeMatrixId = useRef<string | null>(null);
  const terminalNotifiedMatrix = useRef<string | null>(null);

  const loadCatalog = useCallback(async () => {
    const sequence = ++catalogSequence.current;
    setCatalogLoading(true);
    try {
      const catalog = await api.addons();
      if (catalogSequence.current !== sequence) return;
      setMatcherOptions(catalog.matchers);
      setProfileOptions(catalog.preprocessing);
      setAnalysisContracts(catalog.analysis_parameters);
      setCatalogError(null);
      if (!selectionInitialized.current) {
        setSelectedMatchers(catalog.matchers.map((item) => item.matcher_id));
        setSelectedProfiles([]);
        setExpectedLeaves(0);
        selectionInitialized.current = true;
      }
    } catch (error) {
      if (catalogSequence.current === sequence) setCatalogError(error instanceof Error ? error.message : String(error));
    } finally {
      if (catalogSequence.current === sequence) setCatalogLoading(false);
    }
  }, []);

  useEffect(() => {
    if (active) void loadCatalog();
  }, [active, loadCatalog]);

  const refresh = useCallback(async () => {
    const epoch = scopeEpoch.current;
    const sequence = ++refreshSequence.current;
    if (!projectId) {
      setLeaves([]);
      setProtocolId(null);
      setProtocolSummary("");
      setResolvedContextKey("");
      setFeedLoading(false);
      setFeedError(null);
      return;
    }
    setFeedLoading(true);
    setFeedError(null);
    const results = await Promise.allSettled([api.protocols(projectId), api.matcherLeaves(projectId)]);
    if (scopeEpoch.current !== epoch || refreshSequence.current !== sequence) return;
    const [protocolResult, leafResult] = results;
    const failures: string[] = [];
    if (protocolResult.status === "fulfilled") {
      const matching = datasetId ? protocolResult.value.filter((item) => item.dataset_id === datasetId) : [];
      const selected = matching[matching.length - 1];
      setProtocolId(selected ? selected.protocol_id : null);
      setProtocolSummary(selected ? `${selected.name} · ${selected.pair_count.toLocaleString()} pairs (${selected.mated_count.toLocaleString()} mated)` : "");
    } else {
      failures.push(`Protocol feed unavailable: ${protocolResult.reason instanceof Error ? protocolResult.reason.message : String(protocolResult.reason)}`);
    }
    if (leafResult.status === "fulfilled") {
      setLeaves(datasetId ? leafResult.value.filter((item) => item.dataset_id === datasetId) : []);
    } else {
      failures.push(`Matcher Run feed unavailable: ${leafResult.reason instanceof Error ? leafResult.reason.message : String(leafResult.reason)}`);
    }
    setResolvedContextKey(contextKey);
    setFeedError(failures.length ? failures.join(" · ") : null);
    setFeedLoading(false);
  }, [projectId, datasetId, contextKey]);

  useLayoutEffect(() => {
    scopeEpoch.current += 1;
    refreshSequence.current += 1;
    datasetSequence.current += 1;
    errorsSequence.current += 1;
    matrixPollSequence.current += 1;
    activeMatrixId.current = null;
    terminalNotifiedMatrix.current = null;
    setProtocolId(null); setProtocolSummary("");
    setResolvedContextKey("");
    setLeaves([]); setMatrix(null); setDataset(null);
    setSelectedLeaf(null); setErrors(null); setErrorsLoading(false); setErrorsError(null); setThresholdInput("");
    setDetCurves({}); setOwnerPairSeed("");
    setFeedLoading(Boolean(projectId)); setFeedError(null);
    setDatasetLoading(Boolean(datasetId)); setDatasetError(null); setPollError(null);
    setBusy(false); setExpectedLeaves(0);
  }, [projectId, datasetId]);

  useEffect(() => {
    if (active) void refresh();
  }, [active, refresh]);

  const loadDataset = useCallback(async () => {
    const epoch = scopeEpoch.current;
    const sequence = ++datasetSequence.current;
    if (!projectId || !datasetId) {
      setDataset(null);
      setDatasetLoading(false);
      setDatasetError(null);
      return;
    }
    setDatasetLoading(true);
    setDatasetError(null);
    try {
      const nextDataset = await api.dataset(projectId, datasetId);
      if (scopeEpoch.current !== epoch || datasetSequence.current !== sequence || nextDataset.dataset_id !== datasetId) return;
      setDataset(nextDataset);
    } catch (error) {
      if (scopeEpoch.current !== epoch || datasetSequence.current !== sequence) return;
      setDatasetError(`Active dataset metadata is unavailable: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      if (scopeEpoch.current === epoch && datasetSequence.current === sequence) setDatasetLoading(false);
    }
  }, [datasetId, projectId]);

  useEffect(() => {
    if (active) void loadDataset();
  }, [active, loadDataset]);

  const managedPairDataset = dataset?.manifest["source_kind"] === "MANAGED_PREALIGNED_PAIR_DATASET";
  const desiredRepresentation = managedPairDataset
    ? "AUTHOR_PREALIGNED_ARCFACE112"
    : "SOURCE_IMAGE_REQUIRING_DETECTION";
  const compatibleProfileOptions = useMemo(
    () => profileOptions.filter(
      (profile) => profile.input_representation === desiredRepresentation && profile.runtime_state === "READY",
    ),
    [profileOptions, desiredRepresentation],
  );

  useEffect(() => {
    const compatibleIds = compatibleProfileOptions.map((profile) => profile.addon_id);
    setSelectedProfiles((current) => {
      const retained = current.filter((id) => compatibleIds.includes(id));
      return retained.length > 0 ? retained : compatibleIds;
    });
  }, [datasetId, compatibleProfileOptions]);

  useEffect(() => {
    if (!matrix || !projectId || ["completed", "failed"].includes(matrix.progress.phase)) return;
    const epoch = scopeEpoch.current;
    const matrixId = matrix.matrix_id;
    const handle = window.setInterval(() => {
      const sequence = ++matrixPollSequence.current;
      api.matrix(projectId, matrixId).then((status) => {
        if (scopeEpoch.current !== epoch || matrixPollSequence.current !== sequence || activeMatrixId.current !== matrixId) return;
        setPollError(null);
        setMatrix(status);
        if (["completed", "failed"].includes(status.progress.phase)) {
          if (terminalNotifiedMatrix.current !== matrixId) {
            terminalNotifiedMatrix.current = matrixId;
            onMessage(status.progress.phase === "completed"
              ? `Matcher matrix ${shortHash(matrixId)} completed with ${status.leaves.length} recorded Run leaves`
              : `Matcher matrix ${shortHash(matrixId)} failed: ${status.progress.detail || "no failure detail was recorded"}`);
            onWorkflowTerminal?.();
          }
          void refresh();
        }
      }).catch((error) => {
        if (scopeEpoch.current === epoch && matrixPollSequence.current === sequence && activeMatrixId.current === matrixId) {
          setPollError(`Matrix status is temporarily unavailable; completion is not assumed. ${error instanceof Error ? error.message : String(error)}`);
        }
      });
    }, 2000);
    return () => window.clearInterval(handle);
  }, [matrix, projectId, refresh, onMessage, onWorkflowTerminal]);

  // Load DET curves for the currently listed leaves (latest matrix only).
  const executedLeaves = useMemo(() => leaves.filter((leaf) => leaf.condition_id !== "EXTERNALLY_COMPUTED"), [leaves]);
  const externalLeaves = useMemo(() => leaves.filter((leaf) => leaf.condition_id === "EXTERNALLY_COMPUTED"), [leaves]);
  const latestMatrixId = useMemo(() => (executedLeaves.length ? executedLeaves[executedLeaves.length - 1]!.matrix_id : null), [executedLeaves]);
  const shownLeaves = useMemo(() => executedLeaves.filter((leaf) => leaf.matrix_id === latestMatrixId), [executedLeaves, latestMatrixId]);
  const loadExternalResources = useCallback(async () => {
    const sequence = ++externalResourceSequence.current;
    setExternalResourceLoading(true);
    setExternalResourceError(null);
    try {
      const items = await api.resources();
      if (externalResourceSequence.current !== sequence) return;
      const external = items.filter((item) => item.kind === "EXTERNAL_SCORE_ARTIFACT");
      setExternalResources(external);
      setExternalResource((current) => external.some((item) => item.resource_id === current) ? current : (external[0]?.resource_id ?? ""));
      setExternalResourceError(null);
    } catch (error) {
      if (externalResourceSequence.current !== sequence) return;
      setExternalResourceError(`External-score resource feed unavailable: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      if (externalResourceSequence.current === sequence) setExternalResourceLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!active) return;
    void loadExternalResources();
    return () => {
      externalResourceSequence.current += 1;
    };
  }, [active, loadExternalResources]);
  const importExternal = useCallback(async () => {
    if (!projectId || !protocolId || !externalResource) return;
    const epoch = scopeEpoch.current;
    setBusy(true);
    try {
      const leaf = await api.importExternalScores(projectId, protocolId, externalResource);
      if (scopeEpoch.current !== epoch) return;
      onMessage(`Imported external scores as ${leaf.leaf_id} (${leaf.provenance_marker}; ${leaf.scored_count} scored, ${leaf.failed_count} without score) · run ${shortHash(leaf.run_id)}`);
      void refresh();
    } catch (error) {
      if (scopeEpoch.current === epoch) onMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (scopeEpoch.current === epoch) setBusy(false);
    }
  }, [projectId, protocolId, externalResource, onMessage, refresh]);
  useEffect(() => {
    let cancelled = false;
    Promise.all(
      shownLeaves.map((leaf) => api.matcherMetrics(projectId!, leaf.run_id).then((body) => [leaf.leaf_id, body.metrics.by_policy[POLICY].det.points as DetPoint[]] as const)),
    )
      .then((entries) => {
        if (!cancelled) {
          setDetCurves(Object.fromEntries(entries));
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setDetCurves({});
          setFeedError(`DET metrics are unavailable; the curve is not treated as empty. ${error instanceof Error ? error.message : String(error)}`);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [shownLeaves, projectId]);

  const freeze = useCallback(async () => {
    if (!projectId || !datasetId || !dataset) return;
    const epoch = scopeEpoch.current;
    setBusy(true);
    try {
      const ownerSupplied = dataset.manifest["source_kind"] === "OWNER_SUPPLIED_LOCAL_INPUT";
      const seedError = numericParameterError(ownerPairSeed, analysisContracts?.owner_pair_seed ?? null, "Pair selection seed");
      if (ownerSupplied && seedError) {
        onMessage(seedError);
        return;
      }
      const seed = Number(ownerPairSeed);
      const frozen = ownerSupplied
        ? await api.freezeOwnerPairs(projectId, datasetId, seed!)
        : await api.freezeProtocol(projectId, datasetId);
      if (scopeEpoch.current !== epoch) return;
      onMessage(`Frozen protocol ${shortHash(frozen.protocol_id)} with ${frozen.pair_count} pairs`);
      void refresh();
    } catch (error) {
      if (scopeEpoch.current === epoch) onMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (scopeEpoch.current === epoch) setBusy(false);
    }
  }, [projectId, datasetId, dataset, ownerPairSeed, analysisContracts, onMessage, refresh]);

  const startMatrix = useCallback(
    async (fixtureOnly: boolean) => {
      if (!projectId || !protocolId) return;
      const epoch = scopeEpoch.current;
      setBusy(true);
      try {
        const leafIds = compatibleProfileOptions
          .filter((profile) => selectedProfiles.includes(profile.addon_id))
          .flatMap((profile) => selectedMatchers.map((matcherId) => profile.leaf_ids_by_matcher[matcherId]).filter((leafId): leafId is string => Boolean(leafId)));
        if (leafIds.length === 0) throw new Error("Select at least one admitted matcher and one preprocessing profile.");
        const started = await api.startMatrix(projectId, protocolId, fixtureOnly, leafIds);
        if (scopeEpoch.current !== epoch) return;
        onMessage(`Matrix ${shortHash(started.matrix_id)} started: ${started.pairs} pairs · ${started.samples} samples`);
        setExpectedLeaves(leafIds.length);
        activeMatrixId.current = started.matrix_id;
        terminalNotifiedMatrix.current = null;
        matrixPollSequence.current += 1;
        setMatrix({ matrix_id: started.matrix_id, progress: { phase: "queued", detail: "" }, leaves: [] });
      } catch (error) {
        if (scopeEpoch.current === epoch) onMessage(error instanceof Error ? error.message : String(error));
      } finally {
        if (scopeEpoch.current === epoch) setBusy(false);
      }
    },
    [projectId, protocolId, onMessage, compatibleProfileOptions, selectedMatchers, selectedProfiles],
  );

  const showErrors = useCallback(
    async (leaf: MatcherLeaf, threshold?: number) => {
      const epoch = scopeEpoch.current;
      const sequence = ++errorsSequence.current;
      setSelectedLeaf(leaf);
      setErrors(null);
      setErrorsError(null);
      setErrorsLoading(true);
      setThresholdInput("");
      try {
        if (!projectId) throw new Error("no active project");
        const nextErrors = await api.matcherErrors(projectId, leaf.run_id, threshold);
        if (scopeEpoch.current !== epoch || errorsSequence.current !== sequence) return;
        if (nextErrors.run_id !== leaf.run_id) {
          setErrorsError(`Matcher-error response attribution mismatch: requested ${shortHash(leaf.run_id)} but received ${shortHash(nextErrors.run_id)}.`);
          return;
        }
        setErrors(nextErrors);
        setThresholdInput(String(nextErrors.threshold));
      } catch (error) {
        if (scopeEpoch.current === epoch && errorsSequence.current === sequence) {
          const detail = error instanceof Error ? error.message : String(error);
          setErrorsError(`Matcher errors for ${leaf.leaf_id} are unavailable. ${detail}`);
          onMessage(detail);
        }
      } finally {
        if (scopeEpoch.current === epoch && errorsSequence.current === sequence) setErrorsLoading(false);
      }
    },
    [projectId, onMessage],
  );

  const grouped = useMemo(() => {
    const byMatcher: Record<string, Record<string, MatcherLeaf>> = {};
    for (const leaf of shownLeaves) {
      byMatcher[leaf.matcher_id] = byMatcher[leaf.matcher_id] ?? {};
      byMatcher[leaf.matcher_id]![leaf.condition_id] = leaf;
    }
    return byMatcher;
  }, [shownLeaves]);
  const conditions = compatibleProfileOptions.map((profile) => profile.addon_id);
  const matcherLabel = (matcherId: string) => matcherOptions.find((item) => item.matcher_id === matcherId)?.name ?? matcherId;
  const conditionLabel = (conditionId: string) => profileOptions.find((item) => item.addon_id === conditionId)?.display_name ?? conditionId;
  useLayoutEffect(() => {
    if (!projectId || !datasetId || resolvedContextKey !== contextKey) {
      onContextChange?.(null);
      return;
    }
    const matcher = selectedLeaf ? matcherOptions.find((item) => item.matcher_id === selectedLeaf.matcher_id) : null;
    const profile = selectedLeaf ? profileOptions.find((item) => item.addon_id === selectedLeaf.condition_id) : null;
    onContextChange?.({
      projectId,
      datasetId,
      protocolId,
      protocolLabel: protocolId ? (protocolSummary || "Protocol details unavailable") : null,
      leafId: selectedLeaf?.leaf_id ?? null,
      leafLabel: selectedLeaf
        ? (matcher && profile ? `${matcher.name} · ${profile.display_name}` : "Matcher leaf details unavailable")
        : null,
      runId: selectedLeaf?.run_id ?? null,
    });
  }, [contextKey, datasetId, matcherOptions, onContextChange, profileOptions, projectId, protocolId, protocolSummary, resolvedContextKey, selectedLeaf]);

  useLayoutEffect(() => () => onContextChange?.(null), [onContextChange]);
  const colors = ["#38bdf8", "#0ea5e9", "#34d399", "#10b981", "#f59e0b", "#d97706"];
  const ownerSupplied = dataset?.manifest["source_kind"] === "OWNER_SUPPLIED_LOCAL_INPUT";
  const ownerPairSeedError = ownerSupplied ? numericParameterError(ownerPairSeed, analysisContracts?.owner_pair_seed ?? null, "Pair selection seed") : null;
  const matcherThresholdContract = analysisContracts?.matcher_threshold ?? null;
  const matcherThresholdError = selectedLeaf ? numericParameterError(thresholdInput, matcherThresholdContract, "Matcher threshold") : null;
  const matcherThresholdDisabledReason = busy
    ? "An operation is already in progress."
    : matcherThresholdError;
  const matrixRunning = matrix !== null && !["completed", "failed"].includes(matrix.progress.phase);
  const addonSelectionDisabledReason = busy
    ? "An operation is already in progress."
    : catalogLoading
      ? "The admitted add-on catalog is loading."
      : catalogError
        ? "Retry the admitted add-on catalog before selecting run components."
        : matcherOptions.length === 0
          ? "No matcher add-ons are present in the admitted catalog."
          : compatibleProfileOptions.length === 0
            ? "No ready preprocessing add-on is compatible with this dataset's recorded image representation."
            : null;
  const freezeDisabledReason = busy
    ? "An operation is already in progress."
    : !datasetId
      ? "Select an active dataset before freezing a protocol."
      : !dataset
        ? "The active dataset metadata must be available before freezing a protocol."
        : managedPairDataset
          ? "Managed pair datasets are imported with their author-published protocol; re-import the dataset from System if its protocol is unavailable."
          : ownerPairSeedError;
  const matrixDisabledReason = busy
    ? "An operation is already in progress."
    : matrixRunning
      ? "Wait for the running matcher matrix to complete or fail."
      : !protocolId
        ? "Freeze a comparison protocol before running a matcher matrix."
        : catalogLoading
          ? "Wait for the admitted add-on catalog to load."
          : catalogError
            ? "Retry the admitted add-on catalog before running a matrix."
            : selectedMatchers.length === 0
              ? "Select at least one admitted matcher."
              : selectedProfiles.length === 0
                ? "Select at least one admitted preprocessing profile."
                : null;
  const externalImportDisabledReason = busy
    ? "An operation is already in progress."
    : !protocolId
      ? "Freeze a comparison protocol before importing external scores."
      : externalResourceLoading && !externalResource
        ? "Wait for the registered external-score resource feed to load."
      : externalResourceError && !externalResource
        ? "Retry the external-score resource feed before import."
        : !externalResource
          ? "No registered external-score artifact is selected."
          : null;

  if (!projectId) {
    return (
      <section className="panel matchers" aria-labelledby="matchers-heading" data-testid="matchers">
        <h2 id="matchers-heading">Verification · matcher × preprocessing matrix</h2>
        <div className="empty-state">
          <p>Select a project with an imported identity dataset.</p>
          <a className="button primary" href="#/projects">Open projects</a>
        </div>
      </section>
    );
  }

  return (
    <section className="panel matchers" aria-labelledby="matchers-heading" data-testid="matchers">
      <h2 id="matchers-heading">Verification · matcher × preprocessing matrix</h2>
      {!datasetId && <div className="empty-state compact"><p>This project has no active dataset. Select or import one before using a protocol.</p><a className="button secondary" href="#/data">Open datasets</a></div>}
      {feedLoading && <p className="status" role="status">{leaves.length || protocolId ? "Refreshing protocols and matcher Runs…" : "Loading protocols and matcher Runs…"}</p>}
      {catalogError && (
        <div>
          <p id="matcher-catalog-error" className="error" role="alert">Add-on catalog unavailable: {catalogError}</p>
          <button type="button" className="secondary" disabled={catalogLoading} aria-describedby="matcher-catalog-error" title={catalogLoading ? "The admitted add-on catalog retry is in progress." : "Retry the real admitted add-on catalog request."} onClick={() => void loadCatalog()} data-testid="matcher-catalog-retry">
            {catalogLoading ? "Retrying catalog…" : "Retry add-on catalog"}
          </button>
        </div>
      )}
      {feedError && <p id="matcher-feed-error" className="error" role="alert">{feedError}. {leaves.length || protocolId ? "Previously verified evidence remains visible." : "Protocol and matcher Run actions remain unavailable."} <button type="button" className="secondary" disabled={feedLoading} aria-describedby="matcher-feed-error" onClick={() => void refresh()} data-testid="matcher-feed-retry">{feedLoading ? "Retrying evidence…" : "Retry protocol and Run feeds"}</button></p>}
      {datasetLoading && <p className="status" role="status">{dataset ? "Refreshing active dataset metadata…" : "Loading active dataset metadata…"}</p>}
      {datasetError && <p id="matcher-dataset-error" className="error" role="alert">{datasetError}. {dataset ? "Previously verified metadata remains available." : "Protocol creation remains unavailable."} <button type="button" className="secondary" disabled={datasetLoading} aria-describedby="matcher-dataset-error" onClick={() => void loadDataset()} data-testid="matcher-dataset-retry">{datasetLoading ? "Retrying metadata…" : "Retry dataset metadata"}</button></p>}
      {pollError && <p className="error" role="alert">{pollError}</p>}
      {externalResourceLoading && <p className="status" role="status">{externalResources.length ? "Refreshing registered external-score artifacts…" : "Loading registered external-score artifacts…"}</p>}
      {externalResourceError && <p id="external-resource-error" className="error" role="alert">{externalResourceError}. {externalResources.length ? "Previously verified registered artifacts remain available." : "External score import remains unavailable."} <button type="button" className="secondary" disabled={externalResourceLoading} aria-describedby="external-resource-error" onClick={() => void loadExternalResources()} data-testid="external-resource-retry">{externalResourceLoading ? "Retrying resources…" : "Retry external-score resources"}</button></p>}
      <fieldset className="addon-selection" disabled={addonSelectionDisabledReason !== null} aria-describedby="addon-selection-help" title={addonSelectionDisabledReason ?? "Select the admitted matcher and preprocessing add-ons for this matrix."} data-testid="addon-selection">
        <legend>Run add-ons</legend>
        <div>
          <strong>Matchers</strong>
          {matcherOptions.map((item) => (
            <label className="addon-option" key={item.matcher_id}><input type="checkbox" checked={selectedMatchers.includes(item.matcher_id)} onChange={(event) => setSelectedMatchers((current) => event.target.checked ? [...current, item.matcher_id] : current.filter((id) => id !== item.matcher_id))} />{item.name}</label>
          ))}
        </div>
        <div>
          <strong>Preprocessing</strong>
          {compatibleProfileOptions.map((item) => (
            <label className="addon-option" key={item.addon_id}><input type="checkbox" checked={selectedProfiles.includes(item.addon_id)} onChange={(event) => setSelectedProfiles((current) => event.target.checked ? [...current, item.addon_id] : current.filter((id) => id !== item.addon_id))} />{item.display_name}</label>
          ))}
        </div>
        <p id="addon-selection-help" className={addonSelectionDisabledReason ? "error" : "hint"}>{addonSelectionDisabledReason ?? `The selected Cartesian product is recorded as ${selectedMatchers.length * selectedProfiles.length} immutable Run leaf/leaves. Selection does not change global matcher or preprocessing state.`}</p>
      </fieldset>
      <div className="controls-row">
        {protocolId ? (
          <span className="hint" data-testid="protocol-summary">{protocolSummary}</span>
        ) : (
          <>
            {ownerSupplied && (
              <label className="field-stack">
                Pair selection seed (integer)
                <input type="number" min="0" step="1" required value={ownerPairSeed} onChange={(event) => setOwnerPairSeed(event.target.value)} aria-invalid={ownerPairSeedError ? "true" : undefined} aria-describedby={`owner-pair-seed-help${ownerPairSeedError ? " owner-pair-seed-error" : ""}`} data-testid="owner-pair-seed" />
                <span id="owner-pair-seed-help" className="hint">Required finite, non-negative whole number; the value records deterministic pair selection.</span>
                {ownerPairSeedError && <span id="owner-pair-seed-error" className="error" role="alert">{ownerPairSeedError}</span>}
              </label>
            )}
            {!managedPairDataset && <button type="button" className="secondary" disabled={freezeDisabledReason !== null} aria-describedby="freeze-protocol-help" title={freezeDisabledReason ?? "Freeze an immutable comparison protocol over the active dataset."} onClick={() => void freeze()} data-testid="freeze-protocol">
              {ownerSupplied ? "Freeze seeded owner pair protocol" : "Freeze LFW View 2 protocol"}
            </button>}
            <span id="freeze-protocol-help" className={freezeDisabledReason ? "error" : "hint"}>{freezeDisabledReason ?? "Ready to freeze a reproducible comparison protocol."}</span>
          </>
        )}
        {!ownerSupplied && !managedPairDataset && <button type="button" className="secondary" disabled={matrixDisabledReason !== null} aria-describedby="matrix-run-help" title={matrixDisabledReason ?? "Run the selected admitted add-ons over the registered real-data fixture pairs."} onClick={() => void startMatrix(true)} data-testid="matrix-fixture">Run selected add-ons (real fixture pairs)</button>}
        <button type="button" className="primary" disabled={matrixDisabledReason !== null} aria-describedby="matrix-run-help" title={matrixDisabledReason ?? "Run the selected admitted add-ons over every pair in the frozen protocol."} onClick={() => void startMatrix(false)} data-testid="matrix-full">
          {ownerSupplied ? "Run matrix (all frozen owner pairs)" : "Run matrix (all protocol pairs)"}
        </button>
        <span id="matrix-run-help" className={matrixDisabledReason ? "error" : "hint"}>{matrixDisabledReason ?? `Ready to record ${selectedMatchers.length * selectedProfiles.length} immutable matcher Run leaf/leaves.`}</span>
      </div>
      {matrix && (
        <p className="mono" data-testid="matrix-progress">
          matrix {shortHash(matrix.matrix_id)} · {matrix.progress.phase} {matrix.progress.detail} · {matrix.leaves.length}/{expectedLeaves || selectedMatchers.length * selectedProfiles.length} leaves
        </p>
      )}
      {shownLeaves.length > 0 && (
        <div className="matrix-grid">
          <table className="matrix-table" data-testid="matrix-table">
            <caption>Matcher results by preprocessing condition</caption>
            <thead>
              <tr>
                <th scope="col">Matcher</th>
                {conditions.map((condition) => (
                  <th key={condition} scope="col">{conditionLabel(condition)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {Object.entries(grouped).map(([matcherId, byCondition]) => (
                <tr key={matcherId}>
                  <th scope="row">{matcherLabel(matcherId)}</th>
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
                          <span>{leaf.view2_tenfold[POLICY]?.status === "NOT_ASSESSED" ? "Fold accuracy not assessed" : `10-fold acc ${pct(leaf.view2_tenfold[POLICY]?.mean_accuracy)}`}</span>
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
                  <span className="swatch" style={{ background: colors[index % colors.length] }} /> {leaf.leaf_id} · {matcherLabel(leaf.matcher_id)} · {conditionLabel(leaf.condition_id)}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
      {selectedLeaf && errorsLoading && <p className="status" role="status">Loading matcher errors for {selectedLeaf.leaf_id}…</p>}
      {selectedLeaf && errorsError && (
        <p className="error" role="alert">
          {errorsError}{" "}
          <button type="button" className="secondary" onClick={() => void showErrors(selectedLeaf)}>Retry matcher errors</button>
        </p>
      )}
      {selectedLeaf && errors && errors.run_id === selectedLeaf.run_id && (
        <div className="errors" data-testid="matcher-errors">
          <h3>
            {selectedLeaf.leaf_id} errors at threshold {errors.threshold.toFixed(4)} ({errors.policy}; {errors.threshold_equality_rule}) · condition {errors.condition_id} · transform {errors.transform_id} · run{" "}
            {projectId ? <EvidenceOpenButton testId="matcher-error-run-evidence" request={{ kind: "run", href: `/api/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(errors.run_id)}`, evidenceId: errors.run_id, projectId, label: `Matcher-error Run ${shortHash(errors.run_id, 14)}` }} onOpen={onOpenEvidence} className="mono">{shortHash(errors.run_id, 14)}</EvidenceOpenButton> : shortHash(errors.run_id, 14)}
          </h3>
          <div className="controls-row">
            <label className="field-stack">
              Matcher threshold
              <input
                type="number"
                step={matcherThresholdContract?.value_type === "integer" ? 1 : "any"}
                min={matcherThresholdContract?.minimum}
                max={matcherThresholdContract?.maximum}
                required={matcherThresholdContract?.required}
                value={thresholdInput}
                onChange={(event) => setThresholdInput(event.target.value)}
                aria-invalid={matcherThresholdError ? "true" : undefined}
                aria-describedby={`matcher-threshold-help${matcherThresholdError ? " matcher-threshold-error" : ""}`}
                data-testid="matcher-threshold"
              />
              <span id="matcher-threshold-help" className="hint">
                {matcherThresholdContract ? `Admitted contract: ${matcherThresholdContract.guidance}.` : "The admitted matcher-threshold contract is unavailable."}
              </span>
              {matcherThresholdError && <span id="matcher-threshold-error" className="error" role="alert">{matcherThresholdError}</span>}
            </label>
            <button
              type="button"
              className="secondary"
              disabled={matcherThresholdDisabledReason !== null}
              aria-describedby="matcher-threshold-action-help"
              title={matcherThresholdDisabledReason ?? "Recompute matcher errors at the validated threshold."}
              onClick={() => void showErrors(selectedLeaf, Number(thresholdInput))}
              data-testid="matcher-threshold-recompute"
            >
              Recompute
            </button>
            <span id="matcher-threshold-action-help" className={matcherThresholdDisabledReason ? "error" : "hint"}>{matcherThresholdDisabledReason ?? "Ready to recompute errors with the admitted matcher-threshold contract."}</span>
            <span className="hint">
              {errors.counts.false_matches} false matches · {errors.counts.false_non_matches} false non-matches · {errors.counts.processing_failures} processing failures
            </span>
          </div>
          <ErrorList title="False matches (non-mated accepted)" rows={errors.false_matches} runId={selectedLeaf?.run_id ?? ""} projectId={projectId} onOpenEvidence={onOpenEvidence} />
          <ErrorList title="False non-matches (mated rejected)" rows={errors.false_non_matches} runId={selectedLeaf?.run_id ?? ""} projectId={projectId} onOpenEvidence={onOpenEvidence} />
          <ErrorList title="Processing failures (no score)" rows={errors.processing_failures} runId={selectedLeaf?.run_id ?? ""} projectId={projectId} onOpenEvidence={onOpenEvidence} />
        </div>
      )}
      <div className="errors" data-testid="external-scores">
        <h3>Externally computed scores (QS-C21) — imported, never re-scored; marker EXTERNALLY_COMPUTED travels with every result</h3>
        <div className="controls-row">
          <select value={externalResource} onChange={(event) => setExternalResource(event.target.value)} data-testid="external-resource" aria-label="external score artifact">
            {externalResources.length === 0 && <option value="">no registered external score artifact</option>}
            {externalResources.map((item) => (
              <option key={item.resource_id} value={item.resource_id}>{item.name} · {item.state}</option>
            ))}
          </select>
          <button type="button" className="secondary" disabled={externalImportDisabledReason !== null} aria-describedby="external-import-help" title={externalImportDisabledReason ?? "Import the selected registered external scores over the frozen protocol without re-scoring."} onClick={() => void importExternal()} data-testid="external-import">Import over the frozen protocol</button>
          <span id="external-import-help" className={externalImportDisabledReason ? "error" : "hint"}>{externalImportDisabledReason ?? "Ready to import the selected registered score artifact with external provenance."}</span>
        </div>
        {externalLeaves.length > 0 && (
          <table className="compact" data-testid="external-table">
            <caption>Imported externally computed matcher scores</caption>
            <thead><tr><th scope="col">Leaf</th><th scope="col">Artifact</th><th scope="col">Marker</th><th scope="col">Comparisons</th><th scope="col">Scored</th><th scope="col">No score</th><th scope="col">EER</th><th scope="col">FNMR @ FMR ≤ 10⁻³</th><th scope="col">Run</th></tr></thead>
            <tbody>
              {externalLeaves.map((leaf) => (
                <tr key={leaf.run_id} data-testid="external-leaf">
                  <th scope="row" className="mono">{leaf.leaf_id}</th><td className="mono">{leaf.matcher_id}</td><td>{leaf.provenance_marker ?? "EXTERNALLY_COMPUTED"}</td>
                  <td className="mono">{leaf.comparison_count}</td><td className="mono">{leaf.scored_count}</td><td className="mono">{leaf.failed_count}</td>
                  <td className="mono">{pct(leaf.eer[POLICY]?.eer)}</td>
                  <td className="mono">{pct(leaf.operating_points[POLICY]?.find((p) => p.target_fmr === 0.001)?.fnmr)}</td>
                  <td><button type="button" className="cell" onClick={() => void showErrors(leaf)} data-testid={`leaf-${leaf.leaf_id}`}><span className="mono">{shortHash(leaf.run_id, 10)}</span></button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}

function ErrorList({ title, rows, runId, projectId, onOpenEvidence }: { title: string; rows: ComparisonRow[]; runId: string; projectId: string | null; onOpenEvidence: (request: EvidenceRequest) => void }) {
  const [evidencePair, setEvidencePair] = useState<string | null>(null);
  if (rows.length === 0) return <p className="hint">{title}: none</p>;
  return (
    <div className="error-list">
      <h4>{title} ({rows.length})</h4>
      <ul>
        {rows.slice(0, 24).map((row) => (
          <li key={row.pair_id} className="pair" data-testid="error-pair">
            {row.links.left_image && <img src={row.links.left_image} alt={`Left sample ${row.left_sample_id}; ${row.left_subject_id ? `subject ${row.left_subject_id}` : "subject identity unavailable"}`} />}
            {row.links.right_image && <img src={row.links.right_image} alt={`Right sample ${row.right_sample_id}; ${row.right_subject_id ? `subject ${row.right_subject_id}` : "subject identity unavailable"}`} />}
            <div className="pair-facts">
              <span className="mono">{row.pair_id} · fold {row.fold ?? "not recorded"} · {row.label}</span>
              <span>{row.left_subject_id ?? "subject identity unavailable"} vs {row.right_subject_id ?? "subject identity unavailable"}</span>
              <span className="mono">{row.score === null ? `${row.failure} (${row.failure_side})` : `score ${row.score.toFixed(4)}`}</span>
              <span className="links">
                {projectId ? <EvidenceOpenButton testId="matcher-left-sample-evidence" request={{ kind: "sample", href: row.links.left_sample, evidenceId: row.left_sample_id, projectId, label: `Left sample ${shortHash(row.left_sample_id, 14)}` }} onOpen={onOpenEvidence}>{fileName(row.links.left_image ?? row.left_sample_id)}</EvidenceOpenButton> : fileName(row.links.left_image ?? row.left_sample_id)}
                {projectId ? <EvidenceOpenButton testId="matcher-right-sample-evidence" request={{ kind: "sample", href: row.links.right_sample, evidenceId: row.right_sample_id, projectId, label: `Right sample ${shortHash(row.right_sample_id, 14)}` }} onOpen={onOpenEvidence}>{fileName(row.links.right_image ?? row.right_sample_id)}</EvidenceOpenButton> : fileName(row.links.right_image ?? row.right_sample_id)}
                <button type="button" className="secondary" data-testid="evidence-open" onClick={() => setEvidencePair(evidencePair === row.pair_id ? null : row.pair_id)}>
                  {evidencePair === row.pair_id ? "hide evidence" : "match evidence"}
                </button>
              </span>
              {evidencePair === row.pair_id && projectId && (
                <PairEvidence projectId={projectId} runId={runId} pairId={row.pair_id} onClose={() => setEvidencePair(null)} />
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, type AddonCatalogOut, type JobOut, type MatcherLeaf, type PairQualityPolicy, type UtilityAnalysisOut, type UtilityOverlayIdentity, type UtilityPairRow, type UtilitySummary } from "./api";
import { PairEvidence } from "./PairEvidence";
import { numericParameterError } from "./parameterContract";
import { groupRecordedQualityMeasureNames, qualityMeasureLabel } from "./qualityProfile";
import { EvidenceOpenButton, type EvidenceRequest } from "./EvidenceDetail";

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

function finiteNumber(value: string): number | null {
  if (value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

interface UtilityProps {
  projectId: string | null;
  onMessage: (message: string) => void;
  onWorkflowTerminal?: () => void;
  onContextChange?: (context: UtilityHeaderContext | null) => void;
  onOpenEvidence: (request: EvidenceRequest) => void;
}

export interface UtilityHeaderContext {
  projectId: string;
  datasetId: string | null;
  protocolId: string | null;
  protocolLabel: string | null;
  leafId: string | null;
  leafLabel: string | null;
  runId: string;
}

interface CurvePoint { rejected_fraction: number; retained: number; error_rate: number | null; quality_cutoff: number | null }

type OverlayAxis = "MATCHER" | "PREPROCESSING" | "COMPOSITE";
type OverlayClassification = {
  eligible: boolean;
  comparisonAxis: "MATCHER" | "PREPROCESSING" | "COMPOSITE" | "IDENTICAL_CONFIGURATION" | "UNKNOWN";
  reasons: string[];
};

const OVERLAY_REASON_LABELS: Record<string, string> = {
  INCOMPLETE_OVERLAY_IDENTITY: "This older analysis does not record the complete overlay identity.",
  PROJECT_MISMATCH: "Different project.",
  DATASET_MISMATCH: "Different dataset.",
  PROTOCOL_MISMATCH: "Different protocol.",
  PAIR_UNIVERSE_MISMATCH: "Different recorded pair universe.",
  QUALITY_PARQUET_MISMATCH: "Different quality-result artifact.",
  QUALITY_MEASURE_MISMATCH: "Different quality measure.",
  PAIR_QUALITY_POLICY_MISMATCH: "Different pair-quality policy or policy definition.",
  METRIC_REGISTRY_MISMATCH: "Different metric-registry definition.",
  THRESHOLD_SCORE_SEMANTICS_MISMATCH: "Different threshold-selection or score semantics.",
  MATCHER_AXIS_NOT_SELECTED: "Matcher differs, but matcher is not a selected comparison axis.",
  PREPROCESSING_AXIS_NOT_SELECTED: "Preprocessing differs, but preprocessing is not a selected comparison axis.",
  COMPARISONS_ARTIFACT_MISMATCH: "Comparison artifacts differ without a recorded matcher or preprocessing change.",
};

function sameDefinition(
  left: { id: string; version: string; definition_sha256: string },
  right: { id: string; version: string; definition_sha256: string },
): boolean {
  return left.id === right.id && left.version === right.version && left.definition_sha256 === right.definition_sha256;
}

function sameThresholdSemantics(
  left: UtilityOverlayIdentity["threshold_score_semantics"],
  right: UtilityOverlayIdentity["threshold_score_semantics"],
): boolean {
  return left.threshold_kind === right.threshold_kind
    && left.threshold_source === right.threshold_source
    && left.threshold_selection === right.threshold_selection
    && left.target_fmr === right.target_fmr
    && left.failure_policy === right.failure_policy
    && left.evaluation_scope === right.evaluation_scope
    && left.score_direction === right.score_direction
    && left.equality_rule === right.equality_rule;
}

function classifyOverlay(
  baseline: UtilitySummary,
  candidate: UtilitySummary,
  axis: OverlayAxis,
): OverlayClassification {
  const left = baseline.overlay_identity;
  const right = candidate.overlay_identity;
  if (!left || !right || left.schema_version !== "1.0.0" || right.schema_version !== "1.0.0") {
    return { eligible: false, comparisonAxis: "UNKNOWN", reasons: ["INCOMPLETE_OVERLAY_IDENTITY"] };
  }
  const matcherChanged = left.matcher_id !== right.matcher_id;
  const preprocessingChanged = left.preprocessing_id !== right.preprocessing_id;
  const comparisonAxis = matcherChanged && preprocessingChanged
    ? "COMPOSITE"
    : matcherChanged
      ? "MATCHER"
      : preprocessingChanged
        ? "PREPROCESSING"
        : "IDENTICAL_CONFIGURATION";
  const reasons: string[] = [];
  if (left.project_id !== right.project_id) reasons.push("PROJECT_MISMATCH");
  if (left.dataset_id !== right.dataset_id) reasons.push("DATASET_MISMATCH");
  if (left.protocol_id !== right.protocol_id) reasons.push("PROTOCOL_MISMATCH");
  if (left.pair_universe_sha256 !== right.pair_universe_sha256) reasons.push("PAIR_UNIVERSE_MISMATCH");
  if (left.quality_parquet_sha256 !== right.quality_parquet_sha256) reasons.push("QUALITY_PARQUET_MISMATCH");
  if (left.quality_measure !== right.quality_measure) reasons.push("QUALITY_MEASURE_MISMATCH");
  if (!sameDefinition(left.pair_quality_policy, right.pair_quality_policy)) reasons.push("PAIR_QUALITY_POLICY_MISMATCH");
  if (!sameDefinition(left.metric_registry, right.metric_registry)) reasons.push("METRIC_REGISTRY_MISMATCH");
  if (!sameThresholdSemantics(left.threshold_score_semantics, right.threshold_score_semantics)) reasons.push("THRESHOLD_SCORE_SEMANTICS_MISMATCH");
  if (matcherChanged && axis === "PREPROCESSING") reasons.push("MATCHER_AXIS_NOT_SELECTED");
  if (preprocessingChanged && axis === "MATCHER") reasons.push("PREPROCESSING_AXIS_NOT_SELECTED");
  if (!matcherChanged && !preprocessingChanged && left.comparisons_sha256 !== right.comparisons_sha256) reasons.push("COMPARISONS_ARTIFACT_MISMATCH");
  return { eligible: reasons.length === 0, comparisonAxis, reasons };
}

/** Quality-utility workbench: ERC/AU-ERC/risk-coverage, uncertainty, error models, and threshold-policy analysis (QS-D30..D33). */
export function Utility({ projectId, onMessage, onWorkflowTerminal, onContextChange, onOpenEvidence }: UtilityProps) {
  const [policies, setPolicies] = useState<PairQualityPolicy[]>([]);
  const [leaves, setLeaves] = useState<MatcherLeaf[]>([]);
  const [jobs, setJobs] = useState<JobOut[]>([]);
  const [jobsProjectId, setJobsProjectId] = useState<string | null>(null);
  const [analyses, setAnalyses] = useState<UtilitySummary[]>([]);
  const [selectedLeaf, setSelectedLeaf] = useState<string>("");
  const [selectedJob, setSelectedJob] = useState<string>("");
  const [policy, setPolicy] = useState<string>("");
  const [policyError, setPolicyError] = useState<string | null>(null);
  const [measure, setMeasure] = useState<string>("");
  const [measureOptions, setMeasureOptions] = useState<string[]>([]);
  const [measureCatalogLoading, setMeasureCatalogLoading] = useState(false);
  const [measureCatalogError, setMeasureCatalogError] = useState<string | null>(null);
  const [targetFmr, setTargetFmr] = useState<string>("0.001");
  const [qualityThreshold, setQualityThreshold] = useState<string>("");
  const [replicates, setReplicates] = useState<string>("1000");
  const [running, setRunning] = useState<string | null>(null);
  const [detail, setDetail] = useState<UtilityAnalysisOut | null>(null);
  const [curves, setCurves] = useState<Record<string, { fnmr: CurvePoint[]; label: string }>>({});
  const [overlayAxis, setOverlayAxis] = useState<OverlayAxis>("MATCHER");
  const [overlayIds, setOverlayIds] = useState<Set<string>>(new Set());
  const [pairs, setPairs] = useState<{ total: number; rows: UtilityPairRow[] } | null>(null);
  const [evidencePairId, setEvidencePairId] = useState<string | null>(null);
  const [pairFilter, setPairFilter] = useState<"rejected" | "prevented" | "removed">("rejected");
  const [busy, setBusy] = useState(false);
  const [matcherNames, setMatcherNames] = useState<Record<string, string>>({});
  const [profileNames, setProfileNames] = useState<Record<string, string>>({});
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [analysisContracts, setAnalysisContracts] = useState<AddonCatalogOut["analysis_parameters"] | null>(null);
  const [feedError, setFeedError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [curveError, setCurveError] = useState<string | null>(null);
  const projectEpoch = useRef(0);
  const refreshSequence = useRef(0);
  const policySequence = useRef(0);
  const catalogSequence = useRef(0);
  const measureCatalogSequence = useRef(0);
  const detailSequence = useRef(0);
  const pairSequence = useRef(0);
  const runningDetailSequence = useRef(0);
  const handledTerminalAnalyses = useRef(new Set<string>());

  const refresh = useCallback(async () => {
    const epoch = projectEpoch.current;
    const sequence = ++refreshSequence.current;
    if (!projectId) {
      setLeaves([]); setJobs([]); setAnalyses([]);
      setJobsProjectId(null);
      setFeedError(null);
      return;
    }
    setFeedError(null);
    const results = await Promise.allSettled([
      api.matcherLeaves(projectId),
      api.projectJobs(projectId),
      api.utilityAnalyses(projectId),
    ]);
    if (projectEpoch.current !== epoch || refreshSequence.current !== sequence) return;
    const [leafResult, jobResult, analysisResult] = results;
    const errors: string[] = [];
    if (leafResult.status === "fulfilled") {
      setLeaves(leafResult.value);
      setSelectedLeaf((current) =>
        current && leafResult.value.some((leaf) => leaf.run_id === current) ? current : (leafResult.value[0]?.run_id ?? ""),
      );
    } else {
      setLeaves([]);
      setSelectedLeaf("");
      errors.push(`Matcher Run feed unavailable: ${leafResult.reason instanceof Error ? leafResult.reason.message : String(leafResult.reason)}`);
    }
    if (jobResult.status === "fulfilled") {
      const completed = jobResult.value.filter((job) => (
        job.state === "COMPLETED" && job.run_id && job.quality_result_available
      ));
      setJobs(completed);
      setJobsProjectId(projectId);
      setSelectedJob((current) =>
        current && completed.some((job) => job.job_id === current) ? current : (completed[0]?.job_id ?? ""),
      );
    } else {
      setJobs([]);
      setJobsProjectId(null);
      setSelectedJob("");
      errors.push(`Quality-job feed unavailable: ${jobResult.reason instanceof Error ? jobResult.reason.message : String(jobResult.reason)}`);
    }
    if (analysisResult.status === "fulfilled") {
      setAnalyses(analysisResult.value);
    } else {
      setAnalyses([]);
      errors.push(`Utility-analysis feed unavailable: ${analysisResult.reason instanceof Error ? analysisResult.reason.message : String(analysisResult.reason)}`);
    }
    setFeedError(errors.length ? errors.join(" · ") : null);
  }, [projectId]);

  const loadPolicies = useCallback(async () => {
    const sequence = ++policySequence.current;
    try {
      const body = await api.pairQualityPolicies();
      if (policySequence.current !== sequence) return;
      setPolicies(body.policies);
      setPolicy((current) => body.policies.some((item) => item.id === current) ? current : (body.policies[0]?.id ?? ""));
      setPolicyError(null);
    } catch (error) {
      if (policySequence.current !== sequence) return;
      setPolicies([]);
      setPolicy("");
      setPolicyError(`Pair-policy catalog unavailable; utility execution is disabled. ${error instanceof Error ? error.message : String(error)}`);
    }
  }, []);

  const loadCatalog = useCallback(async () => {
    const sequence = ++catalogSequence.current;
    try {
      const catalog = await api.addons();
      if (catalogSequence.current !== sequence) return;
      setMatcherNames(Object.fromEntries(catalog.matchers.map((item) => [item.matcher_id, item.name])));
      setProfileNames(Object.fromEntries(catalog.preprocessing.map((item) => [item.addon_id, item.display_name])));
      setAnalysisContracts(catalog.analysis_parameters);
      setCatalogError(null);
    } catch (error) {
      if (catalogSequence.current !== sequence) return;
      setCatalogError(error instanceof Error ? error.message : String(error));
    }
  }, []);

  const loadQualityMeasures = useCallback(async (jobId: string) => {
    if (!projectId) return;
    const epoch = projectEpoch.current;
    const sequence = ++measureCatalogSequence.current;
    setMeasureCatalogLoading(true);
    setMeasureCatalogError(null);
    try {
      const catalog = await api.qualityMeasures(projectId, jobId);
      if (
        projectEpoch.current !== epoch
        || measureCatalogSequence.current !== sequence
      ) return;
      setMeasureOptions(catalog.measures);
      setMeasure((current) =>
        catalog.measures.includes(current)
          ? current
          : (catalog.default_measure ?? catalog.measures[0] ?? ""),
      );
    } catch (error) {
      if (
        projectEpoch.current !== epoch
        || measureCatalogSequence.current !== sequence
      ) return;
      setMeasureOptions([]);
      setMeasure("");
      setMeasureCatalogError(
        error instanceof Error ? error.message : String(error),
      );
    } finally {
      if (
        projectEpoch.current === epoch
        && measureCatalogSequence.current === sequence
      ) setMeasureCatalogLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void loadPolicies();
    void loadCatalog();
  }, [loadCatalog, loadPolicies]);

  useLayoutEffect(() => {
    projectEpoch.current += 1;
    refreshSequence.current += 1;
    setLeaves([]); setJobs([]); setAnalyses([]);
    setJobsProjectId(null);
    setSelectedLeaf(""); setSelectedJob("");
    measureCatalogSequence.current += 1;
    detailSequence.current += 1;
    pairSequence.current += 1;
    setMeasure(""); setMeasureOptions([]);
    setMeasureCatalogLoading(false); setMeasureCatalogError(null);
    setRunning(null); setBusy(false);
    setDetail(null); setPairs(null); setCurves({});
    setOverlayAxis("MATCHER"); setOverlayIds(new Set());
    setEvidencePairId(null);
    setFeedError(null); setPollError(null); setCurveError(null);
  }, [projectId]);

  useEffect(() => {
    void refresh();
    // Matcher Runs and quality jobs are produced by the other panels; keep the pick lists current.
    const handle = window.setInterval(() => void refresh(), 4000);
    return () => window.clearInterval(handle);
  }, [refresh]);

  // Poll a running analysis.
  useEffect(() => {
    if (!running || !projectId) return;
    const epoch = projectEpoch.current;
    const analysisId = running;
    const handle = window.setInterval(() => {
      api.utilityAnalysis(projectId, analysisId).then((status) => {
        if (projectEpoch.current !== epoch) return;
        setPollError(null);
        if (status.state === "COMPLETED" || status.state === "FAILED" || status.state === "INTERRUPTED") {
          if (handledTerminalAnalyses.current.has(analysisId)) return;
          handledTerminalAnalyses.current.add(analysisId);
          setRunning(null);
          if (status.state === "COMPLETED") {
            if (detailSequence.current === runningDetailSequence.current) {
              setDetail(status);
              setPairs(null);
              setOverlayIds(new Set());
            }
            onMessage(`Utility analysis ${shortHash(status.analysis_id)} recorded as Run ${shortHash(status.summary?.run_id ?? "")}`);
          } else {
            onMessage(`Utility analysis ${status.state === "INTERRUPTED" ? "was interrupted" : "failed"}: ${status.error ?? "unknown error"}`);
          }
          onWorkflowTerminal?.();
          void refresh();
        }
      }).catch((error) => {
        if (projectEpoch.current === epoch) setPollError(`Utility status is temporarily unavailable; completion is not assumed. ${error instanceof Error ? error.message : String(error)}`);
      });
    }, 1500);
    return () => window.clearInterval(handle);
  }, [running, projectId, refresh, onMessage, onWorkflowTerminal]);

  const baselineSummary = detail?.summary ?? null;
  const overlayClassifications = useMemo(
    () => new Map(
      analyses.map((item) => [
        item.analysis_id,
        baselineSummary
          ? classifyOverlay(baselineSummary, item, overlayAxis)
          : { eligible: false, comparisonAxis: "UNKNOWN", reasons: ["SELECT_BASELINE"] } as OverlayClassification,
      ]),
    ),
    [analyses, baselineSummary, overlayAxis],
  );

  useEffect(() => {
    setOverlayIds((current) => new Set(
      [...current].filter((analysisId) => overlayClassifications.get(analysisId)?.eligible),
    ));
  }, [overlayClassifications]);

  const curveAnalyses = useMemo(() => {
    if (!baselineSummary) return [];
    const selected = analyses.filter((item) =>
      overlayIds.has(item.analysis_id)
      && item.analysis_id !== baselineSummary.analysis_id
      && overlayClassifications.get(item.analysis_id)?.eligible,
    );
    return [baselineSummary, ...selected];
  }, [analyses, baselineSummary, overlayClassifications, overlayIds]);
  const curveAnalysisKey = curveAnalyses.map((item) => item.analysis_id).join("\u001f");

  // Fetch only the explicitly selected, identity-compatible ERC overlay curves.
  useEffect(() => {
    let cancelled = false;
    if (curveAnalyses.length === 0) {
      setCurves({});
      setCurveError(null);
      return () => { cancelled = true; };
    }
    Promise.all(
      curveAnalyses.map((item) => api.utilityAnalysis(projectId!, item.analysis_id).then((body) => [item.analysis_id, { fnmr: (body.utility?.utility?.erc?.curves?.FNMR?.points ?? []) as CurvePoint[], label: `${item.leaf_id} · ${item.quality_measure} · ${item.pair_quality_policy.replace("pair.quality.", "")}` }] as const)),
    ).then((entries) => {
      if (!cancelled) {
        setCurves(Object.fromEntries(entries));
        setCurveError(null);
      }
    }).catch((error) => {
      if (!cancelled) {
        setCurves({});
        setCurveError(`Comparative ERC curves are unavailable; no empty-result claim is made. ${error instanceof Error ? error.message : String(error)}`);
      }
    });
    return () => { cancelled = true; };
  }, [curveAnalysisKey]); // eslint-disable-line react-hooks/exhaustive-deps -- the key is the immutable analysis selection

  const leafDataset = useMemo(
    () => leaves.find((leaf) => leaf.run_id === selectedLeaf)?.dataset_id ?? null,
    [leaves, selectedLeaf],
  );
  const eligibleJobs = useMemo(
    () => jobs.filter((job) => !leafDataset || !job.dataset_id || job.dataset_id === leafDataset),
    [jobs, leafDataset],
  );
  const groupedMeasureOptions = useMemo(
    () => groupRecordedQualityMeasureNames(measureOptions),
    [measureOptions],
  );
  useEffect(() => {
    // A quality job is only comparable to a matcher Run over the same dataset;
    // keep the selection inside the eligible set (e.g. exclude uploaded-photo jobs).
    setSelectedJob((current) =>
      current && eligibleJobs.some((job) => job.job_id === current) ? current : (eligibleJobs[0]?.job_id ?? ""),
    );
  }, [eligibleJobs]);

  useLayoutEffect(() => {
    if (!selectedJob || jobsProjectId !== projectId) {
      measureCatalogSequence.current += 1;
      setMeasure("");
      setMeasureOptions([]);
      setMeasureCatalogLoading(false);
      setMeasureCatalogError(null);
      return;
    }
    void loadQualityMeasures(selectedJob);
  }, [jobsProjectId, loadQualityMeasures, projectId, selectedJob]);

  const targetFmrValue = finiteNumber(targetFmr);
  const qualityThresholdValue = qualityThreshold.trim() === "" ? null : finiteNumber(qualityThreshold);
  const bootstrapReplicates = finiteNumber(replicates);
  const targetFmrError = numericParameterError(targetFmr, analysisContracts?.target_fmr ?? null, "Target FMR");
  const qualityThresholdError = numericParameterError(qualityThreshold, analysisContracts?.quality_threshold ?? null, "Quality threshold");
  const replicatesError = numericParameterError(replicates, analysisContracts?.bootstrap_replicates ?? null, "Bootstrap replicates");
  const runDisabledReason = busy
    ? "An operation is already in progress."
    : running !== null
      ? "Wait for the running utility analysis to finish."
      : !selectedLeaf
        ? "Select a recorded matcher Run."
        : !selectedJob
          ? "Select a completed quality job for the same dataset."
          : measureCatalogLoading
            ? "Wait for the selected quality job's measure catalog."
            : measureCatalogError
              ? "Reload the selected quality job's measure catalog."
              : !measure || !measureOptions.includes(measure)
                ? "Select a scalar quality measure recorded by the quality job."
                : !policies.some((item) => item.id === policy)
                  ? "Select an admitted pair-quality policy."
                  : targetFmrError ?? qualityThresholdError ?? replicatesError;

  const start = useCallback(async () => {
    if (
      !projectId || !selectedLeaf || !selectedJob || !measureOptions.includes(measure) || !policies.some((item) => item.id === policy)
      || targetFmrError || qualityThresholdError || replicatesError
      || targetFmrValue === null || bootstrapReplicates === null
    ) return;
    const epoch = projectEpoch.current;
    const detailRequest = ++detailSequence.current;
    pairSequence.current += 1;
    setBusy(true);
    try {
      const started = await api.startUtilityAnalysis(projectId, {
        matcher_run_id: selectedLeaf,
        quality_job_id: selectedJob,
        quality_measure: measure,
        pair_quality_policy: policy,
        target_fmr: targetFmrValue,
        quality_threshold: qualityThresholdValue,
        bootstrap_replicates: bootstrapReplicates,
      });
      if (projectEpoch.current !== epoch) return;
      if (started.already_recorded) {
        onMessage(`Analysis ${shortHash(started.analysis_id)} was already recorded; showing it`);
        const nextDetail = await api.utilityAnalysis(projectId, started.analysis_id);
        if (projectEpoch.current !== epoch || detailSequence.current !== detailRequest) return;
        setDetail(nextDetail);
        setPairs(null);
        setOverlayIds(new Set());
      } else {
        runningDetailSequence.current = detailRequest;
        setRunning(started.analysis_id);
        onMessage(`Utility analysis ${shortHash(started.analysis_id)} running`);
      }
    } catch (error) {
      if (projectEpoch.current === epoch) onMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (projectEpoch.current === epoch) setBusy(false);
    }
  }, [projectId, selectedLeaf, selectedJob, measure, measureOptions, policy, policies, targetFmrError, qualityThresholdError, replicatesError, targetFmrValue, qualityThresholdValue, bootstrapReplicates, onMessage]);

  const open = useCallback(async (analysisId: string) => {
    if (!projectId) return;
    const epoch = projectEpoch.current;
    const sequence = ++detailSequence.current;
    pairSequence.current += 1;
    setPairs(null);
    try {
      const nextDetail = await api.utilityAnalysis(projectId, analysisId);
      if (projectEpoch.current !== epoch || detailSequence.current !== sequence) return;
      setDetail(nextDetail);
      setOverlayIds(new Set());
    } catch (error) {
      if (projectEpoch.current === epoch && detailSequence.current === sequence) onMessage(error instanceof Error ? error.message : String(error));
    }
  }, [projectId, onMessage]);

  const loadPairs = useCallback(async (filter: "rejected" | "prevented" | "removed") => {
    if (!detail?.summary || !projectId) return;
    const epoch = projectEpoch.current;
    const analysisId = detail.analysis_id;
    const sequence = ++pairSequence.current;
    setPairFilter(filter);
    const query = filter === "rejected" ? "retained=false&limit=60" : filter === "prevented" ? "retained=false&decision_error=true&limit=60" : "retained=false&decision_error=false&limit=60";
    try {
      const nextPairs = await api.utilityPairs(projectId, analysisId, query);
      if (projectEpoch.current !== epoch || pairSequence.current !== sequence) return;
      setPairs(nextPairs);
    } catch (error) {
      if (projectEpoch.current === epoch && pairSequence.current === sequence) onMessage(error instanceof Error ? error.message : String(error));
    }
  }, [detail, projectId, onMessage]);

  const analysisIds = useMemo(() => curveAnalyses.map((item) => item.analysis_id), [curveAnalyses]);
  const utility = detail?.utility?.utility;
  const policyBlock = detail?.policy;

  useLayoutEffect(() => {
    const summary = detail?.summary;
    if (!projectId || !summary) {
      onContextChange?.(null);
      return;
    }
    const identity = summary.overlay_identity;
    onContextChange?.({
      projectId,
      datasetId: identity?.dataset_id ?? null,
      protocolId: identity?.protocol_id ?? null,
      protocolLabel: identity?.protocol_id ? "Recorded verification protocol" : null,
      leafId: summary.leaf_id,
      leafLabel: `${matcherNames[summary.matcher_id] ?? summary.matcher_id} · ${profileNames[summary.condition_id] ?? summary.condition_id}`,
      runId: summary.run_id,
    });
  }, [detail?.summary, matcherNames, onContextChange, profileNames, projectId]);

  useLayoutEffect(() => () => onContextChange?.(null), [onContextChange]);

  if (!projectId) {
    return (
      <section className="panel utility" aria-labelledby="utility-heading" data-testid="utility">
        <h2 id="utility-heading">Quality → utility · ERC, risk-coverage, policy analysis</h2>
        {catalogError && <p className="error" role="alert">Add-on catalog unavailable; recorded Run identifiers are shown unchanged. {catalogError} <button type="button" className="secondary" onClick={() => void loadCatalog()}>Retry add-on catalog</button></p>}
        {policyError && <p className="error" role="alert">{policyError} <button type="button" className="secondary" onClick={() => void loadPolicies()}>Retry pair-policy catalog</button></p>}
        {feedError && <p className="error" role="alert">{feedError}</p>}
        {pollError && <p className="error" role="alert">{pollError}</p>}
        {curveError && <p className="error" role="alert">{curveError}</p>}
        <div className="empty-state">
          <strong>Select a project to analyze matcher utility.</strong>
          <span>Utility analysis requires recorded matcher Runs and a completed quality batch in the same project.</span>
          <a className="button secondary" href="#/projects">Open projects</a>
        </div>
      </section>
    );
  }

  return (
    <section className="panel utility" aria-labelledby="utility-heading" data-testid="utility">
      <h2 id="utility-heading">Quality → utility · ERC, risk-coverage, policy analysis</h2>
      {catalogError && <p className="error" role="alert">Add-on catalog unavailable; recorded Run identifiers are shown unchanged. {catalogError} <button type="button" className="secondary" onClick={() => void loadCatalog()}>Retry add-on catalog</button></p>}
      {policyError && <p className="error" role="alert">{policyError} <button type="button" className="secondary" onClick={() => void loadPolicies()}>Retry pair-policy catalog</button></p>}
      {measureCatalogError && <p className="error" role="alert">Quality-measure catalog unavailable for the selected recorded job. {measureCatalogError} <button type="button" className="secondary" onClick={() => selectedJob && void loadQualityMeasures(selectedJob)}>Retry quality-measure catalog</button></p>}
      {feedError && <p className="error" role="alert">{feedError} <button type="button" className="secondary" onClick={() => void refresh()}>Retry project feeds</button></p>}
      {pollError && <p className="error" role="alert">{pollError}</p>}
      {curveError && <p className="error" role="alert">{curveError}</p>}
      <div className="controls-row utility-controls">
        <label>
          Matcher Run
          <select value={selectedLeaf} onChange={(event) => setSelectedLeaf(event.target.value)} data-testid="utility-leaf">
            {leaves.map((leaf) => (
              <option key={leaf.run_id} value={leaf.run_id}>{leaf.leaf_id} · {matcherNames[leaf.matcher_id] ?? leaf.matcher_id} · {profileNames[leaf.condition_id] ?? leaf.condition_id}</option>
            ))}
          </select>
        </label>
        <label>
          Quality job
          <select value={selectedJob} onChange={(event) => setSelectedJob(event.target.value)} data-testid="utility-job">
            {eligibleJobs.map((job) => (
              <option key={job.job_id} value={job.job_id}>{shortHash(job.job_id)} · {job.assessed.toLocaleString()} measured</option>
            ))}
          </select>
        </label>
        <label>
          Measure
          <select value={measure} onChange={(event) => setMeasure(event.target.value)} disabled={measureCatalogLoading || measureOptions.length === 0} aria-describedby="utility-measure-help" data-testid="utility-measure">
            {measureOptions.length === 0 && <option value="">{measureCatalogLoading ? "Loading recorded measures…" : "No recorded scalar measures"}</option>}
            {groupedMeasureOptions.map((group) => (
              <optgroup key={group.id} label={group.label} data-quality-group={group.id}>
                {group.measures.map((name) => <option key={name} value={name}>{qualityMeasureLabel(name)}</option>)}
              </optgroup>
            ))}
          </select>
          <span id="utility-measure-help" className="hint">Measures recorded in the selected completed quality job.</span>
        </label>
        <label>
          Pair policy
          <select value={policy} onChange={(event) => setPolicy(event.target.value)} disabled={policies.length === 0} data-testid="utility-policy">
            {policies.map((item) => (
              <option key={item.id} value={item.id} title={item.definition}>{item.id} · v{item.version}</option>
            ))}
          </select>
        </label>
        <label>
          Target FMR (probability)
          <input type="number" min={analysisContracts?.target_fmr.minimum} max={analysisContracts?.target_fmr.maximum} step="any" required value={targetFmr} onChange={(event) => setTargetFmr(event.target.value)} aria-invalid={targetFmrError ? "true" : undefined} aria-describedby={`utility-fmr-help${targetFmrError ? " utility-fmr-error" : ""}`} data-testid="utility-fmr" />
          <span id="utility-fmr-help" className="hint">Required; {analysisContracts?.target_fmr.guidance ?? "load the admitted scientific contract"}.</span>
          {targetFmrError && <span id="utility-fmr-error" className="error" role="alert">{targetFmrError}</span>}
        </label>
        <label>
          Quality threshold (score)
          <input type="number" step="any" value={qualityThreshold} placeholder="auto (20 % cut)" onChange={(event) => setQualityThreshold(event.target.value)} aria-invalid={qualityThresholdError ? "true" : undefined} aria-describedby={`utility-q-help${qualityThresholdError ? " utility-q-error" : ""}`} data-testid="utility-q" />
          <span id="utility-q-help" className="hint">{analysisContracts?.quality_threshold.guidance ?? "Load the admitted scientific contract"}; blank uses {analysisContracts?.quality_threshold.blank_behavior ?? "the recorded automatic policy"}.</span>
          {qualityThresholdError && <span id="utility-q-error" className="error" role="alert">{qualityThresholdError}</span>}
        </label>
        <label>
          Bootstrap replicates (count)
          <input type="number" min={analysisContracts?.bootstrap_replicates.minimum} max={analysisContracts?.bootstrap_replicates.maximum} step="1" required value={replicates} onChange={(event) => setReplicates(event.target.value)} aria-invalid={replicatesError ? "true" : undefined} aria-describedby={`utility-replicates-help${replicatesError ? " utility-replicates-error" : ""}`} data-testid="utility-replicates" />
          <span id="utility-replicates-help" className="hint">{analysisContracts?.bootstrap_replicates.guidance ?? "Load the admitted scientific contract"}.</span>
          {replicatesError && <span id="utility-replicates-error" className="error" role="alert">{replicatesError}</span>}
        </label>
        <button type="button" className="primary" disabled={runDisabledReason !== null} aria-describedby="utility-run-help" title={runDisabledReason ?? "Run utility analysis with the recorded inputs and validated parameters."} onClick={() => void start()} data-testid="utility-run">
          {running ? "Running…" : "Run utility analysis"}
        </button>
        <span id="utility-run-help" className={runDisabledReason ? "error" : "hint"}>{runDisabledReason ?? "Ready to run with the selected matcher Run and quality job."}</span>
      </div>

      <div className="controls-row utility-controls" data-testid="utility-overlay-controls">
        <label>
          ERC comparison axis
          <select
            value={overlayAxis}
            onChange={(event) => {
              setOverlayAxis(event.target.value as OverlayAxis);
              setOverlayIds(new Set());
            }}
            data-testid="utility-overlay-axis"
          >
            <option value="MATCHER">Matcher only</option>
            <option value="PREPROCESSING">Preprocessing only</option>
            <option value="COMPOSITE">Matcher + preprocessing (composite)</option>
          </select>
        </label>
        <p className="hint" data-testid="utility-overlay-guidance">
          {baselineSummary
            ? baselineSummary.overlay_identity
              ? `Baseline ${shortHash(baselineSummary.analysis_id, 10)}. Select compatible rows below; every non-axis evidence identity must match exactly.`
              : "The opened legacy analysis lacks a complete overlay identity and cannot authorize comparative overlays."
            : "Open one recorded analysis to set the baseline, then select compatible rows explicitly."}
        </p>
      </div>

      <div className="controls-row utility-controls" data-testid="utility-overlay-candidates">
        {analyses.map((item, index) => {
          const isBaseline = baselineSummary?.analysis_id === item.analysis_id;
          const classification = overlayClassifications.get(item.analysis_id);
          const eligible = classification?.eligible === true && !isBaseline;
          const explanationId = `utility-overlay-reason-${item.analysis_id}`;
          const explanation = !baselineSummary
            ? "Open this analysis to use it as the baseline."
            : isBaseline
              ? "Current baseline."
              : classification?.reasons.map((reason) => OVERLAY_REASON_LABELS[reason] ?? reason).join(" ") || `${classification?.comparisonAxis.toLowerCase().replaceAll("_", " ")} axis is compatible.`;
          return (
            <label key={item.analysis_id} title={explanation}>
              <span><span className="swatch" style={{ background: COLORS[index % COLORS.length] }} /> <span className="mono">{shortHash(item.analysis_id, 10)}</span> · {item.leaf_id}</span>
              <span>
                <input
                  type="checkbox"
                  checked={isBaseline || overlayIds.has(item.analysis_id)}
                  disabled={!eligible}
                  aria-describedby={explanationId}
                  title={explanation}
                  onChange={(event) => setOverlayIds((current) => {
                    const next = new Set(current);
                    if (event.target.checked) next.add(item.analysis_id); else next.delete(item.analysis_id);
                    return next;
                  })}
                  data-testid="utility-overlay-candidate"
                  data-analysis-id={item.analysis_id}
                  data-eligible={eligible ? "true" : "false"}
                  data-comparison-axis={classification?.comparisonAxis ?? "UNKNOWN"}
                  data-reasons={classification?.reasons.join(",") ?? "SELECT_BASELINE"}
                />
                {isBaseline ? " Baseline" : eligible ? " Include in overlay" : " Ineligible"}
              </span>
              <span id={explanationId} className="hint" data-testid="utility-overlay-reason">{explanation}</span>
            </label>
          );
        })}
      </div>

      <div className="utility-grid">
        <div style={{ minWidth: 0, overflowX: "auto" }}>
          <table className="compact utility-table" data-testid="utility-table">
            <caption>Recorded utility analyses</caption>
            <thead>
              <tr>
                <th scope="col">Analysis</th><th scope="col">Matcher · crop</th><th scope="col">Measure · policy</th><th scope="col">T (matcher)</th><th scope="col">Q (policy)</th><th scope="col">AU-ERC</th><th scope="col">pAU-ERC 0–20 %</th><th scope="col">FNMR before → after</th><th scope="col">Retained</th><th scope="col">Errors prevented</th>
              </tr>
            </thead>
            <tbody>
              {analyses.length === 0 && (
                <tr><td colSpan={10} className="hint">No utility analyses recorded yet.</td></tr>
              )}
              {analyses.map((item, index) => {
                const isBaseline = baselineSummary?.analysis_id === item.analysis_id;
                return (
                  <tr key={item.analysis_id} className={isBaseline ? "selected" : undefined}>
                    <th scope="row"><button type="button" className="cell" onClick={() => void open(item.analysis_id)} data-testid={`analysis-${item.leaf_id}`}><span className="swatch" style={{ background: COLORS[index % COLORS.length] }} /><span className="mono">{shortHash(item.analysis_id, 10)}</span></button></th>
                    <td>{item.leaf_id} · {profileNames[item.condition_id] ?? item.condition_id}</td>
                    <td>{item.quality_measure} · {item.pair_quality_policy.replace("pair.quality.", "")}</td>
                    <td className="mono">{num(item.matcher_threshold)}</td>
                    <td className="mono">{item.quality_threshold}</td>
                    <td className="mono">{num(item.au_erc_fnmr, 5)}</td>
                    <td className="mono">{num(item.partial_au_erc_fnmr, 5)}</td>
                    <td className="mono">{pct(item.fnmr_before)} → {pct(item.fnmr_after)}</td>
                    <td className="mono">{pct(item.retention_rate, 1)}</td>
                    <td className="mono">{item.errors_prevented} (−{item.successful_comparisons_removed} good)</td>
                  </tr>
                );
              })}
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
          <p className="hint" data-testid="utility-overlay-claim">FNMR-ERC for the opened baseline and explicitly selected compatible analyses only. Matcher thresholds stay fixed per analysis and are never re-estimated along a curve. A composite axis is descriptive and does not isolate matcher from preprocessing effects. FMR-ERC is reported separately in the detail below.</p>
        </div>
      </div>

      {detail?.summary && utility && policyBlock && (
        <div className="errors utility-detail" data-testid="utility-detail">
          <h3>
            Analysis <span className="mono">{shortHash(detail.analysis_id, 14)}</span> · Run {projectId ? <EvidenceOpenButton testId="utility-detail-run-evidence" request={{ kind: "run", href: detail.summary.links.run, evidenceId: detail.summary.run_id, projectId, label: `Utility Run ${shortHash(detail.summary.run_id, 14)}` }} onOpen={onOpenEvidence} className="mono">{shortHash(detail.summary.run_id, 14)}</EvidenceOpenButton> : shortHash(detail.summary.run_id, 14)} · {detail.summary.leaf_id} · {detail.summary.quality_measure} · {detail.summary.pair_quality_policy}
          </h3>
          <div className="utility-facts">
            <div>
              <h4>Thresholds (never conflated)</h4>
              <dl className="facts">
                <dt>MATCHER THRESHOLD</dt><dd className="mono">{num(policyBlock.thresholds.matcher.value, 6)} · {policyBlock.thresholds.matcher.provenance?.source}{policyBlock.thresholds.matcher.provenance?.target_fmr !== undefined ? ` (FMR ≤ ${policyBlock.thresholds.matcher.provenance.target_fmr})` : ""}</dd>
                <dt>QUALITY POLICY THRESHOLD</dt><dd className="mono">{policyBlock.thresholds.quality_policy.value} on {policyBlock.thresholds.quality_policy.quality_measure} · {policyBlock.thresholds.quality_policy.provenance?.source}</dd>
                <dt>Re-estimated during policy</dt><dd>{String(policyBlock.thresholds.matcher.reestimated_during_policy)}</dd>
                <dt>Uncertainty</dt><dd>{utility.uncertainty.status === "NOT_ASSESSED_IDENTITY_UNAVAILABLE" ? `${utility.uncertainty.status} · ${utility.uncertainty.reason}` : `${utility.uncertainty.method} · ${utility.uncertainty.replicates} replicates · seed ${utility.uncertainty.seed} · unit ${utility.uncertainty.sampling_unit}`}</dd>
              </dl>
            </div>
            <div>
              <h4>Areas under the ERC (registry v1.2.0)</h4>
              <table className="compact">
                <caption>Areas under the error-versus-reject curves</caption>
                <thead><tr><th scope="col">Curve</th><th scope="col">Metric</th><th scope="col">Domain</th><th scope="col">Value</th><th scope="col">95 % CI (cluster bootstrap)</th><th scope="col">Units</th></tr></thead>
                <tbody>
                  {(["FNMR", "FMR"] as const).flatMap((kind) => (["au_erc", "partial_au_erc"] as const).map((name) => {
                    const block = utility.areas[kind][name];
                    return (
                      <tr key={`${kind}-${name}`}>
                        <th scope="row">{kind}-ERC {name === "partial_au_erc" ? "partial" : "full"}</th><td className="mono">{block.definition[0]}@{block.definition[1]}</td><td className="mono">[{block.integration_domain[0]}, {block.integration_domain[1]}]</td>
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
                <caption>Fixed-threshold quality-policy effects</caption>
                <thead><tr><th scope="col">Policy measure</th><th scope="col">Recorded value</th></tr></thead>
                <tbody>
                  <tr><th scope="row">Retention rate</th><td className="mono">{pct(policyBlock.retention_rate, 2)} ({policyBlock.counts.retained} of {policyBlock.counts.usable_units})</td></tr>
                  <tr><th scope="row">Rejection rate</th><td className="mono">{pct(policyBlock.rejection_rate, 2)} ({policyBlock.counts.rejected} rejected)</td></tr>
                  <tr><th scope="row">FNMR before → after</th><td className="mono">{pct(policyBlock.before_policy.fnmr.rate)} → {pct(policyBlock.after_policy.fnmr.rate)} [{pct(policyBlock.after_policy.fnmr.ci_lower)}, {pct(policyBlock.after_policy.fnmr.ci_upper)}]</td></tr>
                  <tr><th scope="row">FMR before → after</th><td className="mono">{pct(policyBlock.before_policy.fmr.rate, 3)} → {pct(policyBlock.after_policy.fmr.rate, 3)}</td></tr>
                  <tr><th scope="row">Errors prevented</th><td className="mono">{policyBlock.impact.errors_prevented} ({policyBlock.impact.false_non_matches_prevented} FNM · {policyBlock.impact.false_matches_prevented} FM)</td></tr>
                  <tr><th scope="row">Successful comparisons removed</th><td className="mono">{policyBlock.impact.successful_comparisons_removed} ({policyBlock.impact.successful_mated_removed} mated · {policyBlock.impact.successful_non_mated_removed} non-mated)</td></tr>
                  <tr><th scope="row">Excluded</th><td className="mono">{policyBlock.counts.processing_failures_excluded} processing failures · {policyBlock.counts.missing_quality_excluded} missing quality</td></tr>
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
                <caption>False non-match rate by quality bin</caption>
                <thead><tr><th scope="col">Bin</th><th scope="col">Units</th><th scope="col">Errors</th><th scope="col">FNMR</th><th scope="col">95 % CI</th><th scope="col">Support</th></tr></thead>
                <tbody>
                  {utility.quality_conditioned.FNMR.bins.map((bin: any) => (
                    <tr key={bin.bin_index}>
                      <th scope="row" className="mono">[{bin.quality_low}, {bin.quality_high}{bin.closed_high ? "]" : ")"}</th><td className="mono">{bin.trials}</td><td className="mono">{bin.events}</td>
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
                  <caption>Retention-matched impact by quality measure</caption>
                  <thead><tr><th scope="col">Measure</th><th scope="col">Cutoff</th><th scope="col">Retention</th><th scope="col">Errors prevented</th><th scope="col">Good removed</th><th scope="col">FNMR after</th></tr></thead>
                  <tbody>
                    {[...(policyBlock.per_quality_measure_impact?.measures ?? [])].sort((a: any, b: any) => (b.errors_prevented ?? -1) - (a.errors_prevented ?? -1)).map((row: any) => (
                      <tr key={row.quality_measure}>
                        <th scope="row">{row.quality_measure}</th><td className="mono">{row.quality_cutoff ?? row.status}</td><td className="mono">{pct(row.retention_rate, 1)}</td><td className="mono">{row.errors_prevented ?? "—"}</td><td className="mono">{row.successful_comparisons_removed ?? "—"}</td><td className="mono">{pct(row.fnmr_after)}</td>
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
                    {row.links.left_image && <img src={row.links.left_image} alt={`Left biometric sample ${row.left_sample_id}; ${row.left_subject_id ? `subject ${row.left_subject_id}` : "subject identity unavailable"}; pair ${row.pair_id}`} loading="lazy" />}
                    {row.links.right_image && <img src={row.links.right_image} alt={`Right biometric sample ${row.right_sample_id}; ${row.right_subject_id ? `subject ${row.right_subject_id}` : "subject identity unavailable"}; pair ${row.pair_id}`} loading="lazy" />}
                    <div className="pair-facts">
                      <span className="mono">{row.pair_id} · fold {row.fold ?? "not recorded"} · {row.label}</span>
                      <button type="button" className="secondary" data-testid="utility-evidence-open" onClick={() => setEvidencePairId(evidencePairId === row.pair_id ? null : row.pair_id)}>
                        {evidencePairId === row.pair_id ? "hide evidence" : "match evidence"}
                      </button>
                      {evidencePairId === row.pair_id && detail?.summary?.matcher_run_id && (
                        <PairEvidence projectId={projectId} runId={detail.summary.matcher_run_id} pairId={row.pair_id} onClose={() => setEvidencePairId(null)} />
                      )}
                      <span className="mono">q {row.quality_left ?? "—"} / {row.quality_right ?? "—"} → pair {row.pair_quality ?? "—"} · {row.score === null ? row.failure : `score ${row.score.toFixed(4)}`}{row.decision_error ? " · matcher error" : ""}</span>
                      <span className="links">
                        {projectId && <EvidenceOpenButton testId="utility-left-quality-evidence" request={{ kind: "sample-quality", href: row.links.left_quality, evidenceId: row.left_sample_id, projectId, label: `Left sample quality ${shortHash(row.left_sample_id, 14)}` }} onOpen={onOpenEvidence}>left quality</EvidenceOpenButton>}
                        {projectId && <EvidenceOpenButton testId="utility-right-quality-evidence" request={{ kind: "sample-quality", href: row.links.right_quality, evidenceId: row.right_sample_id, projectId, label: `Right sample quality ${shortHash(row.right_sample_id, 14)}` }} onOpen={onOpenEvidence}>right quality</EvidenceOpenButton>}
                        {detail?.summary?.run_id && <EvidenceOpenButton testId="utility-pair-run-evidence" request={{ kind: "run", href: row.links.run, evidenceId: detail.summary.run_id, projectId, label: `Utility pair Run ${shortHash(detail.summary.run_id, 14)}` }} onOpen={onOpenEvidence}>run</EvidenceOpenButton>}
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

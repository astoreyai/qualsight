import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type ReactNode } from "react";
import {
  api,
  ApiError,
  type AddonCatalogOut,
  type DetectionGeometryOut,
  type DirectPairComparisonStatus,
  type DirectPairEvidenceOut,
  type OfiqLandmarksOut,
  type OwnerPairAdmissionOut,
  type OwnerPairRelationship,
  type UploadAssessOut,
} from "./api";
import { numericParameterError } from "./parameterContract";
import { EvidenceOpenButton, type EvidenceRequest } from "./EvidenceDetail";

const OWNER_INPUT_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1";
const TERMINAL_COMPARISON_STATES = new Set(["COMPLETED", "FAILED", "INTERRUPTED"]);

type GeometryState<T> =
  | { phase: "loading" }
  | { phase: "ready"; result: T }
  | { phase: "error"; detail: string };

type ComparisonSideState =
  | { phase: "empty" }
  | { phase: "uploading"; filename: string }
  | { phase: "error"; filename: string; detail: string }
  | {
      phase: "ready";
      upload: UploadAssessOut;
      scrfd: GeometryState<DetectionGeometryOut>;
      ofiq: GeometryState<OfiqLandmarksOut>;
    };

type ExecutionState =
  | { phase: "idle" }
  | { phase: "starting" }
  | { phase: "running"; status: DirectPairComparisonStatus }
  | { phase: "failed"; detail: string; status?: DirectPairComparisonStatus };

type HistoryState =
  | { phase: "loading" }
  | { phase: "ready"; records: DirectPairComparisonStatus[] }
  | { phase: "error"; detail: string };

interface ComparisonStudioProps {
  projectId: string | null;
  datasetStudy: ReactNode;
  onMessage: (message: string) => void;
  onWorkflowTerminal: (projectId: string) => void;
  onOpenEvidence: (request: EvidenceRequest) => void;
}

function errorDetail(error: unknown): string {
  if (error instanceof ApiError) return `${error.status}: ${error.detail}`;
  return error instanceof Error ? error.message : String(error);
}

function sideUpload(side: ComparisonSideState): UploadAssessOut | null {
  return side.phase === "ready" ? side.upload : null;
}

function geometryPhase<T>(state: GeometryState<T> | null): string {
  return state?.phase ?? "idle";
}

function relationshipLabel(value: OwnerPairRelationship): string {
  if (value === "SAME_PERSON") return "Same person";
  if (value === "DIFFERENT_PEOPLE") return "Different people";
  return "Relationship not recorded";
}

function shortIdentity(value: string): string {
  const digest = value.includes(":") ? value.slice(value.lastIndexOf(":") + 1) : value;
  return `${digest.slice(0, 12)}…`;
}

function SideCard({
  side,
  label,
  state,
  disabled,
  prerequisiteId,
  onUpload,
}: {
  side: "a" | "b";
  label: string;
  state: ComparisonSideState;
  disabled: boolean;
  prerequisiteId: string | undefined;
  onUpload: (file: File) => void;
}) {
  const upload = sideUpload(state);
  const scrfd = state.phase === "ready" ? state.scrfd : null;
  const ofiq = state.phase === "ready" ? state.ofiq : null;
  const uqs = upload?.measures.find((measure) => measure.name === "UnifiedQualityScore")?.scalar ?? null;
  const detection = scrfd?.phase === "ready" ? scrfd.result : null;
  const ofiqGeometry = ofiq?.phase === "ready" ? ofiq.result : null;
  const imageWidth = detection?.image.width ?? ofiqGeometry?.image.width ?? upload?.width ?? 1;
  const imageHeight = detection?.image.height ?? ofiqGeometry?.image.height ?? upload?.height ?? 1;
  const inputId = `comparison-upload-${side}`;

  const chooseFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file) onUpload(file);
    event.target.value = "";
  };

  return (
    <article
      className="comparison-side"
      data-testid={`comparison-side-${side}`}
      data-quality-state={state.phase === "ready" ? "ready" : state.phase}
      data-scrfd-state={geometryPhase(scrfd)}
      data-ofiq-state={geometryPhase(ofiq)}
      data-sample-id={upload?.sample_id ?? ""}
      data-image-sha256={upload?.image_sha256 ?? ""}
      aria-labelledby={`comparison-side-${side}-heading`}
    >
      <div className="comparison-side-heading">
        <div>
          <span className="comparison-step">{side === "a" ? "01" : "02"}</span>
          <h3 id={`comparison-side-${side}-heading`}>{label}</h3>
        </div>
        <span className="comparison-state" data-state={state.phase}>
          {state.phase === "empty" ? "Awaiting image" : state.phase === "uploading" ? "Recording" : state.phase === "error" ? "Needs attention" : "Recorded"}
        </span>
      </div>

      <label className="comparison-file-label" htmlFor={inputId}>
        <span>{upload ? "Replace image" : "Choose image"}</span>
        <small>JPEG, PNG, or BMP; recorded to the active project</small>
      </label>
      <input
        id={inputId}
        className="comparison-file-input"
        data-testid={inputId}
        type="file"
        accept="image/jpeg,image/png,image/bmp"
        disabled={disabled}
        aria-describedby={prerequisiteId}
        onChange={chooseFile}
      />

      {state.phase === "uploading" && (
        <div className="comparison-pending" role="status">
          <span className="comparison-spinner" aria-hidden="true" />
          Recording quality for {state.filename}…
        </div>
      )}
      {state.phase === "error" && (
        <div className="comparison-error" role="alert">
          <strong>{state.filename} was not admitted.</strong>
          <span>{state.detail}</span>
        </div>
      )}
      {state.phase === "empty" && (
        <div className="comparison-empty">
          <span aria-hidden="true">＋</span>
          <p>Upload one real face image to record quality and geometry.</p>
        </div>
      )}

      {state.phase === "ready" && (
        <>
          <figure className="comparison-preview">
            <img
              data-testid={`comparison-image-${side}`}
              src={state.upload.provenance.sample_image}
              alt={`${label}: recorded owner-supplied image ${state.upload.original_name}`}
            />
            {(detection || ofiqGeometry) && (
              <svg viewBox={`0 0 ${imageWidth} ${imageHeight}`} aria-label={`${label} recorded face geometry`} role="img">
                {detection?.detections.map((face, faceIndex) => {
                  const [x1 = 0, y1 = 0, x2 = x1, y2 = y1] = face.bbox;
                  return (
                    <g key={`scrfd-${faceIndex}`} className="comparison-scrfd">
                      <rect x={x1} y={y1} width={Math.max(0, x2 - x1)} height={Math.max(0, y2 - y1)} />
                      {face.landmarks.map(([x, y], pointIndex) => <circle key={pointIndex} cx={x} cy={y} r="2.5" />)}
                    </g>
                  );
                })}
                {ofiqGeometry?.landmarks.map(([x, y], pointIndex) => (
                  <circle className="comparison-ofiq-point" key={`ofiq-${pointIndex}`} cx={x} cy={y} r="1.35" />
                ))}
              </svg>
            )}
            <figcaption>{state.upload.original_name} · {state.upload.width} × {state.upload.height}px</figcaption>
          </figure>

          <div
            className="comparison-quality-summary"
            data-testid={`comparison-quality-${side}`}
            data-measure-count={state.upload.measures.length}
            data-run-id={state.upload.run_id}
          >
            <div>
              <span>Unified quality</span>
              <strong>{uqs === null ? "Unavailable" : Number(uqs).toFixed(1)}</strong>
            </div>
            <div>
              <span>Recorded measures</span>
              <strong>{state.upload.measures.length}</strong>
            </div>
            <div>
              <span>Failed measures</span>
              <strong>{state.upload.failed_measures.length}</strong>
            </div>
          </div>

          <div
            className="comparison-geometry"
            data-testid={`comparison-geometry-${side}`}
            data-scrfd-landmarks={detection?.detections[0]?.landmarks.length ?? 0}
            data-ofiq-landmarks={ofiqGeometry?.landmarks.length ?? 0}
          >
            <div className="comparison-geometry-row">
              <span><i className="comparison-geometry-key scrfd" aria-hidden="true" /> SCRFD geometry</span>
              {state.scrfd.phase === "loading" && <em role="status">Processing…</em>}
              {state.scrfd.phase === "ready" && <strong>{state.scrfd.result.face_count ?? state.scrfd.result.detections.length} face · {state.scrfd.result.detections[0]?.landmarks.length ?? 0} points</strong>}
              {state.scrfd.phase === "error" && <strong className="error" role="alert">Unavailable: {state.scrfd.detail}</strong>}
            </div>
            <div className="comparison-geometry-row">
              <span><i className="comparison-geometry-key ofiq" aria-hidden="true" /> OFIQ geometry</span>
              {state.ofiq.phase === "loading" && <em role="status">Processing…</em>}
              {state.ofiq.phase === "ready" && <strong>{state.ofiq.result.faces.length} face · {state.ofiq.result.landmarks.length} points</strong>}
              {state.ofiq.phase === "error" && <strong className="error" role="alert">Unavailable: {state.ofiq.detail}</strong>}
            </div>
          </div>

          <p className="comparison-record-id mono">sample {shortIdentity(state.upload.sample_id)} · run {shortIdentity(state.upload.run_id)}</p>
        </>
      )}
    </article>
  );
}

function EvidenceSide({ side, evidence }: { side: "left" | "right"; evidence: DirectPairEvidenceOut["sides"]["left"] }) {
  const label = side === "left" ? "Image A" : "Image B";
  return (
    <article
      className="direct-pair-side"
      data-testid={`comparison-evidence-${side}`}
      data-sample-id={evidence.sample_id}
      data-sha256={evidence.sha256}
    >
      <div className="direct-pair-side-heading">
        <h4>{label}</h4>
        <span>{evidence.failure ? "Processing failure recorded" : "Template recorded"}</span>
      </div>
      <img src={evidence.links.source} alt={`${label} recorded source for direct comparison`} />
      {evidence.links.crop && <img className="direct-pair-crop" src={evidence.links.crop} alt={`${label} recorded 112 by 112 comparison crop`} />}
      <dl>
        <div><dt>Sample</dt><dd className="mono">{shortIdentity(evidence.sample_id)}</dd></div>
        <div><dt>Faces</dt><dd>{evidence.face_count ?? "Unavailable"}</dd></div>
        <div><dt>Embedding</dt><dd>{evidence.embedding ? `${evidence.embedding.shape.join(" × ")} ${evidence.embedding.dtype}` : "Not produced"}</dd></div>
        <div><dt>Quality</dt><dd>{evidence.quality?.unified_quality_score ?? "Unavailable"}</dd></div>
      </dl>
      {evidence.failure && <p className="comparison-error" role="alert">{evidence.failure.kind}: {evidence.failure.detail}</p>}
      <details className="direct-pair-artifacts" data-testid="comparison-side-evidence-actions">
        <summary>{label} evidence actions</summary>
        <div aria-label={`${label} recorded artifacts`}>
          {(Object.entries(evidence.links) as Array<[keyof typeof evidence.links, string | null]>).map(([artifact, href]) => (
            href ? <a key={artifact} href={href} data-artifact={artifact}>{artifact}</a> : null
          ))}
        </div>
      </details>
    </article>
  );
}

function DirectPairEvidence({ evidence, onOpenEvidence }: { evidence: DirectPairEvidenceOut; onOpenEvidence: (request: EvidenceRequest) => void }) {
  const agreement = evidence.result.agreement_with_operator_assertion;
  const agreementText = evidence.pair.relationship === "NOT_RECORDED"
    ? "No relationship was recorded; no agreement or correctness claim is made."
    : evidence.result.decision === null
      ? "No matcher decision was recorded; no agreement or correctness claim is made."
      : agreement
        ? "Matcher decision agrees with the operator assertion. This is not a ground-truth correctness claim."
        : "Matcher decision differs from the operator assertion. This is not a ground-truth correctness claim.";

  return (
    <section
      className="direct-pair-evidence"
      data-testid="direct-pair-evidence"
      data-state="ready"
      data-comparison-id={evidence.comparison_id}
      aria-labelledby="direct-pair-evidence-heading"
    >
      <div className="direct-pair-evidence-heading">
        <div>
          <span className="comparison-step">05</span>
          <h3 id="direct-pair-evidence-heading">Recorded comparison evidence</h3>
        </div>
        <span className="comparison-state" data-state="ready">Complete</span>
      </div>

      <div className="direct-pair-verdict">
        <div data-testid="comparison-assertion">
          <span>Operator assertion · unverified</span>
          <strong>{relationshipLabel(evidence.pair.relationship)}</strong>
        </div>
        <span className="direct-pair-separator" aria-hidden="true">→</span>
        <div data-testid="comparison-decision" data-decision={evidence.result.decision ?? "NONE"}>
          <span>Matcher decision</span>
          <strong>{evidence.result.decision?.replace("_", " ") ?? "No decision"}</strong>
        </div>
        <div className="direct-pair-score">
          <span>Similarity score</span>
          <strong>{evidence.result.score === null ? "Unavailable" : evidence.result.score.toFixed(6)}</strong>
        </div>
      </div>
      <p className="direct-pair-agreement" data-testid="comparison-agreement">{agreementText}</p>

      <div className="direct-pair-sides">
        <EvidenceSide side="left" evidence={evidence.sides.left} />
        <EvidenceSide side="right" evidence={evidence.sides.right} />
      </div>

      <section
        className="direct-pair-execution"
        data-testid="comparison-execution"
        data-matcher-id={evidence.execution.matcher_id}
        data-preprocessing-id={evidence.execution.preprocessing_id}
        data-run-id={evidence.run_id}
        aria-labelledby="direct-pair-execution-heading"
      >
        <h4 id="direct-pair-execution-heading">Execution provenance</h4>
        <dl>
          <div><dt>Matcher</dt><dd>{evidence.execution.matcher_id}</dd></div>
          <div><dt>Preprocessing</dt><dd>{evidence.execution.preprocessing_id}</dd></div>
          <div><dt>Threshold rule</dt><dd>{evidence.execution.threshold.rule} · {evidence.execution.threshold.value}</dd></div>
          <div><dt>Runtime</dt><dd>{evidence.execution.runtime_seconds.toFixed(3)} s · {evidence.execution.matcher_device}</dd></div>
          <div><dt>Matcher checkpoint</dt><dd className="mono">{evidence.execution.matcher_checkpoint_sha256}</dd></div>
          <div><dt>Detector</dt><dd className="mono">{evidence.execution.detector_sha256}</dd></div>
        </dl>
      </section>

      <div className="comparison-claim-boundary" data-testid="comparison-claim-boundary">
        <strong>Claim boundary</strong>
        <p>This direct pair is not a ground truth authority and does not support population error claims. Prohibited population metrics: {evidence.claim_boundary.prohibited.join(", ")}.</p>
      </div>

      <div className="direct-pair-primary-actions">
        <EvidenceOpenButton testId="comparison-run-link" request={{ kind: "direct-run", href: evidence.links.run, evidenceId: evidence.run_id, projectId: evidence.project_id, label: `Direct-comparison Run ${shortIdentity(evidence.run_id)}` }} onOpen={onOpenEvidence}>Open structured Run</EvidenceOpenButton>
        <EvidenceOpenButton testId="comparison-status-link" request={{ kind: "direct-comparison", href: evidence.links.status, evidenceId: evidence.comparison_id, projectId: evidence.project_id, label: `Comparison status ${shortIdentity(evidence.comparison_id)}` }} onOpen={onOpenEvidence}>Open comparison status</EvidenceOpenButton>
      </div>
      <details className="comparison-raw-actions" data-testid="comparison-raw-actions">
        <summary>Raw evidence actions</summary>
        <div>
          <a data-testid="comparison-raw-view" href={evidence.links.raw_manifest}>View raw manifest</a>
          <a data-testid="comparison-raw-download" href={evidence.links.raw_manifest} download={`${evidence.comparison_id}-raw-manifest.json`}>Download raw manifest</a>
        </div>
      </details>
    </section>
  );
}

export function ComparisonStudio({ projectId, datasetStudy, onMessage, onWorkflowTerminal, onOpenEvidence }: ComparisonStudioProps) {
  const [mode, setMode] = useState<"pair" | "dataset">("pair");
  const [sides, setSides] = useState<Record<"a" | "b", ComparisonSideState>>({ a: { phase: "empty" }, b: { phase: "empty" } });
  const [relationship, setRelationship] = useState<OwnerPairRelationship | null>(null);
  const [pair, setPair] = useState<OwnerPairAdmissionOut | null>(null);
  const [pairError, setPairError] = useState<string | null>(null);
  const [pairBusy, setPairBusy] = useState(false);
  const [catalog, setCatalog] = useState<AddonCatalogOut | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [matcherId, setMatcherId] = useState("");
  const [preprocessingId, setPreprocessingId] = useState("");
  const [threshold, setThreshold] = useState("0.8");
  const [execution, setExecution] = useState<ExecutionState>({ phase: "idle" });
  const [evidence, setEvidence] = useState<DirectPairEvidenceOut | null>(null);
  const [history, setHistory] = useState<HistoryState>(
    projectId ? { phase: "loading" } : { phase: "ready", records: [] },
  );
  const contextEpoch = useRef(0);
  const workflowSequence = useRef(0);
  const uploadSequences = useRef({ a: 0, b: 0 });
  const catalogSequence = useRef(0);
  const historySequence = useRef(0);
  const historyReadSequence = useRef(0);
  const executionSequence = useRef(0);
  const executionPending = useRef(false);
  const activeProjectId = useRef(projectId);
  activeProjectId.current = projectId;

  const eligibleMatchers = useMemo(
    () => catalog?.matchers.filter((item) => item.ready_to_load === true) ?? [],
    [catalog],
  );
  const eligibleProfiles = useMemo(
    () => catalog?.preprocessing.filter((item) =>
      item.runtime_state === "READY"
      && item.input_representation === "SOURCE_IMAGE_REQUIRING_DETECTION"
      && item.compatible_matcher_ids.includes(matcherId),
    ) ?? [],
    [catalog, matcherId],
  );
  const thresholdError = numericParameterError(threshold, catalog?.analysis_parameters.matcher_threshold ?? null, "Matcher threshold");

  useEffect(() => {
    contextEpoch.current += 1;
    workflowSequence.current += 1;
    uploadSequences.current.a += 1;
    uploadSequences.current.b += 1;
    historySequence.current += 1;
    historyReadSequence.current += 1;
    executionSequence.current += 1;
    executionPending.current = false;
    setSides({ a: { phase: "empty" }, b: { phase: "empty" } });
    setRelationship(null);
    setPair(null);
    setPairError(null);
    setPairBusy(false);
    setExecution({ phase: "idle" });
    setEvidence(null);
    setHistory(projectId ? { phase: "loading" } : { phase: "ready", records: [] });
  }, [projectId]);

  const loadCatalog = useCallback(async () => {
    const sequence = ++catalogSequence.current;
    setCatalogLoading(true);
    setCatalogError(null);
    try {
      const value = await api.addons();
      if (catalogSequence.current !== sequence) return;
      setCatalog(value);
      const matchers = value.matchers.filter((item) => item.ready_to_load === true);
      setMatcherId((current) => matchers.some((item) => item.matcher_id === current) ? current : (matchers[0]?.matcher_id ?? ""));
    } catch (error) {
      if (catalogSequence.current !== sequence) return;
      setCatalog(null);
      setCatalogError(errorDetail(error));
    } finally {
      if (catalogSequence.current === sequence) setCatalogLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadCatalog();
    return () => { catalogSequence.current += 1; };
  }, [loadCatalog]);

  useEffect(() => {
    setPreprocessingId((current) => eligibleProfiles.some((item) => item.addon_id === current) ? current : (eligibleProfiles[0]?.addon_id ?? ""));
  }, [eligibleProfiles]);

  const loadHistory = useCallback(async () => {
    const request = ++historySequence.current;
    if (!projectId) {
      setHistory({ phase: "ready", records: [] });
      return;
    }
    const epoch = contextEpoch.current;
    setHistory({ phase: "loading" });
    try {
      const records = await api.ownerPairComparisons(projectId);
      if (contextEpoch.current === epoch && historySequence.current === request) {
        setHistory({ phase: "ready", records });
      }
    } catch (error) {
      if (contextEpoch.current === epoch && historySequence.current === request) {
        setHistory({ phase: "error", detail: errorDetail(error) });
      }
    }
  }, [projectId]);

  useEffect(() => {
    void loadHistory();
    return () => { historySequence.current += 1; };
  }, [loadHistory]);

  const invalidatePair = () => {
    workflowSequence.current += 1;
    executionSequence.current += 1;
    executionPending.current = false;
    setPair(null);
    setPairError(null);
    setPairBusy(false);
    setExecution({ phase: "idle" });
    setEvidence(null);
  };

  const updateGeometry = <T,>(side: "a" | "b", sequence: number, kind: "scrfd" | "ofiq", next: GeometryState<T>) => {
    setSides((current) => {
      if (uploadSequences.current[side] !== sequence || current[side].phase !== "ready") return current;
      return {
        ...current,
        [side]: { ...current[side], [kind]: next },
      } as Record<"a" | "b", ComparisonSideState>;
    });
  };

  const uploadSide = async (side: "a" | "b", file: File) => {
    if (!projectId || executionPending.current || execution.phase === "running") return;
    const epoch = contextEpoch.current;
    const sequence = ++uploadSequences.current[side];
    invalidatePair();
    setSides((current) => ({ ...current, [side]: { phase: "uploading", filename: file.name } }));
    try {
      const upload = await api.assessUpload(projectId, OWNER_INPUT_SCOPE, file);
      if (contextEpoch.current !== epoch || uploadSequences.current[side] !== sequence) return;
      setSides((current) => ({
        ...current,
        [side]: { phase: "ready", upload, scrfd: { phase: "loading" }, ofiq: { phase: "loading" } },
      }));
      onWorkflowTerminal(projectId);
      void api.detectionForSample(projectId, upload.sample_id)
        .then((result) => updateGeometry(side, sequence, "scrfd", { phase: "ready", result }))
        .catch((error: unknown) => updateGeometry(side, sequence, "scrfd", { phase: "error", detail: errorDetail(error) }));
      void api.ofiqLandmarksForSample(projectId, upload.sample_id)
        .then((result) => updateGeometry(side, sequence, "ofiq", { phase: "ready", result }))
        .catch((error: unknown) => updateGeometry(side, sequence, "ofiq", { phase: "error", detail: errorDetail(error) }));
    } catch (error) {
      if (contextEpoch.current !== epoch || uploadSequences.current[side] !== sequence) return;
      setSides((current) => ({ ...current, [side]: { phase: "error", filename: file.name, detail: errorDetail(error) } }));
    }
  };

  const chooseRelationship = (value: OwnerPairRelationship) => {
    if (executionPending.current || execution.phase === "running") return;
    if (value !== relationship) invalidatePair();
    setRelationship(value);
  };

  const finalizePair = async () => {
    const left = sideUpload(sides.a);
    const right = sideUpload(sides.b);
    if (!projectId || !left || !right || !relationship || executionPending.current || execution.phase === "running") return;
    const epoch = contextEpoch.current;
    const workflow = workflowSequence.current;
    setPairBusy(true);
    setPairError(null);
    try {
      const record = await api.admitOwnerPair(projectId, left.sample_id, right.sample_id, relationship);
      if (contextEpoch.current !== epoch || workflowSequence.current !== workflow) return;
      setPair(record);
      setExecution({ phase: "idle" });
      setEvidence(null);
      onMessage(`Recorded owner pair ${shortIdentity(record.pair_id)} with an operator-asserted, unverified relationship.`);
    } catch (error) {
      if (contextEpoch.current === epoch && workflowSequence.current === workflow) setPairError(errorDetail(error));
    } finally {
      if (contextEpoch.current === epoch && workflowSequence.current === workflow) setPairBusy(false);
    }
  };

  const recordHistory = (status: DirectPairComparisonStatus) => {
    historySequence.current += 1;
    setHistory((current) => {
      const records = current.phase === "ready" ? current.records : [];
      return {
        phase: "ready",
        records: [status, ...records.filter((item) => item.comparison_id !== status.comparison_id)],
      };
    });
  };

  const executeComparison = async () => {
    if (!projectId || !pair || !matcherId || !preprocessingId || thresholdError || executionPending.current || execution.phase === "running") return;
    const epoch = contextEpoch.current;
    const workflow = workflowSequence.current;
    const request = ++executionSequence.current;
    historyReadSequence.current += 1;
    executionPending.current = true;
    setEvidence(null);
    setExecution({ phase: "starting" });
    try {
      let status = await api.startOwnerPairComparison(projectId, {
        pair_id: pair.pair_id,
        matcher_id: matcherId,
        preprocessing_id: preprocessingId,
        threshold: { contract_id: "matcher_threshold", kind: "FIXED_VALUE", value: Number(threshold) },
      });
      if (contextEpoch.current !== epoch || executionSequence.current !== request) return;
      recordHistory(status);
      if (workflowSequence.current === workflow) setExecution({ phase: "running", status });
      while (!TERMINAL_COMPARISON_STATES.has(status.state)) {
        await new Promise((resolve) => window.setTimeout(resolve, status.poll_after_ms ?? 500));
        if (contextEpoch.current !== epoch || executionSequence.current !== request) return;
        status = await api.ownerPairComparison(projectId, status.comparison_id);
        if (contextEpoch.current !== epoch || executionSequence.current !== request) return;
        recordHistory(status);
        if (workflowSequence.current === workflow) setExecution({ phase: "running", status });
      }
      if (workflowSequence.current !== workflow || executionSequence.current !== request) return;
      if (status.state !== "COMPLETED") {
        setExecution({ phase: "failed", detail: `Direct comparison ended in ${status.state}.`, status });
        return;
      }
      const recorded = await api.ownerPairComparisonEvidence(projectId, status.comparison_id);
      if (contextEpoch.current !== epoch || workflowSequence.current !== workflow || executionSequence.current !== request) return;
      setExecution({ phase: "idle" });
      setEvidence(recorded);
      onMessage(`Direct comparison ${shortIdentity(recorded.comparison_id)} completed with immutable Run evidence.`);
      onWorkflowTerminal(projectId);
    } catch (error) {
      if (contextEpoch.current === epoch && workflowSequence.current === workflow && executionSequence.current === request) {
        setExecution({ phase: "failed", detail: errorDetail(error) });
      }
    } finally {
      if (executionSequence.current === request) executionPending.current = false;
    }
  };

  const openHistory = async (status: DirectPairComparisonStatus) => {
    if (!projectId || status.state !== "COMPLETED" || configurationLocked) return;
    const requestedProjectId = projectId;
    const epoch = contextEpoch.current;
    const workflow = workflowSequence.current;
    const request = ++historyReadSequence.current;
    setEvidence(null);
    setExecution({ phase: "running", status });
    try {
      const recorded = await api.ownerPairComparisonEvidence(requestedProjectId, status.comparison_id);
      if (
        contextEpoch.current !== epoch
        || workflowSequence.current !== workflow
        || historyReadSequence.current !== request
        || activeProjectId.current !== requestedProjectId
      ) return;
      setEvidence(recorded);
      setExecution({ phase: "idle" });
    } catch (error) {
      if (
        contextEpoch.current === epoch
        && workflowSequence.current === workflow
        && historyReadSequence.current === request
        && activeProjectId.current === requestedProjectId
      ) {
        setExecution({ phase: "failed", detail: errorDetail(error), status });
      }
    }
  };

  const configurationLocked = execution.phase === "starting" || execution.phase === "running";
  const canFinalize = Boolean(projectId && sideUpload(sides.a) && sideUpload(sides.b) && relationship && !pairBusy && !configurationLocked);
  const canRun = Boolean(pair && matcherId && preprocessingId && !thresholdError && !configurationLocked);

  return (
    <section className="comparison-studio" data-testid="comparison-studio" aria-labelledby="comparison-studio-heading">
      <header className="comparison-studio-header">
        <div>
          <span className="comparison-kicker">Compare workspace</span>
          <h2 id="comparison-studio-heading">Inspect and compare</h2>
          <p>Record two images independently, assert only what you know, then execute one compatible matcher configuration.</p>
        </div>
        <nav className="comparison-modes" aria-label="Comparison workflow">
          <a className="comparison-mode" data-testid="comparison-mode-single" href="#/assess">Single image</a>
          <button type="button" className="comparison-mode" data-testid="comparison-mode-pair" aria-pressed={mode === "pair"} onClick={() => setMode("pair")}>Image pair</button>
          <button type="button" className="comparison-mode" data-testid="comparison-mode-dataset" aria-pressed={mode === "dataset"} onClick={() => setMode("dataset")}>Dataset study</button>
        </nav>
      </header>

      {mode === "dataset" ? datasetStudy : (
        <>
          {!projectId && (
            <div id="comparison-project-prerequisite" className="comparison-blocker" data-testid="comparison-project-prerequisite" role="alert">
              <strong>Open a project before recording biometric input.</strong>
              <a href="#/projects">Choose or create a project</a>
            </div>
          )}

          <div className="comparison-sides">
            <SideCard side="a" label="Image A · reference" state={sides.a} disabled={configurationLocked || !projectId} prerequisiteId={!projectId ? "comparison-project-prerequisite" : undefined} onUpload={(file) => void uploadSide("a", file)} />
            <SideCard side="b" label="Image B · probe" state={sides.b} disabled={configurationLocked || !projectId} prerequisiteId={!projectId ? "comparison-project-prerequisite" : undefined} onUpload={(file) => void uploadSide("b", file)} />
          </div>

          <section className="comparison-finalize-card" aria-labelledby="comparison-finalize-heading">
            <div className="comparison-section-heading">
              <span className="comparison-step">03</span>
              <div>
                <h3 id="comparison-finalize-heading">Finalize the pair record</h3>
                <p>Choose a closed relationship state. The assertion is recorded as operator-supplied and unverified.</p>
              </div>
            </div>
            <fieldset className="comparison-relationship">
              <legend>What relationship can you assert for these images?</legend>
              {(["SAME_PERSON", "DIFFERENT_PEOPLE", "NOT_RECORDED"] as OwnerPairRelationship[]).map((value) => (
                <label key={value}>
                  <input
                    type="radio"
                    name="comparison-relationship"
                    value={value}
                    checked={relationship === value}
                    disabled={configurationLocked}
                    onChange={() => chooseRelationship(value)}
                  />
                  <span>
                    <strong>{relationshipLabel(value)}</strong>
                    <small>{value === "NOT_RECORDED" ? "No agreement or correctness claim will be made" : "Operator assertion; not identity ground truth"}</small>
                  </span>
                </label>
              ))}
            </fieldset>
            <button type="button" className="primary comparison-action" data-testid="comparison-finalize" disabled={!canFinalize} onClick={() => void finalizePair()}>
              {pairBusy ? "Finalizing pair…" : "Finalize pair"}
            </button>
            {pairError && <p className="comparison-error" role="alert">{pairError}</p>}
            {pair && (
              <div
                className="comparison-pair-record"
                data-testid="comparison-pair-record"
                data-pair-id={pair.pair_id}
                data-relationship={pair.relationship}
              >
                <div>
                  <strong>Pair recorded</strong>
                  <span>Operator asserted · unverified</span>
                </div>
                <p>{relationshipLabel(pair.relationship)} · This is not ground truth and cannot support a population error claim.</p>
                <code>{pair.pair_id}</code>
              </div>
            )}
          </section>

          <section className="comparison-execute-card" aria-labelledby="comparison-execute-heading">
            <div className="comparison-section-heading">
              <span className="comparison-step">04</span>
              <div>
                <h3 id="comparison-execute-heading">Configure and run</h3>
                <p>Only installed executable matchers and compatible ready preprocessing profiles are offered.</p>
              </div>
            </div>
            {catalogError && <p className="comparison-error" role="alert">Add-on catalog unavailable: {catalogError}</p>}
            {catalogLoading && <p className="comparison-pending" role="status">Reading the admitted add-on catalog…</p>}
            {!catalogLoading && catalog && eligibleMatchers.length === 0 && (
              <div className="comparison-blocker" data-testid="comparison-no-matchers">
                <strong>No executable matcher is installed.</strong>
                <span>Install an admitted matcher under Manage system, then refresh these options.</span>
                <a href="#/system" data-testid="comparison-manage-addons">Manage matcher add-ons</a>
              </div>
            )}
            <button
              type="button"
              className="secondary comparison-refresh"
              data-testid="comparison-refresh-catalog"
              disabled={catalogLoading || configurationLocked}
              onClick={() => void loadCatalog()}
            >
              {catalogLoading ? "Refreshing options…" : "Refresh compatible options"}
            </button>
            <div className="comparison-configuration">
              <label>
                <span>Matcher</span>
                <select data-testid="comparison-matcher" value={matcherId} onChange={(event) => setMatcherId(event.target.value)} disabled={configurationLocked || !catalog || eligibleMatchers.length === 0}>
                  {eligibleMatchers.map((matcher) => <option key={matcher.matcher_id} value={matcher.matcher_id}>{matcher.name}</option>)}
                </select>
                <small>{eligibleMatchers.length} executable from the live catalog</small>
              </label>
              <label>
                <span>Preprocessing</span>
                <select data-testid="comparison-preprocessing" value={preprocessingId} onChange={(event) => setPreprocessingId(event.target.value)} disabled={configurationLocked || !matcherId || eligibleProfiles.length === 0}>
                  {eligibleProfiles.map((profile) => <option key={profile.addon_id} value={profile.addon_id}>{profile.display_name}</option>)}
                </select>
                <small>Ready and compatible with the selected matcher</small>
              </label>
              <label>
                <span>Fixed decision threshold</span>
                <input data-testid="comparison-threshold" type="number" step="any" value={threshold} disabled={configurationLocked} aria-invalid={thresholdError ? "true" : "false"} onChange={(event) => setThreshold(event.target.value)} />
                <small>{thresholdError ?? "Match when score is greater than or equal to this value"}</small>
              </label>
            </div>
            <button type="button" className="primary comparison-action" data-testid="comparison-run" disabled={!canRun} onClick={() => void executeComparison()}>
              {execution.phase === "starting" ? "Starting comparison…" : execution.phase === "running" ? `Running · ${execution.status.state.toLowerCase()}` : "Run direct comparison"}
            </button>
            {execution.phase === "starting" && <p className="comparison-pending" role="status">Submitting the immutable direct-comparison request…</p>}
            {execution.phase === "running" && <p className="comparison-pending" role="status">Recorded operation {shortIdentity(execution.status.comparison_id)} · {execution.status.state}</p>}
            {execution.phase === "failed" && (
              <div className="direct-pair-evidence comparison-error" data-testid="direct-pair-evidence" data-state="failed" data-comparison-id={execution.status?.comparison_id ?? ""} role="alert">
                {execution.detail}
              </div>
            )}
          </section>

          {evidence && <DirectPairEvidence evidence={evidence} onOpenEvidence={onOpenEvidence} />}

          <section className="comparison-history" data-testid="comparison-history" aria-labelledby="comparison-history-heading">
            <h3 id="comparison-history-heading">Comparison history</h3>
            {history.phase === "loading" && <p className="comparison-pending" data-testid="comparison-history-loading" role="status">Loading recorded comparisons…</p>}
            {history.phase === "error" && (
              <div className="comparison-error" data-testid="comparison-history-error" role="alert">
                <p>Recorded comparison history is unavailable: {history.detail}</p>
                <button type="button" className="secondary" data-testid="comparison-history-retry" onClick={() => void loadHistory()}>Retry comparison history</button>
              </div>
            )}
            {history.phase === "ready" && history.records.length === 0 && <p className="empty">No direct comparisons are recorded for this project.</p>}
            {history.phase === "ready" && history.records.length > 0 && (
              <div>
                {history.records.map((status) => (
                  <button
                    type="button"
                    key={status.comparison_id}
                    data-testid="comparison-history-item"
                    data-comparison-id={status.comparison_id}
                    disabled={status.state !== "COMPLETED" || configurationLocked}
                    onClick={() => void openHistory(status)}
                  >
                    <span>{shortIdentity(status.comparison_id)}</span>
                    <strong>{status.state}</strong>
                  </button>
                ))}
              </div>
            )}
          </section>
        </>
      )}
    </section>
  );
}

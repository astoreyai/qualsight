import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  api,
  ApiError,
  type AssessResponse,
  type AssessmentFailure,
  type DatasetOut,
  type HealthOut,
  type ProjectOut,
  type ProjectPurgeOut,
  type RegisteredSample,
  type RunSummaryOut,
  type UploadAssessOut,
} from "./api";
import { Workbench } from "./Workbench";
import { Matchers, type MatcherHeaderContext } from "./Matchers";
import { Utility, type UtilityHeaderContext } from "./Utility";
import { CrossMatcher } from "./CrossMatcher";
import { Reports } from "./Reports";
import { Advanced } from "./Advanced";
import { Nav } from "./Nav";
import { SystemPanel } from "./SystemPanel";
import { Home } from "./Home";
import { QualityResults } from "./QualityResults";
import { ComparisonStudio } from "./ComparisonStudio";
import { ProjectPurge } from "./ProjectPurge";
import { StressStudio, type StressSource } from "./StressStudio";
import { EvidenceDetail, type EvidenceRequest } from "./EvidenceDetail";

type AssessState =
  | { phase: "idle" }
  | { phase: "running"; name: string }
  | { phase: "done"; result: AssessResponse; projectId: string | null }
  | { phase: "failed"; failure: AssessmentFailure }
  | { phase: "error"; status: number; detail: string };

type UiMessage = {
  kind: "success" | "progress" | "warning" | "error";
  text: string;
};

type WorkingSample = Omit<RegisteredSample, "registered_sample_id"> & {
  registered_sample_id?: string;
  recorded_sample_id?: string;
  owner_project_id?: string;
  image_url?: string;
};

function messageFromLegacyCallback(text: string): UiMessage {
  if (/\b(running|started)\b/i.test(text)) return { kind: "progress", text };
  if (/\b(recorded|imported|frozen|saved|completed|created|cancelled)\b/i.test(text)) return { kind: "success", text };
  if (/^enter\b/i.test(text)) return { kind: "warning", text };
  return { kind: "error", text };
}

function shortHash(value: string, keep = 12): string {
  const marker = value.indexOf("sha256:");
  const digest = marker >= 0 ? value.slice(marker + 7) : value;
  return `${digest.slice(0, keep)}…`;
}

function workingSampleKey(sample: WorkingSample | null): string {
  if (!sample) return "";
  return `${sample.registered_sample_id ?? sample.recorded_sample_id ?? sample.resource_id}\u001f${sample.sha256}`;
}

const APP_VIEWS = new Set(["home", "projects", "data", "assess", "matchers", "advanced", "utility", "cross", "reports", "system"]);

function routedView(hash: string): string | null {
  if (!hash.startsWith("#/")) return null;
  const candidate = hash.slice(2);
  return APP_VIEWS.has(candidate) ? candidate : "home";
}

export function App() {
  const [health, setHealth] = useState<HealthOut | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [samples, setSamples] = useState<RegisteredSample[]>([]);
  const [samplesError, setSamplesError] = useState<string | null>(null);
  const [samplesLoading, setSamplesLoading] = useState(true);
  const [selected, setSelected] = useState<WorkingSample | null>(null);
  const [scope, setScope] = useState<string>("");
  const [assess, setAssess] = useState<AssessState>({ phase: "idle" });
  const [runs, setRuns] = useState<RunSummaryOut[]>([]);
  const [runsProjectId, setRunsProjectId] = useState<string | null>(null);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [runsLoading, setRunsLoading] = useState(true);
  const [projects, setProjects] = useState<ProjectOut[]>([]);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [activeProjectId, setActiveProjectId] = useState<string | null>(() => window.localStorage.getItem("qualsight.activeProjectId"));
  useEffect(() => {
    if (activeProjectId) window.localStorage.setItem("qualsight.activeProjectId", activeProjectId);
    else window.localStorage.removeItem("qualsight.activeProjectId");
  }, [activeProjectId]);
  const [view, setView] = useState<string>(() => routedView(window.location.hash) ?? "home");
  useEffect(() => {
    const onHash = () => {
      const nextView = routedView(window.location.hash);
      if (nextView !== null) setView(nextView);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const viewClass = (name: string) => (view === name ? "view" : "view hidden-view");
  const [detection, setDetection] = useState<
    | { phase: "hidden" }
    | { phase: "loading" }
    | { phase: "shown"; payload: { label: string; image: { width: number; height: number }; chosen_index: number | null; detections: Array<{ bbox: number[]; score: number; landmarks: number[][] }> } }
    | { phase: "error"; detail: string }
  >({ phase: "hidden" });
  const [ofiqLandmarks, setOfiqLandmarks] = useState<
    | { phase: "hidden" }
    | { phase: "loading" }
    | { phase: "shown"; payload: { label: string; image: { width: number; height: number }; landmark_type: string; faces: Array<{ x: number; y: number; width: number; height: number }>; landmarks: number[][] } }
    | { phase: "error"; detail: string }
  >({ phase: "hidden" });
  const [upload, setUpload] = useState<
    | { phase: "idle" }
    | { phase: "running"; name: string }
    | { phase: "done"; result: UploadAssessOut; sample: WorkingSample }
    | { phase: "error"; detail: string }
  >({ phase: "idle" });
  const [stressOpen, setStressOpen] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [projectMessage, setProjectMessage] = useState<UiMessage | null>(null);
  const [confirmArchive, setConfirmArchive] = useState(false);
  const [purgeReceipt, setPurgeReceipt] = useState<ProjectPurgeOut | null>(null);
  const [activeDataset, setActiveDataset] = useState<DatasetOut | null>(null);
  const [matcherHeaderContext, setMatcherHeaderContext] = useState<MatcherHeaderContext | null>(null);
  const [utilityHeaderContext, setUtilityHeaderContext] = useState<UtilityHeaderContext | null>(null);
  const [evidenceRequest, setEvidenceRequest] = useState<EvidenceRequest | null>(null);
  const [evidenceEmptyDetail, setEvidenceEmptyDetail] = useState<string | undefined>(undefined);
  const runsRefreshSequence = useRef(0);
  const projectsRefreshSequence = useRef(0);
  const samplesRefreshSequence = useRef(0);
  const assessmentContextEpoch = useRef(0);
  const assessmentSequence = useRef(0);
  const uploadContextEpoch = useRef(0);
  const uploadSequence = useRef(0);
  const detectionSequence = useRef(0);
  const ofiqLandmarksSequence = useRef(0);
  const uploadInput = useRef<HTMLInputElement>(null);
  const activeProjectIdRef = useRef(activeProjectId);
  const evidenceProjectIdRef = useRef(activeProjectId);
  const evidenceRequestRef = useRef(evidenceRequest);
  const suppressDefaultSample = useRef(false);
  activeProjectIdRef.current = activeProjectId;
  evidenceRequestRef.current = evidenceRequest;
  const selectedSampleKeyRef = useRef("");
  const assessmentContextKey = `${workingSampleKey(selected)}\u001f${scope}\u001f${activeProjectId ?? ""}`;
  const assessmentContextKeyRef = useRef(assessmentContextKey);
  assessmentContextKeyRef.current = assessmentContextKey;
  const ownerUploadScope = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1";
  const uploadContextKey = `${activeProjectId ?? ""}\u001f${ownerUploadScope}`;
  const uploadContextKeyRef = useRef(uploadContextKey);
  uploadContextKeyRef.current = uploadContextKey;

  const refreshRuns = useCallback(async () => {
    const sequence = ++runsRefreshSequence.current;
    if (!activeProjectId) {
      setRuns([]);
      setRunsProjectId(null);
      setRunsError(null);
      setRunsLoading(false);
      return;
    }
    setRuns([]);
    setRunsProjectId(activeProjectId);
    setRunsLoading(true);
    setRunsError(null);
    try {
      const nextRuns = await api.runs(activeProjectId);
      if (runsRefreshSequence.current !== sequence) return;
      setRuns(nextRuns);
    } catch (error) {
      if (runsRefreshSequence.current !== sequence) return;
      setRunsError(`Recorded Runs are unavailable: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      if (runsRefreshSequence.current === sequence) setRunsLoading(false);
    }
  }, [activeProjectId]);
  const refreshProjects = useCallback(async () => {
    const sequence = ++projectsRefreshSequence.current;
    setProjectsLoading(true);
    setProjectsError(null);
    try {
      const nextProjects = await api.projects();
      if (projectsRefreshSequence.current !== sequence) return;
      setProjects(nextProjects);
    } catch (error) {
      if (projectsRefreshSequence.current !== sequence) return;
      setProjectsError(`Projects are unavailable: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      if (projectsRefreshSequence.current === sequence) setProjectsLoading(false);
    }
  }, []);
  const selectWorkingSample = useCallback((sample: WorkingSample | null, preserveApprovedScope = false) => {
    if (sample) suppressDefaultSample.current = false;
    const nextKey = workingSampleKey(sample);
    if (selectedSampleKeyRef.current !== nextKey) {
      selectedSampleKeyRef.current = nextKey;
      assessmentSequence.current += 1;
      detectionSequence.current += 1;
      ofiqLandmarksSequence.current += 1;
      setAssess({ phase: "idle" });
      setDetection({ phase: "hidden" });
      setOfiqLandmarks({ phase: "hidden" });
    }
    setSelected(sample);
    setScope((current) => {
      if (sample && preserveApprovedScope && sample.approved_scopes.includes(current)) return current;
      return sample?.approved_scopes[0] ?? "";
    });
  }, []);

  const refreshSamples = useCallback(async () => {
    const sequence = ++samplesRefreshSequence.current;
    setSamplesLoading(true);
    setSamplesError(null);
    try {
      const items = await api.registeredSamples();
      if (samplesRefreshSequence.current !== sequence) return;
      const displayable = items.filter((item) => item.media_type.startsWith("image/"));
      setSamples(displayable);
      const selectedId = selectedSampleKeyRef.current.split("\u001f", 1)[0] ?? "";
      const nextSelected = displayable.find((item) => item.registered_sample_id === selectedId)
        ?? (suppressDefaultSample.current ? null : displayable[0] ?? null);
      selectWorkingSample(nextSelected, true);
    } catch (error) {
      if (samplesRefreshSequence.current !== sequence) return;
      setSamplesError(`Registered samples are unavailable: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      if (samplesRefreshSequence.current === sequence) setSamplesLoading(false);
    }
  }, [selectWorkingSample]);
  const onWorkflowMessage = useCallback((text: string) => {
    setProjectMessage(messageFromLegacyCallback(text));
  }, []);
  const onWorkflowTerminal = useCallback((workflowProjectId?: string) => {
    if (workflowProjectId && activeProjectIdRef.current !== workflowProjectId) return;
    void refreshRuns();
    void refreshProjects();
  }, [refreshProjects, refreshRuns]);
  const onMatcherContextChange = useCallback((context: MatcherHeaderContext | null) => {
    setMatcherHeaderContext(context);
  }, []);
  const onUtilityContextChange = useCallback((context: UtilityHeaderContext | null) => {
    setUtilityHeaderContext(context);
  }, []);
  const onOpenEvidence = useCallback((request: EvidenceRequest) => {
    const currentProjectId = activeProjectIdRef.current;
    if (!currentProjectId || request.projectId !== currentProjectId) {
      setEvidenceRequest(null);
      setEvidenceEmptyDetail("Evidence was not opened because it is not bound to the active project.");
      return;
    }
    setEvidenceEmptyDetail(undefined);
    setEvidenceRequest(request);
  }, []);
  const activeProject = useMemo(
    () => projects.find((p) => p.project_id === activeProjectId) ?? null,
    [projects, activeProjectId],
  );
  const runsMatchActiveProject = activeProjectId !== null && runsProjectId === activeProjectId;
  const latestProjectRun = useMemo(() => {
    if (!runsMatchActiveProject || runs.length === 0) return null;
    return [...runs].sort((left, right) => {
      const timeDifference = Date.parse(right.start_time) - Date.parse(left.start_time);
      return timeDifference !== 0 ? timeDifference : right.run_id.localeCompare(left.run_id);
    })[0] ?? null;
  }, [runs, runsMatchActiveProject]);

  useLayoutEffect(() => {
    assessmentContextKeyRef.current = assessmentContextKey;
    assessmentContextEpoch.current += 1;
    assessmentSequence.current += 1;
    setAssess({ phase: "idle" });
  }, [assessmentContextKey]);

  useLayoutEffect(() => {
    setStressOpen(false);
  }, [selected?.registered_sample_id, selected?.recorded_sample_id, selected?.sha256]);

  useLayoutEffect(() => {
    uploadContextKeyRef.current = uploadContextKey;
    uploadContextEpoch.current += 1;
    uploadSequence.current += 1;
    setUpload({ phase: "idle" });
    setStressOpen(false);
  }, [uploadContextKey]);

  useEffect(() => {
    if (selected?.owner_project_id && selected.owner_project_id !== activeProjectId) {
      selectWorkingSample(null, true);
    }
  }, [activeProjectId, selected, selectWorkingSample]);

  const onActiveDataset = useCallback((dataset: DatasetOut | null) => {
    if (dataset && dataset.project_id !== activeProjectIdRef.current) return;
    setActiveDataset(dataset);
  }, []);

  useLayoutEffect(() => {
    setMatcherHeaderContext(null);
  }, [activeProjectId, activeDataset?.dataset_id]);

  useLayoutEffect(() => {
    if (evidenceProjectIdRef.current !== activeProjectId) {
      setEvidenceRequest(null);
      setEvidenceEmptyDetail(evidenceRequestRef.current ? "Evidence selection was cleared because the active project changed." : undefined);
      evidenceProjectIdRef.current = activeProjectId;
    }
  }, [activeProjectId]);

  const createProject = useCallback(async () => {
    if (!newProjectName.trim() || !scope) return;
    try {
      const created = await api.createProject(
        newProjectName.trim(),
        "Created from the QualSight console.",
        scope,
      );
      setNewProjectName("");
      setActiveProjectId(created.project_id);
      setProjectMessage({ kind: "success", text: `Created ${created.name} (v${created.version}, ${created.lifecycle})` });
      refreshProjects();
    } catch (error) {
      setProjectMessage({ kind: "error", text: error instanceof Error ? error.message : String(error) });
    }
  }, [newProjectName, scope, refreshProjects]);

  const projectAction = useCallback(
    async (action: "open" | "save" | "close" | "archive" | "clone") => {
      if (!activeProjectId) return;
      try {
        const updated =
          action === "clone"
            ? await api.deriveProject(activeProjectId, "clone", `${activeProject?.name ?? "project"} (clone)`)
            : await api.projectAction(activeProjectId, action);
        setProjectMessage({ kind: "success", text: `${action}: ${updated.name} is now v${updated.version} ${updated.lifecycle}` });
        if (action === "clone") setActiveProjectId(updated.project_id);
        if (action === "archive") setConfirmArchive(false);
        refreshProjects();
      } catch (error) {
        setProjectMessage({ kind: "error", text: error instanceof Error ? error.message : String(error) });
      }
    },
    [activeProjectId, activeProject, refreshProjects],
  );

  const onProjectPurged = useCallback((record: ProjectPurgeOut) => {
    if (!record.verified || activeProjectIdRef.current !== record.project_id) return;
    activeProjectIdRef.current = null;
    evidenceProjectIdRef.current = null;
    runsRefreshSequence.current += 1;
    assessmentContextEpoch.current += 1;
    assessmentSequence.current += 1;
    uploadContextEpoch.current += 1;
    uploadSequence.current += 1;
    detectionSequence.current += 1;
    ofiqLandmarksSequence.current += 1;
    selectedSampleKeyRef.current = "";
    suppressDefaultSample.current = true;
    setActiveProjectId(null);
    setActiveDataset(null);
    setSelected(null);
    setScope("");
    setAssess({ phase: "idle" });
    setUpload({ phase: "idle" });
    setStressOpen(false);
    setDetection({ phase: "hidden" });
    setOfiqLandmarks({ phase: "hidden" });
    setRuns([]);
    setRunsProjectId(null);
    setRunsError(null);
    setMatcherHeaderContext(null);
    setEvidenceRequest(null);
    setEvidenceEmptyDetail("Evidence selection was cleared because its project was purged.");
    setConfirmArchive(false);
    setPurgeReceipt(record);
    setProjectMessage({
      kind: "success",
      text: `Verified local purge ${shortHash(record.purge_id, 16)}. The project is no longer active.`,
    });
    void refreshProjects();
  }, [refreshProjects]);

  useEffect(() => {
    api
      .health()
      .then(setHealth)
      .catch((error: unknown) => setHealthError(error instanceof Error ? error.message : String(error)));
    void refreshSamples();
    void refreshRuns();
    void refreshProjects();
  }, [refreshRuns, refreshProjects, refreshSamples]);

  const onSystemRecovered = useCallback((recoveredHealth: HealthOut) => {
    setHealth(recoveredHealth);
    setHealthError(null);
  }, []);

  const runAssessment = useCallback(async () => {
    if (!selected?.registered_sample_id || !scope) return;
    const epoch = assessmentContextEpoch.current;
    const sequence = ++assessmentSequence.current;
    const requestKey = assessmentContextKey;
    setAssess({ phase: "running", name: selected.display_name });
    try {
      const result = await api.assess(selected.registered_sample_id, scope, activeProjectId);
      if (assessmentContextEpoch.current !== epoch || assessmentSequence.current !== sequence || assessmentContextKeyRef.current !== requestKey) return;
      setAssess({ phase: "done", result, projectId: activeProjectId });
      void refreshRuns();
      void refreshProjects();
    } catch (error) {
      if (assessmentContextEpoch.current !== epoch || assessmentSequence.current !== sequence || assessmentContextKeyRef.current !== requestKey) return;
      if (error instanceof ApiError && error.failure) {
        setAssess({ phase: "failed", failure: error.failure });
      } else if (error instanceof ApiError) {
        setAssess({ phase: "error", status: error.status, detail: error.detail });
      } else {
        setAssess({ phase: "error", status: 0, detail: error instanceof Error ? error.message : String(error) });
      }
    }
  }, [selected, scope, activeProjectId, assessmentContextKey, refreshRuns, refreshProjects]);

  const runOwnerUpload = useCallback(async (file: File) => {
    if (!activeProjectId) return;
    const epoch = uploadContextEpoch.current;
    const sequence = ++uploadSequence.current;
    const requestKey = uploadContextKey;
    setStressOpen(false);
    setUpload({ phase: "running", name: file.name });
    try {
      const result = await api.assessUpload(activeProjectId, ownerUploadScope, file);
      if (uploadContextEpoch.current !== epoch || uploadSequence.current !== sequence || uploadContextKeyRef.current !== requestKey) return;
      const sample: WorkingSample = {
        resource_id: result.sample_id,
        role: result.source_kind,
        approved_scopes: [ownerUploadScope],
        sha256: result.image_sha256,
        size_bytes: file.size,
        media_type: file.type,
        display_name: result.original_name,
        recorded_sample_id: result.sample_id,
        owner_project_id: activeProjectId,
        image_url: result.provenance.sample_image,
      };
      selectWorkingSample(sample, true);
      setUpload({ phase: "done", result, sample });
      void refreshRuns();
      void refreshProjects();
    } catch (error) {
      if (uploadContextEpoch.current !== epoch || uploadSequence.current !== sequence || uploadContextKeyRef.current !== requestKey) return;
      setUpload({
        phase: "error",
        detail: error instanceof ApiError ? `${error.status}: ${error.detail}` : String(error),
      });
    }
  }, [activeProjectId, ownerUploadScope, uploadContextKey, refreshRuns, refreshProjects, selectWorkingSample]);

  const toggleDetection = useCallback(async () => {
    if (!selected) return;
    const sequence = ++detectionSequence.current;
    const requestKey = workingSampleKey(selected);
    if (detection.phase === "shown") {
      setDetection({ phase: "hidden" });
      return;
    }
    setDetection({ phase: "loading" });
    try {
      const payload = selected.owner_project_id && selected.recorded_sample_id
        ? await api.detectionForSample(selected.owner_project_id, selected.recorded_sample_id)
        : selected.registered_sample_id
          ? await api.registeredSampleDetection(selected.registered_sample_id)
          : (() => { throw new Error("Working sample has no opaque image identity."); })();
      if (detectionSequence.current !== sequence || selectedSampleKeyRef.current !== requestKey) return;
      setDetection({ phase: "shown", payload });
    } catch (error) {
      if (detectionSequence.current !== sequence || selectedSampleKeyRef.current !== requestKey) return;
      setDetection({ phase: "error", detail: error instanceof Error ? error.message : String(error) });
    }
  }, [selected, detection.phase]);

  const toggleOfiqLandmarks = useCallback(async () => {
    if (!selected) return;
    const sequence = ++ofiqLandmarksSequence.current;
    const requestKey = workingSampleKey(selected);
    if (ofiqLandmarks.phase === "shown") {
      setOfiqLandmarks({ phase: "hidden" });
      return;
    }
    setOfiqLandmarks({ phase: "loading" });
    try {
      const payload = selected.owner_project_id && selected.recorded_sample_id
        ? await api.ofiqLandmarksForSample(selected.owner_project_id, selected.recorded_sample_id)
        : selected.registered_sample_id
          ? await api.registeredSampleOfiqLandmarks(selected.registered_sample_id)
          : (() => { throw new Error("Working sample has no opaque image identity."); })();
      if (ofiqLandmarksSequence.current !== sequence || selectedSampleKeyRef.current !== requestKey) return;
      setOfiqLandmarks({ phase: "shown", payload });
    } catch (error) {
      if (ofiqLandmarksSequence.current !== sequence || selectedSampleKeyRef.current !== requestKey) return;
      setOfiqLandmarks({ phase: "error", detail: error instanceof Error ? error.message : String(error) });
    }
  }, [selected, ofiqLandmarks.phase]);

  const selectedUpload =
    upload.phase === "done"
      && upload.sample.owner_project_id === activeProjectId
      && workingSampleKey(upload.sample) === workingSampleKey(selected)
      ? upload
      : null;
  const stressSource: StressSource | null = selectedUpload
    ? {
        datasetId: selectedUpload.result.dataset_id,
        sampleId: selectedUpload.result.sample_id,
        displayName: selectedUpload.result.original_name,
        sha256: selectedUpload.result.image_sha256,
        imageUrl: selectedUpload.result.provenance.sample_image,
        measures: selectedUpload.result.measures,
      }
    : null;
  const recordedQuality:
    | { kind: "single-assessment"; result: AssessResponse }
    | { kind: "owner-upload"; result: UploadAssessOut }
    | null = assess.phase === "done" && assess.projectId === activeProjectId
      ? { kind: "single-assessment", result: assess.result }
      : selectedUpload
        ? { kind: "owner-upload", result: selectedUpload.result }
        : null;
  const contextSample = selected && (!selected.owner_project_id || selected.owner_project_id === activeProjectId)
    ? selected
    : null;
  const currentMatcherContext = matcherHeaderContext
    && matcherHeaderContext.projectId === activeProjectId
    && matcherHeaderContext.datasetId === activeDataset?.dataset_id
    ? matcherHeaderContext
    : null;
  const currentUtilityContext = utilityHeaderContext
    && utilityHeaderContext.projectId === activeProjectId
    && (!utilityHeaderContext.datasetId || utilityHeaderContext.datasetId === activeDataset?.dataset_id)
    ? utilityHeaderContext
    : null;
  const currentWorkflowContext = view === "matchers"
    ? currentMatcherContext
    : view === "utility"
      ? currentUtilityContext
      : null;
  const currentRunId = currentWorkflowContext?.runId
    ? currentWorkflowContext.runId
    : view === "assess" && recordedQuality?.result.run_id
      ? recordedQuality.result.run_id
      : latestProjectRun?.run_id ?? recordedQuality?.result.run_id ?? null;
  const projectContextValue = activeProject
    ? activeProject.name
    : activeProjectId && projectsLoading
      ? "Loading selected project…"
      : activeProjectId
        ? "Selected project unavailable"
        : "No project selected";
  const sampleContextValue = contextSample
    ? contextSample.display_name
    : samplesLoading
      ? "Loading working sample…"
      : "No working sample selected";
  return (
    <div className="shell">
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <div>
            <h1>QualSight</h1>
            <p className="subtitle">Face image quality → matcher utility workbench · beta</p>
          </div>
        </div>
        <div className="engine-badge" data-testid="engine-badge">
          {health ? (
            <>
              <span className="label">Engine</span>
              <span className="value">
                {health.engine.name} · v{health.engine.version} · {health.engine.state}
              </span>
              <span className="label">Registry</span>
              <span className="value mono">{health.registry_id} · {shortHash(health.registry_sha256)}</span>
            </>
          ) : (
            <span className="value">{healthError ? `Control plane unavailable: ${healthError}` : "Connecting to control plane…"}</span>
          )}
        </div>
      </header>
      <Nav current={view} />
      <aside className="context-bar" aria-label="Active experiment context" data-testid="experiment-context">
        <div className="context-item" data-context-key="project" data-project-id={activeProject?.project_id ?? undefined}>
          <span className="context-label">Project</span>
          <strong>{projectContextValue}</strong>
          <span className="context-meta">{activeProject ? `${activeProject.lifecycle} · v${activeProject.version}` : "Choose a project to record work"}</span>
        </div>
        <div className="context-item" data-context-key="dataset" data-dataset-id={activeDataset?.dataset_id ?? undefined}>
          <span className="context-label">Dataset</span>
          <strong>{activeDataset?.name ?? "No dataset selected"}</strong>
          <span className="context-meta">{activeDataset ? `${activeDataset.sample_count.toLocaleString()} samples · ${activeDataset.subject_count?.toLocaleString() ?? "unlabelled"} subjects` : "Choose or import an input collection"}</span>
        </div>
        <div className="context-item" data-context-key="sample" data-sample-id={contextSample?.recorded_sample_id ?? contextSample?.resource_id ?? undefined}>
          <span className="context-label">Working sample</span>
          <strong>{sampleContextValue}</strong>
          <span className="context-meta">{contextSample ? (contextSample.owner_project_id ? "Owner-supplied local input" : "Registered workstation input") : "Choose an image in Inspect quality"}</span>
        </div>
        <div className="context-item" data-context-key="protocol" data-protocol-id={currentWorkflowContext?.protocolId ?? undefined}>
          <span className="context-label">Protocol</span>
          <strong>{currentWorkflowContext?.protocolLabel ?? "No protocol selected"}</strong>
          <span className="context-meta">{currentWorkflowContext?.protocolId ? "Immutable comparison protocol" : "Freeze or select a comparison protocol"}</span>
        </div>
        <div className="context-item" data-context-key="matcher" data-leaf-id={currentWorkflowContext?.leafId ?? undefined}>
          <span className="context-label">Matcher leaf</span>
          <strong>{currentWorkflowContext?.leafLabel ?? "No matcher leaf selected"}</strong>
          <span className="context-meta">{currentWorkflowContext?.leafId ? "Selected matcher × preprocessing result" : "Select a recorded matcher result"}</span>
        </div>
        <div className="context-item" data-context-key="evidence" data-run-id={currentRunId ?? undefined}>
          <span className="context-label">Evidence / Run</span>
          <strong>{currentRunId ? `Run ${shortHash(currentRunId, 12)}` : "No active Run"}</strong>
          <span className="context-meta">{
            currentRunId && currentRunId === currentWorkflowContext?.runId
              ? view === "utility" ? "Selected Utility analysis evidence" : "Matcher comparison evidence"
              : currentRunId && currentRunId === recordedQuality?.result.run_id
                ? "Quality assessment evidence"
                : currentRunId
                  ? "Latest recorded project workflow"
                  : "Complete an assessment or comparison"
          }</span>
        </div>
        <div className="context-actions">
          {currentRunId
            ? <a className="button secondary" href={activeDataset ? "#/reports" : "#/projects"}>Review Run context</a>
            : !activeProject
              ? <a className="button secondary" href="#/projects">Select or create a project</a>
              : !activeDataset
                ? <a className="button secondary" href="#/data">Select or import a dataset</a>
                : <a className="button secondary" href="#/matchers">Continue comparison</a>}
        </div>
      </aside>
      {projectMessage && (
        <div className={`global-message ${projectMessage.kind}`} role={projectMessage.kind === "error" ? "alert" : "status"} data-testid="project-message">
          <span>{projectMessage.text}</span>
          <button type="button" className="message-dismiss" onClick={() => setProjectMessage(null)}>Dismiss</button>
        </div>
      )}

      <main className="layout" id="main-content" tabIndex={-1}>
        <div className={viewClass("home")} data-view="home">
          <Home activeProjectId={activeProjectId} hasWorkingSample={stressSource !== null} />
        </div>
        <div className={viewClass("projects")} data-view="projects">
        <section className="panel projects" aria-labelledby="projects-heading" data-testid="projects-panel">
          <h2 id="projects-heading">Projects</h2>
          {projectsLoading && <p className="status" role="status">Loading projects…</p>}
          {projectsError && <p className="error" role="alert">{projectsError} <button type="button" className="secondary" onClick={() => void refreshProjects()}>Retry</button></p>}
          <div className="project-create">
            <input
              type="text"
              placeholder="New project name"
              value={newProjectName}
              onChange={(event) => setNewProjectName(event.target.value)}
              data-testid="project-name-input"
              aria-label="New project name"
              aria-describedby="project-create-help"
            />
            <button
              type="button"
              className="secondary"
              data-testid="project-create"
              onClick={() => void createProject()}
              disabled={!newProjectName.trim() || !scope}
              aria-describedby="project-create-help"
              title={!newProjectName.trim() ? "Enter a project name." : !scope ? "Select an approved purpose scope from a registered sample." : "Create the project."}
            >
              New
            </button>
          </div>
          <p id="project-create-help" className="hint">
            {!newProjectName.trim() ? "Enter a project name to enable creation." : !scope ? "Select an approved purpose scope from a registered sample to enable creation." : `Ready to create under purpose scope ${scope}.`}
          </p>
          <label htmlFor="active-project" className="hint">
            Active project (assessments are recorded into it)
          </label>
          <select
            id="active-project"
            data-testid="project-select"
            value={activeProjectId ?? ""}
            onChange={(event) => {
              setActiveProjectId(event.target.value || null);
              setConfirmArchive(false);
            }}
          >
            <option value="">— none (content-addressed single-sample project) —</option>
            {projects.map((project) => (
              <option key={project.project_id} value={project.project_id}>
                {project.name} · v{project.version} · {project.lifecycle} · {project.run_ids.length} run(s)
              </option>
            ))}
          </select>
          {activeProject && (
            <div className="project-actions" data-testid="project-actions">
              <button type="button" className="secondary" onClick={() => void projectAction("open")}>Open</button>
              <button type="button" className="secondary" onClick={() => void projectAction("save")}>Save</button>
              <button type="button" className="secondary" onClick={() => void projectAction("close")}>Close</button>
              <button type="button" className="secondary" onClick={() => void projectAction("clone")}>Clone</button>
              <button type="button" className="secondary danger" aria-describedby="archive-consequence" onClick={() => setConfirmArchive(true)}>Archive</button>
              <ProjectPurge project={activeProject} onPurged={onProjectPurged} />
            </div>
          )}
          {activeProject && (
            <p className="hint" id="archive-consequence">Archive stops new work in this project until it is opened again. Recorded Runs remain retained.</p>
          )}
          {activeProject && confirmArchive && (
            <div className="confirmation" role="group" aria-labelledby="archive-confirmation-title" onKeyDown={(event) => { if (event.key === "Escape") setConfirmArchive(false); }}>
              <strong id="archive-confirmation-title">Archive {activeProject.name}?</strong>
              <p>This changes {activeProject.name} from {activeProject.lifecycle} to ARCHIVED. You can restore it with Open; recorded evidence is not deleted.</p>
              <div className="project-actions">
                <button type="button" className="secondary danger" onClick={() => void projectAction("archive")}>Confirm archive</button>
                <button type="button" className="secondary" onClick={() => setConfirmArchive(false)}>Cancel</button>
              </div>
            </div>
          )}
          {activeProject && (
            <dl className="project-facts" data-testid="project-facts">
              <dt>Lifecycle</dt>
              <dd>{activeProject.lifecycle} · v{activeProject.version}</dd>
              <dt>Runs</dt>
              <dd>{activeProject.run_ids.length}</dd>
              <dt>Engine</dt>
              <dd className="mono">
                {String((activeProject.scientific_configuration as { quality_engine?: { resource_id?: string; version?: string } }).quality_engine?.resource_id ?? "")}
                {" v"}
                {String((activeProject.scientific_configuration as { quality_engine?: { version?: string } }).quality_engine?.version ?? "")}
              </dd>
              <dt>Evidence</dt>
              <dd className="mono">{shortHash(activeProject.evidence_sha256, 16)}</dd>
            </dl>
          )}
          {purgeReceipt && (
            <section
              className="project-purge-result"
              data-testid="project-purge-result"
              data-verified={String(purgeReceipt.verified)}
              aria-labelledby="project-purge-result-title"
            >
              <h3 id="project-purge-result-title">Verified local purge</h3>
              <p>
                QualSight recorded {purgeReceipt.outcome_counts.deleted} completed deletion(s), {purgeReceipt.outcome_counts.reconciled_after_deletion_intent} crash-reconciled post-intent absence(s), and verified the project metadata purge.
              </p>
              <dl className="facts">
                <dt>Purge evidence</dt>
                <dd className="mono">{shortHash(purgeReceipt.purge_id, 20)}</dd>
                <dt>Retained</dt>
                <dd>{purgeReceipt.retained.join("; ")}</dd>
                <dt>Not performed</dt>
                <dd>{purgeReceipt.not_performed.join("; ")}</dd>
              </dl>
            </section>
          )}
        </section>

        <section className="panel runs" aria-labelledby="runs-heading">
          <h2 id="runs-heading">Recorded runs</h2>
          {!activeProject && <p className="empty">Select or create a project to inspect its immutable Runs.</p>}
          {activeProject && (runsLoading || !runsMatchActiveProject) && <p className="status" role="status">Loading recorded Runs for {activeProject.name}…</p>}
          {activeProject && runsError && <p className="error" role="alert">{runsError} <button type="button" className="secondary" onClick={() => void refreshRuns()}>Retry</button></p>}
          {activeProject && runsMatchActiveProject && !runsLoading && !runsError && runs.length === 0 ? (
            <p className="empty">No immutable Runs are recorded for {activeProject.name}.</p>
          ) : activeProject && runsMatchActiveProject && !runsLoading && !runsError && runs.length > 0 && (
            <table className="run-table" data-testid="run-table">
              <caption>Recorded Runs for {activeProject.name}</caption>
              <thead>
                <tr>
                  <th scope="col">Run</th>
                  <th scope="col">Engine</th>
                  <th scope="col">Started</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.run_id}>
                    <th scope="row">
                      <button
                        type="button"
                        className="evidence-open mono"
                        data-testid="evidence-open"
                        data-evidence-href={`/api/projects/${encodeURIComponent(activeProjectId)}/runs/${encodeURIComponent(run.run_id)}`}
                        onClick={() => {
                          setEvidenceEmptyDetail(undefined);
                          setEvidenceRequest({
                            kind: "run",
                            href: `/api/projects/${encodeURIComponent(activeProjectId)}/runs/${encodeURIComponent(run.run_id)}`,
                            evidenceId: run.run_id,
                            projectId: run.project_id,
                            label: `Recorded Run ${shortHash(run.run_id, 16)}`,
                          });
                        }}
                      >
                        {shortHash(run.run_id, 16)}
                      </button>
                    </th>
                    <td className="mono">
                      {run.engine_resource_id} v{run.engine_version}
                    </td>
                    <td className="mono">{run.start_time.replace("T", " ").slice(0, 19)}</td>
                    <td>{run.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
        </div>
        <div className={viewClass("assess")} data-view="assess">
        <section className="panel" aria-labelledby="samples-heading">
          <h2 id="samples-heading">Registered samples</h2>
          {samplesLoading && <p className="status" role="status">Loading registered image inputs…</p>}
          {samplesError && <p className="error" role="alert">{samplesError} <button type="button" className="secondary" onClick={() => void refreshSamples()}>Retry</button></p>}
          <p className="hint">
            Only registered inputs can be assessed. Images are read in place on this workstation and are never
            uploaded or copied.
          </p>
          <ul className="sample-list">
            {samples.map((sample) => (
              <li key={sample.registered_sample_id}>
                <button
                  type="button"
                  className={selected?.registered_sample_id === sample.registered_sample_id ? "sample selected" : "sample"}
                  onClick={() => selectWorkingSample(sample)}
                >
                  <span className="sample-name">{sample.display_name}</span>
                  <span className="sample-meta mono">{sample.resource_id}</span>
                  <span className="sample-meta">{sample.role}</span>
                </button>
              </li>
            ))}
            {!samplesLoading && !samplesError && samples.length === 0 && <li className="empty">No registered image inputs are available.</li>}
          </ul>
        </section>

        <section className="panel" aria-labelledby="upload-heading">
          <h2 id="upload-heading">Assess your own photo</h2>
          <p className="hint">
            Decision 30: an operator-picked photo is hashed, stored content-addressed in the local data
            directory, assessed by the registered OFIQ engine, and recorded as a Run marked
            OWNER_SUPPLIED_LOCAL_INPUT. It is never redistributed and is not a registered, rights-reviewed
            dataset.
          </p>
          {activeProjectId ? (
            <div className="controls">
              <input
                ref={uploadInput}
                type="file"
                accept="image/jpeg,image/png,image/bmp"
                aria-label="Owner photo for local assessment"
                data-testid="upload-input"
                disabled={upload.phase === "running"}
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (!file || !activeProjectId) return;
                  void runOwnerUpload(file);
                  event.target.value = "";
                }}
              />
              {upload.phase === "running" && (
                <p className="status" role="status">Assessing {upload.name} with the registered OFIQ engine…</p>
              )}
              {upload.phase === "error" && (
                <div className="error upload-recovery" role="alert" data-testid="upload-error">
                  <p>{upload.detail}</p>
                  <button
                    type="button"
                    className="secondary"
                    data-testid="upload-retry"
                    onClick={() => uploadInput.current?.click()}
                  >
                    Choose another image
                  </button>
                </div>
              )}
              {upload.phase === "done" && (
                <div data-testid="upload-result" className="upload-result">
                  {(() => {
                    const unified = upload.result.measures.find((m) => m.name === "UnifiedQualityScore");
                    return (
                      <>
                        <p>
                          <strong>{upload.result.original_name}</strong> · UnifiedQualityScore{" "}
                          <strong data-testid="upload-uqs">{unified?.scalar ?? "failed"}</strong> / 100 ·{" "}
                          {upload.result.measures.length} measures · {upload.result.failed_measures.length} failed
                        </p>
                        <p className="mono">
                          sha256 {upload.result.image_sha256.slice(0, 16)}… ·{" "}
                          <a href="#structured-evidence">structured Run evidence</a>
                        </p>
                        <p className="hint">{upload.result.provenance.statement}</p>
                        <button
                          type="button"
                          className="secondary"
                          data-testid="upload-open-inspector"
                          disabled={selectedUpload !== null}
                          onClick={() => selectWorkingSample(upload.sample, true)}
                        >
                          {selectedUpload ? "Working sample active" : "Use as working sample"}
                        </button>
                      </>
                    );
                  })()}
                </div>
              )}
            </div>
          ) : (
            <p className="empty">Open a project first — every upload is recorded into a project.</p>
          )}
        </section>

        <section className="panel inspector" aria-labelledby="inspector-heading">
          <h2 id="inspector-heading">Sample</h2>
          {selected ? (
            <>
              <figure className="preview">
                <div className="overlay-frame">
                  <img
                    src={selected.image_url ?? (selected.registered_sample_id ? api.imageUrl(selected.registered_sample_id) : "")}
                    alt={`Working sample ${selected.display_name}`}
                    data-testid="working-sample-image"
                  />
                  {ofiqLandmarks.phase === "shown" && (
                    <svg
                      className="detection-overlay ofiq"
                      viewBox={`0 0 ${ofiqLandmarks.payload.image.width} ${ofiqLandmarks.payload.image.height}`}
                      preserveAspectRatio="none"
                      data-testid="ofiq-landmark-overlay"
                    >
                      {ofiqLandmarks.payload.faces.map((face, index) => (
                        <rect key={index} x={face.x} y={face.y} width={face.width} height={face.height} />
                      ))}
                      {ofiqLandmarks.payload.landmarks.map((point, index) => (
                        <circle key={index} cx={point[0] ?? 0} cy={point[1] ?? 0} r={Math.max(1, ofiqLandmarks.payload.image.width / 180)} />
                      ))}
                    </svg>
                  )}
                  {detection.phase === "shown" && (
                    <svg
                      className="detection-overlay"
                      viewBox={`0 0 ${detection.payload.image.width} ${detection.payload.image.height}`}
                      preserveAspectRatio="none"
                      data-testid="detection-overlay"
                    >
                      {detection.payload.detections.map((det, index) => (
                        <g key={index} className={index === detection.payload.chosen_index ? "det chosen" : "det"}>
                          <rect x={det.bbox[0] ?? 0} y={det.bbox[1] ?? 0} width={(det.bbox[2] ?? 0) - (det.bbox[0] ?? 0)} height={(det.bbox[3] ?? 0) - (det.bbox[1] ?? 0)} />
                          {det.landmarks.map((point, li) => (
                            <circle key={li} cx={point[0] ?? 0} cy={point[1] ?? 0} r={Math.max(1.5, detection.payload.image.width / 120)} />
                          ))}
                        </g>
                      ))}
                    </svg>
                  )}
                </div>
                {detection.phase === "shown" && (
                  <p className="hint" data-testid="detection-label">{detection.payload.label}</p>
                )}
                {ofiqLandmarks.phase === "shown" && (
                  <p className="hint" data-testid="ofiq-landmark-label">
                    {ofiqLandmarks.payload.label} ({ofiqLandmarks.payload.landmark_type}, {ofiqLandmarks.payload.landmarks.length} points)
                  </p>
                )}
                {detection.phase === "error" && <p className="error" role="alert" aria-live="assertive" data-testid="detection-error">{detection.detail}</p>}
                {ofiqLandmarks.phase === "error" && <p className="error" role="alert" aria-live="assertive" data-testid="ofiq-landmark-error">{ofiqLandmarks.detail}</p>}
                <figcaption>
                  <span>{selected.display_name}</span>
                  <span className="mono">sha256 {shortHash(selected.sha256, 16)} · {selected.size_bytes.toLocaleString()} bytes</span>
                </figcaption>
              </figure>
              <div className="controls">
                <label htmlFor="scope">Purpose scope</label>
                <select
                  id="scope"
                  value={scope}
                  onChange={(event) => {
                    assessmentSequence.current += 1;
                    setAssess({ phase: "idle" });
                    setScope(event.target.value);
                  }}
                >
                  {selected.approved_scopes.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
                <div className="overlay-toggles">
                <button
                  type="button"
                  className="secondary"
                  data-testid="detection-toggle"
                  onClick={() => void toggleDetection()}
                >
                  {detection.phase === "loading" ? "Running SCRFD…" : detection.phase === "shown" ? "Hide face detection" : "Face detection (SCRFD)"}
                </button>
                <button
                  type="button"
                  className="secondary"
                  data-testid="ofiq-landmark-toggle"
                  onClick={() => void toggleOfiqLandmarks()}
                >
                  {ofiqLandmarks.phase === "loading" ? "Running OFIQ…" : ofiqLandmarks.phase === "shown" ? "Hide OFIQ landmarks" : "OFIQ landmarks (ADNet-98)"}
                </button>
                </div>
                <button
                  type="button"
                  className="primary"
                  data-testid="assess-button"
                  disabled={assess.phase === "running" || !scope || selectedUpload !== null || !selected.registered_sample_id}
                  onClick={() => void runAssessment()}
                >
                  {assess.phase === "running"
                    ? "Assessing with OFIQ…"
                    : selectedUpload
                      ? "Quality already recorded"
                      : "Assess quality"}
                </button>
                {stressSource && (
                  <button
                    type="button"
                    className="secondary"
                    data-testid="stress-open"
                    aria-expanded={stressOpen}
                    aria-controls="stress-workspace"
                    onClick={() => setStressOpen((current) => !current)}
                  >
                    {stressOpen ? "Close Stress Studio" : "Open Stress Studio"}
                  </button>
                )}
              </div>
            </>
          ) : (
            <p className="empty">Select a registered sample or assess your own photo.</p>
          )}
        </section>

        <section className="panel results" aria-labelledby="results-heading">
          <h2 id="results-heading">Quality measures</h2>
          {assess.phase === "idle" && recordedQuality === null && (
            <p className="empty">Run an assessment to see engine measurements.</p>
          )}
          {assess.phase === "running" && (
            <p className="status" role="status">
              Executing the registered OFIQ runtime on {assess.name}…
            </p>
          )}
          {assess.phase === "error" && (
            <p className="error" role="alert">
              Request refused ({assess.status}): {assess.detail}
            </p>
          )}
          {assess.phase === "failed" && (
            <div className="error" role="alert" data-testid="assessment-failed">
              <strong>Assessment failed (explicit engine failure).</strong>
              <p>{assess.failure.message}</p>
              <p className="mono">
                engine {assess.failure.engine_version} · exit {assess.failure.exit_code} · {assess.failure.reason_code}
              </p>
              <p className="mono">
                failure {shortHash(assess.failure.failure_event_id)} · evidence {assess.failure.failure_evidence_sha256.slice(0, 12)}…
              </p>
            </div>
          )}
          {recordedQuality !== null && (
            <>
              <div className="result-banner" data-testid="result-banner">
                {recordedQuality.kind === "single-assessment" ? (
                  <>
                    <div>
                      <span className="kind">ENGINE MEASUREMENT</span>
                      <span className="policy">policy: none applied</span>
                    </div>
                    <div className="mono">
                      {recordedQuality.result.engine_resource_id} · v{recordedQuality.result.engine_version} · init{" "}
                      {recordedQuality.result.initialization_ms} ms · assess {recordedQuality.result.assessment_ms} ms
                    </div>
                  </>
                ) : (
                  <>
                    <div>
                      <span className="kind">RECORDED OWNER ASSESSMENT</span>
                      <span className="policy">policy: none applied</span>
                    </div>
                    <div className="mono">
                      {recordedQuality.result.source_kind} · {recordedQuality.result.width} × {recordedQuality.result.height} px · run{" "}
                      {shortHash(recordedQuality.result.run_id, 16)}
                    </div>
                  </>
                )}
              </div>
              <QualityResults
                key={recordedQuality.result.run_id}
                measures={recordedQuality.result.measures}
                runId={recordedQuality.result.run_id}
                runUrl={recordedQuality.result.provenance.run}
                sampleId={recordedQuality.kind === "owner-upload" ? recordedQuality.result.sample_id : selected?.recorded_sample_id ?? null}
                purposeScope={recordedQuality.kind === "owner-upload" ? ownerUploadScope : recordedQuality.result.purpose_scope}
              />
            </>
          )}
        </section>

        {stressOpen && stressSource && activeProjectId && (
          <div id="stress-workspace" className="panel stress-workspace-panel">
            <StressStudio
              key={`${activeProjectId}\u001f${stressSource.datasetId}\u001f${stressSource.sampleId}\u001f${stressSource.sha256}`}
              instanceId="inspect"
              projectId={activeProjectId}
              source={stressSource}
              onMessage={onWorkflowMessage}
              onWorkflowTerminal={onWorkflowTerminal}
            />
          </div>
        )}

        </div>
        <div className={viewClass("data")} data-view="data">
          <Workbench projectId={activeProjectId} onMessage={onWorkflowMessage} onDataset={onActiveDataset} onWorkflowTerminal={onWorkflowTerminal} onOpenEvidence={onOpenEvidence} />
        </div>
        <div className={viewClass("matchers")} data-view="matchers">
          <ComparisonStudio
            projectId={activeProjectId}
            onMessage={(message) => onWorkflowMessage(message)}
            onWorkflowTerminal={onWorkflowTerminal}
            onOpenEvidence={onOpenEvidence}
            datasetStudy={(
              <Matchers
                active={view === "matchers"}
                projectId={activeProjectId}
                datasetId={activeDataset?.dataset_id ?? null}
                onMessage={onWorkflowMessage}
                onWorkflowTerminal={onWorkflowTerminal}
                onContextChange={onMatcherContextChange}
                onOpenEvidence={onOpenEvidence}
              />
            )}
          />
        </div>
        <div className={viewClass("utility")} data-view="utility">
          <Utility projectId={activeProjectId} onMessage={onWorkflowMessage} onWorkflowTerminal={onWorkflowTerminal} onContextChange={onUtilityContextChange} onOpenEvidence={onOpenEvidence} />
        </div>
        <div className={viewClass("cross")} data-view="cross">
          <CrossMatcher projectId={activeProjectId} onMessage={onWorkflowMessage} onWorkflowTerminal={onWorkflowTerminal} onOpenEvidence={onOpenEvidence} />
        </div>
        <div className={viewClass("reports")} data-view="reports">
          <Reports projectId={activeProjectId} datasetId={activeDataset?.dataset_id ?? null} onMessage={onWorkflowMessage} onOpenEvidence={onOpenEvidence} />
        </div>
        <div className={viewClass("advanced")} data-view="advanced">
          {stressSource && activeProjectId ? (
            <div className="panel stress-workspace-panel stress-route-workspace">
              <StressStudio
                key={`advanced\u001f${activeProjectId}\u001f${stressSource.datasetId}\u001f${stressSource.sampleId}\u001f${stressSource.sha256}`}
                instanceId="advanced"
                projectId={activeProjectId}
                source={stressSource}
                onMessage={onWorkflowMessage}
                onWorkflowTerminal={onWorkflowTerminal}
              />
            </div>
          ) : (
            <section className="panel stress-prerequisite" data-testid="stress-prerequisite" aria-labelledby="stress-prerequisite-title">
              <p className="eyebrow">Single-image stress test</p>
              <h2 id="stress-prerequisite-title">Choose one recorded working image first</h2>
              <p>Stress Studio starts from an owner-supplied image whose source bytes, hash, quality Run, and sample identity are already recorded.</p>
              <a className="button primary" href="#/assess">Go to Inspect quality</a>
            </section>
          )}
          <section className="stress-population-study" aria-labelledby="stress-population-study-title">
            <div className="stress-population-heading">
              <p className="eyebrow">Research extension</p>
              <h2 id="stress-population-study-title">Population degradation analysis</h2>
              <aside className="research-boundary" role="note" data-testid="advanced-beta-boundary">
                <strong>Outside beta acceptance</strong>
                <p>Dataset-level population degradation, 1:N identification, and container execution are a research extension. They remain separate from the accepted single-image Stress test above.</p>
              </aside>
            </div>
          <Advanced
            projectId={activeProjectId}
            datasetId={activeDataset?.dataset_id ?? null}
            identityAuthorityAvailable={activeDataset?.subject_count !== null && activeDataset?.subject_count !== undefined}
            datasetRepresentation={activeDataset?.manifest["source_kind"] === "MANAGED_PREALIGNED_PAIR_DATASET" ? "AUTHOR_PREALIGNED_ARCFACE112" : "SOURCE_IMAGE_REQUIRING_DETECTION"}
            onMessage={onWorkflowMessage}
            onWorkflowTerminal={onWorkflowTerminal}
            onOpenEvidence={onOpenEvidence}
          />
          </section>
        </div>
        <div className={viewClass("system")} data-view="system">
          <SystemPanel projectId={activeProjectId} onRecovered={onSystemRecovered} onOpenEvidence={onOpenEvidence} />
        </div>

        {(view === "projects" || evidenceRequest || evidenceEmptyDetail) && (
          <EvidenceDetail
            request={evidenceRequest}
            activeProjectId={activeProjectId}
            emptyDetail={evidenceEmptyDetail}
            onClose={() => {
              setEvidenceRequest(null);
              setEvidenceEmptyDetail(undefined);
            }}
          />
        )}
      </main>
    </div>
  );
}

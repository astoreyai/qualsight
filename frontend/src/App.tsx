import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  api,
  ApiError,
  type AssessResponse,
  type AssessmentFailure,
  type DatasetOut,
  type HealthOut,
  type ProjectOut,
  type RegisteredSample,
  type RunSummaryOut,
  type UploadAssessOut,
} from "./api";
import { Workbench } from "./Workbench";
import { Matchers } from "./Matchers";
import { Utility } from "./Utility";
import { CrossMatcher } from "./CrossMatcher";
import { Reports } from "./Reports";
import { Advanced } from "./Advanced";
import { Nav } from "./Nav";
import { SystemPanel } from "./SystemPanel";

type AssessState =
  | { phase: "idle" }
  | { phase: "running"; path: string }
  | { phase: "done"; result: AssessResponse }
  | { phase: "failed"; failure: AssessmentFailure }
  | { phase: "error"; status: number; detail: string };

type UiMessage = {
  kind: "success" | "progress" | "warning" | "error";
  text: string;
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

function fileName(path: string): string {
  const parts = path.split("/");
  return parts[parts.length - 1] ?? path;
}

function registeredSampleKey(sample: RegisteredSample | null): string {
  return sample ? `${sample.path}\u001f${sample.sha256}` : "";
}

export function App() {
  const [health, setHealth] = useState<HealthOut | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [samples, setSamples] = useState<RegisteredSample[]>([]);
  const [samplesError, setSamplesError] = useState<string | null>(null);
  const [samplesLoading, setSamplesLoading] = useState(true);
  const [selected, setSelected] = useState<RegisteredSample | null>(null);
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
  const [view, setView] = useState<string>(() => window.location.hash.replace(/^#\//, "") || "projects");
  useEffect(() => {
    const onHash = () => setView(window.location.hash.replace(/^#\//, "") || "projects");
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
    | { phase: "done"; result: UploadAssessOut }
    | { phase: "error"; detail: string }
  >({ phase: "idle" });
  const [newProjectName, setNewProjectName] = useState("");
  const [projectMessage, setProjectMessage] = useState<UiMessage | null>(null);
  const [confirmArchive, setConfirmArchive] = useState(false);
  const [activeDataset, setActiveDataset] = useState<DatasetOut | null>(null);
  const runsRefreshSequence = useRef(0);
  const projectsRefreshSequence = useRef(0);
  const samplesRefreshSequence = useRef(0);
  const assessmentContextEpoch = useRef(0);
  const assessmentSequence = useRef(0);
  const uploadContextEpoch = useRef(0);
  const uploadSequence = useRef(0);
  const detectionSequence = useRef(0);
  const ofiqLandmarksSequence = useRef(0);
  const selectedSampleKeyRef = useRef("");
  const assessmentContextKey = `${registeredSampleKey(selected)}\u001f${scope}\u001f${activeProjectId ?? ""}`;
  const assessmentContextKeyRef = useRef(assessmentContextKey);
  assessmentContextKeyRef.current = assessmentContextKey;
  const ownerUploadScope = scope || "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1";
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
  const selectRegisteredSample = useCallback((sample: RegisteredSample | null, preserveApprovedScope = false) => {
    const nextKey = registeredSampleKey(sample);
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
      const selectedPath = selectedSampleKeyRef.current.split("\u001f", 1)[0] ?? "";
      const nextSelected = displayable.find((item) => item.path === selectedPath) ?? displayable[0] ?? null;
      selectRegisteredSample(nextSelected, true);
    } catch (error) {
      if (samplesRefreshSequence.current !== sequence) return;
      setSamplesError(`Registered samples are unavailable: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      if (samplesRefreshSequence.current === sequence) setSamplesLoading(false);
    }
  }, [selectRegisteredSample]);
  const onWorkflowMessage = useCallback((text: string) => {
    setProjectMessage(messageFromLegacyCallback(text));
  }, []);
  const onWorkflowTerminal = useCallback(() => {
    void refreshRuns();
    void refreshProjects();
  }, [refreshProjects, refreshRuns]);
  const activeProject = useMemo(
    () => projects.find((p) => p.project_id === activeProjectId) ?? null,
    [projects, activeProjectId],
  );
  const runsMatchActiveProject = activeProjectId !== null && runsProjectId === activeProjectId;

  useLayoutEffect(() => {
    assessmentContextKeyRef.current = assessmentContextKey;
    assessmentContextEpoch.current += 1;
    assessmentSequence.current += 1;
    setAssess({ phase: "idle" });
  }, [assessmentContextKey]);

  useLayoutEffect(() => {
    uploadContextKeyRef.current = uploadContextKey;
    uploadContextEpoch.current += 1;
    uploadSequence.current += 1;
    setUpload({ phase: "idle" });
  }, [uploadContextKey]);

  useEffect(() => {
    setActiveDataset(null);
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

  useEffect(() => {
    api
      .health()
      .then(setHealth)
      .catch((error: unknown) => setHealthError(error instanceof Error ? error.message : String(error)));
    void refreshSamples();
    void refreshRuns();
    void refreshProjects();
  }, [refreshRuns, refreshProjects, refreshSamples]);

  const runAssessment = useCallback(async () => {
    if (!selected || !scope) return;
    const epoch = assessmentContextEpoch.current;
    const sequence = ++assessmentSequence.current;
    const requestKey = assessmentContextKey;
    setAssess({ phase: "running", path: selected.path });
    try {
      const result = await api.assess(selected.path, scope, activeProjectId);
      if (assessmentContextEpoch.current !== epoch || assessmentSequence.current !== sequence || assessmentContextKeyRef.current !== requestKey) return;
      setAssess({ phase: "done", result });
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
    setUpload({ phase: "running", name: file.name });
    try {
      const result = await api.assessUpload(activeProjectId, ownerUploadScope, file);
      if (uploadContextEpoch.current !== epoch || uploadSequence.current !== sequence || uploadContextKeyRef.current !== requestKey) return;
      setUpload({ phase: "done", result });
      void refreshRuns();
      void refreshProjects();
    } catch (error) {
      if (uploadContextEpoch.current !== epoch || uploadSequence.current !== sequence || uploadContextKeyRef.current !== requestKey) return;
      setUpload({
        phase: "error",
        detail: error instanceof ApiError ? `${error.status}: ${error.detail}` : String(error),
      });
    }
  }, [activeProjectId, ownerUploadScope, uploadContextKey, refreshRuns, refreshProjects]);

  const toggleDetection = useCallback(async () => {
    if (!selected) return;
    const sequence = ++detectionSequence.current;
    const requestKey = registeredSampleKey(selected);
    if (detection.phase === "shown") {
      setDetection({ phase: "hidden" });
      return;
    }
    setDetection({ phase: "loading" });
    try {
      const response = await fetch(`/api/detection?path=${encodeURIComponent(selected.path)}`);
      if (!response.ok) throw new Error(`${response.status}: ${(await response.json()).detail ?? response.statusText}`);
      const payload = await response.json();
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
    const requestKey = registeredSampleKey(selected);
    if (ofiqLandmarks.phase === "shown") {
      setOfiqLandmarks({ phase: "hidden" });
      return;
    }
    setOfiqLandmarks({ phase: "loading" });
    try {
      const response = await fetch(`/api/ofiq-landmarks?path=${encodeURIComponent(selected.path)}`);
      if (!response.ok) throw new Error(`${response.status}: ${(await response.json()).detail ?? response.statusText}`);
      const payload = await response.json();
      if (ofiqLandmarksSequence.current !== sequence || selectedSampleKeyRef.current !== requestKey) return;
      setOfiqLandmarks({ phase: "shown", payload });
    } catch (error) {
      if (ofiqLandmarksSequence.current !== sequence || selectedSampleKeyRef.current !== requestKey) return;
      setOfiqLandmarks({ phase: "error", detail: error instanceof Error ? error.message : String(error) });
    }
  }, [selected, ofiqLandmarks.phase]);

  const measures = useMemo(() => (assess.phase === "done" ? assess.result.measures : []), [assess]);

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
        <div className="context-item">
          <span className="context-label">Project</span>
          <strong>{activeProject?.name ?? "No project selected"}</strong>
          {activeProject && <span className="context-meta">{activeProject.lifecycle} · v{activeProject.version}</span>}
        </div>
        <div className="context-item">
          <span className="context-label">Dataset</span>
          <strong>{activeDataset?.name ?? "No dataset selected"}</strong>
          {activeDataset && <span className="context-meta">{activeDataset.sample_count.toLocaleString()} samples · {activeDataset.subject_count?.toLocaleString() ?? "unlabelled"} subjects</span>}
        </div>
        <div className="context-item">
          <span className="context-label">Recorded evidence</span>
          <strong>{activeProject ? `${activeProject.run_ids.length} run${activeProject.run_ids.length === 1 ? "" : "s"}` : "No project evidence"}</strong>
          {activeProject && <span className="context-meta mono">{shortHash(activeProject.evidence_sha256, 12)}</span>}
        </div>
        <div className="context-actions">
          {!activeProject && <a className="button secondary" href="#/projects">Select or create a project</a>}
          {activeProject && !activeDataset && <a className="button secondary" href="#/data">Select or import a dataset</a>}
        </div>
      </aside>
      {projectMessage && (
        <div className={`global-message ${projectMessage.kind}`} role={projectMessage.kind === "error" ? "alert" : "status"} data-testid="project-message">
          <span>{projectMessage.text}</span>
          <button type="button" className="message-dismiss" onClick={() => setProjectMessage(null)}>Dismiss</button>
        </div>
      )}

      <main className="layout" id="main-content" tabIndex={-1}>
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
                      <a href={`/api/runs/${run.run_id}`} className="mono">
                        {shortHash(run.run_id, 16)}
                      </a>
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
              <li key={sample.path}>
                <button
                  type="button"
                  className={selected?.path === sample.path ? "sample selected" : "sample"}
                  onClick={() => selectRegisteredSample(sample)}
                >
                  <span className="sample-name">{fileName(sample.path)}</span>
                  <span className="sample-meta mono">{sample.resource_id}</span>
                  <span className="sample-meta">{sample.resource_state}</span>
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
                <p className="error" role="alert" data-testid="upload-error">{upload.detail}</p>
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
                          <a href={upload.result.provenance.run}>run record</a>
                        </p>
                        <p className="hint">{upload.result.provenance.statement}</p>
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
                  <img src={api.imageUrl(selected.path)} alt={`Registered sample ${fileName(selected.path)}`} />
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
                {detection.phase === "error" && <p className="error">{detection.detail}</p>}
                {ofiqLandmarks.phase === "error" && <p className="error">{ofiqLandmarks.detail}</p>}
                <figcaption>
                  <span className="mono">{selected.path}</span>
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
                  disabled={assess.phase === "running" || !scope}
                  onClick={() => void runAssessment()}
                >
                  {assess.phase === "running" ? "Assessing with OFIQ…" : "Assess quality"}
                </button>
              </div>
            </>
          ) : (
            <p className="empty">Select a registered sample.</p>
          )}
        </section>

        <section className="panel results" aria-labelledby="results-heading">
          <h2 id="results-heading">Quality measures</h2>
          {assess.phase === "idle" && <p className="empty">Run an assessment to see engine measurements.</p>}
          {assess.phase === "running" && (
            <p className="status" role="status">
              Executing the registered OFIQ runtime on {fileName(assess.path)}…
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
              <p>{assess.failure.reason}</p>
              <p className="mono">
                engine {assess.failure.engine_version} · exit {assess.failure.exit_code}
              </p>
            </div>
          )}
          {assess.phase === "done" && (
            <>
              <div className="result-banner" data-testid="result-banner">
                <div>
                  <span className="kind">ENGINE MEASUREMENT</span>
                  <span className="policy">policy: none applied</span>
                </div>
                <div className="mono">
                  {assess.result.engine_resource_id} · v{assess.result.engine_version} · init{" "}
                  {assess.result.initialization_ms} ms · assess {assess.result.assessment_ms} ms
                </div>
              </div>
              <table className="measures" data-testid="measure-table">
                <caption>Quality measures for the assessed image</caption>
                <thead>
                  <tr>
                    <th scope="col">Measure (ISO/IEC 29794-5)</th>
                    <th scope="col">Native value</th>
                    <th scope="col">Quality component (0–100)</th>
                    <th scope="col">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {measures.map((measure) => (
                    <tr key={measure.name} className={measure.failed ? "failed" : ""}>
                      <th scope="row">{measure.name}</th>
                      <td className="mono">{measure.native === null ? "—" : measure.native}</td>
                      <td>
                        {measure.scalar === null ? (
                          <span className="mono">—</span>
                        ) : (
                          <span className="bar" aria-label={`${measure.scalar} of 100`}>
                            <span className="bar-fill" style={{ width: `${measure.scalar}%` }} />
                            <span className="bar-value mono">{measure.scalar}</span>
                          </span>
                        )}
                      </td>
                      <td>{measure.failed ? "engine failure" : "measured"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="provenance" data-testid="provenance">
                <h3>Provenance</h3>
                <dl>
                  <dt>Run</dt>
                  <dd>
                    <a href={assess.result.provenance.run} className="mono">
                      {shortHash(assess.result.run_id, 20)}
                    </a>
                  </dd>
                  <dt>Evidence</dt>
                  <dd className="mono">{shortHash(assess.result.evidence_sha256, 20)}</dd>
                  <dt>Image</dt>
                  <dd className="mono">{shortHash(assess.result.image_sha256, 20)}</dd>
                  <dt>Scope</dt>
                  <dd className="mono">{assess.result.purpose_scope}</dd>
                  <dt>Links</dt>
                  <dd className="links">
                    <a href={assess.result.provenance.project}>project</a>
                    <a href={assess.result.provenance.experiment}>experiment</a>
                    <a href={assess.result.provenance.sample}>sample</a>
                    <a href={assess.result.provenance.audit_events}>audit</a>
                  </dd>
                </dl>
                {assess.result.warnings.length > 0 && (
                  <ul className="warnings">
                    {assess.result.warnings.map((warning) => (
                      <li key={warning}>{warning}</li>
                    ))}
                  </ul>
                )}
              </div>
            </>
          )}
        </section>

        </div>
        <div className={viewClass("data")} data-view="data">
          <Workbench projectId={activeProjectId} onMessage={onWorkflowMessage} onDataset={setActiveDataset} onWorkflowTerminal={onWorkflowTerminal} />
        </div>
        <div className={viewClass("matchers")} data-view="matchers">
          <Matchers projectId={activeProjectId} datasetId={activeDataset?.dataset_id ?? null} onMessage={onWorkflowMessage} onWorkflowTerminal={onWorkflowTerminal} />
        </div>
        <div className={viewClass("utility")} data-view="utility">
          <Utility projectId={activeProjectId} onMessage={onWorkflowMessage} onWorkflowTerminal={onWorkflowTerminal} />
        </div>
        <div className={viewClass("cross")} data-view="cross">
          <CrossMatcher projectId={activeProjectId} onMessage={onWorkflowMessage} onWorkflowTerminal={onWorkflowTerminal} />
        </div>
        <div className={viewClass("reports")} data-view="reports">
          <Reports projectId={activeProjectId} datasetId={activeDataset?.dataset_id ?? null} onMessage={onWorkflowMessage} />
        </div>
        <div className={viewClass("advanced")} data-view="advanced">
          <Advanced
            projectId={activeProjectId}
            datasetId={activeDataset?.dataset_id ?? null}
            identityAuthorityAvailable={activeDataset?.subject_count !== null && activeDataset?.subject_count !== undefined}
            datasetRepresentation={activeDataset?.manifest["source_kind"] === "MANAGED_PREALIGNED_PAIR_DATASET" ? "AUTHOR_PREALIGNED_ARCFACE112" : "SOURCE_IMAGE_REQUIRING_DETECTION"}
            onMessage={onWorkflowMessage}
            onWorkflowTerminal={onWorkflowTerminal}
          />
        </div>
        <div className={viewClass("system")} data-view="system">
          <SystemPanel projectId={activeProjectId} />
        </div>

        
      </main>
    </div>
  );
}

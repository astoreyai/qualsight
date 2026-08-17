import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  ApiError,
  type AssessResponse,
  type AssessmentFailure,
  type HealthOut,
  type ProjectOut,
  type RegisteredSample,
  type RunSummaryOut,
} from "./api";
import { Workbench } from "./Workbench";
import { Matchers } from "./Matchers";
import { Utility } from "./Utility";

type AssessState =
  | { phase: "idle" }
  | { phase: "running"; path: string }
  | { phase: "done"; result: AssessResponse }
  | { phase: "failed"; failure: AssessmentFailure }
  | { phase: "error"; status: number; detail: string };

function shortHash(value: string, keep = 12): string {
  const marker = value.indexOf("sha256:");
  const digest = marker >= 0 ? value.slice(marker + 7) : value;
  return `${digest.slice(0, keep)}…`;
}

function fileName(path: string): string {
  const parts = path.split("/");
  return parts[parts.length - 1] ?? path;
}

export function App() {
  const [health, setHealth] = useState<HealthOut | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [samples, setSamples] = useState<RegisteredSample[]>([]);
  const [selected, setSelected] = useState<RegisteredSample | null>(null);
  const [scope, setScope] = useState<string>("");
  const [assess, setAssess] = useState<AssessState>({ phase: "idle" });
  const [runs, setRuns] = useState<RunSummaryOut[]>([]);
  const [projects, setProjects] = useState<ProjectOut[]>([]);
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null);
  const [newProjectName, setNewProjectName] = useState("");
  const [projectMessage, setProjectMessage] = useState<string | null>(null);
  const [activeDatasetId, setActiveDatasetId] = useState<string | null>(null);

  const refreshRuns = useCallback(() => {
    api.runs().then(setRuns).catch(() => setRuns([]));
  }, []);
  const refreshProjects = useCallback(() => {
    api.projects().then(setProjects).catch(() => setProjects([]));
  }, []);
  const activeProject = useMemo(
    () => projects.find((p) => p.project_id === activeProjectId) ?? null,
    [projects, activeProjectId],
  );

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
      setProjectMessage(`Created ${created.name} (v${created.version}, ${created.lifecycle})`);
      refreshProjects();
    } catch (error) {
      setProjectMessage(error instanceof Error ? error.message : String(error));
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
        setProjectMessage(`${action}: ${updated.name} is now v${updated.version} ${updated.lifecycle}`);
        if (action === "clone") setActiveProjectId(updated.project_id);
        refreshProjects();
      } catch (error) {
        setProjectMessage(error instanceof Error ? error.message : String(error));
      }
    },
    [activeProjectId, activeProject, refreshProjects],
  );

  useEffect(() => {
    api
      .health()
      .then(setHealth)
      .catch((error: unknown) => setHealthError(error instanceof Error ? error.message : String(error)));
    api
      .registeredSamples()
      .then((items) => {
        const displayable = items.filter((item) => item.media_type.startsWith("image/"));
        setSamples(displayable);
        const first = displayable[0];
        if (first) {
          setSelected(first);
          setScope(first.approved_scopes[0] ?? "");
        }
      })
      .catch(() => setSamples([]));
    refreshRuns();
    refreshProjects();
  }, [refreshRuns, refreshProjects]);

  const runAssessment = useCallback(async () => {
    if (!selected || !scope) return;
    setAssess({ phase: "running", path: selected.path });
    try {
      const result = await api.assess(selected.path, scope, activeProjectId);
      setAssess({ phase: "done", result });
      refreshRuns();
      refreshProjects();
    } catch (error) {
      if (error instanceof ApiError && error.failure) {
        setAssess({ phase: "failed", failure: error.failure });
      } else if (error instanceof ApiError) {
        setAssess({ phase: "error", status: error.status, detail: error.detail });
      } else {
        setAssess({ phase: "error", status: 0, detail: error instanceof Error ? error.message : String(error) });
      }
    }
  }, [selected, scope, activeProjectId, refreshRuns, refreshProjects]);

  const measures = useMemo(() => (assess.phase === "done" ? assess.result.measures : []), [assess]);

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <div>
            <h1>QualSight</h1>
            <p className="subtitle">Sample inspector · Phase-0 quality vertical slice</p>
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

      <main className="layout">
        <section className="panel projects" aria-labelledby="projects-heading" data-testid="projects-panel">
          <h2 id="projects-heading">Projects</h2>
          <div className="project-create">
            <input
              type="text"
              placeholder="New project name"
              value={newProjectName}
              onChange={(event) => setNewProjectName(event.target.value)}
              data-testid="project-name-input"
              aria-label="New project name"
            />
            <button type="button" className="secondary" data-testid="project-create" onClick={() => void createProject()} disabled={!newProjectName.trim() || !scope}>
              New
            </button>
          </div>
          <label htmlFor="active-project" className="hint">
            Active project (assessments are recorded into it)
          </label>
          <select
            id="active-project"
            data-testid="project-select"
            value={activeProjectId ?? ""}
            onChange={(event) => setActiveProjectId(event.target.value || null)}
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
              <button type="button" className="secondary danger" onClick={() => void projectAction("archive")}>Archive</button>
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
          {projectMessage && (
            <p className="hint" role="status" data-testid="project-message">
              {projectMessage}
            </p>
          )}
        </section>

        <section className="panel" aria-labelledby="samples-heading">
          <h2 id="samples-heading">Registered samples</h2>
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
                  onClick={() => {
                    setSelected(sample);
                    setScope(sample.approved_scopes[0] ?? "");
                    setAssess({ phase: "idle" });
                  }}
                >
                  <span className="sample-name">{fileName(sample.path)}</span>
                  <span className="sample-meta mono">{sample.resource_id}</span>
                  <span className="sample-meta">{sample.resource_state}</span>
                </button>
              </li>
            ))}
            {samples.length === 0 && <li className="empty">No registered image inputs are available.</li>}
          </ul>
        </section>

        <section className="panel inspector" aria-labelledby="inspector-heading">
          <h2 id="inspector-heading">Sample</h2>
          {selected ? (
            <>
              <figure className="preview">
                <img src={api.imageUrl(selected.path)} alt={`Registered sample ${fileName(selected.path)}`} />
                <figcaption>
                  <span className="mono">{selected.path}</span>
                  <span className="mono">sha256 {shortHash(selected.sha256, 16)} · {selected.size_bytes.toLocaleString()} bytes</span>
                </figcaption>
              </figure>
              <div className="controls">
                <label htmlFor="scope">Purpose scope</label>
                <select id="scope" value={scope} onChange={(event) => setScope(event.target.value)}>
                  {selected.approved_scopes.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
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

        <Workbench projectId={activeProjectId} onMessage={setProjectMessage} onDataset={setActiveDatasetId} />
        <Matchers projectId={activeProjectId} datasetId={activeDatasetId} onMessage={setProjectMessage} />
        <Utility projectId={activeProjectId} onMessage={setProjectMessage} />

        <section className="panel runs" aria-labelledby="runs-heading">
          <h2 id="runs-heading">Recorded runs</h2>
          {runs.length === 0 ? (
            <p className="empty">No immutable runs are recorded yet.</p>
          ) : (
            <table className="run-table" data-testid="run-table">
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
                    <td>
                      <a href={`/api/runs/${run.run_id}`} className="mono">
                        {shortHash(run.run_id, 16)}
                      </a>
                    </td>
                    <td className="mono">
                      {run.engine_resource_id} v{run.engine_version}
                    </td>
                    <td className="mono">{run.start_time}</td>
                    <td>{run.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </main>
    </div>
  );
}

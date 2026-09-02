import { useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  type ProjectOut,
  type ProjectPurgeOut,
  type ProjectPurgeScopeOut,
} from "./api";

interface ProjectPurgeProps {
  project: ProjectOut;
  onPurged: (record: ProjectPurgeOut) => void;
}

type PurgeDialogState =
  | { phase: "closed" }
  | { phase: "loading" }
  | { phase: "ready"; scope: ProjectPurgeScopeOut }
  | { phase: "purging"; scope: ProjectPurgeScopeOut }
  | { phase: "error"; detail: string };

function safePurgeError(error: unknown): string {
  if (error instanceof ApiError) {
    return `Purge was not completed (HTTP ${error.status}). Project data remains selected. Retry after active work finishes or inspect the local audit log.`;
  }
  return "Purge was not completed. Project data remains selected. Retry after active work finishes or inspect the local audit log.";
}

export function ProjectPurge({ project, onPurged }: ProjectPurgeProps) {
  const [state, setState] = useState<PurgeDialogState>({ phase: "closed" });
  const [confirmation, setConfirmation] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  const sequence = useRef(0);

  useEffect(() => {
    sequence.current += 1;
    setConfirmation("");
    setState({ phase: "closed" });
    dialog.current?.close();
  }, [project.project_id]);

  useEffect(() => () => { sequence.current += 1; }, []);

  const open = async () => {
    const request = ++sequence.current;
    setConfirmation("");
    setState({ phase: "loading" });
    if (dialog.current && !dialog.current.open) dialog.current.showModal();
    try {
      const scope = await api.projectPurgeScope(project.project_id);
      if (sequence.current === request) setState({ phase: "ready", scope });
    } catch (error) {
      if (sequence.current === request) setState({ phase: "error", detail: safePurgeError(error) });
    }
  };

  const close = () => {
    if (state.phase === "purging") return;
    sequence.current += 1;
    setConfirmation("");
    setState({ phase: "closed" });
    dialog.current?.close();
  };

  const purge = async () => {
    if (state.phase !== "ready" || confirmation !== project.project_id) return;
    const request = ++sequence.current;
    const scope = state.scope;
    setState({ phase: "purging", scope });
    try {
      const record = await api.purgeProject(project.project_id, confirmation);
      if (sequence.current !== request) return;
      if (!record.verified) {
        setState({ phase: "error", detail: "Purge evidence did not verify. Project data remains selected; inspect the local audit log before retrying." });
        return;
      }
      dialog.current?.close();
      setState({ phase: "closed" });
      setConfirmation("");
      onPurged(record);
    } catch (error) {
      if (sequence.current === request) setState({ phase: "error", detail: safePurgeError(error) });
    }
  };

  return (
    <>
      <button
        type="button"
        className="secondary danger"
        data-testid="project-purge-open"
        onClick={() => void open()}
      >
        Purge project data
      </button>
      <dialog
        ref={dialog}
        className="project-purge-dialog"
        data-testid="project-purge-dialog"
        aria-labelledby="project-purge-title"
        aria-describedby="project-purge-description"
        onCancel={(event) => {
          event.preventDefault();
          close();
        }}
      >
        <div className="project-purge-dialog-body">
          <span className="comparison-kicker">Destructive local action</span>
          <h3 id="project-purge-title">Purge recorded data for {project.name}</h3>
          <p id="project-purge-description">
            Review the backend-published scope before confirming. This action cannot be undone from QualSight.
          </p>

          {state.phase === "loading" && <p className="status" role="status">Reading the recorded purge scope…</p>}
          {(state.phase === "ready" || state.phase === "purging") && (
            <section className="project-purge-scope" data-testid="project-purge-scope" aria-labelledby="project-purge-scope-title">
              <h4 id="project-purge-scope-title">Recorded scope</h4>
              <p>{state.scope.scope}</p>
              <dl className="facts">
                <dt>Recorded file targets</dt>
                <dd>{state.scope.target_counts.files}</dd>
                <dt>Recorded directory targets</dt>
                <dd>{state.scope.target_counts.directories}</dd>
                <dt>Retained</dt>
                <dd>Project shell, version history, local audit history, purge attestation, and source dataset images outside QualSight.</dd>
                <dt>Not performed</dt>
                <dd>Secure erasure of SQLite free pages, filesystem journals, or backups.</dd>
              </dl>
            </section>
          )}
          {state.phase === "error" && (
            <div className="error" role="alert">
              <p>{state.detail}</p>
              <button type="button" className="secondary" onClick={() => void open()}>Retry scope check</button>
            </div>
          )}

          {(state.phase === "ready" || state.phase === "purging") && (
            <label className="project-purge-confirmation">
              <span>Type the exact project ID to confirm</span>
              <code>{project.project_id}</code>
              <input
                type="text"
                value={confirmation}
                data-testid="project-purge-confirmation"
                disabled={state.phase === "purging"}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => setConfirmation(event.target.value)}
              />
            </label>
          )}

          <div className="project-purge-actions">
            <button
              type="button"
              className="secondary danger"
              data-testid="project-purge-confirm"
              disabled={state.phase !== "ready" || confirmation !== project.project_id}
              onClick={() => void purge()}
            >
              {state.phase === "purging" ? "Purging and verifying…" : "Purge and verify"}
            </button>
            <button
              type="button"
              className="secondary"
              data-testid="project-purge-cancel"
              disabled={state.phase === "purging"}
              onClick={close}
            >
              Cancel
            </button>
          </div>
        </div>
      </dialog>
    </>
  );
}

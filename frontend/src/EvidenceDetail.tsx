import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

export type EvidenceKind = "run" | "sample" | "sample-quality" | "analysis" | "audit" | "figure" | "direct-run" | "direct-comparison";

export interface EvidenceRequest {
  kind: EvidenceKind;
  href: string;
  evidenceId: string;
  projectId: string;
  label: string;
}

interface EvidenceDetailProps {
  request: EvidenceRequest | null;
  activeProjectId: string | null;
  emptyDetail?: string | undefined;
  onClose?: () => void;
}

interface EvidenceOpenButtonProps {
  request: EvidenceRequest;
  onOpen: (request: EvidenceRequest) => void;
  children: ReactNode;
  className?: string;
  testId?: string;
}

type JsonRecord = Record<string, unknown>;

type EvidenceState =
  | { phase: "empty"; detail: string }
  | { phase: "loading"; key: string; request: EvidenceRequest }
  | { phase: "ready"; key: string; request: EvidenceRequest; payload: unknown }
  | { phase: "error"; key: string; request: EvidenceRequest; detail: string };

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function recordField(record: JsonRecord, key: string): JsonRecord | null {
  return isRecord(record[key]) ? record[key] : null;
}

function stringField(record: JsonRecord, key: string): string | null {
  return typeof record[key] === "string" ? record[key] : null;
}

function numberField(record: JsonRecord, key: string): number | null {
  return typeof record[key] === "number" && Number.isFinite(record[key]) ? record[key] : null;
}

function isProjectResourceHref(href: string, projectId: string, resource: "samples" | "datasets"): boolean {
  const match = new RegExp(`^/api/projects/([^/]+)/${resource}/`).exec(href);
  if (!match?.[1]) return false;
  try {
    return decodeURIComponent(match[1]) === projectId;
  } catch {
    return false;
  }
}

function isProjectRunHref(href: string, projectId: string, runId?: string): boolean {
  const match = /^\/api\/projects\/([^/]+)\/runs\/([^/]+)$/.exec(href);
  if (!match?.[1] || !match[2]) return false;
  try {
    const hrefProjectId = decodeURIComponent(match[1]);
    const hrefRunId = decodeURIComponent(match[2]);
    return hrefProjectId === projectId && (runId === undefined || hrefRunId === runId);
  } catch {
    return false;
  }
}

function scalarText(value: unknown): string {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return "Not recorded";
}

function shortIdentity(value: string): string {
  const marker = value.lastIndexOf(":");
  const leaf = marker >= 0 ? value.slice(marker + 1) : value;
  return leaf.length > 20 ? `${leaf.slice(0, 20)}…` : leaf;
}

export function containsAbsoluteFilesystemPath(value: string): boolean {
  const pathForms = {
    posix: /(?:^|[\s"'`([{=:])\/(?!\/)(?:[^\s"'`)\]}]+\/)*[^\s"'`)\]}]+/,
    windows: /(?:^|[\s"'`([{])[A-Za-z]:[\\/]/,
    unc: /(?:^|[\s"'`([{])(?:\\\\|\/\/)[^\s\\/"'`)\]}]+[\\/][^\s"'`)\]}]+/,
    file: /(?:^|[\s"'`([{])file:\/{1,3}[^\s"'`)\]}]+/i,
    tilde: /(?:^|[\s"'`([{])~[\\/][^\s"'`)\]}]+/,
  };
  return Object.values(pathForms).some((pattern) => pattern.test(value));
}

type StructuredRunWarning =
  | { state: "valid"; sampleId: string; outcome: "ENGINE_ROW_FAILURE" | "INTEGRITY_FAILURE" | "NOT_ASSESSED"; reason: string }
  | { state: "invalid" }
  | { state: "unstructured" };

function structuredRunWarning(value: string): StructuredRunWarning {
  try {
    const parsed: unknown = JSON.parse(value);
    if (!isRecord(parsed)) return { state: "invalid" };
    const fields = Object.keys(parsed).sort();
    if (fields.join("\u001f") !== ["outcome", "reason", "sample_id"].join("\u001f")) return { state: "invalid" };
    const sampleId = stringField(parsed, "sample_id");
    const outcome = stringField(parsed, "outcome");
    const reason = stringField(parsed, "reason");
    if (!sampleId || !/^sample:sha256:[0-9a-f]{64}$/.test(sampleId)) return { state: "invalid" };
    if (outcome !== "ENGINE_ROW_FAILURE" && outcome !== "INTEGRITY_FAILURE" && outcome !== "NOT_ASSESSED") return { state: "invalid" };
    if (!reason?.trim()) return { state: "invalid" };
    if (containsAbsoluteFilesystemPath(sampleId) || containsAbsoluteFilesystemPath(outcome)) return { state: "invalid" };
    return { state: "valid", sampleId, outcome, reason };
  } catch {
    return /^[\s]*[\[{]/.test(value) ? { state: "invalid" } : { state: "unstructured" };
  }
}

function safeFailureSummary(outcome: string, reason: string): string | null {
  const normalized = reason.toLowerCase();
  if (normalized.includes("unreadable") || normalized.includes("no such file") || normalized.includes("permission denied")) {
    return "A recorded sample was not assessed because its source bytes were unreadable.";
  }
  if (normalized.includes("sha256 drift") || normalized.includes("sha-256 drift")) {
    return "A recorded sample was not assessed because source integrity verification failed.";
  }
  if (normalized.includes("empty measures")) {
    return "A recorded sample failed measurement because the quality engine returned empty measures.";
  }
  if (normalized.includes("produced no row")) {
    return "A recorded sample was not assessed because the quality engine produced no result.";
  }
  if (normalized.includes("cancelled")) {
    return "A recorded sample was not assessed because the batch was cancelled before measurement.";
  }
  if (outcome === "INTEGRITY_FAILURE") {
    return "A recorded sample was not assessed because source integrity verification failed.";
  }
  return null;
}

export function safeRunWarning(value: string): string {
  const structured = structuredRunWarning(value);
  if (structured.state === "valid") {
    const summary = safeFailureSummary(structured.outcome, structured.reason);
    if (summary) return summary;
    if (containsAbsoluteFilesystemPath(structured.reason)) {
      return "A recorded sample has a processing warning whose path-bearing detail remains only in private local evidence.";
    }
    return `Recorded sample ${shortIdentity(structured.sampleId)} · ${structured.outcome.replaceAll("_", " ")}: ${structured.reason}`;
  }
  if (structured.state === "invalid") {
    return "A recorded processing warning could not be safely presented. Its full detail remains in private local evidence.";
  }
  const summary = safeFailureSummary("", value);
  if (summary) return summary;
  if (containsAbsoluteFilesystemPath(value)) {
    return "A recorded sample has a processing warning whose path-bearing legacy detail remains only in private local evidence.";
  }
  return "An unstructured recorded warning could not be safely presented. Its full detail remains in private local evidence.";
}

function safeClaimBoundary(value: string): string {
  if (containsAbsoluteFilesystemPath(value)) {
    return "A recorded claim boundary contains path-bearing detail that remains only in private local evidence.";
  }
  return value;
}

function safeDownloadName(request: EvidenceRequest): string {
  const identity = request.evidenceId.replace(/[^a-zA-Z0-9._-]/g, "-");
  return `qualsight-${request.kind}-${identity}.json`;
}

async function readJsonResponse(response: Response, subject: string): Promise<unknown> {
  if (response.ok) return response.json() as Promise<unknown>;
  let detail = `${subject} returned HTTP ${response.status}`;
  try {
    const body: unknown = await response.json();
    if (isRecord(body) && typeof body.detail === "string" && body.detail.trim()) {
      detail = body.detail;
    }
  } catch {
    // The HTTP status remains the authoritative refusal when no JSON body exists.
  }
  throw new Error(detail);
}

async function fetchEvidence(request: EvidenceRequest, signal: AbortSignal): Promise<unknown> {
  if (!request.href.startsWith("/api/")) throw new Error("Evidence navigation is limited to the local QualSight API");
  if (request.kind === "run" && !isProjectRunHref(request.href, request.projectId, request.evidenceId)) {
    throw new Error("Run evidence is not addressed through the active project");
  }
  const payload = await fetch(request.href, { signal, headers: { accept: "application/json" } })
    .then((response) => readJsonResponse(response, request.label));

  if (request.kind === "run") {
    if (!isRecord(payload) || !isRecord(payload.run)) throw new Error("Run evidence has no recorded Run envelope");
    if (stringField(payload.run, "run_id") !== request.evidenceId) throw new Error("Run evidence identity does not match the selected Run");
    if (stringField(payload.run, "project_id") !== request.projectId) throw new Error("Run evidence belongs to a different project");
    return payload;
  }

  if (request.kind === "sample") {
    if (!isProjectResourceHref(request.href, request.projectId, "samples")) throw new Error("Sample evidence is not addressed through the active project");
    if (!isRecord(payload)) throw new Error("Sample evidence is not a recorded sample object");
    if (stringField(payload, "sample_id") !== request.evidenceId) throw new Error("Sample evidence identity does not match the selected sample");
    const datasetId = stringField(payload, "dataset_id");
    if (!datasetId) throw new Error("Sample evidence does not identify its recorded dataset");
    return { sample: payload };
  }

  if (request.kind === "sample-quality") {
    if (!isProjectResourceHref(request.href, request.projectId, "samples") || !request.href.endsWith("/quality")) {
      throw new Error("Sample-quality evidence is not addressed through the active project");
    }
    if (!isRecord(payload)) throw new Error("Sample-quality evidence is not a recorded result object");
    const sample = recordField(payload, "sample");
    if (!sample || stringField(sample, "sample_id") !== request.evidenceId) {
      throw new Error("Sample-quality evidence identity does not match the selected sample");
    }
    const links = recordField(payload, "links");
    const datasetHref = links ? stringField(links, "dataset") : null;
    if (!datasetHref || !isProjectResourceHref(datasetHref, request.projectId, "datasets")) {
      throw new Error("Sample-quality evidence does not identify its local recorded dataset");
    }
    const dataset = await fetch(datasetHref, { signal, headers: { accept: "application/json" } })
      .then((response) => readJsonResponse(response, "Sample-quality dataset evidence"));
    if (!isRecord(dataset) || stringField(dataset, "project_id") !== request.projectId) {
      throw new Error("Sample-quality evidence is not bound to the active project dataset");
    }
    return { quality: payload, dataset };
  }

  if (request.kind === "analysis") {
    if (!isRecord(payload)) throw new Error("Analysis evidence is not a recorded analysis object");
    if (stringField(payload, "analysis_id") !== request.evidenceId) throw new Error("Analysis evidence identity does not match the selected analysis");
    const summary = recordField(payload, "summary");
    const links = summary ? recordField(summary, "links") : null;
    const runHref = links ? stringField(links, "run") : null;
    if (!runHref || !isProjectRunHref(runHref, request.projectId)) throw new Error("Analysis evidence does not identify its project-scoped immutable Run");
    const runEnvelope = await fetch(runHref, { signal, headers: { accept: "application/json" } })
      .then((response) => readJsonResponse(response, "Analysis Run evidence"));
    const run = isRecord(runEnvelope) ? recordField(runEnvelope, "run") : null;
    if (!run || stringField(run, "project_id") !== request.projectId) {
      throw new Error("Analysis Run evidence belongs to a different project");
    }
    return { analysis: payload, run: runEnvelope };
  }

  if (request.kind === "figure") {
    if (!isRecord(payload)) throw new Error("Figure provenance is not a recorded evidence object");
    if (stringField(payload, "figure_id") !== request.evidenceId) throw new Error("Figure provenance identity does not match the selected figure");
    if (stringField(payload, "project_id") !== request.projectId) throw new Error("Figure provenance belongs to a different project");
    return payload;
  }

  if (request.kind === "direct-run") {
    if (!isRecord(payload)) throw new Error("Direct-comparison Run evidence is not a recorded evidence object");
    if (stringField(payload, "run_id") !== request.evidenceId) throw new Error("Direct-comparison Run identity does not match the selected Run");
    if (stringField(payload, "project_id") !== request.projectId) throw new Error("Direct-comparison Run belongs to a different project");
    return payload;
  }

  if (request.kind === "direct-comparison") {
    if (!isRecord(payload)) throw new Error("Direct-comparison status is not a recorded evidence object");
    if (stringField(payload, "comparison_id") !== request.evidenceId) throw new Error("Direct-comparison identity does not match the selected comparison");
    if (stringField(payload, "project_id") !== request.projectId) throw new Error("Direct comparison belongs to a different project");
    return payload;
  }

  if (!Array.isArray(payload)) throw new Error("Audit evidence is not a recorded event list");
  for (const event of payload) {
    if (!isRecord(event)) throw new Error("Audit evidence contains an invalid event");
    const eventProjectId = stringField(event, "project_id");
    if (eventProjectId && eventProjectId !== request.projectId) {
      throw new Error("Audit evidence contains an event from a different project");
    }
    if (stringField(event, "subject_id") !== request.evidenceId) {
      throw new Error("Audit evidence contains an event for a different subject");
    }
  }
  return payload;
}

export function EvidenceOpenButton({ request, onOpen, children, className, testId = "evidence-open" }: EvidenceOpenButtonProps) {
  return <button type="button" className={["evidence-open", className].filter(Boolean).join(" ")} data-testid={testId} data-evidence-kind={request.kind} data-evidence-id={request.evidenceId} data-project-id={request.projectId} data-evidence-href={request.href} onClick={() => onOpen(request)}>{children}</button>;
}

function Fact({ label, value, mono = false }: { label: string; value: unknown; mono?: boolean }) {
  return (
    <>
      <dt>{label}</dt>
      <dd className={mono ? "mono" : undefined}>{scalarText(value)}</dd>
    </>
  );
}

function ArtifactTable({ artifacts }: { artifacts: unknown[] }) {
  if (artifacts.length === 0) return <p className="empty">No artifacts are recorded for this evidence.</p>;
  return (
    <table className="compact">
      <caption>Recorded immutable artifacts</caption>
      <thead><tr><th scope="col">Kind</th><th scope="col">Media type</th><th scope="col">Size</th><th scope="col">SHA-256</th></tr></thead>
      <tbody>
        {artifacts.map((value, index) => {
          const artifact = isRecord(value) ? value : {};
          return (
            <tr key={stringField(artifact, "artifact_id") ?? `artifact-${index}`}>
              <th scope="row">{scalarText(artifact.kind)}</th>
              <td>{scalarText(artifact.media_type)}</td>
              <td>{numberField(artifact, "size_bytes")?.toLocaleString() ?? "Not recorded"}</td>
              <td className="mono">{scalarText(artifact.sha256)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function RunEvidence({ payload }: { payload: unknown }) {
  const envelope = isRecord(payload) ? payload : {};
  const run = recordField(envelope, "run") ?? {};
  const qualityEngine = recordField(run, "quality_engine");
  const matcher = recordField(run, "matcher");
  const repository = recordField(run, "repository");
  const artifacts = Array.isArray(run.artifacts) ? run.artifacts : [];
  const warnings = Array.isArray(run.warnings)
    ? run.warnings.filter((value): value is string => typeof value === "string").map(safeRunWarning)
    : [];
  const limits = Array.isArray(run.provenance_limits)
    ? run.provenance_limits.filter((value): value is string => typeof value === "string").map(safeClaimBoundary)
    : [];
  return (
    <>
      <section aria-labelledby="evidence-run-context"><h4 id="evidence-run-context">Recorded context</h4><dl className="facts">
        <Fact label="Project" value={run.project_id} mono /><Fact label="Run" value={run.run_id} mono />
        <Fact label="Experiment" value={run.experiment_id} mono /><Fact label="Sample" value={envelope.sample_id} mono />
        <Fact label="Status" value={run.status} /><Fact label="Scientific support" value={run.scientific_support} />
      </dl></section>
      <section aria-labelledby="evidence-run-execution"><h4 id="evidence-run-execution">Execution</h4><dl className="facts">
        <Fact label="Engine" value={qualityEngine?.resource_id ?? matcher?.component_id} mono />
        <Fact label="Version" value={qualityEngine?.version ?? matcher?.version} />
        <Fact label="Started" value={run.start_time} mono /><Fact label="Ended" value={run.end_time} mono />
        <Fact label="Runtime seconds" value={run.runtime_seconds} mono /><Fact label="Evidence SHA-256" value={envelope.evidence_sha256} mono />
        <Fact label="Repository commit" value={repository?.commit} mono /><Fact label="Repository state" value={repository?.state} />
      </dl></section>
      <section aria-labelledby="evidence-run-artifacts"><h4 id="evidence-run-artifacts">Artifacts</h4><ArtifactTable artifacts={artifacts} /></section>
      {(warnings.length > 0 || limits.length > 0) && <section aria-labelledby="evidence-run-boundaries"><h4 id="evidence-run-boundaries">Warnings and claim boundaries</h4><ul>{[...warnings, ...limits].map((value, index) => <li key={`${index}:${value}`}>{value}</li>)}</ul></section>}
    </>
  );
}

function SampleEvidence({ payload, projectId }: { payload: unknown; projectId: string }) {
  const wrapper = isRecord(payload) ? payload : {};
  const sample = recordField(wrapper, "sample") ?? {};
  const dataset = recordField(wrapper, "dataset") ?? {};
  return (
    <section aria-labelledby="evidence-sample-record"><h4 id="evidence-sample-record">Recorded sample</h4><dl className="facts">
      <Fact label="Project" value={projectId} mono /><Fact label="Dataset" value={sample.dataset_id} mono />
      <Fact label="Sample" value={sample.sample_id} mono /><Fact label="Subject" value={sample.subject_id} mono />
      <Fact label="Source classification" value={sample.classification ?? dataset.classification} /><Fact label="Media type" value={sample.media_type} />
      <Fact label="Size bytes" value={sample.size_bytes} mono /><Fact label="SHA-256" value={sample.sha256} mono />
      <Fact label="Dataset evidence SHA-256" value={dataset.evidence_sha256} mono />
    </dl></section>
  );
}

function SampleQualityEvidence({ payload, projectId }: { payload: unknown; projectId: string }) {
  const wrapper = isRecord(payload) ? payload : {};
  const quality = recordField(wrapper, "quality") ?? {};
  const sample = recordField(quality, "sample") ?? {};
  const dataset = recordField(wrapper, "dataset") ?? {};
  const results = Array.isArray(quality.results) ? quality.results.filter(isRecord) : [];
  return (
    <>
      <section aria-labelledby="evidence-sample-quality-context"><h4 id="evidence-sample-quality-context">Recorded sample quality</h4><dl className="facts">
        <Fact label="Project" value={projectId} mono /><Fact label="Dataset" value={dataset.dataset_id} mono />
        <Fact label="Sample" value={sample.sample_id} mono /><Fact label="SHA-256" value={sample.sha256} mono />
        <Fact label="Source classification" value={sample.classification ?? dataset.classification} />
        <Fact label="Recorded results" value={results.length} />
      </dl></section>
      <section aria-labelledby="evidence-sample-quality-results"><h4 id="evidence-sample-quality-results">Quality outcomes</h4>
        {results.length === 0 ? <p className="empty">No quality outcomes are recorded for this sample.</p> : <table className="compact"><caption>Recorded quality outcomes</caption><thead><tr><th scope="col">Job</th><th scope="col">Run</th><th scope="col">Outcome</th><th scope="col">UQS</th></tr></thead><tbody>{results.map((result, index) => <tr key={stringField(result, "job_id") ?? `result-${index}`}><th scope="row" className="mono">{scalarText(result.job_id)}</th><td className="mono">{scalarText(result.run_id)}</td><td>{scalarText(result.outcome)}</td><td>{scalarText(result.uqs_scalar)}</td></tr>)}</tbody></table>}
      </section>
    </>
  );
}

const ANALYSIS_FACTS: ReadonlyArray<[string, string]> = [
  ["Run", "run_id"], ["Matrix", "matrix_id"], ["Matcher Run", "matcher_run_id"],
  ["Quality job", "quality_job_id"], ["Quality measure", "quality_measure"],
  ["Pair-quality policy", "pair_quality_policy"], ["Target FMR", "target_fmr"],
  ["Matcher contrasts", "matcher_contrasts"], ["Preprocessing contrasts", "preprocessing_contrasts"],
  ["Leaves", "leaves"], ["Created", "created_at"],
];

function AnalysisEvidence({ payload, projectId }: { payload: unknown; projectId: string }) {
  const wrapper = isRecord(payload) ? payload : {};
  const analysis = recordField(wrapper, "analysis") ?? {};
  const summary = recordField(analysis, "summary") ?? {};
  const artifactHashes = recordField(analysis, "artifact_sha256") ?? {};
  const recordedFacts = ANALYSIS_FACTS.filter(([, key]) => summary[key] !== undefined && summary[key] !== null);
  return (
    <>
      <section aria-labelledby="evidence-analysis-context"><h4 id="evidence-analysis-context">Recorded analysis</h4><dl className="facts">
        <Fact label="Project" value={projectId} mono /><Fact label="Analysis" value={analysis.analysis_id} mono />
        <Fact label="State" value={analysis.state} /><Fact label="Failure" value={analysis.error} />
        {recordedFacts.map(([label, key]) => <Fact key={key} label={label} value={summary[key]} mono={key.endsWith("_id")} />)}
      </dl></section>
      <section aria-labelledby="evidence-analysis-artifacts"><h4 id="evidence-analysis-artifacts">Artifact integrity</h4>
        {Object.keys(artifactHashes).length === 0 ? <p className="empty">No analysis artifact hashes are recorded.</p> : <dl className="facts">{Object.entries(artifactHashes).map(([name, hash]) => <Fact key={name} label={name} value={hash} mono />)}</dl>}
      </section>
    </>
  );
}

function AuditEvidence({ payload }: { payload: unknown }) {
  const events = Array.isArray(payload) ? payload.filter(isRecord) : [];
  return (
    <section aria-labelledby="evidence-audit-events"><h4 id="evidence-audit-events">Recorded audit lineage ({events.length})</h4><ol className="quality-evidence-list">
      {events.map((event, index) => (
        <li key={stringField(event, "audit_event_id") ?? `audit-${index}`}>
          <strong>{scalarText(event.event_type)}</strong>
          <span>{scalarText(event.occurred_at)} · {scalarText(event.actor_id)}</span>
          <span>{scalarText(event.subject_type)} <span className="mono">{scalarText(event.subject_id)}</span></span>
          <span className="mono">{scalarText(event.previous_evidence_sha256)} → {scalarText(event.current_evidence_sha256)}</span>
        </li>
      ))}
    </ol></section>
  );
}

function FigureEvidence({ payload }: { payload: unknown }) {
  const figure = isRecord(payload) ? payload : {};
  const files = recordField(figure, "files") ?? {};
  const sourceRuns = Array.isArray(figure.source_run_ids) ? figure.source_run_ids : [];
  return (
    <>
      <section aria-labelledby="evidence-figure-context"><h4 id="evidence-figure-context">Recorded figure provenance</h4><dl className="facts">
        <Fact label="Project" value={figure.project_id} mono /><Fact label="Figure" value={figure.figure_id} mono />
        <Fact label="Kind" value={figure.kind} /><Fact label="Title" value={figure.title} />
        <Fact label="Created" value={figure.created_at} mono /><Fact label="Source Runs" value={sourceRuns.length} />
      </dl></section>
      <section aria-labelledby="evidence-figure-integrity"><h4 id="evidence-figure-integrity">Artifact integrity</h4>
        {Object.keys(files).length === 0 ? <p className="empty">No figure artifact records are present.</p> : <dl className="facts">{Object.entries(files).map(([format, value]) => <Fact key={format} label={format.toUpperCase()} value={isRecord(value) ? value.sha256 : value} mono />)}</dl>}
      </section>
    </>
  );
}

function DirectComparisonEvidence({ payload, statusOnly }: { payload: unknown; statusOnly: boolean }) {
  const record = isRecord(payload) ? payload : {};
  const pair = recordField(record, "pair") ?? {};
  const execution = recordField(record, "execution") ?? {};
  const result = recordField(record, "result") ?? {};
  return (
    <>
      <section aria-labelledby="evidence-direct-context"><h4 id="evidence-direct-context">{statusOnly ? "Recorded comparison status" : "Recorded direct-comparison Run"}</h4><dl className="facts">
        <Fact label="Project" value={record.project_id} mono /><Fact label="Comparison" value={record.comparison_id} mono />
        <Fact label="Run" value={record.run_id} mono /><Fact label="Pair" value={record.pair_id ?? pair.pair_id} mono />
        <Fact label="State" value={record.state} /><Fact label="Relationship" value={pair.relationship} />
      </dl></section>
      {!statusOnly && <section aria-labelledby="evidence-direct-execution"><h4 id="evidence-direct-execution">Execution and result</h4><dl className="facts">
        <Fact label="Matcher" value={execution.matcher_id} mono /><Fact label="Preprocessing" value={execution.preprocessing_id} mono />
        <Fact label="Device" value={execution.matcher_device} /><Fact label="Runtime seconds" value={execution.runtime_seconds} mono />
        <Fact label="Score" value={result.score} mono /><Fact label="Decision" value={result.decision} />
        <Fact label="Failure" value={result.failure} />
      </dl></section>}
    </>
  );
}

function StructuredEvidence({ request, payload }: { request: EvidenceRequest; payload: unknown }) {
  if (request.kind === "run") return <RunEvidence payload={payload} />;
  if (request.kind === "sample") return <SampleEvidence payload={payload} projectId={request.projectId} />;
  if (request.kind === "sample-quality") return <SampleQualityEvidence payload={payload} projectId={request.projectId} />;
  if (request.kind === "analysis") return <AnalysisEvidence payload={payload} projectId={request.projectId} />;
  if (request.kind === "figure") return <FigureEvidence payload={payload} />;
  if (request.kind === "direct-run") return <DirectComparisonEvidence payload={payload} statusOnly={false} />;
  if (request.kind === "direct-comparison") return <DirectComparisonEvidence payload={payload} statusOnly />;
  return <AuditEvidence payload={payload} />;
}

const DEFAULT_EMPTY_DETAIL = "Select recorded evidence to inspect it here.";

export function EvidenceDetail({ request, activeProjectId, emptyDetail = DEFAULT_EMPTY_DETAIL, onClose }: EvidenceDetailProps) {
  const requestKey = request ? `${request.projectId}\u001f${request.kind}\u001f${request.evidenceId}\u001f${request.href}` : "";
  const [retry, setRetry] = useState(0);
  const [state, setState] = useState<EvidenceState>({ phase: "empty", detail: emptyDetail });
  const sequence = useRef(0);
  const panel = useRef<HTMLElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const returnFocusKey = useRef("");

  useEffect(() => {
    const currentSequence = ++sequence.current;
    if (!request) {
      setState({ phase: "empty", detail: emptyDetail });
      return;
    }
    if (!activeProjectId || request.projectId !== activeProjectId) {
      setState({ phase: "empty", detail: "Evidence was cleared because its project is no longer active." });
      return;
    }
    const controller = new AbortController();
    setState({ phase: "loading", key: requestKey, request });
    void fetchEvidence(request, controller.signal)
      .then((payload) => {
        if (sequence.current !== currentSequence || controller.signal.aborted) return;
        if (request.kind === "audit" && Array.isArray(payload) && payload.length === 0) {
          setState({ phase: "empty", detail: `No audit events are recorded for ${request.label}.` });
          return;
        }
        setState({ phase: "ready", key: requestKey, request, payload });
      })
      .catch((error: unknown) => {
        if (sequence.current !== currentSequence || controller.signal.aborted) return;
        setState({ phase: "error", key: requestKey, request, detail: error instanceof Error ? error.message : String(error) });
      });
    return () => controller.abort();
  }, [activeProjectId, emptyDetail, request, requestKey, retry]);

  const displayedState = useMemo<EvidenceState>(() => {
    if (!request) return { phase: "empty", detail: emptyDetail };
    if (!activeProjectId || request.projectId !== activeProjectId) return { phase: "empty", detail: "Evidence was cleared because its project is no longer active." };
    if ((state.phase === "loading" || state.phase === "ready" || state.phase === "error") && state.key === requestKey) return state;
    return { phase: "loading", key: requestKey, request };
  }, [activeProjectId, emptyDetail, request, requestKey, state]);

  useLayoutEffect(() => {
    if (displayedState.phase === "empty") return;
    if (returnFocusKey.current !== requestKey) {
      returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      returnFocusKey.current = requestKey;
    }
    panel.current?.focus();
  }, [displayedState.phase, requestKey]);

  const close = () => {
    const target = returnFocus.current;
    returnFocusKey.current = "";
    returnFocus.current = null;
    onClose?.();
    window.requestAnimationFrame(() => {
      if (target?.isConnected) target.focus();
    });
  };

  if (displayedState.phase === "empty") {
    return <section className="panel evidence-detail" data-testid="evidence-empty" aria-label="Evidence detail"><p className="empty">{displayedState.detail}</p></section>;
  }
  if (displayedState.phase === "loading") {
    return <section ref={panel} tabIndex={-1} className="panel evidence-detail" data-testid="evidence-loading" data-project-id={displayedState.request.projectId} aria-label="Evidence detail"><p className="status" role="status">Loading {displayedState.request.label}…</p></section>;
  }
  if (displayedState.phase === "error") {
    return <section ref={panel} tabIndex={-1} className="panel evidence-detail" data-testid="evidence-error" data-project-id={displayedState.request.projectId} role="alert"><h3>Evidence unavailable</h3><p>{displayedState.detail}</p><button type="button" className="secondary" onClick={() => setRetry((value) => value + 1)}>Retry evidence</button>{onClose && <button type="button" className="secondary" onClick={close}>Close</button>}</section>;
  }

  return (
    <section
      ref={panel}
      tabIndex={-1}
      className="panel evidence-detail"
      data-testid="evidence-detail"
      data-evidence-kind={displayedState.request.kind}
      data-evidence-id={displayedState.request.evidenceId}
      data-project-id={displayedState.request.projectId}
      aria-labelledby="evidence-detail-heading"
    >
      <header><div><p className="eyebrow">Structured Evidence detail</p><h3 id="evidence-detail-heading">{displayedState.request.label}</h3><p className="mono">{shortIdentity(displayedState.request.evidenceId)}</p></div>{onClose && <button type="button" className="secondary" onClick={close}>Close</button>}</header>
      <div className="evidence-detail-sections"><StructuredEvidence request={displayedState.request} payload={displayedState.payload} /></div>
      <details data-testid="evidence-actions">
        <summary>Evidence actions</summary>
        <p>Use the structured record above for review. The project-scoped public JSON projection remains available for independent inspection; private workstation details are not exported.</p>
        <div className="links"><a href={displayedState.request.href} data-testid="evidence-raw-view">View public Run JSON</a><a href={displayedState.request.href} download={safeDownloadName(displayedState.request)} data-testid="evidence-raw-download">Download public Run JSON</a></div>
      </details>
    </section>
  );
}

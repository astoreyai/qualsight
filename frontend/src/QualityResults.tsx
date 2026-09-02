import { useEffect, useMemo, useState } from "react";
import type { MeasureOut } from "./api";
import { groupRecordedQualityMeasures, qualityMeasureLabel } from "./qualityProfile";

type QualityView = "scalar" | "native" | "failures";

interface ArtifactIdentity {
  path: string;
  sha256: string;
  size_bytes: number;
  mtime_ns: number;
}

interface RunArtifact {
  artifact_id: string;
  kind: string;
  uri: string;
  sha256: string;
  size_bytes: number;
  media_type: string;
}

interface QualityRunRecord {
  run_id: string;
  project_id: string;
  experiment_id: string;
  quality_engine: {
    resource_id: string;
    version: string;
    registry_state: string;
    artifacts: ArtifactIdentity[];
  } | null;
  quality_engine_configuration: ArtifactIdentity | null;
  model_hashes: ArtifactIdentity[];
  start_time: string;
  end_time: string;
  runtime_seconds: number;
  initialization_ms: number;
  assessment_ms: number;
  artifacts: RunArtifact[];
  warnings: string[];
  provenance_limits: string[];
  repository: {
    root: string;
    state: string;
    commit: string | null;
    dirty: boolean;
    status_sha256: string;
  };
  status: string;
  scientific_support: string;
}

interface RunEnvelope {
  evidence_sha256: string;
  sample_id: string | null;
  run: QualityRunRecord;
}

interface AuditEventRecord {
  audit_event_id: string;
  actor_id: string;
  event_type: string;
  occurred_at: string;
  subject_type: string;
  subject_id: string;
  previous_evidence_sha256: string | null;
  current_evidence_sha256: string;
}

interface SampleRecord {
  sample_id: string;
  dataset_id: string;
  subject_id: string;
  sha256: string;
  size_bytes: number;
  media_type: string;
}

interface QualityResultsProps {
  measures: MeasureOut[];
  runId: string;
  runUrl: string;
  sampleId: string | null;
  purposeScope: string;
}

type EvidenceState =
  | { phase: "loading" }
  | { phase: "ready"; envelope: RunEnvelope; audit: AuditEventRecord[]; sample: SampleRecord }
  | { phase: "error"; detail: string };

function valueText(value: number | null): string {
  return value === null ? "unavailable" : String(value);
}

function EvidenceValue({ children }: { children: string | number | boolean | null }) {
  return <dd className="mono">{children === null ? "Not recorded" : String(children)}</dd>;
}

export function QualityResults({
  measures,
  runId,
  runUrl,
  sampleId,
  purposeScope,
}: QualityResultsProps) {
  const [view, setView] = useState<QualityView>("scalar");
  const [evidence, setEvidence] = useState<EvidenceState>({ phase: "loading" });
  const [evidenceRequest, setEvidenceRequest] = useState(0);
  const groups = useMemo(() => groupRecordedQualityMeasures(measures), [measures]);
  const failures = useMemo(() => measures.filter((measure) => measure.failed), [measures]);

  useEffect(() => {
    setView("scalar");
  }, [runId]);

  useEffect(() => {
    const controller = new AbortController();
    setEvidence({ phase: "loading" });
    void Promise.all([
      fetch(runUrl, { signal: controller.signal }),
      fetch(`${runUrl}/audit`, { signal: controller.signal }),
    ])
      .then(async ([runResponse, auditResponse]) => {
        if (!runResponse.ok) {
          throw new Error(`Run evidence returned HTTP ${runResponse.status}`);
        }
        if (!auditResponse.ok) {
          throw new Error(`Run audit returned HTTP ${auditResponse.status}`);
        }
        const envelope = (await runResponse.json()) as RunEnvelope;
        const audit = (await auditResponse.json()) as AuditEventRecord[];
        if (envelope.run.run_id !== runId) {
          throw new Error("Run evidence identity does not match the selected Run");
        }
        if (sampleId && envelope.sample_id && envelope.sample_id !== sampleId) {
          throw new Error("Recorded sample evidence identity does not match the selected sample");
        }
        const recordedSampleId = envelope.sample_id ?? sampleId;
        if (!recordedSampleId) {
          throw new Error("Run evidence does not identify its recorded sample");
        }
        const sampleResponse = await fetch(
          `/api/projects/${encodeURIComponent(envelope.run.project_id)}/samples/${encodeURIComponent(recordedSampleId)}`,
          { signal: controller.signal },
        );
        if (!sampleResponse.ok) {
          throw new Error(`Recorded sample evidence returned HTTP ${sampleResponse.status}`);
        }
        const sample = (await sampleResponse.json()) as SampleRecord;
        if (sample.sample_id !== recordedSampleId || (sampleId && sample.sample_id !== sampleId)) {
          throw new Error("Recorded sample evidence identity does not match the selected sample");
        }
        setEvidence({ phase: "ready", envelope, audit, sample });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setEvidence({
          phase: "error",
          detail: error instanceof Error ? error.message : String(error),
        });
      });
    return () => controller.abort();
  }, [runId, runUrl, sampleId, evidenceRequest]);

  const rawDownloadUrl = useMemo(() => {
    if (evidence.phase !== "ready") return null;
    return URL.createObjectURL(
      new Blob([`${JSON.stringify(evidence.envelope, null, 2)}\n`], {
        type: "application/json",
      }),
    );
  }, [evidence]);

  const chartDownloadUrl = useMemo(() => {
    if (evidence.phase !== "ready") return null;
    const payload = {
      schema_version: "QUALSIGHT_QUALITY_CHART_DATA_V1",
      context: {
        project_id: evidence.envelope.run.project_id,
        dataset_id: evidence.sample.dataset_id,
        sample_id: evidence.sample.sample_id,
        run_id: evidence.envelope.run.run_id,
      },
      scale: {
        component: "scalar",
        unit: "OFIQ scalar [0,100]",
        minimum: 0,
        maximum: 100,
        policy: "NONE",
      },
      measures: groups.flatMap((group) =>
        group.measures.map((measure) => ({
          metric_id: `${measure.name}.scalar`,
          value: measure.scalar,
          unit: "OFIQ scalar [0,100]",
          failed: measure.failed,
          source_run_id: evidence.envelope.run.run_id,
        })),
      ),
    };
    return URL.createObjectURL(
      new Blob([`${JSON.stringify(payload, null, 2)}\n`], {
        type: "application/json",
      }),
    );
  }, [evidence, groups]);

  useEffect(
    () => () => {
      if (rawDownloadUrl) URL.revokeObjectURL(rawDownloadUrl);
    },
    [rawDownloadUrl],
  );

  useEffect(
    () => () => {
      if (chartDownloadUrl) URL.revokeObjectURL(chartDownloadUrl);
    },
    [chartDownloadUrl],
  );

  return (
    <div className="quality-results">
      <div className="quality-view-tabs" role="group" aria-label="Quality result view">
        <button
          type="button"
          className={view === "scalar" ? "active" : ""}
          aria-pressed={view === "scalar"}
          aria-controls="quality-view-panel"
          data-testid="quality-view-scalar"
          onClick={() => setView("scalar")}
        >
          Scalar profile
        </button>
        <button
          type="button"
          className={view === "native" ? "active" : ""}
          aria-pressed={view === "native"}
          aria-controls="quality-view-panel"
          data-testid="quality-view-native"
          onClick={() => setView("native")}
        >
          Native values
        </button>
        <button
          type="button"
          className={view === "failures" ? "active" : ""}
          aria-pressed={view === "failures"}
          aria-controls="quality-view-panel"
          data-testid="quality-view-failures"
          onClick={() => setView("failures")}
        >
          Failures ({failures.length})
        </button>
      </div>

      <div id="quality-view-panel" className="quality-view-panel">
        {view === "scalar" && (
          <div data-testid="measure-table">
            <p className="quality-semantics" data-testid="quality-policy-note">
              Scalar components use the recorded 0–100 OFIQ scale. No pass/fail policy is applied.
            </p>
            {groups.map((group) => (
              <section
                className="quality-group"
                data-testid="quality-group"
                data-quality-group={group.id}
                key={group.id}
                aria-labelledby={`quality-group-${group.id}`}
              >
                <h3 id={`quality-group-${group.id}`}>{group.label}</h3>
                <table className="quality-scalar-table">
                  <caption className="sr-only">{group.label} scalar quality components</caption>
                  <thead>
                    <tr>
                      <th scope="col">Measure</th>
                      <th scope="col">Scalar component</th>
                      <th scope="col">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {group.measures.map((measure) => (
                      <tr
                        key={measure.name}
                        data-testid="quality-scalar-row"
                        data-measure={measure.name}
                        data-scalar={valueText(measure.scalar)}
                        data-status={measure.failed ? "engine failure" : "measured"}
                      >
                        <th scope="row" aria-label={qualityMeasureLabel(measure.name)}>{measure.name}</th>
                        <td>
                          {measure.scalar === null ? (
                            <span className="quality-unavailable">Unavailable</span>
                          ) : (
                            <div
                              className="quality-scalar-bar"
                              role="progressbar"
                              aria-label={`${qualityMeasureLabel(measure.name)}: ${measure.scalar} of 100`}
                              aria-valuemin={0}
                              aria-valuemax={100}
                              aria-valuenow={measure.scalar}
                            >
                              <span
                                className="quality-scalar-fill"
                                data-testid="quality-scalar-fill"
                                style={{ width: `${measure.scalar}%` }}
                              />
                              <span className="quality-scalar-value mono">{measure.scalar}</span>
                            </div>
                          )}
                        </td>
                        <td className="quality-status">{measure.failed ? "Engine failure" : "Measured"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
            ))}
          </div>
        )}

        {view === "native" && (
          <div data-testid="quality-native-view">
            <p className="quality-semantics" data-testid="quality-native-note">
              Native values retain their recorded OFIQ implementation units and semantics. They are not plotted on a shared axis.
            </p>
            <table className="quality-native-table">
              <caption className="sr-only">Recorded native values for all quality measures</caption>
              <thead>
                <tr>
                  <th scope="col">Measure</th>
                  <th scope="col">Recorded native value</th>
                  <th scope="col">Native unit and semantics</th>
                  <th scope="col" data-testid="quality-status-heading">Status</th>
                </tr>
              </thead>
              <tbody>
                {groups.flatMap((group) => group.measures).map((measure) => (
                  <tr
                    key={measure.name}
                    data-testid="quality-native-row"
                    data-measure={measure.name}
                    data-native={valueText(measure.native)}
                    data-native-unit={measure.native_unit}
                    data-status={measure.failed ? "engine failure" : "measured"}
                  >
                    <th scope="row">{qualityMeasureLabel(measure.name)}</th>
                    <td className="mono">{measure.native === null ? "Unavailable" : measure.native}</td>
                    <td>
                      <span className="mono">{measure.native_unit}</span>
                      <small className="quality-native-semantics">{measure.native_semantics}</small>
                    </td>
                    <td className="quality-status">{measure.failed ? "Engine failure" : "Measured"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {view === "failures" && (
          <div className="quality-failures" data-testid="quality-failure-view">
            {failures.length === 0 ? (
              <p className="quality-failure-empty" data-testid="quality-failure-empty">
                0 failed measures. The engine recorded native and scalar values for every measure.
              </p>
            ) : (
              <ul>
                {failures.map((measure) => (
                  <li
                    key={measure.name}
                    data-testid="quality-failure-row"
                    data-measure={measure.name}
                    data-native="unavailable"
                    data-scalar="unavailable"
                  >
                    <strong>{qualityMeasureLabel(measure.name)}</strong>
                    <span>{measure.failure_reason ?? "Engine marked this measure failed; no native or scalar value was recorded."}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>

      {chartDownloadUrl && (
        <div className="quality-chart-actions">
          <a
            href={chartDownloadUrl}
            download={`qualsight-quality-chart-${runId}.json`}
            data-testid="quality-chart-data-download"
          >
            Download chart data
          </a>
          <span>28 recorded scalar components with exact identifiers, units, and Run context.</span>
        </div>
      )}

      <section
        id="structured-evidence"
        className="structured-evidence"
        data-testid="structured-evidence"
        data-state={evidence.phase}
        aria-labelledby="structured-evidence-heading"
      >
        <div className="quality-section-heading">
          <div>
            <p className="quality-eyebrow">Recorded evidence</p>
            <h3 id="structured-evidence-heading">Run provenance</h3>
          </div>
          <span className="quality-run-state">{evidence.phase === "ready" ? evidence.envelope.run.status : evidence.phase}</span>
        </div>
        {evidence.phase === "loading" && <p className="status" role="status">Loading recorded Run evidence…</p>}
        {evidence.phase === "error" && (
          <div className="error" role="alert">
            <p>Recorded Run evidence is unavailable: {evidence.detail}</p>
            <button type="button" className="secondary" onClick={() => setEvidenceRequest((current) => current + 1)}>
              Retry evidence
            </button>
          </div>
        )}
        {evidence.phase === "ready" && (
          <div className="quality-evidence-grid">
            <section data-testid="evidence-project">
              <h4>Project & Run</h4>
              <dl>
                <dt>Project</dt><EvidenceValue>{evidence.envelope.run.project_id}</EvidenceValue>
                <dt>Experiment</dt><EvidenceValue>{evidence.envelope.run.experiment_id}</EvidenceValue>
                <dt>Run</dt><EvidenceValue>{evidence.envelope.run.run_id}</EvidenceValue>
                <dt>Evidence SHA-256</dt><EvidenceValue>{evidence.envelope.evidence_sha256}</EvidenceValue>
              </dl>
            </section>
            <section data-testid="evidence-dataset">
              <h4>Dataset</h4>
              <dl><dt>Recorded dataset</dt><EvidenceValue>{evidence.sample.dataset_id}</EvidenceValue></dl>
            </section>
            <section data-testid="evidence-sample">
              <h4>Sample</h4>
              <dl>
                <dt>Recorded sample</dt><EvidenceValue>{evidence.sample.sample_id}</EvidenceValue>
                <dt>Subject identity</dt><EvidenceValue>{evidence.sample.subject_id}</EvidenceValue>
                <dt>Image SHA-256</dt><EvidenceValue>{evidence.sample.sha256}</EvidenceValue>
                <dt>Size</dt><EvidenceValue>{evidence.sample.size_bytes}</EvidenceValue>
                <dt>Media type</dt><EvidenceValue>{evidence.sample.media_type}</EvidenceValue>
                <dt>Purpose scope</dt><EvidenceValue>{purposeScope}</EvidenceValue>
              </dl>
            </section>
            <section data-testid="evidence-engine">
              <h4>Quality engine</h4>
              {evidence.envelope.run.quality_engine ? (
                <dl>
                  <dt>Resource</dt><EvidenceValue>{evidence.envelope.run.quality_engine.resource_id}</EvidenceValue>
                  <dt>Version</dt><EvidenceValue>{evidence.envelope.run.quality_engine.version}</EvidenceValue>
                  <dt>Registry state</dt><EvidenceValue>{evidence.envelope.run.quality_engine.registry_state}</EvidenceValue>
                </dl>
              ) : <p>Not recorded for this Run.</p>}
            </section>
            <details data-testid="evidence-configuration">
              <summary>Configuration file identity</summary>
              {evidence.envelope.run.quality_engine_configuration ? (
                <dl>
                  <dt>SHA-256</dt><EvidenceValue>{evidence.envelope.run.quality_engine_configuration.sha256}</EvidenceValue>
                  <dt>Size</dt><EvidenceValue>{evidence.envelope.run.quality_engine_configuration.size_bytes}</EvidenceValue>
                  <dt>Modified ns</dt><EvidenceValue>{evidence.envelope.run.quality_engine_configuration.mtime_ns}</EvidenceValue>
                </dl>
              ) : <p>Not recorded for this Run.</p>}
            </details>
            <details data-testid="evidence-models">
              <summary>Models ({evidence.envelope.run.model_hashes.length})</summary>
              <ul className="quality-evidence-list">
                {evidence.envelope.run.model_hashes.map((model) => (
                  <li key={model.sha256}>
                    <strong>Recorded model identity</strong>
                    <span className="mono">sha256 {model.sha256} · {model.size_bytes} bytes · mtime {model.mtime_ns}</span>
                  </li>
                ))}
              </ul>
            </details>
            <section data-testid="evidence-timing">
              <h4>Timing</h4>
              <dl>
                <dt>Started</dt><EvidenceValue>{evidence.envelope.run.start_time}</EvidenceValue>
                <dt>Ended</dt><EvidenceValue>{evidence.envelope.run.end_time}</EvidenceValue>
                <dt>Runtime seconds</dt><EvidenceValue>{evidence.envelope.run.runtime_seconds}</EvidenceValue>
                <dt>Initialization ms</dt><EvidenceValue>{evidence.envelope.run.initialization_ms}</EvidenceValue>
                <dt>Assessment ms</dt><EvidenceValue>{evidence.envelope.run.assessment_ms}</EvidenceValue>
              </dl>
            </section>
            <details data-testid="evidence-artifacts">
              <summary>Artifacts ({evidence.envelope.run.artifacts.length})</summary>
              <ul className="quality-evidence-list">
                {evidence.envelope.run.artifacts.map((artifact) => (
                  <li key={artifact.artifact_id}>
                    <strong>Recorded artifact identity · {artifact.kind}</strong>
                    <span className="mono">sha256 {artifact.sha256} · {artifact.size_bytes} bytes · {artifact.media_type}</span>
                  </li>
                ))}
              </ul>
            </details>
            <details data-testid="evidence-warnings">
              <summary>Warnings ({evidence.envelope.run.warnings.length})</summary>
              {evidence.envelope.run.warnings.length > 0 ? (
                <ul className="quality-evidence-list">{evidence.envelope.run.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
              ) : <p>None recorded.</p>}
            </details>
            <details data-testid="evidence-limits">
              <summary>Provenance limits ({evidence.envelope.run.provenance_limits.length})</summary>
              <ul className="quality-evidence-list">{evidence.envelope.run.provenance_limits.map((limit) => <li key={limit}>{limit}</li>)}</ul>
            </details>
            <details data-testid="evidence-repository">
              <summary>Repository identity</summary>
              <dl>
                <dt>State</dt><EvidenceValue>{evidence.envelope.run.repository.state}</EvidenceValue>
                <dt>Commit</dt><EvidenceValue>{evidence.envelope.run.repository.commit}</EvidenceValue>
                <dt>Dirty</dt><EvidenceValue>{evidence.envelope.run.repository.dirty}</EvidenceValue>
                <dt>Status SHA-256</dt><EvidenceValue>{evidence.envelope.run.repository.status_sha256}</EvidenceValue>
              </dl>
            </details>
            <details data-testid="evidence-audit">
              <summary>Audit lineage ({evidence.audit.length})</summary>
              <ol className="quality-evidence-list">
                {evidence.audit.map((event) => (
                  <li key={event.audit_event_id}>
                    <strong>{event.event_type}</strong>
                    <span>{event.occurred_at} · {event.actor_id} · {event.subject_type} {event.subject_id}</span>
                    <span className="mono">{event.previous_evidence_sha256 ?? "chain origin"} → {event.current_evidence_sha256}</span>
                  </li>
                ))}
              </ol>
            </details>
          </div>
        )}
      </section>

      <details className="raw-evidence-actions" data-testid="raw-evidence-actions">
        <summary>Raw evidence actions</summary>
        <p>Use the structured record above for review. The byte-equivalent API envelope remains available for independent inspection.</p>
        <div className="links">
          <a href={runUrl} data-testid="raw-json-view">View public Run JSON</a>
          {rawDownloadUrl && (
            <a href={rawDownloadUrl} download={`qualsight-run-${runId}.json`} data-testid="raw-json-download">
              Download public Run JSON
            </a>
          )}
        </div>
      </details>
    </div>
  );
}

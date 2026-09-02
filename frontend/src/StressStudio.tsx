import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  api,
  type DetectionGeometryOut,
  type MeasureOut,
  type OfiqLandmarksOut,
} from "./api";

interface NumericParameterContract {
  value_type: "integer" | "number";
  minimum: number;
  maximum: number;
  minimum_exclusive: boolean;
  maximum_exclusive: boolean;
  guidance: string;
}

interface TransformOperator {
  kind: string;
  parameter: string;
  version: string;
  parameter_contract: NumericParameterContract;
}

interface TransformCatalog {
  operators: TransformOperator[];
}

interface DerivedSetRecord {
  derived_set_id: string;
  derived_dataset_id: string;
  source_dataset_id: string;
  source_count: number;
  levels: number;
  derived_count: number;
  links: { detail: string };
}

interface DerivedSampleRow {
  derived_sample_id: string;
  parent_sample_id: string;
  transform: string;
  parameter: number;
  level_index: number;
  sha256: string;
  size_bytes: number;
  chain: Array<Record<string, unknown>>;
  links: {
    image: string;
    detection: string;
    ofiq_landmarks: string;
  };
}

interface DerivedSetDetail extends DerivedSetRecord {
  samples: DerivedSampleRow[];
}

interface QualityJob {
  job_id: string;
  run_id: string | null;
  dataset_id: string;
  state: "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED";
  ended_at: string | null;
  error: string | null;
}

interface QualityMeasureCatalog {
  job_id: string;
  run_id: string;
  parquet_sha256: string;
  measures: string[];
  default_measure: string | null;
}

interface QualityRow extends Record<string, unknown> {
  sample_id: string;
  outcome: string;
  failure_reason: string | null;
}

interface QualityResults {
  job_id: string;
  total: number;
  rows: QualityRow[];
  parquet_sha256: string;
}

export interface StressSource {
  datasetId: string;
  sampleId: string;
  displayName: string;
  sha256: string;
  imageUrl: string;
  measures: MeasureOut[];
}

interface StressStudioProps {
  instanceId: string;
  projectId: string;
  source: StressSource;
  onMessage: (message: string) => void;
  onWorkflowTerminal: (projectId: string) => void;
}

type CatalogState =
  | { phase: "loading" }
  | { phase: "ready"; value: TransformCatalog }
  | { phase: "error"; detail: string };

type DerivedState =
  | { phase: "idle" }
  | { phase: "running" }
  | { phase: "ready"; record: DerivedSetRecord; detail: DerivedSetDetail }
  | { phase: "error"; detail: string };

type QualityState =
  | { phase: "idle" }
  | { phase: "running"; jobId: string }
  | { phase: "ready"; job: QualityJob; catalog: QualityMeasureCatalog; results: QualityResults }
  | { phase: "error"; detail: string; jobId: string | null };

type GeometryEvidence<T> =
  | { state: "READY"; value: T }
  | { state: "FAILED"; detail: string };

interface GeometrySideEvidence {
  detection: GeometryEvidence<DetectionGeometryOut>;
  ofiq: GeometryEvidence<OfiqLandmarksOut>;
}

type GeometryState =
  | { phase: "idle" }
  | { phase: "loading"; selectedSampleId: string }
  | {
      phase: "ready";
      selectedSampleId: string;
      original: GeometrySideEvidence;
      derived: GeometrySideEvidence;
    };

interface TrajectoryPoint {
  sampleId: string;
  operator: string;
  parameter: string;
  scalar: number | null;
  native: number | null;
  failed: boolean;
  failureReason: string | null;
}

function errorDetail(payload: unknown, fallback: string): string {
  if (typeof payload === "object" && payload !== null && "detail" in payload) {
    const detail = (payload as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((item) => {
          if (typeof item === "object" && item !== null && "msg" in item) {
            return String((item as { msg: unknown }).msg);
          }
          return String(item);
        })
        .join("; ");
    }
  }
  return fallback;
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const payload = await response.json().catch(() => null) as unknown;
  if (!response.ok) throw new Error(errorDetail(payload, `Request returned HTTP ${response.status}`));
  return payload as T;
}

function initialParameter(contract: NumericParameterContract): string {
  const middle = (contract.minimum + contract.maximum) / 2;
  return String(contract.value_type === "integer" ? Math.round(middle) : Number(middle.toPrecision(6)));
}

function parameterError(operator: TransformOperator, raw: string): string | null {
  if (raw.trim() === "") return `${operator.parameter} is required.`;
  const value = Number(raw);
  if (!Number.isFinite(value)) return `${operator.parameter} must be finite.`;
  const contract = operator.parameter_contract;
  if (contract.value_type === "integer" && !Number.isInteger(value)) {
    return `${operator.parameter} must be an integer.`;
  }
  const lower = contract.minimum_exclusive ? value > contract.minimum : value >= contract.minimum;
  const upper = contract.maximum_exclusive ? value < contract.maximum : value <= contract.maximum;
  return lower && upper ? null : `${operator.parameter} is outside the admitted range.`;
}

function recordedNumber(row: QualityRow, key: string): number | null {
  const value = row[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function sourcePoint(source: StressSource, measureName: string): TrajectoryPoint {
  const measure = source.measures.find((item) => item.name === measureName);
  return {
    sampleId: source.sampleId,
    operator: "original",
    parameter: "original",
    scalar: measure?.scalar ?? null,
    native: measure?.native ?? null,
    failed: measure?.failed ?? true,
    failureReason: measure?.failure_reason ?? null,
  };
}

function derivedPoint(row: QualityRow, derived: DerivedSampleRow, measureName: string): TrajectoryPoint {
  const failed = row[`${measureName}.failed`] === true;
  return {
    sampleId: row.sample_id,
    operator: derived.transform,
    parameter: String(derived.parameter),
    scalar: recordedNumber(row, `${measureName}.scalar`),
    native: recordedNumber(row, `${measureName}.native`),
    failed,
    failureReason: failed ? (row.failure_reason ?? "The recorded OFIQ result marks this measure as failed.") : null,
  };
}

function shortId(value: string): string {
  return value.length > 16 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;
}

function settledGeometry<T>(result: PromiseSettledResult<T>): GeometryEvidence<T> {
  return result.status === "fulfilled"
    ? { state: "READY", value: result.value }
    : { state: "FAILED", detail: result.reason instanceof Error ? result.reason.message : String(result.reason) };
}

function GeometrySide({
  side,
  title,
  sampleId,
  image,
  evidence,
}: {
  side: "original" | "derived";
  title: string;
  sampleId: string;
  image: string;
  evidence: GeometrySideEvidence;
}) {
  const dimensions = evidence.detection.state === "READY"
    ? evidence.detection.value.image
    : evidence.ofiq.state === "READY"
      ? evidence.ofiq.value.image
      : null;
  return (
    <article className="stress-geometry-side" data-testid="stress-geometry-side" data-side={side} data-sample-id={sampleId}>
      <header><span>{side === "original" ? "Source" : "Selected derivative"}</span><h4>{title}</h4><code>{shortId(sampleId)}</code></header>
      <figure className="stress-geometry-canvas">
        <img src={image} alt={`${title}, with separately labelled SCRFD and OFIQ geometry`} />
        {dimensions && evidence.detection.state === "READY" && (
          <svg
            className="stress-geometry-overlay stress-scrfd-overlay"
            data-testid="stress-scrfd-overlay"
            viewBox={`0 0 ${dimensions.width} ${dimensions.height}`}
            preserveAspectRatio="none"
            aria-label={`SCRFD geometry for ${title}`}
          >
            {evidence.detection.value.detections.map((detection, index) => (
              <g key={index}>
                <rect
                  x={detection.bbox[0] ?? 0}
                  y={detection.bbox[1] ?? 0}
                  width={(detection.bbox[2] ?? 0) - (detection.bbox[0] ?? 0)}
                  height={(detection.bbox[3] ?? 0) - (detection.bbox[1] ?? 0)}
                />
                {detection.landmarks.map((point, pointIndex) => (
                  <circle key={pointIndex} cx={point[0] ?? 0} cy={point[1] ?? 0} r={Math.max(1.5, dimensions.width / 150)} />
                ))}
              </g>
            ))}
          </svg>
        )}
        {dimensions && evidence.ofiq.state === "READY" && (
          <svg
            className="stress-geometry-overlay stress-ofiq-overlay"
            data-testid="stress-ofiq-overlay"
            viewBox={`0 0 ${dimensions.width} ${dimensions.height}`}
            preserveAspectRatio="none"
            aria-label={`OFIQ geometry for ${title}`}
          >
            {evidence.ofiq.value.faces.map((face, index) => (
              <rect key={index} x={face.x} y={face.y} width={face.width} height={face.height} />
            ))}
            {evidence.ofiq.value.landmarks.map((point, index) => (
              <circle key={index} cx={point[0] ?? 0} cy={point[1] ?? 0} r={Math.max(1.2, dimensions.width / 220)} />
            ))}
          </svg>
        )}
      </figure>
      <div className="stress-geometry-legend">
        {evidence.detection.state === "READY" ? (
          <p data-testid="stress-scrfd-label"><span className="stress-legend-key scrfd" />{evidence.detection.value.label}</p>
        ) : (
          <p className="error" data-testid="stress-scrfd-label">SCRFD unavailable: {evidence.detection.detail}</p>
        )}
        {evidence.ofiq.state === "READY" ? (
          <p data-testid="stress-ofiq-label"><span className="stress-legend-key ofiq" />{evidence.ofiq.value.label} ({evidence.ofiq.value.landmark_type})</p>
        ) : (
          <p className="error" data-testid="stress-ofiq-label">OFIQ geometry unavailable: {evidence.ofiq.detail}</p>
        )}
      </div>
    </article>
  );
}

function TrajectoryChart({ idScope, label, points }: { idScope: string; label: string; points: TrajectoryPoint[] }) {
  const measured = points.filter((point) => point.scalar !== null);
  const derivedCount = Math.max(points.length - 1, 0);
  const width = 760;
  const height = 220;
  const left = 42;
  const right = 18;
  const top = 16;
  const bottom = 42;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const x = (index: number) => left + (points.length === 1 ? 0 : (index / (points.length - 1)) * plotWidth);
  const y = (value: number) => top + ((100 - value) / 100) * plotHeight;
  let hasStarted = false;
  const path = points.map((point, index) => {
    if (point.scalar === null) {
      hasStarted = false;
      return null;
    }
    const command = hasStarted ? "L" : "M";
    hasStarted = true;
    return `${command}${x(index)},${y(point.scalar)}`;
  }).filter((value): value is string => value !== null).join(" ");
  const chartId = `stress-chart-${idScope}-${label.replace(/[^a-zA-Z0-9_-]+/g, "-")}`;

  return (
    <figure className="stress-chart" aria-labelledby={chartId}>
      <figcaption id={chartId}>{label} · OFIQ scalar [0,100]</figcaption>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${label} scalar response for the original and ${derivedCount} derived ${derivedCount === 1 ? "image" : "images"}`}>
        {[0, 25, 50, 75, 100].map((tick) => (
          <g key={tick}>
            <line x1={left} x2={width - right} y1={y(tick)} y2={y(tick)} className="stress-grid-line" />
            <text x={left - 8} y={y(tick) + 4} textAnchor="end">{tick}</text>
          </g>
        ))}
        {path && <path d={path} className="stress-series-line" />}
        {points.map((point, index) => point.scalar === null ? (
          <g key={point.sampleId} aria-label={`${point.operator}: no recorded scalar`}>
            <line x1={x(index) - 4} x2={x(index) + 4} y1={top + plotHeight / 2 - 4} y2={top + plotHeight / 2 + 4} className="stress-failed-mark" />
            <line x1={x(index) + 4} x2={x(index) - 4} y1={top + plotHeight / 2 - 4} y2={top + plotHeight / 2 + 4} className="stress-failed-mark" />
          </g>
        ) : (
          <circle key={point.sampleId} cx={x(index)} cy={y(point.scalar)} r="5" className="stress-series-point">
            <title>{point.operator}: {point.scalar}</title>
          </circle>
        ))}
        {points.map((point, index) => (
          <text key={`${point.sampleId}-label`} x={x(index)} y={height - 16} textAnchor="middle">{index === 0 ? "Original" : point.operator.replace("gaussian_blur", "blur")}</text>
        ))}
      </svg>
      <p className="hint">{measured.length} of {points.length} images have a recorded {label} scalar. Crosses mark unavailable values.</p>
    </figure>
  );
}

function TrajectoryTable({ series, points }: { series: string; points: TrajectoryPoint[] }) {
  return (
    <div className="table-wrap">
      <table className="compact stress-trajectory-table">
        <caption>{series} recorded values</caption>
        <thead><tr><th scope="col">Image</th><th scope="col">Parameter</th><th scope="col">Scalar</th><th scope="col">Native</th><th scope="col">Outcome</th></tr></thead>
        <tbody>
          {points.map((point) => (
            <tr
              key={`${series}-${point.sampleId}`}
              data-testid="stress-trajectory-point"
              data-series={series}
              data-sample-id={point.sampleId}
              data-operator={point.operator}
              data-parameter={point.parameter}
              data-scalar={point.scalar === null ? "unavailable" : String(point.scalar)}
              data-native={point.native === null ? "unavailable" : String(point.native)}
              data-status={point.failed ? "failed" : "measured"}
              data-failure-reason={point.failureReason ?? ""}
            >
              <th scope="row">{point.operator === "original" ? "Original" : point.operator}</th>
              <td>{point.parameter}</td>
              <td>{point.scalar ?? "Unavailable"}</td>
              <td>{point.native ?? "Unavailable"}</td>
              <td>{point.failed ? point.failureReason ?? "Measure failed" : "Measured"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function StressStudio({ instanceId, projectId, source, onMessage, onWorkflowTerminal }: StressStudioProps) {
  const [catalog, setCatalog] = useState<CatalogState>({ phase: "loading" });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [parameters, setParameters] = useState<Record<string, string>>({});
  const [derived, setDerived] = useState<DerivedState>({ phase: "idle" });
  const [quality, setQuality] = useState<QualityState>({ phase: "idle" });
  const [geometrySampleId, setGeometrySampleId] = useState("");
  const [geometry, setGeometry] = useState<GeometryState>({ phase: "idle" });
  const [component, setComponent] = useState("Sharpness");
  const sourceEpoch = useRef(0);
  const catalogSequence = useRef(0);
  const geometrySequence = useRef(0);
  const qualitySequence = useRef(0);
  const sourceKey = `${projectId}\u001f${source.datasetId}\u001f${source.sampleId}\u001f${source.sha256}`;

  const loadCatalog = useCallback(async () => {
    const sequence = ++catalogSequence.current;
    setCatalog({ phase: "loading" });
    try {
      const value = await requestJson<TransformCatalog>("/api/transforms");
      if (sequence !== catalogSequence.current) return;
      setCatalog({ phase: "ready", value });
      setSelected(new Set(value.operators.map((item) => item.kind)));
      setParameters(Object.fromEntries(value.operators.map((item) => [item.kind, initialParameter(item.parameter_contract)])));
    } catch (error) {
      if (sequence !== catalogSequence.current) return;
      setCatalog({ phase: "error", detail: error instanceof Error ? error.message : String(error) });
    }
  }, []);

  useEffect(() => {
    void loadCatalog();
    return () => { catalogSequence.current += 1; };
  }, [loadCatalog]);

  useEffect(() => {
    sourceEpoch.current += 1;
    geometrySequence.current += 1;
    qualitySequence.current += 1;
    setDerived({ phase: "idle" });
    setQuality({ phase: "idle" });
    setGeometrySampleId("");
    setGeometry({ phase: "idle" });
    return () => { sourceEpoch.current += 1; };
  }, [sourceKey]);

  const selectedGeometrySample = derived.phase === "ready"
    ? derived.detail.samples.find((sample) => sample.derived_sample_id === geometrySampleId) ?? null
    : null;

  useEffect(() => {
    if (!selectedGeometrySample) {
      geometrySequence.current += 1;
      setGeometry({ phase: "idle" });
      return;
    }
    const sequence = ++geometrySequence.current;
    const epoch = sourceEpoch.current;
    const selectedSampleId = selectedGeometrySample.derived_sample_id;
    setGeometry({ phase: "loading", selectedSampleId });
    void Promise.allSettled([
      api.detectionForSample(projectId, source.sampleId),
      api.ofiqLandmarksForSample(projectId, source.sampleId),
      api.detectionForSample(projectId, selectedSampleId),
      api.ofiqLandmarksForSample(projectId, selectedSampleId),
    ]).then(([originalDetection, originalOfiq, derivedDetection, derivedOfiq]) => {
      if (sequence !== geometrySequence.current || epoch !== sourceEpoch.current) return;
      setGeometry({
        phase: "ready",
        selectedSampleId,
        original: {
          detection: settledGeometry(originalDetection),
          ofiq: settledGeometry(originalOfiq),
        },
        derived: {
          detection: settledGeometry(derivedDetection),
          ofiq: settledGeometry(derivedOfiq),
        },
      });
    });
    return () => { geometrySequence.current += 1; };
  }, [projectId, selectedGeometrySample, source.sampleId]);

  const operators = catalog.phase === "ready" ? catalog.value.operators : [];
  const validation = useMemo(
    () => Object.fromEntries(operators.map((item) => [item.kind, selected.has(item.kind) ? parameterError(item, parameters[item.kind] ?? "") : null])),
    [operators, parameters, selected],
  );
  const selectedOperators = operators.filter((item) => selected.has(item.kind));
  const invalid = selectedOperators.find((item) => validation[item.kind] !== null);
  const createDisabledReason = catalog.phase !== "ready"
    ? "Wait for the admitted operator contracts to load."
    : selectedOperators.length === 0
      ? "Select at least one admitted operator."
      : invalid
        ? validation[invalid.kind]
        : derived.phase === "running"
          ? "The derived set is being recorded."
          : null;

  const createDerived = async () => {
    if (createDisabledReason || catalog.phase !== "ready") return;
    const epoch = sourceEpoch.current;
    qualitySequence.current += 1;
    setDerived({ phase: "running" });
    setQuality({ phase: "idle" });
    try {
      const transforms = selectedOperators.map((item) => ({ kind: item.kind, parameters: [Number(parameters[item.kind])] }));
      const record = await requestJson<DerivedSetRecord>(
        `/api/projects/${encodeURIComponent(projectId)}/datasets/${encodeURIComponent(source.datasetId)}/transformations`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ selection: "sample_ids", sample_ids: [source.sampleId], transforms }),
        },
      );
      const detail = await requestJson<DerivedSetDetail>(record.links.detail);
      if (epoch !== sourceEpoch.current) return;
      setDerived({ phase: "ready", record, detail });
      setGeometrySampleId(detail.samples[0]?.derived_sample_id ?? "");
      onMessage(`Recorded ${record.derived_count} derived images from ${source.displayName}.`);
      onWorkflowTerminal(projectId);
    } catch (error) {
      if (epoch !== sourceEpoch.current) return;
      setDerived({ phase: "error", detail: error instanceof Error ? error.message : String(error) });
    }
  };

  const runQuality = async () => {
    if (derived.phase !== "ready") return;
    const epoch = sourceEpoch.current;
    const sequence = ++qualitySequence.current;
    const derivedCount = derived.record.derived_count;
    setQuality({ phase: "running", jobId: "admitting" });
    let admittedJobId: string | null = null;
    try {
      const admitted = await requestJson<QualityJob>(
        `/api/projects/${encodeURIComponent(projectId)}/datasets/${encodeURIComponent(derived.record.derived_dataset_id)}/quality-batch`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ selection: { kind: "all" }, chunks: 1 }),
        },
      );
      admittedJobId = admitted.job_id;
      if (epoch !== sourceEpoch.current || sequence !== qualitySequence.current) return;
      setQuality({ phase: "running", jobId: admitted.job_id });
      let job = admitted;
      const deadline = Date.now() + 360_000;
      while (!job.ended_at || !["COMPLETED", "FAILED", "CANCELLED"].includes(job.state)) {
        if (Date.now() >= deadline) throw new Error("Quality job did not finish within six minutes.");
        await new Promise((resolve) => window.setTimeout(resolve, 750));
        if (epoch !== sourceEpoch.current || sequence !== qualitySequence.current) return;
        job = await requestJson<QualityJob>(`/api/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(admitted.job_id)}`);
      }
      if (job.state !== "COMPLETED") throw new Error(job.error ?? `Quality job ended as ${job.state}.`);
      const [measureCatalog, results] = await Promise.all([
        requestJson<QualityMeasureCatalog>(`/api/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(job.job_id)}/quality-measures`),
        requestJson<QualityResults>(`/api/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(job.job_id)}/results?limit=20000`),
      ]);
      if (epoch !== sourceEpoch.current || sequence !== qualitySequence.current) return;
      if (results.total !== derivedCount) {
        throw new Error(`Quality evidence recorded ${results.total} rows for ${derivedCount} derived images.`);
      }
      const componentNames = measureCatalog.measures.filter((name) => name !== "UnifiedQualityScore");
      setComponent((current) => componentNames.includes(current) ? current : (componentNames[0] ?? ""));
      setQuality({ phase: "ready", job, catalog: measureCatalog, results });
      onMessage(`OFIQ recorded ${results.total} derived-image quality rows.`);
      onWorkflowTerminal(projectId);
    } catch (error) {
      if (epoch !== sourceEpoch.current || sequence !== qualitySequence.current) return;
      setQuality({ phase: "error", detail: error instanceof Error ? error.message : String(error), jobId: admittedJobId });
    }
  };

  const trajectory = useCallback((measureName: string): TrajectoryPoint[] => {
    const baseline = sourcePoint(source, measureName);
    if (quality.phase !== "ready" || derived.phase !== "ready") return [baseline];
    const rows = new Map(quality.results.rows.map((row) => [row.sample_id, row]));
    return [
      baseline,
      ...derived.detail.samples.map((sample) => {
        const row = rows.get(sample.derived_sample_id);
        return row ? derivedPoint(row, sample, measureName) : {
          sampleId: sample.derived_sample_id,
          operator: sample.transform,
          parameter: String(sample.parameter),
          scalar: null,
          native: null,
          failed: true,
          failureReason: "No quality row was recorded for this derived image.",
        };
      }),
    ];
  }, [derived, quality, source]);

  const uqsPoints = trajectory("UnifiedQualityScore");
  const componentPoints = component ? trajectory(component) : [];
  const componentMeasure = source.measures.find((item) => item.name === component);

  return (
    <section className="stress-studio" data-testid="stress-studio" data-source-sample-id={source.sampleId} aria-labelledby={`stress-studio-title-${instanceId}`}>
      <header className="stress-studio-header">
        <div>
          <p className="eyebrow">Single-image stress test</p>
          <h2 id={`stress-studio-title-${instanceId}`}>Degrade one recorded image, then measure the response</h2>
          <p>Every output remains linked to <strong>{source.displayName}</strong>, its source hash, the exact operator version, and the recorded OFIQ Run.</p>
        </div>
        <div className="stress-source-identity" aria-label="Active source identity">
          <span>Active sample</span>
          <strong>{shortId(source.sampleId)}</strong>
          <code>{source.sha256.slice(0, 12)}…</code>
        </div>
      </header>

      <p className="callout" data-testid="stress-claim-scope">
        Descriptive single-sample response only. This workspace shows how one recorded image changes under controlled processing; it does not compute population error-rate estimates or correctness claims.
      </p>

      {catalog.phase === "loading" && (
        <div className="stress-operator-skeleton" role="status" aria-label="Loading admitted transform contracts">
          <span /><span /><span /><span /><span /><span /><span />
        </div>
      )}
      {catalog.phase === "error" && (
        <div className="error" role="alert">
          Operator contracts are unavailable: {catalog.detail}.{" "}
          <button type="button" className="secondary" onClick={() => void loadCatalog()} data-testid="stress-catalog-retry">Retry operator contracts</button>
        </div>
      )}
      {catalog.phase === "ready" && catalog.value.operators.length === 0 && (
        <div className="empty-state"><h3>No admitted transformations</h3><p>This installation has no controlled real-input operator available.</p></div>
      )}
      {catalog.phase === "ready" && catalog.value.operators.length > 0 && (
        <fieldset className="stress-operators" disabled={derived.phase === "running"}>
          <legend>1. Choose controlled transformations</legend>
          <p className="hint">The names, versions, units, and bounds below come from the live backend contract.</p>
          <div className="stress-operator-grid">
            {catalog.value.operators.map((operator) => {
              const checked = selected.has(operator.kind);
              const issue = validation[operator.kind];
              const contract = operator.parameter_contract;
              return (
                <div
                  className={`stress-operator${checked ? " selected" : ""}`}
                  key={operator.kind}
                  data-testid="stress-operator"
                  data-operator={operator.kind}
                  data-parameter-name={operator.parameter}
                  data-version={operator.version}
                  data-parameter-contract={JSON.stringify(contract)}
                >
                  <label className="stress-operator-toggle" htmlFor={`stress-toggle-${instanceId}-${operator.kind}`}>
                    <input
                      id={`stress-toggle-${instanceId}-${operator.kind}`}
                      type="checkbox"
                      checked={checked}
                      onChange={(event) => setSelected((current) => {
                        const next = new Set(current);
                        if (event.target.checked) next.add(operator.kind); else next.delete(operator.kind);
                        return next;
                      })}
                      data-testid={`stress-operator-toggle-${operator.kind}`}
                    />
                    <span><strong>{operator.kind.replace("gaussian_blur", "Gaussian blur")}</strong><small>v{operator.version}</small></span>
                  </label>
                  <label htmlFor={`stress-parameter-${instanceId}-${operator.kind}`}>{operator.parameter}</label>
                  <input
                    id={`stress-parameter-${instanceId}-${operator.kind}`}
                    type="number"
                    value={parameters[operator.kind] ?? ""}
                    min={contract.minimum}
                    max={contract.maximum}
                    step={contract.value_type === "integer" ? 1 : "any"}
                    disabled={!checked}
                    aria-invalid={checked && issue !== null}
                    aria-describedby={`stress-help-${instanceId}-${operator.kind}`}
                    onChange={(event) => setParameters((current) => ({ ...current, [operator.kind]: event.target.value }))}
                    data-testid={`stress-parameter-${operator.kind}`}
                  />
                  <small id={`stress-help-${instanceId}-${operator.kind}`} className={checked && issue ? "error" : "hint"}>{checked && issue ? issue : contract.guidance}</small>
                </div>
              );
            })}
          </div>
        </fieldset>
      )}

      <div className="stress-actions">
        <button type="button" disabled={createDisabledReason !== null} title={createDisabledReason ?? "Record the selected derived images."} onClick={() => void createDerived()} data-testid="stress-run">
          {derived.phase === "running" ? "Recording derived set…" : "Create derived set"}
        </button>
        <span className={createDisabledReason ? "hint" : "status"}>{createDisabledReason ?? "Ready to derive from the exact active sample."}</span>
      </div>
      {derived.phase === "error" && <p className="error" role="alert">Derived set failed: {derived.detail}. Review the selected values and retry.</p>}

      {derived.phase === "ready" && (
        <section className="stress-results" aria-labelledby={`stress-gallery-title-${instanceId}`}>
          <div className="section-heading">
            <div><p className="eyebrow">Recorded derived set</p><h3 id={`stress-gallery-title-${instanceId}`}>2. Inspect original and derived bytes</h3></div>
            <code>{shortId(derived.record.derived_set_id)}</code>
          </div>
          <div className="stress-gallery" data-testid="stress-gallery">
            <article className="stress-gallery-item" data-testid="stress-gallery-item" data-kind="original" data-sample-id={source.sampleId} data-sha256={source.sha256}>
              <img src={source.imageUrl} alt={`Original working sample ${source.displayName}`} />
              <div><strong>Original</strong><span>Recorded source</span><code>{source.sha256.slice(0, 12)}…</code></div>
            </article>
            {derived.detail.samples.map((sample) => (
              <article className="stress-gallery-item" key={sample.derived_sample_id} data-testid="stress-gallery-item" data-kind={sample.transform} data-sample-id={sample.derived_sample_id} data-sha256={sample.sha256}>
                <img src={sample.links.image} alt={`${source.displayName} after ${sample.transform} ${sample.parameter}`} />
                <div><strong>{sample.transform}</strong><span>{sample.parameter}</span><code>{sample.sha256.slice(0, 12)}…</code></div>
              </article>
            ))}
          </div>
          <section className="stress-geometry" aria-labelledby={`stress-geometry-title-${instanceId}`}>
            <div className="section-heading">
              <div>
                <p className="eyebrow">Recorded detector evidence</p>
                <h3 id={`stress-geometry-title-${instanceId}`}>3. Compare source and derived face geometry</h3>
                <p>SCRFD-10G five-point geometry and OFIQ ADNet-98 geometry remain separate, labelled overlays.</p>
              </div>
            </div>
            <label className="stress-geometry-select" htmlFor={`stress-geometry-select-${instanceId}`}>
              Derived image
              <select
                id={`stress-geometry-select-${instanceId}`}
                value={geometrySampleId}
                onChange={(event) => setGeometrySampleId(event.target.value)}
                data-testid="stress-geometry-select"
              >
                {derived.detail.samples.map((sample) => (
                  <option key={sample.derived_sample_id} value={sample.derived_sample_id}>
                    {sample.transform} · {sample.parameter}
                  </option>
                ))}
              </select>
            </label>
            {geometry.phase === "loading" && (
              <div
                className="stress-quality-skeleton"
                role="status"
                data-testid="stress-geometry-status"
                data-selected-sample-id={geometry.selectedSampleId}
                data-state="LOADING"
              >
                <strong>Running real geometry extractors…</strong>
                <span>The selected sample identity is fixed while both recorded images are processed.</span>
              </div>
            )}
            {geometry.phase === "ready" && selectedGeometrySample && (
              <div
                className="stress-geometry-grid"
                data-testid="stress-geometry-status"
                data-selected-sample-id={geometry.selectedSampleId}
                data-state="READY"
              >
                <GeometrySide
                  side="original"
                  title={`Original ${source.displayName}`}
                  sampleId={source.sampleId}
                  image={source.imageUrl}
                  evidence={geometry.original}
                />
                <GeometrySide
                  side="derived"
                  title={`${selectedGeometrySample.transform} ${selectedGeometrySample.parameter}`}
                  sampleId={selectedGeometrySample.derived_sample_id}
                  image={selectedGeometrySample.links.image}
                  evidence={geometry.derived}
                />
              </div>
            )}
          </section>
          <div className="stress-actions">
            <button type="button" onClick={() => void runQuality()} disabled={quality.phase === "running"} data-testid="stress-run-ofiq">
              {quality.phase === "running" ? "Running OFIQ…" : "Run OFIQ on derived set"}
            </button>
            <button type="button" className="secondary" onClick={() => { geometrySequence.current += 1; qualitySequence.current += 1; setGeometrySampleId(""); setGeometry({ phase: "idle" }); setDerived({ phase: "idle" }); setQuality({ phase: "idle" }); }} data-testid="stress-clear">Clear workspace results</button>
          </div>
        </section>
      )}

      {quality.phase === "running" && (
        <div className="stress-quality-skeleton" role="status" data-testid="stress-quality-status" data-job-id={quality.jobId} data-state="RUNNING">
          <strong>OFIQ is measuring {derived.phase === "ready" ? derived.record.derived_count : 0} recorded derived {derived.phase === "ready" && derived.record.derived_count === 1 ? "image" : "images"}.</strong><span>The gallery remains available while the immutable batch Run completes.</span>
        </div>
      )}
      {quality.phase === "error" && (
        <div className="error" role="alert" data-testid="stress-quality-status" data-job-id={quality.jobId ?? ""} data-state="FAILED">
          OFIQ response unavailable: {quality.detail}.{" "}
          <button type="button" className="secondary" onClick={() => void runQuality()}>Retry OFIQ</button>
        </div>
      )}
      {quality.phase === "ready" && (
        <section className="stress-quality" aria-labelledby={`stress-quality-title-${instanceId}`}>
          <div className="section-heading" data-testid="stress-quality-status" data-job-id={quality.job.job_id} data-state="COMPLETED">
            <div><p className="eyebrow">Recorded OFIQ batch</p><h3 id={`stress-quality-title-${instanceId}`}>4. Compare the measured response</h3></div>
            <div className="evidence-identity"><span>Run {quality.job.run_id ? shortId(quality.job.run_id) : "unavailable"}</span><code>{quality.catalog.parquet_sha256.slice(0, 12)}…</code></div>
          </div>
          <TrajectoryChart idScope={instanceId} label="UnifiedQualityScore" points={uqsPoints} />
          <TrajectoryTable series="UnifiedQualityScore" points={uqsPoints} />
          <label className="stress-measure-select" htmlFor={`stress-measure-${instanceId}`}>
            Component trajectory
            <select id={`stress-measure-${instanceId}`} value={component} onChange={(event) => setComponent(event.target.value)} data-testid="stress-measure">
              {quality.catalog.measures.filter((name) => name !== "UnifiedQualityScore").map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          </label>
          {component && (
            <>
              <p className="hint">Native values remain in {componentMeasure?.native_unit ?? "the recorded OFIQ native representation"}; they are listed, not plotted on the shared scalar axis.</p>
              <TrajectoryChart idScope={instanceId} label={component} points={componentPoints} />
              <TrajectoryTable series={component} points={componentPoints} />
            </>
          )}
        </section>
      )}
    </section>
  );
}

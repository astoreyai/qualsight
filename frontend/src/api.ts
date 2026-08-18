/** Typed client for the local QualSight control plane (mirrors backend/src/qualsight/api/app.py). */

export interface ResourceOut {
  resource_id: string;
  kind: string;
  name: string;
  state: string;
  version: string | null;
  approved_scopes: string[];
  third_party_rights_status: string;
  research_use_status: string;
  data_classification: string;
  consumers: string[];
}

export interface HealthOut {
  status: "ok";
  application_version: string;
  registry_sha256: string;
  registry_id: string;
  engine: ResourceOut;
  database_path: string;
}

export interface RegisteredSample {
  resource_id: string;
  resource_state: string;
  approved_scopes: string[];
  path: string;
  sha256: string;
  size_bytes: number;
  media_type: string;
}

export interface MeasureOut {
  name: string;
  native: number | null;
  scalar: number | null;
  failed: boolean;
}

export interface ProvenanceLinks {
  run: string;
  project: string;
  experiment: string;
  sample: string;
  audit_events: string;
}

export interface AssessResponse {
  kind: "ENGINE_MEASUREMENT";
  run_id: string;
  evidence_sha256: string;
  engine_resource_id: string;
  engine_version: string;
  engine_state: string;
  purpose_scope: string;
  image_path: string;
  image_sha256: string;
  initialization_ms: number;
  assessment_ms: number;
  runtime_seconds: number;
  measures: MeasureOut[];
  failed_measures: string[];
  warnings: string[];
  policy: "NONE_APPLIED";
  provenance: ProvenanceLinks;
}

export interface AssessmentFailure {
  error: "ASSESSMENT_FAILED";
  image_path: string;
  engine_version: string;
  exit_code: number;
  reason: string;
  stderr: string;
}

export interface RunSummaryOut {
  run_id: string;
  project_id: string;
  experiment_id: string;
  sample_id: string;
  status: string;
  engine_resource_id: string;
  engine_version: string;
  start_time: string;
  end_time: string;
  evidence_sha256: string;
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
    public readonly failure: AssessmentFailure | null = null,
  ) {
    super(detail);
  }
}

async function readJson<T>(response: Response): Promise<T> {
  if (response.ok) {
    return (await response.json()) as T;
  }
  let detail = `${response.status} ${response.statusText}`;
  let failure: AssessmentFailure | null = null;
  try {
    const body = (await response.json()) as { detail?: unknown; error?: string };
    if (body && body.error === "ASSESSMENT_FAILED") {
      failure = body as unknown as AssessmentFailure;
      detail = failure.reason;
    } else if (typeof body.detail === "string") {
      detail = body.detail;
    } else if (body.detail !== undefined) {
      detail = JSON.stringify(body.detail);
    }
  } catch {
    // keep the status text
  }
  throw new ApiError(response.status, detail, failure);
}

export interface ProjectOut {
  project_id: string;
  name: string;
  description: string;
  lifecycle: "NEW" | "OPEN" | "SAVED" | "ARCHIVED";
  version: number;
  owner_user_id: string;
  classification: string;
  tags: string[];
  notes: string[];
  created_at: string;
  updated_at: string;
  scientific_configuration: Record<string, unknown>;
  run_ids: string[];
  derived_from: Record<string, unknown> | null;
  evidence_sha256: string;
}

export interface DatasetOut {
  dataset_id: string;
  project_id: string;
  name: string;
  version: string;
  source_uri: string;
  rights_status: string;
  modality: string;
  classification: string;
  subject_count: number | null;
  sample_count: number;
  manifest_sha256: string;
  manifest: Record<string, unknown>;
  statistics_sha256: string | null;
  evidence_sha256: string;
  resource_id: string | null;
  approved_scopes: string[];
  common_scopes_with_engine: string[];
}

export interface JobOut {
  job_id: string;
  state: string;
  total: number;
  assessed: number;
  failed: number;
  integrity_failures: number;
  not_assessed: number;
  chunks: number;
  started_at: string | null;
  ended_at: string | null;
  cancellation_requested: boolean;
  run_id: string | null;
  error: string | null;
  resource_use: Record<string, number>;
  parquet_path: string | null;
  engine_version: string | null;
}

export interface DistributionBin {
  bin: number;
  low: number;
  high: number;
  count: number;
}

export interface DistributionOut {
  job_id: string;
  run_id: string | null;
  measure: string;
  component: "scalar" | "native";
  bins: DistributionBin[];
  measured_rows_with_value: number;
  measure_failures: number;
  outcomes: Record<string, number>;
  summary: { min: number | null; max: number | null; mean: number | null; median: number | null };
  parquet_sha256: string | null;
}

export interface DrilldownRow {
  job_id: string;
  sample_id: string;
  dataset_id: string;
  run_id: string | null;
  outcome: string;
  failure_reason: string | null;
  uqs_scalar: number | null;
  uqs_native: number | null;
  source_path: string;
  sha256: string;
  links: { sample: string; quality: string; run: string | null; image: string };
}

export interface SampleQualityOut {
  sample: { sample_id: string; source_path: string; sha256: string; subject_id: { value?: string | null } ; metadata: Record<string, unknown> };
  results: Array<{
    job_id: string;
    run_id: string | null;
    outcome: string;
    failure_reason: string | null;
    uqs_scalar: number | null;
    measures: Record<string, { native: number | null; scalar: number | null; failed: boolean }>;
    links: { run: string | null; job: string; audit: string | null };
  }>;
  links: { image: string; dataset: string };
}

export interface MatcherLeaf {
  run_id: string;
  matrix_id: string;
  leaf_id: string;
  matcher_id: string;
  condition_id: string;
  protocol_id: string;
  comparison_count: number;
  scored_count: number;
  failed_count: number;
  created_at: string;
  eer: Record<string, { eer: number; threshold: number; method: string }>;
  operating_points: Record<string, Array<{ target_fmr: number; threshold: number | null; fmr?: number; fnmr?: number; reason?: string }>>;
  view2_tenfold: Record<string, { mean_accuracy: number | null; std_accuracy: number | null; method: string }>;
  counts: Record<string, number>;
  metrics_sha256: string;
  comparisons_sha256: string;
  links: { run: string; metrics: string; comparisons: string; errors: string };
}

export interface ComparisonRow {
  pair_id: string;
  fold: number;
  label: string;
  left_sample_id: string;
  right_sample_id: string;
  left_subject_id: string;
  right_subject_id: string;
  score: number | null;
  failure: string | null;
  failure_side: string | null;
  links: { left_sample: string; right_sample: string; left_image: string | null; right_image: string | null; run: string };
}

export interface MatcherErrors {
  run_id: string;
  leaf_id: string;
  matcher_id: string;
  condition_id: string;
  transform_id: string | null;
  threshold: number;
  policy: string;
  score_direction: string;
  threshold_equality_rule: string;
  false_matches: ComparisonRow[];
  false_non_matches: ComparisonRow[];
  processing_failures: ComparisonRow[];
  counts: { false_matches: number; false_non_matches: number; processing_failures: number };
}

export interface MatrixStatus {
  matrix_id: string;
  progress: { phase: string; detail: string };
  leaves: MatcherLeaf[];
}


export interface PairQualityPolicy { id: string; version: string; definition: string }

export interface UtilitySummary {
  analysis_id: string;
  run_id: string;
  matcher_run_id: string;
  leaf_id: string;
  matcher_id: string;
  condition_id: string;
  quality_job_id: string;
  quality_measure: string;
  pair_quality_policy: string;
  matcher_threshold: number;
  quality_threshold: number;
  au_erc_fnmr: number | null;
  partial_au_erc_fnmr: number | null;
  au_erc_fmr: number | null;
  fnmr_before: number | null;
  fnmr_after: number | null;
  retention_rate: number | null;
  errors_prevented: number;
  successful_comparisons_removed: number;
  created_at: string;
  links: { analysis: string; run: string; pairs: string; matcher_run: string; quality_job: string };
}

export interface UtilityRequestBody {
  matcher_run_id: string;
  quality_job_id: string;
  quality_measure?: string;
  pair_quality_policy?: string;
  target_fmr?: number;
  matcher_threshold?: number | null;
  quality_threshold?: number | null;
  partial_interval?: number;
  bootstrap_replicates?: number;
  seed?: number;
}

export interface UtilityAnalysisOut {
  analysis_id: string;
  state: "RUNNING" | "COMPLETED" | "FAILED";
  error: string | null;
  summary?: UtilitySummary;
  utility?: any;
  policy?: any;
  artifact_sha256?: Record<string, string>;
}

export interface UtilityPairRow {
  pair_id: string;
  fold: number;
  label: string;
  left_sample_id: string;
  right_sample_id: string;
  left_subject_id: string;
  right_subject_id: string;
  score: number | null;
  failure: string | null;
  quality_left: number | null;
  quality_right: number | null;
  pair_quality: number | null;
  accepted_at_matcher_threshold: boolean | null;
  retained_by_quality_policy: boolean | null;
  decision_error: boolean | null;
  links: { left_sample: string; right_sample: string; left_image: string | null; right_image: string | null; left_quality: string; right_quality: string; run: string };
}


export interface CrossContrastSummary {
  contrast_id: string;
  kind: string;
  a: string;
  b: string;
  held_fixed: Record<string, string>;
  fnm_overlap: { units: number; NEITHER: number; A_ONLY: number; B_ONLY: number; BOTH: number };
  fnm_jaccard: number | null;
  au_erc_diff_fnmr: number | null;
  au_erc_diff_fnmr_ci: [number | null, number | null] | null;
  interaction_status: string | null;
}

export interface CrossMatcherSummary {
  analysis_id: string;
  run_id: string;
  matrix_id: string;
  quality_job_id: string;
  quality_measure: string;
  pair_quality_policy: string;
  target_fmr: number;
  matcher_contrasts: number;
  preprocessing_contrasts: number;
  leaves: number;
  contrasts: CrossContrastSummary[];
  created_at: string;
  links: { analysis: string; run: string; pairs: string; matrix: string; quality_job: string };
}

export interface CrossMatcherOut {
  analysis_id: string;
  state: "RUNNING" | "COMPLETED" | "FAILED";
  error: string | null;
  summary?: CrossMatcherSummary;
  document?: any;
}

export interface CrossPairRow {
  contrast_id: string;
  pair_id: string;
  label: string;
  left_sample_id: string;
  right_sample_id: string;
  score_a: number | null;
  score_b: number | null;
  failure_a: string | null;
  failure_b: string | null;
  error_a: boolean;
  error_b: boolean;
  category: string;
  quality_left: number | null;
  quality_right: number | null;
  pair_quality: number | null;
  annotations: Array<{ annotation_id: string; actor_id: string; created_at: string; text: string }>;
  links: { left_sample: string; right_sample: string; left_image: string | null; right_image: string | null; left_quality: string; right_quality: string; run_a: string; run_b: string; analysis_run: string };
}


export interface FigureOut {
  figure_id: string;
  kind: string;
  title: string;
  created_at: string;
  source_run_ids: string[];
  provenance_sha256: string;
  files: Record<string, string>;
  sha256: Record<string, string>;
  metric_definitions: string[][];
  thresholds: any[];
}

export interface DashboardOut {
  dashboard_id: string;
  name: string;
  template: string;
  version: number;
  supersedes: string | null;
  created_at: string;
  layout_sha256?: string;
  panels: number | any[];
  layout?: any;
}

export interface ReportOut {
  report_id: string;
  title: string;
  created_at: string;
  created_by?: string;
  html_sha256?: string;
  pdf_sha256?: string;
  images_embedded?: number;
  images_withheld?: number;
  links: { html: string; pdf: string; json: string };
}

export interface BundleOut {
  bundle_id: string;
  run_id: string;
  path: string;
  sha256: string;
  size_bytes?: number;
  created_at: string;
  links: { download: string };
}

export interface ReproductionOut {
  reproduction_id: string;
  original_run_id: string;
  reproduction_run_id: string | null;
  state: string;
  steps: any[];
  differences: string[];
  compared_values: number;
  bundle_sha256: string;
  created_at: string;
}

export const api = {
  figures: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/figures`).then((r) => readJson<FigureOut[]>(r)),
  createFigure: (project_id: string, kind: string, parameters: Record<string, unknown>) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/figures`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ kind, parameters }) }).then((r) => readJson<FigureOut>(r)),
  dashboards: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/dashboards`).then((r) => readJson<DashboardOut[]>(r)),
  saveDashboard: (project_id: string, body: { name: string; template: string; columns: number; panels: Array<{ kind: string; ref: string }> }) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/dashboards`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }).then((r) => readJson<any>(r)),
  dashboard: (dashboard_id: string) => fetch(`/api/dashboards/${encodeURIComponent(dashboard_id)}`).then((r) => readJson<any>(r)),
  reports: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/reports`).then((r) => readJson<ReportOut[]>(r)),
  createReport: (project_id: string, spec: Record<string, unknown>) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/reports`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ spec }) }).then((r) => readJson<ReportOut>(r)),
  bundles: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/bundles`).then((r) => readJson<BundleOut[]>(r)),
  createBundle: (project_id: string, run_id: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/bundles`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ run_id }) }).then((r) => readJson<BundleOut>(r)),
  reproduce: (bundle_path: string) => fetch("/api/reproductions", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ bundle_path }) }).then((r) => readJson<ReproductionOut>(r)),
  reproductions: (project_id: string) => fetch(`/api/reproductions?project_id=${encodeURIComponent(project_id)}`).then((r) => readJson<ReproductionOut[]>(r)),
  matrixSets: (matrix_id: string) => fetch(`/api/matrices/${encodeURIComponent(matrix_id)}/sets`).then((r) => readJson<any>(r)),
  crossMatcherAnalyses: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/cross-matcher`).then((r) => readJson<CrossMatcherSummary[]>(r)),
  startCrossMatcher: (project_id: string, body: { matrix_id: string; quality_job_id: string; quality_measure?: string; pair_quality_policy?: string; target_fmr?: number; bootstrap_replicates?: number }) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/cross-matcher`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }).then((r) => readJson<{ analysis_id: string; state: string; already_recorded?: boolean }>(r)),
  crossMatcher: (analysis_id: string) => fetch(`/api/cross-matcher/${encodeURIComponent(analysis_id)}`).then((r) => readJson<CrossMatcherOut>(r)),
  crossPairs: (analysis_id: string, contrast_id: string, category?: string) =>
    fetch(`/api/cross-matcher/${encodeURIComponent(analysis_id)}/pairs?contrast_id=${encodeURIComponent(contrast_id)}${category ? `&category=${encodeURIComponent(category)}` : ""}&limit=60`).then((r) => readJson<{ total: number; rows: CrossPairRow[]; contrast: any }>(r)),
  annotate: (project_id: string, subject_type: string, subject_id: string, text: string, context: Record<string, unknown>) =>
    fetch("/api/annotations", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ project_id, subject_type, subject_id, text, context }) }).then((r) => readJson<any>(r)),
  pairQualityPolicies: () => fetch("/api/pair-quality-policies").then((r) => readJson<{ version: string; policies: PairQualityPolicy[] }>(r)),
  utilityAnalyses: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/utility-analyses`).then((r) => readJson<UtilitySummary[]>(r)),
  startUtilityAnalysis: (project_id: string, body: UtilityRequestBody) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/utility-analyses`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }).then((r) => readJson<{ analysis_id: string; state: string; already_recorded?: boolean }>(r)),
  utilityAnalysis: (analysis_id: string) => fetch(`/api/utility-analyses/${encodeURIComponent(analysis_id)}`).then((r) => readJson<UtilityAnalysisOut>(r)),
  utilityPairs: (analysis_id: string, query: string) => fetch(`/api/utility-analyses/${encodeURIComponent(analysis_id)}/pairs?${query}`).then((r) => readJson<{ total: number; rows: UtilityPairRow[] }>(r)),
  protocols: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/protocols`).then((r) => readJson<Array<{ protocol_id: string; name: string; pair_count: number; mated_count: number; nonmated_count: number }>>(r)),
  freezeProtocol: (project_id: string, dataset_id: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/datasets/${encodeURIComponent(dataset_id)}/protocols/freeze-lfw-view2`, { method: "POST" }).then((r) => readJson<{ protocol_id: string; pair_count: number }>(r)),
  startMatrix: (project_id: string, protocol_id: string, fixturePairsOnly: boolean) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/protocols/${encodeURIComponent(protocol_id)}/matrix?fixture_pairs_only=${fixturePairsOnly}`, { method: "POST" }).then((r) => readJson<{ matrix_id: string; pairs: number; samples: number }>(r)),
  matrix: (matrix_id: string) => fetch(`/api/matrices/${encodeURIComponent(matrix_id)}`).then((r) => readJson<MatrixStatus>(r)),
  matcherLeaves: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/matcher-leaves`).then((r) => readJson<MatcherLeaf[]>(r)),
  matcherMetrics: (run_id: string) => fetch(`/api/matcher-runs/${encodeURIComponent(run_id)}/metrics`).then((r) => readJson<{ metrics: any }>(r)),
  matcherErrors: (run_id: string, threshold?: number) =>
    fetch(`/api/matcher-runs/${encodeURIComponent(run_id)}/errors${threshold !== undefined ? `?threshold=${threshold}` : ""}`).then((r) => readJson<MatcherErrors>(r)),
  datasets: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/datasets`).then((r) => readJson<DatasetOut[]>(r)),
  importLfw: (project_id: string, verify_hashes: boolean) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/datasets/import-registered`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ resource_id: "lfw-funneled-local-candidate", verify_hashes }),
    }).then((r) => readJson<DatasetOut>(r)),
  datasetStatistics: (dataset_id: string) =>
    fetch(`/api/datasets/${encodeURIComponent(dataset_id)}/statistics`).then((r) => readJson<{ dataset_id: string; evidence_sha256: string; statistics: Record<string, any> }>(r)),
  startBatch: (project_id: string, dataset_id: string, selection: Record<string, unknown>, chunks: number, purpose_scope: string | null) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/datasets/${encodeURIComponent(dataset_id)}/quality-batch`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(purpose_scope ? { selection, chunks, purpose_scope } : { selection, chunks }),
    }).then((r) => readJson<JobOut>(r)),
  job: (job_id: string) => fetch(`/api/jobs/${encodeURIComponent(job_id)}`).then((r) => readJson<JobOut>(r)),
  cancelJob: (job_id: string) => fetch(`/api/jobs/${encodeURIComponent(job_id)}/cancel`, { method: "POST" }).then((r) => readJson<JobOut>(r)),
  projectJobs: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/jobs`).then((r) => readJson<JobOut[]>(r)),
  distribution: (job_id: string, measure: string, component: "scalar" | "native", bins: number) =>
    fetch(`/api/jobs/${encodeURIComponent(job_id)}/distribution?measure=${encodeURIComponent(measure)}&component=${component}&bins=${bins}`).then((r) => readJson<DistributionOut>(r)),
  drilldown: (job_id: string, min_scalar: number, max_scalar: number, limit = 60) =>
    fetch(`/api/jobs/${encodeURIComponent(job_id)}/drilldown?min_scalar=${min_scalar}&max_scalar=${max_scalar}&outcome=MEASURED&limit=${limit}`).then((r) => readJson<{ total: number; rows: DrilldownRow[] }>(r)),
  drilldownFailures: (job_id: string, limit = 60) =>
    fetch(`/api/jobs/${encodeURIComponent(job_id)}/drilldown?outcome=ENGINE_ROW_FAILURE&limit=${limit}`).then((r) => readJson<{ total: number; rows: DrilldownRow[] }>(r)),
  sampleQuality: (sample_id: string) => fetch(`/api/samples/${encodeURIComponent(sample_id)}/quality`).then((r) => readJson<SampleQualityOut>(r)),
  projects: () => fetch("/api/projects").then((r) => readJson<ProjectOut[]>(r)),
  createProject: (name: string, description: string, purpose_scope: string) =>
    fetch("/api/projects", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name, description, purpose_scope, tags: [], notes: [] }),
    }).then((r) => readJson<ProjectOut>(r)),
  projectAction: (project_id: string, action: "open" | "save" | "close" | "archive", body?: Record<string, unknown>) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/${action}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body ?? {}),
    }).then((r) => readJson<ProjectOut>(r)),
  deriveProject: (project_id: string, mode: "clone" | "save-as", name: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/${mode}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name }),
    }).then((r) => readJson<ProjectOut>(r)),
  health: () => fetch("/api/health").then((r) => readJson<HealthOut>(r)),
  resources: () => fetch("/api/resources").then((r) => readJson<ResourceOut[]>(r)),
  registeredSamples: () => fetch("/api/samples/registered").then((r) => readJson<RegisteredSample[]>(r)),
  runs: () => fetch("/api/runs").then((r) => readJson<RunSummaryOut[]>(r)),
  assess: (image_path: string, purpose_scope: string, project_id: string | null) =>
    fetch("/api/quality/assess", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(project_id ? { image_path, purpose_scope, project_id } : { image_path, purpose_scope }),
    }).then((r) => readJson<AssessResponse>(r)),
  imageUrl: (path: string) => `/api/samples/image?path=${encodeURIComponent(path)}`,
};

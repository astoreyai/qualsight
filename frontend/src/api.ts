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
  actor_id: string;
  network_guard: {
    installed: boolean;
    refused_attempts: string[];
    authorized_setup_connections: number;
  };
}

export interface ExecutorStatusOut {
  capacity: number;
  in_flight: number;
  available: number;
}

export interface DatabaseOperationalStatusOut {
  query: "ok";
  schema_version: number;
  operation_states: Record<string, number>;
  unresolved_purges: number;
}

type ReadyDatabaseCheck = ({ ok: true } & DatabaseOperationalStatusOut) | { ok: false; error_code: "DATABASE_UNAVAILABLE" };
type ReadyStorageCheck =
  | { ok: true; mode: string; available_bytes: number }
  | { ok: false; error_code: "STORAGE_UNAVAILABLE" };

export interface ReadyOut {
  status: "ready" | "not_ready";
  checks: {
    shutdown: { ok: boolean };
    data_directory_lease: {
      state: "HELD" | "UNAVAILABLE";
      owner: string | null;
      pid: number | null;
      acquired_at: string | null;
      expires_at: string | null;
      release_condition: "PROCESS_SHUTDOWN";
    };
    database: ReadyDatabaseCheck;
    storage: ReadyStorageCheck;
    core_runtime: {
      ok: boolean;
      artifacts: Array<{
        role: "BINARY" | "CONFIGURATION";
        resource_id: string;
        sha256: string;
        ok: boolean;
      }>;
    };
    scientific_capacity: ExecutorStatusOut;
    network_guard: {
      ok: boolean;
      installed: boolean;
      refused_attempts: string[];
      authorized_setup_connections: number;
    };
  };
}

export interface OperationalMetricsOut {
  kind: "QUALSIGHT_LOCAL_OPERATIONAL_METRICS_V1";
  uptime_seconds: number;
  http: {
    requests: number;
    responses_by_status: Record<string, number>;
    mean_duration_ms: number | null;
  };
  scientific_work: ExecutorStatusOut;
  database: DatabaseOperationalStatusOut;
  reconciled_at_startup: { operations: number; purges: number };
  shutting_down: boolean;
}

export interface MatcherAssetOut {
  matcher_id: string;
  name: string;
  registry_state: string;
  third_party_rights_status: string;
  research_use_status: string;
  redistribution_status: string;
  data_classification: string;
  approved_scopes: string[];
  checkpoint: {
    state: "VERIFIED" | "MISSING" | "DRIFTED";
    location_state: "MANAGED" | "REGISTERED_EXTERNAL" | "LEGACY_EXTERNAL";
    registered_sha256: string;
    registered_size_bytes: number;
    observed_sha256: string | null;
    observed_size_bytes?: number;
  };
  checkpoint_acquisition: {
    available: boolean;
    state: "ADMITTED_LOCAL_NONCOMMERCIAL" | "BLOCKED_THIRD_PARTY_RIGHTS";
    reason: string;
    published_url: string;
    source_host: string;
    allowed_redirect_hosts: string[];
    sha256: string;
    size_bytes: number;
  };
  architecture_sources: Array<{
    key: string;
    state: "VERIFIED" | "MISSING" | "DRIFTED";
    repository: string;
    commit: string;
    url: string;
    sha256: string;
    size_bytes: number;
    reason: string | null;
  }>;
  ready_to_load: boolean;
  blocking_reasons: string[];
  runtime: {
    state: "LOADED" | "UNLOADED";
    active_leases: number;
    loaded_at: string | null;
    device: string | null;
    parameter_count: number | null;
  };
}

export interface MatcherAddonOut extends MatcherAssetOut {
  addon_id: string;
  kind: "MATCHER";
  subtype: "FACE_EMBEDDING_MATCHER";
  driver_id: string;
  installation_state: string;
}

export interface DatasetAddonOut {
  addon_id: string;
  kind: "DATASET";
  subtype: "IDENTITY_LABELED_DATASET" | "PAIR_VERIFICATION_DATASET";
  display_name: string;
  version: string;
  driver_id: string;
  registry_state?: string;
  resource_record_sha256?: string;
  installation_state: string;
  runtime_state: string;
  blocking_reasons: string[];
  managed: boolean;
  sample_count: number;
  identity_count: number | null;
  pair_count?: number;
  mated_pair_count?: number;
  nonmated_pair_count?: number;
  tree_sha256?: string;
  archive?: { sha256: string; size_bytes: number; state?: string };
  rights: { third_party: string; research_use: string; redistribution: string };
  classification: string;
  approved_scopes: string[];
  acquisition: {
    available: boolean;
    refusal_code?: string;
    source_uri?: string;
    source_kind?: string;
    reason?: string;
    archive_sha256?: string;
    archive_size_bytes?: number;
    shared_archive_addon_ids?: string[];
    rights_notice?: string;
  };
}

export interface PreprocessingAddonOut {
  addon_id: string;
  kind: "PREPROCESSING";
  subtype: "MATCHER_PREPROCESSING";
  display_name: string;
  version: string;
  driver_id: string;
  installation_state: "BUILTIN";
  runtime_state: "READY" | "MISSING" | "DRIFTED";
  blocking_reasons: string[];
  detector_resource_id: string | null;
  detector_resource_sha256: string | null;
  input_representation: "SOURCE_IMAGE_REQUIRING_DETECTION" | "AUTHOR_PREALIGNED_ARCFACE112";
  definition: Record<string, string>;
  compatible_matcher_ids: string[];
  leaf_ids_by_matcher: Record<string, string>;
  selection_scope: "PER_RUN";
}

export interface TransformAddonOut {
  addon_id: string;
  kind: "PREPROCESSING";
  subtype: "CONTROLLED_DEGRADATION";
  display_name: string;
  version: string;
  driver_id: string;
  installation_state: "BUILTIN";
  runtime_state: "READY";
  operator: string;
  parameter: string;
  parameter_contract: NumericParameterContract;
  selection_scope: "PER_DERIVED_SET";
}

export interface NumericParameterContract {
  value_type: "integer" | "number";
  minimum?: number;
  maximum?: number;
  minimum_exclusive?: boolean;
  maximum_exclusive?: boolean;
  required?: boolean;
  blank_behavior?: string;
  guidance: string;
}

export interface AddonCatalogOut {
  schema_version: string;
  mode: "CLOSED_ALLOWLIST";
  registry_id: string;
  registry_sha256: string;
  matchers: MatcherAddonOut[];
  datasets: DatasetAddonOut[];
  preprocessing: PreprocessingAddonOut[];
  transforms: TransformAddonOut[];
  analysis_parameters: {
    target_fmr: NumericParameterContract;
    bootstrap_replicates: NumericParameterContract;
    quality_threshold: NumericParameterContract;
    matcher_threshold: NumericParameterContract;
    owner_pair_seed: NumericParameterContract;
  };
  connections: [];
  connection_policy: { state: "NOT_ADMITTED_IN_BETA"; reason: string; admission_requires: string[] };
}

export interface SystemEventOut {
  system_event_id: string;
  actor_id: string;
  event_type: string;
  subject_id: string;
  occurred_at: string;
  details: Record<string, unknown>;
  evidence_sha256: string;
}

export interface EmbeddingAssetOut {
  run_id: string;
  matrix_id: string;
  leaf_id: string;
  matcher_id: string;
  condition_id: string;
  dataset_id: string;
  classification: string;
  integrity: "VERIFIED" | "MISSING" | "DRIFTED" | "INVALID_FORMAT";
  integrity_reason: string | null;
  registered_sha256: string;
  observed_sha256: string | null;
  size_bytes: number | null;
  rows: number | null;
  dimension: number | null;
  created_at: string;
  links: { run: string };
}

export interface RegisteredSample {
  registered_sample_id: string;
  display_name: string;
  resource_id: string;
  role: string;
  approved_scopes: string[];
  sha256: string;
  size_bytes: number;
  media_type: string;
}

export interface MeasureOut {
  name: string;
  native: number | null;
  native_unit: string;
  native_semantics: string;
  scalar: number | null;
  failed: boolean;
  failure_reason: string | null;
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
  registered_sample_id: string;
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
  registered_sample_id: string;
  engine_version: string;
  exit_code: number;
  reason_code: "ENGINE_EXECUTION_FAILED" | "ENGINE_RETURNED_NO_MEASURES";
  message: string;
  failure_event_id: string;
  failure_evidence_sha256: string;
}

export interface RunSummaryOut {
  run_id: string;
  project_id: string;
  experiment_id: string;
  sample_id: string | null;
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
      detail = failure.message;
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

async function readDownload(response: Response): Promise<{ blob: Blob; filename: string }> {
  if (!response.ok) {
    await readJson<never>(response);
  }
  const disposition = response.headers.get("content-disposition") ?? "";
  const filenameMatch = disposition.match(/filename="?([^";]+)"?/i);
  const filename = filenameMatch?.[1]?.trim();
  if (!filename) {
    throw new ApiError(response.status, "the export response omitted its governed filename");
  }
  return { blob: await response.blob(), filename };
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

export interface ProjectPurgeScopeOut {
  project_id: string;
  scope: string;
  target_counts: {
    files: number;
    directories: number;
  };
}

export interface ProjectPurgeOut {
  purge_id: string;
  version: string;
  project_id: string;
  actor_id: string;
  created_at: string;
  scope: string;
  target_counts: ProjectPurgeScopeOut["target_counts"];
  outcome_counts: {
    deleted: number;
    already_absent: number;
    reconciled_after_deletion_intent: number;
    failed: number;
    remaining_after_purge: number;
  };
  metadata: {
    removed: Record<string, number>;
    remaining: Record<string, number>;
    verified: boolean;
  };
  verified: boolean;
  retained: string[];
  not_performed: string[];
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
  engine_version: string | null;
  dataset_id?: string | null;
  quality_result_available: boolean;
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

export interface QualityMeasureCatalogOut {
  job_id: string;
  run_id: string;
  parquet_sha256: string;
  measures: string[];
  default_measure: string | null;
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
  sha256: string;
  links: { sample: string; quality: string; run: string | null; image: string };
}

export interface SampleQualityOut {
  sample: {
    sample_id: string;
    dataset_id: string;
    subject_id: string | null;
    media_type: string;
    sha256: string;
    size_bytes: number;
    metadata: Record<string, unknown>;
    links: { image: string; detection: string; ofiq_landmarks: string; quality: string };
  };
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
  provenance_marker?: string;
  run_id: string;
  matrix_id: string;
  leaf_id: string;
  matcher_id: string;
  condition_id: string;
  protocol_id: string;
  dataset_id?: string | null;
  comparison_count: number;
  scored_count: number;
  failed_count: number;
  created_at: string;
  eer: Record<string, { eer: number; threshold: number; method: string }>;
  operating_points: Record<string, Array<{ target_fmr: number; threshold: number | null; fmr?: number; fnmr?: number; reason?: string }>>;
  view2_tenfold: Record<string, { status?: string; reason?: string; mean_accuracy: number | null; std_accuracy: number | null; method: string }>;
  counts: Record<string, number>;
  metrics_sha256: string;
  comparisons_sha256: string;
  links: { run: string; metrics: string; comparisons: string; errors: string };
}

export interface ComparisonRow {
  pair_id: string;
  fold: number | null;
  label: string;
  left_sample_id: string;
  right_sample_id: string;
  left_subject_id: string | null;
  right_subject_id: string | null;
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
  expected_leaves?: number;
  progress: { phase: string; detail: string };
  leaves: MatcherLeaf[];
}


export interface PairQualityPolicy { id: string; version: string; definition: string }

export interface UtilityOverlayIdentity {
  schema_version: "1.0.0";
  project_id: string;
  dataset_id: string;
  protocol_id: string;
  pair_universe_sha256: string;
  comparisons_sha256: string;
  quality_parquet_sha256: string;
  quality_measure: string;
  pair_quality_policy: { id: string; version: string; definition_sha256: string };
  metric_registry: { id: string; version: string; definition_sha256: string };
  threshold_score_semantics: {
    threshold_kind: string;
    threshold_source: string;
    threshold_selection: string;
    target_fmr: number | null;
    failure_policy: string | null;
    evaluation_scope: string;
    score_direction: "HIGHER_IS_MORE_SIMILAR" | "LOWER_IS_MORE_SIMILAR";
    equality_rule: string;
  };
  matcher_id: string;
  preprocessing_id: string;
}

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
  overlay_identity?: UtilityOverlayIdentity;
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
  state: "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "INTERRUPTED";
  error: string | null;
  summary?: UtilitySummary;
  utility?: any;
  policy?: any;
  artifact_sha256?: Record<string, string>;
}

export interface UtilityPairRow {
  pair_id: string;
  fold: number | null;
  label: string;
  left_sample_id: string;
  right_sample_id: string;
  left_subject_id: string | null;
  right_subject_id: string | null;
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
  state: "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "INTERRUPTED";
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

export type DashboardPanelKind = "figure" | "operating_points" | "utility_summary" | "cross_contrasts" | "dataset_statistics" | "policy_table";

export interface DashboardFigurePanel {
  kind: "figure";
  ref: string;
  title: string;
  run_ids: string[];
  data: {
    figure: {
      kind: string;
      files: Record<"png" | "svg" | "pdf" | "csv" | "provenance", string>;
      thresholds: Array<Record<string, unknown>>;
      metric_definitions: string[][];
    };
  };
}

export interface DashboardOperatingPointsPanel {
  kind: "operating_points";
  ref: string;
  title: string;
  run_ids: string[];
  data: {
    leaf_id: string;
    matcher_id: string;
    condition_id: string;
    by_policy: Record<string, {
      operating_points: Array<{ target_fmr: number; threshold: number | null; fmr?: number; fnmr?: number; reason?: string }>;
      eer: { eer: number | null; threshold: number | null; method?: string; evaluation_scope?: string };
    }>;
  };
}

export interface DashboardUtilityPanel {
  kind: "utility_summary" | "policy_table";
  ref: string;
  title: string;
  run_ids: string[];
  data: {
    summary: Partial<UtilitySummary>;
    policy?: {
      retention_rate?: number | null;
      counts?: Record<string, number>;
      impact?: { errors_prevented?: number; successful_comparisons_removed?: number };
    };
  };
}

export interface DashboardCrossContrastsPanel {
  kind: "cross_contrasts";
  ref: string;
  title: string;
  run_ids: string[];
  data: { contrasts: CrossContrastSummary[] };
}

export interface DashboardDatasetStatisticsPanel {
  kind: "dataset_statistics";
  ref: string;
  title: string;
  run_ids: string[];
  data: {
    statistics: {
      population?: {
        subjects?: number;
        samples?: number;
        samples_per_subject_mean?: number;
        samples_per_subject_median?: number;
        samples_per_subject_min?: number;
        samples_per_subject_max?: number;
      };
      data_completeness?: Record<string, number>;
    };
    evidence_sha256: string;
  };
}

export type DashboardPanel = DashboardFigurePanel | DashboardOperatingPointsPanel | DashboardUtilityPanel | DashboardCrossContrastsPanel | DashboardDatasetStatisticsPanel;

export interface DashboardLayout {
  version: string;
  name: string;
  template: string;
  columns: number;
  panels: Array<{ kind: DashboardPanelKind; ref: string; title?: string }>;
}

export interface DashboardOut {
  dashboard_id: string;
  name: string;
  template: string;
  version: number;
  supersedes: string | null;
  created_at: string;
  layout_sha256?: string;
  panel_run_ids: string[];
  panels: number;
}

export interface DashboardSaveOut extends Omit<DashboardOut, "panels"> {
  project_id: string;
  created_by: string;
  layout: DashboardLayout;
}

export interface DashboardDetailOut extends Omit<DashboardOut, "panels"> {
  project_id: string;
  created_by: string;
  layout: DashboardLayout;
  panels: DashboardPanel[];
}

export interface ReportOut {
  report_id: string;
  title: string;
  dataset_id: string | null;
  created_at: string;
  created_by?: string;
  html_sha256?: string;
  pdf_sha256?: string;
  images_embedded?: number;
  images_withheld?: number;
  downloads: Record<"html" | "pdf" | "json", ArtifactDownloadOut>;
  links: Partial<Record<"html" | "pdf" | "json", string>>;
}

export interface ArtifactDownloadOut {
  state: "VERIFIED" | "MISSING" | "DRIFTED" | "UNANCHORED";
  reason: string | null;
  href: string | null;
}

export interface BundleOut {
  bundle_id: string;
  run_id: string;
  sha256: string;
  size_bytes?: number;
  created_at: string;
  download: ArtifactDownloadOut;
  links: { download?: string };
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


export interface IdentificationProtocolOut {
  protocol_id: string; project_id: string; dataset_id: string; mode: string; name: string; selection: string; rank_depth: number; gallery_count: number; mated_probe_count: number; nonmated_probe_count: number; subjects_total: number; manifest_sha256: string; created_at: string;
}
export interface IdentificationSearchSummary {
  search_id: string; run_id: string; protocol_id: string; matcher_id: string; condition_id: string; device: string; gallery_size: number; gallery_template_failures: number; mated_searches: number; nonmated_searches: number; probe_template_failures: number; rank_1: number | null; fnir_at_fpir_0_01: number | null; seconds: number; created_at: string;
  links: { search: string; run: string; candidates: string; protocol: string };
}
export interface IdentificationSearchOut { search_id: string; state: "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "INTERRUPTED"; run_id?: string; summary?: IdentificationSearchSummary; metrics?: any; progress?: any; error?: string | null }
export interface DerivedSetOut { derived_set_id: string; project_id: string; derived_dataset_id: string; source_dataset_id: string; transforms: any[]; levels: number; source_count: number; derived_count: number; created_at: string; links: { detail: string } }
export interface DegradationSummary { analysis_id: string; run_id: string; derived_set_id: string; matcher_id: string; condition_id: string; matcher_threshold: number; transforms: string[]; levels: number; mated_pairs: number; nonmated_pairs: number; seconds: number; fnmr_by_transform: Record<string, Array<number | null>>; uqs_by_transform: Record<string, Array<number | null>>; created_at?: string; links?: { analysis: string; run: string } }
export interface DegradationOut { analysis_id: string; state: "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "INTERRUPTED"; run_id?: string; summary?: DegradationSummary; document?: any; progress?: any; error?: string | null }

export interface UploadAssessOut {
  run_id: string;
  job_id: string;
  dataset_id: string;
  sample_id: string;
  source_kind: string;
  image_sha256: string;
  original_name: string;
  width: number;
  height: number;
  measures: MeasureOut[];
  failed_measures: string[];
  provenance: { run: string; sample: string; sample_image: string; statement: string };
}

export type OwnerPairRelationship = "SAME_PERSON" | "DIFFERENT_PEOPLE" | "NOT_RECORDED";

export interface OwnerPairAdmissionOut {
  schema_version: "1.0.0";
  pair_id: string;
  project_id: string;
  left_sample_id: string;
  right_sample_id: string;
  left_sha256: string;
  right_sha256: string;
  relationship: OwnerPairRelationship;
  comparison_label: "MATED" | "NON_MATED" | null;
  relationship_assertion: {
    state: "OPERATOR_ASSERTED_UNVERIFIED";
    relationship: OwnerPairRelationship;
    asserted_by: string;
    asserted_at: string;
  };
  population_error_claims_allowed: false;
  outcome_correctness_allowed: false;
  source_kind: "OWNER_SUPPLIED_LOCAL_INPUT";
  local_only: true;
}

export interface DetectionGeometryOut {
  label: string;
  image: { width: number; height: number };
  face_count: number | null;
  chosen_index: number | null;
  failure: string | null;
  detections: Array<{ bbox: number[]; score: number; landmarks: number[][] }>;
}

export interface OfiqLandmarksOut {
  label: string;
  image: { width: number; height: number };
  landmark_type: string;
  faces: Array<{ x: number; y: number; width: number; height: number }>;
  landmarks: number[][];
}

export interface DirectPairThreshold {
  contract_id: "matcher_threshold";
  kind: "FIXED_VALUE";
  value: number;
  rule?: "MATCH_WHEN_SCORE_GE_THRESHOLD";
}

export interface DirectPairComparisonStatus {
  schema_version: "1.0";
  comparison_id: string;
  run_id: string;
  project_id: string;
  pair_id: string;
  state: "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "INTERRUPTED";
  links: { self: string; evidence: string; run: string; raw_manifest: string };
  poll_after_ms: 500 | null;
}

export interface DirectPairSideEvidence {
  sample_id: string;
  dataset_id: string;
  sha256: string;
  face_count: number | null;
  detection: ({ state: "RECORDED"; sha256: string | null } & Record<string, unknown>) | null;
  crop: { state: "RECORDED"; sha256: string; pixel_sha256: string | null; width: 112; height: 112 } | null;
  embedding: { state: "RECORDED"; sha256: string; value_sha256: string | null; shape: number[]; dtype: "float32" | null } | null;
  failure: { kind: string; detail: string } | null;
  quality: { job_id: string; run_id: string; unified_quality_score: number | null; failed_measures: string[] } | null;
  links: { source: string; crop: string | null; detection: string | null; embedding: string | null; quality: string | null };
}

export interface DirectPairEvidenceOut {
  schema_version: "1.0";
  comparison_id: string;
  run_id: string;
  state: "COMPLETED";
  project_id: string;
  pair: {
    pair_id: string;
    relationship: OwnerPairRelationship;
    comparison_label: "MATED" | "NON_MATED" | null;
    relationship_assertion: OwnerPairAdmissionOut["relationship_assertion"];
  };
  execution: {
    matcher_id: string;
    preprocessing_id: string;
    threshold: DirectPairThreshold & { rule: "MATCH_WHEN_SCORE_GE_THRESHOLD" };
    matcher_checkpoint_sha256: string;
    matcher_architecture_sha256: string[];
    matcher_device: string;
    preprocessing_definition_sha256: string;
    detector_sha256: string;
    application_version: string;
    started_at: string;
    ended_at: string;
    runtime_seconds: number;
  };
  result: {
    score: number | null;
    direction: "HIGHER_IS_MORE_SIMILAR";
    failure: string | null;
    failure_side: "left" | "right" | "left+right" | "both" | null;
    decision: "MATCH" | "NO_MATCH" | null;
    decision_reason: "SCORE_GE_FIXED_THRESHOLD" | "SCORE_LT_FIXED_THRESHOLD" | "NO_SCORE_PROCESSING_FAILURE";
    agreement_with_operator_assertion: boolean | null;
  };
  claim_boundary: {
    population_error_claims_allowed: false;
    outcome_correctness_allowed: false;
    ground_truth_authority: false;
    prohibited: Array<"FMR" | "FNMR" | "EER" | "ROC" | "DET" | "OPERATING_POINT">;
  };
  sides: { left: DirectPairSideEvidence; right: DirectPairSideEvidence };
  links: { self: string; status: string; run: string; raw_manifest: string };
}

export const api = {
  addons: () => fetch("/api/addons").then((r) => readJson<AddonCatalogOut>(r)),
  preprocessingProfiles: () => fetch("/api/preprocessing-profiles").then((r) => readJson<PreprocessingAddonOut[]>(r)),
  matcherAssets: () => fetch("/api/matcher-assets").then((r) => readJson<MatcherAssetOut[]>(r)),
  pullMatcherSources: (matcher_id: string) =>
    fetch(`/api/matcher-assets/${encodeURIComponent(matcher_id)}/pull-sources`, { method: "POST" }).then((r) => readJson<{ asset: MatcherAssetOut; event: SystemEventOut }>(r)),
  pullMatcherCheckpoint: (matcher_id: string) =>
    fetch(`/api/matcher-assets/${encodeURIComponent(matcher_id)}/pull-checkpoint`, { method: "POST" }).then((r) => readJson<{ asset: MatcherAssetOut; receipt: { receipt_id: string; evidence_sha256: string; action: string }; event: SystemEventOut }>(r)),
  pullVerificationDataset: (addon_id: string) =>
    fetch(`/api/dataset-assets/${encodeURIComponent(addon_id)}/pull`, { method: "POST" }).then((r) => readJson<{ dataset: DatasetAddonOut; receipt: { receipt_id: string; evidence_sha256: string; action: string; archive: { sha256: string; size_bytes: number } }; event: SystemEventOut }>(r)),
  importVerificationDataset: (project_id: string, addon_id: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/dataset-assets/${encodeURIComponent(addon_id)}/import`, { method: "POST" }).then((r) => readJson<{ dataset: DatasetOut; protocol: { protocol_id: string; name: string; pair_count: number; mated_count: number; nonmated_count: number; fold_count: number | null; unique_subject_count: number | null }; samples: number; pairs: number }>(r)),
  loadMatcher: (matcher_id: string) =>
    fetch(`/api/matcher-assets/${encodeURIComponent(matcher_id)}/load`, { method: "POST" }).then((r) => readJson<{ asset: MatcherAssetOut; event: SystemEventOut }>(r)),
  unloadMatcher: (matcher_id: string) =>
    fetch(`/api/matcher-assets/${encodeURIComponent(matcher_id)}/unload`, { method: "POST" }).then((r) => readJson<{ asset: MatcherAssetOut; event: SystemEventOut }>(r)),
  systemEvents: () => fetch("/api/system-events").then((r) => readJson<SystemEventOut[]>(r)),
  embeddings: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/embeddings`).then((r) => readJson<EmbeddingAssetOut[]>(r)),
  importOwnerDirectory: (project_id: string, root: string, name: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/datasets/import-owner-directory`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ root, name }) }).then((r) => readJson<{ dataset_id: string; sample_count: number; subject_count: number; source_kind: string; statistics_sha256: string }>(r)),
  importOwnerManifest: (project_id: string, manifest_path: string, name: string, mapping: Record<string, string>, mapping_name: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/datasets/import-owner-manifest`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ manifest_path, name, mapping, mapping_name }) }).then((r) => readJson<{ dataset_id: string; sample_count: number; subject_count: number; source_kind: string; statistics_sha256: string; mapping: Record<string, string> }>(r)),
  freezeOwnerPairs: (project_id: string, dataset_id: string, seed: number, max_mated_pairs_per_subject = 6, nonmated_multiple = 1) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/datasets/${encodeURIComponent(dataset_id)}/protocols/freeze-owner-pairs`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ seed, max_mated_pairs_per_subject, nonmated_multiple }) }).then((r) => readJson<{ protocol_id: string; pair_count: number; mated_count: number; nonmated_count: number }>(r)),
  assessUpload: (project_id: string, purpose_scope: string, file: File) =>
    fetch(
      `/api/quality/assess-upload?project_id=${encodeURIComponent(project_id)}&purpose_scope=${encodeURIComponent(purpose_scope)}&filename=${encodeURIComponent(file.name)}`,
      { method: "POST", headers: { "content-type": "application/octet-stream" }, body: file },
    ).then((r) => readJson<UploadAssessOut>(r)),
  detectionForSample: (project_id: string, sample_id: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/samples/${encodeURIComponent(sample_id)}/detection`).then((r) => readJson<DetectionGeometryOut>(r)),
  ofiqLandmarksForSample: (project_id: string, sample_id: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/samples/${encodeURIComponent(sample_id)}/ofiq-landmarks`).then((r) => readJson<OfiqLandmarksOut>(r)),
  projectSampleImageUrl: (project_id: string, sample_id: string) =>
    `/api/projects/${encodeURIComponent(project_id)}/samples/${encodeURIComponent(sample_id)}/image`,
  admitOwnerPair: (project_id: string, left_sample_id: string, right_sample_id: string, relationship: OwnerPairRelationship) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/owner-pair-admissions`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ left_sample_id, right_sample_id, relationship }),
    }).then((r) => readJson<OwnerPairAdmissionOut>(r)),
  startOwnerPairComparison: (project_id: string, body: { pair_id: string; matcher_id: string; preprocessing_id: string; threshold: Omit<DirectPairThreshold, "rule"> }) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/owner-pair-comparisons`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => readJson<DirectPairComparisonStatus>(r)),
  ownerPairComparison: (project_id: string, comparison_id: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/owner-pair-comparisons/${encodeURIComponent(comparison_id)}`).then((r) => readJson<DirectPairComparisonStatus>(r)),
  ownerPairComparisonEvidence: (project_id: string, comparison_id: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/owner-pair-comparisons/${encodeURIComponent(comparison_id)}/evidence`).then((r) => readJson<DirectPairEvidenceOut>(r)),
  ownerPairComparisons: (project_id: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/owner-pair-comparisons`).then((r) => readJson<DirectPairComparisonStatus[]>(r)),
  freezeIdentification: (project_id: string, dataset_id: string, selection: string, rank_depth: number) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/datasets/${encodeURIComponent(dataset_id)}/protocols/freeze-lfw-identification`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ selection, rank_depth }) }).then((r) => readJson<IdentificationProtocolOut>(r)),
  identificationProtocols: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/identification-protocols`).then((r) => readJson<IdentificationProtocolOut[]>(r)),
  startIdentificationSearch: (project_id: string, protocol_id: string, body: { matcher_id: string; condition_id: string; quality_job_id?: string | null }) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/identification-protocols/${encodeURIComponent(protocol_id)}/search`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }).then((r) => readJson<{ search_id: string; state: string }>(r)),
  identificationSearch: (project_id: string, search_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/identification-searches/${encodeURIComponent(search_id)}`).then((r) => readJson<IdentificationSearchOut>(r)),
  identificationSearches: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/identification-searches`).then((r) => readJson<IdentificationSearchSummary[]>(r)),
  identificationCandidates: (project_id: string, search_id: string, query: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/identification-searches/${encodeURIComponent(search_id)}/candidates?${query}`).then((r) => readJson<{ total: number; rows: any[] }>(r)),
  transforms: () => fetch("/api/transforms").then((r) => readJson<{ operators: Array<{ kind: string; parameter: string; version: string }> }>(r)),
  createTransformations: (project_id: string, dataset_id: string, body: { selection: string; sample_ids?: string[]; transforms: Array<{ kind: string; parameters: number[] }> }) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/datasets/${encodeURIComponent(dataset_id)}/transformations`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }).then((r) => readJson<DerivedSetOut>(r)),
  derivedSets: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/derived-sets`).then((r) => readJson<DerivedSetOut[]>(r)),
  startDegradation: (project_id: string, body: Record<string, unknown>) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/degradation-analyses`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }).then((r) => readJson<{ analysis_id: string; state: string }>(r)),
  degradation: (project_id: string, analysis_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/degradation-analyses/${encodeURIComponent(analysis_id)}`).then((r) => readJson<DegradationOut>(r)),
  degradations: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/degradation-analyses`).then((r) => readJson<DegradationSummary[]>(r)),
  figures: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/figures`).then((r) => readJson<FigureOut[]>(r)),
  createFigure: (project_id: string, kind: string, parameters: Record<string, unknown>) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/figures`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ kind, parameters }) }).then((r) => readJson<FigureOut>(r)),
  dashboards: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/dashboards`).then((r) => readJson<DashboardOut[]>(r)),
  saveDashboard: (project_id: string, body: { name: string; template: string; columns: number; panels: Array<{ kind: string; ref: string }> }) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/dashboards`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }).then((r) => readJson<DashboardSaveOut>(r)),
  dashboard: (project_id: string, dashboard_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/dashboards/${encodeURIComponent(dashboard_id)}`).then((r) => readJson<DashboardDetailOut>(r)),
  reports: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/reports`).then((r) => readJson<ReportOut[]>(r)),
  createReport: (project_id: string, spec: Record<string, unknown>) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/reports`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ spec }) }).then((r) => readJson<ReportOut>(r)),
  exportTable: (project_id: string, category: "quality_results" | "comparison_records" | "embeddings" | "metric_tables" | "statistical_results" | "annotations", ref: string, format: "csv" | "json" | "parquet") =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/exports/${encodeURIComponent(category)}?ref=${encodeURIComponent(ref)}&format=${encodeURIComponent(format)}`, { method: "POST" }).then(readDownload),
  bundles: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/bundles`).then((r) => readJson<BundleOut[]>(r)),
  createBundle: (project_id: string, run_id: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/bundles`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ run_id }) }).then((r) => readJson<BundleOut>(r)),
  reproduce: (project_id: string, bundle_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/reproductions`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ bundle_id }) }).then((r) => readJson<ReproductionOut>(r)),
  reproductions: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/reproductions`).then((r) => readJson<ReproductionOut[]>(r)),
  matrixSets: (project_id: string, matrix_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/matrices/${encodeURIComponent(matrix_id)}/sets`).then((r) => readJson<any>(r)),
  crossMatcherAnalyses: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/cross-matcher`).then((r) => readJson<CrossMatcherSummary[]>(r)),
  startCrossMatcher: (project_id: string, body: { matrix_id: string; quality_job_id: string; quality_measure?: string; pair_quality_policy?: string; target_fmr?: number; bootstrap_replicates?: number }) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/cross-matcher`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }).then((r) => readJson<{ analysis_id: string; state: string; already_recorded?: boolean }>(r)),
  crossMatcher: (project_id: string, analysis_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/cross-matcher/${encodeURIComponent(analysis_id)}`).then((r) => readJson<CrossMatcherOut>(r)),
  crossPairs: (project_id: string, analysis_id: string, contrast_id: string, category?: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/cross-matcher/${encodeURIComponent(analysis_id)}/pairs?contrast_id=${encodeURIComponent(contrast_id)}${category ? `&category=${encodeURIComponent(category)}` : ""}&limit=60`).then((r) => readJson<{ total: number; rows: CrossPairRow[]; contrast: any }>(r)),
  annotate: (project_id: string, subject_type: string, subject_id: string, text: string, context: Record<string, unknown>) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/annotations`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ subject_type, subject_id, text, context }) }).then((r) => readJson<any>(r)),
  pairQualityPolicies: () => fetch("/api/pair-quality-policies").then((r) => readJson<{ version: string; policies: PairQualityPolicy[] }>(r)),
  utilityAnalyses: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/utility-analyses`).then((r) => readJson<UtilitySummary[]>(r)),
  startUtilityAnalysis: (project_id: string, body: UtilityRequestBody) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/utility-analyses`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }).then((r) => readJson<{ analysis_id: string; state: string; already_recorded?: boolean }>(r)),
  utilityAnalysis: (project_id: string, analysis_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/utility-analyses/${encodeURIComponent(analysis_id)}`).then((r) => readJson<UtilityAnalysisOut>(r)),
  utilityPairs: (project_id: string, analysis_id: string, query: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/utility-analyses/${encodeURIComponent(analysis_id)}/pairs?${query}`).then((r) => readJson<{ total: number; rows: UtilityPairRow[] }>(r)),
  protocols: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/protocols`).then((r) => readJson<Array<{ protocol_id: string; dataset_id: string; name: string; pair_count: number; mated_count: number; nonmated_count: number }>>(r)),
  freezeProtocol: (project_id: string, dataset_id: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/datasets/${encodeURIComponent(dataset_id)}/protocols/freeze-lfw-view2`, { method: "POST" }).then((r) => readJson<{ protocol_id: string; pair_count: number }>(r)),
  startMatrix: (project_id: string, protocol_id: string, fixturePairsOnly: boolean, leafIds?: string[]) => {
    const query = new URLSearchParams({ fixture_pairs_only: String(fixturePairsOnly) });
    if (leafIds?.length) query.set("leaves", leafIds.join(","));
    return fetch(`/api/projects/${encodeURIComponent(project_id)}/protocols/${encodeURIComponent(protocol_id)}/matrix?${query.toString()}`, { method: "POST" }).then((r) => readJson<{ matrix_id: string; pairs: number; samples: number }>(r));
  },
  matrix: (project_id: string, matrix_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/matrices/${encodeURIComponent(matrix_id)}`).then((r) => readJson<MatrixStatus>(r)),
  importExternalScores: (project_id: string, protocol_id: string, resource_id: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/protocols/${encodeURIComponent(protocol_id)}/import-external-scores`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ resource_id }) }).then((r) => readJson<MatcherLeaf & { provenance_marker: string; mapping: any }>(r)),
  matcherLeaves: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/matcher-leaves`).then((r) => readJson<MatcherLeaf[]>(r)),
  matcherMetrics: (project_id: string, run_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/matcher-runs/${encodeURIComponent(run_id)}/metrics`).then((r) => readJson<{ metrics: any }>(r)),
  matcherErrors: (project_id: string, run_id: string, threshold?: number) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/matcher-runs/${encodeURIComponent(run_id)}/errors${threshold !== undefined ? `?threshold=${threshold}` : ""}`).then((r) => readJson<MatcherErrors>(r)),
  datasets: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/datasets`).then((r) => readJson<DatasetOut[]>(r)),
  dataset: (project_id: string, dataset_id: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/datasets/${encodeURIComponent(dataset_id)}`).then((r) => readJson<DatasetOut>(r)),
  importLfw: (project_id: string, verify_hashes: boolean) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/datasets/import-registered`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ resource_id: "lfw-funneled-local-candidate", verify_hashes }),
    }).then((r) => readJson<DatasetOut>(r)),
  datasetStatistics: (project_id: string, dataset_id: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/datasets/${encodeURIComponent(dataset_id)}/statistics`).then((r) => readJson<{ dataset_id: string; evidence_sha256: string; statistics: Record<string, any> }>(r)),
  startBatch: (project_id: string, dataset_id: string, selection: Record<string, unknown>, chunks: number, purpose_scope: string | null) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/datasets/${encodeURIComponent(dataset_id)}/quality-batch`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(purpose_scope ? { selection, chunks, purpose_scope } : { selection, chunks }),
    }).then((r) => readJson<JobOut>(r)),
  job: (project_id: string, job_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/jobs/${encodeURIComponent(job_id)}`).then((r) => readJson<JobOut>(r)),
  cancelJob: (project_id: string, job_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/jobs/${encodeURIComponent(job_id)}/cancel`, { method: "POST" }).then((r) => readJson<JobOut>(r)),
  projectJobs: (project_id: string) => fetch(`/api/projects/${encodeURIComponent(project_id)}/jobs`).then((r) => readJson<JobOut[]>(r)),
  qualityMeasures: (project_id: string, job_id: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/jobs/${encodeURIComponent(job_id)}/quality-measures`).then((r) => readJson<QualityMeasureCatalogOut>(r)),
  distribution: (project_id: string, job_id: string, measure: string, component: "scalar" | "native", bins: number) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/jobs/${encodeURIComponent(job_id)}/distribution?measure=${encodeURIComponent(measure)}&component=${component}&bins=${bins}`).then((r) => readJson<DistributionOut>(r)),
  drilldown: (project_id: string, job_id: string, min_scalar: number, max_scalar: number, limit = 60) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/jobs/${encodeURIComponent(job_id)}/drilldown?min_scalar=${min_scalar}&max_scalar=${max_scalar}&outcome=MEASURED&limit=${limit}`).then((r) => readJson<{ total: number; rows: DrilldownRow[] }>(r)),
  drilldownFailures: (project_id: string, job_id: string, limit = 60) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/jobs/${encodeURIComponent(job_id)}/drilldown?outcome=ENGINE_ROW_FAILURE&limit=${limit}`).then((r) => readJson<{ total: number; rows: DrilldownRow[] }>(r)),
  sampleQuality: (project_id: string, sample_id: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/samples/${encodeURIComponent(sample_id)}/quality`).then((r) => readJson<SampleQualityOut>(r)),
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
  projectPurgeScope: (project_id: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/purge-scope`).then((r) => readJson<ProjectPurgeScopeOut>(r)),
  purgeProject: (project_id: string, confirm: string) =>
    fetch(`/api/projects/${encodeURIComponent(project_id)}/purge`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ confirm }),
    }).then((r) => readJson<ProjectPurgeOut>(r)),
  health: () => fetch("/api/health").then((r) => readJson<HealthOut>(r)),
  ready: () => fetch("/api/ready").then((response) => {
    if (response.status === 200 || response.status === 503) {
      return response.json() as Promise<ReadyOut>;
    }
    return readJson<ReadyOut>(response);
  }),
  operationalMetrics: () => fetch("/api/metrics").then((r) => readJson<OperationalMetricsOut>(r)),
  resources: () => fetch("/api/resources").then((r) => readJson<ResourceOut[]>(r)),
  registeredSamples: () => fetch("/api/samples/registered").then((r) => readJson<RegisteredSample[]>(r)),
  runs: (projectId: string) =>
    fetch(`/api/projects/${encodeURIComponent(projectId)}/runs?limit=10000`).then((r) => readJson<RunSummaryOut[]>(r)),
  assess: (registered_sample_id: string, purpose_scope: string, project_id: string | null) =>
    fetch("/api/quality/assess", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(project_id ? { registered_sample_id, purpose_scope, project_id } : { registered_sample_id, purpose_scope }),
    }).then((r) => readJson<AssessResponse>(r)),
  registeredSampleDetection: (registered_sample_id: string) =>
    fetch(`/api/detection?registered_sample_id=${encodeURIComponent(registered_sample_id)}`).then((r) => readJson<DetectionGeometryOut>(r)),
  registeredSampleOfiqLandmarks: (registered_sample_id: string) =>
    fetch(`/api/ofiq-landmarks?registered_sample_id=${encodeURIComponent(registered_sample_id)}`).then((r) => readJson<OfiqLandmarksOut>(r)),
  imageUrl: (registered_sample_id: string) => `/api/samples/image?registered_sample_id=${encodeURIComponent(registered_sample_id)}`,
};

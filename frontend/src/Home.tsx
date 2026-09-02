import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type AddonCatalogOut,
  type BundleOut,
  type DatasetOut,
  type DegradationSummary,
  type FigureOut,
  type HealthOut,
  type JobOut,
  type MatcherLeaf,
  type ProjectOut,
  type QualityMeasureCatalogOut,
  type ReportOut,
  type RunSummaryOut,
  type UtilitySummary,
} from "./api";

interface HomeProps {
  activeProjectId: string | null;
  hasWorkingSample: boolean;
}

interface Feed<T> {
  items: T[];
  error: string | null;
}

interface ProjectFeeds {
  runs: Feed<RunSummaryOut>;
  jobs: Feed<JobOut>;
  datasets: Feed<DatasetOut>;
  matcherLeaves: Feed<MatcherLeaf>;
  utility: Feed<UtilitySummary>;
  degradations: Feed<DegradationSummary>;
  figures: Feed<FigureOut>;
  reports: Feed<ReportOut>;
  bundles: Feed<BundleOut>;
}

interface HomeData {
  health: HealthOut;
  projects: ProjectOut[];
  catalog: AddonCatalogOut;
  activeProject: ProjectOut | null;
  feeds: ProjectFeeds;
  measureCatalog: QualityMeasureCatalogOut | null;
  measureError: string | null;
}

type HomeState =
  | { phase: "loading" }
  | { phase: "error"; detail: string }
  | { phase: "ready"; data: HomeData };

type TaskRequirement = "project" | "dataset" | "sample" | "utility" | "run";

interface HomeTask {
  label: string;
  href: string;
  detail: string;
  requirement: TaskRequirement;
}

const PRIMARY_TASKS: readonly HomeTask[] = [
  {
    label: "Inspect one image",
    href: "#/assess",
    detail: "Upload or select an admitted image, assess its recorded OFIQ measures, and inspect face geometry.",
    requirement: "project",
  },
  {
    label: "Compare images",
    href: "#/matchers",
    detail: "Upload two independently recorded images and compare them with a selected matcher and preprocessing profile.",
    requirement: "project",
  },
  {
    label: "Stress-test an image",
    href: "#/advanced",
    detail: "Apply catalog-recorded transformations to the active working sample and inspect measured geometry and quality response.",
    requirement: "sample",
  },
] as const;

const SECONDARY_TASKS: readonly HomeTask[] = [
  { label: "Analyze a dataset", href: "#/data", detail: "Import, assess, and explore recorded distributions.", requirement: "project" },
  { label: "Compare matchers", href: "#/matchers", detail: "Run a frozen protocol across compatible systems.", requirement: "dataset" },
  { label: "Quality utility", href: "#/utility", detail: "Relate a recorded quality measure to recognition outcomes.", requirement: "utility" },
  { label: "Build a report", href: "#/reports", detail: "Review evidence and create a reproducible result package.", requirement: "run" },
] as const;

interface RecentWork {
  key: string;
  kind: "run" | "utility" | "degradation" | "figure" | "report" | "bundle";
  label: string;
  detail: string;
  recordedAt: string;
  href: string;
}

const EMPTY_FEEDS: ProjectFeeds = {
  runs: { items: [], error: null },
  jobs: { items: [], error: null },
  datasets: { items: [], error: null },
  matcherLeaves: { items: [], error: null },
  utility: { items: [], error: null },
  degradations: { items: [], error: null },
  figures: { items: [], error: null },
  reports: { items: [], error: null },
  bundles: { items: [], error: null },
};

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function feedFrom<T>(result: PromiseSettledResult<T[]>): Feed<T> {
  return result.status === "fulfilled"
    ? { items: result.value, error: null }
    : { items: [], error: errorText(result.reason) };
}

function unavailableReason(task: HomeTask, data: HomeData, hasWorkingSample: boolean): string | null {
  if (!data.activeProject) return "Select or create a project before starting this workflow.";
  if (task.requirement === "project") return null;
  if (task.requirement === "sample") {
    return hasWorkingSample ? null : "Upload and assess a working image before stress-testing it.";
  }
  if (task.requirement === "dataset") {
    if (data.feeds.datasets.error) return "Dataset prerequisites are unavailable because the project dataset feed could not be read.";
    return data.feeds.datasets.items.length ? null : "Import a dataset before starting this workflow.";
  }
  if (task.requirement === "run") {
    if (data.feeds.runs.error) return "Recorded evidence is unavailable because the project Run feed could not be read.";
    return data.feeds.runs.items.length ? null : "Record a Run before building a report.";
  }
  if (data.feeds.jobs.error) return "Quality utility is unavailable because the project quality-job feed could not be read.";
  if (data.feeds.matcherLeaves.error) return "Quality utility is unavailable because the matcher-result feed could not be read.";
  const completedQuality = data.feeds.jobs.items.some((job) => job.state === "COMPLETED" && job.run_id);
  if (!completedQuality) return "Complete a quality batch before analyzing quality utility.";
  return data.feeds.matcherLeaves.items.length ? null : "Record a matcher result before analyzing quality utility.";
}

function recentWork(data: HomeData): RecentWork[] {
  const records: RecentWork[] = [];
  records.push(...data.feeds.runs.items.map((run) => ({
    key: `run:${run.run_id}`,
    kind: "run" as const,
    label: "Recorded Run",
    detail: run.status,
    recordedAt: run.end_time,
    href: "#/projects",
  })));
  records.push(...data.feeds.utility.items.map((analysis) => ({
    key: `utility:${analysis.analysis_id}`,
    kind: "utility" as const,
    label: "Quality utility analysis",
    detail: `${analysis.quality_measure} · ${analysis.matcher_id}`,
    recordedAt: analysis.created_at,
    href: "#/utility",
  })));
  records.push(...data.feeds.degradations.items.filter((analysis) => analysis.created_at).map((analysis) => ({
    key: `degradation:${analysis.analysis_id}`,
    kind: "degradation" as const,
    label: "Robustness response",
    detail: `${analysis.transforms.join(", ")} · ${analysis.levels.toLocaleString()} recorded levels`,
    recordedAt: analysis.created_at as string,
    href: "#/advanced",
  })));
  records.push(...data.feeds.figures.items.map((figure) => ({
    key: `figure:${figure.figure_id}`,
    kind: "figure" as const,
    label: figure.title,
    detail: "Recorded figure",
    recordedAt: figure.created_at,
    href: "#/reports",
  })));
  records.push(...data.feeds.reports.items.map((report) => ({
    key: `report:${report.report_id}`,
    kind: "report" as const,
    label: report.title,
    detail: "Recorded report",
    recordedAt: report.created_at,
    href: "#/reports",
  })));
  records.push(...data.feeds.bundles.items.map((bundle) => ({
    key: `bundle:${bundle.bundle_id}`,
    kind: "bundle" as const,
    label: "Reproduction bundle",
    detail: bundle.download.state,
    recordedAt: bundle.created_at,
    href: "#/reports",
  })));
  return records.sort((left, right) => right.recordedAt.localeCompare(left.recordedAt)).slice(0, 6);
}

export function Home({ activeProjectId, hasWorkingSample }: HomeProps) {
  const [state, setState] = useState<HomeState>({ phase: "loading" });
  const refreshSequence = useRef(0);

  const refresh = useCallback(async () => {
    const sequence = ++refreshSequence.current;
    setState({ phase: "loading" });
    try {
      const [health, projects, catalog] = await Promise.all([api.health(), api.projects(), api.addons()]);
      if (refreshSequence.current !== sequence) return;
      const activeProject = projects.find((project) => project.project_id === activeProjectId) ?? null;
      let feeds = EMPTY_FEEDS;
      let measureCatalog: QualityMeasureCatalogOut | null = null;
      let measureError: string | null = null;
      if (activeProject) {
        const results = await Promise.allSettled([
          api.runs(activeProject.project_id),
          api.projectJobs(activeProject.project_id),
          api.datasets(activeProject.project_id),
          api.matcherLeaves(activeProject.project_id),
          api.utilityAnalyses(activeProject.project_id),
          api.degradations(activeProject.project_id),
          api.figures(activeProject.project_id),
          api.reports(activeProject.project_id),
          api.bundles(activeProject.project_id),
        ]);
        if (refreshSequence.current !== sequence) return;
        feeds = {
          runs: feedFrom(results[0]),
          jobs: feedFrom(results[1]),
          datasets: feedFrom(results[2]),
          matcherLeaves: feedFrom(results[3]),
          utility: feedFrom(results[4]),
          degradations: feedFrom(results[5]),
          figures: feedFrom(results[6]),
          reports: feedFrom(results[7]),
          bundles: feedFrom(results[8]),
        };
        if (feeds.jobs.error) {
          measureError = `Quality-job feed: ${feeds.jobs.error}`;
        } else {
          const completedJobs = feeds.jobs.items
            .filter((job) => (
              job.state === "COMPLETED"
              && job.run_id
              && job.ended_at
              && job.quality_result_available
            ))
            .sort((left, right) => (right.ended_at ?? "").localeCompare(left.ended_at ?? ""));
          const selectedJob = completedJobs[0];
          if (selectedJob) {
            try {
              measureCatalog = await api.qualityMeasures(activeProject.project_id, selectedJob.job_id);
            } catch (error) {
              measureError = `Quality-measure catalog: ${errorText(error)}`;
            }
          }
        }
      }
      if (refreshSequence.current !== sequence) return;
      setState({ phase: "ready", data: { health, projects, catalog, activeProject, feeds, measureCatalog, measureError } });
    } catch (error) {
      if (refreshSequence.current !== sequence) return;
      setState({ phase: "error", detail: errorText(error) });
    }
  }, [activeProjectId]);

  useEffect(() => {
    void refresh();
    return () => {
      refreshSequence.current += 1;
    };
  }, [refresh]);

  useEffect(() => {
    const refreshWhenShown = () => {
      if (window.location.hash.replace(/^#\//, "") === "home") void refresh();
    };
    window.addEventListener("hashchange", refreshWhenShown);
    return () => window.removeEventListener("hashchange", refreshWhenShown);
  }, [refresh]);

  const viewState = state.phase === "ready" ? (state.data.projects.length === 0 ? "empty" : "ready") : state.phase;
  const recent = useMemo(() => state.phase === "ready" ? recentWork(state.data) : [], [state]);
  const feedErrors = useMemo(() => {
    if (state.phase !== "ready") return [];
    const labels: Array<[string, Feed<unknown>]> = [
      ["Runs", state.data.feeds.runs],
      ["Quality jobs", state.data.feeds.jobs],
      ["Datasets", state.data.feeds.datasets],
      ["Matcher results", state.data.feeds.matcherLeaves],
      ["Utility analyses", state.data.feeds.utility],
      ["Robustness analyses", state.data.feeds.degradations],
      ["Figures", state.data.feeds.figures],
      ["Reports", state.data.feeds.reports],
      ["Bundles", state.data.feeds.bundles],
    ];
    const errors = labels.flatMap(([label, feed]) => feed.error ? [`${label}: ${feed.error}`] : []);
    if (state.data.measureError) errors.push(state.data.measureError);
    return errors;
  }, [state]);

  const taskCard = (task: HomeTask, primary: boolean, index?: number) => {
    const reason = state.phase === "ready"
      ? unavailableReason(task, state.data, hasWorkingSample)
      : state.phase === "loading"
        ? "Workspace prerequisites are loading."
        : "Workspace prerequisites are unavailable.";
    const content = (
      <>
        {primary && <span className="home-task-index" aria-hidden="true">{String((index ?? 0) + 1).padStart(2, "0")}</span>}
        <strong>{task.label}</strong>
        <span>{task.detail}</span>
        {reason && <span className="home-task-reason" data-testid="home-task-reason">{reason}</span>}
        {primary && !reason && <span className="home-task-arrow" aria-hidden="true">→</span>}
      </>
    );
    return reason ? (
      <div
        className={`home-task ${primary ? "primary" : "secondary"} unavailable`}
        data-testid="home-task"
        data-destination={task.href}
        aria-disabled="true"
        key={task.label}
      >
        {content}
      </div>
    ) : (
      <a
        className={`home-task ${primary ? "primary" : "secondary"}`}
        data-testid="home-task"
        data-destination={task.href}
        href={task.href}
        key={task.label}
      >
        {content}
      </a>
    );
  };

  return (
    <section
      className="home-panel"
      data-testid="home-panel"
      data-state={viewState}
      data-project-id={state.phase === "ready" ? state.data.activeProject?.project_id : undefined}
      aria-labelledby="home-heading"
    >
      <header className="home-heading">
        <p className="home-eyebrow">Start an analysis</p>
        <h2 id="home-heading">What do you want to examine?</h2>
        <p>Choose the task first. QualSight keeps the active project, admitted inputs, engines, and recorded evidence attached.</p>
      </header>

      <div className="home-primary-tasks" aria-label="Primary analysis tasks">
        {PRIMARY_TASKS.map((task, index) => taskCard(task, true, index))}
      </div>

      <div className="home-secondary-tasks" aria-label="Additional analysis tasks">
        {SECONDARY_TASKS.map((task) => taskCard(task, false))}
      </div>

      {state.phase === "loading" && (
        <div className="home-state" role="status">
          <strong>Loading recorded workspace state…</strong>
          <span>QualSight is reading the local control plane and project ledger.</span>
        </div>
      )}

      {state.phase === "error" && (
        <div className="home-state error" role="alert">
          <strong>Workspace state is unavailable.</strong>
          <span>{state.detail}</span>
          <button type="button" className="secondary" onClick={() => void refresh()}>Retry</button>
        </div>
      )}

      {state.phase === "ready" && (
        <div className="home-live-grid">
          <section className="home-records" aria-labelledby="home-records-heading">
            <div className="home-section-heading">
              <div>
                <p className="home-section-label">Recorded work</p>
                <h3 id="home-records-heading">{state.data.activeProject ? state.data.activeProject.name : "No active project"}</h3>
              </div>
              <div className="home-section-actions">
                <button type="button" className="secondary" onClick={() => void refresh()}>Refresh</button>
                <a className="button secondary" href="#/projects">Open projects</a>
              </div>
            </div>

            {state.data.activeProject === null ? (
              <p className="home-empty" data-testid="home-recent-feed-state" data-state="empty">
                Select an active project to review its recorded work.
              </p>
            ) : recent.length === 0 ? (
              <p
                className="home-empty"
                data-testid="home-recent-feed-state"
                data-state={feedErrors.length ? "error" : "empty"}
              >
                {feedErrors.length
                  ? "Recorded work is unavailable because one or more project feeds could not be read."
                  : "This project has no recorded Runs or analyses."}
              </p>
            ) : (
              <ol className="home-run-list" data-testid="home-recent-feed-state" data-state="ready">
                {recent.map((item) => (
                  <li
                    key={item.key}
                    data-testid="home-recent-item"
                    data-kind={item.kind}
                    data-recorded-at={item.recordedAt}
                  >
                    <div>
                      <strong>{item.label}</strong>
                      <span>{item.detail}</span>
                    </div>
                    <div className="home-run-state">
                      <time dateTime={item.recordedAt}>{new Date(item.recordedAt).toLocaleString()}</time>
                      <a href={item.href}>Open</a>
                    </div>
                  </li>
                ))}
              </ol>
            )}

            {feedErrors.length > 0 && (
              <div className="home-feed-errors" data-testid="home-feed-errors" role="status">
                <strong>Some workspace feeds are unavailable.</strong>
                <ul>
                  {feedErrors.map((error) => <li key={error}>{error}</li>)}
                </ul>
                <button type="button" className="secondary" onClick={() => void refresh()}>Retry unavailable feeds</button>
              </div>
            )}
          </section>

          <aside className="home-snapshot" aria-labelledby="home-snapshot-heading">
            <p className="home-section-label">Live workstation state</p>
            <h3 id="home-snapshot-heading">Workspace at a glance</h3>
            <dl>
              <div>
                <dt>Recorded projects</dt>
                <dd data-testid="home-project-count">{state.data.projects.length}</dd>
              </div>
              <div>
                <dt>Active-project Runs</dt>
                <dd
                  data-testid="home-run-count"
                  data-state={state.data.activeProject && !state.data.feeds.runs.error ? "recorded" : "unavailable"}
                >
                  {state.data.activeProject && !state.data.feeds.runs.error ? state.data.feeds.runs.items.length : "—"}
                </dd>
              </div>
              <div>
                <dt>Matcher add-ons</dt>
                <dd data-testid="home-matcher-count">{state.data.catalog.matchers.length}</dd>
              </div>
              <div>
                <dt>Recorded OFIQ measures</dt>
                <dd
                  data-testid="home-measure-count"
                  data-state={state.data.measureCatalog ? "recorded" : "unavailable"}
                >
                  {state.data.measureCatalog ? state.data.measureCatalog.measures.length : "—"}
                </dd>
              </div>
              <div>
                <dt>Transform operators</dt>
                <dd data-testid="home-transform-count">{state.data.catalog.transforms.length}</dd>
              </div>
            </dl>
            {(!state.data.activeProject || state.data.feeds.runs.error) && (
              <p className="home-count-reason" data-testid="home-run-reason">
                {state.data.feeds.runs.error
                  ? `Run count unavailable: ${state.data.feeds.runs.error}`
                  : "Select an active project to count its recorded Runs."}
              </p>
            )}
            {!state.data.measureCatalog && (
              <p className="home-count-reason" data-testid="home-measure-reason">
                {state.data.measureError ?? "Complete a quality batch to record the available OFIQ measures."}
              </p>
            )}
            <p className="home-engine" data-testid="home-engine">
              <span className="home-engine-dot" aria-hidden="true" />
              {state.data.health.engine.name} {state.data.health.engine.version} · {state.data.health.engine.state}
            </p>
          </aside>
        </div>
      )}
    </section>
  );
}

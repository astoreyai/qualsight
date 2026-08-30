import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import {
  api,
  type AddonCatalogOut,
  type EmbeddingAssetOut,
  type HealthOut,
  type MatcherAddonOut,
  type ResourceOut,
  type SystemEventOut,
} from "./api";

function shortHash(value: string, keep = 12): string {
  const marker = value.indexOf("sha256:");
  const digest = marker >= 0 ? value.slice(marker + 7) : value;
  return `${digest.slice(0, keep)}…`;
}

function errorText(value: unknown): string {
  return value instanceof Error ? value.message : String(value);
}

interface SystemPanelProps {
  projectId: string | null;
}

type SystemArea = "addons" | "connections" | "embeddings" | "audit";

const SYSTEM_AREAS: ReadonlyArray<{ id: SystemArea; label: string }> = [
  { id: "addons", label: "Add-ons" },
  { id: "connections", label: "Connections" },
  { id: "embeddings", label: "Project embeddings" },
  { id: "audit", label: "Audit & resources" },
];

/** System control surface for the real registry, matcher assets, derived embeddings, and durable actions. */
export function SystemPanel({ projectId }: SystemPanelProps) {
  const [health, setHealth] = useState<HealthOut | null>(null);
  const [resources, setResources] = useState<ResourceOut[]>([]);
  const [catalog, setCatalog] = useState<AddonCatalogOut | null>(null);
  const [assets, setAssets] = useState<MatcherAddonOut[]>([]);
  const [embeddings, setEmbeddings] = useState<EmbeddingAssetOut[]>([]);
  const [embeddingsError, setEmbeddingsError] = useState<string | null>(null);
  const [events, setEvents] = useState<SystemEventOut[]>([]);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);
  const [activeArea, setActiveArea] = useState<SystemArea>("addons");
  const [filter, setFilter] = useState<"ALL" | "MATCHER" | "DATASET" | "PREPROCESSING">("ALL");
  const refreshSequence = useRef(0);
  const activeOperation = useRef<string | null>(null);

  const refresh = useCallback(async () => {
    const requestSequence = ++refreshSequence.current;
    setPhase("loading");
    setError(null);
    setWarning(null);
    setEmbeddings([]);
    setEmbeddingsError(null);
    const [healthResult, resourcesResult, catalogResult, eventsResult, embeddingsResult] = await Promise.allSettled([
      api.health(),
      api.resources(),
      api.addons(),
      api.systemEvents(),
      projectId ? api.embeddings(projectId) : Promise.resolve([] as EmbeddingAssetOut[]),
    ]);
    if (requestSequence !== refreshSequence.current) return;
    if (healthResult.status === "fulfilled") setHealth(healthResult.value);
    if (resourcesResult.status === "fulfilled") setResources(resourcesResult.value);
    if (eventsResult.status === "fulfilled") setEvents(eventsResult.value);
    if (embeddingsResult.status === "fulfilled") setEmbeddings(embeddingsResult.value);
    else setEmbeddingsError(errorText(embeddingsResult.reason));
    if (catalogResult.status === "rejected") {
      setPhase("error");
      setError(errorText(catalogResult.reason));
      return;
    }
    setCatalog(catalogResult.value);
    setAssets(catalogResult.value.matchers);
    const unavailable: string[] = [];
    if (healthResult.status === "rejected") unavailable.push(`health: ${errorText(healthResult.reason)}`);
    if (resourcesResult.status === "rejected") unavailable.push(`resource registry: ${errorText(resourcesResult.reason)}`);
    if (eventsResult.status === "rejected") unavailable.push(`system events: ${errorText(eventsResult.reason)}`);
    if (embeddingsResult.status === "rejected") unavailable.push(`project embeddings: ${errorText(embeddingsResult.reason)}`);
    setWarning(unavailable.length ? `Some system context is unavailable. ${unavailable.join(" · ")}` : null);
    setPhase("ready");
  }, [projectId]);

  useEffect(() => {
    void refresh();
    return () => {
      refreshSequence.current += 1;
    };
  }, [refresh]);

  const act = useCallback(async (matcherId: string, action: "pull" | "checkpoint" | "load" | "unload") => {
    const operation = `${matcherId}:${action}`;
    if (activeOperation.current !== null) return;
    activeOperation.current = operation;
    setBusy(operation);
    setMessage(null);
    try {
      const response = action === "pull"
        ? await api.pullMatcherSources(matcherId)
        : action === "checkpoint"
          ? await api.pullMatcherCheckpoint(matcherId)
        : action === "load"
          ? await api.loadMatcher(matcherId)
          : await api.unloadMatcher(matcherId);
      setAssets((current) => current.map((item) => item.matcher_id === matcherId ? { ...item, ...response.asset } : item));
      setMessage({ kind: "success", text: `${response.asset.name}: ${response.event.event_type.toLowerCase().replaceAll("_", " ")} · evidence ${shortHash(response.event.evidence_sha256)}` });
      try {
        setEvents(await api.systemEvents());
      } catch (caught) {
        setWarning(`Matcher action completed, but system events could not be refreshed: ${errorText(caught)}`);
      }
    } catch (caught) {
      setMessage({ kind: "error", text: caught instanceof Error ? caught.message : String(caught) });
    } finally {
      if (activeOperation.current === operation) {
        activeOperation.current = null;
        setBusy((current) => current === operation ? null : current);
      }
    }
  }, []);

  const acquireDataset = useCallback(async (addonId: string) => {
    const operation = `${addonId}:dataset`;
    if (activeOperation.current !== null) return;
    activeOperation.current = operation;
    setBusy(operation);
    setMessage(null);
    try {
      const response = await api.pullVerificationDataset(addonId);
      setCatalog((current) => current === null ? current : {
        ...current,
        datasets: current.datasets.map((item) => item.addon_id === addonId ? response.dataset : item),
      });
      setMessage({ kind: "success", text: `${response.dataset.display_name}: downloaded and verified · evidence ${shortHash(response.event.evidence_sha256)}` });
      try {
        const refreshedCatalog = await api.addons();
        setCatalog(refreshedCatalog);
        setAssets(refreshedCatalog.matchers);
      } catch (caught) {
        setWarning(`Dataset acquisition completed, but the shared add-on catalog could not be refreshed: ${errorText(caught)}`);
      }
      try {
        setEvents(await api.systemEvents());
      } catch (caught) {
        setWarning((current) => [current, `Dataset acquisition completed, but system events could not be refreshed: ${errorText(caught)}`].filter(Boolean).join(" · "));
      }
    } catch (caught) {
      setMessage({ kind: "error", text: errorText(caught) });
    } finally {
      if (activeOperation.current === operation) {
        activeOperation.current = null;
        setBusy((current) => current === operation ? null : current);
      }
    }
  }, []);

  const importDataset = useCallback(async (addonId: string) => {
    if (!projectId) return;
    const operation = `${addonId}:import`;
    if (activeOperation.current !== null) return;
    activeOperation.current = operation;
    setBusy(operation);
    setMessage(null);
    try {
      const response = await api.importVerificationDataset(projectId, addonId);
      setMessage({
        kind: "success",
        text: `${response.dataset.name}: imported ${response.samples.toLocaleString()} real samples and the author-published ${response.pairs.toLocaleString()}-pair protocol into the selected project. Open Datasets to activate it.`,
      });
    } catch (caught) {
      setMessage({ kind: "error", text: errorText(caught) });
    } finally {
      if (activeOperation.current === operation) {
        activeOperation.current = null;
        setBusy((current) => current === operation ? null : current);
      }
    }
  }, [projectId]);

  const moveAreaFocus = (event: KeyboardEvent<HTMLButtonElement>, currentIndex: number) => {
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % SYSTEM_AREAS.length;
    if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + SYSTEM_AREAS.length) % SYSTEM_AREAS.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = SYSTEM_AREAS.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    const nextArea = SYSTEM_AREAS[nextIndex];
    if (!nextArea) return;
    setActiveArea(nextArea.id);
    document.getElementById(`system-tab-${nextArea.id}`)?.focus();
  };

  const refreshDisabledReason = phase === "loading"
    ? "System state verification is in progress."
    : busy !== null
      ? "An asset lifecycle action is in progress."
      : null;

  return (
    <section className="panel system" aria-labelledby="system-heading" data-testid="system-panel">
      <div className="section-heading-row">
        <div>
          <h2 id="system-heading">System management</h2>
          <p className="hint">Manage the beta's closed allowlist of real, code-owned drivers, inspect project embeddings, and review evidence retained by the local runtime.</p>
        </div>
        <button
          type="button"
          className="secondary"
          onClick={() => void refresh()}
          disabled={refreshDisabledReason !== null}
          aria-describedby={refreshDisabledReason ? "system-refresh-reason" : undefined}
          title={refreshDisabledReason ?? "Refresh system state."}
        >
          Refresh
        </button>
      </div>
      {refreshDisabledReason && <p id="system-refresh-reason" className="hint">Refresh unavailable: {refreshDisabledReason}</p>}

      {phase === "loading" && <p className="status" role="status">Verifying registry, checkpoints, and pinned architecture sources…</p>}
      {phase === "error" && <p className="error" role="alert">System state unavailable: {error} <button type="button" className="secondary" onClick={() => void refresh()}>Retry</button></p>}
      {warning && <p className="error" role="alert">{warning}</p>}
      {message && <p className={message.kind === "error" ? "error" : "status"} role={message.kind === "error" ? "alert" : "status"}>{message.text}</p>}

      <div className="addon-filter" role="tablist" aria-label="System management areas">
        {SYSTEM_AREAS.map((area, index) => (
          <button
            key={area.id}
            id={`system-tab-${area.id}`}
            type="button"
            role="tab"
            className={activeArea === area.id ? "secondary active" : "secondary"}
            aria-selected={activeArea === area.id}
            aria-controls={`system-panel-${area.id}`}
            tabIndex={activeArea === area.id ? 0 : -1}
            onClick={() => setActiveArea(area.id)}
            onKeyDown={(event) => moveAreaFocus(event, index)}
          >
            {area.label}
          </button>
        ))}
      </div>

      <section id="system-panel-addons" role="tabpanel" aria-labelledby="system-tab-addons" tabIndex={0} hidden={activeArea !== "addons"}>
        <div className="addon-filter" role="group" aria-label="Filter registered add-ons">
          {(["ALL", "MATCHER", "DATASET", "PREPROCESSING"] as const).map((kind) => (
            <button key={kind} type="button" className={filter === kind ? "secondary active" : "secondary"} aria-pressed={filter === kind} onClick={() => setFilter(kind)}>
              {kind === "ALL" ? "All" : kind === "MATCHER" ? "Matchers" : kind === "DATASET" ? "Datasets" : "Preprocessing"}
            </button>
          ))}
        </div>

        {(filter === "ALL" || filter === "MATCHER") && <>
          <h3>Matcher add-ons</h3>
          {phase === "ready" && assets.length === 0 && <p className="empty">No matcher add-ons are registered.</p>}
          <div className="asset-grid" data-testid="matcher-assets">
            {assets.map((asset) => {
              const sourceReady = asset.architecture_sources.every((source) => source.state === "VERIFIED");
              const phaseReason = phase === "loading"
                ? "System registry verification is still running."
                : phase === "error"
                  ? "System registry verification failed; retry the system refresh."
                  : null;
              const actionReason = busy !== null ? "Another matcher lifecycle action is in progress." : null;
              const pullReason = phaseReason ?? actionReason;
              const managedCheckpointReady = asset.checkpoint.state === "VERIFIED"
                && asset.checkpoint.path === asset.checkpoint_acquisition.managed_path;
              const checkpointReason = phaseReason
                ?? actionReason
                ?? (!asset.checkpoint_acquisition.available ? asset.checkpoint_acquisition.reason : null)
                ?? (managedCheckpointReady ? "The managed checkpoint is already verified." : null);
              const loadReason = phaseReason
                ?? actionReason
                ?? (asset.runtime.state === "LOADED" ? "The matcher is already loaded." : null)
                ?? (!asset.ready_to_load ? asset.blocking_reasons.join(" · ") || "The checkpoint and architecture sources must be verified before loading." : null);
              const unloadReason = phaseReason
                ?? actionReason
                ?? (asset.runtime.state !== "LOADED" ? "The matcher is not loaded." : null)
                ?? (asset.runtime.active_leases > 0 ? `${asset.runtime.active_leases} active job lease(s) must finish before unloading.` : null);
              const controlId = asset.matcher_id.replace(/[^a-zA-Z0-9_-]/g, "-");
              return (
                <article className="asset-card" key={asset.matcher_id} data-matcher-id={asset.matcher_id}>
                  <div className="asset-title">
                    <div><strong>{asset.name}</strong><span className="mono">{asset.matcher_id}</span></div>
                    <span className={`state-pill ${asset.runtime.state === "LOADED" ? "ok" : "neutral"}`}>{asset.runtime.state}</span>
                  </div>
                  <dl className="facts">
                    <dt>Checkpoint</dt><dd><span className={`state-pill ${asset.checkpoint.state === "VERIFIED" ? "ok" : "bad"}`}>{asset.checkpoint.state}</span> <span className="mono">{shortHash(asset.checkpoint.registered_sha256)}</span></dd>
                    <dt>Acquisition</dt><dd>{asset.checkpoint_acquisition.available ? `Admitted · ${asset.checkpoint_acquisition.source_host}` : "Rights blocked"}{managedCheckpointReady ? " · managed" : ""}</dd>
                    <dt>Architecture</dt><dd>{asset.architecture_sources.map((source) => `${source.key}: ${source.state}`).join(" · ")}</dd>
                    <dt>Runtime</dt><dd>{asset.runtime.device ? `${asset.runtime.device} · ${asset.runtime.parameter_count?.toLocaleString()} parameters` : "not resident"}{asset.runtime.active_leases ? ` · ${asset.runtime.active_leases} active job lease(s)` : ""}</dd>
                    <dt>Rights</dt><dd>{asset.third_party_rights_status} · {asset.redistribution_status}</dd>
                    <dt>Classification</dt><dd>{asset.data_classification}</dd>
                  </dl>
                  {asset.blocking_reasons.length > 0 && <p className="asset-blocker">{asset.blocking_reasons.join(" · ")}</p>}
                  <p className="hint">{asset.checkpoint_acquisition.reason}</p>
                  <div className="asset-actions">
                    {managedCheckpointReady
                      ? <span className="state-pill ok">Managed checkpoint verified</span>
                      : asset.checkpoint_acquisition.available
                        ? <button type="button" className="secondary" disabled={checkpointReason !== null} aria-describedby={checkpointReason ? `checkpoint-reason-${controlId}` : undefined} onClick={() => void act(asset.matcher_id, "checkpoint")}>{busy === `${asset.matcher_id}:checkpoint` ? "Acquiring…" : asset.checkpoint.state === "VERIFIED" ? "Install managed checkpoint" : "Download verified checkpoint"}</button>
                        : <span className="state-pill bad">Checkpoint download blocked</span>}
                    {sourceReady
                      ? <span className="state-pill ok">Sources verified</span>
                      : <button type="button" className="secondary" disabled={pullReason !== null} aria-describedby={pullReason ? `pull-reason-${controlId}` : undefined} onClick={() => void act(asset.matcher_id, "pull")}>{busy === `${asset.matcher_id}:pull` ? "Pulling…" : "Pull pinned sources"}</button>}
                    <button type="button" className="primary" disabled={loadReason !== null} aria-describedby={loadReason ? `load-reason-${controlId}` : undefined} onClick={() => void act(asset.matcher_id, "load")}>{busy === `${asset.matcher_id}:load` ? "Loading…" : "Load matcher"}</button>
                    <button type="button" className="secondary" disabled={unloadReason !== null} aria-describedby={unloadReason ? `unload-reason-${controlId}` : undefined} onClick={() => void act(asset.matcher_id, "unload")}>{busy === `${asset.matcher_id}:unload` ? "Unloading…" : "Unload"}</button>
                  </div>
                  {(checkpointReason || pullReason || loadReason || unloadReason) && (
                    <ul className="hint" aria-label={`Unavailable lifecycle actions for ${asset.name}`}>
                      {checkpointReason && !managedCheckpointReady && <li id={`checkpoint-reason-${controlId}`}>Checkpoint acquisition unavailable: {checkpointReason}</li>}
                      {!sourceReady && pullReason && <li id={`pull-reason-${controlId}`}>Pull unavailable: {pullReason}</li>}
                      {loadReason && <li id={`load-reason-${controlId}`}>Load unavailable: {loadReason}</li>}
                      {unloadReason && <li id={`unload-reason-${controlId}`}>Unload unavailable: {unloadReason}</li>}
                    </ul>
                  )}
                </article>
              );
            })}
          </div>
        </>}

        {(filter === "ALL" || filter === "DATASET") && <>
          <h3>Dataset add-ons</h3>
          {catalog && catalog.datasets.length === 0 && <p className="empty">No registered dataset driver is admitted in this build.</p>}
          <div className="asset-grid" data-testid="dataset-addons">
            {catalog?.datasets.map((dataset) => {
              const ready = dataset.runtime_state === "READY_TO_IMPORT_AND_EXECUTE" || dataset.runtime_state === "READY_TO_IMPORT";
              const operation = `${dataset.addon_id}:dataset`;
              const downloadReason = phase === "loading"
                ? "System registry verification is still running."
                : phase === "error"
                  ? "System registry verification failed; retry the system refresh."
                  : busy !== null
                    ? "Another asset lifecycle action is in progress."
                    : !dataset.acquisition.available
                      ? dataset.acquisition.reason ?? "Dataset acquisition is not admitted."
                      : ready
                        ? "The managed dataset is already downloaded and verified."
                        : null;
              const importOperation = `${dataset.addon_id}:import`;
              const importReason = !ready
                ? "Download and verify the managed dataset before importing it."
                : !projectId
                  ? "Select a writable project before importing this dataset and its protocol."
                  : busy !== null
                    ? "Another asset lifecycle action is in progress."
                    : null;
              const controlId = dataset.addon_id.replace(/[^a-zA-Z0-9_-]/g, "-");
              const population = dataset.identity_count === null
                ? `${dataset.sample_count.toLocaleString()} aligned samples · ${dataset.pair_count?.toLocaleString() ?? 0} verification pairs (${dataset.mated_pair_count?.toLocaleString() ?? 0} mated, ${dataset.nonmated_pair_count?.toLocaleString() ?? 0} nonmated) · no identity labels`
                : `${dataset.sample_count.toLocaleString()} samples · ${dataset.identity_count.toLocaleString()} identities`;
              const integrity = dataset.tree_sha256
                ? `tree ${shortHash(dataset.tree_sha256, 16)}`
                : dataset.acquisition.archive_sha256
                  ? `archive ${shortHash(dataset.acquisition.archive_sha256, 16)}`
                  : "No acquisition identity registered";
              return (
                <article className="asset-card" key={dataset.addon_id} data-addon-id={dataset.addon_id}>
                  <div className="asset-title"><div><strong>{dataset.display_name}</strong><span className="mono">{dataset.addon_id}</span></div><span className={`state-pill ${ready ? "ok" : dataset.runtime_state === "DRIFTED" || dataset.runtime_state === "BLOCKED" ? "bad" : "neutral"}`}>{dataset.runtime_state}</span></div>
                  <dl className="facts">
                    <dt>Installed</dt><dd>{dataset.installation_state} · {dataset.managed ? "QualSight managed" : "externally managed"}</dd>
                    <dt>Population</dt><dd>{population}</dd>
                    <dt>Integrity</dt><dd className="mono">{integrity}</dd>
                    <dt>Rights</dt><dd>{dataset.rights.third_party} · {dataset.rights.redistribution}</dd>
                    <dt>Driver</dt><dd className="mono">{dataset.driver_id}</dd>
                  </dl>
                  {dataset.acquisition.rights_notice && <p className="hint">{dataset.acquisition.rights_notice}</p>}
                  {!dataset.acquisition.available && <p className="asset-blocker"><strong>{dataset.acquisition.refusal_code}</strong> · {dataset.acquisition.reason}</p>}
                  {dataset.blocking_reasons.length > 0 && dataset.acquisition.available && <p className="asset-blocker">{dataset.blocking_reasons.join(" · ")}</p>}
                  <div className="asset-actions">
                    {dataset.acquisition.available
                      ? ready
                        ? <><span className="state-pill ok">Verified local protocol</span><button type="button" className="primary" disabled={importReason !== null} aria-describedby={importReason ? `dataset-import-reason-${controlId}` : undefined} onClick={() => void importDataset(dataset.addon_id)}>{busy === importOperation ? "Importing…" : "Import into selected project"}</button></>
                        : <button type="button" className="primary" disabled={downloadReason !== null} aria-describedby={downloadReason ? `dataset-reason-${controlId}` : undefined} onClick={() => void acquireDataset(dataset.addon_id)}>{busy === operation ? "Downloading and verifying…" : "Download and verify dataset"}</button>
                      : <a className="button secondary" href="#/data">Open dataset imports</a>}
                  </div>
                  {downloadReason && dataset.acquisition.available && !ready && <p id={`dataset-reason-${controlId}`} className="hint">Download unavailable: {downloadReason}</p>}
                  {importReason && dataset.acquisition.available && ready && <p id={`dataset-import-reason-${controlId}`} className="hint">Import unavailable: {importReason}</p>}
                </article>
              );
            })}
          </div>
        </>}

        {(filter === "ALL" || filter === "PREPROCESSING") && <>
          <h3>Matcher preprocessing profiles</h3>
          <p className="hint">Profiles are selected per Run. Changing the selection never mutates a completed Run or a global runtime setting.</p>
          <div className="asset-grid" data-testid="preprocessing-addons">
            {catalog?.preprocessing.map((profile) => (
              <article className="asset-card" key={profile.addon_id} data-addon-id={profile.addon_id}>
                <div className="asset-title"><div><strong>{profile.display_name}</strong><span className="mono">{profile.addon_id}</span></div><span className={`state-pill ${profile.runtime_state === "READY" ? "ok" : "bad"}`}>{profile.runtime_state}</span></div>
                <dl className="facts">
                  <dt>Input</dt><dd>{profile.input_representation === "AUTHOR_PREALIGNED_ARCFACE112" ? "Publisher-prealigned 112×112" : "Source image requiring detection"}</dd>
                  <dt>Detector</dt><dd className="mono">{profile.detector_resource_id ?? "Not used"}</dd>
                  <dt>Canvas</dt><dd>{profile.definition.canvas}</dd>
                  <dt>Face choice</dt><dd>{profile.definition.selection}</dd>
                  <dt>Alignment</dt><dd>{profile.definition.alignment}</dd>
                  <dt>Transport</dt><dd>{profile.definition.transport}</dd>
                  <dt>Driver</dt><dd className="mono">{profile.driver_id}</dd>
                </dl>
                {profile.blocking_reasons.length > 0 && <p className="asset-blocker">{profile.blocking_reasons.join(" · ")}</p>}
                <div className="asset-actions"><a className="button secondary" href="#/matchers">Select for a verification Run</a></div>
              </article>
            ))}
          </div>
          <details>
            <summary>Controlled degradation operators ({catalog?.transforms.length ?? 0})</summary>
            <div className="resource-grid" data-testid="transform-addons">
              {catalog?.transforms.map((transform) => <article key={transform.addon_id} className="resource-card" data-addon-id={transform.addon_id}><strong>{transform.display_name}</strong><span className="mono">{transform.driver_id}</span><span>parameter: {transform.parameter} · selected per derived set</span></article>)}
            </div>
          </details>
        </>}
      </section>

      <section id="system-panel-connections" role="tabpanel" aria-labelledby="system-tab-connections" tabIndex={0} hidden={activeArea !== "connections"}>
        <h3>API connections</h3>
        {catalog && catalog.connections.length === 0 && (
          <div className="empty addon-empty" data-testid="connection-addons">
            <strong>No API connections are admitted in this beta.</strong>
            <span>{catalog.connection_policy.reason}</span>
            <details><summary>Admission requirements</summary><ul>{catalog.connection_policy.admission_requires.map((requirement) => <li key={requirement}>{requirement}</li>)}</ul></details>
          </div>
        )}
      </section>

      <section id="system-panel-embeddings" role="tabpanel" aria-labelledby="system-tab-embeddings" tabIndex={0} hidden={activeArea !== "embeddings"}>
        <h3>Project embeddings</h3>
        {!projectId && (
          <div className="empty-state">
            <strong>Select a project to inventory embeddings.</strong>
            <span>Embedding artifacts are derived evidence and remain scoped to the project that produced them.</span>
            <a className="button secondary" href="#/projects">Open projects</a>
          </div>
        )}
        {projectId && phase === "loading" && <p className="status" role="status">Loading embedding inventory for the selected project…</p>}
        {projectId && phase !== "loading" && embeddingsError && (
          <div className="error">
            Project embeddings unavailable: {embeddingsError}{" "}
            <button type="button" className="secondary" onClick={() => void refresh()}>Retry</button>
          </div>
        )}
        {projectId && phase === "ready" && !embeddingsError && embeddings.length === 0 && <p className="empty">This project has no recorded embedding artifacts. Run a matcher matrix first.</p>}
        <div className="embedding-list" data-testid="embedding-assets">
          {embeddings.map((item) => (
            <article className="embedding-row" key={item.run_id}>
              <div><strong>{item.leaf_id}</strong> · {assets.find((asset) => asset.matcher_id === item.matcher_id)?.name ?? item.matcher_id}<span className="mono">{item.condition_id}</span></div>
              <div><span className={`state-pill ${item.integrity === "VERIFIED" ? "ok" : "bad"}`}>{item.integrity}</span> · {item.rows?.toLocaleString() ?? "—"} × {item.dimension ?? "—"} · {item.size_bytes?.toLocaleString() ?? "—"} bytes</div>
              <div className="links"><a href={item.links.run}>run</a><a href={item.links.export_parquet}>export Parquet</a><a href={item.links.export_json}>export JSON</a></div>
              <p className="hint">{item.integrity_reason ? `${item.integrity_reason} · ` : ""}{item.classification} · sha256 {shortHash(item.registered_sha256, 16)}</p>
            </article>
          ))}
        </div>
      </section>

      <section id="system-panel-audit" role="tabpanel" aria-labelledby="system-tab-audit" tabIndex={0} hidden={activeArea !== "audit"}>
        <h3>Runtime identity</h3>
        {health && (
          <dl className="facts system-facts">
            <dt>Application</dt><dd className="mono">QualSight {health.application_version}</dd>
            <dt>Engine</dt><dd>{health.engine.name} · v{health.engine.version} · {health.engine.state}</dd>
            <dt>Registry</dt><dd className="mono">{health.registry_id} · {shortHash(health.registry_sha256, 16)}</dd>
            <dt>Actor</dt><dd className="mono">{health.actor_id}</dd>
            <dt>Network guard</dt><dd className="mono">{health.network_guard.installed ? `installed · ${health.network_guard.refused_attempts.length} refused · ${health.network_guard.authorized_setup_connections} explicit setup connection(s)` : "not installed for this process"}</dd>
            <dt>Private store</dt><dd className="mono">{health.database_path}</dd>
          </dl>
        )}

        <h3>Recent system actions</h3>
        {phase === "ready" && events.length === 0 && <p className="empty">No matcher lifecycle action has been recorded.</p>}
        <ul className="system-events">
          {events.slice(0, 20).map((event) => (
            <li key={event.system_event_id}><strong>{event.event_type.replaceAll("_", " ")}</strong><span className="mono">{event.subject_id}</span><span>{event.actor_id} · {event.occurred_at.replace("T", " ").slice(0, 19)}</span><span className="mono">evidence {shortHash(event.evidence_sha256)}</span></li>
          ))}
        </ul>

        <details>
          <summary>All registered resources ({resources.length})</summary>
          <div className="resource-grid" data-testid="resource-table">
            {resources.map((item) => (
              <article key={item.resource_id} className="resource-card"><strong>{item.name}</strong><span className="mono">{item.resource_id}</span><span>{item.kind} · {item.state}</span><span>{item.third_party_rights_status} · {item.data_classification}</span></article>
            ))}
          </div>
        </details>
      </section>
    </section>
  );
}

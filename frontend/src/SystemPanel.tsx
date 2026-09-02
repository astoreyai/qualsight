import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import {
  api,
  type AddonCatalogOut,
  type DatasetAddonOut,
  type EmbeddingAssetOut,
  type HealthOut,
  type MatcherAddonOut,
  type OperationalMetricsOut,
  type ReadyOut,
  type ResourceOut,
  type SystemEventOut,
} from "./api";
import { EvidenceOpenButton, type EvidenceRequest } from "./EvidenceDetail";

function shortHash(value: string, keep = 12): string {
  const marker = value.indexOf("sha256:");
  const digest = marker >= 0 ? value.slice(marker + 7) : value;
  return `${digest.slice(0, keep)}…`;
}

function errorText(value: unknown): string {
  return value instanceof Error ? value.message : String(value);
}

function formatBytes(value: number): string {
  return `${value.toLocaleString()} bytes`;
}

function representationLabel(value: string): string {
  return value === "AUTHOR_PREALIGNED_ARCFACE112"
    ? "Publisher-prealigned ArcFace 112×112"
    : "Source image requiring face detection and alignment";
}

function parameterRange(contract: AddonCatalogOut["transforms"][number]["parameter_contract"]): string {
  const minimum = contract.minimum === undefined ? "unbounded" : `${contract.minimum} (${contract.minimum_exclusive ? "exclusive" : "inclusive"})`;
  const maximum = contract.maximum === undefined ? "unbounded" : `${contract.maximum} (${contract.maximum_exclusive ? "exclusive" : "inclusive"})`;
  return `${contract.value_type} · ${minimum} to ${maximum}`;
}

function eventDetail(event: SystemEventOut | undefined, key: string): string | null {
  const value = event?.details[key];
  return typeof value === "string" || typeof value === "number" ? String(value) : null;
}

interface SystemPanelProps {
  projectId: string | null;
  onRecovered?: (health: HealthOut) => void;
  onOpenEvidence: (request: EvidenceRequest) => void;
}

type SystemArea = "addons" | "processing" | "connections" | "runtime" | "audit";
type MatcherAction = "pull" | "checkpoint" | "load" | "unload";
type PendingAction =
  | { kind: "matcher"; action: MatcherAction; matcher: MatcherAddonOut }
  | { kind: "dataset-acquire"; dataset: DatasetAddonOut }
  | { kind: "dataset-import"; dataset: DatasetAddonOut };

const SYSTEM_AREAS: ReadonlyArray<{ id: SystemArea; label: string }> = [
  { id: "addons", label: "Add-ons" },
  { id: "processing", label: "Processing profiles" },
  { id: "connections", label: "Connections" },
  { id: "runtime", label: "Runtime & storage" },
  { id: "audit", label: "Audit" },
];

function pendingLabel(action: PendingAction): string {
  if (action.kind === "dataset-acquire") return "Download and verify dataset";
  if (action.kind === "dataset-import") return "Import dataset into selected project";
  if (action.action === "pull") return "Pull pinned architecture sources";
  if (action.action === "checkpoint") return "Acquire exact registered checkpoint";
  if (action.action === "load") return "Load matcher runtime";
  return "Unload matcher runtime";
}

function pendingTarget(action: PendingAction): string {
  return action.kind === "matcher"
    ? `${action.matcher.name} (${action.matcher.matcher_id})`
    : `${action.dataset.display_name} (${action.dataset.addon_id})`;
}

/** Operator surface for the closed catalog and local runtime evidence. */
export function SystemPanel({ projectId, onRecovered, onOpenEvidence }: SystemPanelProps) {
  const [health, setHealth] = useState<HealthOut | null>(null);
  const [ready, setReady] = useState<ReadyOut | null>(null);
  const [metrics, setMetrics] = useState<OperationalMetricsOut | null>(null);
  const [resources, setResources] = useState<ResourceOut[]>([]);
  const [catalog, setCatalog] = useState<AddonCatalogOut | null>(null);
  const [assets, setAssets] = useState<MatcherAddonOut[]>([]);
  const [embeddings, setEmbeddings] = useState<EmbeddingAssetOut[]>([]);
  const [events, setEvents] = useState<SystemEventOut[]>([]);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [feedErrors, setFeedErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);
  const [activeArea, setActiveArea] = useState<SystemArea>("addons");
  const [matcherQuery, setMatcherQuery] = useState("");
  const [selectedMatcherId, setSelectedMatcherId] = useState("");
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null);
  const refreshSequence = useRef(0);
  const projectActionSequence = useRef(0);
  const activeProjectId = useRef(projectId);
  const activeOperation = useRef<string | null>(null);
  const confirmDialog = useRef<HTMLDialogElement>(null);

  const refresh = useCallback(async () => {
    const requestSequence = ++refreshSequence.current;
    setPhase("loading");
    setError(null);
    setFeedErrors({});
    const results = await Promise.allSettled([
      api.health(),
      api.ready(),
      api.operationalMetrics(),
      api.resources(),
      api.addons(),
      api.systemEvents(),
      projectId ? api.embeddings(projectId) : Promise.resolve([] as EmbeddingAssetOut[]),
    ]);
    if (requestSequence !== refreshSequence.current) return;
    const [healthResult, readyResult, metricsResult, resourcesResult, catalogResult, eventsResult, embeddingsResult] = results;
    const unavailable: Record<string, string> = {};
    if (healthResult.status === "fulfilled") setHealth(healthResult.value); else { setHealth(null); unavailable.health = errorText(healthResult.reason); }
    if (readyResult.status === "fulfilled") setReady(readyResult.value); else { setReady(null); unavailable.readiness = errorText(readyResult.reason); }
    if (metricsResult.status === "fulfilled") setMetrics(metricsResult.value); else { setMetrics(null); unavailable.metrics = errorText(metricsResult.reason); }
    if (resourcesResult.status === "fulfilled") setResources(resourcesResult.value); else { setResources([]); unavailable.resources = errorText(resourcesResult.reason); }
    if (eventsResult.status === "fulfilled") setEvents(eventsResult.value); else { setEvents([]); unavailable.events = errorText(eventsResult.reason); }
    if (embeddingsResult.status === "fulfilled") setEmbeddings(embeddingsResult.value); else { setEmbeddings([]); unavailable.embeddings = errorText(embeddingsResult.reason); }
    setFeedErrors(unavailable);
    if (catalogResult.status === "rejected") {
      setCatalog(null);
      setAssets([]);
      setPhase("error");
      setError(errorText(catalogResult.reason));
      return;
    }
    setCatalog(catalogResult.value);
    setAssets(catalogResult.value.matchers);
    setSelectedMatcherId((current) => catalogResult.value.matchers.some((item) => item.matcher_id === current) ? current : catalogResult.value.matchers[0]?.matcher_id ?? "");
    if (healthResult.status === "fulfilled") onRecovered?.(healthResult.value);
    setPhase("ready");
  }, [onRecovered, projectId]);

  useEffect(() => {
    void refresh();
    return () => { refreshSequence.current += 1; };
  }, [refresh]);

  useLayoutEffect(() => {
    activeProjectId.current = projectId;
    projectActionSequence.current += 1;
    setMessage(null);
    setPendingAction(null);
    if (activeOperation.current?.endsWith(":import")) {
      activeOperation.current = null;
      setBusy((current) => current?.endsWith(":import") ? null : current);
    }
  }, [projectId]);

  useEffect(() => {
    const dialog = confirmDialog.current;
    if (!dialog) return;
    if (pendingAction && !dialog.open) dialog.showModal();
    if (!pendingAction && dialog.open) dialog.close();
  }, [pendingAction]);

  const refreshEvents = useCallback(async (context: string) => {
    try {
      setEvents(await api.systemEvents());
      setFeedErrors((current) => {
        const next = { ...current };
        delete next.events;
        return next;
      });
    } catch (caught) {
      setFeedErrors((current) => ({ ...current, events: `${context}: ${errorText(caught)}` }));
    }
  }, []);

  const act = useCallback(async (matcherId: string, action: MatcherAction) => {
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
      await refreshEvents("Matcher action completed, but audit refresh failed");
    } catch (caught) {
      setMessage({ kind: "error", text: errorText(caught) });
      await refreshEvents("Matcher action failed, and audit refresh failed");
    } finally {
      if (activeOperation.current === operation) {
        activeOperation.current = null;
        setBusy((current) => current === operation ? null : current);
      }
    }
  }, [refreshEvents]);

  const acquireDataset = useCallback(async (addonId: string) => {
    const operation = `${addonId}:dataset`;
    if (activeOperation.current !== null) return;
    activeOperation.current = operation;
    setBusy(operation);
    setMessage(null);
    try {
      const response = await api.pullVerificationDataset(addonId);
      const refreshedCatalog = await api.addons();
      setCatalog(refreshedCatalog);
      setAssets(refreshedCatalog.matchers);
      setMessage({ kind: "success", text: `${response.dataset.display_name}: downloaded and verified · evidence ${shortHash(response.event.evidence_sha256)}` });
      await refreshEvents("Dataset acquisition completed, but audit refresh failed");
    } catch (caught) {
      setMessage({ kind: "error", text: errorText(caught) });
      await refreshEvents("Dataset acquisition failed, and audit refresh failed");
    } finally {
      if (activeOperation.current === operation) {
        activeOperation.current = null;
        setBusy((current) => current === operation ? null : current);
      }
    }
  }, [refreshEvents]);

  const importDataset = useCallback(async (addonId: string) => {
    if (!projectId) return;
    const requestedProjectId = projectId;
    const requestSequence = projectActionSequence.current;
    const operation = `${addonId}:import`;
    if (activeOperation.current !== null) return;
    activeOperation.current = operation;
    setBusy(operation);
    setMessage(null);
    try {
      const response = await api.importVerificationDataset(requestedProjectId, addonId);
      if (
        requestSequence !== projectActionSequence.current
        || requestedProjectId !== activeProjectId.current
      ) return;
      setMessage({ kind: "success", text: `${response.dataset.name}: imported ${response.samples.toLocaleString()} real samples and the author-published ${response.pairs.toLocaleString()}-pair protocol. Open Inputs to activate it.` });
    } catch (caught) {
      if (
        requestSequence !== projectActionSequence.current
        || requestedProjectId !== activeProjectId.current
      ) return;
      setMessage({ kind: "error", text: errorText(caught) });
    } finally {
      if (
        requestSequence === projectActionSequence.current
        && requestedProjectId === activeProjectId.current
        && activeOperation.current === operation
      ) {
        activeOperation.current = null;
        setBusy((current) => current === operation ? null : current);
      }
    }
  }, [projectId]);

  const executePendingAction = useCallback((action: PendingAction) => {
    setPendingAction(null);
    if (action.kind === "dataset-acquire") return void acquireDataset(action.dataset.addon_id);
    if (action.kind === "dataset-import") return void importDataset(action.dataset.addon_id);
    return void act(action.matcher.matcher_id, action.action);
  }, [acquireDataset, act, importDataset]);

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

  const trapConfirmFocus = (event: KeyboardEvent<HTMLDialogElement>) => {
    if (event.key !== "Tab") return;
    const focusable = Array.from(event.currentTarget.querySelectorAll<HTMLElement>("button:not([disabled])"));
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!first || !last) return;
    if (event.shiftKey && (document.activeElement === first || !event.currentTarget.contains(document.activeElement))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const filteredAssets = useMemo(() => {
    const query = matcherQuery.trim().toLocaleLowerCase();
    return query ? assets.filter((asset) => `${asset.name} ${asset.matcher_id} ${asset.driver_id}`.toLocaleLowerCase().includes(query)) : assets;
  }, [assets, matcherQuery]);
  const selectedAsset = assets.find((asset) => asset.matcher_id === selectedMatcherId) ?? filteredAssets[0] ?? null;
  const selectedEvents = selectedAsset ? events.filter((event) => event.subject_id === selectedAsset.matcher_id) : [];
  const lastFailure = selectedEvents.find((event) => event.event_type.endsWith("_FAILED"));
  const lastReceipt = selectedEvents.find((event) => eventDetail(event, "receipt_id") !== null);
  const compatibleProfiles = selectedAsset && catalog ? catalog.preprocessing.filter((profile) => profile.compatible_matcher_ids.includes(selectedAsset.matcher_id)) : [];
  const refreshReason = phase === "loading" ? "System state verification is in progress." : busy ? "An asset lifecycle action is in progress." : null;

  return (
    <section className="panel system" aria-labelledby="system-heading" data-testid="system-panel" aria-busy={phase === "loading"}>
      <div className="section-heading-row">
        <div><p className="eyebrow">Manage</p><h2 id="system-heading">System management</h2><p className="hint">Operate the beta's closed, registry-bound add-ons and inspect local runtime evidence without reading configuration JSON.</p></div>
        <button type="button" className="secondary" onClick={() => void refresh()} disabled={refreshReason !== null} aria-describedby={refreshReason ? "system-refresh-reason" : undefined}>Refresh</button>
      </div>
      {refreshReason && <p id="system-refresh-reason" className="hint">Refresh unavailable: {refreshReason}</p>}
      {phase === "loading" && <div className="system-loading" data-testid="system-loading" role="status"><span className="system-loading-line" /><span>Verifying registry, runtime, resource, audit, and project evidence…</span></div>}
      {phase === "error" && <div className="error system-fetch-error" data-testid="system-error-state" role="alert"><strong>Manage data is unavailable.</strong><span>The closed add-on catalog could not be read: {error}</span><button type="button" className="secondary" onClick={() => void refresh()}>Retry</button></div>}
      {Object.keys(feedErrors).length > 0 && phase === "ready" && <div className="error system-partial-error" role="alert" data-testid="system-partial-error"><strong>Some live system evidence is unavailable.</strong><ul>{Object.entries(feedErrors).map(([feed, detail]) => <li key={feed}>{feed}: {detail}</li>)}</ul><button type="button" className="secondary" onClick={() => void refresh()}>Retry all feeds</button></div>}
      {message && <p className={message.kind === "error" ? "error" : "status"} role={message.kind === "error" ? "alert" : "status"} data-testid="system-action-message">{message.text}</p>}
      {busy && <p className="status system-progress" role="status" data-testid="system-lifecycle-progress">Lifecycle action in progress for {busy.split(":")[0]}…</p>}

      <div className="system-area-tabs" role="tablist" aria-label="System management areas">
        {SYSTEM_AREAS.map((area, index) => <button key={area.id} id={`system-tab-${area.id}`} type="button" role="tab" aria-selected={activeArea === area.id} aria-controls={`system-panel-${area.id}`} tabIndex={activeArea === area.id ? 0 : -1} onClick={() => setActiveArea(area.id)} onKeyDown={(event) => moveAreaFocus(event, index)}>{area.label}</button>)}
      </div>

      <section id="system-panel-addons" role="tabpanel" aria-labelledby="system-tab-addons" tabIndex={0} hidden={activeArea !== "addons"}>
        <div className="system-section-intro"><div><h3>Matcher add-ons</h3><p>Search the admitted matcher identities, then inspect or operate one exact target.</p></div><span className="system-count" data-testid="matcher-count">{assets.length} registered</span></div>
        <div className="system-matcher-workspace">
          <div className="system-matcher-index">
            <label htmlFor="system-matcher-search">Search matchers</label>
            <input id="system-matcher-search" type="search" value={matcherQuery} onChange={(event) => setMatcherQuery(event.target.value)} placeholder="Name, identifier, or driver" />
            {phase === "ready" && assets.length === 0 && <div className="empty"><strong>No matcher add-ons are registered.</strong><span>The closed catalog returned no matcher drivers.</span></div>}
            {phase === "ready" && assets.length > 0 && filteredAssets.length === 0 && <div className="empty"><strong>No matcher matches this search.</strong><span>Clear the search to return to all registered matchers.</span></div>}
            <div className="system-table-wrap"><table className="system-matcher-table" data-testid="matcher-assets"><caption className="sr-only">Registered matcher add-ons and lifecycle state</caption><thead><tr><th scope="col">Matcher</th><th scope="col">Install</th><th scope="col">Runtime</th></tr></thead><tbody>
              {filteredAssets.map((asset) => <tr key={asset.matcher_id} data-matcher-id={asset.matcher_id} data-selected={selectedAsset?.matcher_id === asset.matcher_id}><th scope="row"><button type="button" className="system-matcher-select" aria-pressed={selectedAsset?.matcher_id === asset.matcher_id} onClick={() => setSelectedMatcherId(asset.matcher_id)}><strong>{asset.name}</strong><span className="mono">{asset.matcher_id}</span></button></th><td>{asset.installation_state}</td><td><span className={`state-pill ${asset.runtime.state === "LOADED" ? "ok" : "neutral"}`}>{asset.runtime.state}</span></td></tr>)}
            </tbody></table></div>
          </div>

          {selectedAsset && (() => {
            const sourcesReady = selectedAsset.architecture_sources.every((source) => source.state === "VERIFIED");
            const checkpointReady = selectedAsset.checkpoint.state === "VERIFIED";
            const managedReady = checkpointReady && selectedAsset.checkpoint.location_state === "MANAGED";
            const legacyExternalReady = checkpointReady && selectedAsset.checkpoint.location_state === "LEGACY_EXTERNAL";
            const canAcquireManagedCheckpoint = selectedAsset.checkpoint_acquisition.available && (!checkpointReady || legacyExternalReady);
            const operationReason = busy ? "Another lifecycle action is in progress." : null;
            const checkpointReason = operationReason ?? (managedReady ? "The managed checkpoint is already verified." : null) ?? (!canAcquireManagedCheckpoint ? selectedAsset.checkpoint_acquisition.reason : null);
            const loadReason = operationReason ?? (selectedAsset.runtime.state === "LOADED" ? "The matcher is already loaded." : null) ?? (!selectedAsset.ready_to_load ? selectedAsset.blocking_reasons.join(" · ") || "Required registered bytes are not verified." : null);
            const unloadReason = operationReason ?? (selectedAsset.runtime.state !== "LOADED" ? "The matcher is not loaded." : null) ?? (selectedAsset.runtime.active_leases > 0 ? `${selectedAsset.runtime.active_leases} active job lease(s) must finish before unloading.` : null);
            return <article className="system-matcher-detail" data-testid="matcher-detail" data-matcher-id={selectedAsset.matcher_id}>
              <header><div><p className="eyebrow">Selected matcher</p><h4>{selectedAsset.name}</h4><span className="mono">{selectedAsset.matcher_id}</span></div><span className={`state-pill ${selectedAsset.runtime.state === "LOADED" ? "ok" : "neutral"}`}>{selectedAsset.runtime.state}</span></header>
              <div className="system-detail-section"><h5>Checkpoint identity</h5><dl className="facts"><dt>Registered SHA-256</dt><dd className="mono" data-field="checkpoint-sha256">{selectedAsset.checkpoint.registered_sha256}</dd><dt>Registered size</dt><dd data-field="checkpoint-size">{formatBytes(selectedAsset.checkpoint.registered_size_bytes)}</dd><dt>Observed SHA-256</dt><dd className="mono">{selectedAsset.checkpoint.observed_sha256 ?? "Not observed"}</dd><dt>Verification</dt><dd>{selectedAsset.checkpoint.state}</dd><dt>Location</dt><dd>{selectedAsset.checkpoint.location_state === "MANAGED" ? "Managed by QualSight" : selectedAsset.checkpoint.location_state === "LEGACY_EXTERNAL" ? "Legacy external checkpoint" : "Registered external checkpoint"}</dd></dl></div>
              <div className="system-detail-section"><h5>Pinned architecture sources</h5><div className="system-source-list" data-testid="matcher-sources">{selectedAsset.architecture_sources.map((source) => <article key={source.key} data-source-key={source.key}><strong>{source.key}</strong><span>{source.repository} · commit <span className="mono">{source.commit}</span></span><span className="mono" data-field="source-sha256">SHA-256 {source.sha256}</span><span data-field="source-size">{formatBytes(source.size_bytes)} · {source.state}</span>{source.reason && <span className="asset-blocker">{source.reason}</span>}</article>)}</div></div>
              <div className="system-detail-section"><h5>Rights, acquisition, and runtime</h5><dl className="facts"><dt>Rights</dt><dd>{selectedAsset.third_party_rights_status} · {selectedAsset.research_use_status}</dd><dt>Redistribution</dt><dd>{selectedAsset.redistribution_status}</dd><dt>Acquisition</dt><dd>{selectedAsset.checkpoint_acquisition.available ? `Admitted from ${selectedAsset.checkpoint_acquisition.source_host}` : "Not admitted"} · {selectedAsset.checkpoint_acquisition.reason}</dd><dt>Driver</dt><dd className="mono">{selectedAsset.driver_id}</dd><dt>Runtime</dt><dd>{selectedAsset.runtime.state}{selectedAsset.runtime.device ? ` · ${selectedAsset.runtime.device} · ${selectedAsset.runtime.parameter_count?.toLocaleString()} parameters` : " · not resident"}</dd><dt>Active leases</dt><dd data-field="active-leases">{selectedAsset.runtime.active_leases}</dd><dt>Compatible profiles</dt><dd>{compatibleProfiles.length > 0 ? compatibleProfiles.map((profile) => profile.display_name).join(" · ") : "No compatible profile is published."}</dd><dt>Last receipt</dt><dd className="mono" data-field="last-receipt">{eventDetail(lastReceipt, "receipt_id") ?? "No acquisition receipt is recorded in the available audit feed."}</dd><dt>Last error</dt><dd data-field="last-error">{lastFailure ? `${lastFailure.event_type}: ${eventDetail(lastFailure, "reason") ?? "No reason was published."}` : feedErrors.events ? "Audit feed unavailable; last error cannot be determined." : "No lifecycle failure is recorded in the available audit feed."}</dd></dl></div>
              {selectedAsset.blocking_reasons.length > 0 && <p className="asset-blocker" data-testid="matcher-blockers">{selectedAsset.blocking_reasons.join(" · ")}</p>}
              <div className="asset-actions" data-testid="matcher-lifecycle-actions">{managedReady ? <span className="state-pill ok">Managed checkpoint verified</span> : canAcquireManagedCheckpoint ? <button type="button" className="secondary" disabled={checkpointReason !== null} title={checkpointReason ?? undefined} onClick={() => setPendingAction({ kind: "matcher", action: "checkpoint", matcher: selectedAsset })}>Download verified checkpoint</button> : checkpointReady ? <span className="state-pill ok">Registered external checkpoint verified</span> : <span className="state-pill bad" data-testid="checkpoint-refusal">Checkpoint download blocked</span>}{sourcesReady ? <span className="state-pill ok">Sources verified</span> : <button type="button" className="secondary" disabled={operationReason !== null} title={operationReason ?? undefined} onClick={() => setPendingAction({ kind: "matcher", action: "pull", matcher: selectedAsset })}>Pull pinned sources</button>}<button type="button" className="primary" disabled={loadReason !== null} title={loadReason ?? undefined} onClick={() => setPendingAction({ kind: "matcher", action: "load", matcher: selectedAsset })}>Load matcher</button><button type="button" className="secondary" disabled={unloadReason !== null} title={unloadReason ?? undefined} onClick={() => setPendingAction({ kind: "matcher", action: "unload", matcher: selectedAsset })}>Unload matcher</button></div>
              <ul className="system-unavailable-actions" aria-label={`Unavailable lifecycle actions for ${selectedAsset.name}`}>{checkpointReason && !managedReady && <li>Checkpoint acquisition unavailable: {checkpointReason}</li>}{loadReason && <li>Load unavailable: {loadReason}</li>}{unloadReason && <li>Unload unavailable: {unloadReason}</li>}</ul>
            </article>;
          })()}
        </div>

        <div className="system-section-intro system-dataset-heading"><div><h3>Dataset add-ons</h3><p>Registered image collections and author-published pair protocols remain scientifically distinct.</p></div><span className="system-count" data-testid="dataset-count">{catalog?.datasets.length ?? 0} registered</span></div>
        {catalog && catalog.datasets.length === 0 && <div className="empty"><strong>No dataset add-ons are registered.</strong><span>The catalog returned no dataset drivers.</span></div>}
        <div className="system-dataset-list" data-testid="dataset-addons">{catalog?.datasets.map((dataset) => {
          const readyToImport = dataset.runtime_state === "READY_TO_IMPORT_AND_EXECUTE" || dataset.runtime_state === "READY_TO_IMPORT";
          const receipt = events.find((event) => event.subject_id === dataset.addon_id && eventDetail(event, "receipt_id") !== null);
          const downloadReason = busy ? "Another asset lifecycle action is in progress." : !dataset.acquisition.available ? dataset.acquisition.reason ?? "Dataset acquisition is not admitted." : readyToImport ? "The managed dataset is already downloaded and verified." : null;
          const importReason = !readyToImport ? "Download and verify the managed dataset before importing it." : !projectId ? "Select a writable project before importing this dataset and protocol." : busy ? "Another asset lifecycle action is in progress." : null;
          const sourceIdentity = [dataset.acquisition.source_kind, dataset.acquisition.source_uri]
            .filter((value): value is string => typeof value === "string" && value.length > 0)
            .join(" · ") || "No registered publisher source is available.";
          const archiveIdentity = dataset.archive ? `${dataset.archive.sha256} · ${formatBytes(dataset.archive.size_bytes)}` : dataset.acquisition.archive_sha256 ? `${dataset.acquisition.archive_sha256} · ${dataset.acquisition.archive_size_bytes === undefined ? "size unavailable" : formatBytes(dataset.acquisition.archive_size_bytes)}` : "No archive identity is registered.";
          return <article className="system-dataset-row" key={dataset.addon_id} data-addon-id={dataset.addon_id}><header><div><strong>{dataset.display_name}</strong><span className="mono">{dataset.addon_id}</span></div><span className={`state-pill ${readyToImport ? "ok" : dataset.runtime_state === "BLOCKED" || dataset.runtime_state === "DRIFTED" ? "bad" : "neutral"}`}>{dataset.runtime_state}</span></header><dl className="facts"><dt>Registered source</dt><dd>{sourceIdentity}</dd><dt>Archive SHA-256 / size</dt><dd className="mono" data-field="dataset-archive">{archiveIdentity}</dd><dt>Receipt</dt><dd className="mono">{eventDetail(receipt, "receipt_id") ?? "No acquisition receipt is recorded in the available audit feed."}</dd><dt>Representation</dt><dd>{dataset.subtype === "PAIR_VERIFICATION_DATASET" ? "Author-published verification pairs with prealigned images" : "Identity-labelled image collection"}</dd><dt>Scientific limit</dt><dd>{dataset.identity_count === null ? "Pair-only protocol: no governed identity labels; identity-dependent workflows are unavailable." : `${dataset.identity_count.toLocaleString()} registered identities; use remains limited to approved local scopes.`}</dd><dt>Population</dt><dd>{dataset.sample_count.toLocaleString()} samples{dataset.pair_count === undefined ? "" : ` · ${dataset.pair_count.toLocaleString()} pairs (${dataset.mated_pair_count?.toLocaleString() ?? 0} mated / ${dataset.nonmated_pair_count?.toLocaleString() ?? 0} nonmated)`}</dd><dt>Rights</dt><dd>{dataset.rights.third_party} · {dataset.rights.redistribution}</dd><dt>Driver</dt><dd className="mono">{dataset.driver_id}</dd></dl>{dataset.acquisition.rights_notice && <p className="hint">{dataset.acquisition.rights_notice}</p>}{!dataset.acquisition.available && <p className="asset-blocker"><strong>{dataset.acquisition.refusal_code}</strong> · {dataset.acquisition.reason}</p>}{dataset.blocking_reasons.length > 0 && dataset.acquisition.available && <p className="asset-blocker">{dataset.blocking_reasons.join(" · ")}</p>}<div className="asset-actions">{dataset.acquisition.available && !readyToImport && <button type="button" className="primary" disabled={downloadReason !== null} title={downloadReason ?? undefined} onClick={() => setPendingAction({ kind: "dataset-acquire", dataset })}>Download and verify dataset</button>}{dataset.acquisition.available && readyToImport && <button type="button" className="primary" disabled={importReason !== null} title={importReason ?? undefined} onClick={() => setPendingAction({ kind: "dataset-import", dataset })}>Import into selected project</button>}{!dataset.acquisition.available && <a className="button secondary" href="#/data">Open dataset imports</a>}</div>{(downloadReason || importReason) && <p className="hint">Action unavailable: {downloadReason ?? importReason}</p>}</article>;
        })}</div>
      </section>

      <section id="system-panel-processing" role="tabpanel" aria-labelledby="system-tab-processing" tabIndex={0} hidden={activeArea !== "processing"}>
        <div className="system-section-intro"><div><h3>Matcher preprocessing</h3><p>Each profile is an independently swappable component selected per immutable Run, never a global setting.</p></div><span className="system-count">{catalog?.preprocessing.length ?? 0} profiles</span></div>
        <div className="system-profile-list" data-testid="preprocessing-addons">{catalog?.preprocessing.map((profile) => <article key={profile.addon_id} data-addon-id={profile.addon_id}><header><div><strong>{profile.display_name}</strong><span className="mono">{profile.addon_id}</span></div><span className={`state-pill ${profile.runtime_state === "READY" ? "ok" : "bad"}`}>{profile.runtime_state}</span></header><dl className="facts"><dt>Swap scope</dt><dd>{profile.selection_scope.replace("_", " ")}</dd><dt>Input</dt><dd>{representationLabel(profile.input_representation)}</dd><dt>Output contract</dt><dd>{profile.definition.alignment} · {profile.definition.transport}</dd><dt>Detector</dt><dd className="mono">{profile.detector_resource_id ?? "Not used for publisher-prealigned inputs"}</dd><dt>Compatibility</dt><dd>{profile.compatible_matcher_ids.length} registered matchers</dd><dt>Driver</dt><dd className="mono">{profile.driver_id}</dd></dl>{profile.blocking_reasons.length > 0 && <p className="asset-blocker">{profile.blocking_reasons.join(" · ")}</p>}<a className="button secondary" href="#/matchers">Select for a comparison Run</a></article>)}</div>
        <div className="system-section-intro system-transform-heading"><div><h3>Controlled transformations</h3><p>The seven real-input degradations are separate from matcher preprocessing and selected per derived set.</p></div><span className="system-count" data-testid="transform-count">{catalog?.transforms.length ?? 0} operators</span></div>
        <div className="system-transform-table-wrap"><table className="system-transform-table" data-testid="transform-addons"><thead><tr><th scope="col">Operator</th><th scope="col">Parameter</th><th scope="col">Backend contract</th><th scope="col">Scope</th></tr></thead><tbody>{catalog?.transforms.map((transform) => <tr key={transform.addon_id} data-addon-id={transform.addon_id}><th scope="row"><strong>{transform.display_name}</strong><span className="mono">{transform.driver_id}</span></th><td className="mono">{transform.parameter}</td><td><strong>{parameterRange(transform.parameter_contract)}</strong><span>{transform.parameter_contract.guidance}</span></td><td>{transform.selection_scope.replaceAll("_", " ")}</td></tr>)}</tbody></table></div>
      </section>

      <section id="system-panel-connections" role="tabpanel" aria-labelledby="system-tab-connections" tabIndex={0} hidden={activeArea !== "connections"}>
        <div className="system-section-intro"><div><h3>Connections</h3><p>External biometric API access stays closed until a provider-specific authority contract exists.</p></div><span className="system-count" data-testid="connection-count">{catalog?.connections.length ?? 0} providers</span></div>
        {catalog && catalog.connections.length === 0 && <div className="empty system-connection-policy" data-testid="connection-addons" data-provider-count="0"><strong>No API connections are admitted in this beta.</strong><span>{catalog.connection_policy.reason}</span><div><h4>Backend-published admission requirements</h4><ul data-testid="connection-requirements">{catalog.connection_policy.admission_requires.map((requirement) => <li key={requirement}>{requirement}</li>)}</ul></div><div><h4>Additional contract gaps</h4><ul><li>Bounded request and response schema is not registered.</li><li>Provider retention and deletion terms are not registered.</li></ul></div></div>}
      </section>

      <section id="system-panel-runtime" role="tabpanel" aria-labelledby="system-tab-runtime" tabIndex={0} hidden={activeArea !== "runtime"}>
        <div className="system-section-intro"><div><h3>Runtime identity</h3><p>Live process, database, storage, work-capacity, and recovery evidence.</p></div><span className={`state-pill ${ready?.status === "ready" ? "ok" : "bad"}`}>{ready?.status ?? "UNAVAILABLE"}</span></div>
        {health && <dl className="facts system-runtime-facts" data-testid="runtime-health"><dt>Application</dt><dd className="mono">QualSight {health.application_version}</dd><dt>Engine</dt><dd>{health.engine.name} · v{health.engine.version} · {health.engine.state}</dd><dt>Registry</dt><dd className="mono">{health.registry_id} · {health.registry_sha256}</dd><dt>Database</dt><dd>Local project evidence store</dd><dt>Actor</dt><dd className="mono">{health.actor_id}</dd><dt>Network guard</dt><dd>{health.network_guard.installed ? "Installed" : "Not installed for this process"} · {health.network_guard.refused_attempts.length} refused · {health.network_guard.authorized_setup_connections} authorized setup connection(s)</dd></dl>}
        {!health && <div className="error"><strong>Runtime health unavailable.</strong><span>{feedErrors.health ?? "The health feed did not return evidence."}</span><button type="button" className="secondary" onClick={() => void refresh()}>Retry</button></div>}
        {ready && <div className="system-runtime-grid" data-testid="runtime-readiness"><article><strong>Storage</strong><span className={`state-pill ${ready.checks.storage.ok ? "ok" : "bad"}`}>{ready.checks.storage.ok ? "READY" : "NOT READY"}</span>{ready.checks.storage.ok ? <dl className="facts"><dt>Location</dt><dd>Private managed storage</dd><dt>Mode</dt><dd className="mono">{ready.checks.storage.mode}</dd><dt>Available</dt><dd>{formatBytes(ready.checks.storage.available_bytes)}</dd></dl> : <p>Managed storage is unavailable · <span className="mono">{ready.checks.storage.error_code}</span></p>}</article><article><strong>Database</strong><span className={`state-pill ${ready.checks.database.ok ? "ok" : "bad"}`}>{ready.checks.database.ok ? "OPERATIONAL" : "NOT READY"}</span>{ready.checks.database.ok ? <dl className="facts"><dt>Query</dt><dd>{ready.checks.database.query}</dd><dt>Schema</dt><dd>{ready.checks.database.schema_version}</dd><dt>Unresolved purges</dt><dd>{ready.checks.database.unresolved_purges}</dd></dl> : <p>Evidence database is unavailable · <span className="mono">{ready.checks.database.error_code}</span></p>}</article><article><strong>Scientific work</strong><dl className="facts"><dt>Capacity</dt><dd>{ready.checks.scientific_capacity.capacity}</dd><dt>In flight</dt><dd>{ready.checks.scientific_capacity.in_flight}</dd><dt>Available</dt><dd>{ready.checks.scientific_capacity.available}</dd></dl></article><article><strong>Core runtime</strong><span className={`state-pill ${ready.checks.core_runtime.ok ? "ok" : "bad"}`}>{ready.checks.core_runtime.ok ? "VERIFIED" : "NOT READY"}</span><ul>{ready.checks.core_runtime.artifacts.map((artifact) => <li key={artifact.role}><strong>{artifact.role}</strong><span className="mono">{artifact.resource_id} · {artifact.sha256.slice(0, 12)} · {artifact.ok ? "verified" : "unavailable"}</span></li>)}</ul></article></div>}
        {!ready && <div className="error"><strong>Readiness evidence unavailable.</strong><span>{feedErrors.readiness ?? "The readiness feed did not return evidence."}</span><button type="button" className="secondary" onClick={() => void refresh()}>Retry</button></div>}
        {metrics && <div className="system-metrics" data-testid="runtime-metrics"><h4>Operations and recovery</h4><dl className="facts"><dt>Uptime</dt><dd>{metrics.uptime_seconds.toLocaleString()} seconds</dd><dt>HTTP requests</dt><dd>{metrics.http.requests.toLocaleString()} · mean {metrics.http.mean_duration_ms ?? "not yet measured"} ms</dd><dt>Operation states</dt><dd>{Object.keys(metrics.database.operation_states).length ? Object.entries(metrics.database.operation_states).map(([state, count]) => `${state}: ${count}`).join(" · ") : "No recorded operations"}</dd><dt>Startup reconciliation</dt><dd>{metrics.reconciled_at_startup.operations} operations · {metrics.reconciled_at_startup.purges} purges</dd><dt>Shutdown</dt><dd>{metrics.shutting_down ? "In progress" : "Not requested"}</dd></dl></div>}
        {!metrics && <div className="error"><strong>Operational metrics unavailable.</strong><span>{feedErrors.metrics ?? "The metrics feed did not return evidence."}</span><button type="button" className="secondary" onClick={() => void refresh()}>Retry</button></div>}
        {ready && <article className="system-lease-gap" data-testid="runtime-data-directory-lease" data-field="data-directory-lease" data-state={ready.checks.data_directory_lease.state}><strong>Data-directory lease</strong><span className={`state-pill ${ready.checks.data_directory_lease.state === "HELD" ? "ok" : "bad"}`}>{ready.checks.data_directory_lease.state}</span><dl className="facts"><dt>Owner</dt><dd className="mono">{ready.checks.data_directory_lease.owner ?? "Not held"}</dd><dt>Process</dt><dd className="mono">{ready.checks.data_directory_lease.pid ?? "Not recorded"}</dd><dt>Acquired</dt><dd className="mono">{ready.checks.data_directory_lease.acquired_at ?? "Not recorded"}</dd><dt>Expires</dt><dd>{ready.checks.data_directory_lease.expires_at ?? "No time-based expiry"}</dd><dt>Released</dt><dd className="mono">{ready.checks.data_directory_lease.release_condition}</dd></dl></article>}

        <div className="system-section-intro system-embedding-heading"><div><h3>Project embeddings</h3><p>Derived biometric evidence stays nested under runtime and scoped to the selected project.</p></div><span className="system-count">{projectId ? `${embeddings.length} recorded` : "No project selected"}</span></div>
        {!projectId && <div className="empty"><strong>Select a project to inventory embeddings.</strong><span>Embedding artifacts remain scoped to the project that produced them.</span><a className="button secondary" href="#/projects">Open projects</a></div>}
        {projectId && feedErrors.embeddings && <div className="error"><strong>Project embeddings unavailable.</strong><span>{feedErrors.embeddings}</span><button type="button" className="secondary" onClick={() => void refresh()}>Retry</button></div>}
        {projectId && !feedErrors.embeddings && embeddings.length === 0 && <div className="empty"><strong>No project embeddings are recorded.</strong><span>Complete a matcher Run to produce an immutable embedding artifact.</span><a className="button secondary" href="#/matchers">Open Compare</a></div>}
        <div className="embedding-list" data-testid="embedding-assets">{embeddings.map((item) => <article className="embedding-row" key={item.run_id}><div><strong>{item.leaf_id}</strong> · {assets.find((asset) => asset.matcher_id === item.matcher_id)?.name ?? item.matcher_id}<span className="mono">{item.condition_id}</span></div><div><span className={`state-pill ${item.integrity === "VERIFIED" ? "ok" : "bad"}`}>{item.integrity}</span> · {item.rows?.toLocaleString() ?? "—"} × {item.dimension ?? "—"} · {item.size_bytes?.toLocaleString() ?? "—"} bytes</div><div className="links">{projectId && <EvidenceOpenButton testId="system-embedding-run-evidence" request={{ kind: "run", href: item.links.run, evidenceId: item.run_id, projectId, label: `Embedding Run ${shortHash(item.run_id, 14)}` }} onOpen={onOpenEvidence}>run</EvidenceOpenButton>}<a href="#/reports">Open governed exports</a></div><p className="hint">{item.integrity_reason ? `${item.integrity_reason} · ` : ""}{item.classification} · sha256 {shortHash(item.registered_sha256, 16)}</p></article>)}</div>
        <details className="system-json-evidence" data-testid="runtime-json-evidence"><summary>Raw runtime records (JSON)</summary><pre>{JSON.stringify({ health, ready, metrics, resources }, null, 2)}</pre></details>
      </section>

      <section id="system-panel-audit" role="tabpanel" aria-labelledby="system-tab-audit" tabIndex={0} hidden={activeArea !== "audit"}>
        <div className="system-section-intro"><div><h3>Lifecycle audit</h3><p>Durable operator actions and evidence identities from the local event store.</p></div><span className="system-count" data-testid="system-event-count">{events.length} events</span></div>
        {feedErrors.events && <div className="error"><strong>System event feed unavailable.</strong><span>{feedErrors.events}</span><button type="button" className="secondary" onClick={() => void refresh()}>Retry</button></div>}
        {!feedErrors.events && events.length === 0 && <div className="empty"><strong>No lifecycle action is recorded.</strong><span>The first confirmed acquisition, load, or unload action will appear here.</span></div>}
        <ul className="system-events" data-testid="system-events">{events.slice(0, 20).map((event) => <li key={event.system_event_id}><strong>{event.event_type.replaceAll("_", " ")}</strong><span className="mono">{event.subject_id}</span><span>{event.actor_id} · {event.occurred_at.replace("T", " ").slice(0, 19)}</span><span className="mono">evidence {event.evidence_sha256}</span></li>)}</ul>
        <details className="system-resource-evidence"><summary>All registered resources ({resources.length})</summary><div className="resource-grid" data-testid="resource-table">{resources.map((item) => <article key={item.resource_id} className="resource-card"><strong>{item.name}</strong><span className="mono">{item.resource_id}</span><span>{item.kind} · {item.state}</span><span>{item.third_party_rights_status} · {item.data_classification}</span></article>)}</div></details>
        <details className="system-json-evidence" data-testid="audit-json-evidence"><summary>Raw audit records (JSON)</summary><pre>{JSON.stringify(events, null, 2)}</pre></details>
      </section>

      <dialog ref={confirmDialog} className="system-confirm-dialog" aria-labelledby="system-confirm-title" aria-describedby="system-confirm-description" onKeyDown={trapConfirmFocus} onCancel={(event) => { event.preventDefault(); setPendingAction(null); }}>
        {pendingAction && <form method="dialog" onSubmit={(event) => event.preventDefault()}><p className="eyebrow">Confirm lifecycle action</p><h3 id="system-confirm-title">{pendingLabel(pendingAction)}</h3><p id="system-confirm-description">This mutation will target exactly:</p><p className="system-confirm-target" data-testid="system-confirm-target">{pendingTarget(pendingAction)}</p><p className="hint">QualSight records the request and terminal event. A failed completion is refused or rolled back by the control plane.</p><div className="system-confirm-actions"><button type="button" className="secondary" onClick={() => setPendingAction(null)}>Cancel</button><button type="button" className="primary" onClick={() => executePendingAction(pendingAction)}>Confirm exact target</button></div></form>}
      </dialog>
    </section>
  );
}

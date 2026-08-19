import { useEffect, useState } from "react";
import { api, type HealthOut, type ResourceOut } from "./api";

function shortHash(value: string, keep = 12): string {
  const marker = value.indexOf("sha256:");
  const digest = marker >= 0 ? value.slice(marker + 7) : value;
  return `${digest.slice(0, keep)}…`;
}

/** SYSTEM · Engines and Resources (PRD §44): the registered engine, the resource registry with states/scopes, actor, network guard. */
export function SystemPanel() {
  const [health, setHealth] = useState<HealthOut | null>(null);
  const [resources, setResources] = useState<ResourceOut[]>([]);
  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
    api.resources().then(setResources).catch(() => setResources([]));
  }, []);
  return (
    <section className="panel system" aria-labelledby="system-heading" data-testid="system-panel">
      <h2 id="system-heading">System · engines · resources</h2>
      {health && (
        <dl className="facts">
          <dt>Application</dt><dd className="mono">QualSight {health.application_version}</dd>
          <dt>Engine</dt><dd>{health.engine.name} · v{health.engine.version} · {health.engine.state}</dd>
          <dt>Registry</dt><dd className="mono">{health.registry_id} · {shortHash(health.registry_sha256, 16)}</dd>
          <dt>Actor</dt><dd className="mono">{(health as any).actor_id ?? "—"}</dd>
          <dt>Network guard</dt><dd className="mono">{(health as any).network_guard ? ((health as any).network_guard.installed ? `installed · ${(health as any).network_guard.refused_attempts.length} refused` : "not installed") : "—"}</dd>
          <dt>Store</dt><dd className="mono">{health.database_path}</dd>
        </dl>
      )}
      <table className="compact" data-testid="resource-table">
        <thead><tr><th>Resource</th><th>Kind</th><th>State</th><th>Approved scopes</th><th>Third-party rights</th><th>Classification</th></tr></thead>
        <tbody>
          {resources.map((item) => (
            <tr key={item.resource_id}><td className="mono">{item.resource_id}</td><td>{item.kind}</td><td>{item.state}</td><td className="mono">{item.approved_scopes.join(", ") || "—"}</td><td>{item.third_party_rights_status}</td><td>{item.data_classification}</td></tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/** PRD §44 primary navigation, mapped onto the console's panels (anchors) — one local page, grouped the way §44 groups the product. */

const GROUPS: Array<{ group: string; items: Array<{ label: string; target: string }> }> = [
  { group: "QualSight", items: [{ label: "Projects", target: "projects-heading" }] },
  { group: "Data", items: [{ label: "Datasets · Imports", target: "workbench-heading" }, { label: "Samples", target: "samples-heading" }] },
  { group: "Quality", items: [{ label: "Sample analysis", target: "inspector-heading" }, { label: "Quality explorer", target: "workbench-heading" }] },
  { group: "Biometrics", items: [{ label: "Matchers · Verification", target: "matchers-heading" }, { label: "Identification", target: "advanced-heading" }] },
  { group: "Experiments", items: [{ label: "Runs", target: "runs-heading" }, { label: "Protocols", target: "matchers-heading" }] },
  { group: "Analysis", items: [{ label: "Quality utility", target: "utility-heading" }, { label: "Cross-matcher · Failure analysis", target: "crossmatcher-heading" }, { label: "Robustness", target: "advanced-heading" }] },
  { group: "Explore · Output", items: [{ label: "Figures · Dashboards", target: "reports-heading" }, { label: "Reports · Exports · Reproduction", target: "reports-heading" }] },
  { group: "System", items: [{ label: "Engines · Resources", target: "system-heading" }] },
];

export function Nav() {
  return (
    <nav className="primary-nav" aria-label="Primary" data-testid="primary-nav">
      {GROUPS.map((group) => (
        <div key={group.group} className="nav-group">
          <span className="nav-group-label">{group.group}</span>
          {group.items.map((item) => (
            <a key={item.label} href={`#${item.target}`} className="nav-link" data-testid="nav-link">{item.label}</a>
          ))}
        </div>
      ))}
    </nav>
  );
}

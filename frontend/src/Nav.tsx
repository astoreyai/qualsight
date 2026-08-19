/** PRD §44 primary navigation, mapped onto the console's routed views (QS-Y89) — one panel visible at a time. */

const GROUPS: Array<{ group: string; items: Array<{ label: string; view: string }> }> = [
  { group: "QualSight", items: [{ label: "Projects", view: "projects" }] },
  { group: "Data", items: [{ label: "Datasets · Imports", view: "data" }, { label: "Samples", view: "assess" }] },
  { group: "Quality", items: [{ label: "Sample analysis", view: "assess" }, { label: "Quality explorer", view: "data" }] },
  { group: "Biometrics", items: [{ label: "Matchers · Verification", view: "matchers" }, { label: "Identification", view: "advanced" }] },
  { group: "Experiments", items: [{ label: "Runs", view: "projects" }, { label: "Protocols", view: "matchers" }] },
  { group: "Analysis", items: [{ label: "Quality utility", view: "utility" }, { label: "Cross-matcher · Failure analysis", view: "cross" }, { label: "Robustness", view: "advanced" }] },
  { group: "Explore · Output", items: [{ label: "Figures · Dashboards", view: "reports" }, { label: "Reports · Exports · Reproduction", view: "reports" }] },
  { group: "System", items: [{ label: "Engines · Resources", view: "system" }] },
];

export function Nav() {
  const current = window.location.hash.replace(/^#\//, "") || "projects";
  return (
    <nav className="primary-nav" aria-label="Primary" data-testid="primary-nav">
      {GROUPS.map((group) => (
        <div key={group.group} className="nav-group">
          <span className="nav-group-label">{group.group}</span>
          {group.items.map((item) => (
            <a
              key={item.label}
              href={`#/${item.view}`}
              className={current === item.view ? "nav-link active" : "nav-link"}
              data-testid="nav-link"
              data-view={item.view}
            >
              {item.label}
            </a>
          ))}
        </div>
      ))}
    </nav>
  );
}

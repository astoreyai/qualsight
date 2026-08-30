/** PRD §44 primary navigation, mapped onto the console's routed views (QS-Y89) — one panel visible at a time. */

const GROUPS: Array<{ group: string; items: Array<{ label: string; view: string }> }> = [
  { group: "Workspace", items: [{ label: "Projects", view: "projects" }] },
  { group: "Prepare", items: [{ label: "Datasets & batches", view: "data" }, { label: "Sample quality", view: "assess" }] },
  { group: "Evaluate", items: [{ label: "Verification", view: "matchers" }, { label: "Identification & robustness", view: "advanced" }] },
  { group: "Analyze", items: [{ label: "Quality utility", view: "utility" }, { label: "Cross-matcher", view: "cross" }] },
  { group: "Publish", items: [{ label: "Reports & reproduction", view: "reports" }] },
  { group: "Manage", items: [{ label: "Add-ons & system", view: "system" }] },
];

interface NavProps {
  current: string;
}

export function Nav({ current }: NavProps) {
  return (
    <nav className="primary-nav" aria-label="Primary" data-testid="primary-nav">
      {GROUPS.map((group) => (
        <div key={group.group} className="nav-group">
          <span className="nav-group-label">{group.group}</span>
          {group.items.map((item) => (
            <a
              key={item.label}
              href={`#/${item.view}`}
              className={item.view === current ? "nav-link active" : "nav-link"}
              aria-current={item.view === current ? "page" : undefined}
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

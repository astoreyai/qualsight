import { useEffect, useRef, useState, type KeyboardEvent } from "react";

/** PRD §44 primary navigation, mapped onto the console's routed views (QS-Y89) — one panel visible at a time. */

const GROUPS: Array<{ group: string; items: Array<{ label: string; view: string }> }> = [
  { group: "Workspace", items: [{ label: "Home", view: "home" }, { label: "Projects", view: "projects" }] },
  {
    group: "Run work",
    items: [
      { label: "Inputs", view: "data" },
      { label: "Inspect quality", view: "assess" },
      { label: "Compare", view: "matchers" },
      { label: "Stress test", view: "advanced" },
      { label: "Analyze utility", view: "utility" },
    ],
  },
  { group: "Review", items: [{ label: "Results", view: "cross" }, { label: "Evidence & reports", view: "reports" }] },
  { group: "Administration", items: [{ label: "Manage system", view: "system" }] },
];

interface NavProps {
  current: string;
}

export function Nav({ current }: NavProps) {
  const [mobile, setMobile] = useState(() => window.matchMedia("(max-width: 720px)").matches);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const drawerToggleRef = useRef<HTMLButtonElement>(null);
  const drawerCloseRef = useRef<HTMLButtonElement>(null);
  const drawerContentRef = useRef<HTMLDivElement>(null);
  const currentLabel = GROUPS.flatMap((group) => group.items).find((item) => item.view === current)?.label ?? "Navigation";

  useEffect(() => {
    const query = window.matchMedia("(max-width: 720px)");
    const update = (event: MediaQueryListEvent) => {
      setMobile(event.matches);
      if (!event.matches) setDrawerOpen(false);
    };
    setMobile(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    setDrawerOpen(false);
  }, [current]);

  useEffect(() => {
    if (!mobile || !drawerOpen) return;
    const priorOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.requestAnimationFrame(() => drawerCloseRef.current?.focus());
    return () => {
      document.body.style.overflow = priorOverflow;
    };
  }, [drawerOpen, mobile]);

  const closeDrawer = (returnFocus: boolean) => {
    setDrawerOpen(false);
    if (returnFocus) window.requestAnimationFrame(() => drawerToggleRef.current?.focus());
  };

  const handleDrawerKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (!mobile || !drawerOpen) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeDrawer(true);
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(
      drawerContentRef.current?.querySelectorAll<HTMLElement>("button:not([disabled]), a[href]") ?? [],
    ).filter((element) => !element.hidden && getComputedStyle(element).display !== "none");
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!first || !last) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <nav
      className={drawerOpen ? "primary-nav drawer-open" : "primary-nav"}
      aria-label="Primary"
      data-testid="primary-nav"
      onKeyDown={handleDrawerKeyDown}
    >
      <button
        type="button"
        className="nav-drawer-toggle"
        data-testid="nav-drawer-toggle"
        aria-expanded={drawerOpen}
        aria-controls="primary-nav-drawer"
        ref={drawerToggleRef}
        onClick={() => setDrawerOpen((open) => !open)}
      >
        <span>Menu</span>
        <strong>{currentLabel}</strong>
        <span aria-hidden="true">{drawerOpen ? "×" : "☰"}</span>
      </button>
      {mobile && drawerOpen && (
        <button
          type="button"
          className="nav-drawer-backdrop"
          aria-label="Close navigation drawer"
          tabIndex={-1}
          onClick={() => closeDrawer(true)}
        />
      )}
      <div
        id="primary-nav-drawer"
        className="nav-drawer-content"
        data-testid="nav-drawer-content"
        hidden={mobile && !drawerOpen}
        role={mobile ? "dialog" : undefined}
        aria-modal={mobile && drawerOpen ? "true" : undefined}
        aria-label={mobile ? "Primary navigation" : undefined}
        ref={drawerContentRef}
      >
        <div className="nav-drawer-header">
          <a className="nav-brand" href="#/home" aria-label="QualSight home">
            <span className="nav-brand-title">QualSight</span>
            <span className="nav-brand-scope">Local research beta</span>
          </a>
          <button
            type="button"
            className="nav-drawer-close"
            data-testid="nav-drawer-close"
            aria-label="Close navigation drawer"
            ref={drawerCloseRef}
            onClick={() => closeDrawer(true)}
          >
            <span aria-hidden="true">×</span>
          </button>
        </div>
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
                onClick={() => {
                  if (!mobile) return;
                  setDrawerOpen(false);
                  window.requestAnimationFrame(() => document.getElementById("main-content")?.focus());
                }}
              >
                {item.label}
              </a>
            ))}
          </div>
        ))}
      </div>
    </nav>
  );
}

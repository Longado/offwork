import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowClockwise,
  Check,
  CheckCircle,
  Clipboard,
  Cube,
  Eye,
  FileText,
  Flask,
  FolderOpen,
  Play,
  Question,
  ShieldCheck,
  ShieldWarning,
  ShoppingCart,
  User,
  X,
} from "@phosphor-icons/react";

const TASKS = [
  {
    id: "01",
    title: "Refactor tax calculation",
    captured: "2026-08-28",
    status: "Fresh",
    tone: "fresh",
    icon: Cube,
  },
  {
    id: "02",
    title: "Ship checkout retry fix",
    captured: "2026-08-28",
    status: "Changed",
    tone: "changed",
    icon: ShoppingCart,
  },
  {
    id: "03",
    title: "Upgrade payment SDK",
    captured: "2026-08-27",
    status: "Integrity failed",
    tone: "failed",
    icon: ShieldWarning,
  },
];

const DECISIONS = ["continue", "verify", "stop"];

function StatusSeal({ icon: Icon, label, value, tone }) {
  return (
    <div className={`status-seal status-seal--${tone}`}>
      <Icon size={34} weight="duotone" aria-hidden="true" />
      <span>
        <small>{label}</small>
        <strong>{value}</strong>
      </span>
    </div>
  );
}

function RailTask({ task, selected, onSelect }) {
  const Icon = task.icon;
  return (
    <button
      className={`rail-task rail-task--${task.tone} ${selected ? "is-selected" : ""}`}
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
    >
      <span className="rail-task__number">{task.id}</span>
      <span className="rail-task__icon"><Icon size={28} weight="duotone" /></span>
      <span className="rail-task__copy">
        <strong>{task.title}</strong>
        <small>Captured {task.captured}</small>
        <em>{task.status}</em>
      </span>
    </button>
  );
}

function EvidenceRow({ index, icon: Icon, label, value, description, tone }) {
  return (
    <li className="evidence-row">
      <span className="evidence-row__index">{index}</span>
      <Icon className="evidence-row__icon" size={22} weight="duotone" aria-hidden="true" />
      <strong>{label}</strong>
      <span className={`evidence-value evidence-value--${tone}`}>{value}</span>
      <span className="evidence-row__description">{description}</span>
    </li>
  );
}

export function Prototype() {
  const [selectedId, setSelectedId] = useState("02");
  const [decision, setDecision] = useState("verify");
  const [acceptance, setAcceptance] = useState("pending");
  const [checkStatus, setCheckStatus] = useState("not_run");
  const [copyState, setCopyState] = useState("Copy first action");
  const [showUnknown, setShowUnknown] = useState(false);
  const [showNote, setShowNote] = useState(false);
  const [note, setNote] = useState("");
  const [decisionTime, setDecisionTime] = useState("");
  const checkTimer = useRef(null);

  const task = useMemo(
    () => TASKS.find((item) => item.id === selectedId) ?? TASKS[1],
    [selectedId],
  );

  useEffect(() => () => window.clearTimeout(checkTimer.current), []);

  function selectTask(id) {
    setSelectedId(id);
    setDecision(id === "01" ? "continue" : id === "03" ? "stop" : "verify");
    setAcceptance("pending");
    setDecisionTime("");
    setCheckStatus("not_run");
    setShowUnknown(false);
  }

  function runSafeCheck() {
    window.clearTimeout(checkTimer.current);
    setCheckStatus("running");
    checkTimer.current = window.setTimeout(() => setCheckStatus("passed"), 900);
  }

  async function copyFirstAction() {
    const command = "python3 -m unittest tests.test_checks -v";
    try {
      await navigator.clipboard.writeText(command);
      setCopyState("Copied");
    } catch {
      setCopyState("Select command below");
    }
    window.setTimeout(() => setCopyState("Copy first action"), 1600);
  }

  function acceptCapsule() {
    setAcceptance("accepted");
    setDecisionTime(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
  }

  function rejectCapsule() {
    setAcceptance("rejected");
    setDecisionTime(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
  }

  const checkValue = checkStatus === "not_run"
    ? "Not run"
    : checkStatus === "running"
      ? "Running…"
      : "Passed";

  const handoffValue = task.tone === "failed" ? "Stop" : task.tone === "fresh" ? "Continue" : "Verify";
  const integrityValue = task.tone === "failed" ? "Failed" : "Passed";
  const freshnessValue = task.tone === "fresh" ? "Fresh" : task.tone === "failed" ? "Not evaluated" : "Changed";

  return (
    <main className="offwork-shell">
      <aside className="capsule-rail" aria-label="Handoff capsules">
        <header className="brand-block">
          <span className="brand-index">00</span>
          <span><strong>Offwork</strong><small>Capsule ledger</small></span>
        </header>
        <span className="rail-caption">Handoff trail · Local system</span>
        <nav className="rail-list">
          {TASKS.map((item) => (
            <RailTask
              key={item.id}
              task={item}
              selected={item.id === selectedId}
              onSelect={() => selectTask(item.id)}
            />
          ))}
        </nav>
        <div className="rail-footnote">Three local capsules · Demo data</div>
      </aside>

      <section className="receipt" aria-label="Selected handoff receipt">
        <header className="topline">
          <span><FolderOpen size={20} weight="duotone" /> Acme checkout / checkout-retry</span>
          <span className="local-only">Local only</span>
        </header>

        <div className="receipt-heading">
          <div>
            <span className="section-kicker">Capsule {task.id}</span>
            <h1>{task.title}</h1>
            <p>Captured {task.captured} 17:42 · By agent Orion · Capsule ID c9f4e7b2</p>
          </div>
          <div className="status-seals">
            <StatusSeal
              icon={task.tone === "failed" ? ShieldWarning : ShieldCheck}
              label="Capsule integrity"
              value={integrityValue}
              tone={task.tone === "failed" ? "failed" : "blue"}
            />
            <StatusSeal
              icon={ArrowClockwise}
              label="Workspace freshness"
              value={freshnessValue}
              tone={task.tone === "fresh" ? "fresh" : "orange"}
            />
          </div>
        </div>

        <section className="decision-strip">
          <div>
            <span className="decision-label">Current decision</span>
            <h2>{handoffValue} first — {task.tone === "changed" ? "the workspace changed after capture" : task.tone === "failed" ? "capsule integrity failed" : "the workspace matches capture"}.</h2>
          </div>
          <div className="decision-actions">
            <button className="primary-action" type="button" onClick={runSafeCheck} disabled={checkStatus === "running" || task.tone === "failed"}>
              <Play size={20} weight="fill" />
              {checkStatus === "running" ? "Running safe check" : "Run safe check"}
            </button>
            <button className="secondary-action" type="button" onClick={copyFirstAction}>
              <Clipboard size={20} weight="duotone" /> {copyState}
            </button>
          </div>
          <div className="decision-switch" aria-label="Handoff decision">
            {DECISIONS.map((item) => (
              <button
                key={item}
                type="button"
                className={decision === item ? "is-active" : ""}
                onClick={() => setDecision(item)}
                aria-pressed={decision === item}
              >
                {item}
              </button>
            ))}
          </div>
        </section>

        <section className="audit-ledger">
          <span className="vertical-label">Audit ledger</span>
          <ol>
            <EvidenceRow index="01" icon={User} label="Agent claimed" value="Tests pass" tone="green" description="Summary supplied by agent Orion at capture time." />
            <EvidenceRow index="02" icon={Eye} label="Offwork observed" value="2 files changed" tone="green" description="src/retry.ts and src/checkout.ts were observed." />
            <EvidenceRow index="03" icon={Flask} label="Auto checked" value={checkValue} tone={checkStatus === "passed" ? "green" : checkStatus === "running" ? "orange" : "neutral"} description={checkStatus === "not_run" ? "The safe check has never been executed." : checkStatus === "running" ? "Offwork is running the authorized local check." : "Offwork ran the authorized local check."} />
            <EvidenceRow index="04" icon={ShieldCheck} label="Handoff verified" value={handoffValue} tone={task.tone === "failed" ? "pink" : "acid"} description={task.tone === "failed" ? "Stop: the published Capsule did not verify." : "Derived from the published Capsule and current workspace."} />
            <EvidenceRow index="05" icon={User} label="Human acceptance" value={acceptance} tone={acceptance === "accepted" ? "green" : acceptance === "rejected" ? "pink" : "neutral"} description={acceptance === "pending" ? "Waiting for your explicit decision." : `Explicitly ${acceptance}${decisionTime ? ` at ${decisionTime}` : ""}.`} />
          </ol>
        </section>

        <section className="open-items">
          <article className="open-item open-item--unknown">
            <span className="vertical-label">Unknown</span>
            <Question size={46} weight="bold" aria-hidden="true" />
            <div>
              <strong>Payment sandbox response not reproduced</strong>
              <p>{showUnknown ? "The 502 response is referenced in the handoff, but no replay evidence is present in the Capsule." : "Could not replay the 502 response from sandbox."}</p>
            </div>
            <button type="button" onClick={() => setShowUnknown((value) => !value)}>{showUnknown ? "Hide details" : "View details"}</button>
          </article>
          <article className="open-item open-item--loop">
            <span className="vertical-label">Open loop</span>
            <ArrowClockwise size={46} weight="bold" aria-hidden="true" />
            <div>
              <strong>Confirm retry limit with PM</strong>
              <p>{note || "Current value is 3. Should we increase to 5?"}</p>
              {showNote && (
                <input
                  autoFocus
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                  placeholder="Add a local demo note"
                  aria-label="Open loop note"
                />
              )}
            </div>
            <button type="button" onClick={() => setShowNote((value) => !value)}>{showNote ? "Close note" : "Add note"}</button>
          </article>
        </section>

        <section className="acceptance-gate">
          <div>
            <span className="vertical-label">Human acceptance</span>
            <strong>Review the evidence above.</strong>
            <p>Automation cannot change this decision. Accept or reject explicitly.</p>
          </div>
          <div className="acceptance-actions" aria-live="polite">
            <button className={acceptance === "accepted" ? "is-selected" : ""} type="button" onClick={acceptCapsule}>
              <Check size={32} weight="bold" /> Accept
            </button>
            <button className={acceptance === "rejected" ? "is-selected" : ""} type="button" onClick={rejectCapsule}>
              <X size={32} weight="bold" /> Reject
            </button>
          </div>
        </section>

        <footer className="receipt-footer">
          <span><FileText size={16} /> Receipt demo · no state is written</span>
          <span><CheckCircle size={16} /> JSON truth boundaries preserved</span>
        </footer>
      </section>
    </main>
  );
}

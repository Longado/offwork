import {
  ArrowRight,
  CheckCircle,
  ClockCounterClockwise,
  Command,
  FileArchive,
  FolderOpen,
  MagnifyingGlass,
  Package,
  Play,
  SealCheck,
  ShieldCheck,
  WarningCircle,
  XCircle,
} from "@phosphor-icons/react";
import "./storyboard.css";

const STORY = {
  status: {
    step: "01 / STATUS",
    eyebrow: "RECORDED RUN · CURRENT CLI USES TASK SHOW",
    title: "Ready to leave this work behind?",
    deck: "Offwork reads the selected project and keeps claims, checks, verification, freshness, and acceptance separate.",
  },
  capture: {
    step: "02 / CAPTURE",
    eyebrow: "RECORDED RUN · CAPSULE CREATED",
    title: "Current work packed.",
    deck: "A local immutable Capsule was published from the explicit project boundary.",
  },
  resume: {
    step: "03 / RESUME",
    eyebrow: "RECORDED RUN · NEW CLI PROCESS",
    title: "Start from evidence, not memory.",
    deck: "The next Session receives one safe Receipt and an exact first action. Resume does not execute it.",
  },
  history: {
    step: "04 / HISTORY",
    eyebrow: "DESIGN TARGET · NOT CURRENT CLI EVIDENCE",
    title: "Find the handoff, not the chat.",
    deck: "History search is not implemented in the CLI yet. This screen is the proposed interaction for Capsule records only.",
  },
  review: {
    step: "05 / REVIEW",
    eyebrow: "RECORDED REJECTION + DESIGN TARGET WORKFLOW STATE",
    title: "Rejected stays in review.",
    deck: "The explicit rejection is recorded. The proposed desktop workflow keeps the task visible for another handoff instead of presenting it as complete.",
  },
};

const TRUTHS = [
  ["Agent claimed", "Tests pass", "claim"],
  ["Offwork observed", "context.json changed", "observed"],
  ["Auto checked", "Failed", "failed"],
  ["Handoff verified", "Passed", "passed"],
  ["Human acceptance", "Pending", "pending"],
];

function DesktopCapsule({ active = false, packed = false }) {
  return (
    <div className={`desktop-capsule ${active ? "is-active" : ""}`}>
      <span className="desktop-capsule__icon"><Package size={32} weight="duotone" /></span>
      <span>
        <small>{packed ? "CAPSULE READY" : "OFFWORK"}</small>
        <strong>{packed ? "Open receipt" : "Pack current work"}</strong>
      </span>
      <ArrowRight size={24} weight="bold" />
    </div>
  );
}

function WindowFrame({ scene, children }) {
  const info = STORY[scene];
  return (
    <div className="story-canvas">
      <div className="desktop-noise" aria-hidden="true" />
      <header className="story-masthead">
        <span className="story-logo">OFF/WORK</span>
        <span>LOCAL HANDOFF SYSTEM</span>
        <span>{info.step}</span>
      </header>
      <section className="story-window">
        <div className="window-bar">
          <span><i /><i /><i /></span>
          <strong><FolderOpen size={17} weight="duotone" /> token-refresh-fix · main</strong>
          <em>LOCAL ONLY</em>
        </div>
        {children}
      </section>
      <DesktopCapsule active={scene === "capture"} packed={scene !== "status"} />
      <footer className="story-footer">
        <span>{info.eyebrow}</span>
        <span>OFFWORK MVP · 2026</span>
      </footer>
    </div>
  );
}

function StatusScene() {
  return (
    <>
      <div className="story-intro">
        <span className="story-number">01</span>
        <div><small>PROJECT STATUS</small><h1>{STORY.status.title}</h1><p>{STORY.status.deck}</p></div>
        <button type="button"><Package size={22} weight="duotone" /> Pack current work</button>
      </div>
      <div className="truth-grid">
        {TRUTHS.map(([label, value, tone], index) => (
          <article key={label} className={`truth-card truth-card--${tone}`}>
            <span>0{index + 1}</span><small>{label}</small><strong>{value}</strong>
          </article>
        ))}
      </div>
      <div className="status-band">
        <ShieldCheck size={38} weight="duotone" />
        <div><small>CAPSULE INTEGRITY</small><strong>PASSED</strong></div>
        <SealCheck size={38} weight="duotone" />
        <div><small>WORKSPACE FRESHNESS</small><strong>FRESH</strong></div>
        <span className="acid-tag">HUMAN ACCEPTANCE · PENDING</span>
      </div>
    </>
  );
}

function CaptureScene() {
  return (
    <div className="capture-layout">
      <div className="capture-hero">
        <span className="story-number">02</span>
        <div className="giant-capsule"><FileArchive size={78} weight="duotone" /><span>CAPSULE<br />00CE8D</span></div>
        <small>CLICK COMPLETE · 09:01:09</small>
      </div>
      <div className="capture-copy">
        <small>CAPTURE COMPLETE</small><h1>{STORY.capture.title}</h1><p>{STORY.capture.deck}</p>
        <dl>
          <div><dt>Agent claimed</dt><dd>“测试全部通过”</dd></div>
          <div><dt>Offwork check</dt><dd className="pink-ink">FAILED · RETURN CODE 1</dd></div>
          <div><dt>Unknown</dt><dd>旧 Token 迁移行为尚未确认</dd></div>
          <div><dt>Next step</dt><dd>运行旧 Token 迁移测试</dd></div>
        </dl>
        <div className="capture-stamps"><span>INTEGRITY · PASSED</span><span>FRESHNESS · FRESH</span><span>ACCEPTANCE · PENDING</span></div>
      </div>
    </div>
  );
}

function ResumeScene() {
  return (
    <div className="resume-layout">
      <aside><span className="story-number">03</span><small>NEW SESSION</small><Command size={52} weight="duotone" /><strong>NO CHAT HISTORY<br />LOADED</strong></aside>
      <section>
        <small>SAFE HANDOFF RECEIPT</small><h1>{STORY.resume.title}</h1><p>{STORY.resume.deck}</p>
        <div className="decision-box"><span>DECISION</span><strong>VERIFY</strong><em>Agent claim conflicts with Offwork's failed check.</em></div>
        <div className="first-action"><Play size={26} weight="fill" /><span><small>FIRST ACTION</small><strong>运行旧 Token 迁移测试</strong></span><button type="button">COPY</button></div>
        <div className="resume-note"><WarningCircle size={24} weight="duotone" /> Human acceptance remains <strong>pending</strong>. Resume did not execute this action.</div>
      </section>
    </div>
  );
}

function HistoryScene() {
  return (
    <div className="history-layout">
      <div className="history-head"><span className="story-number">04</span><div><small>CAPSULE HISTORY · DESIGN TARGET</small><h1>{STORY.history.title}</h1><p>{STORY.history.deck}</p></div></div>
      <div className="search-box"><MagnifyingGlass size={30} weight="bold" /><span>token migration</span><kbd>⌘ K</kbd></div>
      <div className="history-results">
        <article className="is-hit"><span>29 AUG · 09:01</span><strong>修复登录失败</strong><small>Unknown: 旧 Token 迁移行为尚未确认</small><em>REJECTED</em></article>
        <article><span>28 AUG · 17:42</span><strong>Ship checkout retry fix</strong><small>Open loop: confirm retry limit with PM</small><em>PENDING</em></article>
        <article><span>27 AUG · 11:18</span><strong>Upgrade payment SDK</strong><small>Integrity failure · stop</small><em>STOP</em></article>
      </div>
      <div className="concept-warning"><WarningCircle size={25} weight="fill" /> This is a proposed Capsule-record search. It is not Shell history and is not implemented in the current CLI.</div>
    </div>
  );
}

function ReviewScene() {
  return (
    <div className="review-layout">
      <div className="review-banner"><XCircle size={54} weight="fill" /><span><small>HUMAN ACCEPTANCE</small><strong>REJECTED</strong></span><em>09:01:16 · “迁移测试未完成，退回 review”</em></div>
      <div className="review-main">
        <span className="story-number">05</span><div><small>PROPOSED WORKFLOW STATE</small><h1>{STORY.review.title}</h1><p>{STORY.review.deck}</p></div>
        <div className="review-state"><small>TASK</small><strong>REVIEW</strong><span>NOT COMPLETE</span></div>
      </div>
      <div className="review-next"><ClockCounterClockwise size={42} weight="duotone" /><div><small>NEXT HANDOFF</small><strong>Resolve the Unknown, capture again, then ask for explicit acceptance.</strong></div><button type="button">OPEN TASK <ArrowRight size={19} /></button></div>
      <div className="review-proof"><CheckCircle size={23} weight="fill" /> Recorded fact: human acceptance is rejected. Design target: task remains visible in Review until another explicit decision.</div>
    </div>
  );
}

export function Storyboard() {
  const params = new URLSearchParams(window.location.search);
  const requested = params.get("scene") || "status";
  const scene = Object.hasOwn(STORY, requested) ? requested : "status";
  return (
    <WindowFrame scene={scene}>
      {scene === "status" && <StatusScene />}
      {scene === "capture" && <CaptureScene />}
      {scene === "resume" && <ResumeScene />}
      {scene === "history" && <HistoryScene />}
      {scene === "review" && <ReviewScene />}
    </WindowFrame>
  );
}

import { Server, MemoryStick, Cpu, Globe, Radar, History, CircleCheck, CircleX, CirclePause, Users } from "lucide-react"

// Live status side panel: one card per app plus the latest audited operations.
function Meter({ icon: Icon, label, value, pct, warnAt = 80, critAt = 90 }) {
  const level = pct == null ? "none" : pct >= critAt ? "crit" : pct >= warnAt ? "warn" : "ok"
  const width = pct == null ? 0 : Math.max(2, Math.min(100, pct))
  return (
    <div className="hil-meter">
      <div className="hil-meter-top">
        <span className="hil-meter-label"><Icon size={14} aria-hidden="true" />{label}</span>
        <span className="hil-meter-value" dir="ltr">{value}</span>
      </div>
      <div className="hil-bar" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100}
           aria-valuenow={pct == null ? undefined : Math.round(pct)}>
        <div className={`hil-bar-fill hil-${level}`} style={{ width: `${width}%` }} />
      </div>
    </div>
  )
}

function AppCard({ app }) {
  const state = app.running === false ? "off" : app.running ? "on" : "unknown"
  return (
    <section className="hil-card hil-app" aria-label={`وضعیت ${app.name}`}>
      <div className="hil-card-head">
        <span className="hil-icon hil-icon-neutral" aria-hidden="true"><Server size={16} /></span>
        <div className="hil-head-text">
          <div className="hil-title" dir="ltr">{app.name}</div>
          <div className="hil-eyebrow" dir="ltr">{[app.platform, app.plan].filter(Boolean).join(" · ")}</div>
        </div>
        <span className={`hil-pill hil-pill-${state}`}>
          {state === "on" ? <CircleCheck size={13} aria-hidden="true" /> : state === "off" ? <CirclePause size={13} aria-hidden="true" /> : null}
          {state === "on" ? "روشن" : state === "off" ? "خاموش" : "نامشخص"}
        </span>
      </div>
      {app.error ? <p className="hil-muted">{app.error}</p> : null}
      <Meter icon={MemoryStick} label="رم" pct={app.memory_pct}
             value={app.memory_mb == null ? "—" : `${Math.round(app.memory_mb)} MB${app.memory_pct == null ? "" : ` · ${Math.round(app.memory_pct)}%`}`} />
      <Meter icon={Cpu} label="CPU" pct={app.cpu_pct} value={app.cpu_pct == null ? "—" : `${app.cpu_pct}%`} />
      <div className="hil-kv">
        <span className="hil-meter-label"><Globe size={14} aria-hidden="true" />HTTP</span>
        <span className={`hil-pill ${app.http_ok ? "hil-pill-on" : "hil-pill-bad"}`} dir="ltr">
          {app.http_ok ? <CircleCheck size={13} aria-hidden="true" /> : <CircleX size={13} aria-hidden="true" />}
          {app.http_status || "خطا"}{app.latency_ms ? ` · ${app.latency_ms}ms` : ""}
        </span>
      </div>
      <div className="hil-kv">
        <span className="hil-meter-label"><Radar size={14} aria-hidden="true" />پایش خودکار</span>
        <span className={`hil-pill ${app.watching ? "hil-pill-on" : "hil-pill-unknown"}`}>{app.watching ? "فعال" : "غیرفعال"}</span>
      </div>
    </section>
  )
}

export default function StatusPanel() {
  const apps = props.apps || []
  const audit = props.audit || []
  const statusText = { ok: "موفق", error: "خطا", rejected: "رد شد", blocked: "مسدود" }
  return (
    <div dir="rtl" className="hil-panel">
      <div className="hil-team"><Users size={14} aria-hidden="true" /><span>تیم Human in the Liara</span></div>
      {apps.length === 0 ? (
        <p className="hil-muted">هنوز برنامه‌ای انتخاب نشده. از دستیار بخواهید وضعیت یک برنامه را نشان دهد.</p>
      ) : apps.map((a) => <AppCard key={a.name} app={a} />)}
      {audit.length > 0 ? (
        <section className="hil-card" aria-label="آخرین عملیات">
          <div className="hil-section-title"><History size={14} aria-hidden="true" />آخرین عملیات</div>
          <ol className="hil-timeline">
            {audit.map((e, n) => (
              <li key={n} className={`hil-tl-${e.status}`}>
                <span className="hil-tl-dot" aria-hidden="true" />
                <span className="hil-tl-title">{e.title}</span>
                <span className="hil-tl-meta" dir="ltr">{e.time}</span>
                <span className={`hil-tl-status hil-tl-status-${e.status}`}>
                  {statusText[e.status] || e.status}{e.by === "user" ? " · با تأیید شما" : ""}
                </span>
              </li>
            ))}
          </ol>
        </section>
      ) : null}
      <p className="hil-updated">به‌روزرسانی: <span dir="ltr">{props.updated}</span></p>
    </div>
  )
}

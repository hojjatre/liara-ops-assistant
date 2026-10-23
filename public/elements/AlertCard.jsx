import { Button } from "@/components/ui/button"
import { TriangleAlert, OctagonAlert, Stethoscope } from "lucide-react"
import { useState } from "react"

// Proactive monitoring alert with a one-click "diagnose" action (handled by @cl.action_callback("diagnose")).
export default function AlertCard() {
  const [sent, setSent] = useState(false)
  const critical = props.severity === "critical"
  const issues = props.issues || []

  const diagnose = () => {
    if (sent) return
    setSent(true)
    callAction({ name: "diagnose", payload: { app: props.app, issues: issues.map((i) => i.title).join("؛ ") } })
  }

  return (
    <div dir="rtl" className={`hil-card hil-alert ${critical ? "hil-risk-high" : "hil-risk-medium"}`} role="alert">
      <div className="hil-card-head">
        <span className="hil-icon" aria-hidden="true">
          {critical ? <OctagonAlert size={18} /> : <TriangleAlert size={18} />}
        </span>
        <div className="hil-head-text">
          <div className="hil-eyebrow">هشدار پایش خودکار · {props.time}</div>
          <div className="hil-title">{props.app}</div>
        </div>
        <span className={`hil-badge ${critical ? "hil-badge-high" : "hil-badge-medium"}`}>
          {critical ? "بحرانی" : "هشدار"}
        </span>
      </div>
      <ul className="hil-list">
        {issues.map((i, n) => (<li key={n}>{i.title}</li>))}
      </ul>
      <div className="hil-actions">
        <Button className="hil-btn-primary" onClick={diagnose} disabled={sent}>
          <Stethoscope size={16} aria-hidden="true" /> {sent ? "در حال عیب‌یابی…" : "عیب‌یابی کن"}
        </Button>
      </div>
    </div>
  )
}

import { Button } from "@/components/ui/button"
import { ShieldAlert, ShieldCheck, Clock, Wallet, Undo2, Info, ChevronDown, Check, X } from "lucide-react"
import { useState } from "react"

// Human-in-the-loop approval card. Rendered through cl.AskElementMessage; the user's choice is
// returned with submitElement({ approved }). When props.decided is set it renders read-only.
export default function ApprovalCard() {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const high = props.risk === "high"
  const decided = props.decided
  const impact = props.impact || {}
  const rows = [
    ["downtime", Clock, "قطعی سرویس"],
    ["cost", Wallet, "هزینه"],
    ["rollback", Undo2, "بازگشت‌پذیری"],
    ["note", Info, "نکته"],
  ].filter(([key]) => impact[key])

  const decide = (approved) => {
    if (busy) return
    setBusy(true)
    submitElement({ approved })
  }

  return (
    <div dir="rtl" className={`hil-card hil-approval ${high ? "hil-risk-high" : "hil-risk-medium"}`}
         role="group" aria-label={`درخواست تأیید: ${props.title}`}>
      <div className="hil-card-head">
        <span className="hil-icon" aria-hidden="true">
          {high ? <ShieldAlert size={18} /> : <ShieldCheck size={18} />}
        </span>
        <div className="hil-head-text">
          <div className="hil-eyebrow">{high ? "اقدام حساس — نیازمند تأیید شما" : "نیازمند تأیید شما"}</div>
          <div className="hil-title">{props.title}</div>
        </div>
        <span className={`hil-badge ${high ? "hil-badge-high" : "hil-badge-medium"}`}>
          {high ? "ریسک بالا" : "ریسک متوسط"}
        </span>
      </div>

      <p className="hil-action">{props.action}</p>

      {props.reason ? (
        <div className="hil-reason">
          <div className="hil-label">چرا؟</div>
          <p>{props.reason}</p>
        </div>
      ) : null}

      {rows.length > 0 ? (
        <dl className="hil-impact">
          {rows.map(([key, Icon, label]) => (
            <div className="hil-impact-row" key={key}>
              <dt><Icon size={15} aria-hidden="true" /><span>{label}</span></dt>
              <dd>{impact[key]}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      <button type="button" className="hil-link" aria-expanded={open} onClick={() => setOpen(!open)}>
        <ChevronDown size={14} aria-hidden="true" className={open ? "hil-rot" : ""} />
        <span>{open ? "پنهان کردن جزئیات فنی" : "جزئیات فنی (API)"}</span>
      </button>
      {open ? (
        <pre className="hil-code" dir="ltr">{`${props.tool}(${JSON.stringify(props.args || {}, null, 2)})`}</pre>
      ) : null}

      {decided ? (
        <div className={`hil-decision ${decided === "approved" ? "hil-ok" : "hil-no"}`} role="status">
          {decided === "approved" ? <Check size={16} aria-hidden="true" /> : <X size={16} aria-hidden="true" />}
          <span>{decided === "approved" ? "تأیید شد و اجرا شد" : (props.note || "رد شد؛ هیچ تغییری انجام نشد")}</span>
        </div>
      ) : (
        <div className="hil-actions">
          <Button className="hil-btn-approve" onClick={() => decide(true)} disabled={busy}>
            <Check size={16} aria-hidden="true" /> تأیید و اجرا
          </Button>
          <Button variant="outline" className="hil-btn-reject" onClick={() => decide(false)} disabled={busy}>
            <X size={16} aria-hidden="true" /> رد
          </Button>
        </div>
      )}
    </div>
  )
}

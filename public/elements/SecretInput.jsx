import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Lock, Eye, EyeOff, Check, X } from "lucide-react"
import { useState } from "react"

// Private secret entry. The value goes straight to the server with submitElement and is never
// posted as a chat message or sent to the language model. Paste and password managers are allowed.
export default function SecretInput() {
  const [value, setValue] = useState("")
  const [show, setShow] = useState(false)
  const [busy, setBusy] = useState(false)
  const id = `secret-${props.name}`

  const submit = (e) => {
    if (e) e.preventDefault()
    if (!value.trim() || busy) return
    setBusy(true)
    submitElement({ value })
  }

  return (
    <form dir="rtl" className="hil-card hil-secret" onSubmit={submit}>
      <div className="hil-card-head">
        <span className="hil-icon hil-icon-neutral" aria-hidden="true"><Lock size={18} /></span>
        <div className="hil-head-text">
          <div className="hil-eyebrow">ورود امن</div>
          <label className="hil-title" htmlFor={id}>مقدار <code dir="ltr">{props.name}</code></label>
        </div>
      </div>
      <p className="hil-muted">
        این مقدار فقط به لیارا فرستاده می‌شود؛ در گفتگو نمایش داده نمی‌شود و به مدل زبانی یا گزارش‌ها نمی‌رسد.
      </p>
      <div className="hil-secret-row">
        <Input id={id} dir="ltr" type={show ? "text" : "password"} autoComplete="off" spellCheck={false}
               value={value} onChange={(e) => setValue(e.target.value)} placeholder={props.placeholder || ""}
               className="hil-input" autoFocus />
        <button type="button" className="hil-icon-btn" onClick={() => setShow(!show)}
                aria-label={show ? "پنهان کردن مقدار" : "نمایش مقدار"}>
          {show ? <EyeOff size={16} aria-hidden="true" /> : <Eye size={16} aria-hidden="true" />}
        </button>
      </div>
      <div className="hil-actions">
        <Button type="submit" className="hil-btn-primary" disabled={!value.trim() || busy}>
          <Check size={16} aria-hidden="true" /> ثبت امن
        </Button>
        <Button type="button" variant="outline" onClick={() => cancelElement()} disabled={busy}>
          <X size={16} aria-hidden="true" /> انصراف
        </Button>
      </div>
    </form>
  )
}

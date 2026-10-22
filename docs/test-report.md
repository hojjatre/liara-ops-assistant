# Live test report

**Date:** 22 Oct 2026 (1 Aban 1405 eve) · **Environment:** assistant running locally (`chainlit run app.py -w`),
real Liara team account, BoxAPI `gpt-5.6-sol` · **Test app:** `demo-shop-hil` (Node, `small-g2` + basic bundle)

The scenarios from the [implementation plan](implementation-plan.md) were run end to end through the chat UI
against the real Liara API. Every 🟡/🔴 action went through an approval card.

## Results

| # | Scenario | What happened | Result |
|---|---|---|---|
| 1 | List apps, analyse project, recommend plan | Detected Node.js / port 3000 / `liara.json` health check / expected `DATABASE_URL`; recommended `small-g2` with cost per month | ✅ |
| 2 | Create app | 1st attempt: Liara returned `g2_plan_unavailable`. Found two causes: g2 plans need a private network, and the `free` bundle isn't offered for `small-g2`. After the fix, the card showed the network and the cheapest available bundle (basic); approved → app created | ✅ after fix |
| 3 | First deploy | 1st attempt: Liara rejected the release (`"healthCheck.startPeriod" must be less than or equal to 3000`); the assistant read the error and told the user exactly what to change. After fixing `liara.json`: build → READY, health check 200 | ✅ after fix |
| 4 | HTTP 500 troubleshooting | Deployed without `DATABASE_URL` → 3/3 probes 500, 18 log errors `DATABASE_URL is not set` → top hypothesis *missing env var* → approval card (value hidden) → value entered in the private 🔐 prompt → app restarted → verify: **500 → 200, 0 new errors** | ✅ |
| 5 | Secret handling | The entered DB URL never appeared in the chat text, the LLM context or `data/audit.jsonl` (`"DATABASE_URL": "***"`) | ✅ |
| 6 | Slowness / memory leak | `/api/leak` pushed RAM to ~90% → diagnosis *memory pressure / leak* (29 MB → 459 MB in an hour) → approved restart → real RSS 460 → 62 MB | ✅ (verification bug found, see below) |
| 7 | Reject path | Asked to resize to `medium-g2`; card showed **+350,000 T/month**; rejected → nothing changed, assistant acknowledged and offered to retry later | ✅ |
| 8 | Blocked action | "Delete the app" → refused, with console steps | ✅ |
| 9 | Domain guidance | Explained Liara DNS flow incl. the manual nic.ir step; created nothing as asked | ✅ |
| 10 | Monitoring | `watch_app` → background tick saw RAM 89.8% → ⚠️ alert with 🔍 button → one click → diagnosis → approved restart → verify **RAM 89.3% → 5.5%, CPU 0.3%** | ✅ after fix |
| 11 | Cleanup | Approved `stop_app` → app off | ✅ |

## Bugs found by live testing and fixed

| Bug | Cause | Fix |
|---|---|---|
| App creation failed (`g2_plan_unavailable`) | No private network sent; requested bundle not offered for the plan | `create_app` picks/creates a network (as the official CLI does) and validates bundle availability; plan parser keeps only available bundles |
| Deploy rejected | Demo `liara.json` used ms-style health-check values | Use Liara's documented values (`interval 30, timeout 15, retries 3, startPeriod 10`) |
| "RAM still 90%" right after a successful restart | Liara keeps reporting the old container's series for a few minutes and the parser summed containers | Read only the newest container per timestamp; verification uses only samples taken after the action |
| CPU reported as 130-150% on an idle app | CPU values are CPU-seconds over a trailing 5-minute window, not cores | Convert with `value / 300`; idle app now shows 0.2-0.4% |
| No monitor alert at 89.8% RAM | Alerts fired only on *critical* (≥90%) issues | Alert on warnings too; monitor errors are logged to `data/monitor.log` instead of being swallowed |

All fixes have regression tests (`python -m pytest`: 21 passing, mocked API + scripted LLM).

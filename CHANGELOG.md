# Changelog

Notable changes to the Centsys Gate Remote integration.

## 0.6.0 (2026-09-26) — Live status (real-time updates)

### New: opt-in "Live status" mode

The integration normally cloud-polls every 60 seconds. That means a gate
operated from **outside** Home Assistant — a **physical remote**, the app, or a
schedule — may only update on the next poll, and a quick open → auto-close cycle
can fall entirely between polls, so the cover looks like it jumped straight from
closed to open (or never moved at all).

**Live status** fixes that. When you turn it on, the integration keeps a
lightweight live connection to your SMART Wi-Fi gate and reports movement **in
real time no matter who triggers it** — including a physical remote — streaming
`opening → open → closing → closed` as it happens. It also keeps battery and
safety-beam readings fresh while the gate sits idle.

It connects with its **own identity**, separate from the MyCentsys app, so the
two run side by side: enabling it does **not** sign you out of the app, and using
the app doesn't disturb Home Assistant. (Confirmed on hardware.)

### How to enable

**Settings → Devices & Services → Centsys Gate Remote → Configure → Enable live
status.**

It's **off by default** for now while we gather feedback across operator models
(see below). GSM/ULTRA and shared gates are unaffected — they have no live
telemetry channel and continue to use polling.

### Help us make it the default — feedback wanted

The goal is to enable Live status by default once it's proven across the range.
So far it's confirmed on a **D5-Evo SMART+**. If you switch it on, please open a
GitHub issue with your **operator model** and whether live status works —
**battery/solar-powered** units and the **AU** region especially.

| Operator | Live status |
|---|---|
| D5-Evo SMART+ | Confirmed on hardware |
| Other SMART / SMART+ sliders (D3, D4, D6, D10, D20, ...) | Please test |
| SD0-series garage doors | Please test |
| VANTAGE / VertX / Vector (swing) | Please test |
| GSM / ULTRA, shared gates | Not applicable (no live telemetry channel) |

### Under the hood

- No change to how the gate is opened, or to the default polling behaviour when
  Live status is off.
- The internal MQTT transport was consolidated behind a single shared session
  helper; the open, telemetry and follow paths are byte-for-byte unchanged
  (verified by the regression harness).

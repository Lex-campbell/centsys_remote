# Changelog

Notable changes to the Centsys Gate Remote integration.

## 0.6.4 (2026-10-30) — Stable release

First stable release since 0.6.0. It includes everything that shipped in the
0.6.1b1–0.6.4b1 betas. Update and restart Home Assistant. Existing installs
that were already working need no re-setup; accounts without a stored region
keep using the South African datacentre.

### New: automatic regional datacentre selection

Centurion runs separate regional datacentres, and each account lives on exactly
one of them. Until now the integration only talked to the South African
datacentre, so **Australia / New Zealand** accounts couldn't see their gates.

The datacentre is now chosen **automatically from the country you pick at setup**
— AU/NZ use the Australia datacentre, everyone else uses South Africa — so there
is normally nothing to configure. Every backend call (the Azure gateway, the
GWeb/access-sharing hosts, and the MQTT broker) follows the account's region.

- The setup form has an optional **Datacentre** dropdown, best left on
  **Auto (from country)**. The explicit *South Africa* / *Australia & New Zealand*
  choices cover the rare case where a number's country and its gate's datacentre
  don't line up.
- You can switch datacentre after setup without removing the integration:
  **Settings → Devices & Services → Centsys Gate Remote → ⋮ → Reconfigure**. A
  fresh one-time PIN is sent on the chosen datacentre to reconnect.

### New: admin-linked gates are now discovered

Some accounts are set up as an **operator admin** rather than (or as well as) a
**remote user**. Those are two separate lists on Centurion's side, and until now
the integration only read the remote-user one — so a gate that showed up in the
app could be missing from Home Assistant with a "no gates linked" notice.

The integration now also checks the admin list and **combines both**, removing
duplicates, so a gate linked either way appears. There is nothing to configure,
and accounts that were already working are unaffected (a gate linked both ways is
shown once). The extra lookup is best-effort: if it is unavailable it is simply
skipped and never interrupts the normal update.

### Fixed: garage doors no longer show gate-only controls

Some garage operators report telemetry that looks like a gate, so the integration
could show gate-only controls (Holiday Lock, Keep Open, Pedestrian) on a garage.
Holiday Lock shares its command with the door open, so toggling it would move the
door. Gate vs garage is now decided from the product code the operator reports
about itself, and those controls appear only once an operator is positively a
gate. (#10)

Telemetry is decoded by the operator's reported family rather than guessed from
the message size, and the newer garage telemetry format is read correctly
(status and battery), so garage diagnostics are no longer blank on recent
firmware. On a gate, the controls appear immediately; on a brand-new setup they
appear once the operator has reported in.

### Fixed: live status stays current while the app is open

Live gate status — and the **Keep open** / **Holiday lock** switches — could stop
updating (or lag and flip between on/off) when the **MyCentsys app was open at the
same time**. The integration's status reads were identifying to the gate with the
same connection id as the app, so the gate treated them as a duplicate and stopped
sending status updates until the app disconnected.

Status reads now use their own connection id, so they keep working alongside the
app. Opening/closing and the switch commands are unchanged.

### Documentation

- The known-devices table now lists the **Centurion Vantage** (swing, Wi-Fi) as
  reported working, and calls out the **D10 SMART** as Bluetooth-only and
  therefore not controllable remotely. The Wi-Fi model is the **D10 SMART+**.

## 0.6.4b1 (2026-10-28) — Fix live status going stale while the app is open (beta)

### Fixed

Live gate status — and the **Keep open** / **Holiday lock** switches — could stop
updating (or lag and flip between on/off) when the **MyCentsys app was open at the
same time**. The integration's status reads were identifying to the gate with the
same connection id as the app, so the gate treated them as a duplicate and stopped
sending status updates until the app disconnected.

Status reads now use their own connection id, so they keep working alongside the
app. Opening/closing and the switch commands are unchanged. If you saw the switches
not matching the motor, or state bouncing around, update and it should track
correctly again.

## 0.6.3b1 (2026-10-08) — Find gates linked by admin as well as remote user (beta)

### New: admin-linked gates are now discovered

Some accounts are set up as an **operator admin** rather than (or as well as) a
**remote user**. Those are two separate lists on Centurion's side, and until now
the integration only read the remote-user one — so a gate that showed up in the
app could be missing from Home Assistant with a "no gates linked" notice.

The integration now also checks the admin list and **combines both**, removing
duplicates, so a gate linked either way appears. There is nothing to configure,
and accounts that were already working are unaffected (a gate linked both ways is
shown once). The extra lookup is best-effort: if it is unavailable it is simply
skipped and never interrupts the normal update.

## 0.6.1b1 (2026-09-29) — Region / datacentre support (beta)

### New: automatic regional datacentre selection

Centurion runs separate regional datacentres, and each account lives on exactly
one of them. Until now the integration only talked to the South African
datacentre, so **Australia / New Zealand** accounts couldn't see their gates.

The datacentre is now chosen **automatically from the country you pick at setup**
— AU/NZ use the Australia datacentre, everyone else uses South Africa — so there
is normally nothing to configure. Every backend call (the Azure gateway, the
GWeb/access-sharing hosts, and the MQTT broker) follows the account's region.

### Changing it later

- The setup form has an optional **Datacentre** dropdown, best left on
  **Auto (from country)**. The explicit *South Africa* / *Australia & New Zealand*
  choices cover the rare case where a number's country and its gate's datacentre
  don't line up.
- You can switch datacentre after setup without removing the integration:
  **Settings → Devices & Services → Centsys Gate Remote → ⋮ → Reconfigure**. A
  fresh one-time PIN is sent on the chosen datacentre to reconnect.

Existing installs are unaffected — entries without a stored region keep using the
South African datacentre.

### Beta — feedback wanted

This is a prerelease so **Australia / New Zealand** users can confirm their gates
are discovered and controllable. Please open a GitHub issue with your region and
operator type (SMART Wi-Fi vs GSM/ULTRA).

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

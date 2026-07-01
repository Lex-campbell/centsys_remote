# CenSys Gate Remote for Home Assistant

Control and monitor your Centurion **SMART Wi-Fi gate operator** (e.g. **D5 Evo SMART**) directly from Home Assistant — open and close the gate, see live open/closed status, and track battery, mains power, safety beams and more.

> **Status: beta.** This is an unofficial, community-built integration and is not affiliated with or endorsed by Centurion Systems. It talks to the same cloud service the official CenSys app uses. Use at your own risk; feedback is very welcome (see [Giving feedback](#giving-feedback)).

![The gate operator in Home Assistant, showing controls, sensors and diagnostics](images/device-dashboard.png)

---

## Features

- **Gate cover** — open / close from the dashboard, automations, voice assistants, etc.
- **Live status** — the gate animates `opening → open → closing → closed` in real time while it moves, then settles to the steady status.
- **Rich diagnostics** — battery voltage, mains/power supply status, safety-beam states, online/offline, fault and warranty flags, last-seen time, and Wi-Fi signal.
- **Simple onboarding** — sign in with your phone number and a one-time PIN (delivered via WhatsApp or SMS), exactly like the app.

---

## Requirements

- **Home Assistant 2024.1 or newer.**
- A **Centurion SMART Wi-Fi operator** that is already set up and working in the official **CenSys / MyCentsys Remote** app.
- The **phone number** registered to that operator in the app (you'll receive a one-time PIN during setup).
- Home Assistant must have **outbound internet access** (the integration talks to Centurion's cloud).
- **The gate must be linked to your number as a remote user** (see below).

> [!IMPORTANT]
> **Your gate must appear in the official MyCentsys Remote app** when you log in with the phone number you'll use here. The integration only sees operators that have your number added as a **remote user** — a "direct"/Bluetooth-only connection is **not** enough.
>
> If you've only ever connected to the gate directly, ask whoever has **admin access** to the operator to add your cell number as a remote user (in the app, under the operator's users), or add/claim the operator to your own account first. A quick check: **if the gate doesn't show up in the official app for your number, it won't show up here either.**
>
> You can still add the integration before your gate is linked — it will sign in and show a notification explaining there are no gates yet. Once your number is added as a remote user, **the gate appears automatically within about a minute, no restart needed.**

There are no extra Python packages to install by hand — Home Assistant installs everything the integration needs automatically on first start.

---

## Installation

### HACS (custom repository)

1. In HACS, open the **⋮** menu → **Custom repositories**.
2. Add `https://github.com/lex-campbell/centsys_remote` with category **Integration**.
3. Find **CenSys Gate Remote** in HACS, **Download** it, then **restart Home Assistant**.

### Manual

The integration code lives under `custom_components/centsys_remote/` in this repository. Copy that whole folder into your Home Assistant `custom_components` directory.

1. Download this repository — the green **Code → Download ZIP** button, or `git clone`.
2. Copy the repo's `custom_components/centsys_remote/` folder into your Home Assistant config so you end up with:

   ```
   <config>/custom_components/centsys_remote/__init__.py
   <config>/custom_components/centsys_remote/manifest.json
   ... (and the rest of the files)
   ```

   - **HAOS / Supervised:** use the *Samba share* or *Studio Code Server* add-on to copy the folder into `config/custom_components/`.
   - **Container / Core:** copy it into your mapped config directory, e.g. `scp -r custom_components/centsys_remote user@homeassistant:/config/custom_components/`.

3. **Restart Home Assistant** (a full restart, not just "Quick reload"). On first boot it will install the integration's dependencies — give it a minute.

---

## Setup

![Adding the integration and entering your mobile number](images/setup-flow.png)

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **CenSys Gate Remote**.
3. Enter the **phone number** registered to your gate (optionally a name/email), and choose how you'd like to receive your **one-time PIN**:
   - **WhatsApp** *(default)* — the PIN arrives as a WhatsApp message, exactly as the official app does it. This is the method we've tested.
   - **SMS** — the PIN is sent as a text message instead. This option is offered by Centurion's backend, but we haven't been able to verify it on every account, so if no code arrives, switch back to WhatsApp.
4. Enter the **code** to finish. Your gate(s) appear as a device with all the entities below.

If you have more than one operator on your account, all of them are added.

---

## Entities

Each gate operator becomes one device. The main control is the **cover**; everything else is read-only.

### Controls

| Entity | What it does |
| --- | --- |
| **Gate** (cover) | Open / close the gate. Centurion operators are single-button triggers, so both buttons pulse the gate and it decides direction from its current position — just like the physical remote. The control greys out to reflect the current state (open disabled when already open, etc.). |

### Sensors

| Entity | Description |
| --- | --- |
| **Operator status** | `open`, `closed`, `opening`, `closing`, `partly open`, `partly closed`. |
| **Battery voltage** | Operator battery voltage (V). Refreshed on a slower cycle — see [About the battery & live telemetry](#about-the-battery--live-telemetry). |
| **Power supply** | Mains/power supply condition: `normal`, `low`, `off`, `unknown`. |
| **Closing safety beam** / **Opening safety beam** | Beam condition: `clear`, `obstructed`, `disabled`, etc. |
| **Theft alarm state** | `activated`, `cleared`, `disabled`. |
| **Last seen** | When the operator last reported to the cloud. |
| **Wi-Fi signal** | Operator Wi-Fi RSSI (disabled by default — enable it in the entity settings if you want it). |
| **Operator temperature** | Reported board temperature (disabled by default; not populated on all models). |

### Binary sensors

| Entity | Description |
| --- | --- |
| **Online** | Whether the operator is currently reachable. |
| **Fault** | Operator-reported fault/health problem. |
| **Warranty void** | Warranty flag (disabled by default). |

> Sensors marked *disabled by default* won't appear until you enable them: open the device, click the entity, then the cog → **Enable**.

---

## Using it

Once set up, the gate behaves like any other cover in Home Assistant:

- **Dashboard:** add the gate as a tile or cover card. The tile card gives a compact open/close control.
- **Automations & scripts:** use the standard `cover.open_cover`, `cover.close_cover` services, or the cover state in conditions/triggers.
- **Voice / mobile:** works with anything that understands HA covers.

Example automation — notify if the gate is left open for more than 5 minutes:

```yaml
automation:
  - alias: Gate left open
    triggers:
      - trigger: state
        entity_id: cover.d5_evo_smart   # use your gate's entity id
        to: "open"
        for: "00:05:00"
    actions:
      - action: notify.mobile_app_your_phone
        data:
          message: "The gate has been open for 5 minutes."
```

---

## About the battery & live telemetry

- **Live open/close status** is real time: when you press open/close, the integration follows the gate's live updates for the duration of the cycle, so the cover animates accurately.
- **Battery voltage and similar deep diagnostics** come from a heavier check that briefly wakes the operator, so they refresh on a **slower schedule (about every 15 minutes)**, not every few seconds. After first setup the **battery voltage may show *unknown* until the first telemetry cycle completes** — this is normal. It will populate shortly.
- Battery is reported as **voltage** (e.g. `13.4 V`) rather than a percentage, because that's the trustworthy value the operator provides. A reading around 13–14 V typically means the operator is on mains with a healthy battery.

---

## Enabling debug logging

If something isn't working, debug logs make it much easier to help. Add this to your `configuration.yaml`, then restart Home Assistant:

```yaml
logger:
  default: info
  logs:
    custom_components.centsys_remote: debug
```

You can then watch the logs live under **Settings → System → Logs** (use **Load full logs**), or download them to attach to a bug report.

To turn debug logging back off, remove those lines and restart, or run the **Logger: Set level** action with level `info`.

---

## Troubleshooting

- **"CenSys Gate Remote" doesn't appear in Add Integration.** Make sure the files are at `config/custom_components/centsys_remote/` (with `manifest.json` directly inside) and that you did a **full restart**. Clear your browser cache if needed.
- **No PIN arrives.** Confirm the number is exactly the one registered in the CenSys app (including country code). If you chose **SMS** and nothing comes through, retry the setup and pick **WhatsApp** instead (it's the channel we've confirmed working). Make sure the chosen app (WhatsApp or your messaging app) is reachable on that number.
- **A notification says "no gates linked" / the device has no entities.** Login worked, but no operator has your number added as a **remote user**. Open the official MyCentsys Remote app with the same number — if the gate isn't there either, get an admin to add your number as a remote user on the operator (or add/claim the gate to your account). It will then appear here automatically within about a minute — no restart needed. See [Requirements](#requirements).
- **Gate won't open from HA but works in the app.** Check the operator is **Online** in HA, and that your account still has permission in the app. Enable debug logging and capture what happens when you press open.
- **Battery voltage stays *unknown*.** Wait for a telemetry cycle (up to ~15 minutes), or restart HA. If it never populates, the operator may have been asleep/offline at each attempt — grab debug logs.
- **State seems to lag.** Steady-state status refreshes about once a minute; live motion is tracked in real time during an open/close. Brief states between polls are expected to be smoothed by the live follow.

---

## Removing the integration

1. **Settings → Devices & Services → CenSys Gate Remote → ⋮ → Delete.** This removes the device, all entities, and your stored login.
2. (Optional) Delete the `custom_components/centsys_remote` folder.
3. Restart Home Assistant.

Always delete via the UI **before** removing the folder, so Home Assistant can clean up properly.

---

## Privacy & security

- Your login is stored only inside Home Assistant's config entry for this integration; removing the integration deletes it.
- The integration communicates with Centurion's cloud service over encrypted connections.
- No data is sent anywhere other than Centurion's service.

---

## Giving feedback

This is a beta — please report anything odd! When opening an issue, it helps to include:

- Your Home Assistant version and how you run it (HAOS, Container, etc.).
- Your operator model (e.g. D5 Evo SMART).
- What you expected vs. what happened.
- Relevant **debug logs** (see [Enabling debug logging](#enabling-debug-logging)).

Thanks for testing!

---

## License

Released under the [MIT License](LICENSE).

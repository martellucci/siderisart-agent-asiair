<p align="center">
  <img src="logo.png" alt="Sideris Art" width="180">
</p>

<h1 align="center">ASIAIR agent for a remote observatory</h1>

<p align="center">
  <em>A full night of imaging, unattended: plan start, monitoring, weather pause,
  flats and darks at dawn, safe shutdown, session log and statistics.</em>
</p>

<p align="center">
  <a href="#license"><img src="https://img.shields.io/badge/license-MIT-f5e6b8" alt="MIT"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-071535" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/ASIAIR-firmware%20v1-0a1c3e" alt="ASIAIR v1">
</p>

---

<p align="center">
  <strong>English</strong> · <a href="README.it.md">Italiano</a>
</p>

---

## What this is

This is the software that runs my nights on a **remote observatory**, a few
thousand kilometres from home. It lives on a small Linux server, talks to the
**ASIAIR** directly over its JSON-RPC protocol — no app, no VNC, no tapping —
and takes care of everything you would normally do by hand, except it does it at
three in the morning while you sleep, and at dawn it sends you the night's
summary on Telegram with a link to the statistics.

This is not a product. It's a personal project that grew night after night, with
real failures behind every safeguard in it. I'm publishing it because the traps I
walked into — and there were many — might save someone else the same wasted
evenings.

> **If you find it useful**, the thing I'd appreciate most is that you subscribe
> to the newsletter: that's where I write about how the project evolves, what
> breaks, what I learn night after night — and the photographs that come out of
> it.
>
> 👉 **[sideris.art/journal](https://sideris.art/journal/)**
>
> *(The Journal and the newsletter are in Italian. The code and this
> documentation are in English.)*

---

## What it does

**Through the night**

- Checks that all devices are connected and that the **mount is actually pointing
  at the right coordinates** before starting anything (position gate).
- Reads **roof state** from an ASCOM Alpaca API and only operates inside
  **nautical night**, computed from the observatory's coordinates.
- Turns on the camera's anti-dew heater and the dew strap, opens the motorised
  flat panel, **starts the plan**.
- Every 5 minutes: verifies imaging is progressing, **syncs FITS** to the NAS
  incrementally, updates the session log.
- **Weather closure is a pause, not an ending**: it stops the plan, parks, closes
  the panel — but keeps the cooler running and resets nothing. When the roof
  reopens, **the plan resumes by itself from where it left off**.

**At dawn**

- Stops the plan, parks the mount, resets the plan for the next night.
- Dew strap to full and **30 minutes of drying** before any flat, because
  condensation on the panel ruins flats and you won't notice until it's too late.
- **Flats per (filter, gain) actually used that night**: it reads from the
  session log which filters you shot and at what gain, groups them, sets panel
  brightness per group and runs one autorun for each, with AUTO exposure.
- **Dark flats** immediately after, panel off, at exactly the exposure computed
  from the flats it just took.
- Final sync, `pi_shutdown`, waits until the box is **actually** dead, and only
  then cuts power at the smart plugs.

**Always**

- **Telegram bot** with a button menu: status, power-on, start plan, manual sync,
  on-demand flats/darks, shutdown — every dangerous action behind a confirmation.
- **MQTT telemetry** to Home Assistant: ~50 entities covering guiding (RMS and
  peaks over a moving window), camera, focus, mount, storage, the box's CPU
  temperature and per-outlet power draw.
- **Automatic session log**: reads FITS headers into SQLite, updates a Google
  Sheet with aggregates, writes per-night detail to CSV.
- **Self-contained HTML dashboard**: hours per target and filter, yield against
  available darkness, guiding RMS and HFR per night, histograms.

---

## How it's built

```
                 ┌─────────────┐   Alpaca/HTTPS    ┌──────────────┐
                 │  roof API   │◄──────────────────┤              │
                 └─────────────┘                   │              │
                 ┌─────────────┐   TP-Link cloud   │    AGENT     │
                 │ smart plugs │◄──────────────────┤  (every 5')  │
                 └─────────────┘                   │              │
                 ┌─────────────┐   JSON-RPC 4700   │    state     │
                 │   ASIAIR    │◄──────────────────┤   machine    │
                 │             │   JSON-RPC 4400   │              │
                 └──────┬──────┘◄──────────────────┤              │
                        │ SMB                      └──┬────┬──────┘
                        ▼                             │    │
                 ┌─────────────┐  rsync            ┌──▼─┐ ┌▼─────────┐
                 │     NAS     │◄──────────────────┤ DB │ │ Telegram │
                 └─────────────┘                   └──┬─┘ └──────────┘
                                                      │
                                          ┌───────────▼────────────┐
                                          │ Google Sheet · CSV ·   │
                                          │ HTML dashboard · MQTT  │
                                          └────────────────────────┘
```

| File | Role |
|---|---|
| `sfro_agent.py` | The agent. A systemd **oneshot timer every 5 minutes**: no daemon that can die quietly, every cycle starts fresh by reading state from disk. Night orchestration, teardown, flat/dark flow, sync. |
| `sfro_mqtt.py` | Long-running service: MQTT/Home Assistant telemetry, listens to the ASIAIR's push events, writes guiding and autofocus JSONL sinks. |
| `sfro_telegram.py` | Long-running service: Telegram bot with button menu and confirmations. |
| `sfro_sessionlog.py` | Session log: FITS → SQLite → Google Sheet + CSV. |
| `sfro_report.py` | HTML statistics dashboard (single file, no CDN). |
| `asiair_client.py` | JSON-RPC transport to the ASIAIR (ports 4700 and 4400). |

**Why oneshot instead of a daemon**: a process that runs for days accumulates
zombie sockets, CIFS mounts stuck in D-state and inconsistent state. A cycle that
is born, reads its state from a JSON file, does one thing and dies is far harder
to break — and if it does hang, the next timer fires anyway.

> **A note on names**: files and services are prefixed `sfro_` / `sfro-`, after
> the remote observatory this was written for. It's just a name — nothing in the
> code is tied to any particular site.

---

## Requirements

- An **ASIAIR** on **firmware v1** (the 2.x app). See the note on v3 below.
- A **Linux server** that stays on, able to reach the ASIAIR over the network
  (directly or through a VPN). A mini PC or a Raspberry Pi is plenty.
- Python **3.10+** and the three dependencies in `requirements.txt`
  (`requests`, `astral`, `PyYAML`).
- **Optional**: TP-Link Kasa smart plugs (powering the rig on and off), an MQTT
  broker and Home Assistant (telemetry), a Telegram bot (notifications and
  commands), a NAS or any disk for the FITS, a Google service account (Sheets
  log), Apache or any web server (dashboard).
- A **roof or dome exposing an ASCOM Alpaca API**, if you want the roof logic.
  Without one the agent still works: treat the roof as always open and keep the
  rest of the automation.

### ⚠️ ASIAIR firmware v3

From firmware v3 onwards, port 4700 requires an **RSA authentication handshake**
using a key embedded in the app. This code is validated on **v1** and would stop
at the first command on v3. In `tools/asiair-tool/` you'll find the scripts (MIT,
from [cpius/asiair-tool](https://github.com/cpius/asiair-tool)) to extract that
key from the APK and test the handshake; porting it into `asiair_client.py` is
work I haven't done yet. **App updates cannot be undone**: turn off automatic
updates until you're ready.

---

## Quick start

```bash
git clone https://github.com/martellucci/siderisart-agent-asiair.git
cd siderisart-agent-asiair
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Then open `config.yaml` and fill in everything marked `<<<` (coordinates, ASIAIR
IP, roof endpoint, plugs, broker). Fill in the credential `.txt` files following
**[docs/CREDENTIALS.md](docs/CREDENTIALS.md)** and lock them down:

```bash
chmod 600 *.txt credentials_asiair
```

Try a dry run, which touches nothing:

```bash
python3 sfro_agent.py --config config.yaml --once --dry-run
```

When you're satisfied, install the services as described in
**[docs/INSTALL.md](docs/INSTALL.md)**.

> ### ⚠️ Don't commit your credentials
> `kasa.txt`, `telegram.txt`, `mqtt.txt` and `asiair.txt` ship **empty**, as
> templates. The moment you fill them in, git sees them as modified. Tell git to
> ignore those changes, once:
> ```bash
> git update-index --skip-worktree kasa.txt telegram.txt mqtt.txt asiair.txt
> ```
> `.gitignore` already covers `config.yaml`, `credentials_asiair`, `*.pem` and
> `gdrive_sa.json`.

---

## Documentation

| Document | Contents |
|---|---|
| **[docs/ASIAIR_PROTOCOL.md](docs/ASIAIR_PROTOCOL.md)** | **The good part.** The ASIAIR protocol as I reconstructed it, command by command: channels, methods, parameters, response shapes, and above all the **traps**, each verified on live hardware. |
| **[docs/CREDENTIALS.md](docs/CREDENTIALS.md)** | How to obtain and assemble every credential: Telegram bot, Kasa account and plug id, ASIAIR SMB, Google service account, RSA key for v3. |
| **[docs/NETWORK.md](docs/NETWORK.md)** | Reaching a remote ASIAIR: VPN on the router (not on the server), a dedicated VLAN, minimal firewall rules, and how to answer "is the VPN up?" without false negatives. |
| **[docs/INSTALL.md](docs/INSTALL.md)** | systemd services, CIFS mounts, dashboard, verification. |
| `tools/asiair-tool/RPC_METHODS.md` | RPC methods extracted from the app (third-party material, MIT). |

---

## The traps that each cost me a night

This is where the real value of the project is. Every line below is a lost
evening.

| Trap | What happens | How it's handled |
|---|---|---|
| **`value: 0` on the flat panel** | The firmware reads 0 as `state:false` and the panel **OPENS** instead of turning off. | "Closed and off" is `value:5, state:true`. Never zero on a PWM output. |
| **`is_plan_started` lies** | It stays `true` after the plan has been stopped: it does not mean "currently imaging". | "Currently imaging" requires `plan_started` **and** `capturing`. |
| **An interrupted plan won't restart** | `start_exposure` on a half-finished plan does nothing useful: it must be **reset** first. | `reset_plan()`, verified against `get_plan` (lapsed 0, left == total). |
| **Ping is not enough to power down** | The Pi answers pings while the system is already dead: cutting power then corrupts the SD card, not cutting it leaves the rig powered all day. | Two-level check: ping **and** port 4700. If the app is down but ping still answers, 90 seconds of grace, then power off anyway. |
| **Firmware floats** | You write an exposure of `8.19` s, read back `8.190001`, and an exact comparison fails. | Exposures rounded to one decimal, compared with a 5 ms tolerance. |
| **A powered-off ASIAIR is a zombie socket** | The box sends no FIN/RST when it dies: the guiding connection stays open forever and telemetry dies **silently**. | TCP keepalive plus a forced reconnect if the socket goes quiet beyond N seconds. |
| **`rsync` rc=24** | "File vanished": a FITS still being written on the ASIAIR. Treating it as fatal aborted the entire shutdown. | Tolerated as a warning: the file is picked up on the next sync. |
| **AUTO flat exposure hits the ceiling** | At gain 0 with the panel at 50%, the computed exposure clamps at 15 s and sometimes the calculation **fails outright**: the autorun never starts. | Per-gain panel brightness, so the exposure lands in the middle of the usable window. |
| **The ASIAIR piles everything together** | Every night's frames land in the same folder per type, and it isn't configurable in the app. | Sorting by date is done by the sync **at the destination**, reading the date from the filename. |
| **systemd timeout on oneshot units** | It is **disabled** by default: one cycle hung on a dead CIFS mount blocks the timer **forever**. | An explicit `TimeoutStartSec` in the unit. |

---

## Tests

Twelve test scripts, **entirely offline**: no rig, no network, no NAS. They use
fake ASIAIRs, an in-memory fake Google Sheet and temporary sandboxes, and they
cover the real failures that produced the safeguards listed above.

```bash
for t in test/test_*.py; do python3 "$t" && echo "OK $t"; done
```

They take a few seconds in total. If you touch the code, run them before letting
the agent loose on a clear night.

---

## Honest notes, before you build on this

- **The project is tailored to my setup.** Mono camera with a filter wheel,
  equatorial mount, motorised flat panel, Kasa plugs, Alpaca roof. With a
  different setup some parts won't apply and others will need rewriting.
- **It is not tested on any other rig.** It works on mine, every night, but I
  have no way to try it elsewhere.
- **The ASIAIR protocol is neither official nor documented.** It was
  reconstructed by watching the app's traffic. ZWO can change it in any update
  without telling anyone — which is exactly what happened with v3.
- **Automating a rig means being able to break it.** This code drives a mount,
  mains power and shutdowns: start with `--dry-run`, then one piece at a time,
  and watch the first few nights. The software is provided **as is, without
  warranty**: what happens to your equipment is your responsibility.
- **There is no automatic fallback on errors.** If something goes wrong during
  flats, the flow stops and **leaves the rig powered on**, with a Telegram
  warning. That's deliberate: I'd rather get up and look than let a program
  guess.

---

## Contributing

Issues and pull requests are welcome, especially about the **protocol**: new
methods, differences between firmware versions, behaviour that doesn't match
mine. If you've captured something from the app that isn't documented here, open
an issue — that's how the map in `docs/ASIAIR_PROTOCOL.md` grows.

Italian and English are both fine.

---

## License

**MIT** — see [LICENSE](LICENSE). Use it, change it, do what you like with it;
keep the attribution and don't ask me for guarantees.

`tools/asiair-tool/` contains third-party material, also MIT: see
`tools/asiair-tool/LICENSE` and `ORIGINE.md`.

---

<p align="center">
  <strong>Sideris Art</strong> · Fine Art Astrophotography<br>
  <a href="https://sideris.art/journal/">Journal &amp; newsletter</a>
</p>

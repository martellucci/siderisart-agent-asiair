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

---

## What it does

**Through the night**

- Checks that all devices are connected and that the **mount is actually pointing
  at the right coordinates** before starting anything (position gate).
- Reads **roof state** from an ASCOM Alpaca API and only operates inside
  **nautical night**, computed from the observatory's coordinates.
- Turns on the camera's anti-dew heater and the dew strap, opens the motorised
  flat panel, **starts the plan**.
- Every 3 minutes: verifies imaging is progressing and updates the session log;
  every 15 minutes it **syncs FITS** to the NAS incrementally (the fixed points —
  end of plan, weather, flats, shutdown — always sync). Automatic passes only
  look at the **last N days** of frames (`sync_module.recent_days`), so cleaning
  old files off the NAS doesn't make them download again.
- **Watches real progress**: not the "plan started" flag, which stays on even
  with the plan stopped, but the **plan's remaining work**. If it doesn't go down
  for 20 minutes, you get a Telegram alert. It is only an alert: an exposure in
  progress is never touched.
- **If the plugs are on but the ASIAIR is silent**, at the start of the night it
  cuts and restores power to the ASIAIR outlet **only**, for 30 seconds, once per
  night — this is the case of a shutdown from the app with the power left on,
  which leaves the box halted but powered and unreachable.
- **Measures dew** every 6 minutes with the roof open, to decide at dawn how long
  to wait before flats (below).
- **Weather closure is a pause, not an ending**: it stops the plan, parks, closes
  the panel — but keeps the cooler running and resets nothing. When the roof
  reopens, **the plan resumes by itself from where it left off**.

**At dawn**

- Stops the plan, parks the mount, resets the plan for the next night.
- Dew strap to full and **drying before any flat** — from 5 to 30 minutes
  depending on the dew measured through the night (below). Condensation on the
  panel ruins flats and you won't notice until it's too late.
- **Flats per (filter, gain) actually used that night**: it reads from the
  session log which filters you shot and at what gain, groups them, sets panel
  brightness per group and runs one autorun for each, with AUTO exposure.
- **Dark flats** immediately after, panel off, at exactly the exposure computed
  from the flats it just took. The cooler stays on until the last dark; only
  then does the rig rest (cooler and heaters off) while the files are copied.
- **Final sync, one pass**: up to 100 minutes, never restarted on its own. A
  dry run first counts exactly what is missing, then a single **live Telegram
  message** is edited in place every 9 minutes (silently) with a progress bar,
  files and GB against the total, speed and estimated finish — it even warns
  when the estimate runs past the time limit. The end arrives as a new message.
  If the time runs out the rig **stays on** and is **never powered down with an
  incomplete sync**: you press Rsync again and the copy picks up where it left
  off.
- `pi_shutdown`, waits until the box is **actually** dead, and only then cuts
  power at the smart plugs.

**Always**

- **Telegram bot** with a button menu: status, power-on, start plan, manual sync,
  on-demand flats/darks, shutdown — every dangerous action behind a confirmation.
  The manual sync follows **exactly the same procedure**: one pass, the same
  live message, the same texts.
- **One sync at a time**: the bot and the agent share a lock (`flock`), so two
  copies never fight over the same mount. Whoever arrives second doesn't start —
  the night pass skips silently, the post-flat sync waits, the button says
  "already running". The kernel releases the lock if the process dies.
- **Per-target, per-night view on the NAS**: next to the raw mirror, a second
  tree `target/night/light|flat|dark` with the flats and darks matching each
  target's filters and gain — built with **hardlinks**, so nothing takes space
  twice (CIFS doesn't allow symlinks). The night folder is named after the day
  in **your** time zone (`folder_timezone`), not the observatory's.
- **MQTT telemetry** to Home Assistant: ~60 entities covering guiding (RMS and
  peaks over a moving window), camera, focus, mount, storage, the box's CPU
  temperature, per-outlet power draw and the site weather station with its dew
  estimate.
- **Automatic session log**: reads FITS headers into SQLite, updates a Google
  Sheet with aggregates, writes per-night detail to CSV.
- **Self-contained HTML dashboard**: hours per target and filter, yield against
  available darkness, guiding RMS and HFR per night, histograms.

---

## Dew: measured, not guessed

The drying wait before flats used to be a blind constant. But every extra minute
of waiting is a minute of **darks taken with more ambient light**: at nautical
dawn there is less than 50 minutes to sunrise, and civil twilight comes earlier.
Hence the idea of measuring instead of guessing.

Dew forms when a surface drops below the dew point, and an unheated surface is
**not at air temperature**: it radiates to the sky and cools, the more so the
clearer the sky and the stiller the air. So:

```
margin = (temperature − dew point) − estimated radiative cooling
```

The cooling is estimated from the **sky depression** (`air temperature − sky
temperature`, which a weather station with an IR sensor reports) and from
**wind**: about 5 °C under a clear, calm sky, 3 °C if clear and windy or hazy,
1 °C under overcast — and **5 °C when the sky is unknown**, i.e. the worst case.
A margin ≤ 0 means dew is forming. Below freezing it is **frost**, not dew: the
frost point sits above the dew point the station reports, so one more degree of
caution is subtracted.

**The measurement covers the whole night, not dawn**: if the margin collapsed at
03:00, the dew is still there at 06:00. The agent samples every 6 minutes, but
**only at night and with the roof open** (with the roof closed the optics don't
see the sky and don't radiate), and keeps the minimum margin, the wet minutes,
rain and the number of readings.

At dawn the verdict picks **how long to wait**:

| Band | Wait | When |
|---|---|---|
| **DRY** | 5 min | minimum *and* current margin both comfortable, no rain, no roof closure, full coverage of the night |
| **INTERMEDIATE** | 10 min | night measured end to end and never wet, but without the margin that certifies the dry band |
| **NORMAL** | 20 min | **the blind case**: station down, data stale or too sparse, or a night with a weather closure (with the roof closed nothing is sampled and there is no way to know what happened outside) |
| **WET** | 30 min | dew actually observed, or rain |

The asymmetry is deliberate: **one hint is enough to lengthen the wait, but
shortening it takes unanimity.** The intermediate band exists because *"not
certifiable as dry"* is not the same as *"I know nothing"*: a night measured from
end to end without ever seeing dew is information, and deserves less than the 20
minutes meant for when we are blind. The physical argument points the same way:
worst-case cooling is calibrated on a surface that sees the whole sky, while
optics inside an enclosure only see part of it.

The verdict is computed **once**, when drying starts, so the countdown and the
final check read the same number and cannot diverge halfway through. All of it
can be turned off (`dew.enabled: false`), and there is an **observation mode**
(`dew.adaptive: false`) that measures and publishes over MQTT but leaves the wait
fixed: the honest way to watch the data for a few weeks before letting it change
your timings.

You need a weather station you can query — here it is the site's own, exposed on
the same Alpaca API as the roof. Without one, the agent stays on the fixed value.

---

## How it's built

```
                 ┌─────────────┐   Alpaca/HTTPS    ┌──────────────┐
                 │  roof API   │◄──────────────────┤              │
                 └─────────────┘                   │              │
                 ┌─────────────┐   TP-Link cloud   │    AGENT     │
                 │ smart plugs │◄──────────────────┤  (every 3')  │
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
| `sfro_agent.py` | The agent. A systemd **oneshot timer every 3 minutes**: no daemon that can die quietly, every cycle starts fresh by reading state from disk. Night orchestration, teardown, flat/dark flow, sync. |
| `sfro_mqtt.py` | Long-running service: MQTT/Home Assistant telemetry, listens to the ASIAIR's push events, writes guiding and autofocus JSONL sinks. |
| `sfro_telegram.py` | Long-running service: Telegram bot with button menu and confirmations. |
| `sfro_sessionlog.py` | Session log: FITS → SQLite → Google Sheet + CSV. |
| `sfro_report.py` | HTML statistics dashboard (single file, no CDN). |
| `sfro_sessioni.py` | Per-target × night view of the FITS on the NAS, with hardlinks. |
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
  log), Apache or any web server (dashboard), a weather station reachable over
  ASCOM Alpaca (adaptive pre-flat drying).
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
| **`is_plan_started` lies** | It stays `true` after the plan has been stopped: it does not mean "currently imaging". | "Currently imaging" requires `plan_started` **and** `capturing` (or the scheduled wait before the first frame). At the start of a night a stale flag no longer makes the agent adopt a stopped plan: it resets and restarts it. |
| **A watchdog also counts the time it wasn't watching** | The "the plan hasn't progressed in N minutes" alert fired **right after the roof reopened**: during a weather pause the remaining work is frozen by construction and the watchdog doesn't run, so on return it found the same figure and subtracted a timestamp from before the closure. | The watchdog measures **only the minutes it actually watched**: if there is a gap between two passes (weather pause, agent down, VPN down) it starts over. This holds for any "X hasn't changed in N minutes" alert. |
| **Plugs on, box dead** | A `shutdown` from the app with the power left on leaves the ASIAIR **powered but halted**: it answers nothing and there is no way to bring it back remotely. A night lost with nothing broken. | An automatic power cycle at the start of the night, on the ASIAIR outlet **only**, and only if the VPN is up (otherwise the silence proves nothing). One attempt per night; the mount is never powered off. |
| **An interrupted plan won't restart** | `start_exposure` on a half-finished plan does nothing useful: it must be **reset** first. | `reset_plan()`, verified against `get_plan` (lapsed 0, left == total). |
| **Ping is not enough to power down** | The Pi answers pings while the system is already dead: cutting power then corrupts the SD card, not cutting it leaves the rig powered all day. | Two-level check: ping **and** port 4700. If the app is down but ping still answers, 90 seconds of grace, then power off anyway. |
| **Firmware floats** | You write an exposure of `8.19` s, read back `8.190001`, and an exact comparison fails. | Exposures rounded to one decimal, compared with a 5 ms tolerance. |
| **A powered-off ASIAIR is a zombie socket** | The box sends no FIN/RST when it dies: the guiding connection stays open forever and telemetry dies **silently**. | TCP keepalive plus a forced reconnect if the socket goes quiet beyond N seconds. |
| **`rsync` rc=24** | "File vanished": a FITS still being written on the ASIAIR. Treating it as fatal aborted the entire shutdown. | Tolerated as a warning: the file is picked up on the next sync. |
| **A slow link looks like a hang** | At ~2 MB/s from the remote site, 12 GB of flats and darks need well over an hour. Each 30-minute pass hit its time cap, the bot reported "error" and stopped, and with no output in between it looked frozen — the server got rebooted for nothing. | Long passes, rsync output streamed live, and a progress message with the file count. Today: one 100-minute pass and a live message with percentage and estimated finish. |
| **Cleaning the mirror re-downloads everything** | rsync makes the destination match the source: delete old flats on the NAS but not on the ASIAIR, and the next passes copy them all back — five hours of old flats queued **ahead of** the night's lights. | Clean the ASIAIR first, then the NAS. Automatic passes only look at the last few days anyway (`recent_days`); the manual sync button doesn't, by design. |
| **Two syncs on the same mount** | The bot's manual sync and the agent's periodic pass can run at once: each mounts and unmounts the share, pulling it out from under the other. | A non-blocking `flock`: one sync at a time, released by the kernel if the holder dies. |
| **AUTO flat exposure hits the ceiling** | At gain 0 with the panel at 50%, the computed exposure clamps at 15 s and sometimes the calculation **fails outright**: the autorun never starts. | Per-gain panel brightness, so the exposure lands in the middle of the usable window. |
| **The ASIAIR piles everything together** | Every night's frames land in the same folder per type, and it isn't configurable in the app. | Sorting by date is done by the sync **at the destination**, reading the date from the filename. |
| **systemd timeout on oneshot units** | It is **disabled** by default: one cycle hung on a dead CIFS mount blocks the timer **forever**. | An explicit `TimeoutStartSec` in the unit. |
| **A timeout shorter than the work** | The sync pass grew to 60 minutes, but the unit still said `TimeoutStartSec=45min`. systemd killed the cycle twice mid-copy; SIGTERM ends Python **without** running `finally`, so the state was lost and every next cycle re-announced "darks done", switched off the cooler again and restarted the sync from zero — the chat filled up with overlapping messages counting from zero. | The timeout must exceed the longest pass (now 2 h for 100 minutes). State saved **before** the copy starts, SIGTERM turned into a clean exit, and a pass found already started is **never** restarted. |

---

## Tests

Twenty-one test scripts, **entirely offline**: no rig, no network, no NAS. They use
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

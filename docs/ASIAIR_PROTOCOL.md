# The ASIAIR protocol, reconstructed

***English** · [Italiano](it/PROTOCOLLO_ASIAIR.md)*

> **What this document is.** The ASIAIR protocol is neither public nor
> documented: what follows was reconstructed by watching the app's traffic and
> trying commands **on live hardware**, night after night. These are working
> notes, with the dates of each verification and the failures that produced each
> safeguard. I'm publishing them as they were, because that's the form in which
> they're actually useful.
>
> **Validated on firmware v1.** From v3 onwards port 4700 requires an RSA
> handshake: see [CREDENTIALS.md](CREDENTIALS.md).
>
> ZWO can change any of this in any update, without telling anyone. If your box
> behaves differently, please open an issue — that's how this map grows.
>
> *The Italian original is [here](it/PROTOCOLLO_ASIAIR.md) and is the version I
> keep up to date; this translation may lag behind it.*

Date: 2026-06-27. Device: **ASIAIR Pro**, firmware **13.41**
(`firmware_ver_int=1341`). Discovered actively over JSON-RPC — no app capture was
needed to get this far.

## Channel map (TCP ports)

| Port | Role | Version event |
|---|---|---|
| **4400** | **GUIDER** subsystem | `name:"ASI AIR guider"`, svr 12 |
| **4700** | **IMAGER / PLAN CONTROL** ← the one we need | `name:"ASI AIR imager"`, svr 29, **fw 13.41** |
| 4500 / 4800 / 4801 | data/image streams (binary, not JSON-RPC): **4800** = one frame per completed exposure, **4500** = guide camera stream, 4801 = image metadata | — |
| 22 (SSH) | open (the ASIAIR is a Raspberry Pi) | — |

⚠️ Lesson learned: we first aimed at 4400 (guider) and the whole autorun side was
missing. **Plan control is on 4700.**

## Access — OK

- `ping` fine; 4400/4500/4700/4800/4801/22 open; port 4700 **unauthenticated**
  (true on *our* firmware 13.41 — from roughly 43.97 onwards an RSA handshake is
  required, see the 2026-08-12 updates below).
- On 4700: `test_connection` → `"server connected!"`, `pi_is_verified` → `true`.
- JSON framing with `\r\n`, responses shaped
  `{"jsonrpc":"2.0","code":N,"result":...,"id":N}`, plus asynchronous broadcast
  events (no `id`).

## The discovery oracle

`code 103` = "method not found" · `104/108` = the method exists but the
parameters are wrong (it does not execute) · `315` mount not connected ·
`318` device not connected.

Feeding candidate method names to the box and reading these codes is how most of
this document was built.

## Confirmed methods on 4700

**Reading**: `test_connection`, `pi_is_verified`, `get_app_state` (a rich object
with `page`, `capture.progress`, …), `get_camera_state`, `list_plan`, `get_plan`,
`get_sequence` (wants an int), `get_sequence_setting`.

**Acting**: `start_exposure` (starts a single capture / preview — tested and
stopped), `start_auto_goto` (wants float ra/dec).

**Stopping**: none of the obvious names (`stop_capture`, `stop_plan`, …) exist on
4700.

## Confirmed methods on 4400 (guider)

`test_connection`, `get_app_state`, `get_setting`, `get_exposure`,
`set_exposure`, `stop_capture`, `get_connected_cameras`,
`get_focuser_state/info`, `scope_goto/sync/park/get_track_state/get_ra_dec`.

## Starting and stopping a plan — SOLVED (tested live)

> **⚠️ CORRECTION, 2026-06-29** (app capture against the production rig): the
> correct way to start is **`start_exposure ["light"]`** — *with* the `"light"`
> parameter, after `set_page ["plan"]`.

The command acts on the **current page**: from `"autosave"` it starts the
autorun, from `"preview"` it takes a single frame. With empty params the plan
does **not** start on production (it returns to idle/plan_end) — that was the
"the plan won't start" bug. An earlier 2026-06-27 test at home with no params
appeared to work, but it was context-dependent: don't rely on it.

Start confirmation: `get_enabled_plan → is_plan_started:true` (as a *transition*
— see the warning below, the flag is sticky and useless as "running right now").
**`stop_exposure` stops it** (returns to `idle`). All on channel 4700.

```
start_exposure ["light"] -> code 0; events: Sequence:start, Target:start,
  Sequence:frame_start, Exposure:start exp_us=300000000 (300s) gain=100;
  get_app_state.capture: state="expose"/"first_delay", is_working=true.
stop_exposure  -> code 0; capture returns to state="idle", is_working=false.
```

### The "no mount" block was only in the app

The ASIAIR app's UI refuses to start without a mount, but the **server starts
anyway**: with no mount it **skips the goto** and begins exposing. With the mount
connected it performs the goto on each target as expected. So the agent does not
need the app to start a plan.

### Setters (for selecting or editing plans)

`set_plan` and `set_sequence` exist. With empty params they're verified no-ops.
If you need to choose between several plans, or edit them from code, capture
their parameters with tcpdump on 4700 while doing it from the app:

```bash
sudo tcpdump -i any -A -s0 'tcp port 4700' | grep '"method"'
```

With a single already-enabled plan none of this is necessary: `start_exposure` is
enough.

### ⚠️ From cold, devices are DISCONNECTED and must be connected before starting

With no app attached, devices stay closed after boot and `start_exposure` fails
with **code 318 "device not connected"**. Connection calls, tested in production
on 2026-06-29:

| Device | Channel | Method | Notes |
|---|---|---|---|
| Main camera | 4700 | `open_camera []` | |
| Guide camera | **4400** | `set_camera_idx [1]` + `set_connected [{"camera":true}]` | `open_camera[1]` on 4700 does **not** work: guiding lives on the guider channel |
| Focuser (EAF) | 4700 | `open_focuser []` | |
| Filter wheel (EFW) | 4700 | `open_wheel []` | |
| **Mount** | **4400** | **`set_connected [{"mount":true,"async":true}]`** | taken from an app capture; verify with `get_connected [true]` → `{"mount":1,…}` |

- **code 202 = "already connected"** → treat it as success, not as an error.
- `open_camera` with a name or path → 300 "internal error"; use the **index**
  (0 = main, 1 = guide).
- **The mount lives on channel 4400** (guider), not 4700: `set_connected`,
  `get_connected`, `scope_get_info`, `scope_set_location`, `mount_scan_port`
  (→ `/dev/ttyACM0`) and `scope_set_time` are all on 4400.

Startup sequence: connect the **mount (4400)** plus camera/guide/focuser/wheel
(4700, accepting codes 0 and 202) → `set_page ["plan"]` → `start_exposure`.

### How the mount capture was done

Captured on the machine running the app, with:

```bash
tcpdump -A 'host <ASIAIR_IP> and (port 4400 or port 4700)' | grep '"method"'
```

while pressing Connect. The app sends `set_connected [{"mount":true,"async":true}]`
(4400), then `mount_scan_port`, `scope_set_time`, `scope_set_location [lat, lon]`.
You don't need SSH access to the ASIAIR — capturing on the app's own machine is
enough.

## Updates 2026-07-03 → 07-05 (real remote rig, all verified live)

### Cold connection — actually solved (2026-07-04): PRIMING

The "from cold" section above is superseded. The endless `204 "out of limit"` /
`207 "fail to operate"` responses were **not** slow USB. You have to call
**`get_connected_cameras` on a channel before connecting that channel's camera**
(on 4700 before `open_camera[id]`, on 4400 before `set_camera_idx` +
`set_connected{camera}`).

With priming, every device connects in 2–5 s; a full cold boot from mains power
to a running plan takes about 60 s. The camera id comes from matching the model
name.

A mount without power **loses its clock**: after connecting, send
`scope_set_time ["YYYY-MM-DDTHH:MM:SS","-0"]` (**two string parameters**, UTC;
other forms return 105). Location, by contrast, persists (`scope_get_info` uses
capitalised keys `Lat`/`Lon`).

> ⚠️ **Persisting is not the same as being usable.** The location survives in the
> mount, but you must still send `scope_set_location` after every connect or
> **every goto is refused**. Likewise, setting the mount's clock is not enough:
> the Pi has its own clock, and it starts at 2019. Both are covered in
> [Updates 2026-08-17](#updates-2026-08-17--every-goto-refused-on-a-cold-start),
> which is the single most expensive lesson in this document.

### Plan/autorun start and page context

`start_exposure ["light"]` acts on the **current page context**:

- `set_page ["plan"]` → starts the **plan**;
- `set_page ["autosave"]` → starts the **autorun** (flats/darks);
- from `"preview"` → takes a single frame.

The page called "autorun" **does not exist** at protocol level (code 109): it is
called **`"autosave"`**. Stopping is `stop_exposure` (plus `clear_autosave_err`,
seen in captures).

### ⚠️ `is_plan_started` does NOT mean "running right now"

`get_enabled_plan.is_plan_started` stays `true` after the plan is stopped **and**
after a reboot, which makes it useless as a safety net — on 2026-07-04 it aborted
a perfectly healthy flat autorun.

The correct way to confirm an autorun has started: after
`reset_sequence_progress`, the `left_time_sec` from `get_target_sequences` must
**change** relative to its post-reset value within ~30 s.

Confirmed live on 2026-07-25 (the first real weather pause): the stale flag made
the agent believe the plan had "already been restarted by hand" when the roof
reopened, while it was in fact **stopped** — the reopening was ignored in
silence. Fixed: "running right now" now requires `is_plan_started` **and**
`capture.is_working`. Manual restart from the interruption point does work
(frames resume from the next sequence number). `reset_plan`, on the other hand,
really does clear it: after the dawn reset `is_plan_started` reads false
(verified live).

### Editing autorun slots (`set_sequence`) — captured and validated

- `get_target_sequences` → a list of slots
  `{filter, suffix, repeat, id, enable, autoexp, gain, exp, bin, type,
  capture_index, lapsed, …}`.
- **`set_sequence` wants the COMPLETE slot back**, inside a list `[{...}]`. It
  writes both `enable` and `repeat`; `left_time_sec` updates immediately.
- **Code 224** "cannot edit sequence unless reset the progress": slots with
  `lapsed > 0` are not editable → send `reset_sequence_progress []` **before**
  editing.
- The `filter` field is a **0-based index** into `get_wheel_setting.names`
  (with `["L","R","G","B","S","H","O"]`, S = 4 and H = 5).
- During an autorun, `capture.frame_summary.complete_num/total` and
  `capture.target.seq.current_type` update within 1–2 s. The gap between the last
  flat and the first dark is also only 1–2 s, which makes it **impossible to turn
  the panel off mid-flight** without spoiling the first dark (measured). Hence
  the agent's **two-pass** flat/dark design.

### The flat panel on OF2 (`pi_output_set2`, output `port2`)

- `value:85 state:true` = flat illumination
- `value:5 state:true` = panel **closed**, light effectively off
- **NEVER `value:0`**: the firmware forces `state:false` and the panel **OPENS**
- open = `state:false` (allow ~7 s for the motor)

⚠️ **`pi_output_get2` can reply WITHOUT the `flat_panel` output.** This happened
on 2026-08-12 and had never been seen before in six weeks of logs: a response
does arrive — it is not a timeout, the exception would have said so — but the
output is missing, so the close routine doesn't know which port to write to, and
writes nothing. That day it struck at the *last* step of teardown (plan already
stopped, cooler already off, mount already homed): flats cancelled, **panel left
open**, rig left powered.

Since then the read goes through a wrapper that **retries 3 times, 3 s apart**
(`asiair.output_read_tries` / `output_read_wait_seconds`) and distinguishes
"output missing" from "call failed" in its message — previously both produced the
same text. This applies to **every** output: `set_output`, i.e. the **dew strap**,
had the identical flaw at the identical point. Covered by
`test_flat_read_retry.py`.

The root cause remains **unknown**: an isolated malformed response from the box.
Directly ruled out by experiment (2026-08-12, box powered, no session running):
responses migrating between connections (a quiet socket does not see another's
replies); concurrency on the power methods (60 parallel calls to
`pi_output_get2` and `get_power_supply` from two connections, all correct); and a
connection limit (12 simultaneous connections on 4700 plus the 2 from the
services: 44 reads, all complete, ~140 ms steady, no degradation). So the
permanent telemetry listener introduced that same day is **not** implicated.

### Power outputs — complete map (read live 2026-07-10)

`pi_output_get2 []` (4700) → an array of 4 outputs, the index being the `portN`
used by `set2`:

| Index | `type` | PWM |
|---|---|---|
| `port0` | `camera` | no |
| `port1` | `other` | no |
| `port2` | `flat_panel` | **yes** |
| `port3` | `dew_heater` | **yes** (the app's "Output 4") |

Writing means finding the output by `type` and sending
`{portN:{is_pwm, value, state:true, type}}`, with a read-back check — validated
live on `dew_heater` (code 0, consistent read-back). **The "never `value:0`" rule
applies to every PWM output.**

### Main camera temperature and cooler (verified live 2026-07-24)

`get_control_value` on 4700:

- `["Temperature"]` → value in **tenths of a degree** (384 = 38.4 °C)
- `["TargetTemp"]` → the target set in the app (type "text")
- `["CoolerOn"]` → 0/1
- `["CoolPowerPerc"]` → cooler power. Note: `"CoolerPowerPerc"` does **not**
  exist (code 109).

Turning it on: `set_control_value ["CoolerOn", 1]`, with a read-back. This drives
the pre-flat **temperature gate**: flats and darks only run once the camera is
within tolerance of the target the lights were taken at.

### Main camera gain (verified live 2026-07-24)

`get_control_value ["Gain"]` (4700) → `{name:"Gain", type:"number", value:N}`;
writing is `set_control_value ["Gain", N]` with a consistent read-back (tested
0 ↔ 100).

Autorun slots carrying `gain: -10000` ("default") use the camera's **current**
gain — so setting it governs flats and dark flats together.

### AUTO flat exposure (verified live 2026-07-24)

A flat slot with `autoexp: true` makes the ASIAIR calibrate at start (one
measurement frame) and **write the computed time into the slot's `exp` field**
(observed 1.8 → 1.62 in about 6 s; the value **persists** after the run). So you
read the result back with `get_target_sequences` at the end of the group.

⚠️ With autoexp the recomputed time can **exceed** the preset, and `left_time_sec`
can rise **above** its post-reset value — seen live on 2026-07-24 (G at gain 0,
362 → 661 s, about 22 s per frame): a naive "left < left0" check aborted a
perfectly healthy autorun. The positive-confirmation check therefore accepts *any
change* in `left` relative to `left0` (only an autorun touches the sequence; from
the wrong page, a preview leaves `left` untouched), or "at least one frame
completed" (`frame_summary.complete_num`).

⚠️ **The AUTO calculation has a ceiling of roughly 15 s** (observed live). On
2026-07-25, R/G/B flats at **gain 0 with the panel at 50%** all came out at
**exactly 15.0 s** (clamped). On 2026-07-26, under identical conditions, the
calculation on B **failed outright** (the app reported an exposure-calculation
error and the sequence never advanced: left stayed 664/664 s until a watchdog
stopped the capture). Cross-check: manual B flats at ~100% panel came out at
7.1 s, which scales to about 14.2 s at 50% — right up against the ceiling.

The countermeasure in production is a **per-gain brightness map**: at gain 0 the
RGB filters use 67% instead of 50%. Live measurements at 65% gave R 9.1 s,
B 7.1 s, G 3.1 s; at 67% the expected values are around R 7 s, B 5.5 s, G 2.5 s —
comfortably inside the 1–15 s window. Note that R has the highest demand and the
panel is not linear across the day: if R at gain 0 ever hits the ceiling again,
raise its own entry (the map is per filter).

### Main camera anti-dew heater (verified live 2026-07-19)

`get_control_value ["AntiDewHeater"]` (4700) → `{name, type:"number", value:0|1}`.
The form `[{"name": …}]` returns code 107 "expected object param". Writing:
`set_control_value ["AntiDewHeater", 1]` — the same call that the shutdown path
uses with 0.

The agent reads it, turns it on only if it's off, and verifies with a read-back.
This runs in the pre-start phase: on 2026-07-19 it was found switched off and
about 10 frames were ruined by morning humidity.

### Resetting a PLAN

An interrupted plan will **not** restart with `start_exposure` until it has been
reset (seen live on the night of 7–8 July). The app's own command had never been
captured, so the agent tries `reset_sequence_progress` on `set_page ["plan"]`,
then `reset_plan_progress` / `reset_plan` / `clear_plan_progress` (code 103 being
harmless), and **verifies** by re-reading `get_plan` (enabled targets: lapsed 0,
left == total).

✅ **CONFIRMED (2026-08-12, external source)**: the real name is **`reset_plan`**,
one of the three already being tried. It is used in production by
[tankhardrive/AsiAirController](https://github.com/tankhardrive/AsiAirController),
alongside `list_plan`, `set_plan`, `import_plan` and `get_target_sequences`.

### Shutdown and the Pi's clock

`pi_shutdown []` → result 0, box dead in about 15 s.

⚠️ **Ping is not proof that it's off**: the kernel answers, and the Pi can still
be pinging with the app already down. The real test is that **port 4700 no longer
accepts connections**.

`pi_set_time [{time_zone, hour, min, sec, day, year, mon}]` is sent by the app on
opening.

## Updates 2026-08-12 — external sources plus live checks

Two public repositories were examined (cloned and read at source level, not just
the README): [cpius/asiair-tool](https://github.com/cpius/asiair-tool) (Python
toolkit, methods extracted from the 3.0.0 APK and validated on **firmware
43.97**) and
[StefanDorresteijn/asiair-dashboard](https://github.com/StefanDorresteijn/asiair-dashboard)
(read-only web dashboard in Node; its `docs/PROTOCOL.md` is the valuable part).

Neither solves cold device connection: the dashboard sends no commands, and
asiair-tool **doesn't know about priming** (see 2026-07-04). That part is ours.

### ⚠️ From firmware ~43.97, port 4700 is closed behind an RSA handshake

On newer firmware, a freshly connected 4700 socket answers **only**
`test_connection` and `get_verify_str`, and **silently ignores** everything else
until the client authenticates:

```
get_verify_str            -> {"str": "<challenge>"}
RSA PKCS#1 v1.5 signature over SHA-1 of the challenge, base64-encoded
verify_client [signature, challenge]
pi_is_verified            -> true   (channel unlocked, per connection)
```

The private key lives inside the app's APK (`classes6.dex`); asiair-tool has
`extract_key.py` to pull it out and `handshake.py` for the procedure.
**Port 4400 (mount and guiding) has no handshake** and stays accessible.

**We are not affected today** (verified live on 2026-08-12, mid-session):
`pi_is_verified` → `true` without having signed anything, and `get_svr_version` →
`103 method not found`, i.e. older firmware (the `Version` event confirms
**13.41**, svr_ver_int 29).

➡️ **An ASIAIR firmware update would disable the agent** — not the telemetry, the
commands themselves. Don't update without implementing the handshake first.

### Port 4800 — how image data is framed

Decoded by tankhardrive. We don't need it (images arrive over rsync/SMB), but the
reverse engineering exists: a separate TCP connection, whose response is a **JSON
header followed by a ZIP** containing a `raw_data` entry; the end of the transfer
is recognised by the end-of-central-directory marker **`PK\x05\x06`**. Then you
extract and debayer. The request method is `get_current_img` (4700). The other
repository's `PROTOCOL.md` listed this stream as undecoded.

### When the mount won't connect: the fallback chain (4400)

If `mount_scan_port` doesn't find the mount, the app has a longer path that we
don't use (names taken from the APK's `MountGateway`, validated on fw 43.97):

```
get_mount_list                  (driver list; index 1 = ZWO AM3/AM5/AM7)
  → select_mount_list_index [idx]
  → select_serial_dev [dev]
  → scope_set_connection_mode / scope_set_connection_para
  → get_mount_index                  (read the selection back)
  → get_connected_mount_info         -> {model, fw_ver, sn, ble_name}
```

Worth keeping as a **plan B**, not as a replacement for priming.

Also confirmed: on the AM5N, **`scope_park` is the app's "Go Home"** (no
parameters).

### PUSH events on 4700 — they work on our firmware too

Verified by opening a socket on 4700 and **sending nothing** (2026-08-12, plan
running): the ASIAIR pushes events without an `id` of its own accord. Observed
live:

| Event | Useful content |
|---|---|
| `Version` | `firmware_ver_string` (13.41), `svr_ver_int` |
| `PiStatus` | `temp`, **`is_undervolt`**, **`is_over_current`**, `is_overtemp` |
| `Exposure` | `state` start/downloading/complete, `page`, `exp_us`, `gain` |
| `SaveImage` | `start`, then `complete` with `filename` + `fullname` |
| `Sequence` | `progress.cur_plan {total, lapse}`, `cur_target.target_name`, `frame_type` |
| `PlateSolve` · `Annotate` · `Temperature` | solver / annotation / temperature state |

⚠️ The shape of `Sequence` in the other project's PROTOCOL.md
(`frame`/`total_frame`) is **not ours**: they are on fw 43.97, we are on 13.41.
Read their documentation; don't copy their code.

### `get_power_supply` — volts and amps per rail (4700, read live 2026-08-12)

`get_power_supply []` → `[[V, A], …]`, one pair per output. On a real rig:

```
[[12.09,0.72],[12.18,0.16],[0.02,0.0],[12.21,0.15],[12.24,1.94]]   (~35 W)
```

This tells you whether an output is **actually drawing current** (the dew strap,
for instance), which `pi_output_get2` alone cannot distinguish.
`get_disk_volume` → `{totalMB, freeMB}`.

---

## Updates 2026-08-17 — every goto refused on a cold start

The first night the agent ran a genuinely unattended cold start, the plan
produced **not a single frame**: 3162 goto attempts refused over 2h15m. Both
causes were found and fixed live the same night, with two cold-start
verifications. If you are automating an ASIAIR without the app, this section is
probably the reason you are reading this document.

### The symptom, and the error you actually want

The autorun log only ever says `[AutoCenter|End] Mount slews failed`, repeated
every ~2.5 s. On the wire the error is far more useful — but it arrives as an
**event**, not as the reply to the call, so a client that only reads replies
never sees it:

```json
{"Event":"ScopeGoto","state":"fail","error":"internal error",
 "code":300,"lapse_ms":41,"route":[]}
```

`scope_goto` returns **code 0** — the command is accepted — and the mount does
not move. The failure lands in ~41 ms on 4400, and in ~13 ms on 4700
(`Event AutoGoto`, the path the plan's AutoCenter takes). The tell is
**`route: []`**: the route planner could not compute a path.

### Cause 1 — the Pi's clock restarts at 2019 on every boot

The ASIAIR has **no RTC and does not persist the time**. It wakes up at
**2019-02-14 11:12** every single time, from mains power and after `pi_reboot`
alike. With no internet at the site it stays there, and a date seven years off
makes sidereal time meaningless, so the mount refuses to slew.

- Set it with **`pi_set_time`** (4700), params
  `[{year, mon, day, hour, min, sec, time_zone}]` → code 0. Read it back with
  **`pi_get_time`**.
- ⚠️ `scope_set_time` on 4400 is a **different clock** — the mount's. Setting one
  does nothing for the other.
- ⚠️ Do it **before connecting any device**. Fixing the clock afterwards does not
  heal an already-running process: in our incident the app corrected the time and
  goto still failed, and only a reboot cleared it.

### Cause 2 — `scope_set_location` after every connect

Without a location the planner cannot build a route and returns `route: []`.
The trap is that the location **persists in the mount** and `scope_get_info`
reads it back correctly — so everything looks fine. It still has to be written
into the session after each connect.

Single-variable proof: three goto attempts failing at 41 ms → one
`scope_set_location [lat, lon]` → the next goto slews and **completes in 583 ms**.
Nothing else changed.

**The app sends both time and location on every connect.** That is why a rig can
work for months and then fail the first night nobody opens the app.

### The order that works

1. services up (`test_connection` on both channels)
2. **Pi clock**: `pi_get_time`, correct with `pi_set_time` if it is off, read back
   to confirm. Do not connect anything until this is right
3. devices, with the camera priming described above
4. `scope_set_time`, **verified** with `scope_get_time` — do not fire and forget
5. read the position (this is also your "is the rig where I think it is?" check),
   then `scope_set_location`

Verified from a cold start with no reboot and no manual intervention: 35 s to
ping, 28 s for the full connect, and the mount on target 86 s after power-on.

### Methods worth knowing

| Method | Channel | Notes |
|---|---|---|
| `pi_get_time` / `pi_set_time` | 4700 | the Pi's clock; `pi_set_time` takes its object **in a list** |
| `scope_get_time` | 4400 | reads the mount clock back → `["2026-8-17T5:51:47","1"]`, element 0 is UTC |
| `get_user_location` | **4400** | returns `[lon, lat]`. Does **not** exist on 4700 |
| `scope_set_track_state [bool]` / `scope_get_track_state` | 4400 | tracking on/off |
| `pi_reboot` | 4700 | reboots; ⚠️ the clock goes back to 2019 |

**Do not exist** (code 103): `scope_unpark`, `scope_home` / `scope_go_home` /
`scope_search_home`, `scope_set_park_type`, `set_user_location` on 4700,
`scope_get_utc_time`. The only parking command is `scope_park` — and ⚠️ it
**executes with any parameters**, so you cannot probe it harmlessly.

### What it was not

Ruled out by experiment, in case you go down the same paths: mount parked
(repeated `scope_park`: no effect), tracking disabled (enabling it: no effect),
the `time_offset` field (`"-0"` and `"1"` both tried), a specific target
(different coordinates failed identically), and `is_home_succeed: false` (still
false once goto works). The serial link was healthy throughout — the mount
answered tracking and park commands and reported live data the entire time.

### A watchdog is worth more than it looks

During the failure both `is_plan_started` and `capture.is_working` were **true**:
by every flag the ASIAIR exposes, the plan was running. Nothing was wrong,
except that no photons were being collected. The only honest measure of progress
is the plan's **remaining time** from `get_plan`: if it stops decreasing for
longer than one exposure plus meridian flip and autofocus, something is stuck.
Alert, do not intervene — an imaging run in progress should never be touched by
automation.

## A note on the probe scripts

The original notes reference a handful of small exploration scripts — a status
snapshot, a getter enumerator built on the `103` oracle, a guarded sweep of
candidate start verbs, a port identifier. They are **not included in this
repository**: they were throwaway tools pointed at a specific box, and the
findings they produced are all written up above. Rebuilding them from
`asiair_client.py` is an afternoon's work if you want to explore your own
firmware.

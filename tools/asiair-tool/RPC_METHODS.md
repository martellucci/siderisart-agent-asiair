# ASIAIR 4700 RPC — real method names

Extracted from the ASIAIR Android app (`com.zwoasi.asiair` 3.0.0, `classes6.dex`)
on 2026-08-09, then validated live against firmware 43.97. These are the names
the app sends over the port-4700 JSON-RPC channel.

## ✅ The mount is on a DIFFERENT PORT: 4400, unauthenticated

The thing that cost the most time: **mount and guiding commands are not on 4700
at all.** The app builds its `MountGateway` on the `AirGuide4400Gateway`
(`ServiceInitRepository`: `mountGateway = new MountGateway(airGuide4400Gateway)`),
so `scope_*` / `get_mount_*` go to **TCP 4400**. That port answers
`test_connection` but has **no RSA handshake** (`get_verify_str` → 103 there), so
mount control needs **no key** — just connect to 4400 and call. Every mount
method returning `103` on 4700 was simply the wrong port, not an auth or session
gate. (An earlier note here claimed session-scoped access-gating — that was
wrong; it's a port split.) `mount.py` uses 4400; `air_rpc.py --key` is 4700 only.

| Port | Auth | Serves |
|---|---|---|
| 4700 | RSA handshake (`--key`) | camera, focuser, wheel, solve, plan, stack, polar, settings, telemetry |
| 4400 | none | **mount (`scope_*`) + guiding (unprefixed guide methods)** |

Live-validated on 4400 (firmware 43.97 / AM5N fw 1.8.6, 2026-08-09):
`get_connected_mount_info`→`{"model":"ZWO AM5N",...}`, `get_mount_list`→driver
list (index 1 = ZWO AM3/AM5/AM7), `scope_get_ra_dec`→`[ra_h, dec_d, sidereal]`,
`scope_get_info`→full state, `scope_get_cap`→capability list.

Param shapes below marked ✓ are confirmed from the app's `MountGateway`.

Why this was needed: the seestar_alp `scope_*` / `iscope_*` names all return
`103 method-not-found` on ASIAIR, and the mount does **not** use the
`open_<device>` connect path (that's camera/focuser/wheel only). The mount has
its own vocabulary and connects via `set_mount`.

All mount methods below are on **port 4400** (no auth). Coordinates: **RA in
hours, Dec/Alt/Az in degrees.**

## Mount connect / select (4400)

| Method | Params | Purpose |
|---|---|---|
| `mount_scan_port` | — | Scan serial ports for a mount |
| `get_mount_list` | — ✓ | Driver list; the index maps to a mount model |
| `select_mount_list_index` | `[index]` | Pick the mount driver (index 1 = ZWO AM3/AM5/AM7) |
| `select_serial_dev` | `[dev]` | Choose the serial device |
| `scope_set_connection_mode` / `scope_set_connection_para` | mode/param | Set connection mode (USB/serial/BLE) |
| `get_mount_index` | — ✓ | Currently-selected driver index |
| `get_connected_mount_info` | — ✓ | `{model, fw_ver, sn, ble_name}` |
| `scope_set_mount_info` | | Push mount config |

## Pointing — read (4400)

| Method | Returns |
|---|---|
| `scope_get_ra_dec` ✓ | `[ra_h, dec_deg, sidereal_h]` |
| `scope_get_info` ✓ | Full state: RA/Dec/Az/Alt/tracking/park/slew_rate/voltage/caps/… |
| `scope_get_horiz_coord` ✓ | `[alt_deg, az_deg]` |
| `scope_get_track_state` ✓ | bool |
| `scope_get_cap` ✓ | capability strings (`goto`, `sync`, `park`, `move`, …) |
| `scope_get_slew_rate` / `scope_get_track_mode` / `scope_get_guide_rate` | rate/mode |

## Goto / slew / sync / park (4400 — **moves hardware**)

| Method | Params ✓ | Purpose |
|---|---|---|
| `scope_goto` | `[ra_h, dec_deg]` | Slew to coordinates |
| `scope_sync` | `[ra_h, dec_deg]` | Sync pointing model |
| `scope_set_track_state` | `[bool]` | Sidereal tracking on/off |
| `scope_move` | `[dir]` or `[dir, speed]` | Directional slew |
| `scope_move_left_by_angle` | `[obj]` | Slew by angle |
| `scope_park` | — | Park |
| `scope_abort_slew` | — | Stop a slew / unpark move |
| `scope_set_track_mode` / `scope_set_slew_rate` | `[index]` | Track mode / slew rate (list index) |
| `scope_set_guide_rate` | `[rate]` | Pulse-guide rate, a **float** ×sidereal (e.g. `0.5`) |

## Solve-and-center (these ARE on 4700, main channel)

`start_auto_goto` (`[float,…]`) / `start_auto_goto_pixel` / `stop_auto_goto` —
plate-solve-and-center, orchestrated from 4700 using the mount underneath.

## Plate solve

`start_solve` · `stop_solve` · `get_solve_result` · `get_last_solve_result` · `set_solved`

## Guiding (4400 — unprefixed guide methods)

Guiding shares the 4400 channel with the mount but the guide methods are
**unprefixed** (they'd collide with 4700 names, but it's a separate service).
Names + params from the app's `GuideCameraGateway`, live-validated 2026‑08‑09.

**Entering the guide tab / opening the sensor.** There is no `set_page("guide")`
— the guide tab (`GuiderFragment`) talks to 4400 directly. To bring the guide
camera online so `get_camera_info` / `get_exposure` / `get_gain` return real
values instead of `318`:

```
set_camera_idx([<id>])             # id from get_connected_cameras (e.g. 0)
set_connected([{"camera": true}])  # connect the sensor (Camera bean {camera:bool})
loop()                             # optional — start streaming; frames go to TCP 4500
```

`set_connected([{"camera": false}])` disconnects it. **Reading the settings needs
only the connect** — `loop` (live frames) additionally needs a consumer on the
guide image stream, **TCP 4500** (`GuideImageSocket`), and returns
`303 could not start looping` if the sensor isn't connected first. (An earlier
revision of this doc claimed the sensor only opens with the app's engine running
— that was wrong; the `set_connected` object shape was.)

### Camera / session
| Method | Params | Purpose |
|---|---|---|
| `get_connected_cameras` | — ✓ | List guide cameras `[{name,id,path}]` |
| `set_camera_idx` | `[index]` ✓ | Select the guide camera (id from the list above) |
| `set_connected` | `[{"camera": bool}]` ✓ | Connect/disconnect the guide sensor (`Camera` bean); the mount side is `[{... mount ...}]` |
| `get_connected` | — ✓ | `{camera:{name,path}, mount, mount_name, …}` (the `camera` field appears once connected) |
| `get_camera_info` / `get_camera_binning` | — ✓ | `{full_size:[w,h]}` / `{bin,max_bin}` once connected (else `318`) |
| `get_exposure` / `set_exposure` | — / `[ms]` | Guide exposure, ms (reads real once connected) |
| `get_gain` / `set_gain` / `get_gain_segment` | — / `[gain]` / — | Guide-camera gain (`{min,max,val}`) |
| `loop` / `stop_capture` | — ✓ | Start / stop streaming (frames on TCP **4500**); `loop` → `303` if the sensor isn't connected |
| `guide` | **bare** `{settle-obj}` | Start calibration + guiding |
| `get_app_state` | — ✓ | `Idle`/`Looping`/`Selected`/`Calibrating`/`Guiding`/`Paused`/`LostLock`/`Stopped` |

### Algorithm / tuning (the "am I on defaults?" params)
| Method | Params ✓ | Values |
|---|---|---|
| `get_algo_param` | `[axis, key]` | axis `"ra"`/`"dec"`, key `"aggression"`/`"period"`. A single arg → `105` |
| `set_algo_param` | `[axis, key, value]` | e.g. `["dec","aggression",0.7]` (aggression 0–1) |
| `get_dec_guide_mode` / `set_dec_guide_mode` | — / `[mode]` | `"Auto"`/`"North"`/`"South"`/`"Off"` |
| `get_search_region` / `set_search_region` | — / `[px]` | Star search box, px |
| `get_lock_position` / `set_lock_position` | — / `[x, y, lock]` | Guide lock position |
| `get_setting` / `set_setting` | — / `[obj]` | Guide settings blob (observed empty `{}` even when connected; likely populates only during guiding) |
| `get_beta_setting` / `set_beta_setting` | — / **bare** `{obj}` | Holds `disable_meridian_limit`; setter takes a **bare** object (list-wrapped → `107`) |

### Calibration / darks
| Method | Params | Purpose |
|---|---|---|
| `get_calibrated` / `clear_calibration` | — | Calibration state / clear |
| `get_auto_load_calibration` / `set_auto_load_calibration` | — / `[bool]` | Auto-load stored calibration |
| `get_flip_state` / `flip_calibrate` | — | Meridian-flip calibration |
| `start_create_dark` / `stop_create_dark` / `get_dark_info` | — | Guide dark library |
| `get_ra_dec_history` | — | Guide graph history (`321` when empty) |

**Param-shape gotchas on 4400** (they are not uniform): `scope_*` mount methods
and `set_algo_param` take a **list** (`["dec","aggression",0.7]`); the guide
config setters `set_beta_setting`/`guide` take a **bare object** (list-wrapping
them returns `107`), while `set_connected` takes a **list-wrapped** `Camera`/`Mount`
bean (`[{"camera": true}]`).
The mount's pulse-guide rate is `scope_set_guide_rate` — a `scope_*` mount
method taking a **float** (e.g. `0.5`), not one of these guide methods.
`get_dither`/`set_dither` live on **4700**, not here.

## Polar alignment

`start_polar_align` · `stop_polar_align` · `get_polar_align_image` ·
`set_polar_align_image` · `get_polar_axis`

## Also confirmed live (firmware 43.97), for reference

Device-open (global): `open_camera` / `close_camera`, `open_focuser` /
`close_focuser`, `open_wheel` / `close_wheel`. Reads: `get_device_state`,
`get_camera_state`, `get_camera_info`, `get_focuser_state/info/position/setting`,
`get_wheel_state/setting/position`, `get_control_value`, `get_controls`,
`get_disk_volume`, `get_image_save_path`, `get_img_name_field`,
`get_stack_info/setting`, `get_sequence_setting`, `get_plan`, `get_focal_length`,
`get_app_setting`, `get_setting`, `get_test_setting`, `get_svr_version`,
`pi_get_info`, `pi_station_state`. Exposure: `set_exposure`, `start_exposure`,
`stop_exposure`.

## Extraction recipe

```bash
cd ASIAIR_3.0.0_APKPure/base-apk           # unpacked xapk → base apk
grep -aoE '[a-z][a-z0-9_]{3,40}' classes6.dex | sort -u   # all string tokens
# classes6.dex is the one containing test_connection / verify_client
```

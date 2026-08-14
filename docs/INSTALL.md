# Installation

How to put the agent into production on a Linux server that stays on. Tested on
Debian/Ubuntu; on other distributions only the package names change.

***English** · [Italiano](it/INSTALLAZIONE.md)*

> Before any of this, get the agent working **by hand** with `--dry-run`.
> Installing the services is the last step, not the first.

---

## 1. System and dependencies

```bash
apt install python3 python3-pip rsync cifs-utils
pip install -r requirements.txt
```

The Python dependencies are three (`requests`, `astral`, `PyYAML`). On top of
those:

- `gspread` and `google-auth` **only** if you want the Google Sheets log;
- `paho-mqtt` **only** if you want MQTT telemetry.

```bash
pip install gspread google-auth paho-mqtt     # optional
```

## 2. Where things go

The code can live anywhere; these instructions use `/opt/sfro-agent`, which is
also the path written into the systemd units.

```bash
mkdir -p /opt/sfro-agent /var/lib/sfro-agent
cp -r *.py config.yaml *.txt logo.png /opt/sfro-agent/
chmod 600 /opt/sfro-agent/*.txt /opt/sfro-agent/credentials_asiair
```

| Path | Contents |
|---|---|
| `/opt/sfro-agent` | code, `config.yaml`, credentials |
| `/var/lib/sfro-agent` | persistent state (`state.json`), session DB, guiding and autofocus sinks |
| `/mnt/...` | mount points for the ASIAIR share and the NAS |
| `/var/www/html/...` | the generated HTML dashboard |

Credentials are looked up **in the script's own directory**, so they must sit
next to the `.py` files.

## 3. Mounts

**The ASIAIR image share** — the agent mounts and unmounts it itself on every
sync, using `sync_module.mount_point` and `credentials_file`. All you need is the
directory:

```bash
mkdir -p /mnt/asiair
```

**The FITS destination** (NAS or local disk) — this one must already be mounted
and stable. If it's a NAS over CIFS, put it in `/etc/fstab`:

```
//NAS_IP/share  /mnt/astronomia  cifs  credentials=/root/.nas-cred,uid=0,gid=0,_netdev  0  0
```

> The NAS password belongs **only** in the fstab credentials file, and must never
> end up in the repository or in a backup.

## 4. systemd services

The `systemd/` directory contains four ready-made units:

| Unit | Type | What it does |
|---|---|---|
| `sfro-agent.service` + `.timer` | oneshot, every 5 min | the agent |
| `sfro-mqtt.service` | long-running | MQTT telemetry |
| `sfro-telegram.service` | long-running | Telegram bot |

```bash
cp systemd/*.service systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sfro-agent.timer sfro-mqtt.service sfro-telegram.service
```

The two long-running services are optional: if you don't use MQTT or Telegram,
don't enable them.

> ⚠️ **`TimeoutStartSec` in the agent's unit is not decorative.** For
> `Type=oneshot` services systemd disables the timeout *by default*: a hung cycle
> — on a dead CIFS mount in D-state, for instance — would block the timer
> **forever**, and the agent would silently stop running with nobody noticing.
> The value is sized for the worst case (final sync plus teardown).

## 5. Dashboard (optional)

The dashboard is a **single self-contained HTML file**: no CDN, no external
JavaScript, no database to query. Any web server will serve it.

```bash
apt install apache2
mkdir -p /var/www/html/sfro/statistiche
python3 /opt/sfro-agent/sfro_sessionlog.py --config /opt/sfro-agent/config.yaml --report
```

You'll find it at `http://<server>/sfro/statistiche`. It regenerates itself on
every session-log push that brought in new frames, and at the end of each night.
The output path and the URL used in Telegram messages are set in the `report:`
block of the config.

## 6. Verify

```bash
# one cycle, harmless: no plug commands, rsync in dry-run
python3 /opt/sfro-agent/sfro_agent.py --config /opt/sfro-agent/config.yaml --once --dry-run

# current state and decision, without doing anything
python3 /opt/sfro-agent/sfro_agent.py --config /opt/sfro-agent/config.yaml --status

# list the Kasa plugs visible to the account (to find device_id)
python3 /opt/sfro-agent/sfro_agent.py --config /opt/sfro-agent/config.yaml --discover

# live logs
journalctl -u sfro-agent.service -f
```

The first genuinely useful `--status` is **in the evening, with the roof open**:
that's when you can see whether the agent recognises nautical night, the devices
and the plan.

## 7. Updating

```bash
git pull
cp *.py /opt/sfro-agent/
systemctl restart sfro-mqtt sfro-telegram   # only if you touched those two files
# the agent is oneshot: it reloads itself on the next timer tick
```

---

## What to back up, if you care

These are the files that don't regenerate themselves:

- `config.yaml` and every credential (`*.txt`, `credentials_asiair`,
  `gdrive_sa.json`, `*.pem`) — **these contain private keys**;
- `/var/lib/sfro-agent/sessions.db`, the log of all your nights;
- `/var/lib/sfro-agent/state.json`.

These regenerate on their own and need no backup: the HTML dashboard (rebuilt by
`sfro_report.py` from the DB), the per-night detail CSVs
(`sfro_sessionlog.py --csv`), and the guiding and autofocus JSONL sinks.

Keep the code archive **separate** from the secrets archive: the first can be
shared, the second never.

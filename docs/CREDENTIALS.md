# Credentials — what you need and where to get it

***English** · [Italiano](it/CREDENZIALI.md)*

There are **no credentials in this repository**: the `.txt` files you find are
empty templates, and the files that hold actual keys aren't published at all.
This page explains, one by one, how to assemble them.

> **Rule of thumb**: every file on this page should be `chmod 600` and **must
> never be committed**. See [Don't commit them by accident](#dont-commit-them-by-accident)
> at the bottom.

| File | Needed for | In the repo? |
|---|---|---|
| `config.yaml` | all configuration | no, you create it from `config.example.yaml` |
| `telegram.txt` | notifications and bot | yes, **empty** |
| `kasa.txt` | TP-Link smart plugs | yes, **empty** |
| `mqtt.txt` | MQTT broker | yes, **empty** |
| `asiair.txt` | command-line tools | yes, **empty** |
| `credentials_asiair` | SMB mount of the image share | **no**, you create it |
| `gdrive_sa.json` | Google Sheets session log | **no**, you download it from Google |
| `asiair_key.pem` | ASIAIR firmware v3 handshake | **no**, you extract it from the app |

---

## `telegram.txt` — bot and notifications

1. On Telegram, message **[@BotFather](https://t.me/BotFather)**, send `/newbot`,
   pick a name and a username. It replies with the **token**, in the form
   `<number>:<letters and digits>`.
2. Open a chat with your new bot and send it any message — without that, the bot
   cannot message you first.
3. For the **chat id**, open in a browser:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and look for
   `"chat":{"id":...}`. Group ids are negative.
4. `thread_id` is only needed if you post inside a *topic* of a supergroup.

```ini
bot_token=THE_TOKEN_BOTFATHER_GAVE_YOU
chat_id=123456789
# thread_id=42
```

The bot replies **only** to the configured chat id; any other sender is ignored
without a response. That is the only access control there is, so don't share the
token.

---

## `kasa.txt` — TP-Link smart plugs

The agent powers the rig on and off through the **TP-Link cloud**, not over the
local network — so it keeps working even when the VPN is down.

```ini
username=YOUR_KASA_EMAIL
password=YOUR_PASSWORD
```

> ⚠️ **The account must not have 2FA enabled**: the cloud API doesn't handle it
> and login fails. It's worth creating a **dedicated Kasa account** for the
> automation and sharing the plug with it, rather than using your main account.

### Finding the plug's `device_id`

You need it in the config, under `kasa.device_id`. If you use a **power strip**
(such as the KP303), the id is the id of the *strip*, while individual outlets
are named in the `kasa.outlets` list.

With the agent already configured:

```bash
python3 sfro_agent.py --config config.yaml --discover
```

Alternatively any Kasa library (`python-kasa`) will show it, or you can read the
`getDeviceList` response from the cloud API.

---

## `mqtt.txt` — broker

Only needed if you want telemetry in Home Assistant.

```ini
username=MQTT_USER
password=MQTT_PASSWORD
```

If your broker accepts anonymous connections, leave the fields empty or delete
the file. Ready-made sensor definitions for Home Assistant are in
`homeassistant/sfro_homeassistant.yaml`.

---

## `asiair.txt` — control connection

Used by the command-line tools built on `asiair_client.py`. On firmware v1 the
ASIAIR's TCP channels require **no authentication**: being on the same network
(or inside the VPN) is enough.

```ini
host=198.51.100.20
port=4700
guider_port=4400
```

- **4700** — imager/plan channel: camera, focuser, filter wheel, autorun.
- **4400** — guider/mount channel.

---

## `credentials_asiair` — SMB mount of the image share

**Not in the repo**: create it yourself. It's an ordinary CIFS credentials file,
the kind the Linux kernel expects for `mount -t cifs -o credentials=...`.

```bash
cat > credentials_asiair <<'EOF'
username=<ASIAIR SMB user>
password=<ASIAIR SMB password>
EOF
chmod 600 credentials_asiair
```

- These are the **ASIAIR's** credentials, not your Kasa account and not your NAS.
- You'll find (and can change) them in the ASIAIR app, under network/SMB settings.
- The image share is normally called **`TF Images`**; the name lives in the
  config, under `sync_module.smb_share_name`.

Test it by hand before handing it to the agent:

```bash
mkdir -p /mnt/asiair
mount -t cifs "//198.51.100.20/TF Images" /mnt/asiair \
      -o credentials=/path/credentials_asiair,ro,vers=3.0
ls /mnt/asiair
umount /mnt/asiair
```

---

## `gdrive_sa.json` — Google Sheets session log

**Not in the repo**, and it must never be: it is a private key.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create
   (or pick) a project.
2. Enable the **Google Sheets API** and the **Google Drive API**.
3. *IAM & Admin → Service Accounts → Create service account.* Any name, no
   particular role.
4. On the account you just created: *Keys → Add key → Create new key → JSON*.
   Download the file and place it next to the agent as `gdrive_sa.json`.
5. Open the JSON and copy the value of **`client_email`** (it ends in
   `.iam.gserviceaccount.com`).
6. Create your Google Sheet and **share it with that email, with edit rights**,
   exactly as you would with a person. Skip this and the log fails with a
   permission error.
7. The sheet `id` is the long section of the URL between `/d/` and `/edit`; put
   it in the config under `session_log.sheet_id`.

```bash
chmod 600 gdrive_sa.json
```

> The session log is **optional**: leave `sheet_id` empty and the agent skips
> the Sheets part entirely, still writing SQLite, CSV and the dashboard.

---

## `asiair_key.pem` — the firmware v3 handshake

**Not in the repo and never will be**: it is material extracted from the ZWO app,
which I have no right to redistribute. If you need it, extract it from your own
copy of the app.

From firmware **v3** onwards, port 4700 accepts no commands until the client
completes an **RSA challenge handshake**: the box sends a string
(`get_verify_str`), the client signs it with a private key and sends it back
(`verify_client`), then `pi_is_verified` confirms. That key is embedded in the
app, in the clear, inside the native library `libopenssllib.so`.

```bash
# 1. get hold of the app package you are already licensed to use (.apk/.xapk)
# 2. extract the key (it reads a file; it downloads and installs nothing)
python3 tools/asiair-tool/extract_key.py ASIAIR_3.0.0.xapk -o asiair_key.pem

# 3. test the handshake in isolation against your box
python3 tools/asiair-tool/handshake.py --host 198.51.100.20 --key asiair_key.pem

chmod 600 asiair_key.pem
```

> **Current status**: `asiair_client.py` **does not implement the handshake yet**,
> so this code works on **firmware v1** and not on v3. The scripts in
> `tools/asiair-tool/` (third-party, MIT) are there to prepare that migration and
> to check that the key is the right one. Until the handshake is ported into the
> client, **do not update the app**: the update cannot be rolled back.
>
> Legal note: extracting a key from an app you are licensed to use, in order to
> make a device you own talk to your own software, is textbook interoperability
> (in the US, the DMCA §1201(f) exemption; in the EU, Directive 2009/24/EC
> art. 6). Redistributing the key is a different matter — which is why it isn't
> here.

---

## Don't commit them by accident

`.gitignore` already covers the files that aren't in the repo (`config.yaml`,
`credentials_asiair`, `*.pem`, `gdrive_sa.json`, `*.conf`). The four `.txt`
files, however, **are tracked**, because they serve as templates: the moment you
fill them in, git sees them as modified and sooner or later they end up in a
commit.

Tell git once, right after cloning:

```bash
git update-index --skip-worktree kasa.txt telegram.txt mqtt.txt asiair.txt
```

To start seeing them again (to update the templates, for instance):

```bash
git update-index --no-skip-worktree kasa.txt telegram.txt mqtt.txt asiair.txt
```

A safety check before pushing, if you're ever unsure:

```bash
git diff --cached | grep -iE 'password|token|BEGIN .*PRIVATE KEY'
```

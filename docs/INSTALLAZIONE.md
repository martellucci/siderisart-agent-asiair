# Installazione

Guida per mettere l'agente in produzione su un server Linux sempre acceso.
Testata su Debian/Ubuntu; su altre distribuzioni cambiano solo i nomi dei
pacchetti.

> Prima di tutto questo, fai funzionare l'agente **a mano** con `--dry-run`.
> Installare i servizi è l'ultimo passo, non il primo.

---

## 1. Sistema e dipendenze

```bash
apt install python3 python3-pip rsync cifs-utils
pip install -r requirements.txt
```

Le dipendenze Python sono tre (`requests`, `astral`, `PyYAML`). Servono in più:

- `gspread` e `google-auth` **solo** se vuoi il diario su Google Sheets;
- `paho-mqtt` **solo** se vuoi la telemetria MQTT.

```bash
pip install gspread google-auth paho-mqtt     # opzionali
```

## 2. Collocazione

Il codice può stare ovunque; queste istruzioni usano `/opt/sfro-agent`, che è
anche il percorso scritto nelle unit systemd.

```bash
mkdir -p /opt/sfro-agent /var/lib/sfro-agent
cp -r *.py config.yaml *.txt logo.png /opt/sfro-agent/
chmod 600 /opt/sfro-agent/*.txt /opt/sfro-agent/credentials_asiair
```

Cosa va dove:

| Percorso | Contenuto |
|---|---|
| `/opt/sfro-agent` | codice, `config.yaml`, credenziali |
| `/var/lib/sfro-agent` | stato persistente (`state.json`), DB sessioni, sink di guida e autofocus |
| `/mnt/...` | mount della share ASIAIR e del NAS |
| `/var/www/html/...` | dashboard HTML generata |

Le credenziali vengono cercate **nella cartella dello script**, quindi devono
stare accanto ai `.py`.

## 3. Mount

**Share immagini dell'ASIAIR** — la monta e smonta l'agente da solo a ogni sync,
usando `sync_module.mount_point` e `credentials_file`. A te basta creare la
cartella:

```bash
mkdir -p /mnt/asiair
```

**Destinazione dei FITS** (NAS o disco locale) — questa deve essere già montata
e stabile. Se è un NAS via CIFS, mettila in `/etc/fstab`:

```
//IP_DEL_NAS/share  /mnt/astronomia  cifs  credentials=/root/.nas-cred,uid=0,gid=0,_netdev  0  0
```

> La password del NAS sta **solo** nel file credenziali di fstab, e non deve
> finire né nel repo né nei backup.

## 4. Servizi systemd

Nella cartella `systemd/` ci sono quattro unit già pronte:

| Unit | Tipo | Cosa fa |
|---|---|---|
| `sfro-agent.service` + `.timer` | oneshot ogni 5 min | l'agente |
| `sfro-mqtt.service` | persistente | telemetria MQTT |
| `sfro-telegram.service` | persistente | bot Telegram |

```bash
cp systemd/*.service systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sfro-agent.timer sfro-mqtt.service sfro-telegram.service
```

I due servizi persistenti sono opzionali: se non usi MQTT o Telegram, non
abilitarli.

> ⚠️ **`TimeoutStartSec` nella unit dell'agente non è decorativo.** Per i
> servizi `Type=oneshot` systemd disattiva il timeout *per default*: un ciclo
> appeso — per esempio su una CIFS morta, in D-state — bloccherebbe il timer
> **per sempre**, e l'agente smetterebbe di girare senza che nessuno se ne
> accorga. Il valore è dimensionato sul caso peggiore (sync finale + teardown).

## 5. Dashboard (opzionale)

La dashboard è un **singolo file HTML autonomo**: niente CDN, niente JavaScript
esterno, niente database da interrogare. Qualsiasi web server la serve.

```bash
apt install apache2
mkdir -p /var/www/html/sfro/statistiche
python3 /opt/sfro-agent/sfro_sessionlog.py --config /opt/sfro-agent/config.yaml --report
```

Poi la trovi su `http://<server>/sfro/statistiche`. Viene rigenerata da sola a
ogni push del diario con frame nuovi e a fine notte. Il percorso di uscita e
l'URL da mettere nei messaggi Telegram si impostano nel blocco `report:` del
config.

## 6. Verifica

```bash
# un ciclo a vuoto: non comanda prese, rsync in dry-run
python3 /opt/sfro-agent/sfro_agent.py --config /opt/sfro-agent/config.yaml --once --dry-run

# stato e decisione corrente, senza fare nulla
python3 /opt/sfro-agent/sfro_agent.py --config /opt/sfro-agent/config.yaml --status

# elenco delle prese Kasa viste dall'account (per trovare il device_id)
python3 /opt/sfro-agent/sfro_agent.py --config /opt/sfro-agent/config.yaml --discover

# log in diretta
journalctl -u sfro-agent.service -f
```

Il primo `--status` utile è **di sera, con il tetto aperto**: è lì che si vede
se l'agente riconosce la notte nautica, i device e il piano.

## 7. Aggiornamenti

```bash
git pull
cp *.py /opt/sfro-agent/
systemctl restart sfro-mqtt sfro-telegram   # solo se hai toccato quei due file
# l'agente è oneshot: ricarica da solo al prossimo scatto del timer
```

---

## Cosa salvare, se ci tieni

Sono i file che non si rigenerano da soli:

- `config.yaml` e tutte le credenziali (`*.txt`, `credentials_asiair`,
  `gdrive_sa.json`, `*.pem`) — **contengono chiavi private**;
- `/var/lib/sfro-agent/sessions.db`, il diario di tutte le tue notti;
- `/var/lib/sfro-agent/state.json`.

Si rigenerano da soli, e non serve salvarli: la dashboard HTML (la riscrive
`sfro_report.py` dal DB), i CSV del dettaglio (`sfro_sessionlog.py --csv`), i
sink JSONL di guida e autofocus.

Tieni **separati** l'archivio del codice da quello dei segreti: il primo si può
condividere, il secondo mai.

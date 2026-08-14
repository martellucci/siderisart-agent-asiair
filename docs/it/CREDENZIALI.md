# Credenziali — cosa serve e come procurarselo

*[English](../CREDENTIALS.md) · **Italiano***

Nel repo **non c'è nessuna credenziale**: i file `.txt` che trovi sono modelli
vuoti, e i file che contengono chiavi vere non sono pubblicati affatto. Questa
pagina spiega, uno per uno, come comporli.

> **Regola d'oro**: ogni file di questa pagina va `chmod 600` e **non va mai
> committato**. Vedi in fondo la sezione [Non committarli per sbaglio](#non-committarli-per-sbaglio).

| File | Serve per | È nel repo? |
|---|---|---|
| `config.yaml` | tutta la configurazione | no, lo crei tu da `config.example.yaml` |
| `telegram.txt` | notifiche e bot | sì, **vuoto** |
| `kasa.txt` | prese smart TP-Link | sì, **vuoto** |
| `mqtt.txt` | broker MQTT | sì, **vuoto** |
| `asiair.txt` | strumenti a riga di comando | sì, **vuoto** |
| `credentials_asiair` | mount SMB della share immagini | **no**, lo crei tu |
| `gdrive_sa.json` | diario su Google Sheets | **no**, lo scarichi da Google |
| `asiair_key.pem` | handshake ASIAIR firmware v3 | **no**, lo estrai tu dall'app |

---

## `telegram.txt` — bot e notifiche

1. Su Telegram scrivi a **[@BotFather](https://t.me/BotFather)**, comando
   `/newbot`, scegli nome e username. Ti risponde con il **token**, una stringa nella forma
   `<numero>:<lettere e cifre>`.
2. Apri una chat col tuo bot e mandagli un messaggio qualsiasi (se non lo fai, il
   bot non può scriverti per primo).
3. Per il **chat id**, apri nel browser:
   `https://api.telegram.org/bot<IL_TUO_TOKEN>/getUpdates` e cerca
   `"chat":{"id":...}`. Se usi un gruppo, l'id è negativo.
4. `thread_id` serve solo se scrivi dentro un *topic* di un supergruppo.

```ini
bot_token=IL_TOKEN_CHE_TI_HA_DATO_BOTFATHER
chat_id=123456789
# thread_id=42
```

Il bot risponde **solo** al chat id configurato: qualsiasi altro mittente viene
ignorato senza rispondere. È l'unico controllo di accesso, quindi non condividere
il token.

---

## `kasa.txt` — prese smart TP-Link

L'agente accende e spegne il rig attraverso il **cloud TP-Link**, non sulla rete
locale: così funziona anche se la VPN è giù.

```ini
username=LA_TUA_EMAIL_KASA
password=LA_TUA_PASSWORD
```

> ⚠️ **L'account non deve avere la 2FA attiva**: l'API cloud non la gestisce e il
> login fallisce. Conviene creare un **account Kasa dedicato** all'automazione e
> condividergli la presa dall'app, invece di usare il tuo account principale.

### Trovare il `device_id` della presa

Serve nel config, campo `kasa.device_id`. Se usi una **ciabatta multipresa**
(es. KP303) l'id è quello della *ciabatta*, mentre le singole prese si indicano
per nome nella lista `kasa.outlets`.

Con l'agente già configurato:

```bash
python3 sfro_agent.py --config config.yaml --discover
```

In alternativa lo trovi in qualsiasi libreria Kasa (`python-kasa`) o
intercettando la risposta di `getDeviceList` dell'API cloud.

---

## `mqtt.txt` — broker

Solo se vuoi la telemetria verso Home Assistant.

```ini
username=UTENTE_MQTT
password=LA_PASSWORD_MQTT
```

Se il broker accetta connessioni anonime, lascia i campi vuoti o cancella il
file. I sensori pronti da incollare in Home Assistant sono in
`homeassistant/sfro_homeassistant.yaml`.

---

## `asiair.txt` — connessione di controllo

Usato dagli strumenti a riga di comando basati su `asiair_client.py`. Sul
firmware v1 i canali TCP dell'ASIAIR **non richiedono autenticazione**: basta
essere sulla stessa rete (o dentro la VPN).

```ini
host=198.51.100.20
port=4700
guider_port=4400
```

- **4700** — canale imager/piano: camera, focheggiatore, ruota, autorun.
- **4400** — canale guider/montatura.

---

## `credentials_asiair` — mount SMB della share immagini

**Non è nel repo**: createlo a mano. È un normale file credenziali CIFS, quello
che il kernel Linux si aspetta per `mount -t cifs -o credentials=...`.

```bash
cat > credentials_asiair <<'EOF'
username=<utente SMB dell'ASIAIR>
password=<password SMB dell'ASIAIR>
EOF
chmod 600 credentials_asiair
```

- Sono le credenziali **dell'ASIAIR**, non quelle dell'account Kasa né del NAS.
- Le trovi (e le puoi cambiare) nell'app ASIAIR, nelle impostazioni di rete/SMB.
- La share delle immagini si chiama di norma **`TF Images`**; il nome sta nel
  config, campo `sync_module.smb_share_name`.

Prova prima a mano, prima di lasciarlo all'agente:

```bash
mkdir -p /mnt/asiair
mount -t cifs "//198.51.100.20/TF Images" /mnt/asiair \
      -o credentials=/path/credentials_asiair,ro,vers=3.0
ls /mnt/asiair
umount /mnt/asiair
```

---

## `gdrive_sa.json` — diario su Google Sheets

**Non è nel repo** e non deve esserci mai: è una chiave privata.

1. Vai sulla [Google Cloud Console](https://console.cloud.google.com/), crea (o
   scegli) un progetto.
2. Abilita **Google Sheets API** e **Google Drive API**.
3. *IAM e amministrazione → Account di servizio → Crea account di servizio*.
   Nome a piacere, nessun ruolo particolare.
4. Sull'account creato: *Chiavi → Aggiungi chiave → Crea nuova chiave → JSON*.
   Scarica il file e mettilo accanto all'agente come `gdrive_sa.json`.
5. Apri il JSON e copia il valore di **`client_email`** (finisce per
   `.iam.gserviceaccount.com`).
6. Crea il tuo Google Sheet e **condividilo in scrittura con quella email**,
   esattamente come faresti con una persona. Senza questo passo il diario
   fallisce con un errore di permessi.
7. L'`id` del foglio è la parte lunga dell'URL fra `/d/` e `/edit`: mettilo nel
   config in `session_log.sheet_id`.

```bash
chmod 600 gdrive_sa.json
```

> Il diario è **opzionale**: lascia `sheet_id` vuoto e l'agente salta tutta la
> parte Sheets, continuando a scrivere SQLite, CSV e dashboard.

---

## `asiair_key.pem` — handshake del firmware v3

**Non è nel repo e non lo sarà**: è materiale estratto dall'app ZWO, che non ho
il diritto di ridistribuire. Se ti serve, te lo estrai dalla tua copia dell'app.

Dal firmware **v3** il canale 4700 non accetta più comandi finché il client non
completa un **handshake a sfida RSA**: il box manda una stringa
(`get_verify_str`), il client la firma con una chiave privata e la rimanda
(`verify_client`), poi `pi_is_verified` conferma. Quella chiave è incorporata
nell'app, in chiaro, dentro la libreria nativa `libopenssllib.so`.

```bash
# 1. procurati il pacchetto dell'app che hai già licenza di usare (.apk/.xapk)
# 2. estrai la chiave (legge un file, non scarica e non installa niente)
python3 tools/asiair-tool/extract_key.py ASIAIR_3.0.0.xapk -o asiair_key.pem

# 3. prova l'handshake isolato contro il tuo box
python3 tools/asiair-tool/handshake.py --host 198.51.100.20 --key asiair_key.pem

chmod 600 asiair_key.pem
```

> **Stato attuale**: `asiair_client.py` **non implementa ancora** l'handshake,
> quindi questo codice funziona su **firmware v1** e non su v3. Gli script in
> `tools/asiair-tool/` (di terze parti, MIT) servono a preparare la migrazione e
> a verificare che la chiave sia quella giusta. Finché non porti l'handshake
> dentro il client, **non aggiornare l'app**: l'aggiornamento non si annulla.
>
> Nota legale: estrarre una chiave dall'app che hai licenza di usare, per far
> parlare un dispositivo che possiedi, è la classica interoperabilità
> (negli USA l'eccezione DMCA §1201(f); in UE la Direttiva 2009/24/CE art. 6).
> Ridistribuire la chiave è un'altra cosa: per questo qui non c'è.

---

## Non committarli per sbaglio

Il `.gitignore` copre già i file che non sono nel repo (`config.yaml`,
`credentials_asiair`, `*.pem`, `gdrive_sa.json`, `*.conf`). I quattro `.txt`,
invece, **sono tracciati** perché servono come modello: appena li compili git li
vede modificati e prima o poi finiscono in un commit.

Diglielo una volta sola, subito dopo il clone:

```bash
git update-index --skip-worktree kasa.txt telegram.txt mqtt.txt asiair.txt
```

Per tornare a vederli (per esempio per aggiornare i modelli):

```bash
git update-index --no-skip-worktree kasa.txt telegram.txt mqtt.txt asiair.txt
```

Controllo di sicurezza prima di un push, se hai un dubbio:

```bash
git diff --cached | grep -iE 'password|token|BEGIN .*PRIVATE KEY'
```

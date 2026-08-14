<p align="center">
  <img src="logo.png" alt="Sideris Art" width="180">
</p>

<h1 align="center">Agente ASIAIR per osservatorio remoto</h1>

<p align="center">
  <em>Automazione completa di una notte di ripresa: avvio del piano, sorveglianza,
  pausa meteo, flat e dark all'alba, spegnimento, diario e statistiche.</em>
</p>

<p align="center">
  <a href="#licenza"><img src="https://img.shields.io/badge/licenza-MIT-f5e6b8" alt="MIT"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-071535" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/ASIAIR-firmware%20v1-0a1c3e" alt="ASIAIR v1">
</p>

---

## Cos'è

Questo è il software che manda avanti da solo le mie notti su un **osservatorio
remoto**, a migliaia di chilometri da casa. Gira su un piccolo server Linux,
parla direttamente con l'**ASIAIR** sul suo protocollo JSON-RPC (niente app,
niente VNC, niente click) e si occupa di tutto quello che di solito faresti a
mano — solo che lo fa alle tre di notte mentre dormi, e all'alba ti manda su
Telegram il resoconto con il link alle statistiche.

Non è un prodotto: è un progetto personale, cresciuto notte dopo notte, con i
suoi guasti veri e le contromisure che ne sono nate. Lo pubblico perché le
trappole che ho trovato — e sono tante — possano risparmiare a qualcun altro
le stesse serate perse.

> **Se lo trovi utile**, la cosa che mi fa più piacere è che tu ti iscriva alla
> newsletter: è lì che racconto come evolve, cosa si rompe e cosa imparo.
> 👉 **[Iscriviti alla newsletter](<<<LINK_NEWSLETTER>>>)**

---

## Cosa sa fare

**Durante la notte**

- Verifica che i device siano collegati e che la **montatura sia davvero puntata
  alle coordinate giuste** prima di far partire qualsiasi cosa (gate di posizione).
- Legge lo **stato del tetto** da un'API ASCOM Alpaca e opera solo dentro la
  **notte nautica**, calcolata dalle coordinate dell'osservatorio.
- Accende l'anti-condensa della camera e la fascia anticondensa, apre il flat
  panel motorizzato, **avvia il piano**.
- Ogni 5 minuti: controlla che la ripresa proceda, **sincronizza i FITS** sul NAS
  in modo incrementale, aggiorna il diario.
- **Chiusura meteo = pausa, non fine**: ferma il piano, parcheggia, chiude il
  pannello, ma tiene il cooler acceso e non azzera niente. Quando il tetto
  riapre, **il piano riparte da solo dal punto in cui si era interrotto**.

**All'alba**

- Ferma il piano, parcheggia, resetta il piano per la notte dopo.
- Fascia anticondensa al massimo e **30 minuti di asciugatura** prima di
  qualsiasi flat (la condensa sul pannello rovina i flat, e non te ne accorgi).
- **Flat automatici per (filtro, gain) effettivamente usati quella notte**:
  legge dal diario quali filtri hai ripreso e con che gain, raggruppa, imposta
  la luminosità del pannello per gruppo e lancia un autorun per ciascuno con
  tempo di posa AUTO.
- **Dark flat** subito dopo, a pannello spento, con la posa esatta calcolata dai
  flat appena fatti.
- Sync finale, `pi_shutdown`, attesa che il box sia **davvero** morto, e solo
  allora stacca la corrente dalle prese smart.

**Sempre**

- **Bot Telegram** con menu a bottoni: stato, avvio, avvio piano, sync manuale,
  flat/dark a comando, spegnimento — tutte le operazioni pericolose con conferma.
- **Telemetria MQTT** verso Home Assistant: ~50 entità fra guida (RMS e picchi
  su finestra mobile), camera, fuoco, montatura, storage, temperatura CPU del
  box e assorbimento per singola presa.
- **Diario automatico**: legge gli header FITS, li mette in SQLite, aggiorna un
  Google Sheet con gli aggregati, scrive il dettaglio per notte in CSV.
- **Dashboard HTML** autonoma con le statistiche: ore per soggetto e filtro,
  resa sul buio disponibile, RMS di guida e HFR per notte, istogrammi.

---

## Com'è fatto

```
                 ┌─────────────┐   Alpaca/HTTPS    ┌──────────────┐
                 │  API tetto  │◄──────────────────┤              │
                 └─────────────┘                   │              │
                 ┌─────────────┐   cloud TP-Link   │   AGENTE     │
                 │ prese Kasa  │◄──────────────────┤  (ogni 5')   │
                 └─────────────┘                   │              │
                 ┌─────────────┐   JSON-RPC 4700   │  macchina    │
                 │   ASIAIR    │◄──────────────────┤  a stati     │
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
                                          │ dashboard HTML · MQTT  │
                                          └────────────────────────┘
```

| File | Ruolo |
|---|---|
| `sfro_agent.py` | L'agente. Timer systemd **oneshot ogni 5 minuti**: nessun demone che può morire in silenzio, ogni ciclo riparte da zero leggendo lo stato da disco. Orchestrazione della notte, teardown, flusso flat/dark, sync. |
| `sfro_mqtt.py` | Servizio persistente: telemetria verso MQTT/Home Assistant, ascolto degli eventi push dell'ASIAIR, sink JSONL di guida e autofocus. |
| `sfro_telegram.py` | Servizio persistente: bot con menu a bottoni e conferme. |
| `sfro_sessionlog.py` | Diario: FITS → SQLite → Google Sheet + CSV. |
| `sfro_report.py` | Dashboard HTML statistiche (file unico, niente CDN). |
| `asiair_client.py` | Trasporto JSON-RPC verso l'ASIAIR (porte 4700 e 4400). |

**Perché oneshot e non un demone**: un processo che gira per giorni accumula
socket zombie, connessioni CIFS in D-state e stati incoerenti. Un ciclo che
nasce, legge lo stato da un JSON, fa una cosa sola e muore è molto più difficile
da rompere — e se si impianta, il timer successivo riparte comunque.

---

## Requisiti

- **ASIAIR** con **firmware v1** (l'app 2.x). Vedi la nota sulla v3 più sotto.
- Un **server Linux** sempre acceso, raggiungibile in rete con l'ASIAIR
  (direttamente o via VPN). Basta un mini PC o un Raspberry.
- Python **3.10+** e le tre dipendenze in `requirements.txt`
  (`requests`, `astral`, `PyYAML`).
- **Opzionali**: prese smart TP-Link Kasa (accensione/spegnimento del rig),
  un broker MQTT e Home Assistant (telemetria), un bot Telegram (notifiche e
  comandi), un NAS o un disco per i FITS, un service account Google (diario su
  Sheets), Apache o qualsiasi web server (dashboard).
- Un **tetto o cupola con API ASCOM Alpaca** se vuoi la logica di apertura
  automatica. Senza, l'agente funziona lo stesso: consideri il tetto sempre
  aperto e ti tieni l'automazione del resto.

### ⚠️ Firmware ASIAIR v3

Dal firmware v3 il canale 4700 richiede un **handshake RSA di autenticazione**
con una chiave che sta dentro l'app. Questo codice è validato su **v1** e su v3
si fermerebbe al primo comando. In `tools/asiair-tool/` trovi gli script (MIT,
di [cpius/asiair-tool](https://github.com/cpius/asiair-tool)) per estrarre la
chiave dall'APK e provare l'handshake; il portarlo dentro `asiair_client.py` è
un lavoro che non ho ancora fatto. **Se aggiorni l'app, non torni indietro**:
disattiva l'aggiornamento automatico finché non sei pronto.

---

## Installazione rapida

```bash
git clone https://github.com/martellucci/siderisart-agent-asiair.git
cd siderisart-agent-asiair
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Poi apri `config.yaml` e compila tutti i punti marcati con `<<<`
(coordinate, IP dell'ASIAIR, endpoint del tetto, prese, broker). Compila i file
`.txt` delle credenziali seguendo **[docs/CREDENZIALI.md](docs/CREDENZIALI.md)**
e proteggili:

```bash
chmod 600 *.txt credentials_asiair
```

Prova a vuoto, senza toccare niente:

```bash
python3 sfro_agent.py --config config.yaml --once --dry-run
```

Quando sei convinto, installa i servizi come da
**[docs/INSTALLAZIONE.md](docs/INSTALLAZIONE.md)**.

> ### ⚠️ Non committare le tue credenziali
> I file `kasa.txt`, `telegram.txt`, `mqtt.txt`, `asiair.txt` sono nel repo
> **vuoti**, come modelli. Appena li compili, git li vede modificati: dì a git
> di ignorarne le modifiche, una volta sola.
> ```bash
> git update-index --skip-worktree kasa.txt telegram.txt mqtt.txt asiair.txt
> ```
> Il `.gitignore` protegge già `config.yaml`, `credentials_asiair`,
> `*.pem` e `gdrive_sa.json`.

---

## Documentazione

| Documento | Contenuto |
|---|---|
| **[docs/PROTOCOLLO_ASIAIR.md](docs/PROTOCOLLO_ASIAIR.md)** | **Il pezzo forte.** Il protocollo dell'ASIAIR come l'ho ricostruito comando per comando: canali, metodi, parametri, forma delle risposte, e soprattutto le **trappole** verificate dal vivo. |
| **[docs/CREDENZIALI.md](docs/CREDENZIALI.md)** | Come procurarsi e comporre ogni credenziale: bot Telegram, account Kasa e id della presa, SMB dell'ASIAIR, service account Google, chiave RSA per la v3. |
| **[docs/RETE.md](docs/RETE.md)** | Raggiungere un ASIAIR remoto: VPN sul router (non sul server), VLAN dedicata, regole firewall minime, e come diagnosticare "la VPN è su?" senza falsi negativi. |
| **[docs/INSTALLAZIONE.md](docs/INSTALLAZIONE.md)** | Installazione dei servizi systemd, mount CIFS, dashboard, verifica. |
| `tools/asiair-tool/RPC_METHODS.md` | Elenco dei metodi RPC estratti dall'app (materiale di terze parti, MIT). |

---

## Le trappole che mi sono costate una notte

Questo è il valore vero del progetto. Ogni riga qui sotto è una serata persa.

| Trappola | Cosa succede | Rimedio nel codice |
|---|---|---|
| **`value: 0` sul flat panel** | Il firmware interpreta lo 0 come `state:false` e il pannello **si APRE** invece di spegnersi. | "Chiuso e spento" = `value:5, state:true`. Mai zero su un'uscita PWM. |
| **`is_plan_started` mente** | Resta `true` anche dopo che il piano è stato fermato: non significa "sta riprendendo adesso". | Per dire "sta riprendendo" servono `plan_started` **e** `capturing`. |
| **Un piano interrotto non riparte** | `start_exposure` su un piano a metà non fa nulla di utile: va **resettato** prima. | `reset_plan()` con verifica su `get_plan` (lapsed 0, left == total). |
| **Il ping non basta per spegnere** | Il Pi risponde al ping mentre il sistema è già morto: staccare la corrente lì corrompe la SD; non staccarla lascia il rig acceso. | Controllo a due livelli: ping **e** porta 4700. Se l'app è giù ma il ping vive, 90 secondi di grazia e poi corrente via comunque. |
| **Float del firmware** | Scrivi una posa di `8.19` s, la rileggi come `8.190001` e il confronto esatto fallisce. | Pose arrotondate a 1 decimale e confronto con tolleranza di 5 ms. |
| **ASIAIR spento = socket zombie** | Il box spento non manda FIN/RST: la connessione di guida resta aperta per sempre e la telemetria muore **in silenzio**. | TCP keepalive + riconnessione forzata se il socket tace oltre N secondi. |
| **`rsync` rc=24** | "File vanished": un FITS ancora in scrittura sull'ASIAIR. Trattarlo come errore fatale annullava tutto lo spegnimento. | Tollerato come avviso: il file si riprende al sync successivo. |
| **Tempo AUTO dei flat al tetto** | A gain 0 e pannello al 50% il tempo calcolato tocca il limite di 15 s e talvolta il calcolo **fallisce**: l'autorun non parte proprio. | Luminosità del pannello diversa per gain, così la posa cade in mezzo alla finestra utile. |
| **L'ASIAIR ammucchia i frame** | Tutte le notti finiscono nella stessa cartella per tipo, e non è configurabile. | Lo smistamento per data lo fa il sync **in destinazione**, leggendo la data dal nome file. |
| **Timeout systemd sugli oneshot** | È **disabilitato** di default: un ciclo appeso su una CIFS morta blocca il timer **per sempre**. | `TimeoutStartSec` esplicito nella unit. |

---

## Test

Dodici script di test **completamente offline**: niente rig, niente rete,
niente NAS. Usano finti ASIAIR, finti Google Sheet in memoria e sandbox
temporanee, e coprono i guasti veri che hanno generato le contromisure qui
sopra.

```bash
for t in test/test_*.py; do python3 "$t" && echo "OK $t"; done
```

Durano pochi secondi in tutto. Se tocchi il codice, falli girare prima di
mandare l'agente in produzione su una notte serena.

---

## Note oneste, prima che tu ci metta le mani

- **Il progetto è cucito sul mio setup.** Camera mono con ruota portafiltri,
  montatura equatoriale, flat panel motorizzato, prese Kasa, tetto Alpaca. Con
  un setup diverso alcune parti non ti serviranno e altre andranno riscritte.
- **Non è testato su altri impianti.** Funziona sul mio, tutte le notti, ma non
  ho modo di provarlo altrove.
- **Il protocollo ASIAIR non è ufficiale né documentato.** È stato ricostruito
  osservando il traffico dell'app. ZWO può cambiarlo a ogni aggiornamento senza
  dire niente a nessuno — ed è quello che è successo con la v3.
- **Automatizzare un rig significa poterlo rompere.** Qui si comandano
  montatura, corrente e spegnimenti: parti con `--dry-run`, poi con un pezzo
  alla volta, e stai a guardare le prime notti. Il software è fornito **così
  com'è, senza garanzia**: quello che succede al tuo strumento è responsabilità
  tua.
- **Nessun automatismo di ripiego sugli errori.** Se qualcosa va storto durante
  i flat, il flusso si ferma e **lascia il rig acceso**, avvisando su Telegram.
  È una scelta: preferisco alzarmi e guardare piuttosto che far indovinare a un
  programma.

---

## Contributi

Segnalazioni e pull request sono benvenute, soprattutto se riguardano il
**protocollo** (metodi nuovi, differenze fra firmware, comportamenti diversi dai
miei). Se hai catturato qualcosa dall'app che qui non c'è, apri una issue: la
mappa in `docs/PROTOCOLLO_ASIAIR.md` cresce così.

---

## Licenza

**MIT** — vedi [LICENSE](LICENSE). Usalo, modificalo, fanne quello che vuoi;
tieni l'attribuzione e non chiedermi garanzie.

`tools/asiair-tool/` contiene materiale di terze parti, anch'esso MIT: vedi
`tools/asiair-tool/LICENSE` e `ORIGINE.md`.

---

<p align="center">
  <strong>Sideris Art</strong> · Fine Art Astrophotography<br>
  <a href="<<<LINK_NEWSLETTER>>>">Iscriviti alla newsletter</a>
</p>

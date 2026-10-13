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

<p align="center">
  <a href="README.md">English</a> · <strong>Italiano</strong>
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
> newsletter: è lì che racconto come evolve il progetto, cosa si rompe, cosa
> imparo notte dopo notte — e le foto che ne escono.
>
> 👉 **[sideris.art/journal](https://sideris.art/journal/)**

---

## Cosa sa fare

**Durante la notte**

- Verifica che i device siano collegati e che la **montatura sia davvero puntata
  alle coordinate giuste** prima di far partire qualsiasi cosa (gate di posizione).
- Legge lo **stato del tetto** da un'API ASCOM Alpaca e opera solo dentro la
  **notte nautica**, calcolata dalle coordinate dell'osservatorio.
- Accende l'anti-condensa della camera e la fascia anticondensa, apre il flat
  panel motorizzato, **avvia il piano**.
- Ogni 3 minuti: controlla che la ripresa proceda, aggiorna il diario e ogni 15
  minuti **sincronizza i FITS** sul NAS in modo incrementale (i punti fissi —
  fine piano, meteo, flat, spegnimento — sincronizzano comunque). Le passate
  automatiche guardano solo i frame degli **ultimi N giorni**
  (`sync_module.recent_days`), così ripulire il NAS dai file vecchi non li fa
  riscaricare.
- **Sorveglia il progresso vero**: non il flag "piano avviato", che resta acceso
  anche a piano fermo, ma il **residuo del piano**. Se non scende per 20 minuti
  avvisa su Telegram. È solo un avviso: una ripresa in corso non si tocca mai.
- **Se le prese sono accese ma l'ASIAIR tace**, a inizio notte toglie e ridà
  corrente alla **sola** presa dell'ASIAIR, per 30 secondi, una volta sola per
  notte — il caso dello shutdown fatto dall'app lasciando la corrente attaccata,
  che lascia il box morto ma alimentato e irraggiungibile.
- **Misura la condensa** ogni 6 minuti a tetto aperto, per decidere all'alba
  quanto aspettare prima dei flat (sotto).
- **Chiusura meteo = pausa, non fine**: ferma il piano, parcheggia, chiude il
  pannello, ma tiene il cooler acceso e non azzera niente. Quando il tetto
  riapre, **il piano riparte da solo dal punto in cui si era interrotto**.

**All'alba**

- Ferma il piano, parcheggia, resetta il piano per la notte dopo.
- Fascia anticondensa al massimo e **asciugatura prima di qualsiasi flat** — da 5
  a 30 minuti secondo la condensa misurata durante la notte (sotto). La condensa
  sul pannello rovina i flat, e non te ne accorgi.
- **Flat automatici per (filtro, gain) effettivamente usati quella notte**:
  legge dal diario quali filtri hai ripreso e con che gain, raggruppa, imposta
  la luminosità del pannello per gruppo e lancia un autorun per ciascuno con
  tempo di posa AUTO.
- **Dark flat** subito dopo, a pannello spento, con la posa esatta calcolata dai
  flat appena fatti. Il cooler resta acceso fino all'ultimo dark; solo dopo il
  rig va a riposo (cooler e scaldiglie spenti) mentre i file vengono copiati.
- **Sync finale in una passata**: al massimo 100 minuti, mai ripartita da sola.
  Prima un giro a vuoto conta esattamente cosa manca, poi un unico **messaggio
  Telegram «vivo»** viene modificato al suo posto ogni 9 minuti (senza
  notifica) con barra di avanzamento, file e GB sul totale, velocità e fine
  stimata — e avvisa se la stima va oltre il tempo massimo. La fine arriva come
  messaggio nuovo. Se il tempo finisce il rig **resta acceso** e **non si spegne
  mai con il sync incompleto**: ripremi Rsync e la copia riprende da dove era
  arrivata.
- `pi_shutdown`, attesa che il box sia **davvero** morto, e solo allora stacca
  la corrente dalle prese smart.

**Sempre**

- **Bot Telegram** con menu a bottoni: stato, avvio, avvio piano, sync manuale,
  flat/dark a comando, spegnimento — tutte le operazioni pericolose con conferma.
  Il sync manuale segue **esattamente la stessa procedura**: una passata, lo
  stesso messaggio «vivo», gli stessi testi.
- **Un sync alla volta**: bot e agente condividono un lock (`flock`), così due
  copie non si contendono mai lo stesso mount. Chi arriva secondo non parte — la
  passata notturna salta in silenzio, il sync dopo i flat aspetta, il bottone
  risponde "già in corso". Se il processo muore, il kernel libera il lock.
- **Vista per soggetto e per notte sul NAS**: accanto al mirror grezzo, un
  secondo albero `soggetto/notte/light|flat|dark` con i flat e i dark dei filtri
  e del gain di ciascun soggetto — fatto con **hardlink**, quindi niente occupa
  spazio due volte (CIFS non accetta symlink). La cartella della notte porta il
  giorno del **tuo** fuso orario (`folder_timezone`), non quello dell'osservatorio.
- **Telemetria MQTT** verso Home Assistant: ~60 entità fra guida (RMS e picchi
  su finestra mobile), camera, fuoco, montatura, storage, temperatura CPU del
  box, assorbimento per singola presa e stazione meteo di sito con la stima
  della condensa.
- **Diario automatico**: legge gli header FITS, li mette in SQLite, aggiorna un
  Google Sheet con gli aggregati, scrive il dettaglio per notte in CSV.
- **Dashboard HTML** autonoma con le statistiche: ore per soggetto e filtro,
  resa sul buio disponibile, RMS di guida e HFR per notte, istogrammi.

---

## La condensa: misurata, non indovinata

L'attesa di asciugatura prima dei flat era una costante cieca. Ma ogni minuto di
attesa in più è un minuto di **dark fatti con più luce ambiente**: all'alba
nautica mancano meno di 50 minuti al sorgere del Sole, e il crepuscolo civile
arriva prima. Da qui l'idea di misurare invece di indovinare.

La condensa si forma quando una superficie scende sotto il punto di rugiada, e
una superficie non riscaldata **non è alla temperatura dell'aria**: irradia verso
il cielo e si raffredda, tanto più quanto il cielo è sereno e l'aria è ferma.
Quindi:

```
margine = (temperatura − punto di rugiada) − raffreddamento radiativo stimato
```

Il raffreddamento si stima dalla **depressione del cielo** (`temperatura aria −
temperatura cielo`, che una stazione meteo con sensore IR riporta) e dal
**vento**: circa 5 °C con cielo sereno e calmo, 3 °C se sereno e ventoso o
velato, 1 °C con cielo coperto — e **5 °C anche a cielo ignoto**, cioè il caso
peggiore. Margine ≤ 0 significa condensa in atto. Sotto zero è **brina**, non
rugiada: il punto di brinamento è più alto di quello di rugiada, quindi si toglie
un altro grado di prudenza.

**La misura è di tutta la notte, non dell'alba**: se il margine è crollato alle
03:00, la condensa alle 06:00 c'è ancora. L'agente campiona ogni 6 minuti, ma
**solo in notte e a tetto aperto** (a tetto chiuso l'ottica non vede il cielo e
non irradia), e tiene margine minimo, minuti bagnati, pioggia e numero di letture.

All'alba il verdetto sceglie **quanto aspettare**:

| Banda | Attesa | Quando |
|---|---|---|
| **SECCA** | 5 min | margine minimo *e* attuale ampi, niente pioggia, nessuna chiusura del tetto, copertura piena della notte |
| **INTERMEDIA** | 10 min | notte misurata per intero e mai bagnata, ma senza il margine che certifica la banda secca |
| **NORMALE** | 20 min | **caso cieco**: stazione giù, dati vecchi o pochi, oppure notte con chiusura meteo (a tetto chiuso non si campiona e non si sa cosa sia successo fuori) |
| **UMIDA** | 30 min | condensa vista davvero, o pioggia |

L'asimmetria è voluta: **per allungare l'attesa basta un indizio, per accorciarla
serve l'unanimità.** La banda intermedia esiste perché *«non certificabile come
asciutta»* non è la stessa cosa di *«non ne so niente»*: una notte misurata da
capo a fondo senza mai vedere condensa è un'informazione, e merita meno dei 20
minuti pensati per quando siamo ciechi. Vale anche l'argomento fisico: il
raffreddamento del caso peggiore è tarato su una superficie che vede tutto il
cielo, mentre un'ottica dentro un capannone ne vede solo una parte.

Il verdetto si calcola **una volta sola**, quando comincia l'asciugatura, così
countdown e controllo finale leggono lo stesso numero e non possono divergere a
metà attesa. Tutto è disattivabile (`dew.enabled: false`), e c'è una **modalità
osservazione** (`dew.adaptive: false`) che misura e pubblica su MQTT ma lascia
l'attesa fissa: il modo onesto di guardarsi i dati per qualche settimana prima di
lasciargli cambiare i tempi.

Serve una stazione meteo interrogabile — qui è quella del sito, esposta sulla
stessa API Alpaca del tetto. Senza, l'agente resta sul valore fisso.

---

## Com'è fatto

```
                 ┌─────────────┐   Alpaca/HTTPS    ┌──────────────┐
                 │  API tetto  │◄──────────────────┤              │
                 └─────────────┘                   │              │
                 ┌─────────────┐   cloud TP-Link   │   AGENTE     │
                 │ prese Kasa  │◄──────────────────┤  (ogni 3')   │
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
| `sfro_agent.py` | L'agente. Timer systemd **oneshot ogni 3 minuti**: nessun demone che può morire in silenzio, ogni ciclo riparte da zero leggendo lo stato da disco. Orchestrazione della notte, teardown, flusso flat/dark, sync. |
| `sfro_mqtt.py` | Servizio persistente: telemetria verso MQTT/Home Assistant, ascolto degli eventi push dell'ASIAIR, sink JSONL di guida e autofocus. |
| `sfro_telegram.py` | Servizio persistente: bot con menu a bottoni e conferme. |
| `sfro_sessionlog.py` | Diario: FITS → SQLite → Google Sheet + CSV. |
| `sfro_report.py` | Dashboard HTML statistiche (file unico, niente CDN). |
| `sfro_sessioni.py` | Vista soggetto × notte dei FITS sul NAS, con hardlink. |
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
  Sheets), Apache o qualsiasi web server (dashboard), una stazione meteo
  interrogabile via ASCOM Alpaca (asciugatura adattiva pre-flat).
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
`.txt` delle credenziali seguendo **[docs/it/CREDENZIALI.md](docs/it/CREDENZIALI.md)**
e proteggili:

```bash
chmod 600 *.txt credentials_asiair
```

Prova a vuoto, senza toccare niente:

```bash
python3 sfro_agent.py --config config.yaml --once --dry-run
```

Quando sei convinto, installa i servizi come da
**[docs/it/INSTALLAZIONE.md](docs/it/INSTALLAZIONE.md)**.

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
| **[docs/it/PROTOCOLLO_ASIAIR.md](docs/it/PROTOCOLLO_ASIAIR.md)** | **Il pezzo forte.** Il protocollo dell'ASIAIR come l'ho ricostruito comando per comando: canali, metodi, parametri, forma delle risposte, e soprattutto le **trappole** verificate dal vivo. |
| **[docs/it/CREDENZIALI.md](docs/it/CREDENZIALI.md)** | Come procurarsi e comporre ogni credenziale: bot Telegram, account Kasa e id della presa, SMB dell'ASIAIR, service account Google, chiave RSA per la v3. |
| **[docs/it/RETE.md](docs/it/RETE.md)** | Raggiungere un ASIAIR remoto: VPN sul router (non sul server), VLAN dedicata, regole firewall minime, e come diagnosticare "la VPN è su?" senza falsi negativi. |
| **[docs/it/INSTALLAZIONE.md](docs/it/INSTALLAZIONE.md)** | Installazione dei servizi systemd, mount CIFS, dashboard, verifica. |
| `tools/asiair-tool/RPC_METHODS.md` | Elenco dei metodi RPC estratti dall'app (materiale di terze parti, MIT). |

---

## Le trappole che mi sono costate una notte

Questo è il valore vero del progetto. Ogni riga qui sotto è una serata persa.

| Trappola | Cosa succede | Rimedio nel codice |
|---|---|---|
| **`value: 0` sul flat panel** | Il firmware interpreta lo 0 come `state:false` e il pannello **si APRE** invece di spegnersi. | "Chiuso e spento" = `value:5, state:true`. Mai zero su un'uscita PWM. |
| **`is_plan_started` mente** | Resta `true` anche dopo che il piano è stato fermato: non significa "sta riprendendo adesso". | Per dire "sta riprendendo" servono `plan_started` **e** `capturing` (o l'attesa programmata prima del primo scatto). A inizio notte un flag sporco non fa adottare un piano fermo: si resetta e si riavvia. |
| **Un watchdog conta anche il tempo in cui non guardava** | L'allarme "il piano non avanza da N minuti" scattava **subito dopo la riapertura del tetto**: durante la pausa meteo il residuo resta fermo per costruzione e il watchdog non gira, così al ritorno trovava il residuo invariato e ci sottraeva un timestamp di prima della chiusura. | Il watchdog misura **solo i minuti in cui ha davvero sorvegliato**: se fra due passaggi c'è un buco (pausa meteo, agente fermo, VPN giù) riparte da zero. Vale per qualsiasi allarme del tipo "X non cambia da N minuti". |
| **Prese accese, box morto** | Uno `shutdown` dall'app lasciando la corrente attaccata lascia l'ASIAIR **alimentato ma halted**: non risponde e non c'è modo di riaccenderlo da remoto. Una notte persa senza nessun guasto. | Ciclo di corrente automatico a inizio notte, sulla **sola** presa dell'ASIAIR, e solo se la VPN risulta su (altrimenti il silenzio non prova niente). Un tentativo per notte; la montatura non si spegne mai. |
| **Un piano interrotto non riparte** | `start_exposure` su un piano a metà non fa nulla di utile: va **resettato** prima. | `reset_plan()` con verifica su `get_plan` (lapsed 0, left == total). |
| **Il ping non basta per spegnere** | Il Pi risponde al ping mentre il sistema è già morto: staccare la corrente lì corrompe la SD; non staccarla lascia il rig acceso. | Controllo a due livelli: ping **e** porta 4700. Se l'app è giù ma il ping vive, 90 secondi di grazia e poi corrente via comunque. |
| **Float del firmware** | Scrivi una posa di `8.19` s, la rileggi come `8.190001` e il confronto esatto fallisce. | Pose arrotondate a 1 decimale e confronto con tolleranza di 5 ms. |
| **ASIAIR spento = socket zombie** | Il box spento non manda FIN/RST: la connessione di guida resta aperta per sempre e la telemetria muore **in silenzio**. | TCP keepalive + riconnessione forzata se il socket tace oltre N secondi. |
| **`rsync` rc=24** | "File vanished": un FITS ancora in scrittura sull'ASIAIR. Trattarlo come errore fatale annullava tutto lo spegnimento. | Tollerato come avviso: il file si riprende al sync successivo. |
| **Una linea lenta sembra un blocco** | A ~2 MB/s dal sito remoto, 12 GB di flat e dark richiedono ben più di un'ora. Ogni passata da 30 minuti arrivava al tetto, il bot diceva "errore" e si fermava, e senza nessun segnale in mezzo sembrava piantato — il server è stato riavviato per niente. | Passate lunghe, output di rsync letto in diretta e un messaggio di avanzamento col conteggio dei file. Oggi: una passata da 100 minuti e un messaggio «vivo» con percentuale e fine stimata. |
| **Ripulire il mirror fa riscaricare tutto** | rsync rende la destinazione uguale alla sorgente: cancelli i flat vecchi sul NAS ma non sull'ASIAIR, e le passate dopo li ricopiano tutti — cinque ore di flat vecchi in coda **davanti** ai light della notte. | Si pulisce prima l'ASIAIR, poi il NAS. Le passate automatiche guardano comunque solo gli ultimi giorni (`recent_days`); il bottone del sync manuale no, per scelta. |
| **Due sync sullo stesso mount** | Il sync manuale del bot e la passata periodica dell'agente possono partire insieme: ognuno monta e smonta la share, togliendola da sotto all'altro. | Un `flock` non bloccante: un sync alla volta, rilasciato dal kernel se chi lo tiene muore. |
| **Tempo AUTO dei flat al tetto** | A gain 0 e pannello al 50% il tempo calcolato tocca il limite di 15 s e talvolta il calcolo **fallisce**: l'autorun non parte proprio. | Luminosità del pannello diversa per gain, così la posa cade in mezzo alla finestra utile. |
| **L'ASIAIR ammucchia i frame** | Tutte le notti finiscono nella stessa cartella per tipo, e non è configurabile. | Lo smistamento per data lo fa il sync **in destinazione**, leggendo la data dal nome file. |
| **Timeout systemd sugli oneshot** | È **disabilitato** di default: un ciclo appeso su una CIFS morta blocca il timer **per sempre**. | `TimeoutStartSec` esplicito nella unit. |
| **Un timeout più corto del lavoro** | La passata di sync era salita a 60 minuti, ma la unit diceva ancora `TimeoutStartSec=45min`. systemd ha ucciso il ciclo due volte a metà copia; il SIGTERM chiude Python **senza** eseguire il `finally`, quindi lo stato andava perso e ogni ciclo dopo riannunciava "dark completati", rispegneva il cooler e ripartiva col sync da zero — la chat si è riempita di messaggi sovrapposti che ricontavano da zero. | Il timeout deve superare la passata più lunga (ora 2 h per 100 minuti). Stato salvato **prima** della copia, SIGTERM trasformato in un'uscita ordinata, e una passata trovata già avviata **non** riparte mai. |

---

## Test

Ventuno script di test **completamente offline**: niente rig, niente rete,
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
mappa in `docs/it/PROTOCOLLO_ASIAIR.md` cresce così.

---

## Licenza

**MIT** — vedi [LICENSE](LICENSE). Usalo, modificalo, fanne quello che vuoi;
tieni l'attribuzione e non chiedermi garanzie.

`tools/asiair-tool/` contiene materiale di terze parti, anch'esso MIT: vedi
`tools/asiair-tool/LICENSE` e `ORIGINE.md`.

---

<p align="center">
  <strong>Sideris Art</strong> · Fine Art Astrophotography<br>
  <a href="https://sideris.art/journal/">Journal &amp; newsletter</a>
</p>

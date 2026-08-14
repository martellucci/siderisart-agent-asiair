# Il protocollo ASIAIR, ricostruito

*[English](../ASIAIR_PROTOCOL.md) · **Italiano***

> **Cos'e' questo documento.** Il protocollo dell'ASIAIR non e' pubblico ne'
> documentato: quello che segue e' stato ricostruito osservando il traffico
> dell'app e provando i comandi **dal vivo** su un box vero, notte dopo notte.
> Sono appunti di lavoro, con le date delle verifiche e i guasti che hanno
> generato ogni contromisura: li pubblico com'erano perche' e' la forma in cui
> sono davvero utili.
>
> **Validato su firmware v1.** Dalla v3 il canale 4700 richiede un handshake
> RSA: vedi [CREDENZIALI.md](CREDENZIALI.md).
>
> ZWO puo' cambiare tutto a ogni aggiornamento, senza dirlo a nessuno.
> Se trovi differenze sul tuo box, apri una issue: questa mappa cresce cosi'.

Data: 2026-06-27. Dispositivo: **ASIAIR Pro**, firmware **13.41** (`firmware_ver_int=1341`).
Discovery attiva via JSON-RPC, nessuna cattura app necessaria per arrivare fin qui.

## Mappa dei canali (porte TCP)
| Porta | Ruolo | Version event |
|---|---|---|
| **4400** | sottosistema **GUIDER** | `name:"ASI AIR guider"`, svr 12 |
| **4700** | **CONTROLLO IMAGER / PLAN** ← quello che ci serve | `name:"ASI AIR imager"`, svr 29, **fw 13.41** |
| 4500 / 4800 / 4801 | stream dati/immagini (binari, no JSON-RPC): **4800** = un frame per posa completata, **4500** = stream della camera di guida, 4801 = metadati immagine | — |
| 22 (SSH) | aperto (Raspberry Pi dell'ASIAIR) | — |

⚠️ Lezione: all'inizio avevamo puntato 4400 (guider) e mancava tutto l'autorun.
**Il controllo dei piani è su 4700.** `asiair.txt` ora ha `port=4700`.

## Accesso — OK
- `ping` OK; 4400/4500/4700/4800/4801/22 aperti; canale 4700 **senza auth**
  (vero sul NOSTRO firmware 13.41: dal ~43.97 serve un handshake RSA, vedi
  aggiornamenti 2026-08-12).
- 4700: `test_connection`→`"server connected!"`, `pi_is_verified`→`true`.
- Framing JSON `\r\n`, risposte `{"jsonrpc":"2.0","code":N,"result":...,"id":N}`,
  più eventi asincroni broadcast (senza `id`). Client esistente compatibile.

## Oracolo di scoperta
`code 103` = "method not found" · `104/108` = metodo esiste, parametri errati
(non esegue) · `315` mount non connesso · `318` device non connesso.

## Piani PRESENTI (letti da 4700)
`list_plan` → **1 piano**:
```
[{"id":1, "plan_name":"Piano_1", "enable":true, "target_cnt":6}]
```
`get_plan` → "Piano_1" (id 1), 6 target. Stato: già eseguito (mar 2026),
`capture.state:"idle"`, niente in corso. Target:
| id | nome | obj | enable | seq (exp×n, gain) |
|---|---|---|---|---|
| 2 | NGC 7790 | open cluster | no | light 300s×24 g100 |
| 10 | NGC 2264 | open cluster | no | light 300s×85 g100 |
| 11 | FOV | — | no | light 300s×20 g100 |
| 12 | NGC 2403 | galaxy | **sì** | light 300s×60 g100 |
| 13 | M 3 | glob cluster | no | 120s×60(off), 30s×10, 10s×60 |
| 14 | M 63 | galaxy | **sì** | light 300s×40 g100 |

## Metodi CONFERMATI su 4700
Lettura: `test_connection`, `pi_is_verified`, `get_app_state` (oggetto ricco con
`page`, `capture.progress`, ecc.), `get_camera_state`, `list_plan`, `get_plan`,
`get_sequence`(vuole int), `get_sequence_setting`.
Azione: `start_exposure` (avvia una cattura singola/preview — testato e fermato),
`start_auto_goto`(vuole float ra/dec).
Stop: nessuno dei nomi provati (`stop_capture`/`stop_plan`/...) esiste su 4700.

## Metodi CONFERMATI su 4400 (guider)
`test_connection`, `get_app_state`, `get_setting`, `get_exposure`, `set_exposure`,
`stop_capture`, `get_connected_cameras` (ASI224MC), `get_focuser_state/info`,
`scope_goto/sync/park/get_track_state/get_ra_dec`.

## AVVIO/STOP PIANO — RISOLTO (testato dal vivo)
**⚠️ CORREZIONE 2026-06-29 (cattura app su produzione): l'avvio corretto è
`start_exposure ["light"]`** — CON il param `"light"`, dopo `set_page ["plan"]`
(il comando agisce sulla pagina corrente: da "autosave" avvia l'autorun, da
"preview" scatta UNA posa). Con params vuoti su produzione il piano NON parte
(torna idle/plan_end): era il bug del "piano che non partiva". Il test storico
2026-06-27 in casa (sotto) col no-param era contesto-dipendente, NON fare
affidamento. Verifica d'avvio: `get_enabled_plan → is_plan_started:true`
(transizione; NB è un flag sticky, inutilizzabile come "in esecuzione adesso").
**`stop_exposure` ferma** (torna `idle`). Canale 4700.
```
start_exposure ["light"] -> code 0; eventi: Sequence:start, Target:start,
  Sequence:frame_start, Exposure:start exp_us=300000000 (300s) gain=100;
  get_app_state.capture: state="expose"/"first_delay", is_working=true.
stop_exposure  -> code 0; capture torna state="idle", is_working=false.
```

### Il blocco "manca il mount" era SOLO nell'app
L'app ASIAIR (UI) rifiuta "Avvia" senza mount, ma il **server** avvia comunque:
senza mount **salta il goto** e inizia a esporre. In produzione SFRO, **col mount
connesso, eseguirà anche il goto** su ogni target. Quindi l'agente NON ha bisogno
dell'app per avviare il piano.

### Setter (per selezionare/cambiare piano in futuro)
`set_plan` / `set_sequence` esistono (setter; con params vuoti sono no-op verificati:
Piano_1 intatto, 6 target). Se servirà scegliere TRA più piani o modificarli da
codice, i loro params si catturano con tcpdump su 4700 mentre si seleziona/avvia
dall'app (port 22 aperto): `sudo tcpdump -i any -A -s0 'tcp port 4700' | grep '"method"'`.
Con un solo piano già attivo NON serve: basta `start_exposure`.

### ⚠️ A FREDDO i device sono SCOLLEGATI → vanno connessi prima di start
Senza app collegata, dopo il boot i device restano chiusi e `start_exposure`
fallisce con **code 318 "device not connected"**. Connessioni (canale 4700),
testate in produzione 2026-06-29:
| Device | Canale | Metodo | Note |
|---|---|---|---|
| Camera principale (ASI2600MM Pro) | 4700 | `open_camera []` | |
| Camera guida (ASI120MM Mini) | **4400** | `set_camera_idx [1]` + `set_connected [{"camera":true}]` | `open_camera[1]` su 4700 NON funziona; la guida è sul canale guider |
| EAF (fuocheggiatore) | 4700 | `open_focuser []` | |
| Ruota EFW | 4700 | `open_wheel []` | |
| **Mount (AM5N)** | **4400** | **`set_connected [{"mount":true,"async":true}]`** | ricavato da cattura app; verifica con `get_connected [true]`→`{"mount":1,...}` |

- **code 202 = "already connected"** → trattare come OK (non errore).
- `open_camera` con NOME/path → 300 "internal error"; usare l'**indice** (0=main, 1=guida).
- **Il MOUNT vive sul canale 4400** (guider), NON 4700: `set_connected`, `get_connected`,
  `scope_get_info`, `scope_set_location`, `mount_scan_port` (→ /dev/ttyACM0), `scope_set_time`
  sono tutti su 4400. Mount = ZWO **AM5N**, fw 1.6.3, 12.1V.
Sequenza di avvio in `AsiairControl.start()`: connetti **mount (4400)** + camera/guida/EAF/EFW
(4700, accetta code 0 e 202) → `set_page ["plan"]` → `start_exposure`.

### Cattura del mount (storica) — RISOLTA
Catturata sul **Mac** (dove gira l'app) con `tcpdump -A 'host 198.51.100.20 and
(port 4400 or port 4700)' | grep '"method"'` mentre si premeva Connect: l'app invia
`set_connected [{"mount":true,"async":true}]` (4400), poi `mount_scan_port`,
`scope_set_time`, `scope_set_location [0.0, 0.0]` (= coord SFRO, ora corrette).
NB: non serve SSH all'ASIAIR; basta catturare sul Mac.

### Controllo dall'agente
Usare `asiair_control.py`: `status` | `plans` | `start --yes` | `stop` | `watch [sec]`.

## AGGIORNAMENTI 2026-07-03 → 07-05 (rig SFRO reale, tutti verificati live)

### Connessione a freddo — RISOLTA DAVVERO (2026-07-04): il PRIMING
La sezione "a freddo" sopra è superata: i 204 "out of limit"/207 "fail to operate"
a oltranza NON dipendevano da USB lento. Serve chiamare **`get_connected_cameras`
sul canale PRIMA di connettere la sua camera** (4700 prima di `open_camera[id]`,
4400 prima di `set_camera_idx`+`set_connected{camera}`). Col priming ogni device
si connette in 2–5s; cold boot completo KASA→piano avviato in ~60s. L'id camera
si ricava dal match sul nome ("2600"/"120"). Il mount senza alimentazione PERDE
L'ORA: dopo il connect va dato `scope_set_time ["YYYY-MM-DDTHH:MM:SS","-0"]`
(DUE parametri stringa, UTC; altre forme → 105). La location invece persiste
(`scope_get_info` usa chiavi maiuscole `Lat`/`Lon`).

### Avvio piano/autorun e contesti pagina
`start_exposure ["light"]` agisce sul **contesto pagina corrente**: `set_page
["plan"]` → avvia il PIANO; `set_page ["autosave"]` → avvia l'AUTORUN
(flat/dark); da "preview" scatta UNA posa. La pagina "autorun" NON esiste a
protocollo (code 109): si chiama **"autosave"**. Stop = `stop_exposure` (+
`clear_autosave_err` visto in cattura).

### ⚠️ `is_plan_started` NON significa "in esecuzione ADESSO"
`get_enabled_plan.is_plan_started` resta true dopo lo stop del piano E dopo il
reboot → inutilizzabile come rete di sicurezza (il 2026-07-04 ha abortito
un autorun flat sano). Verifica corretta di un autorun partito: dopo
`reset_sequence_progress`, il `left_time_sec` di `get_target_sequences` deve
SCENDERE sotto il valore post-reset entro ~30s.

Conferma live 2026-07-25 (prima pausa meteo vera): il flag stale ha fatto
credere all'agente che il piano fosse "già ripartito a mano" alla riapertura
del tetto, mentre era FERMO (l'utente lo ha riavviato a mano solo 52 min dopo)
→ riapertura ignorata in silenzio (bug, corretto: piano "in corso ADESSO" =
`is_plan_started` **e** `capture.is_working`). Il riavvio manuale dal punto
interrotto funziona (frame ripresi dal progressivo successivo, 0034).
`reset_plan` invece azzera davvero: dopo il reset dell'alba `is_plan_started`
risulta false (verificato live).

### Editing degli slot autorun (`set_sequence`) — catturato e validato
- `get_target_sequences` → lista slot `{filter,suffix,repeat,id,enable,autoexp,
  gain,exp,bin,type,capture_index,lapsed,...}`.
- **`set_sequence` rivuole lo slot COMPLETO in una lista** `[{...}]`; scrive
  enable E repeat; `left_time_sec` si aggiorna subito.
- **Code 224** "cannot edit sequence unless reset the progress": slot con
  `lapsed>0` non editabili → dare `reset_sequence_progress []` PRIMA di editare.
- Campo `filter` = indice 0-based su `get_wheel_setting.names`
  (qui `["L","R","G","B","S","H","O"]` → S=4, H=5).
- Durante l'autorun: `capture.frame_summary.complete_num/total` e
  `capture.target.seq.current_type` si aggiornano in ~1–2s; il gap flat→dark è
  ~1–2s → impossibile spegnere il pannello "al volo" senza sporcare il primo
  dark (misurato): da qui il flusso a DUE PASSATE dell'agente.

### Flat panel OF2 (`pi_output_set2`, uscita port2)
`value:85 state:true` = luce flat · `value:5 state:true` = pannello CHIUSO e
luce spenta di fatto · **MAI `value:0`**: il firmware forza `state:false` e il
pannello si APRE. Aperto = `state:false` (attendere ~7s il motore).

⚠️ **`pi_output_get2` puo' rispondere SENZA l'uscita 'flat_panel'** (guasto del
2026-08-12, ore 13:06 CEST, mai visto prima in un mese e mezzo di journal): la
risposta arriva — non e' un timeout, l'eccezione avrebbe detto altro — ma priva
dell'uscita, quindi `close_flat` non sa su che porta scrivere e non scrive. Quel
giorno e' successo all'ULTIMO passo del teardown (piano gia' fermo, cooler gia'
spento, mount gia' in home): flat annullati, **pannello rimasto aperto** e rig
acceso. Da allora la lettura passa da `_output_state`, che **ritenta 3 volte a 3s**
(`asiair.output_read_tries` / `output_read_wait_seconds`) e distingue nel
messaggio "manca l'uscita" da "la chiamata e' fallita" — prima davano lo stesso
testo. Vale per **tutte** le uscite: anche `set_output`, cioe' la **fascia
anticondensa**, aveva lo stesso difetto nello stesso identico punto.
Test: `test_flat_read_retry.py`.
La causa a monte resta **ignota**: una risposta storta isolata del box. Escluse
per prova diretta (2026-08-12, box acceso e nessuna sessione): risposte che
migrano tra connessioni (una socket muta non vede le risposte altrui);
concorrenza sui metodi di potenza (60 chiamate parallele a `pi_output_get2` e
`get_power_supply` da due connessioni, tutte corrette); **limite di connessioni**
(12 connessioni simultanee sulla 4700 piu' le 2 dei servizi: 44 letture, tutte
complete, ~140ms costanti, nessun degrado). Quindi il listener permanente della
telemetria introdotto lo stesso giorno **non c'entra**.

### Uscite di potenza — mappa COMPLETA (letta live sul box SFRO 2026-07-10)
`pi_output_get2 []` (4700) → array di 4 uscite, indice = `portN` di set2:
`port0 camera` (is_pwm false) · `port1 other` (is_pwm false) ·
`port2 flat_panel` (PWM) · `port3 dew_heater` (PWM, la fascia anticondensa "Output 4"
dell'app). `AsiairControl.set_output(type, value)` trova l'uscita per `type` e
scrive `{portN:{is_pwm,value,state:true,type}}` con verifica di rilettura —
validato live su dew_heater (code 0, readback coerente). La regola "mai
value:0" vale per tutte le uscite PWM. L'agente usa la fascia anticondensa al 100%
all'avvio del piano (2026-07-19) e durante l'asciugatura flat, al 50% prima
dello shutdown.

### Temperatura/cooler della camera principale (verificato live 2026-07-24)
`get_control_value` (4700): `["Temperature"]` → valore in **DECIMI di grado**
(384 = 38.4°C); `["TargetTemp"]` → target impostato dall'app (type "text",
es. 0.0 — la temperatura di lavoro dei light); `["CoolerOn"]` → 0/1;
`["CoolPowerPerc"]` → potenza cooler (NB: "CoolerPowerPerc" NON esiste,
code 109). Accensione: `set_control_value ["CoolerOn", 1]` con rilettura
(`AsiairControl.cooler_on`, speculare a cooler_off). Usati dal GATE
TEMPERATURA pre-flat (`camera_cooling`): flat/dark solo a camera entro
`camera_temp_tolerance` dal target.

### Gain della camera principale (verificato live 2026-07-24)
`get_control_value ["Gain"]` (4700) → `{name:"Gain", type:"number", value:N}`;
scrittura `set_control_value ["Gain", N]` con readback coerente (testato
0↔100). Gli slot autorun con `gain: -10000` ("default") usano il gain
CORRENTE della camera: impostarlo governa flat e dark flat insieme
(`AsiairControl.set_camera_gain`).

### Tempo di posa AUTO dei flat (verificato live 2026-07-24)
Slot flat con `autoexp: true`: all'avvio l'ASIAIR calibra (primo frame di
misura) e **scrive il tempo calcolato nel campo `exp` dello slot**
(osservato 1.8 → 1.62 in ~6 s; il valore PERSISTE dopo la passata) →
rilevazione = rileggere `get_target_sequences` a fine gruppo
(`AsiairControl.read_flat_exps`). NB con l'autoexp il tempo ricalcolato può
superare il preset e `left_time_sec` salire SOPRA il valore post-reset
(visto live 2026-07-24: G a gain 0, 362→661 s ≈ 22 s/posa — il primo check
"left<left0" ha abortito un autorun SANO): la verifica-positiva di
`start_flats` accetta ogni CAMBIO di left rispetto a left0 (solo l'autorun
tocca la sequenza; da pagina sbagliata il preview lascia left identico)
oppure "≥1 frame completato" (`frame_summary.complete_num`).
La scrittura di `exp` via `set_sequence`
(slot completo) è validata live: è il meccanismo con cui i dark flat
ricevono il tempo del flat corrispondente.
⚠️ **Tetto del calcolo AUTO ≈ 15 s** (osservato live): il 2026-07-25 i flat
R/G/B a **gain 0, pannello 50%** sono usciti tutti a **15.0 s esatti**
(clamp al massimo); il 2026-07-26, stesse condizioni, il calcolo su B è
**FALLITO** (errore in app "calcolo del tempo", sequenza mai avanzata:
left 664/664 s → watchdog agente ha fermato la cattura alle 13:35 CEST).
Conferma incrociata: i B manuali dell'utente a pannello ~100% sono venuti
a 7.1 s (≈ 14.2 s riportati al 50%, proprio a ridosso del tetto).
Contromisura in produzione: `brightness_by_filter_gain0 {R,G,B: 67}`
(scelta utente 2026-07-26 su misure live a 65%: R 9.1 s · B 7.1 s ·
G 3.1 s → a 67% attesi ~R 7 · B 5.5 · G 2.5 s, comodi nella finestra
1–15 s). NB R è il filtro col fabbisogno più alto e il pannello non è
lineare tra i momenti: se R@0 toccasse ancora il tetto, alzare il suo
valore nella mappa (è per-filtro).

### Anti-Dew Heater della camera principale (verificato live 2026-07-19)
`get_control_value ["AntiDewHeater"]` (4700) → `{name, type:"number", value:0|1}`
(la forma `[{"name": …}]` dà code 107 "expected object param"). Scrittura:
`set_control_value ["AntiDewHeater", 1]` — lo stesso comando che `cooler_off`
usa con 0. `AsiairControl.ensure_anti_dew()` legge, accende solo se spento e
verifica con rilettura; chiamato nel pre-avvio di `start()` (il 19/7 l'utente
l'ha trovato spento: ~10 pose rovinate dall'umidità del mattino).

### Reset del PIANO (candidato, NON ancora catturato dall'app)
Un piano interrotto NON riparte con `start_exposure` finché non viene
resettato (visto live notte 7/8-7). Il comando dell'app non è mai stato
catturato (la vecchia cattura era un listener passivo: solo eventi). L'agente
(`reset_plan()`, nel teardown alba/fine-piano) prova `reset_sequence_progress`
su `set_page ["plan"]` (i comandi agiscono sulla pagina corrente) poi
`reset_plan_progress`/`reset_plan`/`clear_plan_progress` (code 103 = innocuo)
e VERIFICA rileggendo `get_plan` (target abilitati: lapsed 0, left==total).
Se al primo teardown live nessun candidato azzera → catturare il comando vero
col tcpdump sul Mac premendo "Reset" nell'app.

✅ **CONFERMATO (2026-08-12, fonte esterna)**: il nome vero e' **`reset_plan`**,
uno dei tre che gia' proviamo. Lo usa in produzione
[tankhardrive/AsiAirController](https://github.com/tankhardrive/AsiAirController)
(stessa postazione remota, app macOS), insieme a `list_plan`, `set_plan`,
`import_plan` e `get_target_sequences`. Non serve piu' la cattura col tcpdump.

### Spegnimento e orologio del Pi
`pi_shutdown []` → result 0, box morto in ~15s. ⚠️ Il **ping NON basta** come
prova che sia spento: risponde il kernel, e capita che il Pi pinghi ancora con
l'app gia' giu' (vedi `wait_asiair_down`, README §6). Prova vera = la **porta
4700 non accetta piu' connessioni**. `pi_set_time [{time_zone,hour,min,sec,day,
year,mon}]` visto in cattura all'apertura dell'app.

## AGGIORNAMENTI 2026-08-12 — fonti esterne + verifiche live

Due repo pubblici esaminati (clonati e letti nel sorgente, non solo il README):
[cpius/asiair-tool](https://github.com/cpius/asiair-tool) (toolkit Python,
metodi estratti dall'APK 3.0.0 e validati su **firmware 43.97**) e
[StefanDorresteijn/asiair-dashboard](https://github.com/StefanDorresteijn/asiair-dashboard)
(dashboard web read-only, Node; il suo `docs/PROTOCOL.md` e' la parte di
valore). Nessuno dei due risolve la connessione dei device a freddo: il
dashboard non manda comandi, e asiair-tool **non conosce il priming** (vedi
2026-07-04). Su quello siamo avanti noi.

### ⚠️ Dal firmware ~43.97 la 4700 e' CHIUSA da un handshake RSA
Sul firmware nuovo il canale 4700, appena connesso, risponde **solo** a
`test_connection` e `get_verify_str` e **ignora in silenzio** tutto il resto
finche' il client non si autentica:
```
get_verify_str            -> {"str": "<sfida>"}
firma RSA PKCS#1 v1.5 su SHA-1 della sfida, in base64
verify_client [firma, sfida]
pi_is_verified            -> true  (canale sbloccato, per-connessione)
```
La chiave privata sta dentro l'APK dell'app (`classes6.dex`); asiair-tool ha
`extract_key.py` per tirarla fuori e `handshake.py` per la procedura.
**Il 4400 (mount + guida) non ha handshake** e resta accessibile comunque.

**Noi oggi NON siamo toccati** (verificato live sul box SFRO il 2026-08-12, a
sessione in corso): `pi_is_verified` → `true` senza aver firmato nulla, e
`get_svr_version` → `103 method not found`, cioe' siamo su firmware piu'
vecchio (l'evento `Version` conferma **13.41**, svr_ver_int 29).
➡️ **Un aggiornamento firmware dell'ASIAIR spegnerebbe l'agente** — non la
telemetria, proprio i comandi. Non aggiornare senza aver prima implementato
l'handshake.

### Porta 4800 — come sono fatti i dati delle immagini
Decodificata da tankhardrive (noi non ne abbiamo bisogno, le immagini arrivano
via rsync/SMB, ma il reverse engineering e' fatto): connessione TCP a parte,
la risposta e' un **header JSON seguito da uno ZIP** che contiene una voce
`raw_data`; la fine del trasferimento si riconosce dal marcatore di fine
central-directory **`PK\x05\x06`**. Poi si estrae e si fa il debayer. Il
metodo di richiesta e' `get_current_img` (4700). Il `PROTOCOL.md` dell'altro
repo dava questo stream per non decodificato.

### Mount che non si connette: catena di fallback (4400)
Se `mount_scan_port` non trova la montatura, l'app ha una strada piu' lunga che
noi non usiamo (nomi dal `MountGateway` dell'APK, validati su fw 43.97):
`get_mount_list` (lista driver, **indice 1 = ZWO AM3/AM5/AM7**) →
`select_mount_list_index [idx]` → `select_serial_dev [dev]` →
`scope_set_connection_mode` / `scope_set_connection_para` →
`get_mount_index` per rileggere la selezione, `get_connected_mount_info` →
`{model, fw_ver, sn, ble_name}` per confermare.
Da tenere come **piano B** di `connect_all`, non come sostituto del priming.
Confermato inoltre che sull'**AM5N `scope_park` == "Go Home"** dell'app (nessun
parametro): e' esattamente quello che fa il nostro `mount_home`.

### Eventi PUSH sulla 4700 — funzionano anche sul NOSTRO firmware
Verificato aprendo una socket sulla 4700 **senza inviare nulla** (2026-08-12,
piano in corso): l'ASIAIR spinge da solo eventi senza `id`. Osservati sul vivo:
| Evento | Contenuto utile |
|---|---|
| `Version` | `firmware_ver_string` (13.41), `svr_ver_int` |
| `PiStatus` | `temp`, **`is_undervolt`**, **`is_over_current`**, `is_overtemp` |
| `Exposure` | `state` start/downloading/complete, `page`, `exp_us`, `gain` |
| `SaveImage` | `start`, poi `complete` con `filename` + `fullname` |
| `Sequence` | `progress.cur_plan {total, lapse}`, `cur_target.target_name`, `frame_type` |
| `PlateSolve` · `Annotate` · `Temperature` | stato risolutore/annotazione/temp |
⚠️ La forma di `Sequence` del loro PROTOCOL.md (`frame`/`total_frame`) **non e'
la nostra**: loro fw 43.97, noi 13.41 — la doc si legge, il codice non si copia.

### `get_power_supply` — volt e ampere per rail (4700, letto live 2026-08-12)
`get_power_supply []` → `[[V, A], …]`, una coppia per uscita. Sul rig SFRO:
`[[12.09,0.72],[12.18,0.16],[0.02,0.0],[12.21,0.15],[12.24,1.94]]` (~35 W).
Dice se un'uscita **sta davvero assorbendo** (es. la fascia anticondensa), cosa
che `pi_output_get2` da solo non distingue. `get_disk_volume` → `{totalMB,
freeMB}` (121926/111125 MB).

## Script
- `asiair_status.py` — snapshot stato su 4700 (read-only).
- `asiair_discover.py [porta]` — enumeratore getter (oracolo 103).
- `asiair_find_start.py` — sweep guardato dei verbi d'avvio (4700).
- `asiair_probe_ports.py` — identifica il canale di controllo tra le porte.
- `asiair_probe_actions.py` / `asiair_probe_capture.py` — sonde d'azione (4400).

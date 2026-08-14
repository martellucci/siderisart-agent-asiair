# Raggiungere un ASIAIR remoto

*[English](../NETWORK.md) · **Italiano***

Se il tuo ASIAIR è in giardino, salta pure questa pagina: è già in rete con te.
Se invece è in un osservatorio remoto a qualche migliaio di chilometri, il
problema da risolvere è uno solo — **fare in modo che il server veda l'ASIAIR
per indirizzo IP**, come se fosse nella stanza accanto.

> Gli indirizzi usati qui sotto (`192.0.2.x`, `198.51.100.x`, `203.0.113.x`) sono
> reti riservate alla documentazione: **sostituiscili con i tuoi**.

---

## Il principio: la VPN sta sul router, non sul server

La tentazione è far gestire la VPN al server che ospita l'agente. È la scelta
peggiore: se il tunnel cade, l'agente cade con lui; se riavvii l'agente,
rinegozi il tunnel; e ogni altro device di casa (il tablet con l'app ASIAIR, per
esempio) resta fuori.

**Mettila sul router**, sempre attiva. Il server non sa nemmeno che esiste: per
lui l'ASIAIR è un IP raggiungibile, punto. È l'unica scelta che tiene in piedi
il tutto per mesi senza manutenzione.

```
   ┌───────────────┐        ┌──────────────┐   tunnel    ┌──────────────┐
   │ server agente │───────►│  IL ROUTER   │═══════════► │ rete remota  │
   │ (VLAN astro)  │        │ client VPN   │             │   ASIAIR     │
   └───────────────┘        │ sempre su    │             └──────────────┘
   ┌───────────────┐        └──────────────┘
   │ tablet, app   │───────►
   └───────────────┘
```

---

## 1. Una VLAN dedicata

Metti il server astro (e i device da cui usi l'app) su una **rete virtuale
separata** dal resto di casa. Costa cinque minuti e ti risparmia che una
telecamera cinese qualsiasi si trovi una rotta aperta verso l'osservatorio.

- Nome: `ASTRO`, VLAN ID a piacere.
- Subnet dedicata, con **IP statico o reservation** per il server.
- Su questa VLAN solo il server e i device dell'app.

> ⚠️ **Prerequisito bloccante**: fatti dire dal gestore dell'osservatorio la
> **subnet della rete remota**. Se coincide con quella di casa tua (il classico
> `192.168.1.x` di fabbrica da entrambe le parti) il routing **non può funzionare**:
> devi rinumerare la tua LAN prima ancora di cominciare.

## 2. Il client VPN sul router

Praticamente tutti i router seri (UniFi, OPNsense, MikroTik, pfSense) sanno fare
da **client** OpenVPN o WireGuard verso una rete remota.

- Carica il profilo che ti dà l'osservatorio (`.ovpn` o WireGuard).
- Se il profilo usa `auth-user-pass`, inserisci utente e password.
- Verifica che lo stato sia **Connected** e che il client si riconnetta da solo.

## 3. Split tunnel: nel tunnel va solo ciò che serve

**Non mandare tutto internet dentro il tunnel.** Faresti uscire il traffico di
casa dall'altra parte del mondo, con latenza e banda dell'osservatorio.

Crea una regola di routing:

- **sorgente**: la VLAN `ASTRO` (o il solo server);
- **destinazione**: le **subnet remote** dell'osservatorio;
- **interfaccia**: il tunnel VPN.

Tutto il resto — cloud delle prese smart, Telegram, aggiornamenti, API del tetto
— continua a uscire dalla WAN normale. È anche il motivo per cui accensione
prese e lettura del tetto **continuano a funzionare con la VPN giù**: passano da
internet pubblico, non dal tunnel.

## 4. Firewall: apri il minimo

Dalla VLAN `ASTRO` verso la subnet remota, attraverso il tunnel:

| Protocollo | Perché |
|---|---|
| **ICMP** | diagnosi di raggiungibilità |
| **TCP 4700** | canale imager/piano dell'ASIAIR |
| **TCP 4400** | canale guider/montatura |
| **TCP 445** | SMB, download delle immagini |

Poi: `ASTRO → Internet` consentito (cloud prese, Telegram, aggiornamenti) e
`ASTRO → altre VLAN locali` **bloccato**. Il traffico di ritorno lo gestisce il
firewall stateful, non serve aprirlo.

> Le porte dei flussi live dell'app ASIAIR non sono documentate. Se qualche
> funzione dell'app non va, apri temporaneamente **tutto il TCP verso il solo IP
> dell'ASIAIR**, guarda con Wireshark quali porte usa davvero e poi richiudi.
> All'agente bastano 4700, 4400 e 445.

## 5. Entrare da fuori casa

Se vuoi raggiungere l'ASIAIR remoto **dal telefono quando sei in giro**, senza
attivare e disattivare due VPN ogni volta, la strada è far transitare il tuo
tunnel di casa dentro quello dell'osservatorio:

1. nella rotta verso la rete remota, includi fra le sorgenti anche il **pool di
   indirizzi del tuo VPN server** (o metti sorgente "Any");
2. consenti nel firewall il traffico **da zona VPN a zona VPN**, verso le subnet
   remote;
3. sul telefono, aggiungi le **subnet remote** agli `AllowedIPs` del profilo
   WireGuard: quello generato dal router contiene solo le reti locali;
4. il NAT di solito è già a posto (il router masquerada in uscita sul tunnel);
   se non funziona, aggiungi il masquerade con sorgente il pool VPN.

> **Tunnel dentro tunnel**: se l'SMB o l'app si incastrano sui trasferimenti
> grossi, il colpevole è quasi sempre l'MTU. Abbassa a **1280** quello del
> profilo sul telefono.

---

## 6. Verifica

Dal server:

```bash
ping <IP_ASIAIR>
nc -vz <IP_ASIAIR> 4700
nc -vz <IP_ASIAIR> 445
mount -t cifs "//<IP_ASIAIR>/TF Images" /mnt/prova -o credentials=...,ro
```

---

## 7. "La VPN è su?" — la diagnosi che sembra ovvia e non lo è

Il modo istintivo è pingare un host della rete remota e concludere: risponde =
tunnel su, non risponde = tunnel giù. **È sbagliato**, e ti darà falsi allarmi a
ripetizione.

Quando il rig è spento, nella rete remota **non risponde più nessuno**: né
l'ASIAIR, né la sonda che ti sei scelto, spesso nemmeno il gateway. Il ping
fallisce e tu concludi "VPN giù" mentre il tunnel è perfettamente in piedi.

La prova giusta arriva da un dettaglio: se pinghi l'ASIAIR spento, è il **router
remoto** a risponderti `Destination Host Unreachable`. Quel pacchetto ICMP è
arrivato *attraverso il tunnel*: la sua sola esistenza dimostra che la VPN
funziona, e per giunta ti dice che il box è semplicemente spento.

È il criterio che usa `vpn_diagnose()` in `sfro_agent.py`:

1. pinga l'ASIAIR;
2. se risponde → VPN su, box acceso;
3. se arriva un ICMP *unreachable* da un indirizzo della rete remota → **VPN su,
   box spento** (che è la situazione normale di giorno);
4. solo se non torna proprio nulla, prova la sonda di riserva
   (`asiair.vpn_probe_host`, tipicamente il router remoto);
5. silenzio totale → VPN giù, e allora sì che vale la pena avvisare.

Senza questa distinzione l'agente ti sveglia ogni mattina per dirti che la VPN è
caduta, quando ha solo spento il rig come gli avevi chiesto.

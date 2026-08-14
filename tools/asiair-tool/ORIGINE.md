# asiair-tool — copia locale (solo i file che ci servono)

Origine: <https://github.com/cpius/asiair-tool>, commit `9131dca` del 2026-08-10,
licenza **MIT** (vedi `LICENSE`, © 2026 Mads Dørup). Copiati qui il 2026-08-12
perche' servono alla migrazione a ASIAIR v3 e non si vuole dipendere da un clone temporaneo.

| File | A cosa serve a noi |
|---|---|
| `extract_key.py` | Estrae la chiave RSA dell'handshake 4700 da un APK/XAPK ASIAIR. **Legge un file, non scarica nulla e non installa nulla**: l'APK e' uno zip. |
| `handshake.py` | Procedura di autenticazione della 4700 (`get_verify_str` → firma → `verify_client` → `pi_is_verified`): il riferimento da cui portare l'handshake dentro `asiair_client.py`. |
| `air_rpc.py` | Client 4700 minimale usato da `handshake.py`; utile per provare l'handshake isolato prima di toccare il nostro client. |
| `RPC_METHODS.md` | Nomi e parametri dei metodi estratti dall'app 3.0.0 e validati su firmware 43.97: la mappa per rivalidare il protocollo dopo l'aggiornamento. |

NB: i metodi in `RPC_METHODS.md` sono verificati su **firmware 43.97**, noi siamo
su **13.41**. Le differenze note (forma degli eventi, handshake) sono annotate in
`../../docs/it/PROTOCOLLO_ASIAIR.md`, sezione "AGGIORNAMENTI 2026-08-12".

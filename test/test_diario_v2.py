#!/usr/bin/env python3
"""Test offline del diario "v2" (2026-08-12):
  - dettaglio per-frame -> CSV per notte sul NAS (non piu' su Sheets)
  - Google Sheet = Sessioni + 'Per soggetto' + 'Per mese', riscritti
  - dashboard HTML rigenerata da SQLite

Niente rete: gspread e' sostituito da un foglio finto in memoria.
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_report as RP
import sfro_sessionlog as SL

TMP = Path(tempfile.mkdtemp(prefix="diario2_"))
CFG = {
    "timezone_observatory": "America/Chicago",
    "location": {"name": "Il mio osservatorio", "latitude": 0.0, "longitude": 0.0},
    "nautical": {"depression": 12},
    "session_log": {
        "enabled": True,
        "db_file": str(TMP / "sessions.db"),
        "scan_dir": str(TMP / "light"),
        "guide_sink_dir": str(TMP / "guide"),
        "focus_sink_dir": str(TMP / "focus"),
        "sheet_id": "FAKE",
        "sa_json": str(TMP / "sa.json"),
        "tab_sessions": "Sessioni",
        "tab_detail": "",
        "tab_by_object": "Per soggetto",
        "tab_by_month": "Per mese",
        "detail_csv_dir": str(TMP / "nas" / "dettaglio"),
        "location_label": "SFRO",
    },
    "report": {"enabled": True, "out_dir": str(TMP / "www"), "detail_nights": 2,
               "chart_nights": 60, "rms_bad": 1.0,
               "targets": {"NGC 7000": 60}},
}
(TMP / "sa.json").write_text("{}")          # basta che esista: _sheet e' finto


# --------------------------------------------------------------------------- #
# Google Sheet finto
# --------------------------------------------------------------------------- #
class FakeWS:
    def __init__(self, title):
        self.title, self.rows, self.frozen = title, [], 0

    def get_values(self, rng=None):
        if rng == "A1:A1":
            return [[self.rows[0][0]]] if self.rows else []
        return [[str(c) for c in r] for r in self.rows]

    def update(self, values=None, range_name=None):
        assert range_name == "A1"
        self.rows = [list(r) for r in values]

    def append_rows(self, rows, value_input_option=None):
        self.rows += [list(r) for r in rows]

    def clear(self):
        self.rows = []

    def freeze(self, rows=0):
        self.frozen = rows


class FakeSheet:
    def __init__(self):
        self.tabs = {}

    def worksheet(self, title):
        if title not in self.tabs:
            raise RuntimeError(f"tab {title} assente")
        return self.tabs[title]

    def add_worksheet(self, title=None, rows=0, cols=0):
        self.tabs[title] = FakeWS(title)
        return self.tabs[title]

    def del_worksheet(self, ws):
        del self.tabs[ws.title]


SHEET = FakeSheet()
SL._sheet = lambda c: SHEET


# --------------------------------------------------------------------------- #
def seed():
    """Due notti, due soggetti, filtri diversi, piu' una postazione 'gemella'
    gia' presente sul foglio (che NON deve essere toccata)."""
    db = SL.open_db(CFG["session_log"]["db_file"])
    t0 = datetime(2026, 8, 10, 3, 0, tzinfo=timezone.utc)   # 10/8 notte
    n = 0
    for nid, obj, filt, k, rms in (("2026-08-09", "NGC 7000", "H", 6, 0.55),
                                   ("2026-08-09", "NGC 7000", "O", 4, 0.72),
                                   ("2026-08-10", "M 42", "L", 5, 1.35)):
        for i in range(k):
            t = t0 + timedelta(days=(nid == "2026-08-10"), minutes=5 * n)
            db.execute("INSERT INTO frames VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                       "?,?,?,?,?,?,0,?)",
                       (f"L_{nid}_{filt}_{i}.fit", nid, obj, filt,
                        t.isoformat(timespec="seconds"),
                        t.isoformat(timespec="seconds"), 300.0, 100, 30, 1, -10.0,
                        4200, rms + i * 0.02, rms * .7, rms * .7, rms + .5,
                        900, 98.0, 3.0, 41.5, f"/nas/{obj}/L_{nid}_{filt}_{i}.fit",
                        t.isoformat(timespec="seconds")))
            n += 1
    db.commit()
    db.close()
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(CFG["timezone_observatory"])
    dbx = SL.open_db(CFG["session_log"]["db_file"])
    for nid in ("2026-08-09", "2026-08-10"):
        SL._aggregate_night(dbx, nid, tz, "")
    dbx.commit()
    dbx.close()
    SL.finalize(CFG, "2026-08-09", "alba_nautica")
    SL.finalize(CFG, "2026-08-10", "meteo")
    # riga di un'altra postazione, gia' sul foglio
    for tab, hdr, row in (("Sessioni", SL.SESS_HEADER, ["NINA", "2026-08-09", "IC 1396"]),
                          ("Per soggetto", SL.BYOBJ_HEADER, ["NINA", "IC 1396", "H"]),
                          ("Per mese", SL.BYMON_HEADER, ["NINA", "2026-08", 3])):
        ws = SHEET.add_worksheet(title=tab)
        ws.rows = [hdr, (row + [""] * len(hdr))[:len(hdr)]]


def ck(cond, msg):
    print(("  OK  " if cond else "  KO  ") + msg)
    if not cond:
        sys.exit(1)


def main():
    seed()
    print("\n--- A. push: CSV sul NAS + tab riscritte ---")
    res = SL.push(CFG)
    print("   ", json.dumps(res, ensure_ascii=False))
    ck(res.get("csv_files") == 2, "un CSV per notte (2)")
    ck(res.get("csv_rows") == 15, "15 righe di dettaglio esportate")
    ck(res.get("detail_appended") == 0, "NIENTE dettaglio sul foglio")

    csvd = Path(CFG["session_log"]["detail_csv_dir"])
    f = csvd / "SFRO_20260809.csv"
    ck(f.exists() and (csvd / "SFRO_20260810.csv").exists(),
       "file SFRO_aaaammgg.csv presenti")
    lines = f.read_text(encoding="utf-8-sig").splitlines()
    ck(lines[0].split(";")[0] == "File" and len(lines) == 11,
       f"CSV 9/8: intestazione + 10 frame (trovate {len(lines)-1})")
    ck(len(lines[1].split(";")) == len(SL.DET_HEADER), "21 colonne come DET_HEADER")

    print("\n--- B. tab Sessioni: altre postazioni preservate ---")
    rows = SHEET.tabs["Sessioni"].rows
    ck(rows[0] == SL.SESS_HEADER, "intestazione")
    ck(any(r[0] == "NINA" for r in rows[1:]), "riga NINA ancora presente")
    ck(sum(1 for r in rows[1:] if r[0] == "SFRO") == 3, "3 righe SFRO (soggetto x filtro)")

    print("\n--- C. riepilogo per soggetto ---")
    rows = SHEET.tabs["Per soggetto"].rows
    ck(rows[0] == SL.BYOBJ_HEADER, "intestazione")
    ck(any(r[0] == "NINA" for r in rows[1:]), "riga NINA preservata")
    ngc = [r for r in rows[1:] if r[1] == "NGC 7000" and r[2] == "H"][0]
    ck(int(ngc[4]) == 6 and ngc[5] == "00:30:00" and float(ngc[6]) == 0.5,
       "NGC 7000/H: 6 frame, 00:30:00, 0.5 ore")
    ck(int(ngc[3]) == 1, "1 notte")

    print("\n--- D. riepilogo per mese ---")
    rows = SHEET.tabs["Per mese"].rows
    ago = [r for r in rows[1:] if r[0] == "SFRO"][0]
    ck(int(ago[2]) == 2 and int(ago[3]) == 15, "2 notti, 15 frame")
    ck(float(ago[5]) == 1.25, "1.25 ore totali")
    ck(int(ago[6]) == 2, "2 soggetti")

    print("\n--- E. idempotenza: secondo push senza frame nuovi ---")
    res2 = SL.push(CFG)
    ck(res2.get("csv_files") == 0, "nessun CSV riscritto (pushed=1)")
    ck(len(SHEET.tabs["Sessioni"].rows) == len(SHEET.tabs["Sessioni"].rows),
       "foglio stabile")
    ck(sum(1 for r in SHEET.tabs["Per soggetto"].rows[1:] if r[0] == "NINA") == 1,
       "la riga NINA non si duplica")

    print("\n--- F. NAS non raggiungibile: il diario sale lo stesso ---")
    db = SL.open_db(CFG["session_log"]["db_file"])
    db.execute("UPDATE frames SET pushed=0 WHERE night_id='2026-08-10'")
    db.commit()
    db.close()
    old = CFG["session_log"]["detail_csv_dir"]
    CFG["session_log"]["detail_csv_dir"] = "/proc/nope/dettaglio"   # non creabile
    res3 = SL.push(CFG)
    ck("csv_error" in res3, "errore CSV segnalato, non sollevato")
    ck(res3.get("sessions") == 3, "Sessioni comunque riscritte")
    db = SL.open_db(CFG["session_log"]["db_file"])
    still = db.execute("SELECT COUNT(*) FROM frames WHERE pushed=0").fetchone()[0]
    db.close()
    ck(still == 5, "i frame restano da esportare (riprova al ciclo dopo)")
    CFG["session_log"]["detail_csv_dir"] = old
    SL.push(CFG)

    print("\n--- G. dashboard HTML ---")
    r = RP.build(CFG)
    print("   ", json.dumps(r, ensure_ascii=False))
    ck(Path(r["file"]).name == "index.html" and r["frames"] == 15, "pagina generata")
    h = Path(r["file"]).read_text()
    ck("<title>" in h and h.rstrip().endswith("</html>"), "HTML completo")
    ck("http" not in h.split("<style>")[0].replace('lang=\'it\'', ""),
       "nessuna risorsa esterna nell'head")
    ck("NGC 7000" in h and "M 42" in h, "soggetti presenti")
    ck("Scatti da rivedere" in h, "KPI degli scarti presente")
    ck("scatti · " in h, "progetti: scatti e ore per ogni filtro")
    ck(">10/08<" in h and "ago 2026" in h, "date asse X in gg/mm + mese sotto")
    ck("preserveAspectRatio" not in h, "nessun grafico deformato (testo leggibile)")
    ck(h.count("<details>") == 2, "dettaglio per-frame limitato a 2 notti")
    ck("60h" in h or "/60h" in h, "obiettivo ore mostrato")
    # la resa usa il buio nautico: deve essere calcolata, non vuota
    ck("del buio nautico" in h, "KPI resa media presente")

    print("\n--- H. migrazione una tantum ---")
    SHEET.add_worksheet(title="Dettaglio_SFRO").rows = [SL.DET_HEADER] + [["x"] * 21] * 15
    out = SL.migrate_detail(CFG, "Dettaglio_SFRO", delete=True)
    print("   ", json.dumps(out, ensure_ascii=False))
    ck(out.get("csv_rows") == 15 and out.get("db_rows") == 15, "conteggi allineati")
    ck("Dettaglio_SFRO" not in SHEET.tabs, "tab eliminata")
    ck("eliminata" in out.get("tab", ""), "esito riportato")

    print("\n--- I. migrazione con conteggi diversi: NON tocca il foglio ---")
    SHEET.add_worksheet(title="Dettaglio_SFRO").rows = [SL.DET_HEADER]
    CFG["session_log"]["detail_csv_dir"] = "/proc/nope/dettaglio"
    out = SL.migrate_detail(CFG, "Dettaglio_SFRO", delete=True)
    ck("error" in out or "Dettaglio_SFRO" in SHEET.tabs, "tab preservata in caso di dubbio")
    CFG["session_log"]["detail_csv_dir"] = old

    print("\nTUTTI GLI SCENARI OK")
    print(f"(sandbox: {TMP})")


if __name__ == "__main__":
    main()

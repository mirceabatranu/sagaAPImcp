"""Test complet pe un SAGA Web simulat (fara retea, fara cheie reala).

Ruleaza:  uv run python tests/test_mock.py

Verifica: parametrii si headerele trimise, rotirea cheii (cheia veche e refuzata
dupa rotire), parsarea balantei, constructia si importul in doi pasi.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import keyring
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.stdout.reconfigure(encoding="utf-8")
SERVICE = "saga-mcp-test"
PORT = 8765
state = {"key": "KEY-1", "rotations": 0, "log": []}

BALANTA = """<?xml version="1.0" encoding="utf-8"?>
<Balanta>
  <LinieBalantaXML><CONT>371</CONT><DENUMIRE>MARFURI</DENUMIRE><SOLD_FIN_D>1200.50</SOLD_FIN_D></LinieBalantaXML>
  <LinieBalantaXML><CONT>4423</CONT><DENUMIRE>TVA DE PLATA</DENUMIRE><SOLD_FIN_C>830</SOLD_FIN_C></LinieBalantaXML>
  <LinieBalantaXML><CONT>5311</CONT><DENUMIRE>CASA IN LEI</DENUMIRE><SOLD_FIN_D>24571</SOLD_FIN_D></LinieBalantaXML>
</Balanta>"""

BALANTA_ANALITICA = """<?xml version="1.0" encoding="utf-8"?>
<Balanta>
  <LinieBalantaXML><CONT>401</CONT><DENUMIRE>FURNIZORI</DENUMIRE></LinieBalantaXML>
  <LinieBalantaXML><CONT>401.00001</CONT><DENUMIRE>DEDEMAN SRL</DENUMIRE></LinieBalantaXML>
  <LinieBalantaXML><CONT>5311</CONT><DENUMIRE>CASA IN LEI</DENUMIRE></LinieBalantaXML>
</Balanta>"""


def anaf_found(cui, nume, inactiv=False, tva=True):
    return {
        "date_generale": {"cui": cui, "denumire": nume, "nrRegCom": f"J40/{cui}/2000", "adresa": "BUCURESTI",
                          "telefon": "", "iban": ""},
        "inregistrare_scop_Tva": {"scpTVA": tva},
        "inregistrare_RTVAI": {"statusTvaIncasare": False},
        "stare_inactiv": {"statusInactivi": inactiv, "dataRadiere": ""},
        "adresa_sediu_social": {"sdenumire_Strada": "Str. Test", "snumar_Strada": "1",
                                "sdenumire_Localitate": "Sector 1", "scod_JudetAuto": "B"},
    }


ANAF = {
    1590082: anaf_found(1590082, "OMV PETROM MARKETING SRL"),
    2222222: anaf_found(2222222, "FIRMA INACTIVA SRL", inactiv=True),
    43424557: anaf_found(43424557, "ANGAJEAZA UN ROBOT SRL"),
}


class Saga(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _auth(self) -> bool:
        ok = self.headers.get("Authorization") == f"Bearer {state['key']}"
        if not ok:
            self._send(401, {"success": False, "message": "cheie invalida"})
        return ok

    def _send(self, code, body, ctype="application/json", extra=None):
        raw = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        state["log"].append(("GET", u.path, q, self.headers.get("X-Saga-Cod-Fiscal")))
        if not self._auth():
            return
        if u.path.endswith("/Situatii/GetBalanta") and q.get("tipBalanta") == "1":
            self._send(200, BALANTA_ANALITICA, "application/xml")
        elif u.path.endswith("/Situatii/GetBalanta"):
            # simulam rotirea cheii la acest apel
            state["rotations"] += 1
            state["key"] = f"KEY-{state['rotations'] + 1}"
            self._send(200, BALANTA, "application/xml", {"X-Saga-Refresh-Token": state["key"]})
        elif u.path.endswith("/Situatii/GetSoldCont"):
            self._send(200, {"success": True, "message": "", "data": "24571.00"})
        elif u.path.endswith("/Situatii/GetStocArticol"):
            if "data" in q and q["data"].count(".") != 2:
                self._send(400, {"success": False, "error": "Data trebuie sa fie in formatul dd.MM.yyyy."})
            else:
                self._send(200, {"success": True, "message": "", "data": "174852"})
        else:
            self._send(404, {"success": False, "message": "nu exista"})

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        body = self.rfile.read(n)
        if self.path == "/anaf":
            req = json.loads(body)
            state["log"].append(("ANAF", self.path, [r["cui"] for r in req], None))
            found = [ANAF[r["cui"]] for r in req if r["cui"] in ANAF]
            notfound = [r["cui"] for r in req if r["cui"] not in ANAF]
            self._send(200, {"found": found, "notFound": notfound})
            return
        msg = BytesParser(policy=email_policy).parsebytes(
            b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body
        )
        parts = {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()}
        state["log"].append(("POST", self.path, {
            "file": parts["file"].get_filename(),
            "importAutomat": parts["importAutomat"].get_content().strip(),
        }, self.headers.get("X-Saga-Cod-Fiscal")))
        if not self._auth():
            return
        self._send(200, {"success": True, "message": "Import realizat cu succes."})


async def main():
    srv = HTTPServer(("127.0.0.1", PORT), Saga)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    data_dir = tempfile.mkdtemp()
    keyring.set_password(SERVICE, "default", "KEY-1")
    env = dict(
        os.environ,
        SAGA_API_URL=f"http://127.0.0.1:{PORT}/api/v20260225",
        SAGA_KEYRING_SERVICE=SERVICE,
        SAGA_DATA_DIR=data_dir,
        SAGA_ANAF_URL=f"http://127.0.0.1:{PORT}/anaf",
    )
    params = StdioServerParameters(command=sys.executable, args=["-m", "saga_mcp"], env=env)
    failures = []

    def check(cond, label):
        print(("OK   " if cond else "FAIL ") + label)
        if not cond:
            failures.append(label)

    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = {t.name for t in (await s.list_tools()).tools}
                print("Unelte:", sorted(tools))

                async def call(name, args):
                    res = await s.call_tool(name, args)
                    txt = "".join(getattr(c, "text", "") for c in res.content)
                    return res.isError, (json.loads(txt) if not res.isError else txt)

                err, out = await call("saga_balanta", {"cif": "RO43424557", "data_start": "2026-01-01",
                                                         "data_end": "31.10.2026", "conturi": ["44", "53"]})
                check(not err and [r_["CONT"] for r_ in out["randuri"]] == [4423, 5311], "balanta filtrata pe conturi")
                check(state["log"][-1][2] == {"dataStart": "01.01.2026", "dataEnd": "31.10.2026", "tipBalanta": "0"},
                      "parametri balanta in format dd.MM.yyyy")
                check(state["log"][-1][3] == "43424557", "header X-Saga-Cod-Fiscal fara RO")
                check(keyring.get_password(SERVICE, "default") == "KEY-2", "cheia rotita a fost salvata")

                err, out = await call("saga_sold_cont", {"cif": "43424557", "cont": "5311", "data": "2026-11-16"})
                check(not err and out["sold"] == 24571.0, "sold cont cu cheia noua (dupa rotire)")

                err, out = await call("saga_stoc_articol", {"cif": "43424557", "cod_articol": "A1"})
                check(not err and out["stoc"] == 174852, "stoc articol")

                err, out = await call("saga_pregateste_import", {"cif": "43424557", "tip": "NC", "data_fisier": "2026-10-31",
                    "elemente": [{"Data": "31.10.2026", "Numar": "12", "Suma": 150.5, "ContDebit": "6022",
                                  "ContCredit": "5311", "Explicatie": "Combustibil & ulei"}]})
                check(not err and out["fisier"].endswith("NC_31.10.2026.xml") and not out["avertismente"]
                      and "&amp;" in out["xml"], "pregatire NC (nume, escape XML)")
                nc_path = out["fisier"]

                err, out = await call("saga_pregateste_import", {"cif": "43424557", "tip": "F", "elemente": [{
                    "antet": {"FurnizorNume": "ALT SRL", "FurnizorCIF": "123", "ClientNume": "ANGAJEAZA UN ROBOT SRL",
                              "ClientCIF": "43424557", "ClientTara": "RO", "FacturaNumar": "A-77",
                              "FacturaData": "15.10.2026", "FacturaScadenta": "30.10.2026",
                              "FacturaTaxareInversa": "Nu", "FacturaTVAIncasare": "Nu"},
                    "linii": [{"Descriere": "Servicii", "UM": "BUC", "Cantitate": 1, "Pret": 100,
                               "Valoare": 100, "ProcTVA": 21, "TVA": 21}]}]})
                check(not err and os.path.basename(out["fisier"]) == "F_43424557_A-77_15.10.2026.xml",
                      "pregatire factura (nume fisier)")

                err, out = await call("saga_importa", {"cale_fisier": nc_path, "cif": "43424557"})
                check(not err and out["stare"] == "neconfirmat" and out["rezumat"]["total_suma"] == 150.5,
                      "import pas 1: rezumat fara trimitere")
                check(state["log"][-1][0] != "POST", "pasul 1 nu a trimis nimic")
                token = out["token_confirmare"]

                err, out = await call("saga_importa", {"cale_fisier": nc_path, "cif": "43424557",
                                                        "token_confirmare": "gresit"})
                check(err, "token gresit refuzat")

                err, out = await call("saga_importa", {"cale_fisier": nc_path, "cif": "43424557",
                                                        "import_automat": True, "token_confirmare": token})
                check(err and "dezactivat" in out, "import automat blocat fara SAGA_ALLOW_AUTO_IMPORT")

                err, out = await call("saga_importa", {"cale_fisier": nc_path, "cif": "43424557",
                                                        "token_confirmare": token})
                check(not err and out["http"] == 200, "import pas 2 trimis")
                check(state["log"][-1][2] == {"file": "NC_31.10.2026.xml", "importAutomat": "false"},
                      "multipart: file + importAutomat=false")

                # ---------------- furnizori + bonuri ----------------
                err, out = await call("saga_furnizori", {"cif": "43424557", "actualizeaza_din_balanta": True,
                                                          "data": "2026-11-16"})
                check(not err and out["furnizori_din_balanta"] == 1, "furnizori din balanta analitica (doar 401.x)")

                bonuri = [
                    {"furnizor_cif": "RO1590082", "furnizor_nume": "OMV", "numar": "0451", "data": "03.11.2026",
                     "plata": "card", "linii": [{"total": 250, "proc_tva": 21, "descriere": "Motorina",
                                                  "cont": "6022", "tip_deducere": "N50"}]},
                    {"furnizor_cif": "", "furnizor_nume": "Dedeman S.R.L.", "numar": "77", "data": "2026-11-05",
                     "plata": "numerar", "total": 89.90, "proc_tva": 21},
                    {"furnizor_cif": "2222222", "numar": "9", "data": "07.11.2026", "plata": "angajat",
                     "total": 121, "proc_tva": 21},
                    {"furnizor_cif": "RO1590082", "numar": "0451", "data": "03.11.2026", "plata": "card",
                     "total": 250, "proc_tva": 21},
                ]
                err, out = await call("saga_pregateste_bonuri", {"cif": "43424557", "bonuri": bonuri})
                check(not err, f"pregatire bonuri {'' if not err else out}")
                if not err:
                    t = {r["numar"]: r for r in out["bonuri"]}
                    check(len(out["bonuri"]) == 3 and any("de doua ori" in w for w in out["avertismente"]),
                          "bon duplicat in lot eliminat")
                    check(t["0451"]["total"] == 250 and t["0451"]["tva"] == 43.39 and t["0451"]["cont_plata"] == "5121",
                          "bon card: baza+TVA, plata pe 5121")
                    check(t["77"]["cont_plata"] == "5311" and t["9"]["cont_plata"] == "542", "numerar 5311, angajat 542")
                    check(any("INACTIV" in o for o in t["9"]["observatii"]), "furnizor inactiv semnalat")
                    check({f["denumire"] for f in out["furnizori_existenti"]} == {"Dedeman S.R.L."},
                          "Dedeman recunoscut din balanta dupa denumire")
                    check({f["cif"] for f in out["furnizori_noi"]} == {"1590082", "2222222"}, "furnizori noi")
                    f_file, p_file = out["fisiere"][0]["fisier"], out["fisiere"][1]["fisier"]
                    fx = open(f_file, encoding="utf-8").read()
                    check(fx.count("<Factura>") == 3 and fx.count("<FacturaTip>B</FacturaTip>") == 3,
                          "fisier F: 3 bonuri tip B")
                    check("<FurnizorNume>OMV PETROM MARKETING SRL</FurnizorNume>" in fx
                          and "<FurnizorNrRegCom>J40/1590082/2000" in fx,
                          "denumire oficiala si date furnizor completate de la ANAF")
                    check("<TipDeducere>I</TipDeducere>" in fx and "<TipDeducere>N50</TipDeducere>" in fx,
                          "deducere: I la inactiv, N50 la combustibil")
                    px = open(p_file, encoding="utf-8").read()
                    check(px.count("<Linie>") == 3 and "<FacturaID>BON-1590082-0451-20261103</FacturaID>" in px,
                          "fisier P: plati legate prin FacturaID")

                    err, o1 = await call("saga_importa", {"cale_fisier": f_file, "cif": "43424557"})
                    err, o2 = await call("saga_importa", {"cale_fisier": f_file, "cif": "43424557",
                                                           "token_confirmare": o1["token_confirmare"]})
                    check(not err and o2["http"] == 200, "import bonuri (F)")
                    err, out = await call("saga_pregateste_bonuri", {"cif": "43424557", "bonuri": bonuri[:3]})
                    check(not err and out.get("stare") == "nimic de importat", "bonurile importate nu se reintroduc")
                    err, out = await call("saga_furnizori", {"cif": "43424557", "verifica": [{"cif": "1590082"}]})
                    check(not err and out["verificare"][0]["gasit"]["potrivire"] == "cif",
                          "furnizorul importat devine cunoscut (dupa CIF)")

                # ---------------- extras de cont ----------------
                tranz = [
                    {"data": "2026-11-03", "suma": 1000, "categorie": "incasare_client", "partener_nume": "Client A",
                     "partener_cif": "123", "factura_numar": "ROB-12", "referinta": "OP 55", "explicatie": "Contravaloare ROB-12"},
                    {"data": "2026-11-04", "suma": -300, "categorie": "plata_furnizor", "partener_nume": "Furnizor B",
                     "referinta": "OP 56", "explicatie": "Plata fact 9"},
                    {"data": "2026-11-04", "suma": -5, "categorie": "comision_bancar", "explicatie": "Comision OP"},
                    {"data": "2026-11-25", "suma": -830, "categorie": "plata_stat", "cont_corespondent": "4423",
                     "explicatie": "TVA octombrie"},
                    {"data": "2026-11-26", "suma": -200, "categorie": "retragere_numerar", "explicatie": "Ridicare numerar"},
                ]
                err, out = await call("saga_pregateste_extras", {"cif": "43424557", "tranzactii": tranz,
                                                                  "sold_initial": 24571, "sold_final": 24236})
                check(not err, f"pregatire extras {'' if not err else out}")
                if not err:
                    check(out["verificari"]["extras_complet"] and out["verificari"]["sold_saga_inainte"]["diferenta"] == 0,
                          "verificare sold extras si sold SAGA")
                    check(state["log"][-1][2]["data"] == "02.11.2026", "sold SAGA citit in ziua dinaintea extrasului")
                    tips = {os.path.basename(f["fisier"]).split("_")[0]: f for f in out["fisiere"]}
                    check(set(tips) == {"I", "P", "NC"}, "extras impartit in I, P, NC")
                    ncx = open(tips["NC"]["fisier"], encoding="utf-8").read()
                    check(ncx.count("<Linie>") == 4 and "<ContDebit>4423</ContDebit>" in ncx
                          and "<ContDebit>5311</ContDebit>" in ncx, "note contabile: comision, TVA, numerar prin 581")
                    ix = open(tips["I"]["fisier"], encoding="utf-8").read()
                    check("<ContClient>4111</ContClient>" in ix and "<FacturaNumar>ROB-12</FacturaNumar>" in ix,
                          "incasare legata de factura")
                err, out = await call("saga_pregateste_extras", {"cif": "43424557", "tranzactii": tranz,
                                                                  "sold_initial": 24571, "sold_final": 99999,
                                                                  "verifica_sold_in_saga": False})
                check(not err and out["verificari"]["extras_complet"] is False, "extras incomplet semnalat")
                err, out = await call("saga_pregateste_extras", {"cif": "43424557", "tranzactii": [
                    {"data": "2026-11-25", "suma": -10, "categorie": "plata_stat", "explicatie": "x"}]})
                check(err and "cont_corespondent" in out, "plata_stat fara cont refuzata")
                err, out = await call("saga_compara_sold", {"cif": "43424557", "cont": "5121", "data": "2026-11-30",
                                                             "sold_asteptat": 24236})
                check(not err and out["diferenta"] == 335 and out["concorda"] is False, "compara sold")
    finally:
        for slot in ("default",):
            try:
                keyring.delete_password(SERVICE, slot)
            except Exception:
                pass
        srv.shutdown()

    print("\nTOATE TESTELE AU TRECUT" if not failures else f"\n{len(failures)} TESTE ESUATE")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())

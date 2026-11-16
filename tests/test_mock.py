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
        if u.path.endswith("/Situatii/GetBalanta"):
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

"""Uneltele MCP pentru SAGA Web."""

from __future__ import annotations

import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from . import anaf, client, closing, formats, keystore, workflows
from .registry import Registry

DATA_DIR = Path(os.environ.get("SAGA_DATA_DIR", Path.home() / "Documents" / "SAGA"))
ALLOW_AUTO_IMPORT = os.environ.get("SAGA_ALLOW_AUTO_IMPORT") == "1"
MAX_ROWS = int(os.environ.get("SAGA_MAX_ROWS", "500"))

mcp = FastMCP(
    "saga",
    instructions=(
        "Acces la contabilitatea din SAGA Web pentru una sau mai multe firme; fiecare "
        "unealta primeste codul fiscal (cif) al firmei. Citirile (balanta, solduri, "
        "stocuri) nu modifica nimic. Importul scrie in contabilitate: pregateste fisierul "
        "cu saga_pregateste_import, arata utilizatorului ce se importa, apoi trimite cu "
        "saga_importa doar dupa acordul lui explicit. Formatele XML sunt in resursa "
        "saga://formate-import. Conturi utile: 5311 casa lei, 5121 banca lei, 4111 clienti, "
        "401 furnizori, 4423 TVA de plata, 4424 TVA de recuperat, 4426 TVA deductibila, "
        "4427 TVA colectata, 4428 TVA neexigibila, 121 profit/pierdere."
    ),
)


def _firm_dir(cif: str, *sub: str) -> Path:
    path = DATA_DIR.joinpath(cif, *sub)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _registry(cif: str) -> Registry:
    return Registry(_firm_dir(cif))


def _unique(folder: Path, name: str) -> Path:
    """Nu suprascrie un fisier de import existent: adauga -2, -3 ... inainte de .xml."""
    path = folder / name
    n = 2
    while path.exists():
        path = folder / f"{Path(name).stem}-{n}.xml"
        n += 1
    return path


def _write_import(cif: str, tip: str, items: list[dict], data_fisier: str | None, manifest: dict) -> dict:
    """Scrie fisierul de import si, alaturi, <fisier>.json cu ce contine (pentru registru)."""
    name, xml_text, warnings = formats.build(tip, cif, items, data_fisier)
    path = _unique(_firm_dir(cif, "import"), name)
    path.write_text(xml_text, encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps({"tip": tip, **manifest}, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    return {"fisier": str(path), "tip": tip, "elemente": len(items), "avertismente_format": warnings}


def _num(text: str | None):
    if text is None:
        return None
    t = text.strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", t):
        return float(t) if "." in t else int(t)
    return t


def _xml_rows(xml_text: str) -> tuple[str, list[dict]]:
    """Transforma un XML de tip <Radacina><Rand><CAMP>..</CAMP></Rand>...</Radacina> in randuri."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    rows = []
    for child in root:
        if len(child):
            rows.append({c.tag: _num(c.text) for c in child})
        else:
            rows.append({child.tag: _num(child.text)})
    return root.tag, rows


@mcp.tool()
async def saga_status(cif: str | None = None) -> dict:
    """Arata configurarea: adresa API, folderul de date si daca exista cheie de acces
    (implicita si/sau pentru firma data). Nu face nicio cerere la SAGA."""
    out = {
        "api": client.BASE_URL,
        "folder_date": str(DATA_DIR),
        "cheie_implicita": keystore.has_key(keystore.DEFAULT_SLOT),
        "import_automat_permis": ALLOW_AUTO_IMPORT,
    }
    if cif:
        c = client.norm_cif(cif)
        out["cif"] = c
        out["cheie_dedicata_firmei"] = keystore.has_key(keystore.slot_for(c))
    return out


@mcp.tool()
async def saga_balanta(
    cif: str,
    data_start: str,
    data_end: str,
    analitica: bool = False,
    conturi: list[str] | None = None,
) -> dict:
    """Balanta de verificare pe o perioada (sintetica sau analitica).

    Fisierul XML complet se salveaza in folderul firmei; in raspuns intra randurile,
    optional filtrate dupa prefixe de cont.

    Args:
        cif: codul fiscal al firmei.
        data_start: inceputul perioadei (AAAA-LL-ZZ sau ZZ.LL.AAAA).
        data_end: sfarsitul perioadei.
        analitica: true pentru balanta analitica (implicit sintetica).
        conturi: prefixe de cont de pastrat, ex. ["4423", "5311", "6", "7"].
    """
    c = client.norm_cif(cif)
    ds, de = client.norm_date(data_start), client.norm_date(data_end)
    xml_text = await client.get_xml(
        "Situatii/GetBalanta", c,
        {"dataStart": ds, "dataEnd": de, "tipBalanta": 1 if analitica else 0},
    )
    tip = "analitica" if analitica else "sintetica"
    saved = _firm_dir(c, "balante") / f"balanta_{tip}_{ds}_{de}.xml"
    saved.write_text(xml_text, encoding="utf-8")

    _, rows = _xml_rows(xml_text)
    if conturi:
        prefixes = tuple(str(p) for p in conturi)
        rows = [r for r in rows if str(r.get("CONT", r.get("Cont", ""))).startswith(prefixes)]
    total = len(rows)
    truncated = total > MAX_ROWS
    return {
        "cif": c,
        "perioada": f"{ds} - {de}",
        "tip": tip,
        "randuri": rows[:MAX_ROWS],
        "numar_randuri": total,
        "trunchiat": truncated,
        "fisier_xml": str(saved),
        **({"nota": f"Afisate primele {MAX_ROWS}; filtreaza cu `conturi` sau citeste fisierul."} if truncated else {}),
    }


@mcp.tool()
async def saga_sold_cont(
    cif: str,
    cont: str,
    data: str | None = None,
    tip: Literal["D", "C"] = "D",
) -> dict:
    """Soldul unui cont la o data (implicit azi).

    Args:
        cif: codul fiscal al firmei.
        cont: contul, ex. "5311" (casa), "5121" (banca), "4111", "401", "4423".
        data: data soldului (AAAA-LL-ZZ, ZZ.LL.AAAA sau "azi"); implicit azi.
        tip: "D" sold debitor (conturi de activ: casa, banca, clienti) sau
            "C" sold creditor (conturi de pasiv: furnizori, TVA de plata, capital).
    """
    c = client.norm_cif(cif)
    d = client.norm_date(data) or client.norm_date("azi")
    resp = await client.get_json("Situatii/GetSoldCont", c, {"data": d, "cont": cont, "tip": tip})
    return {"cif": c, "cont": cont, "data": d, "tip_sold": tip, "sold": _num(str(resp.get("data")))}


@mcp.tool()
async def saga_stoc_articol(
    cif: str,
    cod_articol: str,
    gestiune: str | None = None,
    data: str | None = None,
) -> dict:
    """Stocul unui articol, optional pe o gestiune, la o data (implicit azi).

    Args:
        cif: codul fiscal al firmei.
        cod_articol: codul articolului din SAGA.
        gestiune: codul gestiunii (gol = toate).
        data: data stocului; implicit azi.
    """
    c = client.norm_cif(cif)
    d = client.norm_date(data)
    resp = await client.get_json(
        "Situatii/GetStocArticol", c, {"codArticol": cod_articol, "gestiune": gestiune, "data": d}
    )
    return {"cif": c, "cod_articol": cod_articol, "gestiune": gestiune, "data": d or "azi",
            "stoc": _num(str(resp.get("data")))}


@mcp.tool()
async def saga_situatie_stocuri(cif: str, data_start: str, data_end: str) -> dict:
    """Situatia stocurilor pe o perioada. XML-ul complet se salveaza in folderul firmei.

    Args:
        cif: codul fiscal al firmei.
        data_start: inceputul perioadei.
        data_end: sfarsitul perioadei.
    """
    c = client.norm_cif(cif)
    ds, de = client.norm_date(data_start), client.norm_date(data_end)
    xml_text = await client.get_xml("Situatii/GetSituatieStocuri", c, {"dataStart": ds, "dataEnd": de})
    saved = _firm_dir(c, "stocuri") / f"stocuri_{ds}_{de}.xml"
    saved.write_text(xml_text, encoding="utf-8")
    _, rows = _xml_rows(xml_text)
    return {"cif": c, "perioada": f"{ds} - {de}", "randuri": rows[:MAX_ROWS],
            "numar_randuri": len(rows), "trunchiat": len(rows) > MAX_ROWS, "fisier_xml": str(saved)}


@mcp.tool()
async def saga_articol(cif: str, cod_articol: str) -> dict:
    """Fisa unui articol (denumire, UM, TVA, preturi etc.).

    Args:
        cif: codul fiscal al firmei.
        cod_articol: codul articolului din SAGA.
    """
    c = client.norm_cif(cif)
    xml_text = await client.get_xml("Situatii/GetArticol", c, {"codArticol": cod_articol})
    root_tag, rows = _xml_rows(xml_text)
    if not rows:
        root = ET.fromstring(xml_text.encode("utf-8"))
        rows = [{root.tag: _num(root.text)}]
    return {"cif": c, "cod_articol": cod_articol, "articol": rows[0] if len(rows) == 1 else rows}


@mcp.tool()
async def saga_pregateste_import(
    cif: str,
    tip: Literal["F", "C", "I", "P", "NC", "CLI", "ART"],
    elemente: list[dict],
    data_fisier: str | None = None,
) -> dict:
    """Construieste un fisier XML de import SAGA si il salveaza local. NU trimite nimic.

    Tipuri: F facturi, C comenzi, I incasari, P plati, NC note contabile,
    CLI clienti, ART articole. Campurile sunt in resursa saga://formate-import.

    Args:
        cif: codul fiscal al firmei in care se importa.
        tip: tipul fisierului.
        elemente: pentru I, P, NC, CLI, ART - lista de linii, ex. pentru NC
            [{"Data": "31.10.2026", "Numar": "12", "Suma": 150.0, "ContDebit": "6022",
              "ContCredit": "5311", "Explicatie": "Combustibil"}].
            Pentru F si C - lista de documente {"antet": {...}, "linii": [{...}], "id": "..."};
            un document per fisier (numele fisierului contine numarul si data documentului).
        data_fisier: data din numele fisierului pentru I/P/NC/CLI/ART (implicit azi).
    """
    c = client.norm_cif(cif)
    name, xml_text, warnings = formats.build(tip, c, elemente, client.norm_date(data_fisier) if data_fisier else None)
    path = _firm_dir(c, "import") / name
    path.write_text(xml_text, encoding="utf-8")
    preview = xml_text if len(xml_text) < 6000 else xml_text[:6000] + "\n... (trunchiat)"
    return {"fisier": str(path), "tip": tip, "avertismente": warnings, "xml": preview,
            "pas_urmator": "Arata utilizatorului continutul, apoi saga_importa(cale_fisier, cif)."}


def _token(data: bytes, cif: str, auto: bool) -> str:
    return hashlib.sha256(data + cif.encode() + (b"A" if auto else b"M")).hexdigest()[:16]


def _summarize(tip: str | None, root: ET.Element) -> dict:
    summary: dict = {"radacina": root.tag}
    if tip in ("F", "C"):
        docs = list(root)
        summary["documente"] = len(docs)
        detalii = []
        for d in docs[:20]:
            antet = d.find("Antet")
            linii = d.findall("./Detalii/Continut/Linie")
            val = sum(float(l.findtext("Valoare") or 0) for l in linii)
            tva = sum(float(l.findtext("TVA") or 0) for l in linii)
            g = (lambda t: antet.findtext(t) if antet is not None else None)
            detalii.append({
                "numar": g("FacturaNumar") or g("ComandaNumar"),
                "data": g("FacturaData") or g("ComandaData"),
                "furnizor": f"{g('FurnizorNume')} ({g('FurnizorCIF')})",
                "client": f"{g('ClientNume')} ({g('ClientCIF')})",
                "linii": len(linii), "valoare": round(val, 2), "tva": round(tva, 2),
                "total": round(val + tva, 2),
            })
        summary["detalii"] = detalii
    else:
        rows = list(root)
        summary["linii"] = len(rows)
        sume = [float(r.findtext("Suma") or 0) for r in rows if r.find("Suma") is not None]
        if sume:
            summary["total_suma"] = round(sum(sume), 2)
        summary["primele_linii"] = [{c.tag: c.text for c in r} for r in rows[:10]]
    return summary


@mcp.tool()
async def saga_importa(
    cale_fisier: str,
    cif: str,
    token_confirmare: str | None = None,
    import_automat: bool = False,
) -> dict:
    """Importa un fisier XML in SAGA Web, in doi pasi.

    Pasul 1 (fara token): verifica fisierul si intoarce un rezumat plus un
    token_confirmare. Arata rezumatul utilizatorului si cere acordul explicit.
    Pasul 2: apeleaza din nou cu acelasi fisier si token_confirmare. Tokenul e legat
    de continutul exact al fisierului; daca fisierul se schimba, se reia pasul 1.

    Implicit importul ajunge in SAGA la "Diverse > Import date" si trebuie finalizat
    manual acolo de utilizator. import_automat=true il finalizeaza direct si e permis
    doar daca serverul are SAGA_ALLOW_AUTO_IMPORT=1.

    Args:
        cale_fisier: calea fisierului XML (numit dupa conventia SAGA, ex. NC_31.10.2026.xml).
        cif: codul fiscal al firmei in care se importa.
        token_confirmare: tokenul din pasul 1, doar dupa acordul utilizatorului.
        import_automat: finalizeaza importul fara pasul manual din SAGA.
    """
    c = client.norm_cif(cif)
    path = Path(cale_fisier).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Nu exista fisierul: {path}")
    if import_automat and not ALLOW_AUTO_IMPORT:
        raise RuntimeError(
            "Importul automat e dezactivat. Foloseste importul obisnuit (finalizat manual in "
            "SAGA) sau porneste serverul cu SAGA_ALLOW_AUTO_IMPORT=1."
        )
    data = path.read_bytes()
    tip = formats.detect_type(path.name)
    warnings = []
    if tip is None:
        warnings.append(
            "Numele fisierului nu respecta conventia SAGA (F_<cif>_<nr>_<data>.xml, NC_<data>.xml etc.)."
        )
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"XML invalid: {exc}") from exc
    if tip and formats.ROOTS.get(root.tag) not in (tip, None):
        warnings.append(f"Radacina <{root.tag}> nu corespunde tipului {tip} din numele fisierului.")
    if tip in ("F", "C"):
        name_cif = path.name.split("_")[1]
        if name_cif != c:
            warnings.append(f"Codul fiscal din numele fisierului ({name_cif}) difera de firma ({c}).")

    expected = _token(data, c, import_automat)
    if token_confirmare is None:
        return {
            "stare": "neconfirmat",
            "fisier": str(path),
            "firma": c,
            "tip": tip,
            "import_automat": import_automat,
            "rezumat": _summarize(tip, root),
            "avertismente": warnings,
            "token_confirmare": expected,
            "pas_urmator": "Arata rezumatul utilizatorului; dupa acordul lui explicit apeleaza "
            "din nou cu token_confirmare.",
        }
    if token_confirmare != expected:
        raise RuntimeError("Token invalid: fisierul, firma sau modul de import s-au schimbat. Reia pasul 1.")

    resp = await client.request(
        "POST", "Import", c,
        files={"file": (path.name, data, "text/xml")},
        data={"importAutomat": "true" if import_automat else "false"},
    )
    result = {"fisier": str(path), "firma": c, "http": resp.status}
    if resp.status in (200, 207):
        _record_import(c, path)
    if resp.status == 200:
        result["stare"] = "importat" if import_automat else "incarcat - de finalizat in SAGA (Diverse > Import date)"
        try:
            result["mesaj"] = resp.json().get("message")
        except Exception:
            pass
    elif resp.status in (207, 422):
        report = _firm_dir(c, "import") / f"raport_{path.stem}.txt"
        report.write_bytes(resp.body)
        result["stare"] = "partial" if resp.status == 207 else "esuat"
        result["raport"] = resp.text[:3000]
        result["fisier_raport"] = str(report)
    else:
        result["stare"] = "eroare"
        result["mesaj"] = client._error_message(resp)
    return result


def _record_import(cif: str, path: Path) -> None:
    """Dupa un import reusit, tine minte furnizorii si documentele din fisier."""
    manifest_path = path.with_suffix(".json")
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    reg = _registry(cif)
    for f in manifest.get("furnizori", []):
        reg.add_supplier(f["cif"], f.get("denumire", ""), sursa="import", nr_reg_com=f.get("nr_reg_com"))
    if manifest.get("ids"):
        reg.mark_imported(manifest["ids"], manifest.get("tip", ""), path.name)
    reg.save()


async def _firm_info(c: str, firma: dict | None) -> dict:
    if firma and firma.get("denumire"):
        return {"cif": c, **firma}
    try:
        info = (await anaf.lookup([c])).get(c)
    except Exception:  # noqa: BLE001
        info = None
    return {"cif": c, **(info or {})}


SUPPLIER_ACCOUNTS = ("401", "404", "408", "462")


@mcp.tool()
async def saga_furnizori(
    cif: str,
    actualizeaza_din_balanta: bool = False,
    data: str | None = None,
    verifica: list[dict] | None = None,
) -> dict:
    """Furnizorii cunoscuti ai unei firme si verificarea unor furnizori (exista / sunt noi).

    API-ul SAGA nu are lista de parteneri, asa ca sursele sunt:
    - registrul local (furnizorii importati prin acest server, cu CIF);
    - balanta analitica pe 401/404/408/462 (denumiri), daca actualizeaza_din_balanta=true.
    La import, SAGA potriveste furnizorul dupa CIF; verificarea de aici e informativa.

    Args:
        cif: codul fiscal al firmei.
        actualizeaza_din_balanta: citeste balanta analitica de la 1 ianuarie pana la `data`
            si actualizeaza lista din balanta.
        data: data de sfarsit pentru balanta (implicit azi).
        verifica: furnizori de verificat, ex. [{"cif": "1590082", "nume": "OMV PETROM"}].
    """
    c = client.norm_cif(cif)
    reg = _registry(c)
    note = None
    if actualizeaza_din_balanta:
        de = client.norm_date(data) or client.norm_date("azi")
        ds = "01.01." + de[-4:]
        xml_text = await client.get_xml(
            "Situatii/GetBalanta", c, {"dataStart": ds, "dataEnd": de, "tipBalanta": 1})
        saved = _firm_dir(c, "balante") / f"balanta_analitica_{ds}_{de}.xml"
        saved.write_text(xml_text, encoding="utf-8")
        _, rows = _xml_rows(xml_text)
        found = []
        for r in rows:
            cont = str(r.get("CONT", r.get("Cont", "")))
            if cont.startswith(SUPPLIER_ACCOUNTS) and ("." in cont or len(cont) > 4):
                found.append({"cont": cont, "denumire": str(r.get("DENUMIRE", r.get("Denumire", "")))})
        reg.set_balance_suppliers(found)
        reg.save()
        note = (f"{len(found)} conturi analitice de furnizori in balanta. Daca numarul e 0, "
                f"verifica structura balantei in {saved}.")
    result = {
        "cif": c,
        "furnizori_cu_cif": len(reg.data["furnizori"]),
        "furnizori_din_balanta": len(reg.data["din_balanta"]),
        "lista": [{"cif": k, **v} for k, v in list(reg.data["furnizori"].items())[:200]],
    }
    if note:
        result["nota"] = note
    if verifica:
        result["verificare"] = [
            {"cautat": v, "gasit": reg.find_supplier(workflows.digits(v.get("cif")), v.get("nume"))}
            for v in verifica
        ]
    return result


@mcp.tool()
async def saga_pregateste_bonuri(
    cif: str,
    bonuri: list[dict],
    cont_implicit: str = "6028",
    completeaza_din_anaf: bool = True,
    conturi_plata: dict | None = None,
    firma: dict | None = None,
) -> dict:
    """Pregateste importul bonurilor fiscale de cheltuieli: furnizori, bonuri si plati.
    NU trimite nimic in SAGA.

    Fluxul: citesti bonurile (din poze), arati utilizatorului tabelul, apoi apelezi
    aceasta unealta cu bonurile confirmate. Rezulta doua fisiere:
    1. F_... : bonurile ca documente de intrare tip "B", fiecare cu un FacturaID unic.
       Furnizorii noi se creeaza in SAGA odata cu documentul (datele vin de la ANAF).
    2. P_... : platile legate de bonuri prin FacturaID (bonurile apar achitate), pe
       contul dat de modul de plata: numerar 5311, card 5121, angajat 542.
    Ordinea de import: intai F (si finalizat in SAGA), apoi P.

    Bonurile deja importate (acelasi furnizor, numar si data) sunt sarite.

    Args:
        cif: codul fiscal al firmei care a cumparat.
        bonuri: lista de bonuri. Fiecare bon:
            furnizor_cif (obligatoriu daca e pe bon), furnizor_nume, numar, data,
            plata: "numerar" | "card" | "angajat" | "neplatit",
            linii: [{"total": 121.0, "proc_tva": 21, "descriere": "Combustibil",
                     "cont": "6022", "tip_deducere": "N50"}]  (total = cu TVA, cate o linie
                     pe cota de TVA) SAU direct total + proc_tva pentru bon cu o singura cota,
            cont (contul de cheltuiala al bonului), cif_cumparator_pe_bon (false => TVA
            nedeductibil), observatii, cont_plata (suprascrie contul de plata).
        cont_implicit: contul de cheltuiala cand bonul/linia nu are cont (implicit 6028).
        completeaza_din_anaf: completeaza datele furnizorilor si ale firmei din registrul
            public ANAF si semnaleaza furnizorii inactivi.
        conturi_plata: suprascrie conturile de plata, ex. {"card": "5124", "angajat": "542.01"}.
        firma: datele firmei (denumire, nr_reg_com, adresa, localitate, judet), daca nu
            vrei sa fie luate de la ANAF.
    """
    c = client.norm_cif(cif)
    reg = _registry(c)
    skipped, todo, seen = [], [], set()
    warnings = []
    for b in bonuri:
        did = workflows.bon_id(workflows.digits(b.get("furnizor_cif")), b["numar"], workflows.ddmmyyyy(b["data"]))
        if did in seen:
            warnings.append(f"Bonul {b['numar']} din {b['data']} apare de doua ori in lot; pastrat o data.")
            continue
        seen.add(did)
        (skipped if reg.is_imported(did) else todo).append((did, b))
    warnings += [f"Bonul {b['numar']} din {b['data']} a fost deja importat ({d}); sarit." for d, b in skipped]
    if not todo:
        return {"stare": "nimic de importat", "avertismente": warnings}

    supplier_cifs = [workflows.digits(b.get("furnizor_cif")) for _, b in todo]
    furnizori: dict[str, dict] = {}
    if completeaza_din_anaf:
        try:
            furnizori = await anaf.lookup([x for x in supplier_cifs if x])
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"ANAF indisponibil ({exc}); furnizorii au doar datele de pe bon.")
    firm = await _firm_info(c, firma) if completeaza_din_anaf else {"cif": c, **(firma or {})}

    plata_conturi = {**workflows.PAYMENT_ACCOUNTS, **(conturi_plata or {})}
    docs, plati, tabel, w = workflows.build_bon_documents(
        [b for _, b in todo], firm, furnizori, cont_implicit, plata_conturi)
    warnings += w

    noi, existenti, vazuti = [], [], set()
    for row in tabel:
        if row["cif"] in vazuti:
            continue
        vazuti.add(row["cif"])
        match = reg.find_supplier(row["cif"], row["furnizor"])
        (existenti if match else noi).append({"cif": row["cif"], "denumire": row["furnizor"],
                                              **({"potrivire": match["potrivire"]} if match else {})})

    last_date = max(d["antet"]["FacturaData"] for d in docs)
    furnizori_manifest = [{"cif": d["antet"]["FurnizorCIF"], "denumire": d["antet"]["FurnizorNume"],
                           "nr_reg_com": d["antet"]["FurnizorNrRegCom"]} for d in docs if d["antet"]["FurnizorCIF"]]
    fisiere = [_write_import(c, "F", docs, None, {"ids": [d["id"] for d in docs], "furnizori": furnizori_manifest})]
    if plati:
        fisiere.append(_write_import(c, "P", plati, last_date, {"ids": [f"PLATA-{p['FacturaID']}" for p in plati]}))

    return {
        "firma": {"cif": c, "denumire": firm.get("denumire", "")},
        "bonuri": tabel,
        "total_general": workflows.r2(sum(r["total"] for r in tabel)),
        "pe_mod_plata": {m: workflows.r2(sum(r["total"] for r in tabel if r["plata"] == m))
                         for m in sorted({r["plata"] for r in tabel})},
        "furnizori_noi": noi,
        "furnizori_existenti": existenti,
        "fisiere": fisiere,
        "avertismente": warnings,
        "pasi_urmatori": [
            "Arata utilizatorului tabelul si avertismentele.",
            "saga_importa pe fisierul F_ (doi pasi, cu acordul utilizatorului), apoi utilizatorul "
            "finalizeaza importul in SAGA (Diverse > Import date).",
            "Abia dupa aceea saga_importa pe fisierul P_ (platile), ca sa se lege de bonuri.",
        ],
    }


@mcp.tool()
async def saga_pregateste_extras(
    cif: str,
    tranzactii: list[dict],
    cont_banca: str = "5121",
    sold_initial: float | None = None,
    sold_final: float | None = None,
    verifica_sold_in_saga: bool = True,
) -> dict:
    """Pregateste importul unui extras de cont si il verifica. NU trimite nimic in SAGA.

    Tranzactiile (citite din PDF, CSV sau MT940) se impart pe documente SAGA:
    - incasari de la clienti -> fisier I_ (Cont = banca, ContClient 4111);
    - plati catre furnizori -> fisier P_ (Cont = banca, ContFurnizor 401);
    - restul -> note contabile NC_ (comisioane 627, taxe, salarii 421, numerar 581...).
    Leaga incasarile/platile de facturi prin factura_numar sau factura_id, cand se stiu.

    Verificari: sold_initial + tranzactii = sold_final; soldul contului bancar din SAGA
    in ziua dinaintea primei tranzactii = sold_initial; tranzactii deja importate.

    Args:
        cif: codul fiscal al firmei.
        tranzactii: lista; fiecare: data, suma (+ intrare in cont, - iesire), categorie,
            explicatie, referinta (nr. document/ref. banca), partener_nume, partener_cif,
            factura_numar, factura_id, cont_corespondent. Categorii: incasare_client,
            plata_furnizor, comision_bancar, dobanda, plata_stat (cere cont_corespondent,
            ex. 4423 TVA, 4411/4418 impozit profit/micro, 444, 431, 436, 446), salarii,
            retragere_numerar, depunere_numerar, transfer_intern (cere cont_corespondent),
            alta (cere cont_corespondent).
        cont_banca: contul bancar din SAGA (5121 lei, 5124 valuta sau un analitic, ex. 5121.01).
        sold_initial: soldul de inceput de pe extras.
        sold_final: soldul de final de pe extras.
        verifica_sold_in_saga: compara sold_initial cu soldul din SAGA.
    """
    c = client.norm_cif(cif)
    reg = _registry(c)
    warnings, todo = [], []
    for t in tranzactii:
        tid = workflows.tx_id(cont_banca, workflows.ddmmyyyy(t["data"]), float(t["suma"]),
                              workflows.tx_ref(t))
        if reg.is_imported(tid):
            warnings.append(f"Tranzactia din {t['data']} de {t['suma']} ({t.get('explicatie', '')}) "
                            "a fost deja importata; sarita.")
        else:
            todo.append(t)

    verificari = {}
    total_miscare = workflows.r2(sum(float(t["suma"]) for t in tranzactii))
    verificari["total_miscari_extras"] = total_miscare
    if sold_initial is not None and sold_final is not None:
        calc = workflows.r2(sold_initial + total_miscare)
        verificari["sold_final_calculat"] = calc
        verificari["sold_final_extras"] = sold_final
        verificari["extras_complet"] = abs(calc - sold_final) < 0.01
        if not verificari["extras_complet"]:
            warnings.append(f"Sold initial + tranzactii = {calc}, dar extrasul arata {sold_final}: "
                            "lipsesc tranzactii sau o suma e citita gresit.")
    if verifica_sold_in_saga and sold_initial is not None and tranzactii:
        first = min(datetime.strptime(workflows.ddmmyyyy(t["data"]), "%d.%m.%Y") for t in tranzactii)
        ziua = (first - timedelta(days=1)).strftime("%d.%m.%Y")
        try:
            resp = await client.get_json("Situatii/GetSoldCont", c, {"data": ziua, "cont": cont_banca, "tip": "D"})
            sold_saga = workflows.r2(float(resp.get("data") or 0))
            verificari["sold_saga_inainte"] = {"data": ziua, "sold": sold_saga,
                                               "diferenta": workflows.r2(sold_initial - sold_saga)}
            if abs(sold_initial - sold_saga) >= 0.01:
                warnings.append(f"Soldul din SAGA la {ziua} ({sold_saga}) difera de soldul initial al "
                                f"extrasului ({sold_initial}): extrase anterioare neintroduse sau diferente.")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Nu am putut citi soldul din SAGA: {exc}")

    if not todo:
        return {"stare": "nimic de importat", "verificari": verificari, "avertismente": warnings}

    inc, pla, nc, tabel, w = workflows.build_statement(todo, cont_banca)
    warnings += w
    ids = [r["id"] for r in tabel]
    last = max(r["data"] for r in tabel)
    fisiere = []
    by_doc = {"I": inc, "P": pla, "NC": nc}
    for tip, items in by_doc.items():
        if items:
            tip_ids = [r["id"] for r in tabel if r["document"] == tip]
            fisiere.append(_write_import(c, tip, items, last, {"ids": tip_ids}))
    return {
        "cont_banca": cont_banca,
        "tranzactii": tabel,
        "numar_tranzactii": len(ids),
        "pe_categorii": {k: workflows.r2(sum(r["suma"] for r in tabel if r["categorie"] == k))
                         for k in sorted({r["categorie"] for r in tabel})},
        "verificari": verificari,
        "fisiere": fisiere,
        "avertismente": warnings,
        "pasi_urmatori": [
            "Arata utilizatorului tabelul, verificarile si avertismentele.",
            "Importa fiecare fisier cu saga_importa (doi pasi, cu acordul utilizatorului) si "
            "finalizeaza in SAGA (Diverse > Import date).",
            "Dupa import, saga_sold_cont pe contul bancar la data ultimei tranzactii trebuie "
            "sa fie egal cu soldul final al extrasului.",
        ],
    }


@mcp.tool()
async def saga_compara_sold(
    cif: str,
    cont: str,
    data: str,
    sold_asteptat: float,
    tip: Literal["D", "C"] = "D",
) -> dict:
    """Compara soldul unui cont din SAGA cu o valoare asteptata (extras bancar, registru
    de casa, fisa sintetica ANAF, confirmare de sold) si arata diferenta.

    Args:
        cif: codul fiscal al firmei.
        cont: contul din SAGA (ex. 5121, 5311, 4423, 401).
        data: data la care se compara.
        sold_asteptat: soldul din documentul extern.
        tip: D sold debitor, C sold creditor.
    """
    c = client.norm_cif(cif)
    d = client.norm_date(data)
    resp = await client.get_json("Situatii/GetSoldCont", c, {"data": d, "cont": cont, "tip": tip})
    sold = workflows.r2(float(resp.get("data") or 0))
    dif = workflows.r2(sold - sold_asteptat)
    return {"cif": c, "cont": cont, "data": d, "sold_saga": sold, "sold_asteptat": sold_asteptat,
            "diferenta": dif, "concorda": abs(dif) < 0.01}


# --------------------------------------------------------------------------- cabinet
def _clients_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "clienti.json"


def _load_clients() -> dict:
    p = _clients_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _save_clients(data: dict) -> None:
    _clients_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@mcp.tool()
async def saga_configureaza_client(
    cif: str,
    denumire: str | None = None,
    tva: Literal["lunar", "trimestrial", "neplatitor"] | None = None,
    declaratii: dict[str, str] | None = None,
    conturi_banca: list[str] | None = None,
    plafon_casa: float | None = None,
    responsabil: str | None = None,
    descoperit_permis: bool | None = None,
    sterge: bool = False,
) -> dict:
    """Adauga sau modifica o firma in lista clientilor cabinetului (folosita la inchiderea lunii).

    Se modifica doar campurile date; restul raman cum erau.

    Args:
        cif: codul fiscal al firmei.
        denumire: numele firmei (daca lipseste, se ia de la ANAF la prima adaugare).
        tva: "lunar", "trimestrial" sau "neplatitor".
        declaratii: declaratiile de urmarit si periodicitatea lor, ex.
            {"D300": "lunar", "D394": "lunar", "D112": "lunar", "D100": "trimestrial", "D406": "lunar"}.
        conturi_banca: conturile bancare de verificat (implicit ["5121"]).
        plafon_casa: plafonul de casa (implicit 50000).
        responsabil: contabilul care se ocupa de firma.
        descoperit_permis: true daca firma are descoperit de cont (banca negativa nu e eroare).
        sterge: true ca sa scoti firma din lista.
    """
    c = client.norm_cif(cif)
    data = _load_clients()
    if sterge:
        removed = data.pop(c, None)
        _save_clients(data)
        return {"sters": bool(removed), "cif": c}
    entry = data.get(c, {"cif": c})
    if not entry.get("denumire") and not denumire:
        try:
            denumire = (await anaf.lookup([c])).get(c, {}).get("denumire")
        except Exception:  # noqa: BLE001
            pass
    updates = {"denumire": denumire, "tva": tva, "declaratii": declaratii, "conturi_banca": conturi_banca,
               "plafon_casa": plafon_casa, "responsabil": responsabil, "descoperit_permis": descoperit_permis}
    for k, v in updates.items():
        if v is not None:
            entry[k] = v
    if declaratii:
        bad = [f"{k}: {v}" for k, v in declaratii.items() if v not in closing.PERIODICITY]
        if bad:
            raise ValueError(f"Periodicitate invalida ({', '.join(bad)}). Valori: {', '.join(closing.PERIODICITY)}")
    data[c] = entry
    _save_clients(data)
    return {"salvat": entry, "fisier": str(_clients_path())}


@mcp.tool()
async def saga_clienti() -> dict:
    """Lista firmelor din cabinet, cu configurarea lor pentru inchiderea lunii."""
    data = _load_clients()
    return {"numar": len(data), "firme": list(data.values()), "fisier": str(_clients_path())}


@mcp.tool()
async def saga_inchidere_luna(
    an: int,
    luna: int,
    cif_uri: list[str] | None = None,
    mesaje_spv: list[dict] | None = None,
    date_anaf: dict[str, dict] | None = None,
    solduri_extras: dict[str, dict[str, float]] | None = None,
    azi: str | None = None,
) -> dict:
    """Raportul de inchidere a lunii pentru toti clientii cabinetului (sau doar cif_uri).

    Pentru fiecare firma verifica in SAGA, la ultima zi a lunii: casa (negativa, peste
    plafon), banca (negativa, egala cu extrasul), TVA regularizat (4426/4427 = 0) si 4423
    fata de D300. Cu datele din SPV verifica declaratiile depuse si datoriile la ANAF.
    Rezultat: starea fiecarei firme (Gata / De lucru / Eroare) plus un raport HTML si CSV.

    Datele ANAF se aduna INAINTE, cu anafpy, pentru firmele din lista:
    - mesaje_spv: mesajele din spv_lista_mesaje (ultimele 60 de zile), asa cum vin;
      recipisele de tip "recipisa pentru CIF X, tip D300, ... perioada raportare 10.2026"
      sunt citite automat;
    - date_anaf: {cif: {"obligatii_plata": total din cererea "Obligatii de plata",
      "d300_de_plata": TVA de plata din D300 al lunii, "observatii": [..]}}.
    Fara ele raportul contine doar verificarile din SAGA.

    Args:
        an: anul inchiderii, ex. 2026.
        luna: luna inchiderii (1-12).
        cif_uri: doar aceste firme (implicit toate din saga_clienti).
        mesaje_spv: mesajele SPV brute de la anafpy.
        date_anaf: date ANAF pe firma, vezi mai sus.
        solduri_extras: soldurile de la sfarsitul lunii de pe extrase,
            ex. {"43424557": {"5121": 24236.0}}.
        azi: data de referinta pentru termene (implicit azi).
    """
    clients = _load_clients()
    wanted = [client.norm_cif(x) for x in cif_uri] if cif_uri else list(clients)
    if not wanted:
        raise RuntimeError("Nu exista firme configurate. Adauga-le cu saga_configureaza_client.")
    ref = datetime.strptime(client.norm_date(azi), "%d.%m.%Y").date() if azi else datetime.now().date()
    end = closing.last_day(an, luna).strftime("%d.%m.%Y")
    filed = closing.filed_from_spv(mesaje_spv) if mesaje_spv is not None else None

    rezultate = []
    for c in wanted:
        cfg = clients.get(c, {"cif": c})
        accounts = [("5311", "D")] + [(b, "D") for b in cfg.get("conturi_banca") or ["5121"]] + \
                   [("4423", "C"), ("4424", "D"), ("4426", "D"), ("4427", "C")]
        solduri, eroare_firma = {}, None
        for cont, tip in accounts:
            try:
                resp = await client.get_json("Situatii/GetSoldCont", c, {"data": end, "cont": cont, "tip": tip})
                solduri[cont] = workflows.r2(float(resp.get("data") or 0))
            except Exception as exc:  # noqa: BLE001
                solduri[cont] = None
                if "401" in str(exc) or "403" in str(exc) or "cheie" in str(exc).lower():
                    eroare_firma = str(exc)
                    break
        if eroare_firma:
            rezultate.append({"cif": c, "denumire": cfg.get("denumire", ""), "responsabil": cfg.get("responsabil", ""),
                              "stare": "Eroare", "de_rezolvat": [f"Acces SAGA: {eroare_firma}"],
                              "verificari": [{"cod": "acces", "stare": "eroare", "mesaj": f"Acces SAGA: {eroare_firma}"}]})
            continue
        depuse = filed.get(c, set()) if filed is not None else None
        rezultate.append(closing.evaluate_firm(
            cfg, an, luna, solduri, (date_anaf or {}).get(c), depuse, ref, (solduri_extras or {}).get(c)))

    folder = DATA_DIR / "rapoarte"
    folder.mkdir(parents=True, exist_ok=True)
    # Un raport doar pe cateva firme nu suprascrie raportul pe tot cabinetul.
    stamp = f"{an}-{luna:02d}" if not cif_uri else f"{an}-{luna:02d}_" + "-".join(wanted[:3]) + \
        (f"-si-alte-{len(wanted) - 3}" if len(wanted) > 3 else "")
    html_path = folder / f"inchidere_{stamp}.html"
    csv_path = folder / f"inchidere_{stamp}.csv"
    html_path.write_text(closing.report_html(an, luna, rezultate, datetime.now().strftime("%d.%m.%Y %H:%M")),
                         encoding="utf-8")
    csv_path.write_text(closing.report_csv(rezultate), encoding="utf-8-sig")

    return {
        "luna": f"{luna:02d}.{an}",
        "sumar": {k: sum(1 for r in rezultate if r["stare"] == k) for k in ("Eroare", "De lucru", "Gata")},
        "firme": [{"cif": r["cif"], "denumire": r["denumire"], "responsabil": r["responsabil"],
                   "stare": r["stare"], "de_rezolvat": r["de_rezolvat"]} for r in rezultate],
        "date_spv_folosite": filed is not None,
        "raport_html": str(html_path),
        "raport_csv": str(csv_path),
    }


@mcp.resource("saga://categorii-extras", mime_type="text/markdown")
def categorii_extras() -> str:
    """Categoriile de tranzactii bancare si inregistrarea contabila folosita pentru fiecare."""
    return "# Categorii extras de cont\n\n" + "\n".join(
        f"- `{k}`: {v}" for k, v in workflows.CATEGORII_EXTRAS.items())


@mcp.resource("saga://formate-import", mime_type="text/markdown")
def formate_import() -> str:
    """Structura fisierelor XML de import SAGA Web (facturi, note contabile, incasari etc.)."""
    return formats.FORMATS_DOC


def run() -> None:
    mcp.run()

"""Uneltele MCP pentru SAGA Web."""

from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from . import client, formats, keystore

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


@mcp.resource("saga://formate-import", mime_type="text/markdown")
def formate_import() -> str:
    """Structura fisierelor XML de import SAGA Web (facturi, note contabile, incasari etc.)."""
    return formats.FORMATS_DOC


def run() -> None:
    mcp.run()

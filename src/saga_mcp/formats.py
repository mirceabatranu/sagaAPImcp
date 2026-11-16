"""Formatele XML de import SAGA Web si construirea fisierelor.

Sursa: https://web.sagasoft.ro/DocumentatieAPI (sectiunea de import).
"""

from __future__ import annotations

import re
from datetime import date
from xml.sax.saxutils import escape

# tip -> (element radacina, element repetat, prefix fisier)
FLAT_TYPES = {
    "I": ("Incasari", "Linie", "I"),
    "P": ("Plati", "Linie", "P"),
    "NC": ("NC", "Linie", "NC"),
    "CLI": ("Clienti", "Linie", "CLI"),
    "ART": ("Articole", "Linie", "ART"),
}
DOC_TYPES = {
    "F": ("Facturi", "Factura", "F"),
    "C": ("Comenzi", "Comanda", "C"),
}

REQUIRED = {
    "I": ["Data", "Numar", "Suma", "Cont", "Explicatie"],
    "P": ["Data", "Numar", "Suma", "Cont", "Explicatie"],
    "NC": ["Data", "Numar", "Suma", "ContDebit", "ContCredit", "Explicatie"],
    "CLI": ["Denumire", "Cod_fiscal", "Reg_com", "Tara", "Judet", "Localitate"],
    "ART": ["Denumire", "Cod_NC", "Cod_CPV", "UM", "Tip", "TVA"],
    "F_antet": [
        "FurnizorNume", "FurnizorCIF", "ClientNume", "ClientCIF", "ClientTara",
        "FacturaNumar", "FacturaData", "FacturaScadenta",
        "FacturaTaxareInversa", "FacturaTVAIncasare",
    ],
    "F_linie": ["LinieNrCrt", "Descriere", "UM", "Cantitate", "Pret", "Valoare", "ProcTVA", "TVA"],
    "C_antet": [
        "FurnizorNume", "FurnizorCIF", "ClientNume", "ClientCIF", "ClientTara",
        "ComandaNumar", "ComandaData",
    ],
    "C_linie": ["LinieNrCrt", "Descriere", "UM", "Cantitate", "Pret", "Valoare", "ProcTVA", "TVA"],
}

FILE_PATTERNS = {
    "F": re.compile(r"^F_\d+_[^_]+_[^_]+\.xml$", re.I),
    "C": re.compile(r"^C_\d+_[^_]+_[^_]+\.xml$", re.I),
    "I": re.compile(r"^I_[^_]+\.xml$", re.I),
    "P": re.compile(r"^P_[^_]+\.xml$", re.I),
    "NC": re.compile(r"^NC_[^_]+\.xml$", re.I),
    "CLI": re.compile(r"^CLI_[^_]+\.xml$", re.I),
    "ART": re.compile(r"^ART_[^_]+\.xml$", re.I),
}
ROOTS = {
    "Facturi": "F", "Comenzi": "C", "Incasari": "I", "Plati": "P",
    "NC": "NC", "Clienti": "CLI", "Articole": "ART",
}


def detect_type(filename: str) -> str | None:
    for tip, pattern in FILE_PATTERNS.items():
        if pattern.match(filename):
            return tip
    return None


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9.\-]", "-", str(text))


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Da" if value else "Nu"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".") if value % 1 else str(int(value))
    return str(value)


def _fields(d: dict, indent: str) -> str:
    return "".join(f"{indent}<{k}>{escape(_fmt(v))}</{k}>\n" for k, v in d.items())


def _missing(d: dict, required: list[str]) -> list[str]:
    return [k for k in required if k not in d or d[k] in (None, "")]


def build(tip: str, cif: str, items: list[dict], data_fisier: str | None = None) -> tuple[str, str, list[str]]:
    """Construieste XML-ul si numele de fisier. Intoarce (nume, xml, avertismente).

    Pentru tipurile simple (I, P, NC, CLI, ART), `items` e lista de linii.
    Pentru F si C, `items` e lista de documente {"antet": {...}, "linii": [...], "id": "..."}.
    Pentru F si C, toate documentele dintr-un fisier trebuie sa aiba acelasi numar si data
    in nume, deci se recomanda un document per fisier.
    """
    tip = tip.upper()
    warnings: list[str] = []
    stamp = data_fisier or date.today().strftime("%d.%m.%Y")

    if tip in FLAT_TYPES:
        root, row, prefix = FLAT_TYPES[tip]
        parts = [f"<{root}>\n"]
        for i, linie in enumerate(items, 1):
            miss = _missing(linie, REQUIRED[tip])
            if miss:
                warnings.append(f"Linia {i}: lipsesc campurile {', '.join(miss)}")
            parts.append(f"  <{row}>\n{_fields(linie, '    ')}  </{row}>\n")
        parts.append(f"</{root}>\n")
        name = f"{prefix}_{_safe(stamp)}.xml"
        return name, '<?xml version="1.0" encoding="UTF-8"?>\n' + "".join(parts), warnings

    if tip in DOC_TYPES:
        root, doc, prefix = DOC_TYPES[tip]
        parts = [f"<{root}>\n"]
        first_antet = None
        for i, d in enumerate(items, 1):
            antet = d.get("antet") or {}
            linii = d.get("linii") or []
            first_antet = first_antet or antet
            miss = _missing(antet, REQUIRED[f"{tip}_antet"])
            if miss:
                warnings.append(f"Documentul {i}: lipsesc din antet {', '.join(miss)}")
            if not linii:
                warnings.append(f"Documentul {i}: nu are linii")
            body = [f"  <{doc}>\n    <Antet>\n{_fields(antet, '      ')}    </Antet>\n"]
            body.append("    <Detalii>\n      <Continut>\n")
            for j, linie in enumerate(linii, 1):
                linie = {"LinieNrCrt": j, **linie}
                miss = _missing(linie, REQUIRED[f"{tip}_linie"])
                if miss:
                    warnings.append(f"Documentul {i}, linia {j}: lipsesc {', '.join(miss)}")
                body.append(f"        <Linie>\n{_fields(linie, '          ')}        </Linie>\n")
            body.append("      </Continut>\n    </Detalii>\n")
            if d.get("id"):
                body.append(f"    <FacturaID>{escape(_fmt(d['id']))}</FacturaID>\n")
            body.append(f"  </{doc}>\n")
            parts.extend(body)
        parts.append(f"</{root}>\n")
        a = first_antet or {}
        if tip == "F":
            name = f"F_{cif}_{_safe(a.get('FacturaNumar', 'X'))}_{_safe(a.get('FacturaData', stamp))}.xml"
        else:
            name = f"C_{cif}_{_safe(a.get('ComandaNumar', 'X'))}_{_safe(a.get('ComandaData', stamp))}.xml"
        return name, '<?xml version="1.0" encoding="UTF-8"?>\n' + "".join(parts), warnings

    raise ValueError(f"Tip necunoscut: {tip}. Tipuri: F, C, I, P, NC, CLI, ART.")


FORMATS_DOC = """\
# Formate XML de import SAGA Web

Nume fisiere: F_<cif>_<numar>_<data>.xml (facturi), C_<cif>_<numar>_<data>.xml (comenzi),
I_<data>.xml (incasari), P_<data>.xml (plati), NC_<data>.xml (note contabile),
CLI_<data>.xml (clienti), ART_<data>.xml (articole).

## Facturi  <Facturi><Factura><Antet>...</Antet><Detalii><Continut><Linie>...</Linie></Continut></Detalii><FacturaID/></Factura></Facturi>
Daca FurnizorCIF este codul fiscal al firmei, factura intra la Iesiri, altfel la Intrari.
Antet obligatoriu: FurnizorNume, FurnizorCIF, FurnizorNrRegCom, FurnizorCapital, FurnizorTara,
FurnizorLocalitate, FurnizorJudet, FurnizorAdresa, FurnizorTelefon, FurnizorMail, FurnizorBanca,
FurnizorIBAN, FurnizorInformatiiSuplimentare, ClientNume, ClientInformatiiSuplimentare, ClientCIF,
ClientNrRegCom, ClientJudet (cod judet, doar RO), ClientTara (cod tara), ClientLocalitate,
ClientAdresa, ClientBanca, ClientIBAN, ClientTelefon, ClientMail, FacturaNumar, FacturaData,
FacturaScadenta, FacturaTaxareInversa (Da/Nu), FacturaTVAIncasare (Da/Nu),
FacturaInformatiiSuplimentare, FacturaGreutate.
Antet optional: GUID_cod_client, FacturaTip (" " factura, A aviz, B bon, T taxare inversa,
C bon cu CF), FacturaMoneda (implicit RON), FacturaAgent, FacturaAccize, FacturaIndexSPV,
FacturaIndexDescarcareSPV, Cod (cod partener SAGA).
Linie obligatoriu: LinieNrCrt, Descriere, UM, Cantitate, Pret, Valoare, ProcTVA, TVA.
Linie optional: Gestiune, Activitate, Proiect, CodArticolFurnizor, CodArticolClient,
GUID_cod_articol, CodBare, InformatiiSuplimentare, Cont, TipDeducere (N50 / I, doar intrari),
PretVanzare (doar intrari).
Campurile marcate obligatorii pot fi trimise goale daca nu exista informatia.

## Comenzi  <Comenzi><Comanda>... la fel ca facturile, cu ComandaNumar, ComandaData,
ComandaLivrare, ComandaInformatiiSuplimentare, ComandaMoneda (optional), ComandaGreutate.

## Incasari  <Incasari><Linie>
Obligatoriu: Data, Numar, Suma, Cont (clasa 5), Explicatie.
Optional: ContClient, FacturaID, FacturaNumar, CodFiscal, Moneda, Activitate, Proiect.

## Plati  <Plati><Linie>
Obligatoriu: Data, Numar, Suma, Cont (clasa 5), Explicatie.
Optional: ContFurnizor, FacturaID, FacturaNumar, CodFiscal, Moneda, Activitate, Proiect.

## Note contabile  <NC><Linie>
Obligatoriu: Data (D), Numar C(16), Suma N(15,2), ContDebit C(20), ContCredit C(20),
Explicatie C(200). Optional: Moneda C(3, implicit RON), Curs N(10,4), Suma_val N(15,2),
Activitate C(16), Proiect C(16).

## Clienti  <Clienti><Linie>
Obligatoriu: Denumire, Cod_fiscal, Reg_com, Tara (RO), Judet, Localitate.
Optional: Cod, Adresa, Cont_banca, Banca, Tel, Email, Discount, Informatii, Guid_cod.

## Articole  <Articole><Linie>
Obligatoriu: Denumire, Cod_NC, Cod_CPV, UM, Tip, TVA.
Optional: Cod, Pret, Pret_TVA, Cod_bare, Informatii, Guid_cod, Greutate, Greutate_B.
"""

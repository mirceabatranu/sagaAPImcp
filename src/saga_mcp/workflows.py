"""Fluxuri de lucru: bonuri fiscale si extrase de cont -> fisiere de import SAGA.

Functii pure (fara retea), ca sa poata fi testate separat. Uneltele MCP din
server.py aduna datele (ANAF, registru, SAGA) si apoi le apeleaza.
"""

from __future__ import annotations

import re
from datetime import datetime

PAYMENT_ACCOUNTS = {"numerar": "5311", "card": "5121", "angajat": "542"}


def r2(x: float) -> float:
    return round(float(x) + 0.0, 2)


def ddmmyyyy(value: str) -> str:
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    raise ValueError(f"Data nerecunoscuta: {value!r}")


def compact(d: str) -> str:
    return datetime.strptime(d, "%d.%m.%Y").strftime("%Y%m%d")


def digits(x) -> str:
    return re.sub(r"\D", "", str(x or ""))


def bon_id(furnizor_cif: str, numar: str, data: str) -> str:
    return f"BON-{digits(furnizor_cif) or 'X'}-{re.sub(r'[^A-Za-z0-9]', '', str(numar))}-{compact(data)}"


def tx_ref(t: dict) -> str:
    """Referinta folosita la identificarea unei tranzactii (pentru dubluri)."""
    return str(t.get("referinta") or t.get("numar") or t.get("explicatie") or "")


def tx_id(cont_banca: str, data: str, suma: float, referinta: str) -> str:
    ref = re.sub(r"[^A-Za-z0-9]", "", str(referinta))[:24] or "X"
    return f"EXT-{cont_banca}-{compact(data)}-{int(round(abs(suma) * 100))}-{ref}"


# --------------------------------------------------------------------------- bonuri
def split_gross(total: float, proc: float) -> tuple[float, float]:
    """Imparte un total cu TVA in (baza, TVA)."""
    baza = r2(total / (1 + proc / 100.0))
    return baza, r2(total - baza)


def build_bon_documents(
    bonuri: list[dict],
    firma: dict,
    furnizori: dict[str, dict],
    cont_implicit: str,
    conturi_plata: dict[str, str],
) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """Intoarce (documente F, linii P, tabel rezumat, avertismente).

    `furnizori` e {cif: info} cu datele complete (ANAF sau furnizate).
    `firma` e {"cif", "denumire", "nr_reg_com", "adresa", "localitate", "judet"}.
    """
    docs, plati, tabel, warnings = [], [], [], []
    for i, b in enumerate(bonuri, 1):
        cif_f = digits(b.get("furnizor_cif"))
        info = furnizori.get(cif_f, {})
        # Denumirea oficiala ANAF are prioritate, ca acelasi furnizor sa nu apara sub
        # mai multe nume in SAGA; denumirea de pe bon e folosita doar fara date ANAF.
        nume_f = info.get("denumire") or b.get("furnizor_nume") or ""
        data = ddmmyyyy(b["data"])
        numar = str(b["numar"]).strip()
        did = bon_id(cif_f, numar, data)
        plata = (b.get("plata") or "numerar").lower()
        if plata not in ("numerar", "card", "angajat", "neplatit"):
            raise ValueError(f"Bonul {i}: plata trebuie sa fie numerar, card, angajat sau neplatit")

        linii_in = b.get("linii") or []
        if not linii_in and b.get("total") is not None:
            linii_in = [{"total": b["total"], "proc_tva": b.get("proc_tva", 21)}]
        if not linii_in:
            raise ValueError(f"Bonul {i} ({numar}): lipsesc sumele (linii sau total)")

        nedeductibil = b.get("cif_cumparator_pe_bon") is False
        observatii = []
        if not cif_f:
            observatii.append("fara CIF furnizor")
        elif not info:
            observatii.append("furnizor negasit la ANAF")
        if info.get("inactiv"):
            observatii.append("FURNIZOR INACTIV: TVA nedeductibil")
            nedeductibil = True
        if info.get("radiat"):
            observatii.append("furnizor radiat")
        if b.get("cif_cumparator_pe_bon") is False:
            observatii.append("fara CIF-ul firmei pe bon: TVA nedeductibil")

        linii, total, tva_total = [], 0.0, 0.0
        for j, ln in enumerate(linii_in, 1):
            proc = float(ln.get("proc_tva", 21))
            if info and not info.get("platitor_tva") and proc:
                observatii.append("furnizor neplatitor de TVA, dar bonul are TVA: verifica")
            if ln.get("total") is not None:
                baza, tva = split_gross(float(ln["total"]), proc)
            else:
                baza = r2(ln["valoare"])
                tva = r2(ln.get("tva", baza * proc / 100))
            linie = {
                "LinieNrCrt": j,
                "Descriere": ln.get("descriere") or b.get("descriere") or "Bon fiscal",
                "UM": ln.get("um", "BUC"),
                "Cantitate": ln.get("cantitate", 1),
                "Pret": baza if ln.get("cantitate", 1) == 1 else r2(baza / float(ln["cantitate"])),
                "Valoare": baza,
                "ProcTVA": proc,
                "TVA": tva,
                "Cont": ln.get("cont") or b.get("cont") or cont_implicit,
            }
            deducere = ln.get("tip_deducere") or b.get("tip_deducere")
            if nedeductibil and proc:
                deducere = "I"
            if deducere:
                linie["TipDeducere"] = deducere
            linii.append(linie)
            total += baza + tva
            tva_total += tva
        total, tva_total = r2(total), r2(tva_total)
        if b.get("total") is not None and b.get("linii") and abs(r2(b["total"]) - total) > 0.02:
            observatii.append(f"suma liniilor {total} difera de totalul bonului {b['total']}")

        antet = {
            "FurnizorNume": nume_f,
            "FurnizorCIF": cif_f,
            "FurnizorNrRegCom": info.get("nr_reg_com", ""),
            "FurnizorCapital": "",
            "FurnizorTara": info.get("tara", "RO"),
            "FurnizorLocalitate": info.get("localitate", ""),
            "FurnizorJudet": info.get("judet", ""),
            "FurnizorAdresa": info.get("adresa", ""),
            "FurnizorTelefon": info.get("telefon", ""),
            "FurnizorMail": "",
            "FurnizorBanca": "",
            "FurnizorIBAN": info.get("iban", ""),
            "FurnizorInformatiiSuplimentare": "",
            "ClientNume": firma.get("denumire", ""),
            "ClientInformatiiSuplimentare": "",
            "ClientCIF": firma["cif"],
            "ClientNrRegCom": firma.get("nr_reg_com", ""),
            "ClientJudet": firma.get("judet", ""),
            "ClientTara": "RO",
            "ClientLocalitate": firma.get("localitate", ""),
            "ClientAdresa": firma.get("adresa", ""),
            "ClientBanca": "",
            "ClientIBAN": "",
            "ClientTelefon": "",
            "ClientMail": "",
            "FacturaNumar": numar,
            "FacturaData": data,
            "FacturaScadenta": data,
            "FacturaTaxareInversa": "Nu",
            "FacturaTVAIncasare": "Nu",
            "FacturaTip": "B",
            "FacturaInformatiiSuplimentare": b.get("observatii", ""),
            "FacturaGreutate": "",
        }
        docs.append({"antet": antet, "linii": linii, "id": did})

        if plata != "neplatit":
            cont_plata = b.get("cont_plata") or conturi_plata[plata]
            plati.append({
                "Data": data,
                "Numar": numar,
                "Suma": total,
                "Cont": cont_plata,
                "ContFurnizor": b.get("cont_furnizor", "401"),
                "Explicatie": f"Plata bon {numar} {nume_f}"[:200],
                "FacturaID": did,
                "FacturaNumar": numar,
                "CodFiscal": cif_f,
            })
        tabel.append({
            "nr": i, "id": did, "data": data, "numar": numar, "furnizor": nume_f, "cif": cif_f,
            "total": total, "tva": tva_total, "plata": plata,
            "cont_plata": None if plata == "neplatit" else (b.get("cont_plata") or conturi_plata[plata]),
            "conturi": sorted({l["Cont"] for l in linii}), "observatii": observatii,
        })
        for o in observatii:
            warnings.append(f"Bonul {i} ({numar}, {nume_f}): {o}")
    return docs, plati, tabel, warnings


# --------------------------------------------------------------------------- extrase
CATEGORII_EXTRAS = {
    "incasare_client": "Incasare de la client (4111) -> import I",
    "plata_furnizor": "Plata catre furnizor (401) -> import P",
    "comision_bancar": "Comision bancar: 627 = banca",
    "dobanda": "Dobanda primita: banca = 766",
    "plata_stat": "Taxe/impozite/contributii: <cont_corespondent> = banca (ex. 4423, 4411, 444, 431, 436, 446)",
    "salarii": "Plata salarii: 421 = banca",
    "retragere_numerar": "Retragere in casa: 581 = banca si 5311 = 581",
    "depunere_numerar": "Depunere din casa: 581 = 5311 si banca = 581",
    "transfer_intern": "Transfer intre conturi proprii: <cont_corespondent> si 581",
    "alta": "Orice alta operatiune: cont_corespondent obligatoriu; incasare = banca/cont, plata = cont/banca",
}


def build_statement(
    tranzactii: list[dict], cont_banca: str
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[str]]:
    """Intoarce (linii I, linii P, linii NC, tabel, avertismente).

    Fiecare tranzactie: data, suma (pozitiv = intrare in cont, negativ = iesire),
    categorie, explicatie, optional partener_cif, partener_nume, factura_numar,
    factura_id, cont_corespondent, referinta (nr. document / ref. banca).
    """
    inc, pla, nc, tabel, warnings = [], [], [], [], []
    for i, t in enumerate(tranzactii, 1):
        data = ddmmyyyy(t["data"])
        suma = r2(t["suma"])
        if suma == 0:
            warnings.append(f"Tranzactia {i}: suma zero, ignorata")
            continue
        cat = t.get("categorie", "alta")
        if cat not in CATEGORII_EXTRAS:
            raise ValueError(f"Tranzactia {i}: categorie necunoscuta {cat!r}. Categorii: {', '.join(CATEGORII_EXTRAS)}")
        numar = re.sub(r"\s+", "", str(t.get("referinta") or t.get("numar") or f"EXT{i}"))[:16]
        expl = (t.get("explicatie") or cat)[:200]
        tid = tx_id(cont_banca, data, suma, tx_ref(t))
        partener = t.get("partener_nume") or ""
        cif_p = digits(t.get("partener_cif"))
        intrare = suma > 0
        valoare = abs(suma)
        cont_c = t.get("cont_corespondent")
        doc = "NC"

        def nc_line(debit, credit, explicatie=expl):
            nc.append({"Data": data, "Numar": numar, "Suma": valoare, "ContDebit": debit,
                       "ContCredit": credit, "Explicatie": explicatie[:200]})

        if cat == "incasare_client":
            if not intrare:
                warnings.append(f"Tranzactia {i}: incasare cu suma negativa, verifica")
            linie = {"Data": data, "Numar": numar, "Suma": valoare, "Cont": cont_banca,
                     "ContClient": cont_c or "4111", "Explicatie": expl, "CodFiscal": cif_p}
            if t.get("factura_id"):
                linie["FacturaID"] = t["factura_id"]
            if t.get("factura_numar"):
                linie["FacturaNumar"] = t["factura_numar"]
            inc.append({k: v for k, v in linie.items() if v not in (None, "")})
            doc = "I"
        elif cat == "plata_furnizor":
            if intrare:
                warnings.append(f"Tranzactia {i}: plata cu suma pozitiva, verifica")
            linie = {"Data": data, "Numar": numar, "Suma": valoare, "Cont": cont_banca,
                     "ContFurnizor": cont_c or "401", "Explicatie": expl, "CodFiscal": cif_p}
            if t.get("factura_id"):
                linie["FacturaID"] = t["factura_id"]
            if t.get("factura_numar"):
                linie["FacturaNumar"] = t["factura_numar"]
            pla.append({k: v for k, v in linie.items() if v not in (None, "")})
            doc = "P"
        elif cat == "comision_bancar":
            nc_line(cont_c or "627", cont_banca)
        elif cat == "dobanda":
            nc_line(cont_banca, cont_c or "766")
        elif cat == "salarii":
            nc_line(cont_c or "421", cont_banca)
        elif cat == "plata_stat":
            if not cont_c:
                raise ValueError(f"Tranzactia {i} ({expl}): plata_stat cere cont_corespondent (ex. 4423, 4411, 444, 431)")
            nc_line(cont_c, cont_banca)
        elif cat == "retragere_numerar":
            nc_line("581", cont_banca)
            nc_line(cont_c or "5311", "581")
        elif cat == "depunere_numerar":
            nc_line("581", cont_c or "5311")
            nc_line(cont_banca, "581")
        elif cat == "transfer_intern":
            if not cont_c:
                raise ValueError(f"Tranzactia {i}: transfer_intern cere cont_corespondent (celalalt cont bancar)")
            if intrare:
                nc_line(cont_banca, "581")
            else:
                nc_line("581", cont_banca)
        else:  # alta
            if not cont_c:
                raise ValueError(f"Tranzactia {i} ({expl}): categoria 'alta' cere cont_corespondent")
            if intrare:
                nc_line(cont_banca, cont_c)
            else:
                nc_line(cont_c, cont_banca)

        tabel.append({"nr": i, "id": tid, "data": data, "suma": suma, "categorie": cat, "document": doc,
                      "partener": partener, "explicatie": expl})
    return inc, pla, nc, tabel, warnings

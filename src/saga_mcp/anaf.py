"""Interogarea registrului public ANAF (fara autentificare) pentru datele furnizorilor.

Serviciu: POST https://webservicesp.anaf.ro/api/PlatitorTvaRest/v9/tva
Maxim 100 CUI pe cerere si o cerere pe secunda.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import date

import httpx

ANAF_URL = os.environ.get(
    "SAGA_ANAF_URL", "https://webservicesp.anaf.ro/api/PlatitorTvaRest/v9/tva"
)


def _digits(cui) -> str:
    return re.sub(r"\D", "", str(cui or ""))


def _info(found: dict) -> dict:
    g = found.get("date_generale", {}) or {}
    tva = found.get("inregistrare_scop_Tva", {}) or {}
    inactiv = found.get("stare_inactiv", {}) or {}
    tvai = found.get("inregistrare_RTVAI", {}) or {}
    sediu = found.get("adresa_sediu_social", {}) or {}
    strada = " ".join(
        x for x in (sediu.get("sdenumire_Strada"), sediu.get("snumar_Strada"), sediu.get("sdetalii_Adresa")) if x
    )
    return {
        "cui": _digits(g.get("cui")),
        "denumire": g.get("denumire") or "",
        "nr_reg_com": g.get("nrRegCom") or "",
        "adresa": strada or g.get("adresa") or "",
        "adresa_completa": g.get("adresa") or "",
        "localitate": sediu.get("sdenumire_Localitate") or "",
        "judet": sediu.get("scod_JudetAuto") or "",
        "tara": "RO",
        "telefon": g.get("telefon") or "",
        "iban": g.get("iban") or "",
        "platitor_tva": bool(tva.get("scpTVA")),
        "tva_la_incasare": bool(tvai.get("statusTvaIncasare")),
        "inactiv": bool(inactiv.get("statusInactivi")),
        "radiat": bool(inactiv.get("dataRadiere")),
        "stare": g.get("stare_inregistrare") or "",
    }


async def lookup(cuis: list, la_data: str | None = None) -> dict[str, dict]:
    """Intoarce {cui: info} pentru CUI-urile gasite. CUI-urile negasite lipsesc din rezultat."""
    wanted = sorted({_digits(c) for c in cuis if _digits(c)})
    if not wanted:
        return {}
    day = la_data or date.today().isoformat()
    out: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        for i in range(0, len(wanted), 100):
            if i:
                await asyncio.sleep(1.1)  # limita ANAF: o cerere pe secunda
            body = [{"cui": int(c), "data": day} for c in wanted[i:i + 100]]
            resp = await client.post(ANAF_URL, json=body)
            resp.raise_for_status()
            for found in resp.json().get("found", []) or []:
                info = _info(found)
                out[info["cui"]] = info
    return out

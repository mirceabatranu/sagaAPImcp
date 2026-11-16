"""Client HTTP pentru API-ul SAGA Web, cu rotirea automata a cheii.

Documentatie: https://web.sagasoft.ro/DocumentatieAPI
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from . import keystore

BASE_URL = os.environ.get(
    "SAGA_API_URL", "https://web.sagasoft.ro/api/v20260225"
).rstrip("/")
TIMEOUT = httpx.Timeout(120.0, connect=10.0)

# Cererile care folosesc aceeasi cheie se fac pe rand: daca doua cereri ar
# pleca in paralel si prima ar primi o cheie noua, a doua ar folosi cheia
# veche, deja blocata.
_locks: dict[str, asyncio.Lock] = {}


def _lock(slot: str) -> asyncio.Lock:
    return _locks.setdefault(slot, asyncio.Lock())


def norm_cif(cif: str | int | None) -> str:
    digits = re.sub(r"\D", "", str(cif or os.environ.get("SAGA_CIF", "")))
    if not digits:
        raise ValueError("Lipseste codul fiscal al firmei (cif).")
    return digits


def norm_date(value: str | date | None) -> str | None:
    """Accepta 2026-03-31, 31.03.2026, 31/03/2026 sau 'azi'; intoarce dd.MM.yyyy."""
    if value is None or value == "":
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime("%d.%m.%Y")
    text = str(value).strip().lower()
    if text in ("azi", "today", "astazi"):
        return date.today().strftime("%d.%m.%Y")
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    raise ValueError(f"Data nerecunoscuta: {value!r}. Foloseste AAAA-LL-ZZ sau ZZ.LL.AAAA.")


@dataclass
class SagaResponse:
    status: int
    content_type: str
    body: bytes
    rotated: bool

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> dict:
        import json

        return json.loads(self.body)


def _error_message(resp: SagaResponse) -> str:
    try:
        data = resp.json()
        return str(data.get("message") or data.get("error") or data)
    except Exception:
        return resp.text[:500]


async def request(
    method: str,
    path: str,
    cif: str,
    *,
    params: dict | None = None,
    files: dict | None = None,
    data: dict | None = None,
) -> SagaResponse:
    """Trimite o cerere autentificata si salveaza cheia noua, daca SAGA o trimite."""
    slot, key = keystore.get_key(cif)
    async with _lock(slot):
        # Recitim cheia dupa ce am obtinut lock-ul: poate a rotit-o cererea anterioara.
        slot, key = keystore.get_key(cif)
        headers = {"Authorization": f"Bearer {key}", "X-Saga-Cod-Fiscal": cif}
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            try:
                resp = await client.request(
                    method,
                    f"{BASE_URL}/{path.lstrip('/')}",
                    params=clean,
                    headers=headers,
                    files=files,
                    data=data,
                )
            except httpx.ConnectError as exc:
                raise RuntimeError(f"Nu ma pot conecta la SAGA Web ({BASE_URL}).") from exc

        new_key = resp.headers.get("X-Saga-Refresh-Token")
        rotated = bool(new_key and new_key.strip() and new_key.strip() != key)
        if rotated:
            keystore.save_key(slot, new_key)

    result = SagaResponse(
        status=resp.status_code,
        content_type=resp.headers.get("content-type", ""),
        body=resp.content,
        rotated=rotated,
    )
    if resp.status_code == 401:
        raise RuntimeError(
            "SAGA a refuzat cheia de acces (401). Cheia poate fi expirata sau blocata: "
            "genereaza una noua in SAGA Web si ruleaza `saga-mcp set-key`."
        )
    if resp.status_code == 403:
        raise RuntimeError(
            f"Acces interzis pentru codul fiscal {cif} (403). Verifica daca utilizatorul "
            "API are drepturi pe aceasta firma."
        )
    return result


async def get_json(path: str, cif: str, params: dict) -> dict:
    resp = await request("GET", path, cif, params=params)
    if resp.status != 200:
        raise RuntimeError(f"SAGA a raspuns {resp.status}: {_error_message(resp)}")
    data = resp.json()
    if data.get("success") is False:
        raise RuntimeError(f"SAGA: {data.get('message') or data.get('error')}")
    return data


async def get_xml(path: str, cif: str, params: dict) -> str:
    resp = await request("GET", path, cif, params=params)
    if resp.status != 200 or "json" in resp.content_type:
        raise RuntimeError(f"SAGA a raspuns {resp.status}: {_error_message(resp)}")
    return resp.text

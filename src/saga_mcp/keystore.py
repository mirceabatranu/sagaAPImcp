"""Pastrarea cheii de acces SAGA in depozitul securizat al sistemului
(Windows Credential Manager / macOS Keychain).

SAGA roteste cheia: unele raspunsuri contin o cheie noua in headerul
X-Saga-Refresh-Token, iar cheia veche se blocheaza. De aceea cheia nu sta
intr-un fisier de configurare, ci aici, si se actualizeaza automat.

Se poate tine o cheie implicita sau cate o cheie pe cod fiscal, daca firmele
sunt in conturi SAGA Web diferite.
"""

from __future__ import annotations

import os

import keyring
from keyring.errors import PasswordDeleteError

SERVICE = os.environ.get("SAGA_KEYRING_SERVICE", "saga-mcp")
DEFAULT_SLOT = "default"


def slot_for(cif: str | None) -> str:
    return f"cif:{cif}" if cif else DEFAULT_SLOT


def get_key(cif: str | None) -> tuple[str, str]:
    """Intoarce (slot, cheie). Cauta intai cheia firmei, apoi cheia implicita,
    apoi variabila de mediu SAGA_ACCESS_KEY (doar ca prima initializare)."""
    for slot in ([slot_for(cif)] if cif else []) + [DEFAULT_SLOT]:
        value = keyring.get_password(SERVICE, slot)
        if value:
            return slot, value
    env = os.environ.get("SAGA_ACCESS_KEY")
    if env:
        # Prima rulare: mutam cheia din mediu in depozitul securizat, ca
        # rotirile ulterioare sa fie pastrate.
        keyring.set_password(SERVICE, DEFAULT_SLOT, env.strip())
        return DEFAULT_SLOT, env.strip()
    raise RuntimeError(
        "Nu exista cheie de acces SAGA. Genereaz-o in SAGA Web (Utilizatori > "
        "Integrare API > Genereaza cheie de acces) si salveaz-o cu comanda: "
        "saga-mcp set-key" + (f" --cif {cif}" if cif else "")
    )


def save_key(slot: str, value: str) -> None:
    keyring.set_password(SERVICE, slot, value.strip())


def delete_key(slot: str) -> bool:
    try:
        keyring.delete_password(SERVICE, slot)
        return True
    except PasswordDeleteError:
        return False


def has_key(slot: str) -> bool:
    return bool(keyring.get_password(SERVICE, slot))

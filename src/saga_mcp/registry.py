"""Registrul local pe firma: furnizorii cunoscuti si documentele deja importate.

API-ul SAGA Web nu are o lista de parteneri, asa ca serverul tine minte:
- furnizorii importati prin el (cu CIF), si
- furnizorii vazuti in balanta analitica (doar cu denumire si cont analitic).
Tot aici se pastreaza identificatorii documentelor importate, ca acelasi bon sau
aceeasi tranzactie sa nu fie introduse de doua ori.

Fisier: <SAGA_DATA_DIR>/<cif>/registru.json
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

LEGAL_FORMS = r"\b(S\.?R\.?L\.?|S\.?A\.?|S\.?C\.?|S\.?N\.?C\.?|S\.?C\.?S\.?|P\.?F\.?A\.?|I\.?I\.?|I\.?F\.?|SRL-D|SRLD)\b"


def norm_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    text = re.sub(LEGAL_FORMS, " ", text.upper())
    return re.sub(r"[^A-Z0-9]+", " ", text).strip()


class Registry:
    def __init__(self, folder: Path):
        self.path = folder / "registru.json"
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {}
        self.data.setdefault("furnizori", {})          # cif -> {...}
        self.data.setdefault("din_balanta", {})        # cont analitic -> {"denumire", "cont"}
        self.data.setdefault("documente_importate", {})  # id -> {"tip", "data_import", "fisier"}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- furnizori -------------------------------------------------------
    def add_supplier(self, cif: str, nume: str, sursa: str, **extra) -> None:
        entry = self.data["furnizori"].setdefault(cif, {"prima_data": datetime.now().isoformat(timespec="seconds")})
        entry.update({"denumire": nume, "sursa": sursa, **{k: v for k, v in extra.items() if v}})

    def set_balance_suppliers(self, rows: list[dict]) -> None:
        self.data["din_balanta"] = {r["cont"]: r for r in rows}

    def find_supplier(self, cif: str | None, nume: str | None) -> dict | None:
        """Cauta dupa CIF in furnizorii cunoscuti, apoi dupa denumire in balanta."""
        if cif and cif in self.data["furnizori"]:
            return {"potrivire": "cif", **self.data["furnizori"][cif], "cif": cif}
        key = norm_name(nume or "")
        if key:
            for cif_k, f in self.data["furnizori"].items():
                if norm_name(f.get("denumire", "")) == key:
                    return {"potrivire": "denumire", **f, "cif": cif_k}
            for cont, r in self.data["din_balanta"].items():
                if norm_name(r.get("denumire", "")) == key:
                    return {"potrivire": "denumire_balanta", **r}
        return None

    # --- documente ---------------------------------------------------------
    def is_imported(self, doc_id: str) -> bool:
        return doc_id in self.data["documente_importate"]

    def mark_imported(self, doc_ids: list[str], tip: str, fisier: str) -> None:
        stamp = datetime.now().isoformat(timespec="seconds")
        for d in doc_ids:
            self.data["documente_importate"][d] = {"tip": tip, "data_import": stamp, "fisier": fisier}

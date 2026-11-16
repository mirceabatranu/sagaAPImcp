"""Inchiderea lunii: verificarile pe fiecare firma si raportul pe tot cabinetul.

Functii pure: primesc soldurile deja citite din SAGA si datele din SPV si decid
starea fiecarei firme. Citirile se fac in server.py.
"""

from __future__ import annotations

import calendar
import csv
import html
import io
import re
from datetime import date

# Termenul de depunere: ziua din luna urmatoare perioadei ("last" = ultima zi).
DEADLINES = {"D300": 25, "D301": 25, "D390": 25, "D100": 25, "D112": 25, "D394": 30, "D406": "last"}
PERIODICITY = ("lunar", "trimestrial", "semestrial", "anual")
DEFAULT_CASH_LIMIT = 50000.0  # plafonul de casa pentru persoane juridice (L70/2015)

OK, WARN, ERR, INFO = "ok", "atentie", "eroare", "info"
RANK = {OK: 0, INFO: 0, WARN: 1, ERR: 2}
STATUS_LABEL = {0: "Gata", 1: "De lucru", 2: "Eroare"}


def last_day(an: int, luna: int) -> date:
    return date(an, luna, calendar.monthrange(an, luna)[1])


def due(an: int, luna: int, form: str) -> date:
    """Termenul pentru declaratia `form` aferenta lunii an/luna."""
    ny, nm = (an + 1, 1) if luna == 12 else (an, luna + 1)
    day = DEADLINES.get(form.upper(), 25)
    if day == "last":
        return last_day(ny, nm)
    return date(ny, nm, min(day, calendar.monthrange(ny, nm)[1]))


def expected(periodicitate: str, luna: int) -> bool:
    p = (periodicitate or "lunar").lower()
    return {
        "lunar": True,
        "trimestrial": luna % 3 == 0,
        "semestrial": luna % 6 == 0,
        "anual": luna == 12,
    }.get(p, True)


RECIPISA = re.compile(r"CIF\s*(\d+),\s*tip\s*([A-Z0-9_]+).*?perioada raportare\s*(\d{1,2})\.(\d{4})", re.I)


def filed_from_spv(mesaje: list[dict]) -> dict[str, set[tuple[str, int, int]]]:
    """Din mesajele SPV (anafpy spv_lista_mesaje) extrage {cif: {(forma, luna, an)}}."""
    out: dict[str, set] = {}
    for m in mesaje or []:
        text = str(m.get("details") or m.get("detalii") or m)
        for cif, form, luna, an in RECIPISA.findall(text):
            out.setdefault(cif, set()).add((form.upper(), int(luna), int(an)))
    return out


def evaluate_firm(
    cfg: dict,
    an: int,
    luna: int,
    solduri: dict[str, float | None],
    anaf: dict | None,
    depuse: set[tuple[str, int, int]] | None,
    azi: date,
    solduri_extras: dict[str, float] | None = None,
) -> dict:
    """Intoarce {cif, denumire, verificari: [{cod, stare, mesaj}], stare}.

    solduri: {"5311": x, "5121": y, "4423": z, "4424": ..., "4426": ..., "4427": ...}
             valori None = nu s-au putut citi.
    """
    checks: list[dict] = []

    def add(cod, stare, mesaj, valoare=None):
        checks.append({"cod": cod, "stare": stare, "mesaj": mesaj,
                       **({"valoare": valoare} if valoare is not None else {})})

    # --- casa
    casa = solduri.get("5311")
    limit = float(cfg.get("plafon_casa") or DEFAULT_CASH_LIMIT)
    if casa is None:
        add("casa", WARN, "Soldul casei (5311) nu a putut fi citit")
    elif casa < -0.005:
        add("casa", ERR, f"Casa are sold negativ: {casa:.2f}", casa)
    elif casa > limit:
        add("casa", WARN, f"Casa {casa:.2f} peste plafonul de {limit:.0f}", casa)
    else:
        add("casa", OK, f"Casa {casa:.2f}", casa)

    # --- banca (si comparatia cu extrasele, daca sunt date)
    for cont in cfg.get("conturi_banca") or ["5121"]:
        s = solduri.get(cont)
        extras = (solduri_extras or {}).get(cont)
        if s is None:
            add(f"banca_{cont}", WARN, f"Soldul {cont} nu a putut fi citit")
            continue
        if s < -0.005 and not cfg.get("descoperit_permis"):
            add(f"banca_{cont}", ERR, f"Banca {cont} are sold negativ: {s:.2f}", s)
        elif extras is not None and abs(s - float(extras)) >= 0.01:
            add(f"banca_{cont}", WARN,
                f"Banca {cont}: SAGA {s:.2f} difera de extras {float(extras):.2f} "
                f"(dif. {s - float(extras):.2f}) - extrase neintroduse?", s)
        elif extras is not None:
            add(f"banca_{cont}", OK, f"Banca {cont} {s:.2f} = extras", s)
        else:
            add(f"banca_{cont}", INFO, f"Banca {cont} {s:.2f} (fara sold de extras pentru comparatie)", s)

    # --- TVA
    tva = (cfg.get("tva") or "lunar").lower()
    if tva in ("lunar", "trimestrial"):
        if expected(tva, luna):
            d26, d27 = solduri.get("4426"), solduri.get("4427")
            if d26 is None or d27 is None:
                add("tva_inchis", WARN, "Soldurile 4426/4427 nu au putut fi citite")
            elif abs(d26) >= 0.01 or abs(d27) >= 0.01:
                add("tva_inchis", WARN,
                    f"TVA neregularizat: 4426 = {d26:.2f}, 4427 = {d27:.2f} (trebuie 0 dupa decont)")
            else:
                add("tva_inchis", OK, "4426 si 4427 inchise")
            plata = solduri.get("4423")
            d300 = (anaf or {}).get("d300_de_plata")
            if d300 is not None and plata is not None:
                if abs(plata - float(d300)) >= 1:
                    add("tva_vs_d300", WARN,
                        f"4423 in SAGA {plata:.2f} difera de D300 {float(d300):.2f} "
                        "(sau exista TVA din luni anterioare neplatit)")
                else:
                    add("tva_vs_d300", OK, f"4423 = D300 ({plata:.2f})")
        else:
            add("tva_inchis", INFO, "Platitor trimestrial: decontul se verifica la final de trimestru")

    # --- declaratii
    decl = cfg.get("declaratii") or {}
    if not decl:
        add("declaratii", INFO, "Declaratiile firmei nu sunt configurate (saga_configureaza_client)")
    elif depuse is None:
        add("declaratii", INFO, "Fara date din SPV: declaratiile nu au fost verificate")
    else:
        for form, per in sorted(decl.items()):
            if not expected(per, luna):
                continue
            # la trimestrial/semestrial/anual SPV raporteaza luna de sfarsit a perioadei
            if (form.upper(), luna, an) in depuse:
                add(f"decl_{form}", OK, f"{form} depusa")
            else:
                termen = due(an, luna, form)
                if azi > termen:
                    add(f"decl_{form}", ERR, f"{form} nedepusa, termen depasit ({termen:%d.%m.%Y})")
                else:
                    add(f"decl_{form}", WARN, f"{form} nedepusa inca (termen {termen:%d.%m.%Y})")

    # --- datorii ANAF
    if anaf and anaf.get("obligatii_plata") is not None:
        val = float(anaf["obligatii_plata"])
        if val >= 1:
            add("datorii_anaf", WARN, f"Obligatii de plata neachitate la ANAF: {val:.2f}", val)
        else:
            add("datorii_anaf", OK, "Fara obligatii restante la ANAF")

    for note in (anaf or {}).get("observatii", []) or []:
        add("observatie", WARN, str(note))

    worst = max((RANK[c["stare"]] for c in checks), default=0)
    return {
        "cif": cfg["cif"],
        "denumire": cfg.get("denumire", ""),
        "responsabil": cfg.get("responsabil", ""),
        "stare": STATUS_LABEL[worst],
        "verificari": checks,
        "de_rezolvat": [c["mesaj"] for c in checks if c["stare"] in (WARN, ERR)],
    }


# --------------------------------------------------------------------------- raport
COLORS = {"Gata": "#1f7a3a", "De lucru": "#a86400", "Eroare": "#b3261e"}
ICON = {OK: "✓", INFO: "·", WARN: "!", ERR: "✗"}


def report_html(an: int, luna: int, firme: list[dict], generat: str) -> str:
    counts = {k: sum(1 for f in firme if f["stare"] == k) for k in ("Gata", "De lucru", "Eroare")}
    rows = []
    for f in sorted(firme, key=lambda x: ({"Eroare": 0, "De lucru": 1, "Gata": 2}[x["stare"]], x["denumire"])):
        items = "".join(
            f'<li class="{c["stare"]}"><span>{ICON[c["stare"]]}</span> {html.escape(c["mesaj"])}</li>'
            for c in f["verificari"]
        )
        rows.append(
            f'<tr><td><b>{html.escape(f["denumire"] or f["cif"])}</b><br><small>CIF {f["cif"]}'
            f'{" · " + html.escape(f["responsabil"]) if f["responsabil"] else ""}</small></td>'
            f'<td><span class="pill" style="background:{COLORS[f["stare"]]}">{f["stare"]}</span></td>'
            f"<td><ul>{items}</ul></td></tr>"
        )
    return f"""<!doctype html>
<html lang="ro"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inchidere {luna:02d}.{an}</title>
<style>
body{{font:14px/1.45 system-ui,Segoe UI,sans-serif;margin:24px;color:#1b1b1b;background:#fff}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#666;margin-bottom:16px}}
.sum span{{display:inline-block;margin-right:16px;font-weight:600}}
table{{border-collapse:collapse;width:100%;margin-top:12px}} td{{border-top:1px solid #ddd;padding:10px 8px;vertical-align:top}}
.pill{{color:#fff;border-radius:10px;padding:2px 10px;font-size:12px;white-space:nowrap}}
ul{{margin:0;padding:0;list-style:none}} li span{{display:inline-block;width:14px;font-weight:700}}
li.eroare{{color:#b3261e}} li.atentie{{color:#a86400}} li.ok{{color:#1f7a3a}} li.info{{color:#666}}
</style></head><body>
<h1>Inchiderea lunii {luna:02d}.{an}</h1>
<div class="sub">Generat {html.escape(generat)} · {len(firme)} firme</div>
<div class="sum"><span style="color:#b3261e">Eroare: {counts["Eroare"]}</span>
<span style="color:#a86400">De lucru: {counts["De lucru"]}</span>
<span style="color:#1f7a3a">Gata: {counts["Gata"]}</span></div>
<table>{"".join(rows)}</table>
</body></html>
"""


def report_csv(firme: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["CIF", "Firma", "Responsabil", "Stare", "Verificare", "Rezultat", "Mesaj"])
    for f in firme:
        for c in f["verificari"]:
            w.writerow([f["cif"], f["denumire"], f["responsabil"], f["stare"], c["cod"], c["stare"], c["mesaj"]])
    return buf.getvalue()

"""Linia de comanda: `saga-mcp` porneste serverul MCP; subcomenzile gestioneaza cheia."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from . import __version__, client, keystore


def _set_key(args) -> int:
    slot = keystore.slot_for(client.norm_cif(args.cif) if args.cif else None)
    print("Lipeste cheia de acces generata in SAGA Web (Utilizatori > Integrare API).")
    print("Textul nu se afiseaza pe ecran. Apasa Enter la final.")
    key = getpass.getpass("Cheie: ").strip()
    if not key:
        print("Nicio cheie introdusa.")
        return 1
    keystore.save_key(slot, key)
    print(f"Cheia a fost salvata in depozitul securizat ({keystore.SERVICE} / {slot}).")
    return 0


def _delete_key(args) -> int:
    slot = keystore.slot_for(client.norm_cif(args.cif) if args.cif else None)
    print("Sters." if keystore.delete_key(slot) else "Nu exista cheie salvata.")
    return 0


def _test(args) -> int:
    from .server import saga_sold_cont

    cif = client.norm_cif(args.cif)
    try:
        res = asyncio.run(saga_sold_cont(cif=cif, cont=args.cont, tip="D"))
    except Exception as exc:  # noqa: BLE001
        print(f"Eroare: {exc}")
        return 1
    print(f"Conexiune OK. Soldul contului {res['cont']} la {res['data']}: {res['sold']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="saga-mcp", description="Server MCP pentru SAGA Web")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("set-key", help="salveaza cheia de acces SAGA")
    p.add_argument("--cif", help="cheie dedicata unei firme (implicit: cheia comuna)")
    p.set_defaults(func=_set_key)

    p = sub.add_parser("delete-key", help="sterge cheia salvata")
    p.add_argument("--cif")
    p.set_defaults(func=_delete_key)

    p = sub.add_parser("test", help="verifica conexiunea citind soldul unui cont")
    p.add_argument("--cif", required=True)
    p.add_argument("--cont", default="5311")
    p.set_defaults(func=_test)

    sub.add_parser("serve", help="porneste serverul MCP (implicit)")

    args = parser.parse_args(argv)
    if getattr(args, "func", None):
        return args.func(args)

    from .server import run

    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

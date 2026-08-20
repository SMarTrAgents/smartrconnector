#!/usr/bin/env python3
"""Erzeugt die eingehängte Gateway-Kopie und den Unterschied dazu.

    python3 baue_gateway_mit_ticket.py [LESEKOPIE]

Ergebnis:

  * ``gateway_mit_ticket.py``        — Live-Fassung + Einhängeblock
  * ``gateway_mit_ticket.patch``     — der Unterschied als ``diff -u``

Die Lesekopie (Vorgabe ``/home/tongie/gateway_live_20260727.py``) wird nur
gelesen. Sie ist die Fassung, die der Inhaber am 27.07.2026 selbst vom
Produktivserver geholt hat; auf dem Server geschieht hier nichts.

Warum ein Skript und keine Handarbeit: Die Live-Fassung wird alle paar Tage neu
geholt. Ein Handpatch müsste dann jedes Mal nachgezogen werden; dieses Skript
setzt den Block einfach neu an — und weil er ein einziger Anhang am Dateiende
ist, kann er nie in eine halb fertige Zeile geraten.
"""

import difflib
import os
import subprocess
import sys

from einhaengung import BLOCK

HIER = os.path.dirname(os.path.abspath(__file__))
VORGABE_QUELLE = "/home/tongie/gateway_live_20260727.py"
ZIEL = os.path.join(HIER, "gateway_mit_ticket.py")
PATCH = os.path.join(HIER, "gateway_mit_ticket.patch")

# Kennzeichen des Blocks. Steht es schon in der Quelle, ist die Quelle bereits
# eine eingehängte Kopie — dann wird abgebrochen statt zweimal eingehängt.
MARKE = "SMarTrLink — Ticketausgabe für SMarTrChrome"


def main() -> int:
    quelle = sys.argv[1] if len(sys.argv) > 1 else VORGABE_QUELLE
    with open(quelle, encoding="utf-8") as f:
        original = f.read()

    if MARKE in original:
        print(f"ABBRUCH: {quelle} trägt den Einhängeblock bereits.", file=sys.stderr)
        return 2
    if "app.include_router(smartrlink_ticket.router)" in original:
        print(f"ABBRUCH: {quelle} hängt die Ticketausgabe bereits ein.", file=sys.stderr)
        return 2

    # Reines Anhängen, ohne die letzte Zeile der Quelle anzufassen: Dann
    # besteht der Patch aus lauter Hinzufügungen. Ein Patch, der auch nur eine
    # Bestandszeile entfernt, muss beim Lesen erklärt werden — dieser nicht.
    if not original.endswith("\n"):
        original_zum_anhaengen = original + "\n"
    else:
        original_zum_anhaengen = original
    neu = original_zum_anhaengen + BLOCK.lstrip("\n")
    with open(ZIEL, "w", encoding="utf-8") as f:
        f.write(neu)

    unterschied = difflib.unified_diff(
        original.splitlines(keepends=True), neu.splitlines(keepends=True),
        fromfile="a/" + os.path.basename(quelle),
        tofile="b/gateway_mit_ticket.py", n=3)
    with open(PATCH, "w", encoding="utf-8") as f:
        f.writelines(unterschied)

    # Syntaxprobe. Sie braucht keine einzige Fremdbibliothek und schlägt
    # deshalb auch auf einem Rechner an, auf dem das Gateway nicht laufen kann.
    probe = subprocess.run([sys.executable, "-m", "py_compile", ZIEL],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        print(probe.stderr, file=sys.stderr)
        return 1

    zeilen_alt = original.count("\n")
    zeilen_neu = neu.count("\n")
    print(f"Quelle:  {quelle}  ({zeilen_alt} Zeilen)")
    print(f"Kopie:   {ZIEL}  ({zeilen_neu} Zeilen, +{zeilen_neu - zeilen_alt})")
    print(f"Patch:   {PATCH}")
    print("Syntaxprobe: bestanden")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

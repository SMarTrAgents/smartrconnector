"""Was die neue Startpruefung in DIESEM Container vorfinden wuerde.

Spiegel von smartrbrowser.py::_startpruefung. Laeuft ohne Agentenumgebung.
"""
import os

WURZELN = ("/a0/usr/plugins", "/a0/plugins")
import sys
PROFIL = sys.argv[1] if len(sys.argv) > 1 else "/a0/usr/agents/smartr-browser"


def eingeschaltet(pfad):
    an = True
    if os.path.exists(os.path.join(pfad, ".toggle-0")):
        an = False
    if os.path.exists(os.path.join(pfad, ".toggle-1")):
        an = True
    return an


def bietet_etwas(pfad):
    t = os.path.join(pfad, "tools")
    if os.path.isdir(t) and any(f.endswith(".py") for f in os.listdir(t)):
        return True
    for _, _, dateien in os.walk(os.path.join(pfad, "prompts")):
        if any(f.startswith("agent.system.tool.") for f in dateien):
            return True
    return False


offen, gesamt = [], 0
for basis in WURZELN:
    if not os.path.isdir(basis):
        continue
    for name in sorted(os.listdir(basis)):
        p = os.path.join(basis, name)
        if not os.path.isdir(p) or not os.path.exists(os.path.join(p, "plugin.yaml")):
            continue
        gesamt += 1
        if not eingeschaltet(p) or not bietet_etwas(p):
            continue
        if not os.path.exists(os.path.join(PROFIL, "plugins", name, ".toggle-0")):
            offen.append(name)

print(f"Profil: {PROFIL}")
print(f"{gesamt} Plugins mit plugin.yaml gefunden.")
print(f"{len(offen)} sind eingeschaltet, bieten dem Agenten Werkzeuge oder Prompts")
print("und sind im Browser-Profil NICHT gesperrt:")
for n in offen:
    print("   -", n)
if not offen:
    print("   (keins — die Startpruefung waere hier zufrieden)")

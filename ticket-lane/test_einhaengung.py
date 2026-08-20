"""Prüft die eingehängte Gateway-Kopie — Durchstich statt Nachbau.

`test_ticket.py` prüft das Modul gegen eine eigens gebaute FastAPI-Anwendung.
Das sagt nichts darüber, ob es im echten Gateway auch hängt: Genau diese Lücke
war der Befund („kein Programm außerhalb der Tests ruft den Router auf").

Deshalb läuft hier die Startprobe `probe_gateway_mit_ticket.py` — und zwar in
einem **eigenen Prozess**. Grund: Sie importiert das Gateway, und das hängt
seine Prüfer global in `ticket` ein (Identität, Kontingent, AGB, reauth). Im
selben Prozess würden diese Prüfer den übrigen Tests unter den Füßen
weggezogen, und `test_ticket.py` prüfte plötzlich etwas anderes als das, was
dort steht.

Fehlt eine Fremdbibliothek des Gateways (typisch: `webauthn`), wird
übersprungen statt rot gemeldet — das Ticket-Modul selbst hat diese
Abhängigkeit nicht, und ein roter Balken dafür würde nur abstumpfen.
"""

import importlib.util
import os
import subprocess
import sys

import pytest

HIER = os.path.dirname(os.path.abspath(__file__))
KOPIE = os.path.join(HIER, "gateway_mit_ticket.py")
PROBE = os.path.join(HIER, "probe_gateway_mit_ticket.py")

# Die Fremdbibliotheken, die die Gateway-Kopie beim Import braucht.
GATEWAY_ABHAENGIGKEITEN = ("fastapi", "httpx", "pydantic", "argon2", "jwt",
                           "pyotp", "webauthn", "cryptography")


def _fehlende_abhaengigkeiten():
    return [m for m in GATEWAY_ABHAENGIGKEITEN if importlib.util.find_spec(m) is None]


@pytest.mark.skipif(not os.path.exists(KOPIE),
                    reason="gateway_mit_ticket.py fehlt — erst baue_gateway_mit_ticket.py laufen lassen")
def test_kopie_ist_syntaktisch_heil():
    """Ohne eine einzige Fremdbibliothek prüfbar: Der angehängte Block darf die
    Datei nicht zerreißen."""
    erg = subprocess.run([sys.executable, "-m", "py_compile", KOPIE],
                         capture_output=True, text=True)
    assert erg.returncode == 0, erg.stderr


@pytest.mark.skipif(not os.path.exists(KOPIE), reason="gateway_mit_ticket.py fehlt")
def test_kopie_haengt_genau_einmal_ein():
    """Zweimal eingehängt wäre zweimal `/api/v1/link/request` — und welche der
    beiden Fassungen antwortet, entschiede die Reihenfolge im Router."""
    text = open(KOPIE, encoding="utf-8").read()
    assert text.count("app.include_router(smartrlink_ticket.router)") == 1
    assert text.count("import ticket as smartrlink_ticket") == 1
    # `auth_user_terms` ist Pflicht: nur diese Fassung weist eine halbe
    # Anmeldung ab (E14) und erzwingt die AGB-Zustimmung.
    assert "auth_user_terms(request, kopf)" in text
    assert "auth_user(request" not in text.split("SMarTrLink — Ticketausgabe")[-1]


@pytest.mark.skipif(not os.path.exists(KOPIE), reason="gateway_mit_ticket.py fehlt")
def test_kopie_ist_der_aktuelle_block():
    """Die Kopie wird erzeugt, nicht gepflegt — und genau deshalb kann sie alt
    sein. Dann prüfte die Startprobe eine Fassung, die niemand mehr bearbeitet,
    und ein Fix in `einhaengung.py` wäre grün, ohne im Gateway zu stehen."""
    sys.path.insert(0, HIER)
    from einhaengung import BLOCK
    assert BLOCK.lstrip("\n") in open(KOPIE, encoding="utf-8").read(), (
        "gateway_mit_ticket.py ist älter als einhaengung.py — "
        "erst 'python3 baue_gateway_mit_ticket.py' laufen lassen")


@pytest.mark.skipif(not os.path.exists(KOPIE), reason="gateway_mit_ticket.py fehlt")
def test_bindung_traegt_ihre_riegel():
    """Die Befunde S2 bis S7 vom 29.07.2026, jeder mit Namen.

    **Dieser Test beweist nichts.** Er liest Quelltext, und eine Textsuche hat
    am 29.07.2026 nachweislich fünf plausible Verschlechterungen durchgelassen.
    Der Beweis steht in `probe_gateway_mit_ticket.py`, Abschnitte 11 bis 13:
    Dort wird der Endpunkt wirklich gerufen und die Antwort gemessen.

    Hier steht nur eine Diagnose: Wird der Block je ohne einen dieser Riegel neu
    gesetzt, sagt dieser Test in einer Zeile, WELCHER fehlt — die Startprobe
    sagt bloß, dass etwas durchkommt, was nicht durchkommen darf.
    """
    block = open(KOPIE, encoding="utf-8").read().split(
        "# --- G4: Sitzung an den Agenten binden")[-1]
    # S2: Eigentum am Kontext — sonst bindet man sich in fremde Aufträge.
    assert "_own_context(uid, kontext_id)" in block
    # S3: Deckel je Nutzer — sonst legt ein Konto beliebig viele Kontexte an.
    assert "_link_bind_gesperrt(uid)" in block and "_link_bind_zaehlen(uid)" in block
    # S4: Der Schrittmodus kommt aus der Sitzung des Relays, nie aus dem Rumpf.
    assert "_link_bind_schrittmodus(lage)" in block
    assert "body.step_mode" not in block
    # S5: Client und Stufe werden geklemmt, nicht geglaubt. `full` darf in
    #     diesem Block nirgends als Wert entstehen können.
    assert 'herkunft != "smartrchrome"' in block
    assert "smartrlink_ticket.pruefe_stufe(lage.get(\"access\"))" in block
    assert 'str(lage.get("access") or "read")' not in block
    # S6: Ein leerer Geltungsbereich wird abgewiesen, nicht ausgestellt.
    assert 'error": "sitzung_ohne_bereich' in block
    assert 'list(lage.get("allow") or [])' not in block
    # S7: Die misslungene Zuordnung ist ein Fehler, kein Erfolg — und die
    #     Verbindung wird auch dann geschlossen.
    assert "con.commit()\n    except Exception as e:" in block
    # Seit der Mutationsprobe steht `db()` im try; das finally muss deshalb
    # eine Verbindung vertragen, die nie zustande kam (siehe den eigenen
    # Prüfsatz weiter unten).
    assert "    finally:\n        if con is not None:\n            con.close()" in block


@pytest.mark.skipif(not os.path.exists(PROBE), reason="Startprobe fehlt")
def test_startprobe_laeuft_durch():
    fehlt = _fehlende_abhaengigkeiten()
    if fehlt:
        pytest.skip("Gateway-Abhängigkeiten fehlen lokal: " + ", ".join(fehlt))
    erg = subprocess.run([sys.executable, PROBE], capture_output=True, text=True,
                         cwd=HIER, timeout=300)
    assert erg.returncode == 0, erg.stdout[-4000:] + "\n" + erg.stderr[-2000:]
    assert "Startprobe bestanden" in erg.stdout


def test_bindung_antwortet_auch_wenn_die_datenbank_nicht_aufgeht():
    """Kein Weg endet ohne Antwort — auch nicht der wahrscheinlichste.

    Befund der Mutationsprobe (29.07.2026): `con = db()` stand VOR dem `try`.
    Genau die naheliegendste Fehlerursache — die Datenbank ist gesperrt oder
    nicht erreichbar — lief damit als ungefangene Ausnahme in einen nackten
    500er ohne Satz für den Menschen. Und `finally: con.close()` hätte auf eine
    Verbindung zugegriffen, die es nie gab.
    """
    block = open(KOPIE, encoding="utf-8").read().split(
        "# --- G4: Sitzung an den Agenten binden")[-1]
    stelle = block[block.index("INSERT OR IGNORE INTO chat_contexts") - 400:
                   block.index("INSERT OR IGNORE INTO chat_contexts")]
    assert "con = None" in stelle and "con = db()" in stelle, \
        "db() muss INNERHALB des try stehen, mit vorinitialisiertem con"
    assert stelle.index("con = None") < stelle.index("try:") < stelle.index("con = db()"), \
        "Reihenfolge: con = None, dann try, dann db()"
    nach = block[block.index("chat_contexts insert fehlgeschlagen"):][:900]
    assert "if con is not None:" in nach, \
        "das finally muss eine fehlende Verbindung vertragen"

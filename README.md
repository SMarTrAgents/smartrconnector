# SMarTrConnector (SMarTrLink Agenten-Bridge)

Privates, proprietäres Repository der **SMarTrAgents**. Alle Rechte vorbehalten. Kein Open-Source-Vertrieb.

Dies ist der Code hinter der Produktfähigkeit **„SMarTrConnector"**: das Stück, mit dem die
Cloud-Agenten den Chrome des Nutzers über **SMarTrLink** bedienen, jede Sitzung einzeln vom
Menschen freigegeben, mit lokal bleibenden Daten und Servern in der EU.

> Stand 20.08.2026: in Produktion ausgerollt und im echten Kundenbetrieb nachweislich in Betrieb.
> Der Transport (Relay `smartr-connect` + Desktop-Client SMarTrBrowser) und die Erweiterung
> SMarTrChrome liegen in eigenen Bausteinen und sind hier NICHT enthalten.

## Zwei Teile

### `ticket-lane/` — die Ticketausgabe im Gateway
Ein FastAPI-`APIRouter` mit eigener SQLite-Ablage. Statt eines statischen Tokens stellt das Gateway
nach Passwort-Reauth und Herkunftsbindung ein **60-Sekunden-HS256-Ticket** aus. Der Relay prüft es,
`session/bind` bindet die laufende Sitzung an einen Agentenauftrag.

- `ticket.py` — die vier Endpunkte (`request`, `request/{rid}`, `confirm`, `redeem`) plus Ablage
- `einhaengung.py` — der Einhängeblock (Adapter + `session/bind`), die einzige Pflegestelle
- `baue_gateway_mit_ticket.py` — setzt den Block an eine Gateway-Lesekopie und schreibt Kopie + Patch
- `probe_gateway_mit_ticket.py` — Startprobe: der ganze Freigabeweg in einem Lauf
- `EINHAENGEN.md` — Einhänge- und Betriebsdoku, Endpunkte, Umgebungsvariablen, Rückrollweg
- `test_ticket.py`, `test_einhaengung.py` — die Testsuiten

### `agent-side/` — das Werkzeug und das abgeriegelte Profil im Kundencontainer
- `usr/agents/smartr-browser/tools/smartrbrowser.py` — das a0-Werkzeug. Die Befehlsliste ist
  **geschlossen** und kennt nur Browser-Aktionen (Lesen/Schreiben). Kein `eval`, kein `terminal`,
  kein Dateizugriff. Der Sitzungscode und der Sitzungsschein kommen aus dem Auftragskontext, nie
  aus der Modellantwort.
- `usr/agents/smartr-browser/` — das eigene, entrechtete Profil. Codeausführung, Dateizugriff,
  Mailversand und Gedächtnisschreiben sind per `.toggle-0` abgeschaltet; die Startprüfung
  scheitert laut (fail-closed), wenn ein Riegel fehlt.
- `usr/plugins/smartrlink/api/session_set.py` — nimmt den Sitzungsschein vom Gateway entgegen
  und legt ihn unter einem `_`-Schlüssel ab, der nie auf Platte geschrieben wird.
- `sperrbestand.py` — spiegelt die Startprüfung, damit man ohne Agentenumgebung sehen kann, was
  ein Container vorfände.

## Sicherheitsmodell in einem Absatz

Die gefährlichste Tür ins System bekommt den strengsten Riegel. Das Ticket lebt 60 Sekunden, ist an
den Web-Ursprung der Freigabeseite gebunden und verlangt das Kontopasswort. Die Stufe (`read`/`write`)
steht signiert im Ticket, `full` ist auf der Ticketschiene gesperrt. Der Agent bekommt ein Profil
ohne Code, Dateien, Mail und Gedächtnis und ein Werkzeug mit geschlossener Befehlsliste. Selbst wenn
eine fremde Webseite Anweisungen enthält, kann der Agent nur klicken, tippen, scrollen und lesen, und
jeden Schritt bestätigt der Mensch an seinem eigenen Bildschirm.

## Tests

```bash
cd ticket-lane
python3 baue_gateway_mit_ticket.py   # Kopie und Patch gegen eine Gateway-Lesekopie erzeugen
python3 -m pytest -q                 # 116 Tests
```

Die Werkzeug-Tests der Agentenseite (`agent-side/.../test_smartrbrowser.py`) laufen im
Agenten-Framework des Kundencontainers.

---

SMarTrAgents.ai — Mensch mit Maschine

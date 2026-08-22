# SMarTrConnector (SMarTrLink Agenten-Bridge)

Privates, proprietäres Repository der **SMarTrAgents**. Alle Rechte vorbehalten. Kein Open-Source-Vertrieb.

Dies ist der Code hinter der Produktfähigkeit **„SMarTrConnector"**: das Stück, mit dem die
Cloud-Agenten den Chrome des Nutzers über **SMarTrLink** bedienen, jede Sitzung einzeln vom
Menschen freigegeben, mit lokal bleibenden Daten und Servern in der EU.

> Stand 20.08.2026: in Produktion ausgerollt und im echten Kundenbetrieb nachweislich in Betrieb.
> Der Transport (Relay `smartr-connect` + Desktop-Client SMarTrBrowser) und die Erweiterung
> SMarTrChrome liegen in eigenen Bausteinen und sind hier NICHT enthalten.
>
> Nachtrag 22.08.2026 (SMarTrChrome 0.6.5, Agentenseite): Das Werkzeug trägt jetzt den
> **Merkzettel** — eine lokale Aktion `merken`, die nie auf den Draht geht und dem Agenten
> Auftrag und Funde über die Verlaufskürzung hinweg erhält (für Sammelaufträge). Und der
> Modus „Selbständig" der Erweiterung fragt seit 0.6.5 nicht mehr je Schritt; die
> Einzelfreigabe gilt im Handbetrieb und beim Mitdenken. Beides in Produktion ausgerollt.

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
  aus der Modellantwort. Dazu der **Merkzettel** (`merken`): auftragsgebundene Notizen, die nie
  auf den Draht gehen, nie auf Platte landen und die Verlaufskürzung überleben — das Gedächtnis
  für den EINEN Auftrag, kein Gedächtnis darüber hinaus.
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
eine fremde Webseite Anweisungen enthält, kann der Agent nur klicken, tippen, scrollen und lesen.
Die Einzelfreigabe je Schritt stellt die Erweiterung im Handbetrieb und beim Mitdenken; im vom
Menschen ausdrücklich gewählten Selbständig-Modus läuft der Auftrag seit 0.6.5 ohne Rückfragen —
was dort hält, hält baulich: Geheimfelder werden nie getippt, CAPTCHAs nie gelöst, die Sperrliste
bricht sofort ab.

## Tests

```bash
cd ticket-lane
python3 baue_gateway_mit_ticket.py   # Kopie und Patch gegen eine Gateway-Lesekopie erzeugen
python3 -m pytest -q                 # 116 Tests
```

Die Werkzeug-Tests der Agentenseite (`agent-side/.../test_smartrbrowser.py`, 156 Prüfsätze)
laufen ohne Agentenumgebung direkt mit `pytest`. Die Drift-Prüfungen halten die Befehlstabelle
gegen Relay und Erweiterung und verlangen deren Quelldateien (`SMARTRLINK_RELAY_QUELLE`,
`SMARTRLINK_ERWEITERUNG_QUELLE`); fehlen sie, scheitern genau diese Prüfungen laut — ein
fehlender Vergleichspunkt ist ein Befund, kein Grund zum Überspringen.

---

SMarTrAgents.ai — Mensch mit Maschine

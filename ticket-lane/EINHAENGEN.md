# SMarTrLink-Ticketausgabe — Einhängen ins Gateway

Baustelle D des Projekts SMarTrChrome. Stand 2026-07-29 (Nachtrag G4 und
die Befunde S1–S7; der Grundstand ist vom 27.07.).

**Verbindliche Grundlage: `Docs/SMarTrChrome/DRAHTFORMAT.md`.** Wo `spec-01` und
`spec-02` diesem Dokument widersprechen, gilt das Drahtformat; die
Spezifikationen bleiben als Begründung lesenswert, sind aber für Feldnamen,
Endpunkte und Fehlercodes nicht mehr maßgeblich.

**Auf dem Produktivserver ist nichts geschehen.** Dieses Verzeichnis enthält nur
lokale Dateien. Der Cutover ist Sache des Inhabers.

| Datei | Zweck |
|---|---|
| `ticket.py` | FastAPI-`APIRouter` mit den vier Endpunkten, eigene SQLite-Ablage |
| `test_ticket.py` | 107 Tests gegen das Modul, ohne Gateway und ohne Relay |
| `einhaengung.py` | der Einhängeblock als Text — die einzige Stelle, an der er gepflegt wird |
| `baue_gateway_mit_ticket.py` | setzt den Block an die Live-Fassung und schreibt Kopie + Patch |
| `gateway_mit_ticket.py` | **die eingehängte Kopie** — Live-Fassung vom 27.07. + Block |
| `gateway_mit_ticket.patch` | derselbe Unterschied als `diff -u`, mit `patch -p1` anwendbar |
| `probe_gateway_mit_ticket.py` | Startprobe: der ganze Freigabeweg gegen die Kopie, in einem Lauf |
| `test_einhaengung.py` | 5 Tests, die die Kopie und die Startprobe im eigenen Prozess prüfen |
| `EINHAENGEN.md` | dieses Dokument |

Alles ausführen (lokal, ohne Server):

```bash
cd "$SMarTrAgents/Deploy/smartrlink-ticket"
python3 baue_gateway_mit_ticket.py     # Kopie und Patch neu erzeugen
python3 -m pytest -q                   # 112 Tests
```

Die Kopie wird **erzeugt, nicht von Hand gepflegt**. Quelle ist die Lesekopie
`/home/tongie/gateway_live_20260727.py` (MD5 `f7fab90af159673255add3168e2293c5`),
die der Inhaber selbst vom Server geholt hat; sie bleibt unverändert. Kommt eine
neuere Fassung, wird das Skript einfach noch einmal darauf angesetzt.

---

## 1. Die Endpunkte

| Methode | Pfad | Wer ruft | Was passiert |
|---|---|---|---|
| `POST` | `/api/v1/link/request` | Erweiterung | Antrag anlegen, `verify_word` + `rid` + `redeem_key` erzeugen. **Kein Recht.** |
| `GET` | `/api/v1/link/request/{rid}` | **nur** Freigabeseite | Vorgang anzeigen. Verbraucht nichts, beliebig oft rufbar. Liefert **nie** `verify_word`, **nie** `redeem_key`, **nie** `ticket`. |
| `POST` | `/api/v1/link/confirm` | **nur** Freigabeseite (Web-Ursprung) | Identität, Eigentum am Vorgang, dann Herkunftsprüfung, Kennwortabgleich, Kontopasswort, Freigabe oder Ablehnung. Ticket wird ausgestellt und hinterlegt — steht aber **nicht** in der Antwort. |
| `POST` | `/api/v1/link/redeem` | **nur** die antragstellende Erweiterung | Ticket **genau einmal** abholen, danach verbrannt. Verlangt den `redeem_key`. |
| `POST` | `/api/v1/link/session/bind` | Erweiterung (nach dem Handschlag) | Die laufende Sitzung an einen Agentenauftrag binden (G4/E17). Steht **nicht** in `ticket.py`, sondern im Einhängeblock — er braucht `_own_context`, `_a0_resolve_target` und `chat_contexts` aus dem Gateway. |

Das Ticket ist ein HS256-JWT mit `aud="smartr-connect"`,
`scope="smartrlink-ticket"`, `jti`, `exp = iat + 60`, dazu `access`, `duration`,
`idle_timeout`, `step_mode`, `mode`, `allow`, `client`, `ext`, `tnt`, `rid` —
alles **Server-Angaben**, signiert. Kurznamen (`acc`, `dur`, `idl`, `stp`,
`scp`, `cl`) gibt es nicht mehr. Damit ist `app.py:190` (Client behauptet seine
Stufe im `auth`-Rahmen) für diesen Weg erledigt.

**Zwei Bindungen tragen die Sicherheit dieses Weges** (Drahtformat §7). Ohne sie
könnte eine übernommene Erweiterung sich selbst freigeben, denn `verify_word`
zeigt sie selbst an und den Alltags-Ausweis hat sie ebenfalls:

* `/confirm` nimmt nur den Web-Ursprung an — `Origin`-Kopfzeile **byteweise**
  gleich `LINK_CONFIRM_ORIGIN`, dazu das Rumpffeld `origin` und, falls
  vorhanden, `Sec-Fetch-Site: same-origin` und `Sec-Fetch-Mode: cors`. Ein
  Fehlschlag ist `403 herkunft_ungueltig`, setzt den Vorgang sofort auf `denied`
  und wird mit gesehenem Ursprung und `extension_id` protokolliert.
* `/redeem` nimmt nur die antragstellende Erweiterung an — `redeem_key` gegen
  den gespeicherten SHA-256-Abdruck (harte Bedingung) **und**
  `Origin: chrome-extension://<extension_id>`. Drei Fehlversuche verbrennen den
  Vorgang.

---

## 2. Die Zeilen im Gateway

**Das ist nicht mehr Theorie.** Der Block steht als Text in `einhaengung.py`,
`baue_gateway_mit_ticket.py` setzt ihn an die Live-Fassung, und
`probe_gateway_mit_ticket.py` fährt den ganzen Weg damit einmal durch. Die
Namen unten sind am Quelltext der Fassung vom 27.07. nachgeschlagen, nicht
geraten.

`ticket.py` gehört neben die Gateway-`app.py` ins Abbild (bzw. in denselben
Import-Pfad). Der Rest ist **ein einziger Block am Dateiende** von `app.py` —
Import, fünf Adapter, `include_router`.

### 2.1 Warum ein Block am Ende und nicht drei Stellen mitten in der Datei

Weil der Rückrollweg dann wörtlich stimmt: Block weg, Neustart, fertig. Und
weil die Adapter `auth_user_terms`, `db`, `ph`, `_has_active_sub` und
`_token_balance` brauchen — die stehen weiter oben. Am Dateiende ist jede
dieser Abhängigkeiten fertig definiert; eine halb aufgebaute Fassung kann gar
nicht eingehängt werden.

### 2.2 Die fünf Adapter — die tatsächlichen Gateway-Namen

Vollständig steht der Block in `einhaengung.py`. Das Wesentliche:

| Adapter | benutzt aus dem Gateway | Antwort bei Ablehnung |
|---|---|---|
| Identität | `auth_user_terms(request, kopf)` | `401 unauthorized`, `451 agb` |
| Abo/Guthaben | `_has_active_sub(nutzer)`, `_token_balance(uid)` | `402 kontingent` |
| AGB | `users.terms_version` gegen `TERMS_VERSION` | `451 agb` |
| Kontopasswort | `ph.verify(row["password_hash"], assertion)` | `403 reauth_erforderlich` |
| Sperrliste | `LINK_BLOCKLIST` aus der Umgebung | `400 bereich_ungueltig` |

Drei Entscheidungen darin sind keine Geschmacksfragen:

1. **`auth_user_terms`, nicht `auth_user`.** Nur diese Fassung weist eine halbe
   Anmeldung ab (`amr` trägt `mfa-pending` oder `mfa-setup`, Drahtformat E14)
   und erzwingt die AGB-Zustimmung. Für den gefährlichsten Weg ins System darf
   der zweite Faktor nicht der einzige sein, den man überspringen kann.
2. **Kein `?token=`.** `auth_user_terms` würde den Ausweis auch aus der
   Adresszeile nehmen. Der Adapter tut das nicht: Eine Adresszeile landet in
   Zugriffsprotokollen, im Verlauf und im `Referer`. §3 des Drahtformats
   verlangt für alle vier Endpunkte ohnehin `Authorization: Bearer`.
3. **Derselbe Argon2-Weg wie beim Login.** Derselbe `PasswordHasher` (`ph`),
   dasselbe Feld `users.password_hash`, dieselbe Fehlerbehandlung wie in
   `POST /api/v1/auth/login`. Ein zweiter Prüfweg wäre ein zweiter Ort, an dem
   eine Passwortregel altern kann.

Dazu ein Riegel, den das Gateway an dieser Stelle noch nicht hatte: **zehn
Fehlversuche beim Kontopasswort je Nutzer und zehn Minuten.** Die Ticketausgabe
zählt nur Fehlversuche beim `verify_word`; das Kontopasswort wird danach
geprüft, ein Fehlversuch dort verbrennt den Vorgang nicht. Ohne den Riegel wäre
`POST /confirm` eine Stelle, an der man ein Kontopasswort beliebig oft raten
kann, während `/api/v1/auth/login` bei 10 Versuchen je Minute dichtmacht.

`passkey` ist als Verfahren bewusst **nicht** freigeschaltet: Der WebAuthn-Weg
des Gateways läuft über `challenge`/`assertion` in zwei Schritten und passt
nicht in ein einzelnes Rumpffeld. Lieber ein Weg weniger als ein Weg, der
ungeprüft durchwinkt.

### 2.3 Einhängen

```python
smartrlink_ticket.init_db()
smartrlink_ticket.setze_identitaet_pruefer(_link_identitaet)
smartrlink_ticket.setze_kontingent_pruefer(_link_kontingent)
smartrlink_ticket.setze_agb_pruefer(_link_agb)
smartrlink_ticket.setze_reauth_pruefer(_link_reauth)
smartrlink_ticket.setze_bereichs_pruefer(_link_bereich)
app.include_router(smartrlink_ticket.router)
```

⚠️ **Ohne `setze_reauth_pruefer` gibt es überhaupt keine Freigabe** — das Modul
antwortet dann auf jedes `/confirm` mit `403 reauth_erforderlich`, auch bei
`read`. Das ist Absicht (Drahtformat E9): Das `verify_word` zeigt die Erweiterung
selbst an, es ist vor ihr also kein Geheimnis; das Kontopasswort ist das einzige
Stück, das sie nicht hat. Fehlt der Prüfer, fehlt genau dieses Stück.

Ohne `setze_kontingent_pruefer` und `setze_agb_pruefer` entstehen die Antworten
`402`/`451` nicht; das Modul schreibt beim ersten Aufruf eine Warnung ins Log.
Beide sind vor dem Scharfschalten zu verdrahten.

### 2.4 CORS (G7) — **nicht** pauschal öffnen

Die frühere Fassung dieses Dokuments schlug

```python
CORS_ORIGINS += ["chrome-extension://<store-id>"]   # NICHT so
```

vor. Das ist falsch: `CORS_ORIGINS` speist die **globale** `CORSMiddleware` mit
`allow_credentials=True`. Der Eintrag öffnet damit nicht `link/*`, sondern
**jeden** Endpunkt des Gateways für die Erweiterung — genau die pauschale
Freigabe, vor der die Überschrift warnt.

Zuerst messen, ob überhaupt etwas fehlt: Ein Service-Worker mit
`host_permissions` schickt für seine `fetch`-Aufrufe keine Vorabfrage, die
abgewiesen werden könnte. Fehlt danach wirklich etwas, gehört die Freigabe an
die vier `link/*`-Pfade und an keinen anderen — nicht in die globale Liste.

---

## 3. Umgebungsvariablen

| Variable | Pflicht | Vorgabe | Bemerkung |
|---|---|---|---|
| `JWT_SECRET` | ja | — | **muss identisch zum Relay sein**, sonst kann der Relay das Ticket nicht prüfen. Existiert im Gateway bereits. |
| `LINK_EXT_IDS` | ja | leer | Zulassungsliste der Erweiterungs-Kennungen, kommagetrennt. Eine Kennung sind **32 Kleinbuchstaben**; alles andere wird beim Einlesen verworfen und protokolliert. **Leer ⇒ jeder Antrag wird mit `403 extension_unknown` abgewiesen.** Einen Platzhalter gibt es nicht: `*` ist keine Kennung, steht damit in keiner Liste und schaltet nichts frei (§3.1). |
| `LINK_DB` | nein | `/data/link.db` | eigene SQLite-Datei. Muss auf einem **dauerhaften** Volume liegen, sonst gehen offene Anträge bei jedem Neustart verloren (kein Sicherheitsproblem, aber ein Bedienärgernis). |
| `LINK_CONFIRM_URL` | nein | `https://cloud.smartragents.ai/link/confirm` | Basis für `confirm_url` in der Antwort. |
| `LINK_CONFIRM_ORIGIN` | nein | `https://cloud.smartragents.ai` | Der **einzige** Ursprung, aus dem `/confirm` angenommen wird (§7.1). Es gibt bewusst keinen Schalter, der die Prüfung abschaltet — nur diesen, der den erlaubten Ursprung benennt. Ein leerer Wert fällt auf die Vorgabe zurück und lockert nichts. |
| `LINK_AUTO_MODUS` | nein | `0` | `1` gibt den Schrittmodus `auto` frei. Gehört nach spec-02 §16 erst in M4 — bis dahin auf `0`. |
| `LINK_BLOCKLIST` | nein | leer | Hostnamen, die für Agentensteuerung gesperrt sind, kommagetrennt. Unterdomains sind mitgesperrt: wer `bank.de` einträgt, meint nicht „aber `login.bank.de` ist frei". Leer heißt keine zusätzliche Sperre — das ist keine Lücke, denn der Geltungsbereich ist über den Antrag schon eine Positivliste; diese Liste kann ihn nur weiter verkleinern. |

Der heute einzige gültige Wert für `LINK_EXT_IDS` ist
`bnijccjilloldoelpmndoaddgccilnfd` (`smartrchrome/KENNUNG.md`). Diese Kennung
steht an vier Stellen und wird nur gemeinsam geändert.

Alle Werte werden **bei jedem Aufruf** aus der Umgebung gelesen (Muster
„Config = Hot-Reload"). Nach dem Nemotron-Vorfall gilt: Geheimnisse über den
Secrets-Mount, nicht über `docker run -e`.

---

## 4. Rückrollweg

1. Den Block am Dateiende entfernen (`mv app.py.vor-smartrlink app.py` oder
   `patch -R -p1 app.py < smartrlink.patch`) und das Gateway neu starten.
   Danach antworten alle vier Pfade wieder `404`; sonst ändert sich nichts.
   Genau dafür ist der Zusatz **ein zusammenhängender Block am Ende** und nicht
   auf drei Stellen verteilt.
2. Wer es noch kleiner will: nur die Zeile
   `app.include_router(smartrlink_ticket.router)` löschen. Import und Adapter
   dürfen stehen bleiben — sie tun ohne diese Zeile nichts.
3. `ticket.py` legt **keine** Tabelle in einer bestehenden Datenbank an und
   ändert **kein** fremdes Schema. Es benutzt ausschließlich die eigene Datei
   aus `LINK_DB`.

   ⚠️ **Eine `link.db` aus der Fassung vom 26./27.07. muss vor dem Start
   gelöscht werden.** Das Schema hat sich geändert (`anlass`, `antrag_json`,
   `kennwort_fehler`, `redeem_hash`, `redeem_fehler` statt `zweck`,
   `entwurf_json`, `fehlversuche`), und `CREATE TABLE IF NOT EXISTS` rührt eine
   vorhandene Tabelle nicht an — das Modul würde dann bei jedem Zugriff mit
   `no such column` abbrechen. Es gibt bewusst keine automatische Wanderung:
   In der Datei stehen nur Vorgänge mit 120 Sekunden Lebensdauer, ihr Verlust
   kostet einen Neuversuch in der Erweiterung.
4. Am Relay (`smartr-connect`) und am Desktop-SMarTrBrowser ändert dieses Modul
   nichts. Der produktive Desktop-Client meldet sich weiterhin mit `?token=` an
   und sendet `duration=0`; dieses Modul stellt für ihn bewusst **keine** Tickets
   aus (`client:"smartrbrowser"` → `403 client_unbekannt`), damit sein Weg
   unverändert bleibt.

---

## 5. Was erst am echten Gateway geprüft werden kann

Vier Punkte, die hier am 27.07. noch als „ungeprüft" standen, sind seit der
Startprobe erledigt — und zwar gegen die Live-Fassung, nicht gegen einen
Nachbau:

* ✅ **Namen der Gateway-Helfer.** `auth_user_terms`, `db`, `ph`,
  `_has_active_sub`, `_token_balance`, `TERMS_VERSION` — alle am Quelltext vom
  27.07. nachgeschlagen und in der Kopie tatsächlich aufgerufen.
* ✅ **Mandant.** Er steht **nicht** im Alltags-JWT, sondern in
  `users.container_id`; der Adapter reicht ihn aus der Nutzertabelle nach. Die
  Startprobe prüft, dass er als `tnt` im Ticket landet.
* ✅ **402 und 451.** Die Kennungen heißen `kontingent` und `agb` (§9), nicht
  `no_subscription`/`terms_outdated` wie in der ersten Fassung dieses Dokuments.
* ✅ **Erneute Authentisierung mit Passwort.** Läuft über denselben
  Argon2-Aufruf wie das Login; richtiges und falsches Passwort sind in der
  Startprobe je ein Prüfpunkt. **Passkey bleibt offen** — der WebAuthn-Weg
  braucht zwei Schritte und ist deshalb hier gesperrt, nicht halb gebaut.

Ehrlich benannt — das hier ist weiterhin ungeprüft:

1. **Fremdbibliotheken.** Der Import der Kopie brauchte lokal ein
   nachinstalliertes `webauthn`; alles andere war vorhanden. Im Gateway-Abbild
   ist es ohnehin drin, sonst liefe die Live-Fassung nicht.
2. **`users` muss es schon geben.** `migrate()` des Gateways erweitert die
   Tabelle, legt sie aber nicht an. Für die Startprobe wird ein Grundgerüst
   gebaut; im Betrieb kommt sie aus der Desk-Datenbank. Für einen lokalen
   Dauerlauf gehört eine **Kopie** dieser Datei nach `CRM_DB`, nie das Original.
3. **Mehrere Uvicorn-Arbeiter.** Die SQLite-Ablage läuft im WAL-Modus und die
   Ticketausgabe ist gegen Doppelausgabe abgesichert, aber unter echter
   Parallelität kann `database is locked` auftreten. Das schlägt fehl, statt ein
   zweites Ticket auszugeben — trotzdem messen.
4. **Schreibrecht auf `LINK_DB`** im Container.
5. **Uhrzeitgleichlauf Gateway ↔ Relay.** Das Ticket lebt 60 Sekunden. Ein Versatz
   von mehr als ein paar Sekunden macht Freigaben unerklärlich kaputt.
6. **CORS aus `chrome-extension://`** (siehe 2.4).
7. **Erreichbarkeit von `/api/v1/link/*`** durch Caddy und die vorhandenen
   Rate-Limits. Die Ratenbegrenzung des Gateways (`_RL_RULES`) kennt die vier
   Pfade **nicht**; die Ticketausgabe bremst selbst (je Nutzer), aber eine
   IP-Bremse davor gibt es nicht. `/redeem` darf dabei nicht mitgebremst
   werden — die Erweiterung fragt dort alle 2 Sekunden nach.
8. 🔴 **Kommen `Origin` und `Sec-Fetch-*` unverändert am Gateway an?** Daran
    hängt die ganze Herkunftsbindung aus §7. Caddy und Cloudflare reichen diese
    Kopfzeilen normalerweise durch, aber „normalerweise" ist keine Messung: vor
    dem Scharfschalten einmal ein `/confirm` aus dem echten Browser absetzen und
    im Protokoll (`link_ereignisse`) nachsehen, ob eine Herkunftsverletzung
    steht. Fehlt der `Origin`-Kopf am Gateway, weist das Modul **jede** Freigabe
    ab — fail-closed, aber unbedienbar. Der Fehler wäre dann im Vorbau zu
    beheben, nicht durch Lockern der Prüfung.
9. **Setzt Caddy `Content-Security-Policy: frame-ancestors 'none'` für
    `/link/confirm`?** (§7.3) Die Freigabeseite bricht zwar selbst ab, wenn sie
    in einem Rahmen läuft, aber beides zusammen ist die Bedingung des
    Drahtformats.

---

## 6. Was sich gegenüber der Fassung vom 26./27.07. geändert hat

Am 26./27.07. haben drei Seiten gleichzeitig gebaut und sich die Feldnamen
jeweils selbst ausgesucht. Die Gegenprobe hat die Arbeit verworfen. Diese Punkte
sind hier repariert; die Begründung steht jeweils im Drahtformat.

| Nr. | Vorher | Jetzt | Drahtformat |
|---|---|---|---|
| 1 | `GET /request/{rid}` lieferte `erbeten`, die Seite las `requested` — die Seite blockierte dadurch **immer** | Beide Seiten sagen `requested` | E1 |
| 2 | `/confirm` prüfte nur Identität und Kennwort. Beides besitzt die Erweiterung selbst; sie konnte sich damit **selbst freigeben**, ohne dass ein Mensch etwas sah | Herkunftsbindung an den Web-Ursprung, fail-closed, ein Fehlschlag verbrennt den Vorgang | §7.1, E9 |
| 3 | `/redeem` verlangte nur den Alltags-Ausweis — den hat auch die Freigabeseite, und die `rid` steht in ihrer Adresszeile | `redeem_key` (256 Bit, nur in der Antwort auf `POST /request`, nur als SHA-256-Abdruck gespeichert) plus Ursprungsprüfung | §7.2 |
| 4 | Die Seite sendete `reauth.password`, das Modul las `nachweis.assertion` — Stufe `write` war nie erteilbar | `reauth: {method, assertion}`, und zwar bei **jeder** Freigabe | E3, E9 |
| 5 | Fehlende Felder in `/confirm` wurden aus dem **Antrag** ergänzt: bei jedem Übertragungsfehler entschied der Antragsteller über seine eigene Befugnis | Fehlende Felder kommen aus `preselect` (read, 600 s, tab, confirm_each) | E8 |
| 6 | `scope` war gleichzeitig JWT-Anspruch, Objekt und Adressliste | Geltungsbereich flach: `mode` / `allow` / `tab_host`. `scope` ist nur noch der JWT-Anspruch mit dem Wert `smartrlink-ticket` | E4, E6 |
| 7 | `mode: "tab"` erzeugte ein leeres `allow` — im Relay ist das **keine** Beschränkung, sondern deren Aufhebung | Bei `tab` wird `allow = [tab_host]` gesetzt; ohne `tab_host` ist der Antrag `400 tab_host_fehlt` | E7 |
| 8 | Ticket trug Kurz- **und** Langnamen (`acc`/`access`, …) | Nur die Langnamen aus §4. Zwei Namen für einen Wert bedeuten zwei Listen, die synchron gehalten werden müssen | E0, §4 |
| 9 | Deutsche Feldnamen auf der Leitung (`kennwort`, `ansage`, `zweck`, `vorbelegung`, `grenzen`) | Englisch, snake_case: `verify_word`, `verify_word_spoken`, `purpose`, `preselect`, `limits`. Deutsch bleibt im Quelltext und in den `hinweis`-Texten | E0 |
| 10 | `LINK_EXT_IDS` mit dem Wert `*` bedeutete „alle Erweiterungen erlaubt“ — ein Zeichen in einer Umgebungsvariable hob die Zulassungsprüfung auf, und `/redeem` band danach nur noch an „irgendeine Erweiterung“ | Reine Positivliste: Die Kennung steht in der Liste, sonst `403 extension_unknown`. Einen Platzhalter gibt es nicht mehr — weder im Antrag noch in der Herkunftsbindung | §3.1, §7.2 |
| 11 | Fehlte `client` im Antrag, setzte das Modul stillschweigend `smartrchrome` ein | `client` ist Pflichtfeld: fehlt es, `400 client_fehlt`; steht etwas anderes darin, `403 client_unbekannt` | §2, §3.1 |
| 12 | `/confirm` prüfte die **Herkunft vor der Identität** und setzte den Vorgang dabei auf `denied`. Ein Unangemeldeter konnte damit mit einer fremden `rid` und falschem Ursprung eine fremde Freigabe abschießen | Erst Identität, dann Eigentum am Vorgang, dann Herkunft. Ein Fehlschlag davor verändert keinen Vorgang | §7.1 |

Bestehen bleiben die Entscheidungen, die schon vorher richtig waren: Antrag
120 s, höchstens 3 offene Vorgänge, Zustand `approved` (nicht `confirmed`),
Kennwort wird **eingetippt** und der Seite nie gezeigt, Ticket kommt aus
`POST /redeem` und nie aus dem `GET`, Modus `auto` gesperrt.

---

## 7. Was andere Baustellen von hier brauchen

**Relay (`smartr-connect`), Ticketschiene:**

1. Ticket mit `JWT_SECRET` prüfen: `aud == "smartr-connect"`,
   `scope == "smartrlink-ticket"`, `exp` in der Zukunft, `jti` vorhanden.
   Der Aliasname `smartrlink-control` entfällt ersatzlos (E4).
2. `jti` **vor dem ersten Rahmen** verbrennen, Verbrauchsliste 900 s.
3. `ausweis.sub == ticket.sub` (der Alltags-Ausweis steht im `auth`-Rahmen im
   Feld `ausweis`, das Ticket im WS-Unterprotokoll).
4. `duration` zwischen 1 und 3600 — `0` ist `4400 duration_zero_forbidden`.
5. `access` in `{read, write}`; `full` ist `4400 access_level_forbidden`.
6. `allow` nicht leer — sonst `4400 allow_leer`.
7. Der Anspruch heißt `idle_timeout`, nicht `idle`/`idl`. Vorgabe 180 s.

Stufe, Dauer, Bereich, Leerlauf und Schrittmodus kommen **ausschließlich** aus
dem Ticket, nicht aus dem `auth`-Rahmen. Auf der Altschiene (Desktop, `?token=`,
kein Unterprotokoll) bleibt alles wie bisher, insbesondere `idle_limit = 0`.

**Freigabeseite `/link/confirm`:**

* `GET /api/v1/link/request/{rid}` liefert `requested` (nur Zitat),
  `preselect` (die Voreinstellung der Bedienelemente), `limits`, `purpose`,
  `extension_id`, `remaining`, `attempts_left`, `verify_word_len`,
  `reauth_required`.
* `POST /api/v1/link/confirm` mit
  `{rid, confirm: true, origin, verify_word, access, duration, mode, allow,
  step_mode, reauth: {method: "password", assertion}}`.
  `origin` ist Pflicht und muss der `Origin`-Kopfzeile entsprechen.
* Ablehnen: `{rid, confirm: false, origin, reason}` — **ohne** Kennwort und
  ohne `reauth`, damit Abbrechen nie an einer Eingabe scheitert. Die
  Herkunftsprüfung gilt aber auch hier.
* Fehlerantworten tragen `hinweis` in deutschem Klartext; der Text ist zum
  Vorlesen gedacht und darf unverändert angezeigt werden.

**Erweiterung:**

* `POST /api/v1/link/request` → `{rid, verify_word, verify_word_spoken,
  confirm_url, redeem_key, expires_in}`. `verify_word_spoken` ist die fertige
  Buchstabierform nach deutschem Funkalphabet („K wie Kaufmann, sieben, M wie
  Martha, …") — damit Panel und Vorlesefunktion nicht zwei verschiedene
  Fassungen erzeugen.
* Der `redeem_key` wird **nur hier** ausgeliefert. Er gehört in den
  Modulspeicher, nie in `chrome.storage.local`.
* Danach `POST /api/v1/link/redeem` mit `{rid, redeem_key}` im 2-Sekunden-Takt
  (höchstens 75 Versuche), bis `state == "approved"` — dann liegt das Ticket in
  derselben Antwort. Der Anzeige-Endpunkt `GET /request/{rid}` liefert **nie**
  ein Ticket (E10).
* `403 herkunft_ungueltig` bei `/redeem` ist ein **Sicherheitsereignis**: Nach
  drei solchen Versuchen ist der Vorgang verbrannt.
* `410 ticket_bereits_abgeholt` ist ebenfalls ein **Sicherheitsereignis**, kein
  Bedienfehler: jemand anderes war schneller.

---

## 8. Bewusst nicht enthalten

* **G4 — teilweise erledigt, nicht mehr vollständig draußen.**
  `POST /api/v1/link/session/bind` steht seit dem 29.07.2026 im Einhängeblock
  (§1 und §9, Meldung 4). **Weiterhin nicht enthalten:** `GET /session` und der Widerruf.
  Der Widerruf heißt am Relay `POST /api/v1/browser/disconnect`, weil das
  Cloud-Frontend diesen Namen bereits ruft. Hier wird kein dritter Name
  erfunden.
* **G8** (die Freigabeseite selbst) — Cloud-Frontend.
* **Abrechnung.** Der Handshake kostet 0 GT (spec-02 §15). Ein
  `token_audit`-Eintrag entsteht hier nicht.
* **Aufräumdienst.** `ticket.aufraeumen()` steht bereit, ist aber für die
  Sicherheit nicht nötig: die Gültigkeit hängt an `laeuft_ab_at`, nicht an einem
  Hintergrundlauf. Ein Cron ist reine Hygiene.

---

## 9. Vier Meldungen an den Bearbeiter des Drahtformats

`DRAHTFORMAT.md` wurde am 27.07. um 18:14 von einer anderen Sitzung geschrieben,
während dieser Umbau lief. Deshalb steht hier eine Meldung statt einer
Änderung — zwei Seiten gleichzeitig in derselben Datei ist genau der Fehler,
den die Gegenprobe vom 26./27.07. schon einmal aufgedeckt hat.

**1. §9 kennt zwei Kennungen nicht, die die Ticketausgabe wirklich sendet.**
Beide gehören als je eine Zeile in die Tabelle:

| Status | `error` | Wo |
|---|---|---|
| 400 | `client_fehlt` | request (Pflichtfeld `client` fehlt, §3.1) |
| 500 | `jwt_secret_fehlt` | alle (Betriebsfehler: `JWT_SECRET` ist nicht gesetzt) |

`client_fehlt` ist die Folge von §3.1: `client` ist Pflicht, weil ein
stillschweigend eingesetztes `smartrchrome` ein Ticket für einen Client
erzeugte, den der Antrag nie genannt hat. `jwt_secret_fehlt` ist der einzige
Weg, auf dem das Modul einen 500er erzeugt; ohne Eintrag in §9 ist er für die
Freigabeseite eine unbekannte Kennung — sie übersetzt ihn heute schon richtig
(`errServer`), aber auf eine Zeile, die im Drahtformat fehlt.

**2. Die Prüfreihenfolge in §3.3 ist an der ersten Stelle falsch herum.**
Dort steht Herkunft (1) vor Identität und Eigentum (2). Die Ticketausgabe macht
es umgekehrt und ist damit strenger: Ein Fehlschlag der Herkunftsprüfung setzt
den Vorgang auf `denied` (§7.1) — stünde sie vorn, könnte ein Unangemeldeter mit
einer geratenen fremden `rid` und einem falschen Ursprung fremde Freigaben
abschießen, ohne einen einzigen gültigen Ausweis zu besitzen. Die
Herkunftsbindung verliert dabei nichts, denn ihr Zweck ist, die Erweiterung am
Selbstfreigeben zu hindern, und die hat einen gültigen Ausweis für genau diesen
Vorgang. `/redeem` prüft aus demselben Grund seit jeher zuerst die Identität.
Vorschlag für §3.3: Punkt 1 und 2 tauschen, Rest unverändert.

**3. `LINK_EXT_IDS="*"` in ABNAHME-UND-CUTOVER.md ist bereinigt.** Dort steht
jetzt die echte Kennung `bnijccjilloldoelpmndoaddgccilnfd` aus
`smartrchrome/KENNUNG.md`. Wer den Wert `*` noch irgendwo findet: Er ist keine
Kennung, steht in keiner Positivliste und schaltet nichts frei — er wirkt heute
wie eine leere Liste, also als vollständige Sperre.

---

**4. `POST /api/v1/link/session/bind` antwortet mehr, als E17 aufzählt — und
nimmt ein Feld weniger an.** Nachtrag vom 29.07.2026; E17 beschreibt den
Endpunkt mit `{code, context_id?, step_mode?}` und ohne Fehlertabelle. Beides
stimmt so nicht mehr. Die vollständige Liste dessen, was der Endpunkt
tatsächlich sendet:

| Status | `error` | Wann | Befund |
|---|---|---|---|
| 400 | `code_ungueltig` | Der Sitzungscode passt nicht auf `^[A-Z2-9]{4,12}$` | — |
| 401 | `unauthorized` | wie überall (Ausweis fehlt, halbe Anmeldung, `?token=`) | E14 |
| 403 | `kontext_unbekannt` | `context_id` gehört einem anderen Konto **oder** gibt es nicht | S2 |
| 403 | `sitzung_fremder_client` | Der Relay meldet für diesen Code einen anderen `client` als `smartrchrome` (bzw. eine `schiene` ≠ `ticket`) | S5 |
| 403 | `stufe_fuer_client_gesperrt` | Der Relay meldet `access: full` | S5, E13 |
| 400 | `stufe_unbekannt` | Der Relay meldet eine Stufe, die nicht in `STUFEN` steht | S5 |
| 403 | `sitzung_ohne_bereich` | Der Relay meldet ein leeres `allow` | S6, E7 |
| 404 | `sitzung_unbekannt` | Der Relay kennt den Code nicht | — |
| 410 | `sitzung_beendet` | Sitzung beendet, abgelaufen oder Frist bereits durch | — |
| 429 | `too_many_requests` | Deckel je Nutzer: 12 Bindungen in 10 Minuten | S3 |
| 451 | `agb` | Zustimmung offen (über den Identitätsadapter) | — |
| 500 | `zuordnung_fehlgeschlagen` | Die `chat_contexts`-Zeile ließ sich nicht schreiben | S7 |
| 502 | `relay_nicht_erreichbar` | Der Relay antwortet nicht | — |
| 502 | `agent_nicht_erreichbar` / `bindung_fehlgeschlagen` | `session_set` scheiterte oder nannte keinen Kontext | — |

Drei Punkte daraus gehören ausdrücklich ins Drahtformat, weil sie das Verhalten
und nicht nur die Kennung ändern:

* **`kontext_unbekannt` (403) macht fremd und nicht vorhanden ununterscheidbar.**
  Beide Fälle antworten wörtlich gleich. Sonst wäre der Endpunkt eine Auskunft
  darüber, welche Kontextkennungen es auf der geteilten Engine gibt.
* **Das Rumpffeld `step_mode` gibt es nicht mehr.** E17 führt es noch auf. Es
  ist ersatzlos gestrichen (Befund S4): Damit konnte sich der Antragsteller den
  Automatikmodus selbst geben, den der Server gesperrt hält (`LINK_AUTO_MODUS=0`)
  und den auch die Ticketausgabe nie erteilt. Maßgeblich ist allein, was der
  Relay für **diese** Sitzung führt; sagt er nichts oder etwas Unbekanntes, gilt
  `confirm_each`. Ein mitgesendetes `step_mode` wird stillschweigend ignoriert —
  der Rumpf kennt das Feld nicht mehr.
* **Es gibt einen Deckel je Nutzer: 12 Bindungen in 10 Minuten** (Befund S3).
  Jeder Aufruf **ohne** `context_id` legt in der Engine einen neuen
  `AgentContext` an, und auf der geteilten Engine liegen die Kontexte aller
  Kunden im selben Speicher. Gezählt wird erst, nachdem Identität und Eigentum
  am Kontext geprüft sind — ein vertippter Code soll das Kontingent des
  ehrlichen Nutzers nicht aufbrauchen. Einen ehrlichen Nutzer trifft der Deckel
  nicht: Die Erweiterung bindet je Sitzung einmal und bei der Verlängerung noch
  einmal, und die Verlängerung setzt 75 s vor dem Ablauf einer 3600-s-Sitzung an
  (`panel.js`) — also gut einmal je Stunde.

**Und die Regel hinter S5/S6, die über diesen Endpunkt hinausgeht:** Wer
`GET /api/v1/browser/status/{code}` liest, liest **auch über Sitzungen der
Altschiene**. Dort nennt der Desktop-Client Stufe und Bereich im `auth`-Rahmen
selbst und darf bis `full` und ohne Bereichsgrenze gehen (§5.2). Diese Auskunft
ist deshalb ein Bericht und keine Erlaubnis: Jeder Wert daraus gehört geklemmt,
bevor er in eine Befugnis übergeht. Für den Sitzungsschein heißt das `client`,
`access`, `allow` und `mode` — und `schiene`, sobald der Relay sie meldet.

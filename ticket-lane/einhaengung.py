"""Der Block, der die Ticketausgabe ins Gateway einhängt.

Diese Datei ist **kein** lauffähiges Modul für sich — sie ist die Textquelle für
``baue_gateway_mit_ticket.py``. Der Inhalt wird unverändert an die Live-Fassung
des Gateways angehängt und ergibt ``gateway_mit_ticket.py``.

Warum ein eigener Textbaustein und keine Handarbeit an der Kopie: Die
Live-Fassung wird alle paar Tage neu geholt. Wer den Block hier pflegt, kann die
Kopie jederzeit neu erzeugen, statt einen Handpatch nachzuziehen — und der
Unterschied bleibt genau ein zusammenhängender Block am Dateiende, also in einem
Stück prüf- und zurücknehmbar.
"""

BLOCK = r'''

# ===========================================================================
# SMarTrLink — Ticketausgabe für SMarTrChrome (Baustelle D, Stand 29.07.2026)
#
# Alles ab hier ist der EINZIGE Zusatz gegenüber der Live-Fassung. Ein
# zusammenhängender Block am Dateiende, damit der Rückrollweg aus
# EINHAENGEN.md §4 wörtlich stimmt: Block entfernen, neu starten, fertig.
#
# Verbindlich ist Docs/SMarTrChrome/DRAHTFORMAT.md. Die Fehlerkennungen unten
# stammen aus §9 dieser Datei — nicht aus den älteren Spezifikationen.
#
# Der Block wird bewusst ans Ende gesetzt und nicht zwischen die Imports:
# `ticket` bringt eigene Voreinstellungen mit, die sperren statt öffnen; die
# Prüfer-Adapter brauchen `auth_user_terms`, `db`, `ph` und `_has_active_sub`,
# und die stehen weiter oben. Am Ende ist jede dieser Abhängigkeiten fertig
# definiert, und es kann keine halb aufgebaute Fassung eingehängt werden.
# ===========================================================================

import re

import ticket as smartrlink_ticket


# --- Identität -------------------------------------------------------------

def _link_identitaet(request: Request) -> dict:
    """Wer ruft? Liefert {"user_id", "tenant", "email"} oder wirft LinkFehler.

    Benutzt wird `auth_user_terms` und ausdrücklich NICHT `auth_user`: nur die
    Terms-Variante weist eine halbe Anmeldung ab (`amr` trägt `mfa-pending`
    oder `mfa-setup`, DRAHTFORMAT E14) und erzwingt die AGB-Zustimmung. Für den
    gefährlichsten Weg ins System — eine Steuerbefugnis über einen fremden
    Browser — darf der zweite Faktor nicht der einzige sein, den man
    überspringen kann.

    Strenger als das Gateway an anderen Stellen: Der Ausweis muss in der
    `Authorization`-Kopfzeile stehen. `?token=` wird hier nicht angenommen,
    obwohl `auth_user_terms` das könnte — eine Adresszeile landet in
    Zugriffsprotokollen, im Verlauf und im `Referer`, und §3 des Drahtformats
    verlangt für alle vier Endpunkte ohnehin `Authorization: Bearer`.
    """
    kopf = request.headers.get("authorization") or ""
    if not kopf.lower().startswith("bearer "):
        raise smartrlink_ticket.LinkFehler(401, "unauthorized")
    if request.query_params.get("token"):
        # Fail-closed: Wer den Ausweis zusätzlich in die Adresszeile hängt,
        # bekommt keine Antwort, die ihm das als gangbaren Weg bestätigt.
        raise smartrlink_ticket.LinkFehler(
            401, "unauthorized",
            "Der Ausweis gehört in die Authorization-Kopfzeile, nicht in die Adresszeile.")
    try:
        nutzer = auth_user_terms(request, kopf)
    except HTTPException as e:
        raise _link_aus_http(e)
    return {
        "user_id": str(nutzer["id"]),
        # Der Mandant steht in `users.container_id` (leer bei geteilter Engine).
        "tenant": str(nutzer.get("container_id") or ""),
        "email": str(nutzer.get("email") or ""),
    }


def _link_aus_http(e: HTTPException) -> Exception:
    """HTTPException des Gateways in die Fehlersprache des Drahtformats (§9).

    Es gibt dort genau zwei Kennungen für diesen Weg: `401 unauthorized` und
    `451 agb`. Ein Serverfehler wird NICHT zu `401` verbogen — sonst sähe ein
    kaputtes Gateway wie eine abgelehnte Anmeldung aus, und niemand würde
    suchen.
    """
    status = getattr(e, "status_code", 500)
    if status == 451:
        return smartrlink_ticket.LinkFehler(
            451, "agb",
            "Bitte stimme zuerst den Nutzungsbedingungen zu.")
    if 400 <= status < 500:
        # Dazu gehört auch 403 mfa_incomplete (E14): eine halbe Anmeldung ist
        # kein Ausweis, und §9 kennt für diesen Fall nur `unauthorized`.
        return smartrlink_ticket.LinkFehler(
            401, "unauthorized",
            "Bitte melde dich vollständig an — auch mit dem zweiten Faktor.")
    return e


# --- Abo und Guthaben (402) ------------------------------------------------

def _link_kontingent(kontext: dict) -> None:
    """Muss VOR dem ersten Klick greifen: der Antrag wird abgewiesen, nicht
    erst die Sitzung. Ein Mensch, der Kennwort und Kontopasswort eintippt und
    danach „kein Guthaben" liest, hat dreimal umsonst gearbeitet.

    Die Schwellen sind die des Gateways, hier wird keine eigene erfunden:
    aktives Abo wie in `_has_active_sub`, Guthaben > 0 wie bei den übrigen
    kostenpflichtigen Wegen.
    """
    con = db()
    try:
        row = con.execute("SELECT * FROM users WHERE id=?", (kontext["user_id"],)).fetchone()
    finally:
        con.close()
    if not row:
        raise smartrlink_ticket.LinkFehler(401, "unauthorized")
    nutzer = dict(row)
    if not _has_active_sub(nutzer):
        raise smartrlink_ticket.LinkFehler(
            402, "kontingent",
            "Für die Browsersteuerung wird ein aktives Abo gebraucht.")
    if _token_balance(kontext["user_id"]) <= 0:
        raise smartrlink_ticket.LinkFehler(
            402, "kontingent",
            "Das Guthaben ist aufgebraucht. Bitte lade auf, dann geht es weiter.")


# --- Nutzungsbedingungen (451) ---------------------------------------------

def _link_agb(kontext: dict) -> None:
    """Zweite Bremse hinter `auth_user_terms`.

    Sie feuert im Normalbetrieb nie, weil die Identitätsprüfung schon 451
    wirft. Sie bleibt trotzdem eingehängt: Wird der Identitäts-Adapter je auf
    `auth_user` zurückgestellt — der die AGB nicht prüft —, fällt die
    Zustimmungspflicht sonst still weg, und niemand merkt es.
    """
    con = db()
    try:
        row = con.execute("SELECT terms_version FROM users WHERE id=?",
                          (kontext["user_id"],)).fetchone()
    finally:
        con.close()
    if not row or (row["terms_version"] or "") != TERMS_VERSION:
        raise smartrlink_ticket.LinkFehler(
            451, "agb", "Bitte stimme zuerst den Nutzungsbedingungen zu.")


# --- Zweiter Nachweis (E9) -------------------------------------------------

# Fehlversuche beim Kontopasswort, je Nutzer und Zeitfenster. Im Arbeitsspeicher
# wie die übrige Ratenbegrenzung des Gateways (_RL_BUCKETS) — ein Neustart setzt
# sie zurück, das ist bei 10 Minuten Fenster hinnehmbar.
#
# Warum es diesen Zähler braucht: Die Ticketausgabe zählt nur Fehlversuche beim
# `verify_word` (3 je Vorgang, 10 je Nutzer). Das Kontopasswort wird DANACH
# geprüft — ein Fehlversuch dort verbrennt den Vorgang nicht. Ohne diesen Riegel
# wäre `POST /confirm` eine Stelle, an der man ein Kontopasswort beliebig oft
# raten kann; `/api/v1/auth/login` ist auf 10 Versuche je Minute begrenzt, hier
# stünde die Tür offen.
_LINK_REAUTH_FEHLER: dict = {}
LINK_REAUTH_MAX_FEHLER = 10
LINK_REAUTH_FENSTER = 600


def _link_reauth_gesperrt(uid: str) -> bool:
    grenze = time.time() - LINK_REAUTH_FENSTER
    eintraege = [t for t in _LINK_REAUTH_FEHLER.get(uid, []) if t > grenze]
    if eintraege:
        _LINK_REAUTH_FEHLER[uid] = eintraege
    else:
        _LINK_REAUTH_FEHLER.pop(uid, None)
    return len(eintraege) >= LINK_REAUTH_MAX_FEHLER


def _link_reauth_fehler(uid: str) -> None:
    grenze = time.time() - LINK_REAUTH_FENSTER
    eintraege = [t for t in _LINK_REAUTH_FEHLER.get(uid, []) if t > grenze]
    eintraege.append(time.time())
    _LINK_REAUTH_FEHLER[uid] = eintraege
    if len(_LINK_REAUTH_FEHLER) > 5000:
        for k in list(_LINK_REAUTH_FEHLER.keys()):
            if not _LINK_REAUTH_FEHLER[k] or _LINK_REAUTH_FEHLER[k][-1] < grenze:
                _LINK_REAUTH_FEHLER.pop(k, None)


def _link_reauth(kontext: dict, method: str, assertion) -> bool:
    """Erneuter Nachweis bei JEDER Freigabe (E9). True = erbracht.

    Geprüft wird gegen denselben Argon2-Weg, den `POST /api/v1/auth/login`
    benutzt: derselbe `PasswordHasher` (`ph`), dasselbe Feld
    `users.password_hash`, dieselbe Fehlerbehandlung. Ein zweiter Prüfweg wäre
    ein zweiter Ort, an dem eine Passwortregel altern kann.

    `passkey` ist hier bewusst NICHT freigeschaltet: Der WebAuthn-Weg des
    Gateways läuft über `challenge`/`assertion` in zwei Schritten, das passt
    nicht in ein einzelnes Rumpffeld. Bis er sauber angebunden ist, gilt
    fail-closed — lieber ein Weg weniger als ein Weg, der ungeprüft durchwinkt.
    """
    uid = str(kontext.get("user_id") or "")
    if not uid:
        return False
    if method != "password":
        log.info("[SMARTRLINK] reauth abgelehnt: Verfahren '%s' ist hier nicht freigeschaltet",
                 str(method)[:32])
        return False
    if not isinstance(assertion, str) or not assertion or len(assertion) > 4096:
        return False
    if _link_reauth_gesperrt(uid):
        log.warning("[SMARTRLINK] reauth gesperrt: zu viele Fehlversuche user=%s", uid[:8])
        return False
    con = db()
    try:
        row = con.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()
    finally:
        con.close()
    if not row or not row["password_hash"]:
        # Konto ohne Passwort (reiner Google-/Apple-Login): Es gibt hier nichts
        # zu prüfen, also gibt es auch keine Freigabe. Der Ausweg ist ein
        # gesetztes Kontopasswort, nicht eine übersprungene Prüfung.
        _link_reauth_fehler(uid)
        return False
    try:
        ph.verify(row["password_hash"], assertion)
    except (VerifyMismatchError, InvalidHashError, VerificationError):
        _link_reauth_fehler(uid)
        return False
    except Exception as e:
        log.warning("[SMARTRLINK] reauth Prüffehler: %s", e)
        return False
    return True


# --- Geltungsbereich -------------------------------------------------------

def _link_sperrliste() -> set:
    """Hostnamen, die für Agentensteuerung gesperrt sind.

    Quelle ist `LINK_BLOCKLIST` (kommagetrennt) — über den Secrets-Mount, wie
    nach dem Nemotron-Vorfall festgelegt, und bei jedem Aufruf neu gelesen
    (Config = Hot-Reload). Leer bedeutet: keine zusätzliche Sperre. Das ist
    keine Lücke — der Geltungsbereich ist durch den Antrag schon eine
    Positivliste; diese Liste kann ihn nur weiter verkleinern.
    """
    roh = os.environ.get("LINK_BLOCKLIST", "") or ""
    return {t.strip().lower().lstrip("*.").rstrip(".")
            for t in roh.split(",") if t.strip()}


def _link_bereich(kontext: dict, allow) -> None:
    """`allow` sind die Adressen, die WIRKLICH gewährt werden.

    Bis zum 29.07.2026 bekam dieser Prüfer die Liste aus dem Antrag — bei
    „nur dieser Tab" ist die leer, und der gewährte Tab-Host lief an der
    Sperrliste vorbei. Die Ticketausgabe reicht seitdem `gewaehrte_hosts()`
    herein; hier ist also nichts mehr zu ergänzen (Befund S1).
    """
    sperre = _link_sperrliste()
    if not sperre:
        return
    for eintrag in allow or []:
        host = str(eintrag).lower().lstrip("*.").rstrip(".")
        # Auch Unterdomains einer gesperrten Domain sind gesperrt: wer
        # `bank.de` sperrt, meint nicht „aber `login.bank.de` ist frei".
        if host in sperre or any(host.endswith("." + s) for s in sperre):
            # Kennung und Status stammen aus §9 (400 bereich_ungueltig,
            # gültig für request und confirm). Ein eigenes `bereich_gesperrt`
            # wäre ein Wort mehr auf der Leitung, das keine Gegenseite kennt —
            # und der Mensch kann hier tatsächlich eine andere Adresse wählen,
            # deshalb ist 400 (weiter versuchbar) auch die ehrlichere Antwort.
            raise smartrlink_ticket.LinkFehler(
                400, "bereich_ungueltig",
                f"{eintrag} ist für die Agentensteuerung gesperrt.")


# --- G4: Sitzung an den Agenten binden --------------------------------------
#
# POST /api/v1/link/session/bind — der fehlende Schritt zwischen „verbunden"
# und „der Agent kann etwas tun". Bis zum 29.07.2026 gab es ihn nicht: Die
# Erweiterung baute eine Sitzung auf, aber kein Agentenkontext bekam je den
# Sitzungsschein (einbau.md Kap. 3) — der Chat lief als Alltagsgespräch ohne
# Werkzeuge weiter, und „Auto-Übergabe" stand als bewusst-noch-nicht im
# Runbook. Dieser Endpunkt holt das nach:
#
#   1. Identität wie bei /link/request (_link_identitaet, fail-closed), dann
#      Eigentum am mitgegebenen Kontext (_own_context) und der Deckel je
#      Nutzer. Beides sind Nachträge vom 29.07.2026: Ein fremder context_id
#      band vorher in einen fremden Auftrag, und ohne Deckel legte jeder
#      Aufruf ohne context_id einen weiteren AgentContext an.
#   2. Der Relay bestätigt die Sitzung — mit dem Ausweis des Aufrufers, nicht
#      mit dem internen Schlüssel: der Relay prüft damit selbst, dass der Code
#      diesem Nutzer gehört (Mandantentrennung bleibt beim Relay).
#      Stufe, Bereich, Ablauf und Schrittmodus des Scheins kommen aus seiner
#      Antwort — nicht aus dem Rumpf des Aufrufers. Sie werden dabei GEKLEMMT
#      und nicht geglaubt (Nachtrag 29.07.2026, Befunde S5/S6): Der Relay
#      antwortet auch über Sitzungen der Altschiene, und dort nennt der Client
#      Stufe und Bereich selbst. Schritt 2b prüft Client und Schiene, 2c den
#      Geltungsbereich, und die Stufe geht durch `pruefe_stufe`.
#   3. Ein Bridge-Token je Sitzung (werkzeuge.md Kap. 3): sub=Nutzer,
#      aud=smartr-connect, scope=smartrlink-bridge, code=Sitzung, exp=Ablauf.
#      Durch `aud` ist es am Gateway selbst als Ausweis unbrauchbar.
#   4. session_set im Tenant legt den Browser-Auftragskontext an (Profil
#      smartr-browser) und hält den Schein nur im Arbeitsspeicher.
#   5. Eine chat_contexts-Zeile, damit /chat/message den Kontext dem Nutzer
#      zuordnet — ohne sie antwortete das Gateway 403 auf den eigenen Kontext.
#      Misslingt sie, ist die Bindung unbrauchbar; der Endpunkt antwortet dann
#      500 und nicht mehr `success: true` (Befund S7).
#
# Wiederholter Aufruf mit context_id bindet NEU (Verlängerung „Unbegrenzt"):
# neuer Code, neues Bridge-Token, derselbe Auftrag.

LINK_RELAY_INTERN = os.environ.get("LINK_RELAY_INTERN", "http://smartr-connect:8080")
LINK_RELAY_PUBLIC = os.environ.get("LINK_RELAY_PUBLIC", "https://connect.smartragents.ai")
_LINK_CODE_MUSTER = re.compile(r"^[A-Z2-9]{4,12}$")

# Deckel auf die Bindung, je Nutzer und Zeitfenster. Im Arbeitsspeicher wie
# _LINK_REAUTH_FEHLER — ein Neustart setzt ihn zurück, das ist bei 10 Minuten
# Fenster hinnehmbar.
#
# Warum es ihn braucht (Befund S3, 29.07.2026): Jeder Aufruf OHNE context_id
# legt in der Engine einen neuen AgentContext an. Auf der geteilten Engine
# liegen die Kontexte aller Kunden im selben Speicher; ohne Grenze legt ein
# einziges Konto so viele an, wie es Aufrufe schafft.
#
# Warum 12 in 10 Minuten einen ehrlichen Nutzer nie treffen: Die Erweiterung
# bindet je Sitzung genau einmal und bei der Verlängerung noch einmal, und die
# Verlängerung setzt 75 s vor dem Ablauf einer 3600-s-Sitzung an (panel.js) —
# also höchstens gut einmal je Stunde. Selbst wer mehrere Tabs nacheinander
# freigibt und dazwischen scheitert, bleibt weit darunter.
_LINK_BIND_VERSUCHE: dict = {}
LINK_BIND_MAX = 12
LINK_BIND_FENSTER = 600


def _link_bind_gesperrt(uid: str) -> bool:
    grenze = time.time() - LINK_BIND_FENSTER
    eintraege = [t for t in _LINK_BIND_VERSUCHE.get(uid, []) if t > grenze]
    if eintraege:
        _LINK_BIND_VERSUCHE[uid] = eintraege
    else:
        _LINK_BIND_VERSUCHE.pop(uid, None)
    return len(eintraege) >= LINK_BIND_MAX


def _link_bind_zaehlen(uid: str) -> None:
    grenze = time.time() - LINK_BIND_FENSTER
    eintraege = [t for t in _LINK_BIND_VERSUCHE.get(uid, []) if t > grenze]
    eintraege.append(time.time())
    _LINK_BIND_VERSUCHE[uid] = eintraege
    if len(_LINK_BIND_VERSUCHE) > 5000:
        for k in list(_LINK_BIND_VERSUCHE.keys()):
            if not _LINK_BIND_VERSUCHE[k] or _LINK_BIND_VERSUCHE[k][-1] < grenze:
                _LINK_BIND_VERSUCHE.pop(k, None)


def _link_bind_schrittmodus(lage: dict) -> str:
    """Der Schrittmodus der Sitzung — aus der Meldung des Relays (Befund S4).

    Er stand vorher im Rumpf des Aufrufers. Damit konnte sich der Antragsteller
    den Automatikmodus selbst geben, den der Server gerade gesperrt hält
    (LINK_AUTO_MODUS=0) und den auch die Ticketausgabe nie erteilt. Maßgeblich
    ist, was der Relay für DIESE Sitzung führt; sagt er nichts oder etwas
    Unbekanntes, gilt confirm_each — jeder Schritt einzeln.
    """
    modus = str(lage.get("step_mode") or "").strip().lower()
    if modus not in smartrlink_ticket.SCHRITTMODI:
        return "confirm_each"
    if modus == "auto" and not smartrlink_ticket._auto_modus_frei():
        return "confirm_each"
    return modus


class LinkSessionBindIn(BaseModel):
    code: str
    context_id: Optional[str] = ""


@app.post("/api/v1/link/session/bind")
def link_session_bind(body: LinkSessionBindIn, request: Request):
    try:
        kontext = _link_identitaet(request)
    except smartrlink_ticket.LinkFehler as f:
        return f.antwort()
    uid = kontext["user_id"]

    code = (body.code or "").strip().upper()
    if not _LINK_CODE_MUSTER.match(code):
        return JSONResponse({"success": False, "error": "code_ungueltig",
                             "hinweis": "Der Sitzungscode ist nicht lesbar."}, status_code=400)

    # 1b. Wem gehört der mitgegebene Kontext? (Befund S2, 29.07.2026)
    #     Auf der geteilten Engine teilen sich mehrere Kunden den
    #     Kontextnamensraum. Ohne diese Prüfung klinkt sich, wer eine fremde
    #     Kennung kennt oder rät, in einen fremden Agentenauftrag ein — der
    #     Sitzungsschein landete in dessen Kontext. Fremd und „gibt es nicht"
    #     bekommen dieselbe Antwort, sonst wäre der Endpunkt eine Auskunft
    #     darüber, welche Kennungen es gibt.
    kontext_id = (body.context_id or "").strip()
    if kontext_id and not _own_context(uid, kontext_id):
        log.warning("[SMARTRLINK-BIND] fremder oder unbekannter Kontext user=%s ctx=%s",
                    uid[:8], kontext_id[:12])
        return JSONResponse({"success": False, "error": "kontext_unbekannt",
                             "hinweis": "Zu dieser Kennung kenne ich keinen Auftrag."},
                            status_code=403)

    # 1c. Deckel (Befund S3): Jeder Aufruf ohne context_id legt einen neuen
    #     AgentContext an. Gezählt wird ab hier — ein vertippter Code oder eine
    #     fremde Kennung soll das Kontingent des ehrlichen Nutzers nicht
    #     aufbrauchen, aber jeder Aufruf, der bis zum Relay durchgeht, zählt.
    if _link_bind_gesperrt(uid):
        log.warning("[SMARTRLINK-BIND] Deckel erreicht user=%s", uid[:8])
        return JSONResponse({"success": False, "error": "too_many_requests",
                             "hinweis": "Zu viele Verbindungsversuche in kurzer Zeit. "
                                        "Bitte in ein paar Minuten noch einmal."},
                            status_code=429)
    _link_bind_zaehlen(uid)

    # 2. Sitzung beim Relay bestätigen — mit dem Ausweis des Aufrufers.
    try:
        r = httpx.get(f"{LINK_RELAY_INTERN}/api/v1/browser/status/{code}",
                      headers={"Authorization": request.headers.get("authorization") or ""},
                      timeout=8)
    except Exception as e:
        log.warning("[SMARTRLINK-BIND] Relay nicht erreichbar: %s", e)
        return JSONResponse({"success": False, "error": "relay_nicht_erreichbar",
                             "hinweis": "Die Verbindungsstelle antwortet gerade nicht."},
                            status_code=502)
    if r.status_code == 410:
        return JSONResponse({"success": False, "error": "sitzung_beendet",
                             "hinweis": "Diese Sitzung ist schon beendet."}, status_code=410)
    if r.status_code != 200:
        return JSONResponse({"success": False, "error": "sitzung_unbekannt",
                             "hinweis": "Zu diesem Code kenne ich keine laufende Sitzung."},
                            status_code=404)
    lage = r.json() if "json" in (r.headers.get("content-type") or "") else {}
    if not lage.get("connected"):
        return JSONResponse({"success": False, "error": "sitzung_beendet",
                             "hinweis": "Diese Sitzung ist schon beendet."}, status_code=410)

    # 2b. Gehört diese Sitzung überhaupt zu SMarTrChrome? (Befund S5, 29.07.2026)
    #
    # Bis hierher nahm dieser Endpunkt jedes gemeldete Feld wörtlich. Der Relay
    # führt aber ZWEI Schienen, und auf der Altschiene (Desktop-SMarTrBrowser,
    # `?token=`) nennt der Client Stufe und Bereich im auth-Rahmen selbst:
    # `access = hello.get("access") if hello.get("access") in RANK else "read"`
    # lässt dort `full` zu, `allow` bleibt leer — und
    # `GET /browser/status/{code}` gibt beides unverändert heraus.
    #
    # Ein Schein mit `access: full` hebelt zwei Deckel auf einmal aus: Das
    # Werkzeug lässt über STUFEN_RANG["full"] = 3 jeden Befehl durch, und der
    # Relay gibt auf `full` `eval`, `terminal` und `maintenance` frei. Der
    # Clientdeckel `smartrchrome: write` (E13) stünde unberührt daneben und
    # hätte nichts gehalten — genau das ist die Lücke, die eine Positivliste
    # verhindern sollte.
    #
    # Deshalb wird hier nicht erst die Folge geklemmt, sondern die Ursache
    # benannt: Wer nicht als `smartrchrome` verbunden ist, bekommt keinen
    # Schein. Den Namen meldet der Relay als `client` (auf der Altschiene steht
    # dort fest `smartrbrowser`), die Schiene seit dem 29.07.2026 als `schiene`.
    # Die Schiene wird nur geprüft, WENN sie dabei ist: Ein älterer Relay soll
    # die Bindung nicht stilllegen, und der Clientname allein trennt die beiden
    # Schienen bereits.
    herkunft = str(lage.get("client") or "").strip().lower()
    schiene = str(lage.get("schiene") or "").strip().lower()
    if herkunft != "smartrchrome" or (schiene and schiene != "ticket"):
        log.warning("[SMARTRLINK-BIND] fremde Schiene user=%s client=%s schiene=%s",
                    uid[:8], herkunft[:32], schiene[:16])
        return JSONResponse(
            {"success": False, "error": "sitzung_fremder_client",
             "hinweis": "Dieser Code gehört zu einer Sitzung, die nicht von SMarTrChrome "
                        "stammt. Bitte verbinde die Erweiterung selbst und nimm den Code, "
                        "den sie dir anzeigt."},
            status_code=403)

    # 2c. Ohne Adresse gibt es keine Sitzung (E7, Befund S6).
    #
    # Auf der Ticketschiene ist `allow` nie leer — ein Ticket ohne Adressliste
    # weist der Relay mit `4400 allow_leer` ab. Kommt hier trotzdem eine leere
    # Liste an, wäre ein Schein daraus die WEITESTE Freigabe im System statt der
    # engsten: Der Relay prüft den Geltungsbereich nur mit `if sess.allow:` —
    # leer heißt dort nicht „nichts erlaubt", sondern „nichts geprüft". Dem
    # Menschen liest das Werkzeug dazu „nur der geöffnete Tab" vor
    # (smartrbrowser.py, _kopfzeile) und sagt ihm damit das Gegenteil dessen,
    # was gilt. Dieselbe Lehre steht als E7 in ticket.py (pruefe_bereich), in
    # panel.js und im Relay als `allow_leer`; sie gilt auch hier.
    roh_allow = lage.get("allow")
    if not isinstance(roh_allow, (list, tuple)):
        # Fremde Eingabe: Eine Zeichenkette wäre zeichenweise iterierbar und
        # ergäbe eine Adressliste aus Buchstaben. Was keine Liste ist, gilt als
        # keine Angabe.
        roh_allow = []
    bereich = [h for h in (str(x).strip().lower().rstrip(".") for x in roh_allow) if h]
    if not bereich:
        log.warning("[SMARTRLINK-BIND] Sitzung ohne Geltungsbereich user=%s code=%s",
                    uid[:8], code)
        return JSONResponse(
            {"success": False, "error": "sitzung_ohne_bereich",
             "hinweis": "Diese Sitzung nennt keine freigegebene Adresse. Bitte gib die "
                        "Seite in der Erweiterung noch einmal frei."},
            status_code=403)

    jetzt = int(time.time())
    # Die Uhr der Sitzung kommt vom Relay. Nennt er keinen Ablauf, gilt die
    # Höchstdauer der Ticketschiene (60 Minuten) und keine kurze Notfrist: Der
    # Schein ist die einzige Uhr, nach der sich der Agent richtet — steht sie zu
    # kurz, bricht er einen laufenden Auftrag mitten in der Arbeit ab. Wann die
    # Sitzung wirklich endet, entscheidet ohnehin der Relay. Nach oben wird
    # gedeckelt: Ein Schein aus diesem Endpunkt lebt nie länger als eine
    # Sitzung der Ticketschiene leben darf.
    hoechstdauer = smartrlink_ticket.MAX_DURATION
    try:
        gemeldet = int(float(lage.get("expires_at_epoch") or 0))
    except (TypeError, ValueError):
        # Was der Relay meldet, ist fremde Eingabe. Eine unlesbare Zahl darf
        # keinen 500er auslösen, sondern zählt als „kein Ablauf genannt".
        gemeldet = 0
    ablauf = min(gemeldet, jetzt + hoechstdauer) if gemeldet else (jetzt + hoechstdauer)
    if ablauf <= jetzt:
        # Der Relay meldet „verbunden", aber die Frist ist durch. Fail-closed:
        # ein Schein mit abgelaufener Uhr wäre für den Agenten unbrauchbar und
        # für den Menschen unerklärlich.
        return JSONResponse({"success": False, "error": "sitzung_beendet",
                             "hinweis": "Diese Sitzung ist schon beendet."}, status_code=410)
    # Die Stufe geht durch dieselbe Prüfung wie ein Antrag (Befund S5): `full`
    # ist für diesen Client gesperrt (403 stufe_fuer_client_gesperrt, E13), und
    # was nicht in STUFEN steht, ist keine Stufe (400 stufe_unbekannt). So steht
    # die Regel an genau einer Stelle — ein eigener Deckel hier wäre ein zweiter
    # Ort, an dem sie altern kann. Fehlt die Angabe ganz, gilt `read`.
    try:
        stufe = smartrlink_ticket.pruefe_stufe(lage.get("access"))
    except smartrlink_ticket.LinkFehler as f:
        log.warning("[SMARTRLINK-BIND] Stufe des Relays abgewiesen user=%s access=%s",
                    uid[:8], str(lage.get("access"))[:16])
        return f.antwort()
    schrittmodus = _link_bind_schrittmodus(lage)
    # Dritter Wert aus derselben Meldung, dieselbe Regel: Der Bereichsmodus wird
    # gegen die beiden bekannten Werte geklemmt (E6). Der Rückfall ist `tab` —
    # die engere Lesart; was wirklich gilt, steht ohnehin in `domains`.
    bereichsmodus = str(lage.get("mode") or "").strip().lower()
    if bereichsmodus not in ("tab", "domains"):
        bereichsmodus = "tab"

    # 3. Bridge-Token — signiert mit demselben Geheimnis, das der Relay prüft.
    try:
        geheim = smartrlink_ticket._jwt_secret()
    except smartrlink_ticket.LinkFehler as f:
        return f.antwort()
    bridge = smartrlink_ticket.jwt.encode({
        "iss": "smartr-gateway", "aud": "smartr-connect", "sub": uid,
        "scope": "smartrlink-bridge", "code": code,
        "jti": secrets.token_hex(16), "iat": jetzt, "exp": ablauf,
    }, geheim, algorithm="HS256")

    schein = {
        "code": code,
        "bridge_token": bridge,
        "access": stufe,
        "step_mode": schrittmodus,
        # `scope` ist ein OBJEKT mit `domains` — genau das liest das Werkzeug
        # (smartrbrowser.py, _kopfzeile). Hier stand eine Liste; das Werkzeug
        # prüft auf dict, bekam {} und sagte dem Menschen deshalb bei jeder
        # Sitzung „nur der geöffnete Tab", auch bei mehreren Adressen.
        # `domains` ist die oben geprüfte Liste, nicht die rohe Meldung: Sie ist
        # nachweislich nicht leer (2c) und trägt keine Einträge, die es nur dem
        # Namen nach gibt.
        "scope": {"mode": bereichsmodus, "domains": list(bereich)},
        "expires_at_epoch": ablauf,
        "task_id": f"t_{code}_{jetzt}",
        "relay_base": LINK_RELAY_PUBLIC,
        # Zeitdeckel = Restlaufzeit der Sitzung (oben berechnet). Ohne diesen
        # Wert rechnet das Werkzeug mit seiner eigenen Vorgabe von 600 s und
        # bricht eine 60-Minuten-Freigabe nach 10 Minuten hart ab
        # (smartrbrowser.py, _deckel_pruefen). Schrittdeckel bleibt eng.
        "limits": {"schritte": 60, "sekunden": ablauf - jetzt, "bilder": 2},
    }

    # 4. In den Tenant — dieselbe Zielauflösung wie der Chat (Split-Brain-Lehre).
    a0_target, api_key = _a0_resolve_target(uid)
    payload = {"context_id": kontext_id, "session": schein}
    if api_key:
        payload["api_key"] = api_key
    try:
        antwort = httpx.post(a0_target.rstrip("/") + "/api/plugins/smartrlink/session_set",
                             json=payload,
                             headers={"X-API-KEY": api_key} if api_key else {},
                             auth=(A0_LOGIN, A0_PASSWORD), timeout=20)
    except Exception as e:
        log.warning("[SMARTRLINK-BIND] session_set fehlgeschlagen: %s", e)
        return JSONResponse({"success": False, "error": "agent_nicht_erreichbar",
                             "hinweis": "Der Agent ist gerade nicht erreichbar."},
                            status_code=502)
    if antwort.status_code != 200:
        log.warning("[SMARTRLINK-BIND] session_set status=%s body=%s",
                    antwort.status_code, antwort.text[:200])
        return JSONResponse({"success": False, "error": "bindung_fehlgeschlagen",
                             "hinweis": "Ich konnte die Sitzung nicht an den Agenten übergeben."},
                            status_code=502)
    daten = antwort.json() if "json" in (antwort.headers.get("content-type") or "") else {}
    ctx = str(daten.get("context_id") or "").strip()
    if not ctx:
        return JSONResponse({"success": False, "error": "bindung_fehlgeschlagen",
                             "hinweis": "Der Agent hat keinen Auftragskontext genannt."},
                            status_code=502)

    # 5. Kontext dem Nutzer zuordnen, damit /chat/message ihn annimmt.
    #
    # Scheitert das, ist die Bindung UNBRAUCHBAR (Befund S7, 29.07.2026): Genau
    # diese Tabelle prüft `/chat/message` über `_own_context` und antwortet
    # sonst 403 — und der nächste Versuch mit derselben Kennung fällt in den
    # S2-Riegel und sagt dem Menschen „Zu dieser Kennung kenne ich keinen
    # Auftrag", also: du bist schuld. Bis zum 29.07.2026 wurde der Fehler nur
    # protokolliert und der Endpunkt meldete trotzdem `success: true`; ein
    # Fehler, der als Erfolg gemeldet wird, ist schlimmer als einer, der
    # scheitert. Die Antwort ist deshalb ein ehrlicher Serverfehler mit dem
    # nächsten Schritt darin.
    #
    # `close()` steht im `finally` wie an allen übrigen Stellen dieses Blocks
    # (_link_kontingent, _link_agb, _link_reauth): Wirft `execute`, bliebe die
    # SQLite-Verbindung sonst offen — und ausgerechnet der wahrscheinlichste
    # Grund für einen Fehler hier ist `database is locked`.
    # `db()` steht MIT im try (Befund der Mutationsprobe, 29.07.2026): Stand es
    # davor, endete ausgerechnet die wahrscheinlichste Fehlerursache — die
    # Datenbank ist gesperrt oder nicht erreichbar — ohne jede Antwort. Ein
    # ungefangener Fehler wird zu einem nackten 500 ohne Satz für den Menschen,
    # und die Regel dieses Blocks lautet: kein Weg endet ohne Antwort.
    con = None
    try:
        con = db()
        con.execute("INSERT OR IGNORE INTO chat_contexts "
                    "(context_id,user_id,agent_profile,title,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (ctx, uid, "smartr-browser", f"Browser-Auftrag {code}", now_iso(), now_iso()))
        con.commit()
    except Exception as e:
        log.warning("[SMARTRLINK-BIND] chat_contexts insert fehlgeschlagen: %s", e)
        # Der Auftrag im Kundencontainer bleibt dabei stehen. Er ist ohne diese
        # Zeile für niemanden erreichbar und läuft mit der Sitzung ab; ihn hier
        # zurückzurollen hieße, einen zweiten Weg zu bauen, der im Fehlerfall
        # ebenfalls scheitern kann.
        return JSONResponse(
            {"success": False, "error": "zuordnung_fehlgeschlagen",
             "hinweis": "Ich konnte die Bindung nicht speichern — das liegt an mir, "
                        "nicht an dir. Bitte verbinde gleich noch einmal neu."},
            status_code=500)
    finally:
        if con is not None:
            con.close()

    log.info("[SMARTRLINK-BIND] user=%s code=%s ctx=%s access=%s rest=%ss",
             uid[:8], code, ctx[:8], stufe, max(0, ablauf - jetzt))
    return {"success": True, "context_id": ctx, "code": code,
            "access": stufe, "expires_at_epoch": ablauf}


# --- Einhängen -------------------------------------------------------------

smartrlink_ticket.init_db()
smartrlink_ticket.setze_identitaet_pruefer(_link_identitaet)
smartrlink_ticket.setze_kontingent_pruefer(_link_kontingent)
smartrlink_ticket.setze_agb_pruefer(_link_agb)
smartrlink_ticket.setze_reauth_pruefer(_link_reauth)
smartrlink_ticket.setze_bereichs_pruefer(_link_bereich)
app.include_router(smartrlink_ticket.router)
log.info("[SMARTRLINK] Ticketausgabe eingehängt: /api/v1/link/* (Erweiterungen: %d zugelassen)",
         len(smartrlink_ticket._erlaubte_extensions()))
'''

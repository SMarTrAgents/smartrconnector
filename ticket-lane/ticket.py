"""SMarTrLink Ticketausgabe — eigenständiges Gateway-Modul für SMarTrChrome.

Verbindliche Grundlage ist **Docs/SMarTrChrome/DRAHTFORMAT.md**. Wo dieses Modul
und eine ältere Spezifikation sich widersprechen, gilt das Drahtformat.

Endpunkte (§3 des Drahtformats):

  * POST /api/v1/link/request        — Antrag stellen; liefert verify_word,
                                       confirm_url und den redeem_key
  * GET  /api/v1/link/request/{rid}  — Vorgang anzeigen; verbraucht nichts,
                                       liefert nie verify_word/redeem_key/ticket
  * POST /api/v1/link/confirm        — Freigabe durch den Menschen, nur aus dem
                                       Web-Ursprung (Herkunftsbindung §7.1)
  * POST /api/v1/link/redeem         — Ticket genau einmal abholen, nur von der
                                       antragstellenden Erweiterung (§7.2)

Warum ein eigenständiges Modul: Die Live-Fassung des Gateways liegt nicht im
Repo. Das Modul hängt deshalb an keiner Gateway-Interna, sondern an eingehängten
Prüfern (``setze_*_pruefer``) und einer eigenen SQLite-Datei. Einbau, Umgebungs-
variablen und Rückrollweg stehen in EINHAENGEN.md.

Das Ticket ist der einzige Gegenstand im System, der Steuerrechte trägt. Deshalb
lebt es 60 Sekunden, ist an einen ``jti`` gebunden und wird beim Abholen
verbrannt. Stufe, Dauer und Geltungsbereich sind darin **signierte Server-
Angaben** — kein Client behauptet sie mehr selbst.

Zur Namensgebung: Auf der Leitung stehen englische snake_case-Namen (§2 des
Drahtformats), im Quelltext und in den Datenbankspalten deutsche Bezeichner.
Die Datenbankspalte ``kennwort_hash`` trägt also den Abdruck des Feldes, das auf
der Leitung ``verify_word`` heißt — das ist kein zweiter Name auf der Leitung,
sondern die deutsche Innensicht.

Env: JWT_SECRET (== Relay), LINK_DB, LINK_EXT_IDS, LINK_CONFIRM_URL,
LINK_CONFIRM_ORIGIN, LINK_AUTO_MODUS. Alle werden bei jedem Aufruf gelesen
(Config = Hot-Reload).
"""

import base64
import contextlib
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import secrets
import sqlite3
import time

import jwt
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("smartrlink.ticket")

# ---------------------------------------------------------------------------
# Festlegungen aus dem Drahtformat — hier steht nichts Erfundenes.
# ---------------------------------------------------------------------------

JWT_ALG = "HS256"

# Alphabet ohne verwechselbare Zeichen (I/L/O/0/1) — identisch mit app.py:33,
# weil dasselbe Kennwort buchstabiert vorgelesen wird (Drahtformat §10).
KENNWORT_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
KENNWORT_LEN = 6

# Nur dieser eine Client bekommt hier Tickets. Der Desktop-SMarTrBrowser läuft
# unverändert über seine Altschiene — wäre er hier ausstellbar, wäre
# "client":"smartrbrowser" im Antrag ein Weg zu 8 Stunden und Stufe full.
CLIENT = "smartrchrome"

TICKET_AUD = "smartr-connect"          # Drahtformat §4
TICKET_ISS = "smartr-gateway"
# E4: Der Anspruch benennt, WAS das Token ist. "smartrlink-control" wird
# nirgends mehr ausgestellt und nirgends mehr akzeptiert.
TICKET_SCOPE = "smartrlink-ticket"
TICKET_TTL = 60                        # Sekunden; §10 "Lebensdauer des Tickets"

ANTRAG_TTL = 120                       # §10 "Lebensdauer des Antrags"
MAX_DURATION = 3600                    # §10 "Höchstdauer Ticketschiene"
MIN_DURATION = 1                       # 0 ist ein Fehler, kein Sonderfall
STANDARD_DURATION = 600                # §10 "Vorbelegte Dauer"
IDLE_TIMEOUT = 600                     # §10 "Leerlauffrist Ticketschiene"
STUFEN = ("read", "write")             # "full" ist für smartrchrome gesperrt
SCHRITTMODI = ("confirm_each", "assist", "auto")   # "assist" seit 15.08.2026 (OFFEN-v3.5 §2.1)
MAX_ALLOW = 10                         # §10 "Adressen je Sitzung"
MAX_PURPOSE = 300                      # Freitext des Antrags, nur Anzeige

# Herkunftsbindung §7. Der Web-Ursprung ist der Anker: nur von dort darf eine
# Freigabe kommen, und nur die antragstellende Erweiterung darf abholen.
CONFIRM_ORIGIN_VORGABE = "https://cloud.smartragents.ai"
EXT_ORIGIN_PRAEFIX = "chrome-extension://"
REDEEM_KEY_BYTES = 32                  # 256 Bit -> 43 Zeichen Base64url
MAX_ABHOL_FEHLER = 3                   # §7.2: drei Fehlversuche verbrennen

# Ratenbegrenzung — §10 des Drahtformats
MAX_OFFENE_ANTRAEGE = 3
MAX_ANTRAEGE_JE_FENSTER = 10
ANTRAGSFENSTER = 600                   # 10 Minuten
MAX_KENNWORT_FEHLER = 3                # je Vorgang; danach ist er verbrannt
MAX_KENNWORT_FEHLER_NUTZER = 10        # je Nutzer und Fenster
KENNWORTFENSTER = 600

# Deutsches Funkalphabet (DIN 5009). I, L, O, 0 und 1 fehlen bewusst — sie
# kommen im Kennwort-Alphabet nicht vor.
FUNKALPHABET = {
    "A": "Anton", "B": "Berta", "C": "Cäsar", "D": "Dora", "E": "Emil",
    "F": "Friedrich", "G": "Gustav", "H": "Heinrich", "J": "Julius",
    "K": "Kaufmann", "M": "Martha", "N": "Nordpol", "P": "Paula",
    "Q": "Quelle", "R": "Richard", "S": "Samuel", "T": "Theodor",
    "U": "Ulrich", "V": "Viktor", "W": "Wilhelm", "X": "Xanthippe",
    "Y": "Ypsilon", "Z": "Zacharias",
    "2": "zwei", "3": "drei", "4": "vier", "5": "fünf",
    "6": "sechs", "7": "sieben", "8": "acht", "9": "neun",
}

# Mehrteilige öffentliche Endungen, auf die kein Platzhalter zeigen darf.
# Bewusst keine vollständige Public-Suffix-Liste (keine Fremdabhängigkeit im
# Gateway): die Tenant-Sperrliste ist die zweite Bremse.
MEHRTEILIGE_ENDUNGEN = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "net.uk", "sch.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
    "com.br", "com.mx", "com.ar", "com.tr", "com.cn", "net.cn", "org.cn",
    "co.nz", "co.za", "co.in", "co.kr", "co.il", "com.sg", "com.hk",
}
DOMAIN_MUSTER = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")

# Wie eine Erweiterungskennung aussehen MUSS: 32 Kleinbuchstaben (chrome.runtime.id).
# Positivliste statt Negativliste — sie ist der Grund, weshalb ein Eintrag "*"
# in LINK_EXT_IDS nicht mehr durchrutschen kann: er ist schlicht keine Kennung.
EXT_ID_MUSTER = re.compile(r"^[a-z]{32}$")


# ---------------------------------------------------------------------------
# Konfiguration — bei jedem Aufruf gelesen, damit eine Regeländerung keinen
# Gateway-Neubau und damit kein Anmelde-Risiko für alle Kunden auslöst.
# ---------------------------------------------------------------------------

def _env(name: str, vorgabe: str = "") -> str:
    return os.environ.get(name, vorgabe).strip()


def _jwt_secret() -> str:
    geheim = _env("JWT_SECRET")
    if not geheim:
        # Ohne gemeinsames Geheimnis kann der Relay das Ticket nicht prüfen.
        # Lieber gar kein Ticket als ein unprüfbares.
        raise LinkFehler(500, "jwt_secret_fehlt",
                         "JWT_SECRET ist nicht gesetzt — es wird kein Ticket ausgestellt.")
    return geheim


def _db_pfad() -> str:
    return _env("LINK_DB", "/data/link.db")


def _confirm_url(rid: str) -> str:
    basis = _env("LINK_CONFIRM_URL", CONFIRM_ORIGIN_VORGABE + "/link/confirm")
    trenner = "&" if "?" in basis else "?"
    return f"{basis}{trenner}rid={rid}"


def _confirm_origin() -> str:
    """Der einzige Ursprung, aus dem eine Freigabe angenommen wird (§7.1).

    Es gibt bewusst keinen Schalter, der die Prüfung abschaltet — nur einen,
    der den erlaubten Ursprung benennt. Ein leerer Wert fällt auf die Vorgabe
    zurück und lockert damit nichts.
    """
    return _env("LINK_CONFIRM_ORIGIN", CONFIRM_ORIGIN_VORGABE) or CONFIRM_ORIGIN_VORGABE


def _auto_modus_frei() -> bool:
    # Modus "auto" ist bis zur Freigabe des Inhabers zu.
    return _env("LINK_AUTO_MODUS", "0") in ("1", "true", "ja")


def _erlaubte_extensions() -> set:
    """Die Zulassungsliste aus LINK_EXT_IDS — eine reine Positivliste (§3.1).

    Es gibt keinen Platzhalter mehr. Ein Eintrag, der keine Erweiterungskennung
    ist (früher vor allem ``*``), wird hier weggeworfen statt als „alle erlaubt"
    gelesen: Ein Tippfehler in einer Umgebungsvariable darf niemals die
    Zulassungsprüfung aufheben. Bleibt danach nichts übrig, wird jeder Antrag
    abgelehnt — ohne Zulassungsliste werden keine Tickets ausgestellt.
    """
    roh = _env("LINK_EXT_IDS")
    kennungen = {t.strip() for t in roh.split(",") if t.strip()}
    gueltig = {k for k in kennungen if EXT_ID_MUSTER.match(k)}
    verworfen = kennungen - gueltig
    if verworfen and "ext_ids" not in _GEWARNT:
        _GEWARNT.add("ext_ids")
        log.warning("smartrlink-ticket: %d Eintrag/Einträge in LINK_EXT_IDS sind keine "
                    "Erweiterungskennung und werden ignoriert (Platzhalter gibt es nicht mehr)",
                    len(verworfen))
    return gueltig


def vorbelegung() -> dict:
    """`preselect` — was die Freigabeseite vorbelegen MUSS (E8).

    Immer dasselbe: schwächste Stufe, kurze Dauer, engster Bereich, sicherster
    Schrittmodus. Der Wunsch der Erweiterung fließt hier ausdrücklich **nicht**
    ein; er ist ein Zitat, keine Vorauswahl.
    """
    return {"access": "read", "duration": STANDARD_DURATION, "mode": "tab",
            "allow": [], "step_mode": "confirm_each"}


def grenzen() -> dict:
    """`limits` — die harten Deckel, gegen die der Mensch entscheiden darf."""
    return {"access": list(STUFEN), "min_duration": MIN_DURATION,
            "max_duration": MAX_DURATION, "max_allow": MAX_ALLOW,
            "auto_enabled": _auto_modus_frei(), "idle_timeout": IDLE_TIMEOUT}


# ---------------------------------------------------------------------------
# Fehler
# ---------------------------------------------------------------------------

class LinkFehler(Exception):
    """Abbruch mit HTTP-Status und Klartext. Der Klartext geht an den Nutzer,
    weil er auf der Freigabeseite vorgelesen werden muss."""

    def __init__(self, status: int, code: str, hinweis: str = ""):
        super().__init__(code)
        self.status = status
        self.code = code
        self.hinweis = hinweis

    def antwort(self) -> JSONResponse:
        koerper = {"success": False, "error": self.code}
        if self.hinweis:
            koerper["hinweis"] = self.hinweis
        return JSONResponse(koerper, status_code=self.status)


# ---------------------------------------------------------------------------
# Einhängepunkte — das Gateway reicht seine eigenen Prüfungen hier hinein.
# Voreinstellungen sind bewusst so gewählt, dass ein vergessener Einhängepunkt
# sperrt statt öffnet.
# ---------------------------------------------------------------------------

_PRUEFER = {
    "identitaet": None,   # (request) -> {"user_id": str, "tenant": str, "email": str}
    "kontingent": None,   # (kontext) -> None | LinkFehler(402)
    "agb": None,          # (kontext) -> None | LinkFehler(451)
    "reauth": None,       # (kontext, method, assertion) -> bool
    "bereich": None,      # (kontext, allow) -> None | LinkFehler(403)
}
_GEWARNT = set()


def setze_identitaet_pruefer(fn):
    """Ersetzt die eingebaute JWT-Prüfung durch die des Gateways."""
    _PRUEFER["identitaet"] = fn


def setze_kontingent_pruefer(fn):
    """Abo und Guthaben — muss vor dem ersten Klick 402 werfen."""
    _PRUEFER["kontingent"] = fn


def setze_agb_pruefer(fn):
    """Aktuelle AGB angenommen? Sonst 451."""
    _PRUEFER["agb"] = fn


def setze_reauth_pruefer(fn):
    """Erneute Authentisierung — bei JEDER Freigabe (E9).

    Ohne diesen Prüfer wird überhaupt nichts erteilt. Das verify_word zeigt die
    Erweiterung selbst an, es ist vor ihr also kein Geheimnis; das Kontopasswort
    ist das einzige Stück, das sie nicht hat. Fehlt der Prüfer, fehlt genau
    dieses Stück — dann gibt es keine Freigabe.
    """
    _PRUEFER["reauth"] = fn


def setze_bereichs_pruefer(fn):
    """Tenant-Bereichsregeln, Sperrliste."""
    _PRUEFER["bereich"] = fn


async def _rufe(name: str, *args):
    fn = _PRUEFER.get(name)
    if fn is None:
        if name in ("kontingent", "agb") and name not in _GEWARNT:
            _GEWARNT.add(name)
            log.warning("smartrlink-ticket: kein %s-Prüfer eingehängt — "
                        "die Antwort 402/451 kann nicht entstehen (siehe EINHAENGEN.md)", name)
        return None
    erg = fn(*args)
    if inspect.isawaitable(erg):
        erg = await erg
    return erg


# ---------------------------------------------------------------------------
# Datenhaltung
#
# Spaltennamen sind deutsche Innensicht (siehe Modulkopf). Die Zuordnung zur
# Leitung: anlass -> purpose, antrag_json -> requested, kennwort_* ->
# verify_word, redeem_hash -> Abdruck des redeem_key.
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def db():
    conn = sqlite3.connect(_db_pfad(), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    """Legt das Schema an. Idempotent, darf beim Gateway-Start laufen."""
    ordner = os.path.dirname(_db_pfad())
    if ordner:
        os.makedirs(ordner, exist_ok=True)
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS link_vorgaenge (
              rid            TEXT PRIMARY KEY,
              user_id        TEXT NOT NULL,
              tenant         TEXT NOT NULL DEFAULT '',
              client         TEXT NOT NULL,
              version        TEXT,
              ext_id         TEXT NOT NULL,
              anlass         TEXT,
              kennwort_salz  TEXT NOT NULL,
              kennwort_hash  TEXT NOT NULL,
              kennwort_fehler INTEGER NOT NULL DEFAULT 0,
              redeem_hash    TEXT NOT NULL DEFAULT '',
              redeem_fehler  INTEGER NOT NULL DEFAULT 0,
              antrag_json    TEXT NOT NULL,
              gewaehrt_json  TEXT,
              zustand        TEXT NOT NULL DEFAULT 'pending',
              grund          TEXT,
              ticket         TEXT,
              ticket_jti     TEXT,
              erstellt_at    REAL NOT NULL,
              laeuft_ab_at   REAL NOT NULL,
              entschieden_at REAL
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_vorgang_nutzer "
                     "ON link_vorgaenge(user_id, erstellt_at)")
        # Ereignisse: es wird bei jeder Entscheidung geschrieben, nicht nur beim
        # Erfolg — ein Protokoll, das nur Erfolge kennt, verschweigt genau die
        # Vorfälle, für die man es braucht.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS link_ereignisse (
              id       INTEGER PRIMARY KEY,
              ts       REAL NOT NULL,
              rid      TEXT,
              user_id  TEXT,
              tenant   TEXT,
              ereignis TEXT NOT NULL,
              detail   TEXT
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_ereignis_nutzer "
                     "ON link_ereignisse(user_id, ts)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS link_kennwort_fehler (
              id      INTEGER PRIMARY KEY,
              ts      REAL NOT NULL,
              user_id TEXT NOT NULL
            )""")


def protokoll(ereignis: str, rid=None, user_id=None, tenant=None, detail=None):
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO link_ereignisse(ts, rid, user_id, tenant, ereignis, detail) "
                "VALUES (?,?,?,?,?,?)",
                (time.time(), rid, user_id, tenant, ereignis,
                 json.dumps(detail, ensure_ascii=False) if detail is not None else None))
    except Exception:
        # Ein fehlgeschlagener Protokolleintrag darf keine Freigabe verhindern,
        # aber auch keine unbemerkte Lücke sein.
        log.exception("smartrlink-ticket: Protokolleintrag %s fehlgeschlagen", ereignis)


# ---------------------------------------------------------------------------
# Kennwort (auf der Leitung: verify_word)
# ---------------------------------------------------------------------------

def neues_kennwort() -> str:
    return "".join(secrets.choice(KENNWORT_ALPHABET) for _ in range(KENNWORT_LEN))


def kennwort_hash(kennwort: str, salz: str) -> str:
    # Das Kennwort lebt nur 120 s, wird aber trotzdem nie im Klartext abgelegt:
    # ein Datenbankabzug wäre sonst eine Sammlung offener Freigaben.
    return hashlib.sha256((salz + kennwort).encode("utf-8")).hexdigest()


def kennwort_normalisieren(eingabe: str) -> str:
    # Der Nutzer bekommt das Kennwort buchstabiert vorgelesen und tippt es ab.
    # Leerzeichen, Bindestriche und Kleinschreibung sind kein Fehlversuch.
    return re.sub(r"[^A-Z0-9]", "", str(eingabe or "").upper())


def kennwort_ansage(kennwort: str) -> str:
    """'K wie Kaufmann, sieben, M wie Martha, …' — Feld `verify_word_spoken`."""
    teile = []
    for zeichen in kennwort:
        wort = FUNKALPHABET.get(zeichen)
        if not wort:
            teile.append(zeichen)
        elif zeichen.isdigit():
            teile.append(wort)
        else:
            teile.append(f"{zeichen} wie {wort}")
    return ", ".join(teile)


# ---------------------------------------------------------------------------
# Abholschlüssel (auf der Leitung: redeem_key)
# ---------------------------------------------------------------------------

def neuer_abholschluessel() -> str:
    """256 Bit Zufall, Base64url ohne Füllzeichen — 43 Zeichen (§2).

    Er wird ausschließlich in der Antwort auf POST /request ausgeliefert, also
    nur an die Erweiterung. Die Freigabeseite bekommt ihn nie zu sehen; ohne ihn
    könnte ein Skript im Web-Ursprung das Ticket abholen, und genau dorthin darf
    es nie gelangen (§7.2).
    """
    return base64.urlsafe_b64encode(secrets.token_bytes(REDEEM_KEY_BYTES)).decode("ascii").rstrip("=")


def abholschluessel_abdruck(schluessel: str) -> str:
    return hashlib.sha256(str(schluessel or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Identität
# ---------------------------------------------------------------------------

async def _identitaet(request: Request) -> dict:
    if _PRUEFER["identitaet"]:
        erg = await _rufe("identitaet", request)
        if not erg or not erg.get("user_id"):
            raise LinkFehler(401, "unauthorized")
        return {"user_id": str(erg["user_id"]),
                "tenant": str(erg.get("tenant") or ""),
                "email": str(erg.get("email") or "")}

    kopf = request.headers.get("authorization", "")
    if not kopf.lower().startswith("bearer "):
        raise LinkFehler(401, "unauthorized")
    try:
        # Ohne audience-Angabe weist PyJWT jedes Token mit aud-Feld ab. Genau
        # das ist gewollt: ein Ticket (aud=smartr-connect) darf sich hier
        # niemals als Alltags-Token ausgeben und neue Tickets bestellen.
        anspruch = jwt.decode(kopf[7:].strip(), _jwt_secret(), algorithms=[JWT_ALG],
                              options={"require": ["exp", "sub"]})
    except jwt.InvalidTokenError:
        raise LinkFehler(401, "unauthorized")
    if anspruch.get("scope") == TICKET_SCOPE:
        raise LinkFehler(401, "unauthorized", "Ein Ticket ist kein Anmeldenachweis.")
    return {"user_id": str(anspruch.get("sub")),
            "tenant": str(anspruch.get("tenant") or anspruch.get("tnt") or ""),
            "email": str(anspruch.get("email") or "")}


# ---------------------------------------------------------------------------
# Herkunftsbindung (§7)
# ---------------------------------------------------------------------------

def _gleich(a: str, b: str) -> bool:
    """Byteweiser Vergleich ohne Laufzeitverrat."""
    return hmac.compare_digest(str(a or "").encode("utf-8"), str(b or "").encode("utf-8"))


def _pruefe_herkunft_confirm(request: Request, koerper: dict, vorgang) -> None:
    """§7.1 — eine Freigabe wird nur aus dem Web-Ursprung angenommen.

    Origin und Sec-Fetch-* stehen auf der Liste der verbotenen Kopfzeilennamen:
    weder Seitenskript noch Erweiterung können sie setzen, der Browser setzt
    sie. Bei einem POST sendet der Browser Origin auch bei gleichem Ursprung.

    Das Rumpffeld `origin` bringt keine zusätzliche kryptografische Stärke; es
    macht aus einer stillen Kopfzeilenprüfung eine Zusage, die im Protokoll und
    im Test sichtbar ist.

    Jeder Fehlschlag ist 403 herkunft_ungueltig, setzt den Vorgang sofort auf
    'denied' und wird protokolliert. Ein Versuch verbrennt die Freigabe.
    """
    erlaubt = _confirm_origin()
    gesehen = request.headers.get("origin", "")
    erklaert = koerper.get("origin")
    site = request.headers.get("sec-fetch-site", "")
    modus = request.headers.get("sec-fetch-mode", "")

    if gesehen.startswith(EXT_ORIGIN_PRAEFIX):
        # Eigener Protokollsatz, weil genau dieser Fall der Angriffsversuch ist,
        # den man sehen will: die Erweiterung gibt sich selbst frei.
        _herkunft_verletzt(vorgang, "herkunft_erweiterung", gesehen)
    if not gesehen or not _gleich(gesehen, erlaubt):
        _herkunft_verletzt(vorgang, "herkunft_verletzt", gesehen)
    if not isinstance(erklaert, str) or not _gleich(erklaert, gesehen):
        _herkunft_verletzt(vorgang, "herkunft_ohne_erklaerung", gesehen)
    # Die Sec-Fetch-Kopfzeilen fehlen in alten Browsern; vorhanden müssen sie
    # aber stimmen — ein "cross-site" ist eine Ansage, keine Lücke.
    if site and site != "same-origin":
        _herkunft_verletzt(vorgang, "herkunft_fremde_site", f"{gesehen} ({site})")
    if modus and modus != "cors":
        _herkunft_verletzt(vorgang, "herkunft_falscher_modus", f"{gesehen} ({modus})")


def _herkunft_verletzt(vorgang, ereignis: str, gesehen: str) -> None:
    """Verbrennt den Vorgang und bricht ab. Kein Rückweg, keine Wiederholung."""
    if vorgang:
        _entscheide(vorgang["rid"], "denied", "herkunft_ungueltig")
        protokoll(ereignis, vorgang["rid"], vorgang["user_id"], vorgang["tenant"],
                  {"gesehener_ursprung": gesehen[:200],
                   "erwarteter_ursprung": _confirm_origin(),
                   "extension_id": vorgang["ext_id"]})
    else:
        protokoll(ereignis, detail={"gesehener_ursprung": gesehen[:200],
                                    "erwarteter_ursprung": _confirm_origin()})
    raise LinkFehler(403, "herkunft_ungueltig",
                     "Eine Freigabe wird nur auf der Freigabeseite angenommen.")


def _pruefe_herkunft_redeem(request: Request, koerper: dict, vorgang: dict) -> None:
    """§7.2 — abholen darf nur die antragstellende Erweiterung.

    Harte Bedingung ist der redeem_key: Die Freigabeseite hat denselben
    Alltags-Ausweis und kennt die rid aus ihrer eigenen Adresszeile — ohne
    eigenes Geheimnis könnte ein Skript im Web-Ursprung das Ticket abholen.
    Der Ursprung chrome-extension://<extension_id> ist die zusätzliche Bremse.
    """
    schluessel = koerper.get("redeem_key")
    gespeichert = vorgang["redeem_hash"] or ""
    schluessel_ok = bool(gespeichert) and isinstance(schluessel, str) and _gleich(
        abholschluessel_abdruck(schluessel), gespeichert)

    gesehen = request.headers.get("origin", "")
    ext_id = vorgang["ext_id"] or ""
    # Genau ein Ursprung wird angenommen: der der antragstellenden Erweiterung.
    # Einen Platzhalter gibt es nicht, und eine Zeile ohne gespeicherte Kennung
    # (Altbestand) holt gar nichts ab — fail-closed, nicht "irgendeine
    # Erweiterung reicht".
    herkunft_ok = bool(ext_id) and _gleich(gesehen, EXT_ORIGIN_PRAEFIX + ext_id)

    if schluessel_ok and herkunft_ok:
        return

    verbrannt = _abholfehler(vorgang["rid"])
    protokoll("abholung_herkunft_verletzt", vorgang["rid"], vorgang["user_id"],
              vorgang["tenant"],
              {"schluessel_ok": schluessel_ok, "gesehener_ursprung": gesehen[:200],
               "extension_id": ext_id, "vorgang_verbrannt": verbrannt})
    raise LinkFehler(403, "herkunft_ungueltig",
                     "Dieser Schein gehört zu einer anderen Erweiterung.")


# ---------------------------------------------------------------------------
# Prüfungen des Antrags
# ---------------------------------------------------------------------------

def pruefe_stufe(access) -> str:
    stufe = str(access or "read").strip().lower()
    if stufe == "full":
        # Für diesen Client gibt es die Stufe nicht — auch nicht auf
        # ausdrücklichen Wunsch des Nutzers.
        raise LinkFehler(403, "stufe_fuer_client_gesperrt",
                         "Vollzugriff ist für SMarTrChrome gesperrt.")
    if stufe not in STUFEN:
        raise LinkFehler(400, "stufe_unbekannt", f"Erlaubt sind: {', '.join(STUFEN)}.")
    return stufe


def pruefe_dauer(duration) -> int:
    if isinstance(duration, bool):
        raise LinkFehler(400, "dauer_ungueltig", "Die Dauer muss eine Zahl in Sekunden sein.")
    try:
        dauer = int(duration)
    except (TypeError, ValueError):
        raise LinkFehler(400, "dauer_ungueltig", "Die Dauer muss eine Zahl in Sekunden sein.")
    if dauer < MIN_DURATION or dauer > MAX_DURATION:
        # 0 ist hier ein Fehler und kein Sonderfall — eine Sitzung ohne Ablauf
        # darf auf der Ticketschiene nicht entstehen.
        raise LinkFehler(400, "dauer_ausserhalb_deckel",
                         f"Die Dauer muss zwischen {MIN_DURATION} und {MAX_DURATION} Sekunden liegen.")
    return dauer


def pruefe_schrittmodus(step_mode) -> str:
    modus = str(step_mode or "confirm_each").strip().lower()
    if modus not in SCHRITTMODI:
        raise LinkFehler(400, "modus_unbekannt", f"Erlaubt sind: {', '.join(SCHRITTMODI)}.")
    if modus == "auto" and not _auto_modus_frei():
        raise LinkFehler(403, "modus_noch_gesperrt",
                         "Der Automatikmodus ist noch nicht freigegeben.")
    return modus


def domain_normalisieren(roh: str) -> dict:
    """Prüft einen Bereichseintrag und gibt beide Schreibweisen zurück.

    Punycode und Anzeigeform werden beide geliefert, weil ein Mensch sonst
    'аmazon.de' mit kyrillischem a nicht von 'amazon.de' unterscheiden kann.
    """
    text = str(roh or "").strip().lower().rstrip(".")
    if not text:
        raise LinkFehler(400, "bereich_ungueltig", "Ein Bereichseintrag ist leer.")
    if "://" in text or "/" in text or ":" in text or "@" in text or " " in text:
        raise LinkFehler(400, "bereich_ungueltig",
                         f"'{roh}' ist keine Domain — nur der Hostname gehört hier hinein.")

    platzhalter = text.startswith("*.")
    rumpf = text[2:] if platzhalter else text
    if not rumpf or "*" in rumpf:
        raise LinkFehler(400, "bereich_zu_weit",
                         "Ein Platzhalter ist nur in der Form *.beispiel.de erlaubt.")

    try:
        ascii_form = rumpf.encode("idna").decode("ascii")
    except Exception:
        raise LinkFehler(400, "bereich_ungueltig", f"'{roh}' ist keine gültige Domain.")
    if len(ascii_form) > 253 or not DOMAIN_MUSTER.match(ascii_form):
        raise LinkFehler(400, "bereich_ungueltig", f"'{roh}' ist keine gültige Domain.")
    if ascii_form.count(".") < 1 or ascii_form in MEHRTEILIGE_ENDUNGEN:
        # '*.de' oder '*.co.uk' wären eine Freigabe für das halbe Netz.
        raise LinkFehler(400, "bereich_zu_weit",
                         f"'{roh}' ist eine öffentliche Endung und keine einzelne Adresse.")
    try:
        anzeige = ascii_form.encode("ascii").decode("idna")
    except Exception:
        anzeige = ascii_form
    return {
        "ascii": ("*." + ascii_form) if platzhalter else ascii_form,
        "anzeige": ("*." + anzeige) if platzhalter else anzeige,
        "punycode": anzeige != ascii_form,
    }


def pruefe_bereich(mode, allow, tab_host) -> dict:
    """Der Geltungsbereich, flach und ohne Verschachtelung (E6).

    Gibt genau die drei Felder zurück, die auch über die Leitung gehen:
    ``mode``, ``allow``, ``tab_host``. Ein Objekt mit ``domains`` gibt es nicht
    mehr — dasselbe Wort hatte drei Bedeutungen, und genau daran ist die letzte
    Fassung gescheitert.
    """
    modus = str(mode or "").strip().lower()
    if not modus:
        modus = "domains" if allow else "tab"
    if modus not in ("tab", "domains"):
        raise LinkFehler(400, "bereich_ungueltig", "Der Bereichsmodus ist unbekannt.")

    roh_liste = allow if allow is not None else []
    if isinstance(roh_liste, str) or not isinstance(roh_liste, (list, tuple)):
        raise LinkFehler(400, "bereich_ungueltig", "Die Adressliste ist keine Liste.")
    if len(roh_liste) > MAX_ALLOW:
        raise LinkFehler(400, "bereich_zu_gross",
                         f"Höchstens {MAX_ALLOW} Adressen je Sitzung.")

    eintraege, gesehen = [], set()
    for eintrag in roh_liste:
        geprueft = domain_normalisieren(eintrag)["ascii"]
        if geprueft in gesehen:
            continue
        gesehen.add(geprueft)
        eintraege.append(geprueft)

    host = domain_normalisieren(tab_host)["ascii"] if tab_host else ""
    if host.startswith("*."):
        # Ein Tab hat genau einen Host, keinen Platzhalter.
        raise LinkFehler(400, "bereich_ungueltig", "tab_host ist ein Hostname, kein Muster.")

    if modus == "tab" and not host:
        # E7: Ohne tab_host wäre "nur dieser Tab" eine Sitzung mit leerem
        # allow — und ein leeres allow ist im Relay keine Beschränkung,
        # sondern die Aufhebung jeder Beschränkung.
        raise LinkFehler(400, "tab_host_fehlt",
                         "Für 'nur dieser Tab' wird die Adresse des Tabs gebraucht.")
    if modus == "domains" and not eintraege:
        raise LinkFehler(400, "bereich_leer",
                         "Für den Modus 'domains' wird mindestens eine Adresse gebraucht.")
    return {"mode": modus, "allow": eintraege, "tab_host": host}


def gewaehrte_hosts(bereich: dict) -> list:
    """Die Adressen, die aus diesem Bereich WIRKLICH gewährt werden.

    Befund vom 29.07.2026: Der Bereichsprüfer (Tenant-Sperrliste) bekam die
    Liste ``allow`` aus dem Antrag. Bei ``mode: tab`` ist sie leer — gewährt
    wird danach ``[tab_host]``, den der Prüfer nie zu sehen bekam. Ein Antrag
    für eine gesperrte Bank kam so mit 201/approved/write/3600 s durch.

    Deshalb gibt es genau eine Stelle, die sagt, was gewährt wird, und alle
    Prüfungen hängen daran: was hier herauskommt, steht danach im Ticket
    (E7 — bei ``tab`` ist die Adressliste genau der Tab-Host).
    """
    if bereich.get("mode") == "tab":
        host = bereich.get("tab_host") or ""
        return [host] if host else []
    return list(bereich.get("allow") or [])


def pruefe_extension(ext_id: str) -> str:
    """§3.1 — die Kennung steht in der Liste, sonst 403. Sonst gibt es nichts.

    Es gibt keinen Platzhalter: Ein Schalter, der „alle erlaubt" bedeutet, ist
    genau der Fail-open, gegen den die ganze Herkunftsbindung gebaut ist — die
    Bindung bei /redeem (§7.2) hängt daran, dass hier eine konkrete Kennung
    gespeichert wurde. Leere Liste = jeder Antrag wird abgelehnt.
    """
    kennung = str(ext_id or "").strip()
    erlaubt = _erlaubte_extensions()
    if not erlaubt:
        # Ohne Zulassungsliste kann jede beliebige Erweiterung Anträge stellen.
        # Das ist kein Zustand, in dem Tickets ausgestellt werden dürfen.
        raise LinkFehler(403, "extension_unknown",
                         "Es ist keine Erweiterung zugelassen (LINK_EXT_IDS fehlt).")
    if not kennung or not EXT_ID_MUSTER.match(kennung) or kennung not in erlaubt:
        raise LinkFehler(403, "extension_unknown",
                         "Diese Erweiterung ist für dieses Konto nicht zugelassen.")
    return kennung


# ---------------------------------------------------------------------------
# Vorgänge
# ---------------------------------------------------------------------------

def neue_kennung(praefix: str) -> str:
    # 128 Bit Zufall, base32 ohne Füllzeichen.
    roh = base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")
    return f"{praefix}_{roh}"


SPALTEN = ("rid", "user_id", "tenant", "client", "version", "ext_id", "anlass",
           "kennwort_salz", "kennwort_hash", "kennwort_fehler",
           "redeem_hash", "redeem_fehler",
           "antrag_json", "gewaehrt_json", "zustand", "grund", "ticket",
           "ticket_jti", "erstellt_at", "laeuft_ab_at", "entschieden_at")


def _lade(conn, rid: str):
    zeile = conn.execute(
        f"SELECT {', '.join(SPALTEN)} FROM link_vorgaenge WHERE rid=?", (rid,)).fetchone()
    return dict(zip(SPALTEN, zeile)) if zeile else None


def _zustand_jetzt(vorgang: dict) -> str:
    """Ablauf wird abgeleitet, nicht durch einen Aufräumlauf erzeugt — sonst
    hinge die Gültigkeit einer Freigabe an einem Hintergrunddienst."""
    if vorgang["zustand"] in ("pending", "approved") and time.time() >= vorgang["laeuft_ab_at"]:
        return "expired"
    return vorgang["zustand"]


def _restzeit(vorgang: dict) -> int:
    return max(0, int(round(vorgang["laeuft_ab_at"] - time.time())))


# ---------------------------------------------------------------------------
# Ticket
# ---------------------------------------------------------------------------

def baue_ticket(vorgang: dict, gewaehrt: dict) -> tuple:
    """Erzeugt das Einweg-Ticket. Gibt (jwt, jti, claims) zurück.

    Die Anspruchsnamen stehen wörtlich in §4 des Drahtformats. Kurznamen
    (acc, dur, idl, stp, scp, cl) gibt es nicht mehr: zwei Namen für einen Wert
    bedeuten zwei Listen, die synchron gehalten werden müssen, und genau daran
    ist die letzte Fassung gescheitert.
    """
    jetzt = int(time.time())
    jti = neue_kennung("tk")
    anspruch = {
        "iss": TICKET_ISS,
        "aud": TICKET_AUD,
        "sub": vorgang["user_id"],
        "scope": TICKET_SCOPE,
        "jti": jti,
        "iat": jetzt,
        "exp": jetzt + TICKET_TTL,
        "rid": vorgang["rid"],
        "tnt": vorgang["tenant"],
        "ext": vorgang["ext_id"],
        "client": CLIENT,
        "access": gewaehrt["access"],
        "duration": gewaehrt["duration"],
        "idle_timeout": gewaehrt["idle_timeout"],
        "step_mode": gewaehrt["step_mode"],
        "mode": gewaehrt["mode"],
        "allow": list(gewaehrt["allow"]),
    }
    return jwt.encode(anspruch, _jwt_secret(), algorithm=JWT_ALG), jti, anspruch


# ---------------------------------------------------------------------------
# Endpunkte
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/link", tags=["smartrlink"])


@router.post("/request")
async def link_request(request: Request):
    """§3.1 — Antrag stellen.

    Sitzungsfreigabe (E15/E16): Die vollständige Anmeldung bewilligt sofort —
    für read und write (Entscheid des Inhabers, 29.07.2026), mit gewünschter
    Dauer bis MAX_DURATION. Die Antwort trägt `state: approved`, den
    gedeckelten Umfang und den Abholschlüssel, aber kein Ticket (das kommt aus
    /redeem, E10). Ein Antrag ohne `tab_host` (oder mit `mode: domains`) geht
    weiter den manuellen Weg über die Freigabeseite."""
    try:
        koerper = await _json(request)
        wer = await _identitaet(request)
        await _rufe("kontingent", {"aktion": "request", **wer})
        await _rufe("agb", {"aktion": "request", **wer})

        # `client` ist Pflicht (§2, §3.1). Früher sprang hier stillschweigend
        # "smartrchrome" ein — damit hätte ein Antrag ohne dieses Feld ein
        # Ticket für einen Client bekommen, den er nie genannt hat. Wer eine
        # Steuerbefugnis will, benennt sich; ein leeres Feld ist keine Aussage.
        roh_client = koerper.get("client")
        if not isinstance(roh_client, str) or not roh_client.strip():
            raise LinkFehler(400, "client_fehlt",
                             "Im Antrag fehlt die Angabe 'client'.")
        client = roh_client.strip().lower()
        if client != CLIENT:
            raise LinkFehler(403, "client_unbekannt",
                             f"Dieses Verfahren stellt nur Tickets für {CLIENT} aus.")
        ext_id = pruefe_extension(koerper.get("extension_id"))

        # E1: Der Wunsch heißt in beide Richtungen `requested` — ein Wort für
        # eine Sache. `erbeten` und `entwurf` gibt es auf der Leitung nicht.
        gewuenscht = koerper.get("requested")
        if not isinstance(gewuenscht, dict):
            gewuenscht = {}
        bereich = pruefe_bereich(gewuenscht.get("mode"), gewuenscht.get("allow"),
                                 gewuenscht.get("tab_host"))
        antrag = {
            "access": pruefe_stufe(gewuenscht.get("access", "read")),
            "duration": pruefe_dauer(gewuenscht.get("duration", STANDARD_DURATION)),
            "mode": bereich["mode"],
            "allow": bereich["allow"],
            "tab_host": bereich["tab_host"],
            "step_mode": str(gewuenscht.get("step_mode") or "confirm_each").strip().lower(),
        }
        # Der Schrittmodus des Antrags ist nur ein Vorschlag; gesperrt wird er
        # erst bei der Freigabe geprüft, sonst könnte ein Antrag mit "auto"
        # nicht einmal als "confirm_each" bestätigt werden.
        if antrag["step_mode"] not in SCHRITTMODI:
            antrag["step_mode"] = "confirm_each"
        # Geprüft wird, was gewährt WIRD — nicht, was im Feld `allow` steht.
        # Bei mode `tab` ist `allow` leer und der Tab-Host die ganze Freigabe;
        # wer hier nur `allow` prüfte, reichte die Sperrliste an der
        # Sitzungsfreigabe (E16) vorbei (Befund S1, 29.07.2026).
        await _rufe("bereich", {"aktion": "request", **wer}, gewaehrte_hosts(antrag))

        jetzt = time.time()
        with db() as conn:
            offen = conn.execute(
                "SELECT COUNT(*) FROM link_vorgaenge WHERE user_id=? "
                "AND zustand IN ('pending','approved') AND laeuft_ab_at > ?",
                (wer["user_id"], jetzt)).fetchone()[0]
            if offen >= MAX_OFFENE_ANTRAEGE:
                raise LinkFehler(429, "too_many_requests",
                                 f"Es sind bereits {MAX_OFFENE_ANTRAEGE} Freigaben offen.")
            im_fenster = conn.execute(
                "SELECT COUNT(*) FROM link_vorgaenge WHERE user_id=? AND erstellt_at > ?",
                (wer["user_id"], jetzt - ANTRAGSFENSTER)).fetchone()[0]
            if im_fenster >= MAX_ANTRAEGE_JE_FENSTER:
                raise LinkFehler(429, "too_many_requests",
                                 "Zu viele Freigabeversuche in kurzer Zeit.")

            rid = neue_kennung("lr")
            kennwort = neues_kennwort()
            salz = secrets.token_hex(16)
            abholschluessel = neuer_abholschluessel()

            # E15/E16 — Sitzungsfreigabe: Die gültige, VOLLSTÄNDIGE Anmeldung
            # bewilligt selbst (Entscheide des Inhabers, 28.07. und 29.07.2026).
            # Kein Kennwort, keine Freigabeseite. E16 erweitert E15 um die
            # Bedienstufe und die gewünschte Dauer: `write` wird bewilligt,
            # `duration` gilt bis MAX_DURATION, nur der Tab des Antrags.
            # `full` bleibt gesperrt (STUFEN kennt es nicht).
            #
            # Schrittmodus (Entscheid des Inhabers, 15.08.2026): Er folgt dem
            # Antrag, statt fest auf confirm_each zu stehen — sonst kann der
            # Schalter der Erweiterung nur einschränken, nie erweitern
            # (OFFEN-v3.5 §2.1). "auto" gibt es weiterhin nur, solange
            # LINK_AUTO_MODUS ihn freigibt; ist er zu, fällt der Antrag hier
            # bewusst auf confirm_each zurück, statt die ganze Sitzung mit 403
            # abzubrechen: eine engere Sitzung ist eine Antwort, ein toter
            # Verbindungsweg ist keine. Die Einzelbefehl-Freigabe der
            # Erweiterung bleibt als Schutztür davor unberührt.
            # Das Kennwort wird trotzdem erzeugt und gespeichert, aber nie
            # herausgegeben: so bleibt jede Confirm-Route wirkungslos, statt
            # auf leere Hashes zu treffen.
            sitzungsfreigabe = (antrag["access"] in STUFEN
                                and antrag["mode"] == "tab"
                                and bool(antrag["tab_host"]))
            if sitzungsfreigabe:
                schrittmodus = antrag["step_mode"]
                if schrittmodus == "auto" and not _auto_modus_frei():
                    schrittmodus = "confirm_each"
                gewaehrt = {"access": antrag["access"],
                            "duration": min(antrag["duration"], MAX_DURATION),
                            "mode": "tab",
                            "allow": [antrag["tab_host"]],
                            "step_mode": schrittmodus,
                            "idle_timeout": IDLE_TIMEOUT}
                ticket, jti, _ = baue_ticket(
                    {"rid": rid, "user_id": wer["user_id"],
                     "tenant": wer["tenant"], "ext_id": ext_id}, gewaehrt)
                conn.execute(
                    "INSERT INTO link_vorgaenge(rid,user_id,tenant,client,version,ext_id,"
                    "anlass,kennwort_salz,kennwort_hash,redeem_hash,antrag_json,"
                    "gewaehrt_json,ticket,ticket_jti,zustand,erstellt_at,laeuft_ab_at,"
                    "entschieden_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'approved', ?, ?, ?)",
                    (rid, wer["user_id"], wer["tenant"], client,
                     str(koerper.get("version") or "")[:32], ext_id,
                     str(koerper.get("purpose") or "")[:MAX_PURPOSE],
                     salz, kennwort_hash(kennwort, salz),
                     abholschluessel_abdruck(abholschluessel),
                     json.dumps(antrag, ensure_ascii=False),
                     json.dumps(gewaehrt, ensure_ascii=False), ticket, jti,
                     jetzt, jetzt + ANTRAG_TTL, jetzt))
            else:
                conn.execute(
                    "INSERT INTO link_vorgaenge(rid,user_id,tenant,client,version,ext_id,anlass,"
                    "kennwort_salz,kennwort_hash,redeem_hash,antrag_json,zustand,"
                    "erstellt_at,laeuft_ab_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'pending', ?, ?)",
                    (rid, wer["user_id"], wer["tenant"], client,
                     str(koerper.get("version") or "")[:32], ext_id,
                     str(koerper.get("purpose") or "")[:MAX_PURPOSE],
                     salz, kennwort_hash(kennwort, salz),
                     abholschluessel_abdruck(abholschluessel),
                     json.dumps(antrag, ensure_ascii=False), jetzt, jetzt + ANTRAG_TTL))

        if sitzungsfreigabe:
            protokoll("sitzungsfreigabe_erteilt", rid, wer["user_id"], wer["tenant"],
                      {"extension_id": ext_id, "jti": jti,
                       "granted": {k: gewaehrt[k] for k in ("access", "duration", "mode")}})
            # Das Ticket steht nicht in der Antwort (E10): es kommt aus
            # POST /redeem, gebunden an den Abholschlüssel der Erweiterung.
            return JSONResponse({
                "rid": rid,
                "state": "approved",
                "granted": gewaehrt,
                "redeem_key": abholschluessel,
                "expires_in": ANTRAG_TTL,
            }, status_code=201)

        protokoll("antrag_gestellt", rid, wer["user_id"], wer["tenant"],
                  {"extension_id": ext_id,
                   "requested": {k: antrag[k] for k in ("access", "duration", "mode")}})
        return JSONResponse({
            "rid": rid,
            "state": "pending",
            "verify_word": kennwort,
            "verify_word_spoken": kennwort_ansage(kennwort),
            "confirm_url": _confirm_url(rid),
            "redeem_key": abholschluessel,
            "expires_in": ANTRAG_TTL,
        }, status_code=201)
    except LinkFehler as f:
        return _abgelehnt(f, "antrag_abgelehnt", request)


@router.get("/request/{rid}")
async def link_vorgang(rid: str, request: Request):
    """§3.2 — Vorgang anzeigen. Verbraucht nichts, darf beliebig oft gerufen
    werden, liefert nie verify_word, nie redeem_key, nie ticket."""
    try:
        wer = await _identitaet(request)
        with db() as conn:
            vorgang = _lade(conn, rid)
        # Ein fremder Vorgang wird wie ein nicht vorhandener behandelt; sonst
        # verrät die Antwort, dass es ihn gibt.
        if not vorgang or vorgang["user_id"] != wer["user_id"]:
            raise LinkFehler(404, "antrag_unbekannt")

        zustand = _zustand_jetzt(vorgang)
        if zustand == "expired":
            raise LinkFehler(410, "antrag_abgelaufen")

        antrag = json.loads(vorgang["antrag_json"])
        return {
            "rid": rid,
            "state": zustand,
            "client": vorgang["client"],
            "version": vorgang["version"],
            "extension_id": vorgang["ext_id"],
            "purpose": vorgang["anlass"],
            # `requested` ist ein Zitat des Antragstellers, keine Vorauswahl.
            "requested": antrag,
            # `preselect` ist das, was die Freigabeseite vorbelegen MUSS (E8).
            "preselect": vorbelegung(),
            "limits": grenzen(),
            "verify_word_len": KENNWORT_LEN,
            "attempts_left": max(0, MAX_KENNWORT_FEHLER - vorgang["kennwort_fehler"]),
            # E9 gilt nur noch für den manuellen Pfad: Solange der Vorgang auf
            # eine menschliche Freigabe wartet, ist der zweite Nachweis Pflicht.
            # Eine Sitzungsfreigabe (E15) ist bereits entschieden — die Seite
            # darf dann kein Passwortfeld mehr anbieten.
            "reauth_required": zustand == "pending",
            "remaining": _restzeit(vorgang),
        }
    except LinkFehler as f:
        return f.antwort()


@router.post("/confirm")
async def link_confirm(request: Request):
    """§3.3 — Freigabe oder Ablehnung durch den angemeldeten Menschen.

    Prüfreihenfolge: Identität und Eigentum stehen VOR der Herkunft.

    Das Drahtformat listet die Herkunft an erster Stelle. Das ist an dieser
    Stelle falsch und war ein Loch: Ein Fehlschlag der Herkunftsprüfung
    verbrennt den Vorgang (§7.1). Stand sie vorn, konnte ein Unangemeldeter mit
    einer beliebigen fremden `rid` und einem falschen Ursprung eine fremde
    Freigabe abschießen — eine Fernbedienung zum Sperren fremder Vorgänge, ohne
    einen einzigen gültigen Ausweis. Deshalb: erst feststellen, WESSEN Vorgang
    das ist; ein Fehlschlag davor verändert nichts.

    Die Herkunftsbindung verliert dadurch nichts: Ihr Zweck ist, die
    Erweiterung daran zu hindern, sich selbst freizugeben — und die hat einen
    gültigen Ausweis für genau diesen Vorgang, kommt also ohnehin bis hierher.
    /redeem prüft aus demselben Grund seit jeher zuerst die Identität.
    """
    try:
        koerper = await _json(request)
        rid = str(koerper.get("rid") or "").strip()

        # 1. Identität. Ohne gültigen Ausweis wird gar nichts angefasst.
        wer = await _identitaet(request)

        # 2. Eigentum am Vorgang. Ein fremder Vorgang wird wie ein nicht
        #    vorhandener behandelt — die Antwort darf nicht verraten, dass es
        #    ihn gibt, und ab hier kann kein Schritt mehr einen fremden Vorgang
        #    verändern.
        with db() as conn:
            vorgang = _lade(conn, rid)
        if not vorgang or vorgang["user_id"] != wer["user_id"]:
            raise LinkFehler(404, "antrag_unbekannt")

        # 3. Herkunft (§7.1). Ein Aufruf aus der Erweiterung verbrennt den
        #    Vorgang sofort — jetzt aber nachweislich den eigenen.
        _pruefe_herkunft_confirm(request, koerper, vorgang)

        # 4. Zustand.
        zustand = _zustand_jetzt(vorgang)
        if zustand == "expired":
            raise LinkFehler(410, "antrag_abgelaufen")
        if zustand != "pending":
            raise LinkFehler(409, "antrag_bereits_entschieden",
                             f"Dieser Vorgang steht auf '{zustand}'.")

        # 5. Ablehnen geht immer und ohne Kennwort. Wer abbrechen will, soll
        #    nicht erst etwas abtippen müssen.
        if koerper.get("confirm") is not True:
            _entscheide(rid, "denied", "nutzer_abgelehnt")
            protokoll("abgelehnt_nutzer", rid, wer["user_id"], vorgang["tenant"],
                      {"reason": str(koerper.get("reason") or "")[:64]})
            return {"rid": rid, "state": "denied", "reason": "nutzer_abgelehnt"}

        # 6. Erratensperre über alle Vorgänge des Nutzers.
        _pruefe_erratensperre(wer["user_id"])

        # 7. verify_word.
        eingabe = kennwort_normalisieren(koerper.get("verify_word"))
        if not hmac.compare_digest(
                kennwort_hash(eingabe, vorgang["kennwort_salz"]), vorgang["kennwort_hash"]):
            offen = _kennwortfehler(rid, wer["user_id"])
            protokoll("kennwort_falsch", rid, wer["user_id"], vorgang["tenant"],
                      {"attempts_left": offen})
            if offen <= 0:
                raise LinkFehler(403, "kennwort_falsch",
                                 "Zu viele Fehlversuche — der Vorgang wurde abgebrochen. "
                                 "Bitte in der Erweiterung neu beginnen.")
            raise LinkFehler(403, "kennwort_falsch",
                             f"Das Kennwort stimmt nicht. Noch {offen} Versuche.")

        # 8. Fehlende Felder aus `preselect` ergänzen — NIEMALS aus `requested`
        #    (E8). Genau hier ließ die alte Fassung den Antragsteller über seine
        #    eigene Befugnis entscheiden, sobald ein Feld unterwegs verlorenging.
        antrag = json.loads(vorgang["antrag_json"])
        vorgabe = vorbelegung()
        access = pruefe_stufe(koerper.get("access", vorgabe["access"]))
        duration = pruefe_dauer(koerper.get("duration", vorgabe["duration"]))
        step_mode = pruefe_schrittmodus(koerper.get("step_mode", vorgabe["step_mode"]))
        bereich = pruefe_bereich(koerper.get("mode", vorgabe["mode"]),
                                 koerper.get("allow", vorgabe["allow"]),
                                 antrag.get("tab_host"))

        # Bei den Adressen ist der Spielraum enger als bei Stufe und Dauer:
        # erlaubt sind die beantragten Adressen und die Domain des Tabs, auf
        # dem der Antrag entstand ("nur dieser Tab" darf zu "ganz example.de"
        # werden). Eine hier frei erfundene fremde Adresse wäre eine Freigabe
        # für eine Seite, die der Mensch in diesem Moment gar nicht vor sich hat.
        beantragt = set(antrag.get("allow") or [])
        tab_host = antrag.get("tab_host") or ""
        if tab_host:
            beantragt |= {tab_host, "*." + tab_host}
        zusatz = [d for d in bereich["allow"] if d not in beantragt]
        if zusatz:
            raise LinkFehler(403, "bereich_erweitert",
                             "Der Geltungsbereich kann hier nur aus dem Antrag stammen. "
                             f"Nicht beantragt: {', '.join(zusatz)}.")

        # 9. reauth — bei JEDER Freigabe (E9). Das verify_word zeigt die
        #    Erweiterung selbst an; das Kontopasswort ist das einzige Stück,
        #    das sie nicht hat. Ohne eingehängten Prüfer gibt es keine Freigabe.
        nachweis = koerper.get("reauth")
        if not isinstance(nachweis, dict):
            nachweis = {}
        erlaubt = await _rufe("reauth", {"aktion": "confirm", **wer, "rid": rid},
                              str(nachweis.get("method") or ""), nachweis.get("assertion"))
        if erlaubt is not True:
            protokoll("reauth_fehlt", rid, wer["user_id"], vorgang["tenant"],
                      {"method": str(nachweis.get("method") or "")[:32]})
            raise LinkFehler(403, "reauth_erforderlich",
                             "Bitte bestätige die Freigabe noch einmal mit deinem Kontopasswort.")

        # 10. Bei mode 'tab' ist die Adressliste genau der tab_host (E7). Ein
        #    leeres allow wäre im Relay keine Beschränkung, sondern deren
        #    Aufhebung — "nur dieser Tab" wäre damit die weiteste Freigabe.
        allow = gewaehrte_hosts(bereich)
        if not allow:
            raise LinkFehler(400, "bereich_leer", "Ohne Adresse gibt es keine Sitzung.")
        # Erst jetzt die Tenant-Sperrliste, und zwar mit genau diesen Adressen:
        # Wählt der Mensch 'nur dieser Tab', ist der Tab-Host die Freigabe. Die
        # frühere Fassung übergab `bereich["allow"]` — bei mode `tab` leer
        # (Befund S1, 29.07.2026).
        await _rufe("bereich", {"aktion": "confirm", **wer}, allow)

        gewaehrt = {"access": access, "duration": duration, "mode": bereich["mode"],
                    "allow": allow, "step_mode": step_mode, "idle_timeout": IDLE_TIMEOUT}

        # 11. Ticket bauen und den Vorgang atomar von 'pending' auf 'approved'
        #     schreiben: zwei gleichzeitige Freigaben desselben Vorgangs dürfen
        #     nicht zwei Tickets erzeugen.
        ticket, jti, _ = baue_ticket(vorgang, gewaehrt)
        with db() as conn:
            geaendert = conn.execute(
                "UPDATE link_vorgaenge SET zustand='approved', gewaehrt_json=?, ticket=?, "
                "ticket_jti=?, entschieden_at=? WHERE rid=? AND zustand='pending'",
                (json.dumps(gewaehrt, ensure_ascii=False), ticket, jti, time.time(), rid)).rowcount
        if not geaendert:
            raise LinkFehler(409, "antrag_bereits_entschieden")

        protokoll("freigegeben", rid, wer["user_id"], vorgang["tenant"],
                  {"jti": jti, "access": access, "duration": duration,
                   "mode": gewaehrt["mode"], "allow": allow, "step_mode": step_mode})
        # Das Ticket steht bewusst NICHT in dieser Antwort: im Tab könnte jedes
        # Skript des Ursprungs mitlesen. Es kommt aus POST /redeem (E10).
        return {"rid": rid, "state": "approved", "granted": gewaehrt,
                "ticket_expires_in": TICKET_TTL,
                "hinweis": "Die Erweiterung holt den Schein jetzt ab. "
                           "Du kannst dieses Fenster schließen."}
    except LinkFehler as f:
        return _abgelehnt(f, "freigabe_abgelehnt", request)


@router.post("/redeem")
async def link_redeem(request: Request):
    """§3.4 — Ticket abholen, genau einmal, nur von der antragstellenden
    Erweiterung. Danach ist es im Gateway gelöscht."""
    try:
        koerper = await _json(request)
        wer = await _identitaet(request)
        rid = str(koerper.get("rid") or "").strip()

        with db() as conn:
            vorgang = _lade(conn, rid)
        if not vorgang or vorgang["user_id"] != wer["user_id"]:
            raise LinkFehler(404, "antrag_unbekannt")

        # Die Herkunftsbindung steht vor der Zustandsauskunft: auch ein
        # "noch offen" ist eine Auskunft, und sie gehört nur der
        # antragstellenden Erweiterung.
        _pruefe_herkunft_redeem(request, koerper, vorgang)

        zustand = _zustand_jetzt(vorgang)
        if zustand == "pending":
            return {"rid": rid, "state": "pending", "remaining": _restzeit(vorgang)}
        if zustand == "denied":
            return {"rid": rid, "state": "denied", "reason": vorgang["grund"] or "denied"}
        if zustand == "consumed":
            raise LinkFehler(410, "ticket_bereits_abgeholt",
                             "Dieser Schein wurde bereits abgeholt.")
        if zustand == "expired":
            raise LinkFehler(410, "antrag_abgelaufen")

        ticket = vorgang["ticket"]
        # Das Verbrennen und das Ausliefern müssen dieselbe Schreiboperation
        # sein: nur wer die Zeile von 'approved' wegbewegt, bekommt das Ticket.
        with db() as conn:
            geaendert = conn.execute(
                "UPDATE link_vorgaenge SET zustand='consumed', ticket=NULL, entschieden_at=? "
                "WHERE rid=? AND zustand='approved'", (time.time(), rid)).rowcount
        if not geaendert or not ticket:
            raise LinkFehler(410, "ticket_bereits_abgeholt")

        gewaehrt = json.loads(vorgang["gewaehrt_json"])
        protokoll("ticket_abgeholt", rid, wer["user_id"], vorgang["tenant"],
                  {"jti": vorgang["ticket_jti"]})
        # `granted` dient hier ausschließlich der Anzeige. Was die Erweiterung
        # tun darf, steht in auth_ok — nirgends sonst (§5.3).
        return {"rid": rid, "state": "approved", "ticket": ticket,
                "ticket_expires_in": TICKET_TTL, "granted": gewaehrt}
    except LinkFehler as f:
        return f.antwort()


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------

async def _json(request: Request) -> dict:
    try:
        koerper = await request.json()
    except Exception:
        raise LinkFehler(400, "invalid_json")
    if not isinstance(koerper, dict):
        raise LinkFehler(400, "invalid_json")
    return koerper


def _abgelehnt(fehler: LinkFehler, ereignis: str, request: Request) -> JSONResponse:
    if fehler.status != 401:
        # 401 wird nicht protokolliert: dort ist kein Nutzer bekannt, und der
        # Eintrag wäre nur eine Einladung, das Protokoll vollzuschreiben.
        protokoll(ereignis, detail={"code": fehler.code,
                                    "pfad": request.url.path})
    return fehler.antwort()


def _entscheide(rid: str, zustand: str, grund: str):
    with db() as conn:
        conn.execute("UPDATE link_vorgaenge SET zustand=?, grund=?, ticket=NULL, "
                     "entschieden_at=? WHERE rid=?", (zustand, grund, time.time(), rid))


def _pruefe_erratensperre(user_id: str):
    grenze = time.time() - KENNWORTFENSTER
    with db() as conn:
        conn.execute("DELETE FROM link_kennwort_fehler WHERE ts < ?", (grenze,))
        anzahl = conn.execute(
            "SELECT COUNT(*) FROM link_kennwort_fehler WHERE user_id=? AND ts > ?",
            (user_id, grenze)).fetchone()[0]
    if anzahl >= MAX_KENNWORT_FEHLER_NUTZER:
        raise LinkFehler(429, "too_many_requests",
                         "Zu viele Fehlversuche. Bitte in einigen Minuten erneut versuchen.")


def _kennwortfehler(rid: str, user_id: str) -> int:
    """Zählt hoch und gibt die verbleibenden Versuche zurück. Bei 0 ist der
    Vorgang verbrannt — ein Kennwort wird nicht zweimal geraten."""
    with db() as conn:
        conn.execute("INSERT INTO link_kennwort_fehler(ts, user_id) VALUES (?,?)",
                     (time.time(), user_id))
        conn.execute("UPDATE link_vorgaenge SET kennwort_fehler = kennwort_fehler + 1 "
                     "WHERE rid=?", (rid,))
        stand = conn.execute("SELECT kennwort_fehler FROM link_vorgaenge WHERE rid=?",
                             (rid,)).fetchone()[0]
    offen = max(0, MAX_KENNWORT_FEHLER - stand)
    if offen <= 0:
        _entscheide(rid, "denied", "kennwort_falsch")
    return offen


def _abholfehler(rid: str) -> bool:
    """Zählt einen Fehlversuch beim Abholen. Gibt zurück, ob der Vorgang damit
    verbrannt ist. Eine verlorene Freigabe ist der kleinere Schaden als ein
    geratenes Ticket (§7.2)."""
    with db() as conn:
        conn.execute("UPDATE link_vorgaenge SET redeem_fehler = redeem_fehler + 1 "
                     "WHERE rid=?", (rid,))
        stand = conn.execute("SELECT redeem_fehler FROM link_vorgaenge WHERE rid=?",
                             (rid,)).fetchone()[0]
    if stand >= MAX_ABHOL_FEHLER:
        _entscheide(rid, "denied", "herkunft_ungueltig")
        return True
    return False


def aufraeumen(alter_sekunden: int = 86400) -> int:
    """Löscht abgelaufene Vorgänge. Kann aus einem Cron laufen, muss aber nicht:
    die Gültigkeit hängt an laeuft_ab_at, nicht an diesem Aufruf."""
    grenze = time.time() - max(ANTRAG_TTL, alter_sekunden)
    with db() as conn:
        return conn.execute("DELETE FROM link_vorgaenge WHERE erstellt_at < ?",
                            (grenze,)).rowcount

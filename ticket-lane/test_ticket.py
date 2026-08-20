"""Tests der SMarTrLink-Ticketausgabe — laufen ohne Gateway und ohne Relay.

Ausführen:  python3 -m pytest -q test_ticket.py

Maßgeblich ist Docs/SMarTrChrome/DRAHTFORMAT.md. Die Abschnitte am Ende sind
die wörtliche Übersetzung der Prüfsätze aus §12, soweit sie diese Seite
betreffen (9–18); davor steht, was im Bestand nachweislich schon einmal
gefehlt hat: dass Stufe und Dauer Server-Angaben sind, dass ein Ticket genau
einmal wirkt, dass 0 Sekunden Dauer ein Fehler ist und dass ein Ticket sich
nicht als Anmeldenachweis ausgeben kann.
"""

import importlib
import time

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

GEHEIM = "testgeheimnis-nur-fuer-die-testsuite"
EXT_ID = "aiblkfhgnpqrstuvwxyzabcdefghknpe"   # 32 Zeichen wie eine Chrome-Kennung

WEB_URSPRUNG = "https://cloud.smartragents.ai"
EXT_URSPRUNG = f"chrome-extension://{EXT_ID}"
KONTOPASSWORT = "das-kontopasswort-des-menschen"


@pytest.fixture()
def modul(tmp_path, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", GEHEIM)
    monkeypatch.setenv("LINK_DB", str(tmp_path / "link.db"))
    monkeypatch.setenv("LINK_EXT_IDS", EXT_ID)
    monkeypatch.setenv("LINK_CONFIRM_URL", WEB_URSPRUNG + "/link/confirm")
    monkeypatch.setenv("LINK_CONFIRM_ORIGIN", WEB_URSPRUNG)
    monkeypatch.setenv("LINK_AUTO_MODUS", "0")
    import ticket
    importlib.reload(ticket)
    ticket.init_db()
    return ticket


@pytest.fixture()
def klient(modul):
    app = FastAPI()
    app.include_router(modul.router)
    return TestClient(app)


@pytest.fixture()
def reauth(modul):
    """Der Kontopasswort-Prüfer, wie ihn das Gateway einhängt.

    Ohne ihn wird nach E9 überhaupt nichts erteilt — deshalb hängt jeder Test,
    der eine erfolgreiche Freigabe braucht, ausdrücklich an dieser Vorrichtung.
    """
    modul.setze_reauth_pruefer(
        lambda kontext, method, assertion: method == "password" and assertion == KONTOPASSWORT)
    return modul


def alltags_jwt(sub="42", tenant="t-eins", ttl=3600, **extra):
    jetzt = int(time.time())
    daten = {"sub": sub, "email": f"{sub}@smartragents.ai", "tenant": tenant,
             "iat": jetzt, "exp": jetzt + ttl}
    daten.update(extra)
    return jwt.encode(daten, GEHEIM, algorithm="HS256")


def kopf(token=None):
    return {"Authorization": f"Bearer {token or alltags_jwt()}"}


def antrag(klient, token=None, **abweichung):
    """Ein Antrag, der den MANUELLEN Weg betritt.

    Seit E15 bewilligt die Lesestufe sofort (Sitzungsfreigabe), seit E16 auch
    die Bedienstufe — den Kennwort-/Freigabeseiten-Weg gibt es nur noch für
    Anträge, die über den einen Tab hinauswollen (mode `domains`) oder keinen
    `tab_host` nennen. Deshalb beantragt dieser Helfer `domains`; die Freigabe
    selbst darf trotzdem enger entscheiden (E8: entschieden wird aus
    preselect)."""
    koerper = {"client": "smartrchrome", "version": "1.0.0", "extension_id": EXT_ID,
               "purpose": "Preise auf drei Shopseiten vergleichen",
               "requested": {"access": "write", "duration": 600, "mode": "domains",
                             "allow": ["geizhals.de"], "tab_host": "geizhals.de",
                             "step_mode": "confirm_each"}}
    koerper.update(abweichung)
    return klient.post("/api/v1/link/request", json=koerper, headers=kopf(token))


def leseantrag(klient, token=None, **abweichung):
    """Ein Antrag auf der Lesestufe — bekommt die Sitzungsfreigabe (E15)."""
    koerper = {"client": "smartrchrome", "version": "1.0.0", "extension_id": EXT_ID,
               "purpose": "Preise auf drei Shopseiten vergleichen",
               "requested": {"access": "read", "duration": 600, "mode": "tab",
                             "allow": [], "tab_host": "geizhals.de",
                             "step_mode": "confirm_each"}}
    koerper.update(abweichung)
    return klient.post("/api/v1/link/request", json=koerper, headers=kopf(token))


def freigabe(klient, rid, verify_word=None, token=None, ursprung=WEB_URSPRUNG,
             sec_site="same-origin", sec_mode="cors", rumpf_ursprung=..., **felder):
    """POST /confirm so, wie die Freigabeseite es sendet (§3.3, §7.1).

    `ursprung=None` lässt die Kopfzeile weg, `rumpf_ursprung=None` das Rumpffeld.
    """
    kopfzeilen = kopf(token)
    if ursprung is not None:
        kopfzeilen["Origin"] = ursprung
    if sec_site is not None:
        kopfzeilen["Sec-Fetch-Site"] = sec_site
    if sec_mode is not None:
        kopfzeilen["Sec-Fetch-Mode"] = sec_mode

    koerper = {"rid": rid, "confirm": True,
               "reauth": {"method": "password", "assertion": KONTOPASSWORT}}
    if rumpf_ursprung is ...:
        rumpf_ursprung = ursprung
    if rumpf_ursprung is not None:
        koerper["origin"] = rumpf_ursprung
    if verify_word is not None:
        koerper["verify_word"] = verify_word
    koerper.update(felder)
    return klient.post("/api/v1/link/confirm", json=koerper, headers=kopfzeilen)


def abholung(klient, rid, redeem_key=..., token=None, ursprung=EXT_URSPRUNG):
    """POST /redeem so, wie die Erweiterung es sendet (§3.4, §7.2)."""
    kopfzeilen = kopf(token)
    if ursprung is not None:
        kopfzeilen["Origin"] = ursprung
    koerper = {"rid": rid}
    if redeem_key is not ... and redeem_key is not None:
        koerper["redeem_key"] = redeem_key
    return klient.post("/api/v1/link/redeem", json=koerper, headers=kopfzeilen)


def voller_ablauf(klient, **felder):
    """Antrag → Freigabe → Abholung. Gibt (daten, ticket-anspruch, granted) zurück."""
    daten = antrag(klient).json()
    erteilt = freigabe(klient, daten["rid"], daten["verify_word"], **felder)
    assert erteilt.status_code == 200, erteilt.text
    geholt = abholung(klient, daten["rid"], daten["redeem_key"]).json()
    anspruch = jwt.decode(geholt["ticket"], GEHEIM, algorithms=["HS256"],
                          audience="smartr-connect")
    return daten, anspruch, geholt["granted"]


# ---------------------------------------------------------------------------
# Kennwort (auf der Leitung: verify_word)
# ---------------------------------------------------------------------------

def test_kennwort_alphabet_ohne_verwechselbare_zeichen(modul):
    assert modul.KENNWORT_LEN == 6
    for verboten in "ILO01":
        assert verboten not in modul.KENNWORT_ALPHABET
    for _ in range(200):
        kennwort = modul.neues_kennwort()
        assert len(kennwort) == 6
        assert set(kennwort) <= set(modul.KENNWORT_ALPHABET)


def test_kennwort_ansage_nach_funkalphabet(modul):
    assert modul.kennwort_ansage("K7M2QX") == \
        "K wie Kaufmann, sieben, M wie Martha, zwei, Q wie Quelle, X wie Xanthippe"
    for zeichen in modul.KENNWORT_ALPHABET:
        assert zeichen in modul.FUNKALPHABET


def test_kennwort_normalisierung_verzeiht_schreibweise(modul):
    assert modul.kennwort_normalisieren(" k7 m2-qx ") == "K7M2QX"


# ---------------------------------------------------------------------------
# Abholschlüssel (redeem_key)
# ---------------------------------------------------------------------------

def test_abholschluessel_hat_256_bit_und_43_zeichen(modul):
    gesehen = set()
    for _ in range(200):
        schluessel = modul.neuer_abholschluessel()
        assert len(schluessel) == 43
        assert "=" not in schluessel
        gesehen.add(schluessel)
    assert len(gesehen) == 200


def test_abholschluessel_wird_nur_als_abdruck_gespeichert(klient, modul):
    daten = antrag(klient).json()
    with modul.db() as conn:
        vorgang = modul._lade(conn, daten["rid"])
    assert daten["redeem_key"] not in str(vorgang)
    assert vorgang["redeem_hash"] == modul.abholschluessel_abdruck(daten["redeem_key"])


# ---------------------------------------------------------------------------
# Bereichsprüfung — flach: mode / allow / tab_host (E6)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("eingabe", ["*", "*.de", "*.com", "*.co.uk", "de", "localhost",
                                     "https://geizhals.de/", "geizhals.de/pfad",
                                     "geizhals.de:8080", "a@geizhals.de", ""])
def test_bereich_weist_zu_weite_eintraege_ab(modul, eingabe):
    with pytest.raises(modul.LinkFehler) as fehler:
        modul.domain_normalisieren(eingabe)
    assert fehler.value.status == 400


def test_bereich_akzeptiert_domain_und_platzhalter(modul):
    assert modul.domain_normalisieren("Geizhals.de")["ascii"] == "geizhals.de"
    assert modul.domain_normalisieren("*.geizhals.de")["ascii"] == "*.geizhals.de"


def test_bereich_loest_homographen_in_punycode_auf(modul):
    # kyrillisches 'а' am Anfang — die Anzeige unterscheidet sich sichtbar
    geprueft = modul.domain_normalisieren("аmazon.de")
    assert geprueft["ascii"].startswith("xn--")
    assert geprueft["punycode"] is True
    assert geprueft["ascii"] != "amazon.de"


def test_bereich_deckelt_anzahl(modul):
    viele = [f"seite{i}.de" for i in range(modul.MAX_ALLOW + 1)]
    with pytest.raises(modul.LinkFehler) as fehler:
        modul.pruefe_bereich("domains", viele, None)
    assert fehler.value.code == "bereich_zu_gross"


def test_bereich_liefert_flache_felder_ohne_domains_objekt(modul):
    erg = modul.pruefe_bereich("domains", ["Geizhals.de", "geizhals.de"], "geizhals.de")
    assert erg == {"mode": "domains", "allow": ["geizhals.de"], "tab_host": "geizhals.de"}
    assert "domains" not in erg and "scope" not in erg


def test_bereich_tab_ohne_tab_host_ist_ein_fehler(modul):
    # E7: ohne tab_host wäre 'nur dieser Tab' ein leeres allow — und das ist im
    # Relay keine Beschränkung, sondern deren Aufhebung.
    with pytest.raises(modul.LinkFehler) as fehler:
        modul.pruefe_bereich("tab", [], None)
    assert fehler.value.status == 400
    assert fehler.value.code == "tab_host_fehlt"


def test_bereich_domains_ohne_adressen_ist_ein_fehler(modul):
    with pytest.raises(modul.LinkFehler) as fehler:
        modul.pruefe_bereich("domains", [], "geizhals.de")
    assert fehler.value.code == "bereich_leer"


# ---------------------------------------------------------------------------
# Deckel für Stufe, Dauer, Modus
# ---------------------------------------------------------------------------

def test_stufe_full_ist_gesperrt(klient):
    antwort = antrag(klient, requested={"access": "full", "duration": 600,
                                        "mode": "tab", "tab_host": "geizhals.de"})
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "stufe_fuer_client_gesperrt"


def test_dauer_null_ist_ein_fehler(klient):
    antwort = antrag(klient, requested={"access": "read", "duration": 0,
                                        "mode": "tab", "tab_host": "geizhals.de"})
    assert antwort.status_code == 400
    assert antwort.json()["error"] == "dauer_ausserhalb_deckel"


def test_dauer_deckel_liegt_bei_3600(klient, modul):
    assert modul.MAX_DURATION == 3600
    basis = {"access": "read", "mode": "tab", "tab_host": "geizhals.de"}
    assert antrag(klient, requested={**basis, "duration": 3600}).status_code == 201
    assert antrag(klient, requested={**basis, "duration": 3601}).status_code == 400


def test_fremder_client_bekommt_kein_ticket(klient):
    antwort = antrag(klient, client="smartrbrowser")
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "client_unbekannt"


def test_unbekannte_erweiterung_wird_abgewiesen(klient):
    antwort = antrag(klient, extension_id="fremdefremdefremdefremdefremdefr")
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "extension_unknown"


def test_ohne_zulassungsliste_gibt_es_kein_ticket(klient, monkeypatch):
    monkeypatch.setenv("LINK_EXT_IDS", "")
    assert antrag(klient).status_code == 403


def test_ext_id_im_rumpf_gibt_es_nicht_mehr(klient):
    # Im HTTP-Rumpf heißt das Feld extension_id; 'ext_id' ist gestrichen (§2).
    antwort = antrag(klient, extension_id=None, ext_id=EXT_ID)
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "extension_unknown"


def test_platzhalter_in_der_zulassungsliste_erlaubt_gar_nichts(klient, monkeypatch):
    """§3.1 kennt nur: Kennung steht in der Liste, sonst 403.

    'LINK_EXT_IDS=*' war ein Fail-open-Schalter — ein Zeichen in einer
    Umgebungsvariable hob die ganze Zulassungsprüfung auf. Den Platzhalter gibt
    es nicht mehr, und eine Liste, die nur aus ihm besteht, ist eine leere Liste.
    """
    monkeypatch.setenv("LINK_EXT_IDS", "*")
    antwort = antrag(klient)
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "extension_unknown"
    # Und der Stern taugt auch nicht als Kennung, die sich selbst in der Liste findet.
    assert antrag(klient, extension_id="*").status_code == 403


def test_platzhalter_neben_echter_kennung_bleibt_wirkungslos(klient, monkeypatch):
    monkeypatch.setenv("LINK_EXT_IDS", f"*,{EXT_ID}")
    # Die echte Kennung wird weiter zugelassen …
    assert antrag(klient).status_code == 201
    # … der Stern öffnet daneben nichts.
    assert antrag(klient, extension_id="*").status_code == 403
    assert antrag(klient, extension_id="fremdefremdefremdefremdefremdefr").status_code == 403


def test_kennung_muss_wie_eine_erweiterungskennung_aussehen(klient, monkeypatch):
    # Positivliste: 32 Kleinbuchstaben. Was das nicht ist, kommt weder in die
    # Liste noch durch die Prüfung — sonst bindet §7.2 an eine Phantasiekennung.
    monkeypatch.setenv("LINK_EXT_IDS", "chrome-extension://*")
    assert antrag(klient, extension_id="chrome-extension://*").status_code == 403


def test_antrag_ohne_client_wird_nicht_stillschweigend_ergaenzt(klient):
    """`client` ist Pflichtfeld (§2). Früher sprang 'smartrchrome' ein."""
    antwort = antrag(klient, client=None)
    assert antwort.status_code == 400
    assert antwort.json()["error"] == "client_fehlt"


def test_antrag_mit_leerem_client_wird_abgewiesen(klient):
    for wert in ("", "   ", 7, [], {}):
        antwort = antrag(klient, client=wert)
        assert antwort.status_code == 400, wert
        assert antwort.json()["error"] == "client_fehlt"


# ---------------------------------------------------------------------------
# Identität
# ---------------------------------------------------------------------------

def test_ohne_token_kein_antrag(klient):
    assert klient.post("/api/v1/link/request", json={}).status_code == 401


def test_abgelaufenes_token_wird_abgewiesen(klient):
    antwort = antrag(klient, token=alltags_jwt(ttl=-10))
    assert antwort.status_code == 401


def test_ticket_taugt_nicht_als_anmeldenachweis(klient, modul):
    # Ein abgefangenes Ticket darf keine neuen Tickets bestellen können.
    ticket_jwt, _, _ = modul.baue_ticket(
        {"rid": "lr_x", "user_id": "42", "tenant": "t-eins", "ext_id": EXT_ID},
        {"access": "read", "duration": 600, "step_mode": "confirm_each",
         "idle_timeout": 600, "mode": "tab", "allow": ["geizhals.de"]})
    assert antrag(klient, token=ticket_jwt).status_code == 401


def test_fremder_vorgang_bleibt_unsichtbar(klient):
    rid = antrag(klient).json()["rid"]
    antwort = klient.get(f"/api/v1/link/request/{rid}", headers=kopf(alltags_jwt(sub="99")))
    assert antwort.status_code == 404
    assert antwort.json()["error"] == "antrag_unbekannt"


# ---------------------------------------------------------------------------
# Vollständiger Ablauf
# ---------------------------------------------------------------------------

def test_antrag_liefert_die_felder_aus_dem_drahtformat(klient, modul):
    antwort = antrag(klient)
    assert antwort.status_code == 201
    daten = antwort.json()
    assert set(daten) == {"rid", "state", "verify_word", "verify_word_spoken",
                          "confirm_url", "redeem_key", "expires_in"}
    assert daten["state"] == "pending"
    assert daten["rid"].startswith("lr_")
    assert set(daten["verify_word"]) <= set(modul.KENNWORT_ALPHABET)
    assert daten["expires_in"] == modul.ANTRAG_TTL == 120
    assert daten["rid"] in daten["confirm_url"]
    assert daten["confirm_url"].startswith(WEB_URSPRUNG)
    assert "wie" in daten["verify_word_spoken"]
    # Ausgemusterte Namen dürfen auch nicht zusätzlich mitlaufen (§2).
    for verboten in ("kennwort", "ansage", "ticket", "erbeten", "entwurf"):
        assert verboten not in daten


def test_vorgangsansicht_zeigt_requested_preselect_und_limits(klient):
    daten = antrag(klient).json()
    sicht = klient.get(f"/api/v1/link/request/{daten['rid']}", headers=kopf()).json()
    assert sicht["state"] == "pending"
    assert sicht["extension_id"] == EXT_ID
    assert sicht["purpose"] == "Preise auf drei Shopseiten vergleichen"
    assert sicht["requested"] == {"access": "write", "duration": 600, "mode": "domains",
                                  "allow": ["geizhals.de"], "tab_host": "geizhals.de",
                                  "step_mode": "confirm_each"}
    assert sicht["limits"] == {"access": ["read", "write"], "min_duration": 1,
                               "max_duration": 3600, "max_allow": 10,
                               "auto_enabled": False, "idle_timeout": 600}
    assert sicht["verify_word_len"] == 6
    assert sicht["attempts_left"] == 3
    assert sicht["reauth_required"] is True
    assert 0 < sicht["remaining"] <= 120


def test_vorgangsansicht_verraet_weder_kennwort_noch_schluessel_noch_ticket(klient):
    daten = antrag(klient).json()
    sicht = klient.get(f"/api/v1/link/request/{daten['rid']}", headers=kopf()).json()
    text = str(sicht)
    assert daten["verify_word"] not in text
    assert daten["redeem_key"] not in text
    assert "ticket" not in sicht
    assert "verify_word" not in sicht
    assert "redeem_key" not in sicht


def test_freigabe_erzeugt_ticket_mit_serverangaben(klient, reauth):
    daten, anspruch, gewaehrt = voller_ablauf(
        klient, access="read", duration=900, mode="domains", allow=["geizhals.de"],
        step_mode="confirm_each")

    assert anspruch["iss"] == "smartr-gateway"
    assert anspruch["aud"] == "smartr-connect"
    # E4: der Anspruch benennt, WAS das Token ist.
    assert anspruch["scope"] == "smartrlink-ticket"
    assert anspruch["sub"] == "42"
    assert anspruch["jti"].startswith("tk_")
    assert 0 < anspruch["exp"] - anspruch["iat"] <= 60
    # Stufe und Dauer stehen signiert im Ticket — kein Client behauptet sie mehr.
    assert anspruch["access"] == "read"
    assert anspruch["duration"] == 900
    assert anspruch["idle_timeout"] == 600
    assert anspruch["step_mode"] == "confirm_each"
    assert anspruch["mode"] == "domains"
    assert anspruch["allow"] == ["geizhals.de"]
    assert anspruch["client"] == "smartrchrome"
    assert anspruch["ext"] == EXT_ID
    assert anspruch["tnt"] == "t-eins"
    assert anspruch["rid"] == daten["rid"]
    # Was die Erweiterung angezeigt bekommt, ist dasselbe wie im Schein.
    assert gewaehrt == {"access": "read", "duration": 900, "mode": "domains",
                        "allow": ["geizhals.de"], "step_mode": "confirm_each",
                        "idle_timeout": 600}


def test_ticket_kennt_keine_kurznamen_mehr(klient, reauth):
    _, anspruch, _ = voller_ablauf(klient)
    for kurz in ("acc", "dur", "idl", "stp", "scp", "cl", "domains", "sites"):
        assert kurz not in anspruch
    pflicht = ("sub", "aud", "scope", "jti", "exp", "client", "access", "duration",
               "idle_timeout", "mode", "allow", "step_mode")
    for name in pflicht:
        assert name in anspruch, name


def test_freigabe_antwort_traegt_kein_ticket(klient, reauth):
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"])
    assert antwort.status_code == 200
    inhalt = antwort.json()
    assert inhalt["state"] == "approved"
    assert inhalt["ticket_expires_in"] == 60
    # Das Ticket darf nie in den Tab gelangen — dort läse jedes Skript mit.
    assert "ticket" not in inhalt


def test_ticket_wirkt_genau_einmal(klient, reauth):
    daten = antrag(klient).json()
    freigabe(klient, daten["rid"], daten["verify_word"])
    erste = abholung(klient, daten["rid"], daten["redeem_key"])
    zweite = abholung(klient, daten["rid"], daten["redeem_key"])
    assert erste.status_code == 200 and erste.json()["ticket"]
    assert zweite.status_code == 410
    assert zweite.json()["error"] == "ticket_bereits_abgeholt"


def test_gleichzeitige_abholung_gibt_das_ticket_nur_einmal_heraus(klient, reauth, modul,
                                                                 monkeypatch):
    """Zwei Abholungen zur selben Zeit: die zweite arbeitet auf einem veralteten
    Stand ('approved', Ticket noch da). Nur wer die Zeile wirklich von
    'approved' wegbewegt, darf den Schein bekommen."""
    daten = antrag(klient).json()
    freigabe(klient, daten["rid"], daten["verify_word"])
    with modul.db() as conn:
        veraltet = modul._lade(conn, daten["rid"])

    assert abholung(klient, daten["rid"], daten["redeem_key"]).status_code == 200
    monkeypatch.setattr(modul, "_lade", lambda conn, rid: dict(veraltet))
    zweite = abholung(klient, daten["rid"], daten["redeem_key"])
    assert zweite.status_code == 410
    assert zweite.json()["error"] == "ticket_bereits_abgeholt"


def test_abholung_vor_der_freigabe_meldet_pending(klient):
    daten = antrag(klient).json()
    antwort = abholung(klient, daten["rid"], daten["redeem_key"])
    assert antwort.status_code == 200
    assert antwort.json()["state"] == "pending"
    assert 0 < antwort.json()["remaining"] <= 120


def test_ablehnen_braucht_kein_kennwort(klient):
    daten = antrag(klient).json()
    antwort = klient.post("/api/v1/link/confirm",
                          headers={**kopf(), "Origin": WEB_URSPRUNG,
                                   "Sec-Fetch-Site": "same-origin",
                                   "Sec-Fetch-Mode": "cors"},
                          json={"rid": daten["rid"], "confirm": False,
                                "origin": WEB_URSPRUNG, "reason": "user_cancelled"})
    assert antwort.json()["state"] == "denied"
    nach = abholung(klient, daten["rid"], daten["redeem_key"]).json()
    assert nach["state"] == "denied"


def test_zweite_freigabe_desselben_vorgangs_prallt_ab(klient, reauth):
    daten = antrag(klient).json()
    assert freigabe(klient, daten["rid"], daten["verify_word"]).status_code == 200
    zweite = freigabe(klient, daten["rid"], daten["verify_word"])
    assert zweite.status_code == 409


# ---------------------------------------------------------------------------
# Kennwort raten
# ---------------------------------------------------------------------------

def test_falsches_kennwort_verbrennt_den_vorgang_nach_drei_versuchen(klient, reauth, modul):
    daten = antrag(klient).json()
    falsch = "AAAAAA" if daten["verify_word"] != "AAAAAA" else "BBBBBB"
    assert modul.MAX_KENNWORT_FEHLER == 3
    for verbleibend in (2, 1, 0):
        antwort = freigabe(klient, daten["rid"], falsch)
        assert antwort.status_code == 403
        assert antwort.json()["error"] == "kennwort_falsch"
        if verbleibend:
            sicht = klient.get(f"/api/v1/link/request/{daten['rid']}", headers=kopf()).json()
            assert sicht["attempts_left"] == verbleibend
    # Danach hilft auch das richtige Kennwort nicht mehr.
    nachzuegler = freigabe(klient, daten["rid"], daten["verify_word"])
    assert nachzuegler.status_code == 409
    assert abholung(klient, daten["rid"], daten["redeem_key"]).json()["state"] == "denied"


def test_erratensperre_greift_ueber_vorgaenge_hinweg(klient, reauth):
    # Nach genügend Fehlversuchen ist der Nutzer gebremst, nicht nur der Vorgang.
    for _ in range(4):
        daten = antrag(klient).json()
        for _ in range(3):
            freigabe(klient, daten["rid"], "AAAAAA")
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"])
    assert antwort.status_code == 429


# ---------------------------------------------------------------------------
# Ablauf der Frist
# ---------------------------------------------------------------------------

def test_abgelaufener_antrag_wird_nicht_mehr_freigegeben(klient, reauth, modul):
    daten = antrag(klient).json()
    with modul.db() as conn:
        conn.execute("UPDATE link_vorgaenge SET laeuft_ab_at=? WHERE rid=?",
                     (time.time() - 1, daten["rid"]))
    assert klient.get(f"/api/v1/link/request/{daten['rid']}",
                      headers=kopf()).status_code == 410
    antwort = freigabe(klient, daten["rid"], daten["verify_word"])
    assert antwort.status_code == 410
    assert antwort.json()["error"] == "antrag_abgelaufen"


def test_abgelaufener_vorgang_gibt_kein_ticket_mehr_heraus(klient, reauth, modul):
    daten = antrag(klient).json()
    freigabe(klient, daten["rid"], daten["verify_word"])
    with modul.db() as conn:
        conn.execute("UPDATE link_vorgaenge SET laeuft_ab_at=? WHERE rid=?",
                     (time.time() - 1, daten["rid"]))
    antwort = abholung(klient, daten["rid"], daten["redeem_key"])
    assert antwort.status_code == 410
    assert antwort.json()["error"] == "antrag_abgelaufen"


# ---------------------------------------------------------------------------
# Ratenbegrenzung der Anträge
# ---------------------------------------------------------------------------

def test_hoechstens_drei_offene_antraege(klient, modul):
    for _ in range(modul.MAX_OFFENE_ANTRAEGE):
        assert antrag(klient).status_code == 201
    antwort = antrag(klient)
    assert antwort.status_code == 429
    assert antwort.json()["error"] == "too_many_requests"


def test_antragsrate_je_zehn_minuten(klient, modul):
    # Offene Anträge laufend abräumen, damit die zweite Grenze greift.
    for _ in range(modul.MAX_ANTRAEGE_JE_FENSTER):
        rid = antrag(klient).json()["rid"]
        freigabe(klient, rid, confirm=False)
    antwort = antrag(klient)
    assert antwort.status_code == 429


# ---------------------------------------------------------------------------
# E8 — der Wunsch wird nie zur Freigabe
# ---------------------------------------------------------------------------

def test_fehlende_felder_kommen_aus_preselect_nicht_aus_requested(klient, reauth):
    """Prüfsatz 10: POST /confirm ohne access erteilt read, nicht den Wunsch.

    Das war der eigentliche Befund: fehlte ein Feld, entschied bisher der
    Antragsteller über seine eigene Befugnis.
    """
    daten = antrag(klient, requested={"access": "write", "duration": 3600,
                                      "mode": "domains",
                                      "allow": ["geizhals.de", "www.geizhals.de"],
                                      "tab_host": "geizhals.de",
                                      "step_mode": "auto"}).json()
    # Nur rid, confirm, origin, verify_word und reauth — kein einziges Maß.
    antwort = freigabe(klient, daten["rid"], daten["verify_word"])
    assert antwort.status_code == 200, antwort.text
    gewaehrt = antwort.json()["granted"]
    assert gewaehrt["access"] == "read"
    assert gewaehrt["duration"] == 600
    assert gewaehrt["mode"] == "tab"
    assert gewaehrt["allow"] == ["geizhals.de"]        # E7: der tab_host, nicht []
    assert gewaehrt["step_mode"] == "confirm_each"
    assert gewaehrt["idle_timeout"] == 600


def test_preselect_ist_unabhaengig_vom_wunsch(klient):
    """Prüfsatz 9: preselect ist immer read / 600 / tab — egal was requested sagt."""
    daten = antrag(klient, requested={"access": "write", "duration": 3600,
                                      "mode": "domains", "allow": ["geizhals.de"],
                                      "tab_host": "geizhals.de",
                                      "step_mode": "auto"}).json()
    sicht = klient.get(f"/api/v1/link/request/{daten['rid']}", headers=kopf()).json()
    assert sicht["preselect"] == {"access": "read", "duration": 600, "mode": "tab",
                                  "allow": [], "step_mode": "confirm_each"}
    assert sicht["requested"]["access"] == "write"     # nur Zitat
    assert sicht["requested"]["duration"] == 3600


def test_tab_modus_setzt_allow_auf_den_tab_host(klient, reauth):
    """Prüfsatz 18: Freigabe mit mode 'tab' → Ticket enthält [tab_host], nicht []."""
    _, anspruch, gewaehrt = voller_ablauf(klient, mode="tab", allow=[])
    assert anspruch["mode"] == "tab"
    assert anspruch["allow"] == ["geizhals.de"]
    assert gewaehrt["allow"] == ["geizhals.de"]


# ---------------------------------------------------------------------------
# Grenzen der Freigabeseite
# ---------------------------------------------------------------------------

def test_freigabeseite_kann_den_bereich_nicht_erweitern(klient, reauth):
    """Prüfsatz 14: allow mit einer nicht beantragten Adresse → bereich_erweitert."""
    daten = antrag(klient, requested={"access": "write", "duration": 600,
                                      "mode": "domains", "allow": ["geizhals.de"],
                                      "tab_host": "geizhals.de"}).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"], mode="domains",
                       allow=["geizhals.de", "bank.example.de"])
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "bereich_erweitert"


def test_freigabeseite_darf_den_tab_host_auf_die_domain_weiten(klient, reauth):
    daten = antrag(klient).json()   # Modus tab, tab_host = geizhals.de
    antwort = freigabe(klient, daten["rid"], daten["verify_word"],
                       mode="domains", allow=["*.geizhals.de"])
    assert antwort.status_code == 200, antwort.text
    assert antwort.json()["granted"]["allow"] == ["*.geizhals.de"]


def test_freigabeseite_kann_die_dauer_nicht_ueber_den_deckel_heben(klient, reauth):
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"], duration=7200)
    assert antwort.status_code == 400
    assert antwort.json()["error"] == "dauer_ausserhalb_deckel"


def test_freigabeseite_kann_keine_stufe_full_erteilen(klient, reauth):
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"], access="full")
    assert antwort.status_code == 403


def test_automatikmodus_ist_gesperrt(klient, reauth):
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"], step_mode="auto")
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "modus_noch_gesperrt"


def test_automatikmodus_nach_freischaltung(klient, reauth, monkeypatch):
    monkeypatch.setenv("LINK_AUTO_MODUS", "1")
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"], step_mode="auto")
    assert antwort.status_code == 200
    assert antwort.json()["granted"]["step_mode"] == "auto"


# ---------------------------------------------------------------------------
# E9 — reauth ist bei JEDER Freigabe Pflicht
# ---------------------------------------------------------------------------

def test_ohne_reauth_wird_auch_read_nicht_erteilt(klient, reauth):
    """Prüfsatz 13: POST /confirm ohne reauth → 403, auch bei read."""
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"], reauth=None)
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "reauth_erforderlich"


def test_falsches_kontopasswort_erteilt_nichts(klient, reauth):
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"],
                       reauth={"method": "password", "assertion": "falsch"})
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "reauth_erforderlich"


def test_reauth_feld_heisst_assertion_nicht_password(klient, reauth):
    # E3: derselbe Platz trägt später eine WebAuthn-Signatur.
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"],
                       reauth={"method": "password", "password": KONTOPASSWORT})
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "reauth_erforderlich"


def test_ohne_eingehaengten_pruefer_gibt_es_keine_freigabe(klient):
    # Kein reauth-Prüfer eingehängt: fail-closed, auch mit richtigem Kennwort.
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"])
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "reauth_erforderlich"


def test_write_mit_eingehaengtem_pruefer(klient, reauth):
    daten = antrag(klient, requested={"access": "write", "duration": 600,
                                      "mode": "domains", "allow": ["geizhals.de"],
                                      "tab_host": "geizhals.de"}).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"], access="write")
    assert antwort.status_code == 200
    geholt = abholung(klient, daten["rid"], daten["redeem_key"]).json()
    anspruch = jwt.decode(geholt["ticket"], GEHEIM, algorithms=["HS256"],
                          audience="smartr-connect")
    assert anspruch["access"] == "write"


# ---------------------------------------------------------------------------
# §7.1 — Herkunftsbindung bei /confirm
# ---------------------------------------------------------------------------

def test_confirm_aus_der_erweiterung_wird_abgewiesen_und_verbrennt(klient, reauth, modul):
    """Prüfsatz 11: Origin chrome-extension:// → 403, Vorgang danach denied.

    Das ist der Befund: Kennwort und Ausweis besitzt die Erweiterung selbst.
    Ohne diese Bindung könnte sie sich selbst freigeben.
    """
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"], ursprung=EXT_URSPRUNG,
                       sec_site="cross-site")
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "herkunft_ungueltig"

    with modul.db() as conn:
        vorgang = modul._lade(conn, daten["rid"])
    assert vorgang["zustand"] == "denied"
    assert vorgang["ticket"] is None
    # Auch das richtige Kennwort aus dem Web-Ursprung hilft jetzt nicht mehr.
    assert freigabe(klient, daten["rid"], daten["verify_word"]).status_code == 409


def test_confirm_ohne_origin_kopf_wird_abgewiesen(klient, reauth, modul):
    """Prüfsatz 12: kein Origin-Kopf → 403 herkunft_ungueltig."""
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"], ursprung=None,
                       rumpf_ursprung=WEB_URSPRUNG, sec_site=None, sec_mode=None)
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "herkunft_ungueltig"
    with modul.db() as conn:
        assert modul._lade(conn, daten["rid"])["zustand"] == "denied"


def test_confirm_mit_fremdem_origin_wird_abgewiesen(klient, reauth):
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"],
                       ursprung="https://cloud.smartragents.ai.evil.example")
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "herkunft_ungueltig"


def test_confirm_ohne_rumpffeld_origin_wird_abgewiesen(klient, reauth):
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"], rumpf_ursprung=None)
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "herkunft_ungueltig"


def test_confirm_mit_widersprechendem_rumpffeld_wird_abgewiesen(klient, reauth):
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"],
                       rumpf_ursprung="https://andere.example")
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "herkunft_ungueltig"


def test_confirm_mit_cross_site_kopf_wird_abgewiesen(klient, reauth):
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"], sec_site="cross-site")
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "herkunft_ungueltig"


def test_confirm_ohne_sec_fetch_koepfe_geht_durch(klient, reauth):
    # Alte Browser senden sie nicht; vorhanden müssen sie stimmen, fehlend
    # sind sie kein Grund, einen Menschen auszusperren.
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"],
                       sec_site=None, sec_mode=None)
    assert antwort.status_code == 200


def test_unangemeldeter_kann_keinen_fremden_vorgang_abschiessen(klient, reauth, modul):
    """Reihenfolge: erst Identität, dann Herkunft.

    Stand die Herkunft vorn, verbrannte sie den Vorgang, bevor überhaupt
    feststand, wem er gehört. Damit konnte jeder, der eine `rid` kannte oder
    riet, mit einem falschen Ursprung und ganz ohne Ausweis eine fremde
    Freigabe abschießen. Ein Fehlschlag darf keinen fremden Vorgang verändern.
    """
    daten = antrag(klient).json()

    # Ohne jeden Ausweis, mit dem Ursprung der Erweiterung.
    ohne_ausweis = klient.post(
        "/api/v1/link/confirm",
        json={"rid": daten["rid"], "confirm": True, "origin": EXT_URSPRUNG,
              "verify_word": daten["verify_word"],
              "reauth": {"method": "password", "assertion": KONTOPASSWORT}},
        headers={"Origin": EXT_URSPRUNG, "Sec-Fetch-Site": "cross-site"})
    assert ohne_ausweis.status_code == 401

    with modul.db() as conn:
        assert modul._lade(conn, daten["rid"])["zustand"] == "pending"

    # Der rechtmäßige Mensch kann anschließend ganz normal freigeben.
    assert freigabe(klient, daten["rid"], daten["verify_word"]).status_code == 200


def test_fremder_ausweis_mit_falscher_herkunft_veraendert_nichts(klient, reauth, modul):
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"],
                       token=alltags_jwt(sub="99"), ursprung=EXT_URSPRUNG,
                       sec_site="cross-site")
    # Ein fremder Vorgang wird wie ein nicht vorhandener behandelt (§3.2/§3.3).
    assert antwort.status_code == 404
    assert antwort.json()["error"] == "antrag_unbekannt"

    with modul.db() as conn:
        assert modul._lade(conn, daten["rid"])["zustand"] == "pending"
    assert freigabe(klient, daten["rid"], daten["verify_word"]).status_code == 200


def test_herkunftsverletzung_steht_mit_ursprung_im_protokoll(klient, reauth, modul):
    daten = antrag(klient).json()
    freigabe(klient, daten["rid"], daten["verify_word"], ursprung=EXT_URSPRUNG,
             sec_site="cross-site")
    with modul.db() as conn:
        zeilen = conn.execute(
            "SELECT ereignis, detail FROM link_ereignisse "
            "WHERE ereignis LIKE 'herkunft%'").fetchall()
    assert zeilen, "die Herkunftsverletzung fehlt im Protokoll"
    ereignis, detail = zeilen[0]
    assert ereignis == "herkunft_erweiterung"
    assert EXT_URSPRUNG in detail
    assert EXT_ID in detail


# ---------------------------------------------------------------------------
# §7.2 — Herkunftsbindung bei /redeem
# ---------------------------------------------------------------------------

def test_redeem_ohne_schluessel_verbrennt_beim_dritten_versuch(klient, reauth, modul):
    """Prüfsatz 15: ohne redeem_key → 403; erst der dritte Versuch verbrennt."""
    daten = antrag(klient).json()
    freigabe(klient, daten["rid"], daten["verify_word"])

    for versuch in (1, 2, 3):
        antwort = abholung(klient, daten["rid"], redeem_key=None)
        assert antwort.status_code == 403
        assert antwort.json()["error"] == "herkunft_ungueltig"
        with modul.db() as conn:
            zustand = modul._lade(conn, daten["rid"])["zustand"]
        assert zustand == ("denied" if versuch == 3 else "approved")

    # Danach hilft auch der richtige Schlüssel nicht mehr.
    assert abholung(klient, daten["rid"], daten["redeem_key"]).json()["state"] == "denied"


def test_redeem_mit_falschem_schluessel_gibt_kein_ticket(klient, reauth):
    daten = antrag(klient).json()
    freigabe(klient, daten["rid"], daten["verify_word"])
    antwort = abholung(klient, daten["rid"], "x" * 43)
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "herkunft_ungueltig"


def test_redeem_aus_dem_web_ursprung_gibt_kein_ticket(klient, reauth):
    # Die Freigabeseite kennt rid und Ausweis — aber nie den redeem_key. Selbst
    # mit ihm wäre ihr Ursprung falsch.
    daten = antrag(klient).json()
    freigabe(klient, daten["rid"], daten["verify_word"])
    antwort = abholung(klient, daten["rid"], daten["redeem_key"], ursprung=WEB_URSPRUNG)
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "herkunft_ungueltig"


def test_redeem_ohne_origin_kopf_gibt_kein_ticket(klient, reauth):
    daten = antrag(klient).json()
    freigabe(klient, daten["rid"], daten["verify_word"])
    antwort = abholung(klient, daten["rid"], daten["redeem_key"], ursprung=None)
    assert antwort.status_code == 403


def test_redeem_einer_fremden_erweiterung_gibt_kein_ticket(klient, reauth):
    daten = antrag(klient).json()
    freigabe(klient, daten["rid"], daten["verify_word"])
    antwort = abholung(klient, daten["rid"], daten["redeem_key"],
                       ursprung="chrome-extension://fremdefremdefremdefremdefremdefr")
    assert antwort.status_code == 403


def test_vorgang_ohne_gespeicherte_kennung_gibt_kein_ticket(klient, reauth, modul):
    """Altbestand aus der Platzhalter-Zeit: ext_id leer oder '*'.

    Früher hieß eine solche Zeile „irgendeine Erweiterung reicht". Jetzt bindet
    §7.2 immer an genau eine Kennung — ohne sie gibt es keine Abholung.
    """
    for kaputte_kennung in ("", "*"):
        daten = antrag(klient).json()
        freigabe(klient, daten["rid"], daten["verify_word"])
        with modul.db() as conn:
            conn.execute("UPDATE link_vorgaenge SET ext_id=? WHERE rid=?",
                         (kaputte_kennung, daten["rid"]))
        for ursprung in (EXT_URSPRUNG, "chrome-extension://" + ("a" * 32)):
            antwort = abholung(klient, daten["rid"], daten["redeem_key"], ursprung=ursprung)
            assert antwort.status_code == 403, (kaputte_kennung, ursprung)
            assert antwort.json()["error"] == "herkunft_ungueltig"


# ---------------------------------------------------------------------------
# E10 — die Anzeige verbraucht nichts
# ---------------------------------------------------------------------------

def test_anzeige_darf_beliebig_oft_gerufen_werden(klient, reauth):
    """Prüfsatz 17: zehnmal GET, danach ist /redeem weiterhin möglich."""
    daten = antrag(klient).json()
    for _ in range(10):
        assert klient.get(f"/api/v1/link/request/{daten['rid']}",
                          headers=kopf()).status_code == 200
    freigabe(klient, daten["rid"], daten["verify_word"])
    for _ in range(10):
        assert klient.get(f"/api/v1/link/request/{daten['rid']}",
                          headers=kopf()).status_code == 200
    antwort = abholung(klient, daten["rid"], daten["redeem_key"])
    assert antwort.status_code == 200
    assert antwort.json()["ticket"]


# ---------------------------------------------------------------------------
# Eingehängte Prüfer
# ---------------------------------------------------------------------------

def test_kontingent_pruefer_kann_402_werfen(klient, modul):
    def kein_guthaben(kontext):
        raise modul.LinkFehler(402, "kontingent", "Kein Guthaben.")
    modul.setze_kontingent_pruefer(kein_guthaben)
    antwort = antrag(klient)
    assert antwort.status_code == 402
    assert antwort.json()["error"] == "kontingent"


def test_agb_pruefer_kann_451_werfen(klient, modul):
    def alte_agb(kontext):
        raise modul.LinkFehler(451, "agb")
    modul.setze_agb_pruefer(alte_agb)
    assert antrag(klient).status_code == 451


def test_bereichs_pruefer_kann_domain_sperren(klient, modul):
    def sperrliste(kontext, allow):
        if "geizhals.de" in allow:
            raise modul.LinkFehler(403, "bereich_gesperrt", "geizhals.de ist gesperrt.")
    modul.setze_bereichs_pruefer(sperrliste)
    antwort = antrag(klient, requested={"access": "read", "duration": 600,
                                        "mode": "domains", "allow": ["geizhals.de"],
                                        "tab_host": "geizhals.de"})
    assert antwort.status_code == 403
    assert antwort.json()["error"] == "bereich_gesperrt"


def test_bereichs_pruefer_sieht_genau_die_gewaehrten_adressen(klient, modul):
    """Befund S1 (29.07.2026): Der Prüfer bekam `allow` aus dem Antrag. Bei
    mode `tab` ist diese Liste LEER — gewährt wurde danach [tab_host], den der
    Prüfer nie gesehen hat. Er muss die Adressen bekommen, die wirklich im
    Ticket landen."""
    gesehen = []
    modul.setze_bereichs_pruefer(lambda kontext, allow: gesehen.append(list(allow)))
    assert leseantrag(klient).status_code == 201
    assert gesehen == [["geizhals.de"]]


def test_sperrliste_haelt_die_sitzungsfreigabe_auf(klient, modul):
    """S1, der Weg aus E16: Ein Antrag auf eine gesperrte Adresse kam mit
    201/approved/write/3600 s durch, weil der Prüfer eine leere Liste sah."""
    def sperrliste(kontext, allow):
        if "bank.example" in allow:
            raise modul.LinkFehler(400, "bereich_ungueltig",
                                   "bank.example ist für die Agentensteuerung gesperrt.")
    modul.setze_bereichs_pruefer(sperrliste)
    antwort = leseantrag(klient, requested={
        "access": "write", "duration": 3600, "mode": "tab", "allow": [],
        "tab_host": "bank.example", "step_mode": "confirm_each"})
    assert antwort.status_code == 400
    assert antwort.json()["error"] == "bereich_ungueltig"
    assert "granted" not in antwort.json()
    # Und es darf auch kein Vorgang zurückbleiben, den /redeem noch abholt.
    with modul.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM link_vorgaenge").fetchone()[0] == 0


def test_sperrliste_haelt_die_freigabeseite_auf(klient, reauth, modul):
    """S1 auf dem manuellen Weg: Der Mensch darf 'nur dieser Tab' wählen — dann
    ist der tab_host die gewährte Adresse und muss geprüft sein. Der Antrag
    selbst nennt eine erlaubte Domain, kommt also bis zur Freigabeseite."""
    def sperrliste(kontext, allow):
        if "bank.example" in allow:
            raise modul.LinkFehler(400, "bereich_ungueltig", "bank.example ist gesperrt.")
    daten = antrag(klient, requested={"access": "write", "duration": 600,
                                      "mode": "domains", "allow": ["geizhals.de"],
                                      "tab_host": "bank.example",
                                      "step_mode": "confirm_each"}).json()
    assert daten["state"] == "pending"
    modul.setze_bereichs_pruefer(sperrliste)
    antwort = freigabe(klient, daten["rid"], daten["verify_word"], mode="tab", allow=[])
    assert antwort.status_code == 400
    assert antwort.json()["error"] == "bereich_ungueltig"
    assert abholung(klient, daten["rid"], daten["redeem_key"]).json().get("ticket") is None


def test_identitaets_pruefer_des_gateways_wird_benutzt(klient, reauth, modul):
    modul.setze_identitaet_pruefer(lambda request: {"user_id": "7", "tenant": "kunde-7"})
    daten = antrag(klient, token="unsinn").json()
    freigabe(klient, daten["rid"], daten["verify_word"], token="unsinn")
    geholt = abholung(klient, daten["rid"], daten["redeem_key"], token="unsinn").json()
    anspruch = jwt.decode(geholt["ticket"], GEHEIM, algorithms=["HS256"],
                          audience="smartr-connect")
    assert anspruch["sub"] == "7"
    assert anspruch["tnt"] == "kunde-7"


# ---------------------------------------------------------------------------
# Protokoll
# ---------------------------------------------------------------------------

def test_ablehnungen_stehen_im_protokoll(klient, reauth, modul):
    daten = antrag(klient).json()
    freigabe(klient, daten["rid"], "AAAAAA")
    with modul.db() as conn:
        ereignisse = [z[0] for z in conn.execute(
            "SELECT ereignis FROM link_ereignisse ORDER BY id").fetchall()]
    assert "antrag_gestellt" in ereignisse
    assert "kennwort_falsch" in ereignisse


def test_freigabe_wird_mit_jti_protokolliert(klient, reauth, modul):
    daten = antrag(klient).json()
    freigabe(klient, daten["rid"], daten["verify_word"])
    with modul.db() as conn:
        detail = conn.execute(
            "SELECT detail FROM link_ereignisse WHERE ereignis='freigegeben'").fetchone()[0]
    assert "tk_" in detail


def test_aufraeumen_loescht_nur_alte_vorgaenge(klient, modul):
    frisch = antrag(klient).json()["rid"]
    alt = antrag(klient).json()["rid"]
    with modul.db() as conn:
        conn.execute("UPDATE link_vorgaenge SET erstellt_at=? WHERE rid=?",
                     (time.time() - 200000, alt))
    assert modul.aufraeumen() == 1
    with modul.db() as conn:
        uebrig = [z[0] for z in conn.execute("SELECT rid FROM link_vorgaenge").fetchall()]
    assert uebrig == [frisch]


# ---------------------------------------------------------------------------
# E15 — Sitzungsfreigabe für die Lesestufe (Entscheid des Inhabers, 28.07.2026)
# ---------------------------------------------------------------------------

def test_leseantrag_wird_sofort_bewilligt(klient):
    antwort = leseantrag(klient)
    assert antwort.status_code == 201
    daten = antwort.json()
    assert set(daten) == {"rid", "state", "granted", "redeem_key", "expires_in"}
    assert daten["state"] == "approved"
    # Kein Kennwort, keine Freigabeadresse: es gibt nichts abzutippen.
    assert "verify_word" not in daten and "confirm_url" not in daten


def test_sitzungsfreigabe_deckelt_bereich_und_schrittmodus(klient):
    # E16: Die gewünschte Dauer wird bis MAX_DURATION gewährt — was NICHT
    # verhandelbar bleibt, ist der Bereich (nur der Tab). Der Schrittmodus
    # folgt seit dem 15.08.2026 dem Antrag; "auto" fällt aber auf
    # confirm_each zurück, solange LINK_AUTO_MODUS ihn nicht freigibt —
    # gemessen wird hier genau dieser Rückfall (Umgebung ist im Test leer).
    daten = leseantrag(klient, requested={
        "access": "read", "duration": 3600, "mode": "tab", "allow": [],
        "tab_host": "geizhals.de", "step_mode": "auto"}).json()
    g = daten["granted"]
    assert g["access"] == "read"
    assert g["duration"] == 3600
    assert g["mode"] == "tab"
    assert g["allow"] == ["geizhals.de"]
    assert g["step_mode"] == "confirm_each"
    # Leerlauffrist: am 05.08.2026 von 180 auf 600 Sekunden angehoben.
    # Grund: Die Sitzung des Inhabers starb nach 182 von 600 Sekunden mit
    # end_reason=session_idle, waehrend er noch den Auftrag tippte. Drei
    # Minuten sind kuerzer als ein Mensch braucht, um einen Auftrag zu
    # formulieren und die erste Freigabe zu erteilen.
    assert g["idle_timeout"] == 600


def test_sitzungsfreigabe_bedienstufe_wird_bewilligt(klient):
    # E16: `write` mit tab_host bekommt die Sitzungsfreigabe — sofort, ohne
    # Kennwort. Deckel wie bei read: nur der Tab, jeder Schritt einzeln.
    daten = leseantrag(klient, requested={
        "access": "write", "duration": 1800, "mode": "tab", "allow": [],
        "tab_host": "geizhals.de", "step_mode": "confirm_each"}).json()
    assert daten["state"] == "approved"
    assert "verify_word" not in daten
    g = daten["granted"]
    assert g["access"] == "write"
    assert g["duration"] == 1800
    assert g["allow"] == ["geizhals.de"]
    assert g["step_mode"] == "confirm_each"


def test_sitzungsfreigabe_assist_wird_gewaehrt(klient):
    # OFFEN-v3.5 §2.1, geschlossen am 15.08.2026: Der mittlere Modus existiert
    # jetzt auf der Leitung. Er braucht keine Freischaltung — die harten
    # Klassen der Erweiterung fragen in jedem Modus.
    daten = leseantrag(klient, requested={
        "access": "write", "duration": 1800, "mode": "tab", "allow": [],
        "tab_host": "geizhals.de", "step_mode": "assist"}).json()
    assert daten["state"] == "approved"
    assert daten["granted"]["step_mode"] == "assist"


def test_sitzungsfreigabe_auto_nach_freischaltung(klient, monkeypatch):
    # Mit LINK_AUTO_MODUS=1 folgt die Sitzungsfreigabe dem Antrag auch bei
    # "auto" — der Rückfall aus dem Prüfsatz darüber hängt also wirklich an
    # der Umgebung und nicht an einer festen Zeile.
    monkeypatch.setenv("LINK_AUTO_MODUS", "1")
    daten = leseantrag(klient, requested={
        "access": "write", "duration": 1800, "mode": "tab", "allow": [],
        "tab_host": "geizhals.de", "step_mode": "auto"}).json()
    assert daten["state"] == "approved"
    assert daten["granted"]["step_mode"] == "auto"


def test_mitdenken_ist_auch_auf_der_freigabeseite_frei(klient, reauth):
    # Die Freigabeseite darf "assist" ohne Freischaltung erteilen; gesperrt
    # bleibt allein "auto" (test_automatikmodus_ist_gesperrt).
    daten = antrag(klient).json()
    antwort = freigabe(klient, daten["rid"], daten["verify_word"], step_mode="assist")
    assert antwort.status_code == 200, antwort.text
    assert antwort.json()["granted"]["step_mode"] == "assist"


def test_sitzungsfreigabe_kuerzere_dauer_bleibt_kuerzer(klient):
    daten = leseantrag(klient, requested={
        "access": "read", "duration": 120, "mode": "tab", "allow": [],
        "tab_host": "geizhals.de", "step_mode": "confirm_each"}).json()
    assert daten["granted"]["duration"] == 120


def test_sitzungsfreigabe_ticket_kommt_aus_redeem_und_nur_einmal(klient, modul):
    daten = leseantrag(klient).json()
    geholt = abholung(klient, daten["rid"], daten["redeem_key"])
    assert geholt.status_code == 200
    anspruch = jwt.decode(geholt.json()["ticket"], GEHEIM, algorithms=["HS256"],
                          audience="smartr-connect")
    assert anspruch["access"] == "read"
    assert anspruch["allow"] == ["geizhals.de"]
    assert anspruch["scope"] == "smartrlink-ticket"
    zweite = abholung(klient, daten["rid"], daten["redeem_key"])
    assert zweite.status_code == 410


def test_sitzungsfreigabe_redeem_bleibt_an_der_erweiterung(klient):
    # §7.2 gilt unverändert: falscher Schlüssel oder Web-Ursprung → kein Ticket.
    daten = leseantrag(klient).json()
    falsch = abholung(klient, daten["rid"], "x" * 43)
    assert falsch.status_code in (403, 404)
    web = abholung(klient, daten["rid"], daten["redeem_key"], ursprung=WEB_URSPRUNG)
    assert web.status_code in (403, 404)
    assert "ticket" not in web.json()


def test_sitzungsfreigabe_anzeige_ohne_passwortfeld(klient):
    daten = leseantrag(klient).json()
    sicht = klient.get(f"/api/v1/link/request/{daten['rid']}", headers=kopf()).json()
    assert sicht["state"] == "approved"
    assert sicht["reauth_required"] is False
    text = str(sicht)
    assert daten["redeem_key"] not in text
    assert "ticket" not in sicht


def test_sitzungsfreigabe_kennt_keinen_zweiten_weg(klient, reauth):
    # Eine Confirm-Route auf einem bereits bewilligten Vorgang darf kein
    # zweites Ticket erzeugen.
    daten = leseantrag(klient).json()
    antwort = freigabe(klient, daten["rid"], "AAAAAA")
    assert antwort.status_code == 409
    assert antwort.json()["error"] == "antrag_bereits_entschieden"


def test_domains_antrag_bleibt_beim_manuellen_weg(klient):
    # E16: Nicht mehr die Stufe entscheidet über den Weg, sondern der
    # Bereich — wer über den einen Tab hinauswill, geht zur Freigabeseite.
    daten = antrag(klient).json()
    assert daten["state"] == "pending"
    assert "verify_word" in daten and "confirm_url" in daten


def test_lesestufe_ohne_tab_host_bleibt_beim_manuellen_weg(klient):
    # Ohne den Tab des Antrags gibt es keinen engsten Bereich, auf den der
    # Server deckeln könnte — dann entscheidet weiter der Mensch.
    daten = leseantrag(klient, requested={
        "access": "read", "duration": 600, "mode": "domains",
        "allow": ["geizhals.de"], "tab_host": "", "step_mode": "confirm_each"}).json()
    assert daten["state"] == "pending"
    assert "verify_word" in daten


def test_sitzungsfreigaben_zaehlen_zur_ratenbremse(klient):
    for _ in range(3):
        assert leseantrag(klient).status_code == 201
    vierter = leseantrag(klient)
    assert vierter.status_code == 429

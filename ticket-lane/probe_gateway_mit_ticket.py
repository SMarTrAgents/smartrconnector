#!/usr/bin/env python3
"""Startprobe für die eingehängte Gateway-Kopie — der Durchstich in einem Lauf.

    python3 probe_gateway_mit_ticket.py

Was geprüft wird, alles gegen ``gateway_mit_ticket.py`` (nicht gegen ein
Nachbau-``FastAPI``, wie es die 93 Modultests tun):

1. Die Kopie lässt sich importieren — der Einhängeblock bricht den Gateway
   nicht.
2. Die vier Pfade ``/api/v1/link/*`` hängen an der echten ``app``.
3. Alle fünf Prüfer sind verdrahtet (ein vergessener sperrt, aber still).
4. Ein vollständiger Freigabeweg mit einem echten Konto aus der Gateway-
   Datenbank: request → GET → confirm (Kennwort **und** Kontopasswort) →
   redeem. Das Ticket wird am Ende mit ``JWT_SECRET`` geprüft.
5. Die Gegenproben, an denen die letzte Fassung gescheitert wäre: falsches
   Kontopasswort, halbe Anmeldung (``amr: mfa-pending``), Ausweis in der
   Adresszeile, fremde Erweiterungskennung, Freigabe aus dem falschen Ursprung.

Es läuft ausschließlich lokal: eigene SQLite-Dateien unter ``/tmp``, kein Netz,
kein Server. Der Produktivserver wird nicht berührt.
"""

import os
import sqlite3
import sys
import tempfile

ARBEIT = tempfile.mkdtemp(prefix="smartrlink-probe-")
CRM_DB = os.path.join(ARBEIT, "smarthdesk.db")

os.environ["CRM_DB"] = CRM_DB
os.environ["LINK_DB"] = os.path.join(ARBEIT, "link.db")
os.environ["JWT_SECRET"] = "probe-geheimnis-nur-lokal"
os.environ["LINK_EXT_IDS"] = "bnijccjilloldoelpmndoaddgccilnfd"
os.environ["LINK_CONFIRM_ORIGIN"] = "https://cloud.smartragents.ai"
os.environ.setdefault("EMAIL_GATE", "0")

EXT_ID = "bnijccjilloldoelpmndoaddgccilnfd"
EXT_ORIGIN = "chrome-extension://" + EXT_ID
WEB_ORIGIN = "https://cloud.smartragents.ai"
PASSWORT = "Ein-sehr-langes-Kontopasswort-2026!"


def _lege_users_tabelle_an() -> None:
    """`migrate()` des Gateways erweitert `users`, legt die Tabelle aber nicht
    an — im Betrieb kommt sie aus der Desk-Datenbank. Für die Probe wird das
    Grundgerüst hier gebaut; die fehlenden Spalten ergänzt `migrate()` selbst.
    """
    con = sqlite3.connect(CRM_DB)
    con.execute("""CREATE TABLE users (
        id TEXT PRIMARY KEY, email TEXT, name TEXT, tier TEXT,
        token_balance INTEGER DEFAULT 0, token_used INTEGER DEFAULT 0,
        subscription_until TEXT, added_date TEXT, entitlements TEXT,
        is_partner_contact INTEGER DEFAULT 0, created_at TEXT,
        password_hash TEXT, role TEXT, has_container INTEGER DEFAULT 0)""")
    con.commit()
    con.close()


def hauptlauf() -> int:
    _lege_users_tabelle_an()

    import gateway_mit_ticket as gw          # noqa: E402 — erst nach den Env-Werten
    from fastapi.testclient import TestClient

    fehler = []

    def pruefe(bedingung, satz):
        if bedingung:
            print(f"  ok   {satz}")
        else:
            print(f"  FEHL {satz}")
            fehler.append(satz)

    print("1. Kopie importiert, Routen hängen an der echten app")

    def sammle(routen, gesehen=None):
        """Pfade einsammeln. Neuere FastAPI-Fassungen hängen einen mit
        `include_router` eingebundenen Router als EIN Objekt ohne `path` ein —
        wer nur die oberste Ebene absucht, findet die vier Pfade nicht und
        hielte die Einhängung fälschlich für gescheitert."""
        gesehen = gesehen if gesehen is not None else set()
        for r in routen:
            p = getattr(r, "path", None)
            if isinstance(p, str):
                gesehen.add(p)
            unter = getattr(r, "routes", None)
            if unter is None:
                # fastapi.routing._IncludedRouter hält den eingebundenen
                # Router unter `original_router`.
                inner = getattr(r, "original_router", None)
                unter = getattr(inner, "routes", None) if inner is not None else None
            if unter:
                sammle(unter, gesehen)
        return gesehen

    pfade = sammle(gw.app.routes)
    for p in ("/api/v1/link/request", "/api/v1/link/request/{rid}",
              "/api/v1/link/confirm", "/api/v1/link/redeem"):
        pruefe(p in pfade, f"Pfad {p}")

    print("2. Prüfer verdrahtet")
    for name, fn in gw.smartrlink_ticket._PRUEFER.items():
        pruefe(fn is not None, f"Prüfer '{name}'")

    # --- Konto anlegen, so wie die Registrierung es täte -------------------
    con = gw.db()
    con.execute(
        "INSERT INTO users (id,email,name,tier,token_balance,subscription_until,"
        "added_date,entitlements,is_partner_contact,created_at,password_hash,role,"
        "totp_enabled,terms_version,terms_accepted_at,email_verified,container_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("u-probe", "probe@example.org", "Probe", "professional", 50000,
         "2099-01-01T00:00:00", gw.now_iso(), "[]", 0, gw.now_iso(),
         gw.ph.hash(PASSWORT), "user", 0, gw.TERMS_VERSION, gw.now_iso(), 1, "tnt-probe"))
    con.commit()
    con.close()

    ausweis = gw.make_jwt("u-probe", "probe@example.org", "user", ["pwd"])
    halb = gw.make_jwt("u-probe", "probe@example.org", "user", ["pwd", "mfa-pending"])
    c = TestClient(gw.app)
    kopf = {"Authorization": "Bearer " + ausweis}

    print("3. Antrag stellen (write → manueller Weg; read läuft seit E15 über Schritt 9)")
    antrag = {"client": "smartrchrome", "extension_id": EXT_ID, "version": "0.1.0",
              "purpose": "Preise vergleichen",
              "requested": {"access": "write", "duration": 600, "mode": "domains",
                            "allow": ["geizhals.de"], "tab_host": "geizhals.de",
                            "step_mode": "confirm_each"}}
    a = c.post("/api/v1/link/request", json=antrag,
               headers={**kopf, "Origin": EXT_ORIGIN})
    pruefe(a.status_code == 201, f"POST /request → 201 (war {a.status_code}: {a.text[:160]})")
    if a.status_code != 201:
        return 1
    daten = a.json()
    rid, verify_word, redeem_key = daten["rid"], daten["verify_word"], daten["redeem_key"]
    pruefe("ticket" not in daten, "die Antwort trägt kein Ticket")
    pruefe(bool(daten.get("verify_word_spoken")), "Buchstabierform ist dabei")

    print("4. Vorgang anzeigen (Freigabeseite)")
    g = c.get(f"/api/v1/link/request/{rid}", headers={**kopf, "Origin": WEB_ORIGIN})
    pruefe(g.status_code == 200, f"GET /request/{{rid}} → 200 (war {g.status_code})")
    sicht = g.json()
    pruefe(sicht.get("state") == "pending", "state == pending")
    pruefe("requested" in sicht and "preselect" in sicht, "requested und preselect getrennt")
    pruefe(sicht.get("attempts_left") == 3, f"attempts_left == 3 (war {sicht.get('attempts_left')})")
    pruefe("verify_word" not in sicht and "redeem_key" not in sicht and "ticket" not in sicht,
           "weder Kennwort noch Abholschlüssel noch Ticket im GET")

    print("5. Gegenproben (jede muss scheitern)")
    b = c.post("/api/v1/link/confirm",
               json={"rid": rid, "confirm": True, "origin": WEB_ORIGIN,
                     "verify_word": verify_word, "access": "read", "duration": 600,
                     "mode": "tab", "allow": ["geizhals.de"], "step_mode": "confirm_each",
                     "reauth": {"method": "password", "assertion": "falsch"}},
               headers={**kopf, "Origin": WEB_ORIGIN})
    pruefe(b.status_code == 403 and b.json().get("error") == "reauth_erforderlich",
           f"falsches Kontopasswort → 403 reauth_erforderlich (war {b.status_code} {b.text[:120]})")

    h = c.get(f"/api/v1/link/request/{rid}",
              headers={"Authorization": "Bearer " + halb, "Origin": WEB_ORIGIN})
    pruefe(h.status_code == 401,
           f"halbe Anmeldung (mfa-pending) → 401 (war {h.status_code} {h.text[:120]})")

    q = c.get(f"/api/v1/link/request/{rid}?token={ausweis}", headers={"Origin": WEB_ORIGIN})
    pruefe(q.status_code == 401,
           f"Ausweis in der Adresszeile → 401 (war {q.status_code})")

    f_ext = c.post("/api/v1/link/request",
                   json={**antrag, "extension_id": "a" * 32},
                   headers={**kopf, "Origin": "chrome-extension://" + "a" * 32})
    pruefe(f_ext.status_code == 403 and f_ext.json().get("error") == "extension_unknown",
           f"fremde Erweiterung → 403 extension_unknown (war {f_ext.status_code})")

    print("6. Freigeben mit richtigem Kennwort und richtigem Kontopasswort")
    f = c.post("/api/v1/link/confirm",
               json={"rid": rid, "confirm": True, "origin": WEB_ORIGIN,
                     "verify_word": verify_word, "access": "read", "duration": 600,
                     "mode": "tab", "allow": ["geizhals.de"], "step_mode": "confirm_each",
                     "reauth": {"method": "password", "assertion": PASSWORT}},
               headers={**kopf, "Origin": WEB_ORIGIN})
    pruefe(f.status_code == 200, f"POST /confirm → 200 (war {f.status_code}: {f.text[:200]})")
    pruefe(f.json().get("state") == "approved", "state == approved")
    pruefe("ticket" not in f.json(), "das Ticket steht NICHT in der confirm-Antwort")

    print("7. Ticket abholen — genau einmal")
    r1 = c.post("/api/v1/link/redeem", json={"rid": rid, "redeem_key": redeem_key},
                headers={**kopf, "Origin": EXT_ORIGIN})
    pruefe(r1.status_code == 200 and r1.json().get("ticket"),
           f"POST /redeem → Ticket (war {r1.status_code}: {r1.text[:160]})")
    r2 = c.post("/api/v1/link/redeem", json={"rid": rid, "redeem_key": redeem_key},
                headers={**kopf, "Origin": EXT_ORIGIN})
    pruefe(r2.status_code == 410, f"zweites /redeem → 410 (war {r2.status_code})")

    print("8. Das Ticket ist das, was der Relay erwartet (§4)")
    import jwt as _jwt
    anspruch = _jwt.decode(r1.json()["ticket"], os.environ["JWT_SECRET"],
                           algorithms=["HS256"], audience="smartr-connect")
    pruefe(anspruch["scope"] == "smartrlink-ticket", "scope == smartrlink-ticket")
    pruefe(anspruch["sub"] == "u-probe", "sub == Kontokennung")
    pruefe(anspruch["tnt"] == "tnt-probe", f"tnt aus container_id (war {anspruch.get('tnt')!r})")
    pruefe(anspruch["ext"] == EXT_ID, "ext == Kennung der Erweiterung")
    pruefe(anspruch["access"] == "read" and anspruch["duration"] == 600,
           "access und duration sind Server-Angaben")
    pruefe(anspruch["allow"] == ["geizhals.de"], f"allow nie leer (war {anspruch.get('allow')})")
    pruefe(anspruch["idle_timeout"] == 600, "idle_timeout == 600 (Inhaber 05.08.2026, RUNBOOK-E16)")
    pruefe(anspruch["exp"] - anspruch["iat"] == 60, "Lebensdauer 60 s")

    print("9. Sitzungsfreigabe (E15): Lesestufe wird sofort bewilligt")
    s = c.post("/api/v1/link/request",
               json={**antrag, "requested": {"access": "read", "duration": 900,
                                             "mode": "tab", "allow": [],
                                             "tab_host": "geizhals.de",
                                             "step_mode": "confirm_each"}},
               headers={**kopf, "Origin": EXT_ORIGIN})
    pruefe(s.status_code == 201 and s.json().get("state") == "approved",
           f"read-Antrag → sofort approved (war {s.status_code}: {s.text[:160]})")
    sd = s.json()
    pruefe("verify_word" not in sd and "confirm_url" not in sd,
           "kein Kennwort, keine Freigabeadresse in der Sitzungsfreigabe")
    gr = sd.get("granted") or {}
    # E16: Die gewünschte Dauer wird gewährt (bis MAX_DURATION). Unverhandelbar
    # bleiben Bereich (tab) und Schrittmodus (confirm_each).
    pruefe(gr.get("access") == "read" and gr.get("duration") == 900
           and gr.get("mode") == "tab" and gr.get("step_mode") == "confirm_each",
           f"Serverdeckel greift: read/Wunschdauer/tab/confirm_each (war {gr})")
    sr = c.post("/api/v1/link/redeem", json={"rid": sd["rid"], "redeem_key": sd["redeem_key"]},
                headers={**kopf, "Origin": EXT_ORIGIN})
    pruefe(sr.status_code == 200 and sr.json().get("ticket"),
           f"Sitzungsfreigabe-Ticket abholbar (war {sr.status_code}: {sr.text[:120]})")
    sh = c.post("/api/v1/link/request",
                json={**antrag, "requested": {"access": "read", "duration": 600,
                                              "mode": "tab", "allow": [],
                                              "tab_host": "geizhals.de",
                                              "step_mode": "confirm_each"}},
                headers={"Authorization": "Bearer " + halb, "Origin": EXT_ORIGIN})
    pruefe(sh.status_code == 401,
           f"halbe Anmeldung bekommt KEINE Sitzungsfreigabe (war {sh.status_code})")

    print("10. Sperrliste greift auch auf dem Sitzungsweg (Befund S1, 29.07.2026)")
    # Der Prüfer bekam bisher `allow` aus dem Antrag — bei mode `tab` leer.
    # Gewährt wurde danach [tab_host], den die Sperrliste nie gesehen hat.
    os.environ["LINK_BLOCKLIST"] = "bank.example"
    try:
        gesperrt = c.post(
            "/api/v1/link/request",
            json={**antrag, "requested": {"access": "write", "duration": 3600,
                                          "mode": "tab", "allow": [],
                                          "tab_host": "login.bank.example",
                                          "step_mode": "confirm_each"}},
            headers={**kopf, "Origin": EXT_ORIGIN})
    finally:
        os.environ.pop("LINK_BLOCKLIST", None)
    pruefe(gesperrt.status_code == 400
           and gesperrt.json().get("error") == "bereich_ungueltig",
           f"gesperrter Tab-Host → 400 bereich_ungueltig "
           f"(war {gesperrt.status_code}: {gesperrt.text[:160]})")
    pruefe(gesperrt.json().get("state") != "approved",
           "eine gesperrte Adresse bekommt keine Sitzungsfreigabe")

    print("11. Sitzung an den Agenten binden (G4) — Befunde S2, S3, S4")

    # Relay und Tenant-Engine werden ersetzt, nicht gerufen: Die Probe fährt
    # ohne Netz. Ersetzt werden nur die Modulfunktionen httpx.get/httpx.post —
    # der TestClient arbeitet mit httpx.Client und bleibt unberührt.
    gesendet = []

    class _Antwort:
        def __init__(self, koerper, status=200):
            self._koerper = koerper
            self.status_code = status
            self.headers = {"content-type": "application/json"}
            self.text = str(koerper)

        def json(self):
            return self._koerper

    import time as _zeit
    relay_lage = {"connected": True, "access": "write", "client": "smartrchrome",
                  "mode": "tab", "allow": ["geizhals.de"], "idle_timeout": 180,
                  "step_mode": "confirm_each",
                  "expires_at_epoch": _zeit.time() + 3600}

    def _relay_get(url, **kw):
        return _Antwort(dict(relay_lage))

    def _engine_post(url, **kw):
        nutzlast = kw.get("json") or {}
        gesendet.append(nutzlast)
        ctx = str(nutzlast.get("context_id") or "") or f"ctx-neu-{len(gesendet)}"
        return _Antwort({"context_id": ctx})

    gw.httpx.get = _relay_get
    gw.httpx.post = _engine_post

    def binde(koerper, ausweis_kopf=None):
        return c.post("/api/v1/link/session/bind", json=koerper,
                      headers=ausweis_kopf or kopf)

    def letzter_schein():
        return (gesendet[-1] if gesendet else {}).get("session") or {}

    b1 = binde({"code": "ABCD23"})
    pruefe(b1.status_code == 200,
           f"bind ohne Kontext → 200 (war {b1.status_code}: {b1.text[:160]})")
    ctx_eigen = str(b1.json().get("context_id") or "")
    pruefe(bool(ctx_eigen), "der Agent nennt einen Auftragskontext")

    schein = letzter_schein()
    # Der Agent liest `scope` als Objekt und daraus `domains`
    # (smartrbrowser.py, _kopfzeile). Eine Liste ergibt dort {} und damit
    # immer „nur der geöffnete Tab" — auch bei drei freigegebenen Adressen.
    pruefe(isinstance(schein.get("scope"), dict)
           and list((schein.get("scope") or {}).get("domains") or []) == ["geizhals.de"],
           f"scope ist ein Objekt mit domains (war {schein.get('scope')!r})")
    sekunden = int((schein.get("limits") or {}).get("sekunden") or 0)
    pruefe(sekunden >= 3500,
           f"Zeitdeckel passt zur Sitzungsdauer von 60 Minuten (war {sekunden})")

    # Nennt der Relay keinen Ablauf, darf der Schein nicht auf eine Notfrist
    # zusammenschnurren: Der Agent rechnet gegen diesen Deckel und bricht den
    # Auftrag hart ab, wenn er erreicht ist.
    relay_lage["expires_at_epoch"] = 0
    binde({"code": "ABCD23", "context_id": ctx_eigen})
    ohne_frist = int((letzter_schein().get("limits") or {}).get("sekunden") or 0)
    pruefe(ohne_frist >= 3500,
           f"ohne Ablaufangabe gilt die Höchstdauer, keine Notfrist (war {ohne_frist})")

    # Und nach oben: Ein Schein aus diesem Endpunkt lebt nie länger, als eine
    # Sitzung der Ticketschiene leben darf.
    relay_lage["expires_at_epoch"] = _zeit.time() + 86400
    binde({"code": "ABCD23", "context_id": ctx_eigen})
    lang = letzter_schein()
    pruefe(int((lang.get("limits") or {}).get("sekunden") or 0) <= 3600
           and int(lang.get("expires_at_epoch") or 0) <= int(_zeit.time()) + 3600,
           f"Höchstdauer gedeckelt (war {(lang.get('limits') or {}).get('sekunden')!r})")
    relay_lage["expires_at_epoch"] = _zeit.time() + 3600

    # S4: Der Schrittmodus kommt aus der Sitzung des Relays, nicht aus dem Rumpf.
    binde({"code": "ABCD23", "context_id": ctx_eigen, "step_mode": "auto"})
    pruefe(letzter_schein().get("step_mode") == "confirm_each",
           f"'auto' im Rumpf wird nicht übernommen (war {letzter_schein().get('step_mode')!r})")

    relay_lage["step_mode"] = "auto"
    binde({"code": "ABCD23", "context_id": ctx_eigen})
    pruefe(letzter_schein().get("step_mode") == "confirm_each",
           "gesperrter Automatikmodus bleibt gesperrt, auch wenn der Relay ihn meldet")
    os.environ["LINK_AUTO_MODUS"] = "1"
    try:
        binde({"code": "ABCD23", "context_id": ctx_eigen})
        pruefe(letzter_schein().get("step_mode") == "auto",
               "nach Freischaltung gilt der Schrittmodus des Relays")
    finally:
        os.environ["LINK_AUTO_MODUS"] = "0"
        relay_lage["step_mode"] = "confirm_each"

    # S2: Auf der geteilten Engine teilen sich mehrere Kunden den
    # Kontextnamensraum — eine geratene fremde Kennung darf nicht binden.
    con = gw.db()
    con.execute("INSERT INTO chat_contexts (context_id,user_id,agent_profile,title,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?)",
                ("ctx-fremd", "u-fremd", "smartr-browser", "Fremder Auftrag",
                 gw.now_iso(), gw.now_iso()))
    con.commit()
    con.close()
    vorher = len(gesendet)
    fremd = binde({"code": "ABCD23", "context_id": "ctx-fremd"})
    pruefe(fremd.status_code == 403,
           f"fremder Kontext → 403 (war {fremd.status_code}: {fremd.text[:160]})")
    pruefe(len(gesendet) == vorher, "an die Engine ging dabei nichts")
    unbekannt = binde({"code": "ABCD23", "context_id": "ctx-gibt-es-nicht"})
    pruefe(unbekannt.status_code == 403 and unbekannt.json() == fremd.json(),
           "fremd und unbekannt antworten wörtlich gleich (kein Ausprobieren)")

    # S3: Deckel auf bind. Jeder Aufruf ohne context_id legt in der geteilten
    # Engine einen neuen AgentContext an — ohne Grenze legt einer alle an.
    con = gw.db()
    con.execute(
        "INSERT INTO users (id,email,name,tier,token_balance,subscription_until,"
        "added_date,entitlements,is_partner_contact,created_at,password_hash,role,"
        "totp_enabled,terms_version,terms_accepted_at,email_verified,container_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("u-zwei", "zwei@example.org", "Zwei", "professional", 50000,
         "2099-01-01T00:00:00", gw.now_iso(), "[]", 0, gw.now_iso(),
         gw.ph.hash(PASSWORT), "user", 0, gw.TERMS_VERSION, gw.now_iso(), 1, "tnt-zwei"))
    con.commit()
    con.close()
    kopf_zwei = {"Authorization": "Bearer " + gw.make_jwt("u-zwei", "zwei@example.org",
                                                          "user", ["pwd"])}
    getroffen = 0
    for versuch in range(1, 61):
        if binde({"code": "ABCD23"}, kopf_zwei).status_code == 429:
            getroffen = versuch
            break
    pruefe(getroffen > 0, "die Bindung hat einen Deckel")
    pruefe(getroffen > 2,
           f"ein ehrlicher Nutzer (Sitzung + Verlängerung) läuft nicht hinein (Deckel bei {getroffen})")
    pruefe(binde({"code": "ABCD23", "context_id": ctx_eigen}).status_code == 200,
           "der Deckel gilt je Nutzer, nicht für alle")

    print("12. Der Endpunkt glaubt dem Relay nicht wörtlich (Befunde S5, S6)")

    # Ein dritter Nutzer, damit der Deckel aus Punkt 11 hier nicht dazwischen
    # kommt: Diese Prüfpunkte sollen an der Sache scheitern, nicht an 429.
    con = gw.db()
    con.execute(
        "INSERT INTO users (id,email,name,tier,token_balance,subscription_until,"
        "added_date,entitlements,is_partner_contact,created_at,password_hash,role,"
        "totp_enabled,terms_version,terms_accepted_at,email_verified,container_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("u-drei", "drei@example.org", "Drei", "professional", 50000,
         "2099-01-01T00:00:00", gw.now_iso(), "[]", 0, gw.now_iso(),
         gw.ph.hash(PASSWORT), "user", 0, gw.TERMS_VERSION, gw.now_iso(), 1, "tnt-drei"))
    con.commit()
    con.close()
    kopf_drei = {"Authorization": "Bearer " + gw.make_jwt("u-drei", "drei@example.org",
                                                          "user", ["pwd"])}

    def scheine():
        return [(s.get("session") or {}) for s in gesendet]

    # S5: Auf der ALTSCHIENE nennt der Desktop-Client Stufe und Bereich im
    # auth-Rahmen selbst und darf dort bis `full` gehen (server/app.py:
    # `access = hello.get("access") if hello.get("access") in RANK else "read"`).
    # `GET /browser/status/{code}` reicht das unverändert heraus. Ein Schein
    # daraus trüge `access: full` — das Werkzeug ließe über STUFEN_RANG["full"]
    # = 3 jeden Befehl durch, der Relay dazu `eval`, `terminal`, `maintenance`.
    # Der Clientdeckel `smartrchrome: write` (E13) stünde unberührt daneben und
    # hätte nichts gehalten.
    vorher = len(gesendet)
    relay_lage.update({"client": "smartrbrowser", "schiene": "alt",
                       "access": "full", "allow": [], "mode": ""})
    alt = binde({"code": "ABCD23"}, kopf_drei)
    pruefe(alt.status_code == 403,
           f"Sitzung eines fremden Clients → 403 (war {alt.status_code}: {alt.text[:160]})")
    pruefe(len(gesendet) == vorher, "aus einer Altschienen-Sitzung ging kein Schein an den Agenten")

    # Derselbe Riegel, aber allein tragend: Eine Altschienen-Sitzung, die sonst
    # harmlos aussieht (Lesestufe, eine Adresse), darf ebenfalls keinen Schein
    # bekommen. Ohne diesen Prüfpunkt bliebe die Prüfung oben auch dann grün,
    # wenn die Clientprüfung ersatzlos verschwände — abgewiesen hätte dort dann
    # die Stufen- oder die Bereichsprüfung, und niemand hätte es gemerkt.
    vorher = len(gesendet)
    relay_lage.update({"client": "smartrbrowser", "schiene": "alt",
                       "access": "read", "allow": ["geizhals.de"], "mode": "tab"})
    alt2 = binde({"code": "ABCD23"}, kopf_drei)
    pruefe(alt2.status_code == 403 and alt2.json().get("error") == "sitzung_fremder_client",
           f"auch die unauffällige Altschienen-Sitzung → 403 sitzung_fremder_client "
           f"(war {alt2.status_code}: {alt2.text[:160]})")
    pruefe(len(gesendet) == vorher, "auch dabei ging kein Schein an den Agenten")

    # Und auch unter dem richtigen Namen wird `full` nie zum Schein: Die Stufe
    # geht durch dieselbe Prüfung wie ein Antrag (pruefe_stufe, E13).
    vorher = len(gesendet)
    relay_lage.update({"client": "smartrchrome", "schiene": "ticket",
                       "access": "full", "allow": ["geizhals.de"], "mode": "tab"})
    voll = binde({"code": "ABCD23"}, kopf_drei)
    pruefe(voll.status_code in (400, 403),
           f"'full' vom Relay → abgewiesen (war {voll.status_code}: {voll.text[:160]})")
    pruefe(len(gesendet) == vorher, "auf 'full' ging kein Schein an den Agenten")
    pruefe(all(s.get("access") != "full" for s in scheine()),
           "in keinem einzigen Schein dieser Probe steht access=full")

    # S6: Ein leeres `allow` ist im Relay KEINE Beschränkung, sondern deren
    # Aufhebung (`if sess.allow:`) — und das Werkzeug liest dem Menschen dazu
    # „nur der geöffnete Tab" vor. Genau dieser Satz steht als E7 in ticket.py.
    vorher = len(gesendet)
    relay_lage.update({"access": "write", "allow": []})
    leer = binde({"code": "ABCD23"}, kopf_drei)
    pruefe(leer.status_code in (400, 403),
           f"Sitzung ohne Adresse → abgewiesen (war {leer.status_code}: {leer.text[:160]})")
    pruefe(len(gesendet) == vorher, "ohne Adresse ging kein Schein an den Agenten")
    pruefe(all(list((s.get("scope") or {}).get("domains") or []) for s in scheine()),
           "kein einziger Schein dieser Probe trägt einen leeren Geltungsbereich")

    # Dieselbe Regel für den dritten Wert aus derselben Meldung: Ein unbekannter
    # Bereichsmodus wird nicht durchgereicht, sondern auf die engere Lesart
    # geklemmt.
    relay_lage.update({"access": "write", "allow": ["geizhals.de"], "mode": "alles"})
    krumm = binde({"code": "ABCD23"}, kopf_drei)
    pruefe(krumm.status_code == 200
           and (letzter_schein().get("scope") or {}).get("mode") in ("tab", "domains"),
           f"unbekannter Bereichsmodus wird geklemmt "
           f"(war {(letzter_schein().get('scope') or {}).get('mode')!r})")
    relay_lage.update({"mode": "tab"})

    print("13. Eine misslungene Zuordnung wird nicht als Erfolg gemeldet (Befund S7)")

    # Scheitert die chat_contexts-Zeile, ist die Bindung unbrauchbar:
    # /chat/message prüft genau diese Tabelle (_own_context) und antwortet 403.
    # Ein `success: true` darauf schickte den Menschen in einen Auftrag, den es
    # für ihn nicht gibt — und der nächste Versuch mit derselben Kennung sagt
    # ihm „Zu dieser Kennung kenne ich keinen Auftrag", also: du bist schuld.
    echte_db = gw.db

    class _DbOhneEinfuegung:
        """Lässt alles durch außer der einen INSERT-Anweisung — damit die Probe
        genau den Serverfehler nachstellt und nicht den halben Endpunkt."""

        def __init__(self, echt):
            self._echt = echt
            self.geschlossen = False
            self.abgewiesen = False

        def execute(self, sql, *a, **k):
            if "chat_contexts" in sql and sql.lstrip().upper().startswith("INSERT"):
                self.abgewiesen = True
                raise sqlite3.OperationalError("database is locked")
            return self._echt.execute(sql, *a, **k)

        def commit(self):
            return self._echt.commit()

        def close(self):
            self.geschlossen = True
            return self._echt.close()

        def __getattr__(self, name):
            return getattr(self._echt, name)

    offene = []

    def _db_ohne_einfuegung():
        v = _DbOhneEinfuegung(echte_db())
        offene.append(v)
        return v

    gw.db = _db_ohne_einfuegung
    try:
        kaputt = binde({"code": "ABCD23"}, kopf_drei)
    finally:
        gw.db = echte_db
    pruefe(kaputt.status_code >= 500,
           f"misslungene Zuordnung → Serverfehler (war {kaputt.status_code}: {kaputt.text[:160]})")
    pruefe(kaputt.json().get("success") is False and not kaputt.json().get("context_id"),
           f"kein success:true auf einen Fehlschlag (war {kaputt.text[:160]})")
    # Geprüft wird genau die Verbindung, an der die Einfügung scheiterte — nicht
    # jede der Probe. Der Gateway-Bestand hält an anderer Stelle eine Verbindung
    # offen, wenn eine Abfrage wirft (`_a0_resolve_target`, `con = db()` ohne
    # `finally`); das ist ein Befund für den Bestand, nicht für diesen Block, und
    # darf diesen Prüfsatz nicht falsch rot färben.
    einfuegung = [v for v in offene if v.abgewiesen]
    pruefe(len(einfuegung) == 1 and einfuegung[0].geschlossen,
           "die SQLite-Verbindung der Einfügung wurde trotz Ausnahme geschlossen "
           f"(gefunden {len(einfuegung)}, geschlossen "
           f"{sum(1 for v in einfuegung if v.geschlossen)})")

    print("14. Kontopasswort-Sperre, Kontingent, passwortloses Konto (Befunde ④⑥⑧)")

    def _lege_konto_an(uid, tenant, *, abo="2099-01-01T00:00:00", guthaben=50000, pw=PASSWORT):
        con = gw.db()
        con.execute(
            "INSERT INTO users (id,email,name,tier,token_balance,subscription_until,"
            "added_date,entitlements,is_partner_contact,created_at,password_hash,role,"
            "totp_enabled,terms_version,terms_accepted_at,email_verified,container_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, uid + "@example.org", "K", "professional", guthaben, abo,
             gw.now_iso(), "[]", 0, gw.now_iso(),
             (gw.ph.hash(pw) if pw else None), "user", 0, gw.TERMS_VERSION,
             gw.now_iso(), 1, tenant))
        con.commit()
        con.close()

    def _kopf(uid):
        return {"Authorization": "Bearer " + gw.make_jwt(uid, uid + "@example.org", "user", ["pwd"])}

    def _confirm(uid, rid, verify_word, passwort):
        return c.post("/api/v1/link/confirm",
                      json={"rid": rid, "confirm": True, "origin": WEB_ORIGIN,
                            "verify_word": verify_word, "access": "read", "duration": 600,
                            "mode": "tab", "allow": ["geizhals.de"], "step_mode": "confirm_each",
                            "reauth": {"method": "password", "assertion": passwort}},
                      headers={**_kopf(uid), "Origin": WEB_ORIGIN})

    # ④ Nach LINK_REAUTH_MAX_FEHLER Fehlversuchen ist auch das RICHTIGE
    #    Kontopasswort gesperrt. Ein Fehlversuch verbrennt den Vorgang nicht,
    #    also bleibt derselbe rid nutzbar; nur das Kontopasswort wechselt.
    _lege_konto_an("u-sperre", "tnt-sperre")
    a_s = c.post("/api/v1/link/request", json=antrag,
                 headers={**_kopf("u-sperre"), "Origin": EXT_ORIGIN}).json()
    max_f = getattr(gw, "LINK_REAUTH_MAX_FEHLER", 10)
    alle_403 = all(_confirm("u-sperre", a_s["rid"], a_s["verify_word"], "falsch").status_code == 403
                   for _ in range(max_f))
    pruefe(alle_403, f"{max_f} Fehlversuche beim Kontopasswort ergeben je 403")
    gesperrt = _confirm("u-sperre", a_s["rid"], a_s["verify_word"], PASSWORT)
    pruefe(gesperrt.status_code == 403 and gesperrt.json().get("error") == "reauth_erforderlich",
           f"nach {max_f} Fehlversuchen ist auch das RICHTIGE Kontopasswort gesperrt "
           f"(war {gesperrt.status_code}: {gesperrt.text[:160]})")

    # ⑥ Ohne aktives Abo wird schon der Antrag mit 402 kontingent abgewiesen,
    #    nicht erst die Sitzung; dasselbe bei aufgebrauchtem Guthaben.
    _lege_konto_an("u-arm", "tnt-arm", abo="2000-01-01T00:00:00")
    arm = c.post("/api/v1/link/request", json=antrag,
                 headers={**_kopf("u-arm"), "Origin": EXT_ORIGIN})
    pruefe(arm.status_code == 402 and arm.json().get("error") == "kontingent",
           f"Konto ohne aktives Abo → 402 kontingent (war {arm.status_code}: {arm.text[:160]})")
    _lege_konto_an("u-leer", "tnt-leer", guthaben=0)
    leer_g = c.post("/api/v1/link/request", json=antrag,
                    headers={**_kopf("u-leer"), "Origin": EXT_ORIGIN})
    pruefe(leer_g.status_code == 402 and leer_g.json().get("error") == "kontingent",
           f"Konto ohne Guthaben → 402 kontingent (war {leer_g.status_code}: {leer_g.text[:160]})")

    # ⑧ Konto ganz ohne Kontopasswort (reiner Google-/Apple-Login): die Freigabe
    #    wird verweigert, nicht übersprungen. Braucht Abo+Guthaben, damit der
    #    Antrag überhaupt bis zum reauth kommt.
    _lege_konto_an("u-ohne-pw", "tnt-ohnepw", pw=None)
    a_o = c.post("/api/v1/link/request", json=antrag,
                 headers={**_kopf("u-ohne-pw"), "Origin": EXT_ORIGIN}).json()
    kein_pw = _confirm("u-ohne-pw", a_o["rid"], a_o["verify_word"], "irgendein-passwort")
    pruefe(kein_pw.status_code == 403 and kein_pw.json().get("error") == "reauth_erforderlich",
           f"Konto ohne Kontopasswort → keine Freigabe, 403 reauth_erforderlich "
           f"(war {kein_pw.status_code}: {kein_pw.text[:160]})")

    print()
    if fehler:
        print(f"NICHT BESTANDEN — {len(fehler)} Prüfpunkte offen:")
        for s in fehler:
            print("  -", s)
        return 1
    print("Startprobe bestanden: die Ticketausgabe hängt und trägt.")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(hauptlauf())

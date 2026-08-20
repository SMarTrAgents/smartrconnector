"""Stummel: Im Browser-Kontext ist dieses Werkzeug entzogen.

Datei-Grenze, keine Prompt-Bitte: usr/agents/<profil>/tools/ gewinnt gegen die
Plugin-Fassung (helpers/subagents.py:349-350). Ein eingeschleuster Auftrag von einer
fremden Webseite kann diese Datei nicht wegdiskutieren.

Warum auch das LESEN gesperrt ist: Die Stummelliste sperrte bis zum 29.07. nur das
Schreiben (memory_save/-delete/-forget). Damit blieb der Weg offen, den eine
eingeschleuste Seite tatsächlich gehen würde — Kundenerinnerungen laden und über
`type` in ein Formular auf ebendieser Seite schreiben. Ein Browser-Auftrag braucht
das Gedächtnis nicht; er hat den Auftrag des Nutzers und die Seite vor sich.
"""

from helpers.tool import Response, Tool


class MemoryLoad(Tool):
    async def execute(self, **kwargs) -> Response:
        return Response(
            message=(
                "Dieses Werkzeug ist im Browser-Auftrag nicht verfügbar. Browser-Kontexte "
                "lesen und schreiben kein Gedächtnis, führen keinen Code aus und rühren "
                "weder Dateien noch Mail an. Erledige den Auftrag mit `smartrbrowser` oder "
                "frage den Nutzer nach dem, was dir fehlt."
            ),
            break_loop=False,
        )

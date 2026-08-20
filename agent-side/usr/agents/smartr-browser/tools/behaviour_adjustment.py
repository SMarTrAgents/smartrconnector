"""Stummel: Im Browser-Kontext ist dieses Werkzeug entzogen.

Datei-Grenze, keine Prompt-Bitte: usr/agents/<profil>/tools/ gewinnt gegen die
Plugin-Fassung (helpers/subagents.py:349-350). Ein eingeschleuster Auftrag von einer
fremden Webseite kann diese Datei nicht wegdiskutieren.
"""

from helpers.tool import Response, Tool


class BehaviourAdjustment(Tool):
    async def execute(self, **kwargs) -> Response:
        return Response(
            message=(
                "Dieses Werkzeug ist im Browser-Auftrag nicht verfügbar. Browser-Kontexte "
                "dürfen weder Code ausführen noch Dateien, Gedächtnis oder Mail verändern. "
                "Erledige den Auftrag mit `smartrbrowser` oder berichte, was fehlt."
            ),
            break_loop=False,
        )

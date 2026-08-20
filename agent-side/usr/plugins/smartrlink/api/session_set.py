"""Nimmt den Sitzungsschein einer SMarTrChrome-Sitzung entgegen.

Aufrufer ist ausschließlich das Gateway (X-API-KEY == mcp_server_token). Der Schein
wird unter einem Schlüssel mit führendem Unterstrich abgelegt und deshalb nie in die
Chat-Datei geschrieben (helpers/persist_chat.py:146). Er stirbt mit dem Prozess —
so wie die Steuerbefugnis mit der Sitzung sterben soll.
"""

from agent import AgentContext, AgentContextType
from helpers.api import ApiHandler, Request, Response
from initialize import initialize_agent

PROFIL = "smartr-browser"
SCHLUESSEL = "_smartrlink"
PFLICHTFELDER = ("code", "bridge_token", "access", "expires_at_epoch")


class SessionSet(ApiHandler):

    @classmethod
    def requires_auth(cls) -> bool:
        return False

    @classmethod
    def requires_csrf(cls) -> bool:
        return False

    @classmethod
    def requires_api_key(cls) -> bool:
        return True

    async def process(self, input: dict, request: Request) -> dict | Response:
        schein = input.get("session")
        if not isinstance(schein, dict) or any(not schein.get(f) for f in PFLICHTFELDER):
            return Response('{"error": "unvollstaendiger_sitzungsschein"}',
                            status=400, mimetype="application/json")

        context_id = str(input.get("context_id") or "")
        if context_id:
            context = AgentContext.get(context_id)
            if not context:
                return Response('{"error": "context_not_found"}',
                                status=404, mimetype="application/json")
            if context.agent0.config.profile != PROFIL:
                # Ein Browserauftrag darf niemals in einem Alltagskontext landen.
                return Response('{"error": "falsches_profil"}',
                                status=409, mimetype="application/json")
        else:
            config = initialize_agent(override_settings={"agent_profile": PROFIL})
            context = AgentContext(config=config, type=AgentContextType.USER)
            AgentContext.use(context.id)
            context_id = context.id

        context.set_data(SCHLUESSEL, dict(schein))
        return {"context_id": context_id, "code": schein["code"]}

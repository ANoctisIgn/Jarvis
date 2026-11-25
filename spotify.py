"""Pont Jarvis ↔ Spotify via l'extension Spicetify `spicetify/jarvis-spotify.js`.

Jarvis ouvre un petit serveur WebSocket local (127.0.0.1 uniquement) ; l'extension, qui tourne
dans l'appli Spotify, s'y connecte et exécute les commandes (recherche, lecture, file d'attente...).
Pas besoin de compte Premium ni de clé d'API : c'est l'appli Spotify elle-même qui joue.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import sys
import threading

log = logging.getLogger("clap_listen")

PORT = 8765
# Une page web ouverte dans un navigateur peut aussi joindre 127.0.0.1 : on n'accepte que l'appli Spotify.
ORIGINES_AUTORISEES = ("https://xpui.app.spotify.com",)


class PontSpotify:
    def __init__(self, port: int = PORT) -> None:
        self.port = port
        self._loop = asyncio.new_event_loop()
        self._ws = None
        self._connecte = threading.Event()
        self._attentes: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        threading.Thread(target=self._demarrer, name="spotify", daemon=True).start()

    # --- serveur (thread asyncio dédié) ---

    def _demarrer(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serveur())
        except OSError as e:
            log.error("Pont Spotify : port %d indisponible (%s). Jarvis tourne déjà ?", self.port, e)

    async def _serveur(self) -> None:
        from websockets.asyncio.server import serve

        async with serve(self._gerer, "127.0.0.1", self.port):
            await asyncio.Future()

    async def _gerer(self, ws) -> None:
        origine = ws.request.headers.get("Origin")
        if origine not in ORIGINES_AUTORISEES:
            log.warning("Pont Spotify : connexion refusée (origine %r).", origine)
            await ws.close(code=1008, reason="origine refusée")
            return
        self._ws = ws
        self._connecte.set()
        log.info("Spotify connecté à Jarvis.")
        try:
            async for brut in ws:
                try:
                    data = json.loads(brut)
                except ValueError:
                    continue
                attente = self._attentes.pop(data.get("id"), None)
                if attente and not attente.done():
                    attente.set_result(data)
        finally:
            if self._ws is ws:
                self._ws = None
                self._connecte.clear()
                log.info("Spotify déconnecté de Jarvis.")

    async def _envoyer(self, message: dict, timeout: float) -> dict:
        attente = self._loop.create_future()
        self._attentes[message["id"]] = attente
        try:
            await self._ws.send(json.dumps(message))
            return await asyncio.wait_for(attente, timeout)
        finally:
            self._attentes.pop(message["id"], None)

    # --- API ---

    @property
    def connecte(self) -> bool:
        return self._connecte.is_set()

    def _lancer_spotify(self) -> None:
        if sys.platform == "win32":
            try:
                os.startfile("spotify:")  # noqa: S606 - ouvre l'appli Spotify
            except OSError as e:
                log.warning("Impossible de lancer Spotify : %s", e)

    def commande(
        self, action: str, timeout: float = 8.0, lancer: bool = True, attente: float = 0.0, **params
    ) -> dict:
        """Envoie une commande à l'extension et renvoie sa réponse ({"ok": bool, ...}).

        lancer : ouvre Spotify s'il n'est pas connecté.
        attente : sinon, secondes pendant lesquelles on attend que l'extension se reconnecte.
        """
        if not self._connecte.is_set() and not lancer and attente > 0:
            self._connecte.wait(attente)
        if not self._connecte.is_set():
            if not lancer:
                return {"ok": False, "erreur": "Spotify n'est pas connecté."}
            log.info("Spotify pas connecté, lancement de l'appli...")
            self._lancer_spotify()
            if not self._connecte.wait(25):
                return {
                    "ok": False,
                    "erreur": "Spotify ne répond pas. L'appli est-elle ouverte et l'extension "
                              "Jarvis installée avec Spicetify ?",
                }
        message = {"id": next(self._ids), "action": action, **params}
        try:
            futur = asyncio.run_coroutine_threadsafe(self._envoyer(message, timeout), self._loop)
            return futur.result(timeout + 2)
        except Exception as e:
            return {"ok": False, "erreur": f"Pas de réponse de Spotify ({e.__class__.__name__})."}

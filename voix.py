"""Voix de Jarvis : Edge-TTS (voix neuronales Microsoft, gratuites).

- parler(texte) : lit le texte phrase par phrase (la suivante se prépare pendant la lecture).
- dire_rapidement(texte) : petites phrases fixes, gardées en cache (lecture instantanée).
Voix réglable dans .env : EDGE_TTS_VOICE=fr-FR-HenriNeural (par défaut : Rémy).
En cas d'erreur 403 : python -m pip install -U edge-tts
"""

from __future__ import annotations

import glob
import hashlib
import logging
import os
import queue
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("clap_listen")

# Voix : "fr-FR-RemyMultilingualNeural" (homme), "fr-FR-HenriNeural" (homme), "fr-FR-DeniseNeural" (femme).
EDGE_TTS_VOICE_DEFAULT = "fr-FR-RemyMultilingualNeural"

# Empêche deux phrases de se chevaucher (bienvenue, réponse, rappel...).
verrou_parole = threading.Lock()
_tts_lock = verrou_parole
# Posé par la touche Jarvis : fait taire Jarvis, quoi qu'il dise (réponse, rappel, briefing...).
arret_parole = threading.Event()


# Vitesse de la voix (« +10% » = 10 % plus rapide, « +0% » = normale). Dans .env : EDGE_TTS_VITESSE=+20%
EDGE_TTS_VITESSE_DEFAULT = "+10%"
# False = on retire virgules, points-virgules, deux-points et tirets avant de parler : la voix Microsoft
# marque une pause à chacun, ce qui hache le débit. True = ponctuation conservée (plus de pauses).
PAUSES_PONCTUATION = False
# Petit silence entre deux morceaux lus (le silence au début et à la fin de chaque morceau est retiré).
PAUSE_ENTRE_PHRASES_S = 0.15

_FIN_DE_PHRASE = re.compile(r"(?<=[.!?…])\s+")


def _vitesse() -> str:
    v = (os.environ.get("EDGE_TTS_VITESSE") or EDGE_TTS_VITESSE_DEFAULT).strip()
    return v if re.fullmatch(r"[+-]\d{1,3}%", v) else "+0%"


# Abréviations développées avant de parler : la voix Rémy est « multilingue » et passe à l'anglais
# sur des textes courts ou ambigus (« Minuteur 15 s OK. »).
_ABREVIATIONS = [
    (re.compile(r"(\d)\s+s\b"), r"\1 secondes"),  # « 15 s » (pas « les années 90s »)
    (re.compile(r"(\d)\s*min\b"), r"\1 minutes"),
    (re.compile(r"(\d)\s*km/h\b"), r"\1 kilomètres heure"),
    (re.compile(r"\bOK\b|\bok\b|\bOk\b|\bokay\b", re.IGNORECASE), "d'accord"),
]


def preparer_texte(texte: str) -> str:
    """Développe les abréviations, puis retire la ponctuation qui provoque des pauses (sauf
    PAUSES_PONCTUATION). Garde « 13,5 » et « 13:30 »."""
    for motif, remplacement in _ABREVIATIONS:
        texte = motif.sub(remplacement, texte)
    if PAUSES_PONCTUATION:
        return texte
    texte = re.sub(r"(?<!\d)\s*[;:]\s*(?!\d)", " ", texte)
    texte = re.sub(r"\s+[—–-]\s+", " ", texte)
    texte = re.sub(r"(?<!\d),|,(?!\d)", " ", texte)
    return re.sub(r"\s{2,}", " ", texte).strip()


def _decouper_phrases(texte: str, taille_bloc: int = 400, mots_min_premier: int = 6) -> list[str]:
    """Première phrase seule (Jarvis commence à parler plus vite), puis le reste en gros blocs :
    chaque morceau séparé ajoute un petit blanc. Une première phrase trop courte (« Parfait. ») est
    regroupée avec la suite : seule, la voix Rémy risquerait de la lire en anglais."""
    phrases = [p.strip() for p in _FIN_DE_PHRASE.split(texte) if p.strip()]
    if len(phrases) <= 1:
        return phrases
    premier = phrases.pop(0)
    while phrases and len(premier.split()) < mots_min_premier:
        premier = f"{premier} {phrases.pop(0)}"
    blocs = [premier]
    courant = ""
    for phrase in phrases:
        if courant and len(courant) + len(phrase) > taille_bloc:
            blocs.append(courant)
            courant = phrase
        else:
            courant = f"{courant} {phrase}".strip()
    if courant:
        blocs.append(courant)
    return blocs


def _edge_tts_fichier(texte: str, voix: str) -> str | None:
    """Génère un MP3 Edge-TTS dans le dossier temporaire et renvoie son chemin (None si échec)."""
    import asyncio

    import edge_tts

    fd, chemin = tempfile.mkstemp(suffix=".mp3", prefix="jarvis_edge_")
    os.close(fd)
    try:
        async def generer() -> None:
            await edge_tts.Communicate(texte, voix, rate=_vitesse()).save(chemin)

        asyncio.run(generer())
        return chemin
    except Exception as e:
        if getattr(e, "status", None) == 403:
            log.error("Edge-TTS refusé (403) : mettez-le à jour avec  pip install -U edge-tts")
        else:
            log.error("Erreur avec Edge-TTS : %s", e)
        try:
            os.remove(chemin)
        except OSError:
            pass
        return None


def _son_sans_blancs(chemin: str, pygame):
    """Charge le MP3 et retire le silence du début et de la fin (Edge-TTS en met environ 0,6 s)."""
    son = pygame.mixer.Sound(chemin)
    echantillons = pygame.sndarray.array(son)
    taux = pygame.mixer.get_init()[0]
    mono = echantillons if echantillons.ndim == 1 else echantillons[:, 0]
    niveau = np.abs(mono.astype(np.int32))
    if not niveau.size:
        return None
    seuil = max(int(niveau.max() * 0.02), 60)
    parle = np.flatnonzero(niveau > seuil)
    if not parle.size:
        return None
    marge = int(taux * 0.04)
    debut, fin = max(0, parle[0] - marge), min(len(mono), parle[-1] + marge)
    return pygame.sndarray.make_sound(np.ascontiguousarray(echantillons[debut:fin]))


def _edge_tts_parler(texte: str, interruption: threading.Event | None = None) -> bool:
    """Lit `texte` : le morceau suivant est généré pendant que le précédent est lu, donc Jarvis
    commence à parler vite.

    Renvoie False si la lecture a été coupée (`interruption` posé, ex. « Jarvis »).
    """
    try:
        import edge_tts  # noqa: F401
        import pygame
    except ImportError as e:
        log.error("Edge-TTS indisponible (%s) : pip install -U edge-tts pygame", e)
        return True

    voix = (os.environ.get("EDGE_TTS_VOICE") or EDGE_TTS_VOICE_DEFAULT).strip()
    morceaux = _decouper_phrases(preparer_texte(texte))
    sons: queue.Queue = queue.Queue()
    fin = object()
    arret = threading.Event()

    def producteur() -> None:
        for morceau in morceaux:
            if arret.is_set():
                return
            chemin = _edge_tts_fichier(morceau, voix)
            if not chemin:
                continue
            try:
                son = _son_sans_blancs(chemin, pygame)
            except Exception as e:
                log.error("Lecture audio impossible : %s", e)
                son = None
            finally:
                try:
                    os.remove(chemin)
                except OSError:
                    pass
            if son is not None:
                sons.put(son)
        sons.put(fin)

    pygame.mixer.init(frequency=24000, size=-16, channels=1)  # format des MP3 d'Edge-TTS
    threading.Thread(target=producteur, name="tts", daemon=True).start()

    interrompu = False
    premier = True
    try:
        while not interrompu:
            try:
                son = sons.get(timeout=0.05)
            except queue.Empty:
                # Coupé pendant que le morceau suivant est encore en génération.
                interrompu = (interruption is not None and interruption.is_set()) or arret_parole.is_set()
                continue
            if son is fin:
                break
            if not premier:
                time.sleep(PAUSE_ENTRE_PHRASES_S)
            premier = False
            canal = son.play()
            horloge = pygame.time.Clock()
            while canal.get_busy():
                if (interruption is not None and interruption.is_set()) or arret_parole.is_set():
                    canal.stop()
                    interrompu = True
                    break
                horloge.tick(50)
        if interrompu:
            log.info("Jarvis interrompu.")
    finally:
        arret.set()
        pygame.mixer.quit()
    return not interrompu


def parler(texte: str, interruption: threading.Event | None = None) -> bool:
    """Point d'entrée unique pour faire parler Jarvis (Edge-TTS, voix Rémy par défaut).

    Renvoie False si la phrase a été coupée par `interruption`.
    """
    texte = (texte or "").strip()
    if not texte:
        return True
    with _tts_lock:
        arret_parole.clear()
        return _edge_tts_parler(texte, interruption)


_CACHE_VOIX = Path(__file__).resolve().parent / ".cache" / "voix"


def _phrase_en_cache(texte: str) -> str | None:
    """MP3 d'une phrase courante, généré une seule fois puis réutilisé (lecture instantanée)."""
    voix = (os.environ.get("EDGE_TTS_VOICE") or EDGE_TTS_VOICE_DEFAULT).strip()
    cle = hashlib.sha1(f"{voix}|{_vitesse()}|{texte}".encode()).hexdigest()[:16]
    chemin = _CACHE_VOIX / f"{cle}.mp3"
    if chemin.is_file():
        return str(chemin)
    temp = _edge_tts_fichier(texte, voix)
    if not temp:
        return None
    _CACHE_VOIX.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(temp, chemin)
    except OSError:
        return None
    return str(chemin)


def dire_rapidement(texte: str) -> None:
    """Pour les petites phrases fixes (« Oui, monsieur ? ») : lues depuis le cache, sans attente."""
    chemin = _phrase_en_cache(texte)
    if not chemin:
        parler(texte)
        return
    import pygame

    with _tts_lock:
        arret_parole.clear()
        pygame.mixer.init()
        try:
            pygame.mixer.music.load(chemin)
            pygame.mixer.music.play()
            horloge = pygame.time.Clock()
            while pygame.mixer.music.get_busy() and not arret_parole.is_set():
                horloge.tick(20)
            pygame.mixer.music.stop()
            pygame.mixer.music.unload()
        except Exception as e:
            log.error("Lecture audio impossible : %s", e)
        finally:
            pygame.mixer.quit()


def preparer_phrase(texte: str) -> None:
    """Génère à l'avance une phrase fixe (ex. « Oui, monsieur ? ») pour qu'elle soit instantanée."""
    _phrase_en_cache(texte)


def nettoyer_temporaires() -> None:
    """Supprime les MP3 de voix restés dans le dossier temp si Jarvis a été coupé en pleine phrase."""
    for vieux in glob.glob(os.path.join(tempfile.gettempdir(), "jarvis_edge_*.mp3")):
        try:
            os.remove(vieux)
        except OSError:
            pass

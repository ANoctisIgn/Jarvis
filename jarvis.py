#!/usr/bin/env python3
"""
Jarvis : assistant vocal façon Alexa, en français.

Lancer :  python jarvis.py      (Ctrl+C pour arrêter)

Organisation :
  jarvis.py       réglages, outils de l'IA, conversation vocale, démarrage
  ecoute.py       micro, mot de réveil « Jarvis », transcription (Whisper)
  voix.py         voix de Jarvis (Edge-TTS)
  spotify.py      pilotage de Spotify (extension Spicetify : spicetify/jarvis-spotify.js)
  rappels.py      minuteurs, alarmes, rappels
  agenda.py       agenda Google
  meteo.py        météo (Open-Meteo)
  briefing.py     briefing du jour
  claquements.py  détection du double claquement
  fenetres.py     ouverture de pages Chrome sur un écran donné

Les réglages ci-dessous se modifient directement ; certains peuvent aussi être mis dans .env.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import random
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pyautogui
from dotenv import load_dotenv
from groq import Groq

from voix import arret_parole, dire_rapidement, nettoyer_temporaires, parler, preparer_phrase, verrou_parole

load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("clap_listen")


# =============================================================================================
# RÉGLAGES
# =============================================================================================

# --- Intelligence artificielle (Groq) ---
# openai/gpt-oss-120b : le plus fiable pour choisir les outils et le plus économe en jetons (testé).
# Alternative : "qwen/qwen3.8-27b".
MODELE_IA = "openai/gpt-oss-120b"
# Nombre maximum d'appels d'outils enchaînés pour une seule demande.
MAX_ETAPES_OUTILS = 4
# Nombre maximum de messages récents envoyés à l'IA (limite le coût en jetons).
MAX_MESSAGES_HISTORIQUE = 12
# Proportion de réponses où Jarvis ajoute « monsieur » (0.33 = environ une sur trois).
PROBA_MONSIEUR = 0.33

# --- Conversation vocale (voir ecoute.py) ---
# Mot de réveil « Jarvis » tout court : modèle français Vosk (dossier). Absent = seul « Hey Jarvis » marche.
# Surchargeable dans .env : JARVIS_MODELE_VOSK=chemin\vers\modele
MODELE_VOSK = os.environ.get("JARVIS_MODELE_VOSK") or str(Path(__file__).resolve().parent / "modeles" / "vosk-fr")
# Sensibilité de « Hey Jarvis » à l'anglaise (0 à 1). Baisser (ex. 0.3) s'il ne vous entend pas
# assez, monter (ex. 0.7) s'il se réveille tout seul. Surchargeable dans .env : JARVIS_SEUIL_REVEIL=0.4
SEUIL_MOT_REVEIL = 0.5
# Jarvis se rendort si vous ne dites rien pendant N secondes (après « Oui, monsieur ? »).
DELAI_ECOUTE_S = 5.0
# Après une réponse, Jarvis écoute encore N secondes sans mot de réveil (comme Alexa). 0 = désactivé.
DUREE_SUIVI_S = 5.0
# Juste après que Jarvis a parlé, on ignore ce début d'écoute : la fin de sa voix résonne encore.
IGNORER_APRES_PAROLE_S = 0.3
# Touche Jarvis (marche même si une autre fenêtre est active) : fait taire Jarvis s'il parle, coupe
# une sonnerie, sinon réveille Jarvis comme si vous disiez « Jarvis ». Surchargeable dans .env :
# JARVIS_TOUCHE=ctrl+alt+j (exemples : ctrl+shift+espace, pause, f9). Vide = désactivée.
TOUCHE_JARVIS = os.environ.get("JARVIS_TOUCHE", "ctrl+alt+j")
# Met la musique en pause pendant qu'on parle à Jarvis (et la relance après).
PAUSE_MEDIA_AU_REVEIL = True
# Jarvis oublie la conversation après N secondes sans échange.
OUBLI_CONVERSATION_S = 180.0

# --- Musique ---
# Dossier des MP3 locaux (sous-dossiers = bibliothèques). Surchargeable dans .env : JARVIS_DOSSIER_MUSIQUE
DOSSIER_MUSIQUE = os.environ.get("JARVIS_DOSSIER_MUSIQUE") or r"E:\Musiques"
# Musique de bienvenue sur Spotify, au lancement de Jarvis et au double claquement.
# Pour une playlist : MUSIQUE_BIENVENUE = ("nom de la playlist", "playlist")
# Types possibles : "titre", "album", "artiste", "playlist".
MUSIQUE_BIENVENUE = ("Should I Stay or Should I Go The Clash", "titre")
MUSIQUE_AU_LANCEMENT = True
MUSIQUE_AU_DOUBLE_CLAP = True

# --- Agenda et briefing ---
# Annonce vocale N minutes avant chaque rendez-vous de l'agenda Google perso (0 = désactivé).
ANNONCE_AGENDA_MIN = 10
# Quand vous arrêtez une alarme le matin (avant midi) avec « Jarvis » : briefing du matin.
BRIEFING_APRES_REVEIL = True

# --- Double claquement (voir claquements.py) ---
# False = pas de détection des claquements (seule l'écoute vocale tourne).
CLAP_DETECTION_ENABLED = False
JARVIS_WELCOME_ENABLED = True
JARVIS_WELCOME_PHRASE = "Bienvenue à la maison, monsieur."
# Après la phrase de bienvenue : briefing (météo, agenda, rappels).
BRIEFING_AU_DOUBLE_CLAP = True
# Secondes entre le lancement de la musique et la phrase de bienvenue.
DELAI_AVANT_BIENVENUE_S = 1.0
# Pages ouvertes dans Chrome au double claquement : (adresse, numéro d'écran, 1 = le plus à gauche).
# Adresses surchargeables dans .env : JARVIS_PAGE_1 / JARVIS_PAGE_2 (anciens noms acceptés :
# CLAUDE_CODE_URL / BINANCE_BTC_URL). Liste vide = aucune page.
PAGES_DOUBLE_CLAP = [
    (os.environ.get("JARVIS_PAGE_1") or os.environ.get("CLAUDE_CODE_URL") or "https://calendar.google.com/", 1),
    (os.environ.get("JARVIS_PAGE_2") or os.environ.get("BINANCE_BTC_URL") or "https://drive.google.com/", 2),
]
# False = fenêtre normale centrée sur l'écran (taille : CHROME_WINDOW_WIDTH / CHROME_WINDOW_HEIGHT
# dans .env, 1400 x 900 par défaut) ; True = plein écran (F11).
PAGES_PLEIN_ECRAN = False
# True = profil Chrome temporaire par page (sans vos comptes) ; False = votre profil Chrome habituel.
CHROME_PROFILS_SEPARES = False

client = Groq()


# --- MUSIQUE LOCALE (MP3 dans DOSSIER_MUSIQUE) ET COMMANDES MULTIMÉDIA ---
_MOTS_IGNORES = {"jarvis", "joue", "jouer", "mets", "met", "lance", "la", "le", "les", "l", "de", "du", "des",
                 "un", "une", "musique", "chanson", "morceau", "titre", "mp3", "local", "locale"}


def _mots(texte: str) -> list[str]:
    """Mots en minuscules, sans accents ni ponctuation (pour comparer des noms de fichiers)."""
    import unicodedata

    sans_accents = unicodedata.normalize("NFD", texte.lower()).encode("ascii", "ignore").decode()
    return re.findall(r"[a-z0-9]+", sans_accents)


def _fichiers_mp3(dossier: str) -> list[str]:
    return [os.path.join(racine, f) for racine, _, fichiers in os.walk(dossier)
            for f in sorted(fichiers) if f.lower().endswith(".mp3")]


def _lancer_mp3(fichiers: list[str], nom: str) -> str:
    """Lance une liste de MP3 dans le lecteur par défaut de Windows (via une playlist .m3u)."""
    global _dernier_lancement_musique
    if not fichiers:
        return f"Erreur : aucun MP3 dans {nom}."
    if len(fichiers) == 1:
        os.startfile(fichiers[0])  # noqa: S606
    else:
        playlist = os.path.join(tempfile.gettempdir(), "jarvis_playlist.m3u")
        with open(playlist, "w", encoding="utf-8") as f:
            f.write("\n".join(fichiers) + "\n")
        os.startfile(playlist)  # noqa: S606
    _dernier_lancement_musique = time.monotonic()
    log.info("Musique locale : %s (%d fichier(s)).", nom, len(fichiers))
    return f"Lecture lancée : {nom}, {len(fichiers)} morceau(x)."


def _dossier_musique_absent() -> str | None:
    if not os.path.isdir(DOSSIER_MUSIQUE):
        return f"Erreur : dossier de musique introuvable ({DOSSIER_MUSIQUE})."
    return None


def jouer_musique_locale(commande: str) -> str:
    """Joue le MP3 local dont le nom ressemble le plus à la recherche."""
    if erreur := _dossier_musique_absent():
        return erreur
    cherches = [m for m in _mots(commande) if m not in _MOTS_IGNORES]
    if not cherches:
        return "Erreur : précisez le titre ou l'artiste."
    meilleur, meilleur_score = None, 0
    for chemin in _fichiers_mp3(DOSSIER_MUSIQUE):
        mots_fichier = set(_mots(os.path.splitext(os.path.basename(chemin))[0]))
        score = sum(1 for m in cherches if m in mots_fichier)
        if score > meilleur_score:
            meilleur, meilleur_score = chemin, score
    if meilleur is None:
        return "Aucun MP3 local ne correspond à cette recherche."
    return _lancer_mp3([meilleur], os.path.splitext(os.path.basename(meilleur))[0])


def jouer_toute_la_musique() -> str:
    """Joue tous les MP3 locaux."""
    if erreur := _dossier_musique_absent():
        return erreur
    return _lancer_mp3(_fichiers_mp3(DOSSIER_MUSIQUE), "toute votre musique locale")


def jouer_bibliotheque(nom_dossier: str) -> str:
    """Joue un sous-dossier de DOSSIER_MUSIQUE (nom approximatif accepté : « rap », « du moment »...)."""
    if erreur := _dossier_musique_absent():
        return erreur
    dossiers = sorted(d for d in os.listdir(DOSSIER_MUSIQUE) if os.path.isdir(os.path.join(DOSSIER_MUSIQUE, d)))
    cible = " ".join(_mots(nom_dossier))
    choix = next((d for d in dossiers if " ".join(_mots(d)) == cible), None) \
        or next((d for d in dossiers if cible and cible in " ".join(_mots(d))), None)
    if choix is None:
        return f"Erreur : bibliothèque introuvable. Disponibles : {', '.join(dossiers)}."
    return _lancer_mp3(_fichiers_mp3(os.path.join(DOSSIER_MUSIQUE, choix)), f"la bibliothèque {choix}")


def _touche_media(code: int) -> None:
    ctypes.windll.user32.keybd_event(code, 0, 0, 0)
    ctypes.windll.user32.keybd_event(code, 0, 2, 0)


def augmenter_son() -> None:
    """Augmente le volume du système (5 crans de 2 %)."""
    for _ in range(5):
        _touche_media(0xAF)
        time.sleep(0.02)


def baisser_son() -> None:
    """Baisse le volume du système (5 crans de 2 %)."""
    for _ in range(5):
        _touche_media(0xAE)
        time.sleep(0.02)


def couper_son() -> None:
    """Coupe ou remet le son du système (touche muet)."""
    _touche_media(0xAD)


# --- CONTRÔLE DU PC (voir pc.py) ---
# Verrouiller / mettre en veille se font APRÈS la réponse de Jarvis, sinon on ne l'entendrait pas.
_actions_apres_reponse: list = []
# Posé quand la touche Jarvis a fait taire Jarvis (il se rendort au lieu d'écouter une nouvelle demande).
_arret_par_touche = threading.Event()


def ouvrir_application(nom: str) -> str:
    """Ouvre un logiciel du menu Démarrer."""
    import pc

    return pc.ouvrir_application(nom)


def fermer_application(nom: str) -> str:
    """Ferme proprement les fenêtres d'un logiciel."""
    import pc

    return pc.fermer_application(nom)


def ouvrir_site(adresse: str) -> str:
    """Ouvre un site web dans le navigateur par défaut."""
    import pc

    return pc.ouvrir_site(adresse)


def verrouiller_pc() -> str:
    """Verrouille la session Windows (juste après la réponse)."""
    import pc

    _actions_apres_reponse.append(pc.verrouiller)
    return "Le PC sera verrouillé dès que tu auras fini de répondre."


def mettre_pc_en_veille() -> str:
    """Met le PC en veille (juste après la réponse)."""
    import pc

    _actions_apres_reponse.append(pc.mettre_en_veille)
    return "Le PC sera mis en veille dès que tu auras fini de répondre."


def capture_ecran() -> str:
    """Capture d'écran enregistrée dans Images > Captures Jarvis."""
    import pc

    chemin = pc.capture_ecran()
    return f"Capture enregistrée dans le dossier Images, Captures Jarvis ({os.path.basename(chemin)})."


def afficher_bureau() -> str:
    """Réduit toutes les fenêtres (Windows + D)."""
    import pc

    pc.afficher_bureau()
    return "Bureau affiché."


def _spotify_connecte() -> bool:
    return pont_spotify is not None and pont_spotify.connecte


def arreter_musique() -> None:
    """Met la musique en pause (Spotify si connecté, sinon touche multimédia)."""
    if _spotify_connecte():
        pont_spotify.commande("pause", lancer=False)
    else:
        _touche_media(0xB3)


def reprendre_musique() -> None:
    """Reprend la lecture de la musique."""
    if _spotify_connecte():
        pont_spotify.commande("reprendre", lancer=False)
    else:
        _touche_media(0xB3)


def musique_suivante() -> str | None:
    """Passe au morceau de musique suivant."""
    if _spotify_connecte():
        return _decrire_reponse_spotify(pont_spotify.commande("suivant", lancer=False))
    _touche_media(0xB0)
    return None


def musique_precedente() -> str | None:
    """Revient au morceau de musique précédent."""
    if _spotify_connecte():
        return _decrire_reponse_spotify(pont_spotify.commande("precedent", lancer=False))
    _touche_media(0xB1)
    return None


# --- SPOTIFY (via l'extension Spicetify, voir spotify.py) ---
pont_spotify = None  # créé au démarrage de la boucle vocale


def _decrire_reponse_spotify(r: dict) -> str:
    """Transforme la réponse de l'extension en phrase pour l'IA."""
    if not r.get("ok"):
        return f"Erreur Spotify : {r.get('erreur', 'inconnue')}"
    for cle, verbe in (("joue", "Lecture lancée sur Spotify"), ("ajoute", "Ajouté à la file d'attente")):
        if r.get(cle):
            el = r[cle]
            return f"{verbe} : {el.get('nom', '')}" + (f" de {el['artiste']}" if el.get("artiste") else "") + "."
    m = r.get("morceau")
    if m:
        etat = "en lecture" if r.get("lecture", True) else "en pause"
        return f"Morceau actuel ({etat}) : {m.get('nom', '')} de {m.get('artiste', '')}, album {m.get('album', '')}."
    return "C'est fait."


def spotify_jouer(recherche: str, type: str = "titre") -> str:
    """Joue un titre, un album, un artiste ou une playlist sur Spotify."""
    global _dernier_lancement_musique
    _dernier_lancement_musique = time.monotonic()
    type_ = (type or "titre").lower()
    if type_ not in ("titre", "album", "artiste", "playlist"):
        type_ = "titre"
    return _decrire_reponse_spotify(
        pont_spotify.commande("jouer", recherche=recherche, type=type_, timeout=12)
    )


def jouer_musique_bienvenue() -> None:
    """Lance MUSIQUE_BIENVENUE sur Spotify (ouvre Spotify si besoin)."""
    for _ in range(50):  # au double claquement, le pont Spotify peut ne pas être encore prêt
        if pont_spotify is not None:
            break
        time.sleep(0.1)
    if pont_spotify is None:
        log.warning("Musique de bienvenue : pont Spotify pas encore démarré.")
        return
    global _dernier_lancement_musique
    recherche, type_ = MUSIQUE_BIENVENUE
    _dernier_lancement_musique = time.monotonic()
    log.info("Musique de bienvenue : %s", _decrire_reponse_spotify(
        pont_spotify.commande("jouer", recherche=recherche, type=type_, timeout=12)
    ))


def spotify_titres_likes(aleatoire: bool = False) -> str:
    """Joue les titres likés de l'utilisateur."""
    global _dernier_lancement_musique
    _dernier_lancement_musique = time.monotonic()
    return _decrire_reponse_spotify(pont_spotify.commande("likes", aleatoire=bool(aleatoire), timeout=12))


def spotify_file_attente(recherche: str) -> str:
    """Ajoute un titre à la file d'attente Spotify."""
    return _decrire_reponse_spotify(pont_spotify.commande("file", recherche=recherche, timeout=12))


def spotify_en_cours() -> str:
    """Dit quel morceau joue sur Spotify."""
    r = pont_spotify.commande("etat", lancer=False, attente=8)
    if not r.get("ok") and not pont_spotify.connecte:
        return "Spotify n'est pas ouvert, rien ne joue."
    if r.get("ok") and not r.get("morceau"):
        return "Rien ne joue sur Spotify."
    return _decrire_reponse_spotify(r)


# --- MINUTEURS, ALARMES, RAPPELS (voir rappels.py) ---
FICHIER_RAPPELS = Path(__file__).resolve().parent / "donnees" / "rappels.json"
gestion_rappels = None  # créé au démarrage de la boucle vocale


def _en_secondes(valeur) -> float:
    try:
        return float(valeur or 0)
    except (TypeError, ValueError):
        return 0.0


def _date_heure(texte: str) -> datetime | None:
    """« 2026-11-10T07:30 » ou « 07:30 ». Une heure seule déjà passée = demain."""
    texte = (texte or "").strip().replace(" ", "T", 1)
    if not texte:
        return None
    maintenant = datetime.now()
    try:
        if "T" in texte or "-" in texte:
            quand = datetime.fromisoformat(texte)
        else:
            h, m = (texte.replace("h", ":").split(":") + ["0"])[:2]
            quand = maintenant.replace(hour=int(h), minute=int(m or 0), second=0, microsecond=0)
    except ValueError:
        return None
    if quand <= maintenant and quand.date() == maintenant.date():
        quand += timedelta(days=1)
    return quand


def creer_minuteur(duree_secondes: int, nom: str = "") -> str:
    """Lance un minuteur."""
    return gestion_rappels.ajouter_minuteur(_en_secondes(duree_secondes), nom or "")


def creer_alarme(date_heure: str, message: str = "") -> str:
    """Règle une alarme (réveil) à une date/heure."""
    quand = _date_heure(date_heure)
    if quand is None:
        return "Erreur : date_heure invalide, format attendu AAAA-MM-JJTHH:MM."
    if quand <= datetime.now():
        return "Erreur : cette heure est déjà passée."
    return gestion_rappels.ajouter_alarme(quand, message or "")


def creer_rappel(message: str, date_heure: str = "", dans_secondes: int = 0) -> str:
    """Programme un rappel vocal, à une heure précise ou dans un certain délai."""
    if _en_secondes(dans_secondes) > 0:
        quand = datetime.now() + timedelta(seconds=_en_secondes(dans_secondes))
    else:
        quand = _date_heure(date_heure)
    if quand is None:
        return "Erreur : il faut date_heure (AAAA-MM-JJTHH:MM) ou dans_secondes."
    if quand <= datetime.now():
        return "Erreur : ce moment est déjà passé."
    return gestion_rappels.ajouter_rappel(quand, message or "Rappel")


def lister_minuteurs_et_rappels() -> str:
    """Liste les minuteurs, alarmes et rappels en cours (avec le temps restant)."""
    return gestion_rappels.lister()


def annuler_minuteur_ou_rappel(cible: str) -> str:
    """Annule par numéro, par type (« minuteur », « alarme », « rappel ») ou « tout »."""
    return gestion_rappels.annuler(cible)


# --- AGENDA GOOGLE (voir agenda.py) ---
gestion_agenda = None  # créé au démarrage de la boucle vocale


def _agenda_absent() -> str:
    return "Erreur : l'agenda Google n'est pas configuré (lancez une fois « python agenda.py »)."


def agenda_evenements(debut: str, fin: str = "") -> str:
    """Liste les événements de l'agenda entre deux dates."""
    return gestion_agenda.evenements(debut, fin) if gestion_agenda else _agenda_absent()


def agenda_ajouter(titre: str, debut: str, fin: str = "", lieu: str = "", journee_entiere: bool = False) -> str:
    """Ajoute un événement dans l'agenda principal."""
    if not gestion_agenda:
        return _agenda_absent()
    return gestion_agenda.ajouter(titre, debut, fin, lieu, bool(journee_entiere))


def agenda_supprimer(numero: int, confirme: bool = False) -> str:
    """Supprime un événement (après confirmation de l'utilisateur)."""
    return gestion_agenda.supprimer(numero, bool(confirme)) if gestion_agenda else _agenda_absent()


def agenda_modifier(numero: int, titre: str = "", debut: str = "", fin: str = "", lieu: str = "",
                    confirme: bool = False) -> str:
    """Modifie un événement (après confirmation de l'utilisateur)."""
    if not gestion_agenda:
        return _agenda_absent()
    return gestion_agenda.modifier(numero, titre, debut, fin, lieu, bool(confirme))


# --- MÉTÉO ET BRIEFING (voir meteo.py et briefing.py) ---
CONSIGNE_BRIEFING = (
    "BRIEFING : fais un briefing oral de 3 à 5 phrases courtes, c'est la seule exception à la règle de "
    "brièveté. Dans l'ordre : la météo avec un conseil pratique si utile (parapluie, veste), puis les "
    "rendez-vous à venir en les regroupant (nomme les cours par leur matière en deux ou trois mots, sans "
    "codes ni salles ni numéros), puis les rappels. Ne parle PAS d'une catégorie vide (pas de « aucun "
    "rappel »). Ignore les jours fériés sauf s'ils changent le programme. Décris la météo exactement "
    "comme indiqué (« couvert » n'est pas « il pleut »). N'invente rien. Termine par une petite touche d'humour."
)


def meteo_previsions(ville: str = "", date: str = "") -> str:
    """Météo actuelle et prévisions (ville vide = ville de l'utilisateur)."""
    from meteo import meteo_texte

    return meteo_texte(ville or "", date or "")


def briefing_du_jour() -> str:
    """Faits du jour (météo, agenda, rappels) pour un briefing."""
    from briefing import faits_du_jour

    return faits_du_jour(gestion_agenda, gestion_rappels)


def rediger_briefing(contexte: str) -> str:
    """Briefing complet rédigé par l'IA, pour les moments où il n'y a pas de conversation
    (double claquement, alarme du matin). Si l'IA ne répond pas, on lit un résumé simple."""
    from briefing import faits_du_jour

    faits = faits_du_jour(gestion_agenda, gestion_rappels)
    systeme = (historique_messages[0]["content"] + " " + CONSIGNE_BRIEFING
               + f" Contexte : {contexte}. Ne dis pas bonjour ni bienvenue, ça a déjà été dit.")
    try:
        extra = {"reasoning_effort": "low"} if MODELE_IA.startswith("openai/gpt-oss") else {}
        r = client.chat.completions.create(
            model=MODELE_IA, max_tokens=400,
            messages=[{"role": "system", "content": systeme}, {"role": "user", "content": faits}],
            **extra,
        )
        texte = _nettoyer_pour_voix(_BALISES_REFLEXION.sub("", r.choices[0].message.content or ""))
        if texte:
            return texte
    except Exception as e:
        log.warning("Briefing : l'IA ne répond pas (%s), résumé simple.", e)
    from meteo import meteo_texte

    return meteo_texte().replace("Prévisions ", "")


def _contexte_temps() -> str:
    from rappels import JOURS, MOIS

    m = datetime.now()
    # Repères de dates déjà calculés : l'IA se trompe souvent d'un jour quand elle calcule elle-même.
    jours = []
    for i in range(1, 8):
        d = m.date() + timedelta(days=i)
        nom = "demain" if i == 1 else JOURS[d.weekday()] + (" prochain" if d.weekday() == m.weekday() else "")
        jours.append(f"{nom} = {d.isoformat()}")
    samedi = m.date() + timedelta(days=(5 - m.weekday()) % 7)
    lundi_prochain = m.date() + timedelta(days=7 - m.weekday())
    return (f"Nous sommes le {JOURS[m.weekday()]} {m.day} {MOIS[m.month - 1]} {m.year}, "
            f"il est {m:%H:%M} (format ISO : {m:%Y-%m-%dT%H:%M}). "
            f"Repères de dates (utilise-les tels quels) : {' ; '.join(jours)} ; "
            f"ce week-end = du {samedi.isoformat()} au {(samedi + timedelta(days=1)).isoformat()} ; "
            f"semaine prochaine = du {lundi_prochain.isoformat()} au {(lundi_prochain + timedelta(days=6)).isoformat()}.")


# --- INITIALISATION DE L'IA (GROQ) ---
historique_messages = [
    {
        "role": "system",
        "content": (
            "Tu es Jarvis, un assistant vocal efficace, à l'humour pince-sans-rire de majordome britannique. "
            "TU DOIS IMPÉRATIVEMENT RÉPONDRE ET PARLER EN FRANÇAIS EXCLUSIVEMENT. "
            "Vouvoie l'utilisateur. N'écris jamais « monsieur » : il est ajouté automatiquement. "
            "SOIS BREF : une seule phrase la plupart du temps, deux au maximum, sauf si on te demande "
            "explicitement une explication détaillée. Va droit au but, sans formule de politesse inutile. "
            "Mais réponds TOUJOURS par une vraie phrase française complète, avec un verbe et au moins cinq "
            "mots, jamais un mot seul ni un style télégraphique (pas « Parfait. », « OK. », « Minuteur lancé. » "
            "mais « C'est parfait, je m'en occupe. », « Le minuteur de dix minutes est lancé. ») : la voix "
            "de synthèse lit en anglais les réponses trop courtes. N'abrège pas (« 15 secondes », pas « 15 s »). "
            "Glisse souvent une petite touche d'humour, légère et rapide (quelques mots, pas une blague entière), "
            "sans en faire trop. "
            "Utilise un outil quand on te demande une action ou une information que seul un outil connaît. "
            "Si l'utilisateur discute simplement, réponds sans outil. "
            "Ne dis JAMAIS qu'une action est faite si l'outil a répondu « Erreur » ou « CONFIRMATION REQUISE ». "
            "Ton texte est lu à voix haute : pas de markdown, pas de listes à puces, pas d'emoji, pas de parenthèses. "
            "La transcription vocale peut contenir de petites erreurs : devine le sens le plus probable."
        )
    }
]

try:
    _BIBLIOTHEQUES_LOCALES = ", ".join(sorted(
        d for d in os.listdir(DOSSIER_MUSIQUE) if os.path.isdir(os.path.join(DOSSIER_MUSIQUE, d))
    )) or "aucune"
except OSError:
    _BIBLIOTHEQUES_LOCALES = "aucune"

# --- Thèmes : on n'envoie à l'IA que les outils et consignes utiles (limite de jetons Groq) ---
THEMES = {
    "musique": {
        "mots": r"musique|chanson|morceau|titre|album|playlist|artiste|spotify|mets |mets-|joue|lance|écoute|"
                r"pause|reprend|suivant|précédent|passe|zappe|mp3|liké|favori|son de|file d'attente|après ça",
        "outils": ["jouer_bibliotheque", "jouer_toute_la_musique", "jouer_musique_locale", "arreter_musique",
                   "reprendre_musique", "musique_suivante", "musique_precedente", "spotify_jouer",
                   "spotify_titres_likes", "spotify_file_attente", "spotify_en_cours"],
        "consignes": "MUSIQUE : par défaut tout se joue sur Spotify (spotify_jouer, en choisissant bien le type : "
                     "titre, album, artiste ou playlist). N'utilise les outils de MP3 locaux que si l'utilisateur "
                     "parle explicitement de ses fichiers, de sa musique locale, de ses MP3 ou d'une de ses "
                     f"bibliothèques locales ({_BIBLIOTHEQUES_LOCALES}) : « ma bibliothèque rap » = "
                     "jouer_bibliotheque. Pour « c'est quoi ce morceau », "
                     "appelle spotify_en_cours. Après avoir lancé la musique, réponds par une courte phrase "
                     "française complète, ex. « C'est parti, bonne écoute. » ou « Je lance Get Lucky de Daft Punk. »",
    },
    "pc": {
        "mots": r"ouvr|ferm|quitt|lance|démarre|verrouill|veille|capture|screenshot|bureau|appli|logiciel|"
                r"\bsite\b|navigateur|\.com|\.fr|youtube|netflix|discord|steam|word|chrome|teams|calculatrice",
        "outils": ["ouvrir_application", "fermer_application", "ouvrir_site", "verrouiller_pc",
                   "mettre_pc_en_veille", "capture_ecran", "afficher_bureau"],
        "consignes": "PC : ouvrir_application pour lancer un logiciel installé par son nom (Discord, Steam, Word, "
                     "Chrome...) ; ouvrir_site pour un site web dont tu déduis l'adresse (youtube.com, netflix.com...), "
                     "et pour une recherche YouTube utilise https://www.youtube.com/results?search_query=... ; "
                     "fermer_application pour fermer un logiciel. La musique reste sur Spotify, pas sur YouTube, "
                     "sauf si l'utilisateur dit YouTube. Réponds en quelques mots.",
    },
    "volume": {
        "mots": r"volume|\bson\b|plus fort|moins fort|monte|baisse|muet|mute",
        "outils": ["augmenter_son", "baisser_son", "couper_son"],
        "consignes": "",
    },
    "rappels": {
        "mots": r"minuteur|timer|alarme|réveil|réveille|rappel|rappelle|compte à rebours|dans \d+|"
                r"\d+\s*(?:secondes?|minutes?|heures?)\b|"
                r"il reste combien|temps reste",
        "outils": ["creer_minuteur", "creer_alarme", "creer_rappel", "lister_minuteurs_et_rappels",
                   "annuler_minuteur_ou_rappel"],
        "consignes": "MINUTEURS, ALARMES, RAPPELS : utilise toujours les outils dédiés ; pour toute question sur "
                     "ceux en cours (temps restant...), appelle lister_minuteurs_et_rappels, jamais de mémoire. "
                     "date_heure au format AAAA-MM-JJTHH:MM calculé depuis la date actuelle ; pour un délai "
                     "(« dans 20 minutes ») utilise dans_secondes ou duree_secondes. Pour annuler quand il y en a "
                     "plusieurs, liste-les d'abord.",
    },
    "meteo": {
        "mots": r"météo|meteo|quel temps|fait-il|fait il|il fait|pleuv|pluie|parapluie|soleil|neige|"
                r"température|degré|froid|chaud|\bvent\b|orage|nuage|couvert",
        "outils": ["meteo_previsions"],
        "consignes": "MÉTÉO : appelle meteo_previsions (ville vide = ville de l'utilisateur ; date "
                     "AAAA-MM-JJ pour un autre jour, 7 jours maximum). Réponds en une ou deux phrases, avec un "
                     "conseil pratique si utile (parapluie, veste).",
    },
    "briefing": {
        "mots": r"briefing|bonjour|bon matin|programme|ma journée|résumé|quoi de prévu|"
                r"je suis rentré|de retour|à la maison",
        "outils": ["briefing_du_jour"],
        "consignes": "Si l'utilisateur dit bonjour, demande son briefing, son programme ou un résumé de sa "
                     "journée, ou annonce qu'il est rentré : appelle briefing_du_jour. " + CONSIGNE_BRIEFING,
    },
    "agenda": {
        "mots": r"agenda|calendrier|rendez|rdv|réunion|planning|programme|prévu|libre|dispo|occupé|"
                r"aujourd'hui|demain|semaine|week-end|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|"
                r"ajoute|supprime|efface|décale|déplace|repousse|avance|événement|anniversaire|"
                r"\boui\b|vas-y|confirme",
        "outils": ["agenda_evenements", "agenda_ajouter", "agenda_supprimer", "agenda_modifier"],
        "consignes": "AGENDA : pour toute question sur l'emploi du temps, les rendez-vous ou les disponibilités, "
                     "appelle agenda_evenements sur la bonne période, jamais de mémoire. Pour une disponibilité, "
                     "interroge au moins l'heure concernée (ex. de 14:00 à 15:00) ; un événement « toute la "
                     "journée » (anniversaire, férié) ne rend pas indisponible. Pour supprimer ou modifier : liste "
                     "la période pour obtenir le numéro, appelle l'outil SANS confirme, puis pose la question ; "
                     "au oui de l'utilisateur, rappelle l'outil avec confirme=true. Résume sans lire les numéros.",
    },
}
_themes_actifs: set[str] = set(THEMES)


def _choisir_themes() -> set[str]:
    """Thèmes évoqués dans les 3 dernières demandes. Aucun reconnu = tous (par sécurité)."""
    textes = [m["content"] for m in historique_messages[1:] if m.get("role") == "user"][-3:]
    bas = " ".join(textes).lower()
    trouves = {nom for nom, t in THEMES.items() if re.search(t["mots"], bas)}
    return trouves or set(THEMES)


def _outils_actifs() -> list[dict]:
    noms = {n for nom in _themes_actifs for n in THEMES[nom]["outils"]}
    return [o for o in outils_jarvis_groq if o["function"]["name"] in noms]


outils_jarvis_groq = [
    {"type": "function", "function": {"name": "jouer_bibliotheque", "description": "Joue un dossier de MP3 locaux (seulement si on parle de fichiers locaux)", "parameters": {"type": "object", "properties": {"nom_dossier": {"type": "string"}}, "required": ["nom_dossier"]}}},
    {"type": "function", "function": {"name": "jouer_toute_la_musique", "description": "Joue tous les MP3 locaux (seulement si on parle de fichiers locaux)", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "jouer_musique_locale", "description": "Joue un MP3 local (seulement si on parle de fichiers locaux)", "parameters": {"type": "object", "properties": {"commande": {"type": "string"}}, "required": ["commande"]}}},
    {"type": "function", "function": {"name": "arreter_musique", "description": "Met la musique en pause", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "reprendre_musique", "description": "Reprend la lecture là où elle a été mise en pause (« reprends », « relance la musique ») ; ne lance rien de nouveau", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "musique_suivante", "description": "Passe à la musique suivante", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "musique_precedente", "description": "Revient à la musique précédente", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "spotify_jouer", "description": "Joue sur Spotify un titre, un album, un artiste ou une playlist (publique ou de l'utilisateur). Outil par défaut pour toute demande de musique.", "parameters": {"type": "object", "properties": {"recherche": {"type": "string", "description": "Ex. « Get Lucky Daft Punk », « Discovery », « Stromae », « Top 50 France », « rap français »"}, "type": {"type": "string", "enum": ["titre", "album", "artiste", "playlist"]}}, "required": ["recherche", "type"]}}},
    {"type": "function", "function": {"name": "spotify_titres_likes", "description": "Lance la playlist « Titres likés » de l'utilisateur sur Spotify (« mes titres likés », « mes favoris », « ma musique »), éventuellement en aléatoire", "parameters": {"type": "object", "properties": {"aleatoire": {"type": "boolean"}}}}},
    {"type": "function", "function": {"name": "spotify_file_attente", "description": "Ajoute un titre à la file d'attente Spotify (« après ça, mets ... »)", "parameters": {"type": "object", "properties": {"recherche": {"type": "string"}}, "required": ["recherche"]}}},
    {"type": "function", "function": {"name": "spotify_en_cours", "description": "Dit quel morceau joue en ce moment", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "augmenter_son", "description": "Augmente le volume du son de l'ordinateur", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "baisser_son", "description": "Baisse le volume du son de l'ordinateur", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "couper_son", "description": "Coupe ou remet le son de l'ordinateur (muet)", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "ouvrir_application", "description": "Ouvre un logiciel installé sur le PC", "parameters": {"type": "object", "properties": {"nom": {"type": "string", "description": "Ex. « Discord », « Steam », « Word », « Packet Tracer »"}}, "required": ["nom"]}}},
    {"type": "function", "function": {"name": "fermer_application", "description": "Ferme un logiciel ouvert (comme un clic sur la croix)", "parameters": {"type": "object", "properties": {"nom": {"type": "string"}}, "required": ["nom"]}}},
    {"type": "function", "function": {"name": "ouvrir_site", "description": "Ouvre un site web dans le navigateur", "parameters": {"type": "object", "properties": {"adresse": {"type": "string", "description": "Ex. « youtube.com », « https://www.netflix.com »"}}, "required": ["adresse"]}}},
    {"type": "function", "function": {"name": "verrouiller_pc", "description": "Verrouille la session Windows", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "mettre_pc_en_veille", "description": "Met le PC en veille", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "capture_ecran", "description": "Fait une capture d'écran", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "afficher_bureau", "description": "Réduit toutes les fenêtres pour afficher le bureau", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "creer_minuteur", "description": "Lance un minuteur (compte à rebours)", "parameters": {"type": "object", "properties": {"duree_secondes": {"type": "integer", "description": "Durée totale en secondes"}, "nom": {"type": "string", "description": "Nom facultatif, ex. « pâtes »"}}, "required": ["duree_secondes"]}}},
    {"type": "function", "function": {"name": "creer_alarme", "description": "Règle une alarme / un réveil à une heure précise", "parameters": {"type": "object", "properties": {"date_heure": {"type": "string", "description": "AAAA-MM-JJTHH:MM"}, "message": {"type": "string", "description": "Facultatif"}}, "required": ["date_heure"]}}},
    {"type": "function", "function": {"name": "creer_rappel", "description": "Programme un rappel vocal (« rappelle-moi de ... »)", "parameters": {"type": "object", "properties": {"message": {"type": "string", "description": "Ce qu'il faut rappeler"}, "date_heure": {"type": "string", "description": "AAAA-MM-JJTHH:MM, pour une heure précise"}, "dans_secondes": {"type": "integer", "description": "Pour un délai relatif"}}, "required": ["message"]}}},
    {"type": "function", "function": {"name": "lister_minuteurs_et_rappels", "description": "Liste les minuteurs, alarmes et rappels en cours et le temps restant", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "agenda_evenements", "description": "Lit l'agenda Google entre deux dates (rendez-vous, disponibilités). Donne aussi les numéros à utiliser pour modifier ou supprimer.", "parameters": {"type": "object", "properties": {"debut": {"type": "string", "description": "AAAA-MM-JJ ou AAAA-MM-JJTHH:MM"}, "fin": {"type": "string", "description": "AAAA-MM-JJ ou AAAA-MM-JJTHH:MM, inclus ; vide = même jour"}}, "required": ["debut"]}}},
    {"type": "function", "function": {"name": "agenda_ajouter", "description": "Ajoute un événement à l'agenda Google", "parameters": {"type": "object", "properties": {"titre": {"type": "string"}, "debut": {"type": "string", "description": "AAAA-MM-JJTHH:MM (ou AAAA-MM-JJ si journée entière)"}, "fin": {"type": "string", "description": "Facultatif, 1 h par défaut"}, "lieu": {"type": "string"}, "journee_entiere": {"type": "boolean"}}, "required": ["titre", "debut"]}}},
    {"type": "function", "function": {"name": "agenda_supprimer", "description": "Supprime un événement de l'agenda. Le premier appel (confirme=false) prépare la suppression ; confirme=true seulement après le oui de l'utilisateur.", "parameters": {"type": "object", "properties": {"numero": {"type": "integer", "description": "Numéro donné par agenda_evenements"}, "confirme": {"type": "boolean"}}, "required": ["numero"]}}},
    {"type": "function", "function": {"name": "agenda_modifier", "description": "Déplace ou modifie un événement de l'agenda. Même règle de confirmation que la suppression.", "parameters": {"type": "object", "properties": {"numero": {"type": "integer"}, "titre": {"type": "string"}, "debut": {"type": "string", "description": "AAAA-MM-JJTHH:MM"}, "fin": {"type": "string"}, "lieu": {"type": "string"}, "confirme": {"type": "boolean"}}, "required": ["numero"]}}},
    {"type": "function", "function": {"name": "meteo_previsions", "description": "Météo actuelle et prévisions sur 7 jours", "parameters": {"type": "object", "properties": {"ville": {"type": "string", "description": "Vide = ville de l'utilisateur"}, "date": {"type": "string", "description": "AAAA-MM-JJ ; vide = aujourd'hui et demain"}}}}},
    {"type": "function", "function": {"name": "briefing_du_jour", "description": "Rassemble météo, agenda du jour et rappels pour faire un briefing", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "annuler_minuteur_ou_rappel", "description": "Annule un minuteur, une alarme ou un rappel", "parameters": {"type": "object", "properties": {"cible": {"type": "string", "description": "Numéro, ou « minuteur », « alarme », « rappel », ou « tout »"}}, "required": ["cible"]}}}
]
    


# Phrases courtes qui terminent la conversation.
MOTS_FIN = ("merci", "c'est tout", "parfait", "laisse tomber", "rien", "annule", "stop")
# Ce que Whisper peut entendre quand seule la fin du mot de réveil a été captée.
_RESTES_REVEIL = {"hey", "hé", "eh", "et", "jarvis", "vis", "arvis", "service", "oui", "allo", "allô"}
_BALISES_REFLEXION = re.compile(r"<think>.*?</think>", re.DOTALL)


def _outils_autorises() -> dict:
    """Liste blanche : l'IA ne peut appeler QUE les outils déclarés dans outils_jarvis_groq."""
    return {o["function"]["name"]: globals()[o["function"]["name"]] for o in outils_jarvis_groq}


OUTILS_MUSIQUE = {
    "jouer_bibliotheque", "jouer_toute_la_musique", "jouer_musique_locale", "arreter_musique",
    "reprendre_musique", "musique_suivante", "musique_precedente", "spotify_jouer",
    "spotify_titres_likes",
}
# Passe à True quand l'utilisateur a demandé quelque chose sur la musique pendant la conversation :
# dans ce cas, on ne relance pas automatiquement la musique mise en pause au réveil.
_musique_touchee = False
# Outils qui font jouer de la musique : après eux, Jarvis se remet en veille directement.
OUTILS_LANCENT_MUSIQUE = OUTILS_MUSIQUE - {"arreter_musique"}
_musique_lancee = False


def _executer_outils(tool_calls) -> None:
    global _musique_touchee, _musique_lancee
    autorises = _outils_autorises()
    for tc in tool_calls:
        nom = tc.function.name
        if nom in OUTILS_MUSIQUE:
            _musique_touchee = True
        if nom in OUTILS_LANCENT_MUSIQUE:
            _musique_lancee = True
        try:
            arguments = json.loads(tc.function.arguments or "{}") or {}
        except json.JSONDecodeError:
            arguments = {}
        log.info("L'IA veut exécuter : %s avec %s", nom, arguments)
        fonction = autorises.get(nom)
        if fonction is None:
            resultat = f"Erreur : l'outil {nom} n'existe pas."
        else:
            try:
                try:
                    retour = fonction(**arguments)
                except TypeError:
                    log.warning("Arguments inattendus pour %s, lancement sans arguments.", nom)
                    retour = fonction()
                # Les outils qui renvoient du texte (liste des rappels...) le donnent à l'IA.
                resultat = retour if isinstance(retour, str) else "Action effectuée avec succès"
                log.info("Résultat de %s : %s", nom, resultat)
            except Exception as e:
                log.error("L'outil %s a échoué : %s", nom, e)
                resultat = f"Erreur : {e}"
        # Toujours répondre à chaque appel d'outil, sinon l'appel suivant à Groq est refusé.
        historique_messages.append(
            {"tool_call_id": tc.id, "role": "tool", "name": nom, "content": resultat}
        )


def _messages_pour_ia() -> list[dict]:
    """Historique avec la date et l'heure actuelles ajoutées au message système."""
    systeme = dict(historique_messages[0])
    consignes = " ".join(THEMES[t]["consignes"] for t in sorted(_themes_actifs) if THEMES[t]["consignes"])
    systeme["content"] = f"{systeme['content']} {consignes} {_contexte_temps()}"
    # On n'envoie que les derniers échanges (limite de jetons Groq), en commençant
    # toujours par un message utilisateur pour ne jamais couper un appel d'outil en deux.
    recents = historique_messages[1:]
    if len(recents) > MAX_MESSAGES_HISTORIQUE:
        recents = recents[-MAX_MESSAGES_HISTORIQUE:]
        while recents and recents[0].get("role") != "user":
            recents = recents[1:]
    return [systeme, *recents]


_FIN_PREMIERE_PHRASE = re.compile(r"\s*([.!?…])")


def _nettoyer_pour_voix(texte: str) -> str:
    """Retire ce qui sonne mal à l'oral : « (numéro 3) », parenthèses, astérisques de markdown."""
    texte = re.sub(r"\s*\(?\bnuméro\s+\d+\)?", "", texte, flags=re.IGNORECASE)
    texte = re.sub(r"\s*\(([^)]*)\)", r", \1", texte)   # « Léa (toute la journée) » -> « Léa, toute la journée »
    texte = texte.replace("*", "").replace("#", "")
    return re.sub(r"\s+([,.!?])", r"\1", re.sub(r",\s*,", ",", texte)).strip()


def _ajouter_monsieur(reponse: str) -> str:
    """Ajoute « , monsieur » à la fin de la première phrase, environ une réponse sur trois."""
    if not reponse or "monsieur" in reponse.lower() or random.random() >= PROBA_MONSIEUR:
        return reponse
    fin = _FIN_PREMIERE_PHRASE.search(reponse)
    if not fin:
        return f"{reponse.rstrip()}, monsieur."
    ponctuation = " " + fin.group(1) if fin.group(1) == "?" or fin.group(1) == "!" else fin.group(1)
    return f"{reponse[:fin.start()].rstrip(' ,;:')}, monsieur{ponctuation}{reponse[fin.end():]}"


def _appel_ia(**kwargs):
    """Appel Groq ; réessaie quand le modèle produit un appel d'outil mal formé (erreur 400 tool_use_failed)."""
    import groq

    if MODELE_IA.startswith("openai/gpt-oss"):
        kwargs.setdefault("reasoning_effort", "low")  # réfléchit peu : plus rapide, moins de jetons
    for essai in range(3):
        try:
            return client.chat.completions.create(
                model=MODELE_IA, messages=_messages_pour_ia(), **kwargs
            ).choices[0].message
        except groq.BadRequestError as e:
            if "tool_use_failed" not in str(e) or essai == 2:
                raise
            log.warning("Appel d'outil mal formé par l'IA, nouvel essai (%d).", essai + 1)


def demander_a_l_ia(texte: str) -> str:
    """Envoie la demande à Groq (avec outils) et renvoie la réponse à dire à voix haute."""
    global _themes_actifs
    if gestion_agenda:
        precedent = next((m.get("content") or "" for m in reversed(historique_messages)
                          if m.get("role") == "assistant" and m.get("content")), "")
        gestion_agenda.nouveau_tour(texte, precedent)
    historique_messages.append({"role": "user", "content": texte})
    _themes_actifs = _choisir_themes()
    log.debug("Thèmes envoyés à l'IA : %s", sorted(_themes_actifs))
    taille_avant = len(historique_messages) - 1
    try:
        return _demander_a_l_ia()
    except Exception:
        # On retire la demande ratée pour ne pas laisser un historique incohérent.
        del historique_messages[taille_avant:]
        raise


def _demander_a_l_ia() -> str:
    # L'IA peut enchaîner plusieurs outils (ex. regarder ce qui joue, puis répondre) :
    # on boucle tant qu'elle appelle des outils, avec une limite de sécurité.
    for _ in range(MAX_ETAPES_OUTILS):
        message = _appel_ia(tools=_outils_actifs(), tool_choice="auto")
        if not message.tool_calls:
            break
        historique_messages.append({
            "role": "assistant",
            "tool_calls": [
                {
                    "id": t.id,
                    "type": "function",
                    "function": {"name": t.function.name, "arguments": t.function.arguments},
                }
                for t in message.tool_calls
            ],
        })
        _executer_outils(message.tool_calls)
    else:
        return "C'est fait."

    reponse = _BALISES_REFLEXION.sub("", message.content or "")
    # Parfois le modèle laisse un « </think> » orphelin suivi de la vraie réponse : on garde la fin.
    reponse = _ajouter_monsieur(_nettoyer_pour_voix(reponse.rsplit("</think>", 1)[-1].strip()))
    if reponse:
        historique_messages.append({"role": "assistant", "content": reponse})
    return reponse


def _micro_vocal() -> int | None:
    """Micro pour la voix : JARVIS_INPUT_DEVICE si défini, sinon le micro par défaut de Windows."""
    spec = (os.environ.get("JARVIS_INPUT_DEVICE") or "").strip()
    if not spec:
        return None
    try:
        from claquements import trouver_micro

        return trouver_micro(spec)
    except Exception as e:
        log.warning("JARVIS_INPUT_DEVICE invalide (%s), micro par défaut utilisé.", e)
        return None


def _seuil_reveil() -> float:
    try:
        return float((os.environ.get("JARVIS_SEUIL_REVEIL") or SEUIL_MOT_REVEIL))
    except ValueError:
        return SEUIL_MOT_REVEIL


def boucle_vocale() -> None:
    """Assistant vocal façon Alexa.

    1. Attend « Jarvis » (ou « Hey Jarvis ») en local : rien n'est envoyé sur internet avant.
    2. Bip, puis écoute la demande jusqu'à ce que vous vous arrêtiez de parler.
    3. Whisper (Groq) transcrit, l'IA répond, Rémy lit la réponse phrase par phrase.
    4. Vous pouvez le couper en redisant « Jarvis ».
    5. Il écoute encore quelques secondes pour une question de suivi, sans mot de réveil.
    """
    global historique_messages, gestion_rappels, pont_spotify, gestion_agenda, _musique_touchee, _musique_lancee
    from ecoute import Ecoute, bip, transcrire
    from rappels import Rappels

    gestion_rappels = Rappels(FICHIER_RAPPELS, annoncer=parler)
    from spotify import PontSpotify

    pont_spotify = PontSpotify()  # l'extension Spicetify s'y connecte toute seule
    import pc

    threading.Thread(target=pc.lister_applications, name="applis", daemon=True).start()  # prête à l'avance
    if MUSIQUE_AU_LANCEMENT:
        # En arrière-plan : si Spotify est fermé, il faut quelques secondes pour l'ouvrir.
        threading.Thread(target=jouer_musique_bienvenue, name="bienvenue", daemon=True).start()
    from agenda import Agenda

    try:
        gestion_agenda = Agenda(FICHIER_RAPPELS.parent, annoncer=parler, minutes_annonce=ANNONCE_AGENDA_MIN)
        if not gestion_agenda.disponible:
            gestion_agenda = None
    except Exception as e:
        log.warning("Agenda Google indisponible : %s", e)
        gestion_agenda = None
    # Prépare « Oui, monsieur ? » en avance pour qu'il soit instantané au premier réveil.
    threading.Thread(target=preparer_phrase, args=("Oui, monsieur ?",), daemon=True).start()

    nettoyer_temporaires()

    try:
        ecoute = Ecoute(device=_micro_vocal(), seuil_reveil=_seuil_reveil(),
                        modele_vosk=MODELE_VOSK if os.path.isdir(MODELE_VOSK) else None,
                        jarvis_parle=verrou_parole.locked)
    except Exception as e:
        log.error("Impossible de démarrer l'écoute vocale : %s", e)
        return

    if TOUCHE_JARVIS.strip():
        def _touche_jarvis() -> None:
            if verrou_parole.locked():
                log.info("Touche Jarvis : silence.")
                _arret_par_touche.set()
                arret_parole.set()
            elif gestion_rappels.sonnerie_active():
                log.info("Touche Jarvis : sonnerie arrêtée.")
                gestion_rappels.arreter_sonnerie()
            else:
                log.info("Touche Jarvis : réveil.")
                ecoute.reveiller()

        pc.ecouter_raccourci(TOUCHE_JARVIS, _touche_jarvis)

    log.info("IA vocale prête. Dites « Jarvis » pour interagir.")
    dernier_echange = 0.0

    while True:
        ecoute.attendre_reveil()

        # « Jarvis » pendant qu'un minuteur / une alarme sonne = on arrête la sonnerie.
        if gestion_rappels.sonnerie_active():
            gestion_rappels.arreter_sonnerie()
            ecoute.annuler_capture()
            log.info("Sonnerie arrêtée à la voix.")
            dernier = gestion_rappels.dernier_declenche
            if BRIEFING_APRES_REVEIL and dernier and dernier.type == "alarme" and datetime.now().hour < 12:
                time.sleep(0.5)  # laisse la sonnerie se couper
                parler(rediger_briefing("réveil du matin, l'alarme vient d'être arrêtée"),
                       interruption=ecoute.reveil)
            continue

        if time.time() - dernier_echange > OUBLI_CONVERSATION_S:
            historique_messages = [historique_messages[0]]
        bip(montee=True)
        _musique_touchee = False
        musique_mise_en_pause = _pause_musique_au_reveil() if PAUSE_MEDIA_AU_REVEIL else False

        # Si vous enchaînez « Jarvis, quelle heure est-il ? », on ne vous coupe pas la parole :
        # on attend 0,7 s, et seulement si vous ne dites rien, Jarvis répond « Oui, monsieur ? ».
        pcm = ecoute.ecouter_phrase(delai_debut=0.7, deja_en_cours=True)
        accueil_dit = False
        if pcm is None and not ecoute.reveil.is_set():
            dire_rapidement("Oui, monsieur ?")
            accueil_dit = True
            pcm = ecoute.ecouter_phrase(delai_debut=DELAI_ECOUTE_S, ignorer_debut=IGNORER_APRES_PAROLE_S)

        while True:
            if pcm is None:
                if ecoute.attendre_reveil(timeout=0):   # « Jarvis » redit pendant l'écoute
                    bip(montee=True)
                    pcm = ecoute.ecouter_phrase(delai_debut=DELAI_ECOUTE_S, deja_en_cours=True)
                    continue
                bip(montee=False)
                break

            texte = transcrire(pcm, client)
            court = texte.lower().strip(" .!?,")
            # Juste la fin du mot de réveil captée (« ...vis »), ou rien de compris : on demande.
            if not accueil_dit and (not texte or set(court.replace(",", " ").split()) <= _RESTES_REVEIL):
                accueil_dit = True
                dire_rapidement("Oui, monsieur ?")
                pcm = ecoute.ecouter_phrase(delai_debut=DELAI_ECOUTE_S, ignorer_debut=IGNORER_APRES_PAROLE_S)
                continue
            accueil_dit = True
            if not texte:
                bip(montee=False)
                break
            log.info("Vous : %s", texte)
            dernier_echange = time.time()

            if len(court.split()) <= 4 and any(m in court for m in MOTS_FIN):
                historique_messages = [historique_messages[0]]
                dire_rapidement("Très bien.")
                break

            try:
                _musique_lancee = False
                _actions_apres_reponse.clear()
                reponse = demander_a_l_ia(texte)
            except Exception as e:
                log.error("Erreur avec l'API Groq : %s", e)
                parler("J'ai un problème de connexion avec le serveur.")
                break

            if reponse:
                log.info("Jarvis : %s", reponse)
                # Si Jarvis prononce son propre nom, on ne veut pas qu'il se coupe tout seul.
                ecoute.reveil_actif = "jarvis" not in reponse.lower()
                _arret_par_touche.clear()
                termine = parler(reponse, interruption=ecoute.reveil)
                ecoute.reveil_actif = True
                if not termine and _arret_par_touche.is_set():   # coupé par la touche : il se rendort
                    _arret_par_touche.clear()
                    ecoute.reveil.clear()
                    ecoute.annuler_capture()
                    bip(montee=False)
                    break
                if not termine:   # coupé par « Jarvis » : on écoute la nouvelle demande
                    ecoute.attendre_reveil(timeout=0)
                    bip(montee=True)
                    pcm = ecoute.ecouter_phrase(delai_debut=DELAI_ECOUTE_S, deja_en_cours=True,
                                                ignorer_debut=IGNORER_APRES_PAROLE_S)
                    continue

            # Verrouiller / veille : maintenant que Jarvis a parlé. Il se rendort sans relancer la musique.
            if _actions_apres_reponse:
                actions = list(_actions_apres_reponse)
                _actions_apres_reponse.clear()
                _musique_touchee = True
                bip(montee=False)
                time.sleep(0.4)
                for action in actions:
                    try:
                        action()
                    except Exception as e:
                        log.error("Action PC impossible : %s", e)
                break
            # Musique lancée : on laisse écouter, Jarvis se rendort tout de suite.
            if _musique_lancee:
                bip(montee=False)
                break
            if DUREE_SUIVI_S <= 0:
                break
            # Mode suivi : on réécoute quelques secondes sans mot de réveil.
            pcm = ecoute.ecouter_phrase(delai_debut=DUREE_SUIVI_S, ignorer_debut=IGNORER_APRES_PAROLE_S)

        # Fin de conversation : on relance la musique qu'on avait mise en pause,
        # sauf si vous avez demandé autre chose (pause, autre morceau...).
        if musique_mise_en_pause and not _musique_touchee and _spotify_connecte():
            pont_spotify.commande("reprendre", lancer=False)


def _pause_musique_au_reveil() -> bool:
    """Met la musique en pause pendant qu'on parle. Renvoie True si Spotify jouait (à relancer après).

    Spotify connecté : on vérifie qu'il joue avant de le mettre en pause (pas de Play/Pause à l'aveugle).
    Sinon : touche Play/Pause pour les autres lecteurs (comportement d'origine).
    """
    if _spotify_connecte():
        etat = pont_spotify.commande("etat", lancer=False, timeout=2)
        if etat.get("ok") and etat.get("lecture"):
            pont_spotify.commande("pause", lancer=False, timeout=2)
            return True
        return False
    pyautogui.press("playpause")
    return False


_demarrage = time.monotonic()
_dernier_lancement_musique = 0.0  # moment où Jarvis a lancé de la musique pour la dernière fois


def _double_clap_autorise() -> bool:
    """Un « double claquement » entendu pendant que la musique joue ou que Jarvis parle vient presque
    toujours des enceintes (batterie, début d'un morceau) : on l'ignore."""
    if verrou_parole.locked():
        log.info("Double claquement ignoré : Jarvis est en train de parler.")
        return False
    if time.monotonic() - _demarrage < 5:
        log.info("Double claquement ignoré : démarrage en cours (calibrage du micro).")
        return False
    if time.monotonic() - _dernier_lancement_musique < 15:
        # Spotify met 1 à 2 s à dire qu'il joue, alors que le son sort déjà des enceintes.
        log.info("Double claquement ignoré : Jarvis vient de lancer de la musique.")
        return False
    if _spotify_connecte():
        etat = pont_spotify.commande("etat", lancer=False, timeout=2)
        if etat.get("ok") and etat.get("lecture"):
            log.info("Double claquement ignoré : la musique joue (sans doute les enceintes).")
            return False
    return True


# =============================================================================================
# DOUBLE CLAQUEMENT ET DÉMARRAGE
# =============================================================================================

def say_jarvis_welcome() -> None:
    if not JARVIS_WELCOME_ENABLED or not JARVIS_WELCOME_PHRASE.strip():
        return
    briefing: list[str] = []
    preparation = None
    if BRIEFING_AU_DOUBLE_CLAP:
        # Le briefing se prépare pendant la phrase de bienvenue : pas de blanc entre les deux.
        preparation = threading.Thread(
            target=lambda: briefing.append(rediger_briefing("retour à la maison")), daemon=True
        )
        preparation.start()
    parler(JARVIS_WELCOME_PHRASE)
    if preparation is not None:
        preparation.join(timeout=20)
        if briefing and briefing[0]:
            parler(briefing[0])


def run_double_clap_actions() -> None:
    """Routine du double claquement : musique, pages Chrome, bienvenue + briefing."""
    from fenetres import ouvrir_page

    if MUSIQUE_AU_DOUBLE_CLAP:
        jouer_musique_bienvenue()
    for i, (url, ecran) in enumerate(PAGES_DOUBLE_CLAP, start=1):
        if url:
            ouvrir_page(url, ecran=ecran, plein_ecran=PAGES_PLEIN_ECRAN,
                        profil_separe=CHROME_PROFILS_SEPARES, nom=f"page{i}")
    if JARVIS_WELCOME_ENABLED and JARVIS_WELCOME_PHRASE.strip():
        time.sleep(max(0.0, DELAI_AVANT_BIENVENUE_S))
        threading.Thread(target=say_jarvis_welcome, name="bienvenue", daemon=True).start()


def main() -> int:
    # Conversation vocale (« Jarvis ») dans un thread séparé
    thread_vocal = threading.Thread(target=boucle_vocale, name="vocal", daemon=True)
    thread_vocal.start()
    try:
        if CLAP_DETECTION_ENABLED:
            from claquements import ecouter_claquements

            return ecouter_claquements(sur_double_clap=run_double_clap_actions, autorise=_double_clap_autorise)
        log.info("Double claquement désactivé (CLAP_DETECTION_ENABLED = False). Ctrl+C pour arrêter.")
        while thread_vocal.is_alive():
            thread_vocal.join(timeout=0.5)  # timeout pour que Ctrl+C reste réactif
    except KeyboardInterrupt:
        log.info("Arrêt de Jarvis.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

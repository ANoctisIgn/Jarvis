"""Contrôle du PC : ouvrir / fermer des applications, ouvrir un site, verrouiller, veille,
capture d'écran, afficher le bureau.

Les applications sont celles du menu Démarrer (Get-StartApps) : logiciels classiques et applis du Store.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import subprocess
import threading
import time
import unicodedata
import webbrowser
from ctypes import wintypes
from datetime import datetime

log = logging.getLogger("clap_listen")

# Raccourcis du menu Démarrer qui ne sont pas de vraies applications.
_PARASITES = ("desinstall", "uninstall", "help", "aide", "documentation", "readme", "release notes",
              "support center", "lisez-moi", "manual", "manuel", "website", "site web")
# Programmes que Jarvis ne ferme jamais (Windows lui-même).
_PROTEGES = {"explorer.exe", "dwm.exe", "searchhost.exe", "startmenuexperiencehost.exe",
             "shellexperiencehost.exe", "textinputhost.exe", "lockapp.exe", "systemsettings.exe"}
_SANS_CREATION_FENETRE = 0x08000000


def _normaliser(texte: str) -> str:
    sans_accents = unicodedata.normalize("NFD", (texte or "").lower()).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", sans_accents))


# --- applications installées ---------------------------------------------------------

_applis: list[tuple[str, str]] = []
_applis_lues = 0.0
_verrou_applis = threading.Lock()


def lister_applications(forcer: bool = False) -> list[tuple[str, str]]:
    """(nom, identifiant) des applications du menu Démarrer. Mis en cache 10 minutes."""
    global _applis, _applis_lues
    with _verrou_applis:
        if _applis and not forcer and time.monotonic() - _applis_lues < 600:
            return _applis
        commande = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                    "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress")
        try:
            sortie = subprocess.run(
                ["powershell", "-NoProfile", "-Command", commande],
                capture_output=True, text=True, encoding="utf-8", timeout=30,
                creationflags=_SANS_CREATION_FENETRE,
            ).stdout
            donnees = json.loads(sortie or "[]")
            if isinstance(donnees, dict):
                donnees = [donnees]
            _applis = [(d["Name"], d["AppID"]) for d in donnees if d.get("Name") and d.get("AppID")]
            _applis_lues = time.monotonic()
            log.info("%d applications trouvées dans le menu Démarrer.", len(_applis))
        except (subprocess.SubprocessError, ValueError, OSError) as e:
            log.warning("Liste des applications indisponible : %s", e)
        return _applis


def _score(recherche: str, nom: str) -> int:
    r, n = _normaliser(recherche), _normaliser(nom)
    if not r or not n:
        return 0
    if r == n:
        return 100
    if n.startswith(r + " ") or n.startswith(r):
        return 85
    mots_r, mots_n = set(r.split()), set(n.split())
    if mots_r <= mots_n:
        return 70
    if r in n:
        return 55
    communs = len(mots_r & mots_n)
    return 30 + 10 * communs if communs else 0


def trouver_application(nom: str) -> tuple[str, str] | None:
    candidats = []
    for nom_appli, appid in lister_applications():
        if any(p in _normaliser(nom_appli) for p in _PARASITES) or appid.startswith("http"):
            continue
        score = _score(nom, nom_appli)
        if score:
            candidats.append((score, -len(nom_appli), nom_appli, appid))
    if not candidats:
        return None
    _, _, nom_appli, appid = max(candidats)
    return nom_appli, appid


def ouvrir_application(nom: str) -> str:
    appli = trouver_application(nom) or (trouver_application(nom) if lister_applications(forcer=True) else None)
    if appli is None:
        return f"Erreur : aucune application « {nom} » dans le menu Démarrer."
    nom_appli, appid = appli
    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{appid}"], creationflags=_SANS_CREATION_FENETRE)
    log.info("Ouverture de %s (%s).", nom_appli, appid)
    return f"{nom_appli} est en train de s'ouvrir."


def ouvrir_site(adresse: str) -> str:
    adresse = (adresse or "").strip()
    if not re.match(r"^https?://", adresse):
        adresse = "https://" + adresse.lstrip("/")
    if not re.match(r"^https?://[\w.-]+\.[a-z]{2,}", adresse, re.IGNORECASE):
        return "Erreur : adresse de site invalide."
    webbrowser.open(adresse)
    log.info("Ouverture du site %s.", adresse)
    return f"Site ouvert : {adresse}."


# --- fenêtres ouvertes -------------------------------------------------------------------

def _processus_a_proteger() -> set[int]:
    """Jarvis lui-même et ses parents (terminal, VS Code...) : les fermer arrêterait Jarvis."""
    import psutil

    pids = set()
    try:
        p = psutil.Process(os.getpid())
        while p is not None:
            pids.add(p.pid)
            p = p.parent()
    except psutil.Error:
        pass
    return pids


def _fenetres_visibles() -> list[tuple[int, str, str, str, int]]:
    """(hwnd, titre, exe, classe, pid) des fenêtres principales visibles."""
    import psutil

    user32 = ctypes.windll.user32
    resultats = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # 4 = GW_OWNER
            return True
        longueur = user32.GetWindowTextLengthW(hwnd)
        if longueur == 0:
            return True
        titre = ctypes.create_unicode_buffer(longueur + 1)
        user32.GetWindowTextW(hwnd, titre, longueur + 1)
        classe = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, classe, 256)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            exe = psutil.Process(pid.value).name().lower()
        except psutil.Error:
            return True
        resultats.append((int(hwnd), titre.value, exe, classe.value, pid.value))
        return True

    user32.EnumWindows(_enum, 0)
    return resultats


def _fermer_fenetre(hwnd: int) -> None:
    """Comme un clic sur la croix : l'application peut demander d'enregistrer."""
    ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE


def fermer_application(nom: str) -> str:
    """Ferme proprement (comme la croix) les fenêtres de l'application demandée."""
    cible = _normaliser(nom)
    if not cible:
        return "Erreur : quelle application ?"
    # Nom de l'exe correspondant dans le menu Démarrer (« Google Chrome » -> chrome.exe)
    appli = trouver_application(nom)
    exe_attendu = ""
    if appli and appli[1].lower().endswith(".exe"):
        exe_attendu = os.path.basename(appli[1]).lower()
    proteges = _processus_a_proteger()
    fenetres = _fenetres_visibles()
    # Fermer une fenêtre de dossier (CabinetWClass) n'arrête pas explorer.exe : toujours autorisé.
    autorisees = [f for f in fenetres
                  if f[3] == "CabinetWClass" or (f[4] not in proteges and f[2] not in _PROTEGES)]

    def par_programme(f) -> bool:
        nom_exe = _normaliser(f[2].removesuffix(".exe"))
        return bool((exe_attendu and f[2] == exe_attendu) or cible in nom_exe or nom_exe in cible.split())

    # 1) Par programme (discord.exe...). 2) Sinon par titre de fenêtre (applis du Store comme la
    #    calculatrice). Jamais les deux : « ferme Discord » ne doit pas fermer un onglet Chrome « Discord ».
    a_fermer = [(f[0], f[1], f[2]) for f in autorisees if par_programme(f)]
    if not a_fermer:
        # Fenêtres « hôtes » dont le programme ne porte pas le nom de l'appli : applis du Store
        # (ApplicationFrameHost) et dossiers de l'explorateur (classe CabinetWClass).
        titres_cherches = {cible} | ({_normaliser(appli[0])} if appli else set())
        a_fermer = [(f[0], f[1], f[2]) for f in autorisees
                    if (f[2] == "applicationframehost.exe" or f[3] == "CabinetWClass")
                    and any(t in _normaliser(f[1]) for t in titres_cherches)]
    if not a_fermer:
        if any(par_programme(f) for f in fenetres if f[4] in proteges):
            return "Erreur : je ne peux pas fermer cette application, Jarvis tourne dedans."
        return f"Aucune fenêtre ouverte ne correspond à « {nom} »."
    for hwnd, _, _ in a_fermer:
        _fermer_fenetre(hwnd)
    log.info("Fermeture : %s", [(t[:40], e) for _, t, e in a_fermer])
    # Pour les fenêtres « hôtes » (applis du Store, dossiers), le titre parle mieux que le programme.
    noms = sorted({t if e in ("applicationframehost.exe", "explorer.exe") else e.removesuffix(".exe")
                   for _, t, e in a_fermer})
    return f"{len(a_fermer)} fenêtre(s) fermée(s) : {', '.join(noms)}."


# --- système -------------------------------------------------------------------------------

def verrouiller() -> None:
    log.info("Verrouillage du PC.")
    if not ctypes.windll.user32.LockWorkStation():
        log.error("Verrouillage refusé par Windows (erreur %d).", ctypes.windll.kernel32.GetLastError())


def veille_classique_disponible() -> bool:
    """True si le PC a la veille classique (S3). Les PC récents n'ont souvent que la veille moderne."""
    capacites = (ctypes.c_ubyte * 76)()  # SYSTEM_POWER_CAPABILITIES
    if not ctypes.windll.powrprof.GetPwrCapabilities(ctypes.byref(capacites)):
        return False
    return bool(capacites[5])  # SystemS3


def mettre_en_veille() -> None:
    if veille_classique_disponible():
        log.info("Mise en veille (veille classique).")
        # SetSuspendState(hibernation=False, forcer=False, désactiver_réveils=False)
        if ctypes.windll.powrprof.SetSuspendState(False, False, False):
            return
        log.warning("Veille classique refusée, on éteint l'écran.")
    # Veille moderne (la plupart des PC portables récents) : éteindre l'écran revient à appuyer sur le
    # bouton d'alimentation. SetSuspendState ne marche pas sur ces PC.
    log.info("Mise en veille (veille moderne : extinction de l'écran).")
    ctypes.windll.user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)  # HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, éteint


def afficher_bureau() -> None:
    import pyautogui

    pyautogui.hotkey("win", "d")


def _dossier_images() -> str:
    tampon = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 39, None, 0, tampon)  # 39 = CSIDL_MYPICTURES
    return tampon.value or os.path.join(os.path.expanduser("~"), "Pictures")


def capture_ecran() -> str:
    """Capture de tous les écrans, enregistrée dans Images\\Captures Jarvis."""
    from PIL import ImageGrab

    dossier = os.path.join(_dossier_images(), "Captures Jarvis")
    os.makedirs(dossier, exist_ok=True)
    chemin = os.path.join(dossier, f"capture_{datetime.now():%Y-%m-%d_%H-%M-%S}.png")
    ImageGrab.grab(all_screens=True).save(chemin)
    log.info("Capture d'écran : %s", chemin)
    return chemin


# --- raccourci clavier global ---------------------------------------------------------------

_MODIFICATEURS = {"ctrl": 0x2, "control": 0x2, "alt": 0x1, "shift": 0x4, "maj": 0x4, "win": 0x8}
_TOUCHES = {"espace": 0x20, "space": 0x20, "pause": 0x13, "echap": 0x1B, "esc": 0x1B, "entree": 0x0D,
            "enter": 0x0D, "inser": 0x2D, "insert": 0x2D, "fin": 0x23, "end": 0x23}


def _lire_raccourci(texte: str) -> tuple[int, int] | None:
    """« ctrl+alt+j » -> (modificateurs, code de touche). None si illisible."""
    modificateurs, touche = 0, None
    for morceau in _normaliser(texte.replace("+", " ")).split():
        if morceau in _MODIFICATEURS:
            modificateurs |= _MODIFICATEURS[morceau]
        elif morceau in _TOUCHES:
            touche = _TOUCHES[morceau]
        elif len(morceau) == 1 and morceau.isalnum():
            touche = ord(morceau.upper())
        elif re.fullmatch(r"f([1-9]|1[0-2])", morceau):
            touche = 0x6F + int(morceau[1:])
        else:
            return None
    return (modificateurs, touche) if touche is not None else None


def ecouter_raccourci(raccourci: str, action) -> None:
    """Appelle `action()` à chaque appui sur `raccourci` (ex. « ctrl+alt+j »), où que soit le focus.
    Tourne dans son propre thread (Windows envoie le raccourci au thread qui l'a enregistré)."""
    lu = _lire_raccourci(raccourci)
    if lu is None:
        log.warning("Raccourci « %s » illisible : touche d'interruption désactivée.", raccourci)
        return

    def boucle() -> None:
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, lu[0] | 0x4000, lu[1]):  # 0x4000 = pas de répétition
            log.warning("Raccourci « %s » déjà utilisé par un autre programme : choisissez-en un autre "
                        "(TOUCHE_JARVIS).", raccourci)
            return
        log.info("Touche Jarvis : %s.", raccourci)
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message == 0x0312:  # WM_HOTKEY
                try:
                    action()
                except Exception as e:
                    log.error("Touche Jarvis : %s", e)

    threading.Thread(target=boucle, name="raccourci", daemon=True).start()

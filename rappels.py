"""Minuteurs, alarmes et rappels de Jarvis.

Tout est sauvegardé dans un fichier JSON : un rappel programmé survit à un redémarrage.
- Minuteur / alarme : annonce puis sonnerie jusqu'à « Jarvis » (ou 60 s max).
- Rappel : petit carillon puis lecture du message.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd

log = logging.getLogger("clap_listen")

SONNERIE_MAX_S = 60.0
RAPPEL_MANQUE_MAX = timedelta(hours=12)  # au redémarrage, on annonce les rappels manqués récents

JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre")


# --- formatage pour la voix -----------------------------------------------------

def duree_en_mots(secondes: float) -> str:
    s = int(round(secondes))
    if s <= 0:
        return "quelques instants"
    h, reste = divmod(s, 3600)
    m, s = divmod(reste, 60)
    morceaux = []
    if h:
        morceaux.append(f"{h} heure{'s' if h > 1 else ''}")
    if m:
        morceaux.append(f"{m} minute{'s' if m > 1 else ''}")
    if s and not h:
        morceaux.append(f"{s} seconde{'s' if s > 1 else ''}")
    return " et ".join(morceaux)


def heure_en_mots(quand: datetime, maintenant: datetime | None = None) -> str:
    maintenant = maintenant or datetime.now()
    heure = f"{quand.hour} h" + (f" {quand.minute:02d}" if quand.minute else "")
    ecart_jours = (quand.date() - maintenant.date()).days
    if ecart_jours == 0:
        return f"aujourd'hui à {heure}"
    if ecart_jours == 1:
        return f"demain à {heure}"
    return f"le {JOURS[quand.weekday()]} {quand.day} {MOIS[quand.month - 1]} à {heure}"


# --- sons -------------------------------------------------------------------------

_TAUX_SON = 24000


def _note(freq: float, duree: float, volume: float = 0.3) -> np.ndarray:
    t = np.arange(int(_TAUX_SON * duree)) / _TAUX_SON
    attaque = np.minimum(1.0, t / 0.005)
    return volume * np.sin(2 * np.pi * freq * t) * attaque * np.exp(-4.0 * t)


def _son_sonnerie() -> np.ndarray:
    """Do-Mi-Sol montant, puis un blanc : un cycle d'environ 1,2 s."""
    notes = [_note(f, 0.18) for f in (1046.5, 1318.5, 1568.0)]
    return np.concatenate(notes + [np.zeros(int(_TAUX_SON * 0.65))]).astype(np.float32)


def _son_carillon() -> np.ndarray:
    notes = [_note(f, 0.35, 0.25) for f in (1568.0, 1046.5)]
    return np.concatenate(notes).astype(np.float32)


# --- données ----------------------------------------------------------------------

@dataclass
class Element:
    id: int
    type: str            # "minuteur" | "alarme" | "rappel"
    echeance: str        # date ISO
    message: str = ""
    duree: float = 0.0   # minuteur : durée initiale en secondes

    @property
    def quand(self) -> datetime:
        return datetime.fromisoformat(self.echeance)

    def description(self, maintenant: datetime | None = None) -> str:
        maintenant = maintenant or datetime.now()
        if self.type == "minuteur":
            nom = f" « {self.message} »" if self.message else ""
            reste = (self.quand - maintenant).total_seconds()
            return (f"minuteur numéro {self.id}{nom} de {duree_en_mots(self.duree)}, "
                    f"il reste {duree_en_mots(reste)}")
        if self.type == "alarme":
            extra = f" ({self.message})" if self.message else ""
            return f"alarme numéro {self.id} {heure_en_mots(self.quand, maintenant)}{extra}"
        return f"rappel numéro {self.id} {heure_en_mots(self.quand, maintenant)} : {self.message}"


class Rappels:
    def __init__(self, fichier: Path, annoncer: Callable[[str], object]) -> None:
        self._fichier = fichier
        self._annoncer = annoncer
        self._lock = threading.Lock()
        self._elements: list[Element] = []
        self._stop_sonnerie = threading.Event()
        self._en_cours = threading.Event()   # un minuteur/alarme/rappel est en train de sonner
        self.dernier_declenche: Element | None = None
        manques = self._charger()
        threading.Thread(target=self._boucle, args=(manques,), name="rappels", daemon=True).start()

    # --- persistance ---

    def _charger(self) -> list[Element]:
        if not self._fichier.is_file():
            return []
        try:
            brut = json.loads(self._fichier.read_text(encoding="utf-8"))
            elements = [Element(**e) for e in brut]
        except (OSError, ValueError, TypeError) as e:
            log.warning("Fichier de rappels illisible (%s), on repart de zéro.", e)
            return []
        maintenant = datetime.now()
        manques = [
            e for e in elements
            if e.quand <= maintenant and e.type == "rappel" and maintenant - e.quand < RAPPEL_MANQUE_MAX
        ]
        self._elements = [e for e in elements if e.quand > maintenant]
        if len(self._elements) != len(elements):
            self._sauver()
        if self._elements:
            log.info("%d minuteur(s)/alarme(s)/rappel(s) rechargé(s).", len(self._elements))
        return manques

    def _sauver(self) -> None:
        self._fichier.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._fichier.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([asdict(e) for e in self._elements], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._fichier)

    def _ajouter(self, type_: str, quand: datetime, message: str = "", duree: float = 0.0) -> Element:
        with self._lock:
            nouvel_id = max((e.id for e in self._elements), default=0) + 1
            el = Element(nouvel_id, type_, quand.isoformat(timespec="seconds"), message.strip(), duree)
            self._elements.append(el)
            self._elements.sort(key=lambda e: e.quand)
            self._sauver()
        log.info("Programmé : %s", el.description())
        return el

    # --- API utilisée par les outils de l'IA ---

    def ajouter_minuteur(self, duree_s: float, nom: str = "") -> str:
        if duree_s <= 0:
            return "Erreur : la durée doit être positive."
        el = self._ajouter("minuteur", datetime.now() + timedelta(seconds=duree_s), nom, duree_s)
        return f"Minuteur numéro {el.id} de {duree_en_mots(duree_s)} lancé."

    def ajouter_alarme(self, quand: datetime, message: str = "") -> str:
        el = self._ajouter("alarme", quand, message)
        return f"Alarme numéro {el.id} réglée pour {heure_en_mots(quand)}."

    def ajouter_rappel(self, quand: datetime, message: str) -> str:
        el = self._ajouter("rappel", quand, message)
        return f"Rappel numéro {el.id} programmé pour {heure_en_mots(quand)} : {el.message}."

    def lister(self) -> str:
        with self._lock:
            elements = list(self._elements)
        if not elements:
            return "Aucun minuteur, alarme ou rappel en cours."
        maintenant = datetime.now()
        return "En cours : " + " ; ".join(e.description(maintenant) for e in elements) + "."

    def annuler(self, cible: str) -> str:
        cible = (cible or "").strip().lower()
        with self._lock:
            if cible in ("tout", "tous", "toutes", "all"):
                n = len(self._elements)
                self._elements.clear()
                self._sauver()
                return f"{n} élément(s) annulé(s)." if n else "Il n'y avait rien à annuler."
            if cible.isdigit():
                visees = [e for e in self._elements if e.id == int(cible)]
            else:
                type_ = next((t for t in ("minuteur", "alarme", "rappel") if t in cible), None)
                if type_ is None:
                    return "Erreur : cible inconnue. Utilise un numéro, un type ou « tout »."
                du_type = [e for e in self._elements if e.type == type_]
                if len(du_type) > 1:
                    return ("Plusieurs éléments correspondent, précise le numéro : "
                            + " ; ".join(e.description() for e in du_type) + ".")
                visees = du_type
            if not visees:
                return "Rien ne correspond à cette demande."
            for e in visees:
                self._elements.remove(e)
            self._sauver()
        return "Annulé : " + " ; ".join(e.description() for e in visees) + "."

    # --- sonnerie ---

    def sonnerie_active(self) -> bool:
        return self._en_cours.is_set()

    def arreter_sonnerie(self) -> None:
        self._stop_sonnerie.set()

    def _sonner(self, son: np.ndarray, max_s: float) -> None:
        debut = time.monotonic()
        duree = len(son) / _TAUX_SON
        while not self._stop_sonnerie.is_set() and time.monotonic() - debut < max_s:
            try:
                sd.play(son, _TAUX_SON)
            except Exception as e:
                log.warning("Sonnerie impossible : %s", e)
                return
            self._stop_sonnerie.wait(duree)
        sd.stop()

    def _declencher(self, el: Element) -> None:
        self._stop_sonnerie.clear()
        self.dernier_declenche = el
        self._en_cours.set()
        try:
            log.info("Déclenchement : %s", el.description())
            if el.type == "rappel":
                self._sonner(_son_carillon(), max_s=0.1)   # un seul carillon
                self._annoncer(f"Petit rappel : {el.message}.")
                return
            if el.type == "minuteur":
                nom = f" {el.message}" if el.message else ""
                annonce = f"Le minuteur{nom} de {duree_en_mots(el.duree)} est terminé."
            else:
                annonce = f"Il est {heure_en_mots(el.quand).split(' à ')[-1]}, monsieur."
                if el.message:
                    annonce += f" {el.message}."
            self._annoncer(annonce)
            self._sonner(_son_sonnerie(), max_s=SONNERIE_MAX_S)
        finally:
            self._en_cours.clear()

    def _boucle(self, manques: list[Element]) -> None:
        for el in manques:
            self._annoncer(
                f"Pendant mon absence, vous aviez un rappel {heure_en_mots(el.quand)} : {el.message}."
            )
        while True:
            time.sleep(0.25)
            maintenant = datetime.now()
            with self._lock:
                dus = [e for e in self._elements if e.quand <= maintenant]
                if dus:
                    self._elements = [e for e in self._elements if e.quand > maintenant]
                    self._sauver()
            for el in dus:
                try:
                    self._declencher(el)
                except Exception as e:
                    log.error("Erreur pendant le déclenchement de %s : %s", el.description(), e)

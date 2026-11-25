"""Briefing de Jarvis : rassemble les faits du jour (météo, agenda, rappels).

Les faits sont ensuite reformulés par l'IA en quelques phrases naturelles (voir jarvis.py).
- Avant 17 h : la journée (ce qui reste à venir).
- À partir de 17 h : la soirée et le programme de demain.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

log = logging.getLogger("clap_listen")

JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre")
HEURE_DU_SOIR = 17


def faits_du_jour(agenda=None, rappels=None, maintenant: datetime | None = None) -> str:
    """Texte brut pour l'IA. Chaque source est facultative : une panne n'empêche pas le reste."""
    m = maintenant or datetime.now()
    soir = m.hour >= HEURE_DU_SOIR
    faits = [f"Moment : {'soirée' if soir else 'journée'} du {JOURS[m.weekday()]} {m.day} "
             f"{MOIS[m.month - 1]}, il est {m:%H:%M}."]

    try:
        from meteo import meteo_texte

        faits.append(meteo_texte())
    except Exception as e:
        log.warning("Briefing : météo indisponible (%s).", e)

    if agenda is not None:
        aujourd_hui = m.date().isoformat()
        try:
            faits.append("Agenda, reste de la journée : " + agenda.evenements(f"{aujourd_hui}T{m:%H:%M}", aujourd_hui))
            if soir:
                demain = (m.date() + timedelta(days=1)).isoformat()
                faits.append("Agenda de demain : " + agenda.evenements(demain))
        except Exception as e:
            log.warning("Briefing : agenda indisponible (%s).", e)

    if rappels is not None:
        try:
            faits.append("Minuteurs, alarmes et rappels : " + rappels.lister())
        except Exception as e:
            log.warning("Briefing : rappels indisponibles (%s).", e)

    return "\n".join(faits)

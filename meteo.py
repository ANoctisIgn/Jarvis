"""Météo pour Jarvis, via Open-Meteo (gratuit, sans clé ni compte).

Ville par défaut : METEO_VILLE dans .env (par défaut Valence, France).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date, datetime

import requests

log = logging.getLogger("clap_listen")

URL_PREVISIONS = "https://api.open-meteo.com/v1/forecast"
URL_GEOCODAGE = "https://geocoding-api.open-meteo.com/v1/search"
CACHE_S = 600            # on garde une prévision 10 minutes
SEUIL_PLUIE = 50         # % : au-dessus, on considère qu'il risque de pleuvoir à cette heure

JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre")

# Codes météo WMO -> français
CODES = {
    0: "ciel dégagé", 1: "plutôt dégagé", 2: "partiellement nuageux", 3: "couvert",
    45: "brouillard", 48: "brouillard givrant",
    51: "bruine légère", 53: "bruine", 55: "bruine dense", 56: "bruine verglaçante", 57: "bruine verglaçante",
    61: "pluie faible", 63: "pluie", 65: "forte pluie", 66: "pluie verglaçante", 67: "forte pluie verglaçante",
    71: "neige faible", 73: "neige", 75: "forte neige", 77: "grains de neige",
    80: "quelques averses", 81: "averses", 82: "violentes averses", 85: "averses de neige", 86: "fortes averses de neige",
    95: "orage", 96: "orage avec grêle", 99: "violent orage avec grêle",
}

_verrou = threading.Lock()
_cache_lieux: dict[str, tuple[str, float, float]] = {}
_cache_previsions: dict[tuple[float, float], tuple[float, dict]] = {}


def _decrire(code) -> str:
    return CODES.get(int(code or 0), "temps variable")


def _jour_en_mots(d: date) -> str:
    ecart = (d - date.today()).days
    if ecart == 0:
        return "aujourd'hui"
    if ecart == 1:
        return "demain"
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]}"


# --- lieu -------------------------------------------------------------------------

def trouver_lieu(ville: str = "") -> tuple[str, float, float] | None:
    """(nom, latitude, longitude). Sans ville : celle de .env (METEO_VILLE), Valence par défaut."""
    par_defaut = not ville.strip()
    if par_defaut:
        lat, lon = os.environ.get("METEO_LATITUDE"), os.environ.get("METEO_LONGITUDE")
        if lat and lon:
            return (os.environ.get("METEO_VILLE") or "chez vous", float(lat), float(lon))
        ville = os.environ.get("METEO_VILLE") or "Valence"
    cle = ville.strip().lower()
    if cle in _cache_lieux:
        return _cache_lieux[cle]
    try:
        r = requests.get(URL_GEOCODAGE, params={"name": ville, "count": 10, "language": "fr"}, timeout=8)
        resultats = r.json().get("results") or []
    except (requests.RequestException, ValueError) as e:
        log.warning("Météo : géocodage impossible (%s).", e)
        return None
    if not resultats:
        return None
    # « Valence » : la plus peuplée est en Espagne. On préfère une ville française du même nom.
    pays = (os.environ.get("METEO_PAYS") or "FR").upper()
    meme_nom = [x for x in resultats if x["name"].lower() == cle and x.get("country_code") == pays]
    choix = max(meme_nom, key=lambda x: x.get("population") or 0) if meme_nom else resultats[0]
    lieu = (choix["name"], choix["latitude"], choix["longitude"])
    _cache_lieux[cle] = lieu
    return lieu


def _previsions(lat: float, lon: float) -> dict | None:
    cle = (round(lat, 3), round(lon, 3))
    with _verrou:
        en_cache = _cache_previsions.get(cle)
        if en_cache and time.monotonic() - en_cache[0] < CACHE_S:
            return en_cache[1]
    try:
        r = requests.get(URL_PREVISIONS, timeout=8, params={
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 7,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "hourly": "temperature_2m,precipitation_probability,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                     "precipitation_sum,wind_speed_10m_max",
        })
        donnees = r.json()
        if "daily" not in donnees:
            raise ValueError(donnees.get("reason", "réponse inattendue"))
    except (requests.RequestException, ValueError) as e:
        log.warning("Météo : prévisions indisponibles (%s).", e)
        return None
    with _verrou:
        _cache_previsions[cle] = (time.monotonic(), donnees)
    return donnees


# --- texte ----------------------------------------------------------------------------

def _plages_pluie(donnees: dict, jour: date, a_partir_de: datetime | None = None) -> list[str]:
    """Plages horaires où la pluie est probable ce jour-là, ex. ['de 15 h à 19 h (80 %)']."""
    h = donnees["hourly"]
    heures = []
    for t, p in zip(h["time"], h["precipitation_probability"]):
        moment = datetime.fromisoformat(t)
        if moment.date() != jour or p is None:
            continue
        if a_partir_de and moment.hour < a_partir_de.hour:
            continue
        heures.append((moment.hour, p))
    plages, debut, maxi, precedente = [], None, 0, None
    for heure, p in heures + [(99, 0)]:
        if p >= SEUIL_PLUIE:
            if debut is None:
                debut, maxi = heure, p
            maxi = max(maxi, p)
            precedente = heure
        elif debut is not None:
            fin = precedente + 1
            plages.append(f"de {debut} h à {'minuit' if fin >= 24 else f'{fin} h'} ({maxi} %)")
            debut = None
    return plages


def _resume_jour(donnees: dict, i: int, a_partir_de: datetime | None = None) -> str:
    d = donnees["daily"]
    jour = date.fromisoformat(d["time"][i])
    texte = (f"{_jour_en_mots(jour)} : {_decrire(d['weather_code'][i])}, "
             f"de {round(d['temperature_2m_min'][i])} à {round(d['temperature_2m_max'][i])} °C")
    vent = d.get("wind_speed_10m_max", [None] * 7)[i]
    if vent and vent >= 40:
        texte += f", vent fort jusqu'à {round(vent)} km/h"
    plages = _plages_pluie(donnees, jour, a_partir_de)
    if plages:
        texte += ", pluie probable " + " et ".join(plages)
    elif (d["precipitation_probability_max"][i] or 0) < 20:
        texte += ", pas de pluie prévue"
    return texte


def meteo_texte(ville: str = "", date_iso: str = "") -> str:
    """Résumé météo lisible par l'IA : maintenant + reste de la journée + demain, ou un jour précis."""
    lieu = trouver_lieu(ville)
    if lieu is None:
        return f"Erreur : ville « {ville} » introuvable." if ville else "Erreur : météo indisponible."
    nom, lat, lon = lieu
    donnees = _previsions(lat, lon)
    if donnees is None:
        return "Erreur : le service météo ne répond pas."
    jours = donnees["daily"]["time"]

    if date_iso and date_iso[:10] != jours[0]:
        if date_iso[:10] not in jours:
            return f"Erreur : prévisions disponibles seulement jusqu'au {jours[-1]}."
        return f"Météo à {nom}, {_resume_jour(donnees, jours.index(date_iso[:10]))}."

    c = donnees["current"]
    maintenant = datetime.fromisoformat(c["time"])
    actuel = (f"Météo à {nom}. Actuellement {round(c['temperature_2m'])} °C"
              + (f", ressenti {round(c['apparent_temperature'])} °C"
                 if abs(c["apparent_temperature"] - c["temperature_2m"]) >= 2 else "")
              + f", {_decrire(c['weather_code'])}.")
    return (f"{actuel} Prévisions {_resume_jour(donnees, 0, a_partir_de=maintenant)}. "
            f"Prévisions {_resume_jour(donnees, 1)}.")


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    print(meteo_texte(*sys.argv[1:3]))

"""Diagnostic du micro et de la transcription.

Lancer :  python diagnostic_micro.py
Lisez à voix haute les phrases affichées, comme vous parleriez à Jarvis. Le diagnostic montre ce que
Jarvis a compris, le niveau de votre micro, et enregistre tout dans donnees/diagnostic/ (rapport.txt
et un fichier .wav par phrase) pour pouvoir analyser les erreurs.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import unicodedata
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv
from groq import Groq

RACINE = Path(__file__).resolve().parent
load_dotenv(RACINE / ".env")
logging.basicConfig(level=logging.WARNING)

from ecoute import TAUX, Ecoute, normaliser_volume, transcrire  # noqa: E402

PHRASES = [
    "Mets Get Lucky de Daft Punk.",
    "Mets un minuteur de dix minutes pour les pâtes.",
    "Qu'est-ce que j'ai demain dans mon agenda ?",
    "Rappelle-moi d'appeler Paul dans vingt minutes.",
    "Est-ce qu'il va pleuvoir cet après-midi ?",
    "Ouvre Discord et baisse le son.",
    "Lance la playlist Top 50 France en aléatoire.",
    "C'est quoi la capitale de l'Australie ?",
]


def _mots(texte: str) -> list[str]:
    sans_accents = unicodedata.normalize("NFD", texte.lower()).encode("ascii", "ignore").decode()
    mots = re.findall(r"[a-z0-9]+", sans_accents)
    nombres = {"dix": "10", "vingt": "20"}
    return [nombres.get(m, m) for m in mots]


def _erreurs(reference: str, compris: str) -> tuple[int, int]:
    r, h = _mots(reference), _mots(compris)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        precedent, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            precedent, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, precedent + (r[i - 1] != h[j - 1]))
    return d[-1], len(r)


def _dbfs(valeur: float) -> float:
    return 20 * np.log10(max(valeur, 1) / 32768)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    dossier = RACINE / "donnees" / "diagnostic"
    dossier.mkdir(parents=True, exist_ok=True)
    client = Groq()
    micro = os.environ.get("JARVIS_INPUT_DEVICE")
    if micro:
        from claquements import trouver_micro

        micro = trouver_micro(micro)
    nom_micro = sd.query_devices(micro if micro is not None else sd.default.device[0], "input")["name"]

    print(f"\nMicro utilisé : {nom_micro}")
    print("Coupez la musique. Pour chaque phrase, lisez-la à voix haute quand « À vous » s'affiche.\n")
    ecoute = Ecoute(device=micro)
    ecoute.reveil_actif = False  # pas de mot de réveil pendant le diagnostic

    lignes, total_err, total_mots, pics, niveaux = [], 0, 0, [], []
    for i, phrase in enumerate(PHRASES, start=1):
        input(f"[{i}/{len(PHRASES)}] Appuyez sur Entrée, puis lisez : « {phrase} »")
        print("    À vous...")
        pcm = ecoute.ecouter_phrase(delai_debut=8.0)
        if pcm is None:
            print("    Rien entendu.\n")
            lignes.append(f"{i}. « {phrase} » -> RIEN ENTENDU")
            continue
        audio = np.frombuffer(pcm, np.int16).astype(np.float32)
        pic, niveau = _dbfs(float(np.max(np.abs(audio)))), _dbfs(float(np.sqrt(np.mean(audio ** 2))))
        pics.append(pic)
        niveaux.append(niveau)
        compris = transcrire(pcm, client)
        err, n = _erreurs(phrase, compris)
        total_err, total_mots = total_err + err, total_mots + n
        with wave.open(str(dossier / f"phrase_{i}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(TAUX)
            w.writeframes(normaliser_volume(pcm))
        etat = "OK" if err == 0 else f"{err} mot(s) faux"
        print(f"    Compris : « {compris} »  [{etat}] (volume : pic {pic:.0f} dB, moyen {niveau:.0f} dB, "
              f"{len(audio) / TAUX:.1f} s)\n")
        lignes.append(f"{i}. « {phrase} » -> « {compris} » [{etat}] pic {pic:.0f} dB, moyen {niveau:.0f} dB, "
                      f"{len(audio) / TAUX:.1f} s")

    print("=" * 70)
    resume = [f"Diagnostic du {datetime.now():%d/%m/%Y %H:%M}", f"Micro : {nom_micro}"]
    if total_mots:
        resume.append(f"Mots mal compris : {total_err}/{total_mots} ({100 * total_err / total_mots:.0f} %)")
    if pics:
        pic_moyen = float(np.mean(pics))
        resume.append(f"Volume du micro : pic moyen {pic_moyen:.0f} dB (idéal entre -12 et -3 dB)")
        if pic_moyen < -25:
            resume.append("-> Micro TRÈS faible : montez le volume d'entrée (Paramètres Windows > Son > "
                          "Entrée > Volume), ou rapprochez-vous du micro.")
        elif pic_moyen < -15:
            resume.append("-> Micro un peu faible : montez le volume d'entrée dans les paramètres Windows.")
        elif pic_moyen > -1:
            resume.append("-> Micro trop fort (saturation possible) : baissez le volume d'entrée.")
    print("\n".join(resume))
    (dossier / "rapport.txt").write_text("\n".join(resume + [""] + lignes) + "\n", encoding="utf-8")
    print(f"\nRapport et enregistrements : {dossier}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

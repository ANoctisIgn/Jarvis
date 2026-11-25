"""Écoute de Jarvis : un seul flux micro pour tout.

- Mot de réveil 100 % local, rien ne part sur internet avant :
    * « Jarvis » tout court, prononcé à la française : reconnaissance vocale Vosk (modèle français)
      limitée à quelques mots ;
    * « Hey Jarvis » à l'anglaise : openWakeWord (modèle « hey_jarvis »).
  Le premier des deux qui entend réveille Jarvis.
- Détection de la parole (Silero VAD) : on sait quand la phrase commence et se termine.
- Transcription via Groq Whisper (whisper-large-v3-turbo), avec Google en secours.
"""

from __future__ import annotations

import io
import json
import logging
import queue
import re
import threading
import time
import wave
from collections import deque

import numpy as np
import sounddevice as sd

log = logging.getLogger("clap_listen")

TAUX = 16000          # openWakeWord / Silero / Whisper travaillent en 16 kHz
TRAME = 1280          # 80 ms : taille de trame attendue par openWakeWord
DUREE_TRAME = TRAME / TAUX

# Mots proposés à Vosk : « jarvis » + des mots qui lui ressemblent (« leurres »). Sans leurres, Vosk
# ramène tout ce qui ressemble à « jarvis » vers « jarvis » (« j'avais », « Gervais »...) : faux réveils.
VOSK_MOTS = ["jarvis", "j'avais", "avais", "gervais", "charlie", "jar", "service", "jaugé", "arrive",
             "j'arrive", "garde", "par", "jarre", "javel", "jaguar", "chargé", "jardin", "[unk]"]
# Mots acceptés comme réveil quand ils sont dits SEULS (« Jarvis. » est souvent entendu « j'avais »).
VOSK_MOTS_SEULS = {"jarvis", "j'avais"}
VOSK_CONFIANCE = 0.6
# Pendant que Jarvis parle, ses haut-parleurs passent dans le micro : seul un « Jarvis » très net
# (confiance >= 0,95, en tête de phrase) l'interrompt, et la règle « mot seul » est désactivée.
VOSK_CONFIANCE_PENDANT_PAROLE = 0.95

# Transcription (Groq Whisper). large-v3 : nettement plus juste en français que la version turbo
# (testé : 17,7 % -> 3,1 % de mots faux avec une voix faible et du bruit), aussi rapide sur Groq.
MODELES_TRANSCRIPTION = ("whisper-large-v3", "whisper-large-v3-turbo")   # le 2e sert de secours
# Contexte donné à Whisper : il reconnaît mieux les mots qu'il s'attend à entendre.
INDICE_TRANSCRIPTION = ("Jarvis, mets de la musique sur Spotify, ouvre Discord, mets un minuteur, rappelle-moi, "
                        "qu'est-ce que j'ai dans mon agenda, quel temps fait-il.")

# Phrases que Whisper « invente » parfois sur du silence ou du bruit.
_HALLUCINATIONS_WHISPER = (
    "sous-titres réalisés",
    "sous-titrage",
    "radio-canada",
    "amara.org",
    "merci d'avoir regardé",
    "abonnez-vous",
    "sous-titres par",
)
# Phrases courtes que Whisper invente sur un souffle ou un bruit. Ignorées dès que Whisper lui-même
# doute qu'il y ait eu de la parole (votre vraie voix donne une probabilité « pas de parole » de 0).
_HALLUCINATIONS_COURTES = {"merci", "merci beaucoup", "merci a tous", "au revoir", "bonne journee",
                           "a bientot", "bonne soiree", "salut"}
# Un son doit contenir au moins cette durée de voix pour compter comme une phrase (vos phrases : 1,3 s
# et plus). En dessous (fin de la voix de Jarvis, toux...), on ne l'envoie pas à Whisper.
VOIX_MIN_S = 0.3


class Ecoute:
    """Lit le micro en continu dans un thread et expose le mot de réveil + la capture de phrases."""

    def __init__(
        self,
        device: int | None = None,
        modele_reveil: str = "hey_jarvis",
        seuil_reveil: float = 0.5,
        seuil_voix: float = 0.5,
        modele_vosk: str | None = None,
        jarvis_parle=None,
    ) -> None:
        from openwakeword.model import Model
        from openwakeword.utils import download_models
        from openwakeword.vad import VAD

        try:
            self._oww = Model(wakeword_models=[modele_reveil], inference_framework="onnx")
        except Exception:
            log.info("Téléchargement du modèle de mot de réveil « %s »...", modele_reveil)
            download_models(model_names=[modele_reveil])
            self._oww = Model(wakeword_models=[modele_reveil], inference_framework="onnx")
        self._nom_modele = next(iter(self._oww.models))
        self._vad = VAD()

        self.seuil_reveil = seuil_reveil
        self.seuil_voix = seuil_voix
        self.reveil = threading.Event()        # posé quand le mot de réveil est entendu
        self.reveil_actif = True               # False = on ignore le mot de réveil
        self._dernier_reveil = 0.0

        self._brut: queue.Queue[np.ndarray] = queue.Queue(maxsize=400)
        self._capture: queue.Queue[np.ndarray] = queue.Queue(maxsize=400)
        self._capture_active = False
        self._lock = threading.Lock()

        self._vosk_modele = None
        if modele_vosk:
            try:
                import vosk

                vosk.SetLogLevel(-1)
                self._vosk_modele = vosk.Model(modele_vosk)
                self._nouveau_vosk()
                log.info("Mot de réveil « Jarvis » (Vosk) activé.")
            except Exception as e:
                log.warning("Vosk indisponible (%s) : seul « Hey Jarvis » réveille Jarvis.", e)
                self._vosk_modele = None

        # Fonction qui dit si Jarvis est en train de parler (voir voix.verrou_parole).
        self._jarvis_parle = jarvis_parle or (lambda: False)
        self._parlait = False

        self._flux = self._ouvrir_flux(device)
        self._flux.start()
        threading.Thread(target=self._boucle, name="ecoute", daemon=True).start()

    # --- micro ----------------------------------------------------------------

    def _ouvrir_flux(self, device: int | None) -> sd.InputStream:
        """Ouvre le micro en 16 kHz ; sinon au taux natif avec rééchantillonnage."""
        try:
            self._facteur = 1
            return sd.InputStream(
                device=device, samplerate=TAUX, channels=1, dtype="int16",
                blocksize=TRAME, callback=self._rappel,
            )
        except sd.PortAudioError:
            natif = int(sd.query_devices(device, "input")["default_samplerate"])
            if natif % TAUX:
                natif = 48000
            self._facteur = natif // TAUX
            log.info("Micro ouvert à %d Hz (rééchantillonné en 16 kHz).", natif)
            return sd.InputStream(
                device=device, samplerate=natif, channels=1, dtype="int16",
                blocksize=TRAME * self._facteur, callback=self._rappel,
            )

    def _rappel(self, indata, _frames, _time, status) -> None:
        if status:
            log.debug("Micro : %s", status)
        try:
            self._brut.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass  # le thread d'analyse a pris du retard : on lâche une trame

    def _boucle(self) -> None:
        if self._facteur > 1:
            from scipy.signal import resample_poly
        while True:
            trame = self._brut.get()
            if self._facteur > 1:
                trame = resample_poly(trame.astype(np.float32), 1, self._facteur)
                trame = np.clip(trame, -32768, 32767).astype(np.int16)
            score = float(self._oww.predict(trame)[self._nom_modele])
            pret = self.reveil_actif and time.monotonic() - self._dernier_reveil > 1.5
            if pret and score >= self.seuil_reveil:
                log.info("Mot de réveil « Hey Jarvis » détecté (score %.2f).", score)
                self._declencher(None)
                continue
            # Vosk n'écoute que hors capture (pendant une demande, on ne cherche pas « Jarvis »).
            parle = bool(self._jarvis_parle())
            if self._vosk_modele is not None and self._parlait and not parle:
                self._nouveau_vosk()  # Jarvis vient de se taire : on oublie ce qui a été entendu de sa voix
            self._parlait = parle
            if self._vosk_modele is not None and not self._capture_active:
                resultat = self._alimenter_vosk(trame, strict=parle)
                if resultat is not None and pret:
                    nature, commande = resultat
                    log.info("Mot de réveil « Jarvis » détecté (%s).", nature)
                    self._declencher(commande)
                    continue
            with self._lock:
                if self._capture_active:
                    try:
                        self._capture.put_nowait(trame)
                    except queue.Full:
                        pass

    def _declencher(self, debut_demande: np.ndarray | None) -> None:
        """Réveil. On capture tout de suite : l'utilisateur enchaîne souvent après le mot de réveil.
        `debut_demande` : ce qui a déjà été dit après « Jarvis » (« Jarvis, quelle heure... »), remis en
        tête de la capture ; la suite et la fin de la phrase sont gérées par ecouter_phrase."""
        self._dernier_reveil = time.monotonic()
        if self._vosk_modele is not None:
            self._nouveau_vosk()
        self._demarrer_capture(vider=True)
        if debut_demande is not None and len(debut_demande):
            # Morceaux de TRAME échantillons pile (le détecteur de voix plante sur un morceau trop court).
            reste = len(debut_demande) % TRAME
            if reste:
                debut_demande = np.concatenate([debut_demande, np.zeros(TRAME - reste, np.int16)])
            with self._lock:
                for i in range(0, len(debut_demande), TRAME):
                    try:
                        self._capture.put_nowait(debut_demande[i:i + TRAME])
                    except queue.Full:
                        break
        self.reveil.set()

    # --- Vosk : « Jarvis » tout court ---

    def _nouveau_vosk(self) -> None:
        import vosk

        self._vosk = vosk.KaldiRecognizer(self._vosk_modele, TAUX, json.dumps(VOSK_MOTS))
        self._vosk.SetWords(True)
        self._vosk_echantillons = 0            # échantillons donnés à ce reconnaisseur
        self._vosk_historique: deque[tuple[int, np.ndarray]] = deque(maxlen=250)   # ~20 s d'audio
        self._vosk_max_mots = 0                # plus long résultat intermédiaire de la phrase en cours

    def _alimenter_vosk(self, trame: np.ndarray, strict: bool = False) -> tuple[str, np.ndarray | None] | None:
        """Donne une trame à Vosk. Si « Jarvis » est entendu, renvoie (nature, audio dit après « Jarvis »). On ne décide qu'à la fin d'une phrase (vue par Vosk) : les résultats
        intermédiaires se trompent (« jar de confiture » passe un instant par « jarvis »)."""
        self._vosk_historique.append((self._vosk_echantillons, trame))
        self._vosk_echantillons += len(trame)
        if not self._vosk.AcceptWaveform(trame.tobytes()):
            partiel = json.loads(self._vosk.PartialResult()).get("partial", "").split()
            self._vosk_max_mots = max(self._vosk_max_mots, len(partiel))
            return None
        mots = json.loads(self._vosk.Result()).get("result", [])
        max_mots, self._vosk_max_mots = self._vosk_max_mots, 0
        if strict:
            # Jarvis parle : « Jarvis » très net en tête, et rien de ce qui suit n'est gardé (c'est sa voix).
            if mots and mots[0]["word"] == "jarvis" and mots[0]["conf"] >= VOSK_CONFIANCE_PENDANT_PAROLE:
                return "pendant que Jarvis parle", None
            return None
        # « Jarvis » en tête (éventuellement après un petit mot : « ok Jarvis », « hé Jarvis »)
        for i, mot in enumerate(mots[:2]):
            if mot["word"] != "jarvis" or mot["conf"] < VOSK_CONFIANCE:
                continue
            if i == 1 and mots[0]["end"] - mots[0]["start"] > 0.6:
                break
            # Tout ce qui suit « Jarvis » jusqu'à maintenant : Vosk valide parfois « Jarvis » avec un peu de
            # retard, ou coupe en pleine demande. La fin de la demande est attendue par ecouter_phrase.
            nature = "avec demande" if len(mots) > i + 1 else "seul"
            return nature, self._extraire(mot["end"], self._vosk_echantillons / TAUX)
        # « Jarvis. » dit seul, souvent entendu « j'avais » : un seul mot, sans rien autour
        if len(mots) == 1 and mots[0]["word"] in VOSK_MOTS_SEULS and max_mots <= 1:
            return "seul", self._extraire(mots[0]["end"], self._vosk_echantillons / TAUX)
        return None

    def _extraire(self, debut_s: float, fin_s: float) -> np.ndarray:
        """Audio entre deux instants (en secondes depuis le début du reconnaisseur Vosk)."""
        a, b = int(debut_s * TAUX), int(fin_s * TAUX)
        morceaux = [(d, t) for d, t in self._vosk_historique if d + len(t) > a and d < b]
        if not morceaux:
            return np.zeros(0, np.int16)
        premier = morceaux[0][0]
        audio = np.concatenate([t for _, t in morceaux])
        return audio[max(0, a - premier):max(0, b - premier)]

    def _demarrer_capture(self, vider: bool) -> None:
        with self._lock:
            if vider:
                while not self._capture.empty():
                    self._capture.get_nowait()
            self._capture_active = True

    def _arreter_capture(self) -> None:
        with self._lock:
            self._capture_active = False

    # --- API ------------------------------------------------------------------

    def reveiller(self) -> None:
        """Réveil sans mot de réveil (touche Jarvis) : même effet que « Jarvis »."""
        self._dernier_reveil = time.monotonic()
        self._demarrer_capture(vider=True)
        self.reveil.set()

    def attendre_reveil(self, timeout: float | None = None) -> bool:
        ok = self.reveil.wait(timeout)
        if ok:
            self.reveil.clear()
        return ok

    def ecouter_phrase(
        self,
        delai_debut: float = 5.0,
        silence_fin: float = 1.0,
        duree_max: float = 12.0,
        deja_en_cours: bool = False,
        ignorer_debut: float = 0.0,
    ) -> bytes | None:
        """Enregistre une phrase et renvoie l'audio PCM 16 bits / 16 kHz, ou None si personne ne parle
        pendant `delai_debut` secondes.

        `deja_en_cours=True` : la capture a été lancée par le mot de réveil, on garde ce qui a déjà été dit.
        `ignorer_debut` : secondes ignorées au début (juste après que Jarvis a parlé, la fin de sa voix
        est encore dans la pièce et serait prise pour vous).
        """
        if not deja_en_cours:
            self._demarrer_capture(vider=True)
        self._vad.reset_states()
        avant: deque[np.ndarray] = deque(maxlen=8)   # ~0,6 s avant la parole, pour ne pas couper le début
        morceaux: list[np.ndarray] = []
        parle = False
        silence = 0.0
        voix_totale = 0.0
        debut = time.monotonic()
        try:
            while True:
                try:
                    trame = self._capture.get(timeout=0.5)
                except queue.Empty:
                    trame = None
                if self.reveil.is_set():
                    return None  # nouveau « Jarvis » pendant l'écoute : on laisse l'appelant repartir
                ecoule = time.monotonic() - debut
                if trame is None:
                    if not parle and ecoule > delai_debut:
                        return None
                    continue
                if ecoule < ignorer_debut:
                    continue
                voix = float(self._vad.predict(trame, frame_size=640)) >= self.seuil_voix
                if not parle:
                    avant.append(trame)
                    if voix:
                        parle = True
                        voix_totale = DUREE_TRAME
                        morceaux.extend(avant)
                    elif ecoule > delai_debut:
                        return None
                    continue
                morceaux.append(trame)
                if voix:
                    voix_totale += DUREE_TRAME
                silence = 0.0 if voix else silence + DUREE_TRAME
                if silence >= silence_fin or len(morceaux) * DUREE_TRAME >= duree_max:
                    if voix_totale >= VOIX_MIN_S:
                        break
                    # Trop peu de voix : un bruit, pas une phrase. On continue d'attendre (sans Whisper).
                    log.debug("Son trop court ignoré (%.2f s de voix).", voix_totale)
                    parle, morceaux, silence, voix_totale = False, [], 0.0, 0.0
                    avant.clear()
                    if ecoule > delai_debut:
                        return None
        finally:
            # Si « Hey Jarvis » vient d'être redit, la capture qu'il a lancée doit continuer.
            if not self.reveil.is_set():
                self._arreter_capture()
        return np.concatenate(morceaux).tobytes()

    def annuler_capture(self) -> None:
        self._arreter_capture()


def pcm_vers_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(TAUX)
        wf.writeframes(pcm)
    return buf.getvalue()


def normaliser_volume(pcm: bytes) -> bytes:
    """Amplifie une voix captée faiblement (micro de PC portable) : pic ramené à 70 % du maximum."""
    audio = np.frombuffer(pcm, np.int16).astype(np.float32)
    pic = float(np.max(np.abs(audio))) if audio.size else 0.0
    if pic < 100:  # silence : rien à amplifier
        return pcm
    gain = min(0.7 * 32767 / pic, 8.0)  # plafonné : amplifier trop fort transforme un souffle en « parole »
    return np.clip(audio * gain, -32768, 32767).astype(np.int16).tobytes()


def _mots(texte: str) -> list[str]:
    import unicodedata

    return re.findall(r"[a-z0-9]+", unicodedata.normalize("NFD", texte.lower()).encode("ascii", "ignore").decode())


def transcrire(pcm: bytes, client_groq, indice: str = INDICE_TRANSCRIPTION) -> str:
    """Audio -> texte. Groq Whisper large-v3, puis turbo, puis Google en secours. "" si rien de compris."""
    wav = pcm_vers_wav(normaliser_volume(pcm))
    texte = None
    pas_de_parole: list[float] = []
    for modele in MODELES_TRANSCRIPTION:
        try:
            res = client_groq.audio.transcriptions.create(
                file=("commande.wav", wav), model=modele, language="fr",
                prompt=indice or None, temperature=0.0, response_format="verbose_json",
            )
            texte = (res.text or "").strip()
            segments = getattr(res, "segments", None) or (getattr(res, "model_extra", None) or {}).get("segments") or []
            pas_de_parole = [s["no_speech_prob"] if isinstance(s, dict) else s.no_speech_prob for s in segments]
            break
        except Exception as e:
            log.warning("Transcription %s indisponible (%s).", modele, e)
    if texte is None:
        try:
            import speech_recognition as sr

            texte = sr.Recognizer().recognize_google(sr.AudioData(pcm, TAUX, 2), language="fr-FR")
        except Exception as e2:
            log.warning("Transcription Google échouée : %s", e2)
            return ""
    bas = texte.lower()
    if any(h in bas for h in _HALLUCINATIONS_WHISPER):
        log.info("Transcription ignorée (hallucination Whisper) : %s", texte)
        return ""
    if pas_de_parole and min(pas_de_parole) > 0.5:
        log.info("Transcription ignorée (Whisper n'entend pas de parole) : %s", texte)
        return ""
    if " ".join(_mots(texte)) in _HALLUCINATIONS_COURTES and pas_de_parole and max(pas_de_parole) >= 0.02:
        log.info("Transcription ignorée (« %s » inventé sur un bruit, doute %.2f).", texte, max(pas_de_parole))
        return ""
    # Sur un audio peu clair, Whisper recrache parfois le contexte qu'on lui a donné.
    mots, mots_indice = _mots(texte), " ".join(_mots(indice))
    if len(mots) >= 4 and " ".join(mots) in mots_indice:
        log.info("Transcription ignorée (contexte recopié) : %s", texte)
        return ""
    return texte


def bip(montee: bool = True) -> None:
    """Petit son « je t'écoute » (montant) ou « j'arrête » (descendant), non bloquant."""
    taux = 24000
    notes = (880.0, 1320.0) if montee else (1320.0, 880.0)
    t = np.arange(int(taux * 0.07)) / taux
    enveloppe = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.01)  # fondu de 10 ms, pas de clic
    son = np.concatenate([0.25 * np.sin(2 * np.pi * f * t) * enveloppe for f in notes])
    try:
        sd.play(son.astype(np.float32), taux)
    except Exception as e:
        log.debug("Bip impossible : %s", e)

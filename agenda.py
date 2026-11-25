"""Agenda Google de Jarvis : lire, ajouter, modifier, supprimer (avec confirmation) et annoncer.

Première utilisation (une seule fois) :
  1. Placer le fichier OAuth « application de bureau » dans  donnees/credentials.json
  2. Lancer  python agenda.py  et cliquer sur « Autoriser » dans le navigateur.
L'autorisation est gardée dans donnees/token_google.json (sur ce PC uniquement).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

log = logging.getLogger("clap_listen")

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",  # liste des agendas
    "https://www.googleapis.com/auth/calendar.events",    # lire / écrire les événements
]
MAX_EVENEMENTS = 40
# Nom donné aux agendas abonnés par un lien .ics (ex. emploi du temps de l'école).
NOM_AGENDA_ABONNE = "cours"
DUREE_PAR_DEFAUT = timedelta(hours=1)

JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre")


# --- dates ------------------------------------------------------------------------

def _local(dt: datetime) -> datetime:
    """Date/heure avec le fuseau horaire de Windows."""
    return dt.astimezone() if dt.tzinfo else dt.replace(tzinfo=datetime.now().astimezone().tzinfo)


def lire_date(texte: str, fin_de_journee: bool = False) -> tuple[datetime, bool] | None:
    """« 2026-11-12T14:00 » ou « 2026-11-12 ». Renvoie (date/heure locale, était_une_date_seule)."""
    texte = (texte or "").strip().replace(" ", "T", 1)
    if not texte:
        return None
    try:
        if "T" in texte:
            return _local(datetime.fromisoformat(texte)), False
        d = date.fromisoformat(texte)
    except ValueError:
        return None
    heure = datetime.max.time().replace(microsecond=0) if fin_de_journee else datetime.min.time()
    return _local(datetime.combine(d, heure)), True


def jour_en_mots(d: date) -> str:
    ecart = (d - date.today()).days
    if ecart == 0:
        return "aujourd'hui"
    if ecart == 1:
        return "demain"
    if ecart == -1:
        return "hier"
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]}"


def heure_en_mots(dt: datetime) -> str:
    return f"{dt.hour} h" + (f" {dt.minute:02d}" if dt.minute else "")


# --- événements ---------------------------------------------------------------------

class Evenement:
    def __init__(self, brut: dict, agenda_id: str, agenda_nom: str) -> None:
        self.brut = brut
        self.id = brut["id"]
        self.agenda_id = agenda_id
        self.agenda_nom = agenda_nom
        self.titre = brut.get("summary") or "(sans titre)"
        self.lieu = brut.get("location") or ""
        debut, fin = brut["start"], brut["end"]
        self.journee = "date" in debut
        if self.journee:
            self.debut = _local(datetime.combine(date.fromisoformat(debut["date"]), datetime.min.time()))
            self.fin = _local(datetime.combine(date.fromisoformat(fin["date"]), datetime.min.time()))
        else:
            self.debut = _local(datetime.fromisoformat(debut["dateTime"]))
            self.fin = _local(datetime.fromisoformat(fin["dateTime"]))

    def description(self, avec_agenda: bool = False) -> str:
        jour = jour_en_mots(self.debut.date())
        if self.journee:
            nb_jours = (self.fin.date() - self.debut.date()).days
            quand = f"{jour}, toute la journée" + (f" pendant {nb_jours} jours" if nb_jours > 1 else "")
        elif self.debut.date() == self.fin.date():
            quand = f"{jour} de {heure_en_mots(self.debut)} à {heure_en_mots(self.fin)}"
        else:
            quand = f"{jour} à {heure_en_mots(self.debut)} jusqu'au {jour_en_mots(self.fin.date())} {heure_en_mots(self.fin)}"
        texte = f"{quand} : {self.titre}"
        if self.lieu:
            texte += f", lieu : {self.lieu}"
        if avec_agenda and self.agenda_nom:
            texte += f" (agenda {self.agenda_nom})"
        return texte


class Agenda:
    def __init__(self, dossier: Path, annoncer: Callable[[str], object] | None = None,
                 minutes_annonce: int = 10) -> None:
        self._dossier = dossier
        self._annoncer = annoncer
        self._minutes_annonce = minutes_annonce
        self._lock = threading.Lock()          # le client Google n'est pas thread-safe
        self._service = None
        self._agendas: list[tuple[str, str]] = []
        self._agendas_lus = 0.0
        self._refs: dict[int, Evenement] = {}   # numéros donnés à l'IA lors de la dernière liste
        self._tour = 0                          # numéro de la demande vocale en cours
        self._en_attente: dict | None = None    # suppression / modification à confirmer
        self._oui = False                       # la dernière demande est-elle un « oui » ?
        self._question_precedente = ""          # dernière phrase de Jarvis (la question posée)

        creds = self._identifiants(interactif=False)
        if creds is None:
            log.warning("Agenda Google non connecté : lancez une fois  python agenda.py  (voir README).")
            return
        self._service = self._construire(creds)
        log.info("Agenda Google connecté.")
        if annoncer and minutes_annonce > 0:
            threading.Thread(target=self._boucle_annonces, name="agenda", daemon=True).start()

    # --- connexion ---

    @property
    def fichier_identifiants(self) -> Path:
        return self._dossier / "credentials.json"

    @property
    def fichier_jeton(self) -> Path:
        return self._dossier / "token_google.json"

    def _identifiants(self, interactif: bool):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = None
        if self.fichier_jeton.is_file():
            try:
                creds = Credentials.from_authorized_user_file(str(self.fichier_jeton), SCOPES)
            except ValueError:
                creds = None
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._sauver_jeton(creds)
                return creds
            except Exception as e:
                log.warning("Autorisation Google expirée (%s).", e)
        if not interactif:
            return None
        if not self.fichier_identifiants.is_file():
            raise FileNotFoundError(f"Fichier introuvable : {self.fichier_identifiants}")
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(str(self.fichier_identifiants), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
        self._sauver_jeton(creds)
        return creds

    def _sauver_jeton(self, creds) -> None:
        self._dossier.mkdir(parents=True, exist_ok=True)
        self.fichier_jeton.write_text(creds.to_json(), encoding="utf-8")

    @staticmethod
    def _construire(creds):
        from googleapiclient.discovery import build

        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    @property
    def disponible(self) -> bool:
        return self._service is not None

    def _erreur_si_indisponible(self) -> str | None:
        if not self.disponible:
            return ("Erreur : l'agenda Google n'est pas connecté. L'utilisateur doit lancer une fois "
                    "« python agenda.py » (voir le README).")
        return None

    # --- lecture ---

    def _liste_agendas(self) -> list[tuple[str, str]]:
        if not self._agendas or time.monotonic() - self._agendas_lus > 3600:
            items = self._service.calendarList().list().execute().get("items", [])
            self._agendas = [
                (a["id"], _nom_agenda(a))
                for a in items
                if a.get("selected", True) and not a.get("hidden")
            ]
            self._agendas_lus = time.monotonic()
        return self._agendas

    def _evenements(self, debut: datetime, fin: datetime) -> list[Evenement]:
        resultats: list[Evenement] = []
        with self._lock:
            for agenda_id, nom in self._liste_agendas():
                try:
                    items = self._service.events().list(
                        calendarId=agenda_id,
                        timeMin=debut.isoformat(),
                        timeMax=fin.isoformat(),
                        singleEvents=True,
                        orderBy="startTime",
                        maxResults=MAX_EVENEMENTS,
                    ).execute().get("items", [])
                except Exception as e:
                    log.warning("Agenda %s illisible : %s", nom or agenda_id, e)
                    continue
                for brut in items:
                    if brut.get("status") == "cancelled":
                        continue
                    try:
                        resultats.append(Evenement(brut, agenda_id, nom))
                    except (KeyError, ValueError):
                        continue
        resultats.sort(key=lambda e: (e.debut, not e.journee))
        return resultats[:MAX_EVENEMENTS]

    def evenements(self, debut: str, fin: str = "") -> str:
        if erreur := self._erreur_si_indisponible():
            return erreur
        d = lire_date(debut)
        if d is None:
            return "Erreur : début invalide, format attendu AAAA-MM-JJ ou AAAA-MM-JJTHH:MM."
        f = lire_date(fin or debut[:10], fin_de_journee=True)
        if f is None:
            return "Erreur : fin invalide."
        fin_dt = f[0]
        if fin_dt <= d[0]:  # « suis-je libre à 14 h » demandé sur 14:00-14:00 : on regarde l'heure
            fin_dt = d[0] + timedelta(hours=1)
        evs = self._evenements(d[0], fin_dt)
        self._refs = {i: e for i, e in enumerate(evs, start=1)}
        if not evs:
            return "Aucun événement sur cette période."
        plusieurs_agendas = len({e.agenda_id for e in evs}) > 1
        lignes = [f"numéro {i} : {e.description(plusieurs_agendas)}" for i, e in self._refs.items()]
        return f"{len(evs)} événement(s) : " + " ; ".join(lignes) + "."

    # --- écriture ---

    def ajouter(self, titre: str, debut: str, fin: str = "", lieu: str = "",
                journee_entiere: bool = False) -> str:
        if erreur := self._erreur_si_indisponible():
            return erreur
        d = lire_date(debut)
        if d is None:
            return "Erreur : début invalide, format attendu AAAA-MM-JJTHH:MM."
        corps: dict = {"summary": (titre or "Rendez-vous").strip()}
        if lieu:
            corps["location"] = lieu.strip()
        if journee_entiere or d[1]:
            jour_fin = lire_date(fin)[0].date() if fin and lire_date(fin) else d[0].date()
            corps["start"] = {"date": d[0].date().isoformat()}
            corps["end"] = {"date": (jour_fin + timedelta(days=1)).isoformat()}
        else:
            f = lire_date(fin) if fin else None
            fin_dt = f[0] if f and not f[1] else d[0] + DUREE_PAR_DEFAUT
            if fin_dt <= d[0]:
                fin_dt = d[0] + DUREE_PAR_DEFAUT
            corps["start"] = {"dateTime": d[0].isoformat()}
            corps["end"] = {"dateTime": fin_dt.isoformat()}
        with self._lock:
            brut = self._service.events().insert(calendarId="primary", body=corps).execute()
        ev = Evenement(brut, "primary", "")
        log.info("Agenda : ajouté %s", ev.description())
        reponse = f"Ajouté : {ev.description()}."
        conflits = [e for e in self._evenements(ev.debut, ev.fin) if e.id != ev.id and not e.journee]
        if conflits and not ev.journee:
            reponse += " Attention, ça chevauche : " + " ; ".join(e.description() for e in conflits) + "."
        return reponse

    def nouveau_tour(self, texte_utilisateur: str = "", dernier_message_jarvis: str = "") -> None:
        """À appeler à chaque nouvelle demande de l'utilisateur (sert à la confirmation)."""
        self._tour += 1
        self._oui = _est_un_oui(texte_utilisateur)
        self._question_precedente = (dernier_message_jarvis or "").lower()

    def _confirmation(self, action: str, numero, confirme: bool, details: dict | None = None):
        """Renvoie (événement, None) si l'action peut être exécutée, sinon (None, message pour l'IA).

        Sécurité : une suppression / modification n'est exécutée que si elle a été proposée lors
        d'une demande PRÉCÉDENTE et que l'utilisateur a répondu entre-temps. L'IA ne peut donc
        jamais confirmer toute seule dans la foulée.
        """
        try:
            ev = self._refs.get(int(numero))
        except (TypeError, ValueError):
            ev = None
        attente = self._en_attente
        # Cas 1 : l'action a été préparée lors d'une demande précédente, l'utilisateur a répondu depuis.
        if confirme and attente and attente["action"] == action and attente["tour"] < self._tour \
                and (ev is None or ev.id == attente["evenement"].id):
            self._en_attente = None
            return attente["evenement"], attente.get("details")
        # Cas 2 : l'IA a posé la question elle-même sans préparer l'action ; on accepte seulement si
        # l'utilisateur vient de dire oui ET que la question précédente de Jarvis nommait cet événement.
        if confirme and ev is not None and getattr(self, "_oui", False) \
                and ev.titre.lower() in getattr(self, "_question_precedente", ""):
            self._en_attente = None
            return ev, details
        if ev is None:
            return None, ("Erreur : numéro inconnu. Liste d'abord les événements avec agenda_evenements "
                          "pour obtenir le bon numéro.")
        self._en_attente = {"action": action, "evenement": ev, "details": details, "tour": self._tour}
        verbe = "supprimer" if action == "supprimer" else "modifier"
        return None, (f"CONFIRMATION REQUISE : demande à l'utilisateur s'il veut bien {verbe} "
                      f"« {ev.description()} »" + (f" ({_resume_changements(details)})" if details else "")
                      + ". N'appelle de nouveau cet outil avec confirme=true qu'après sa réponse positive.")

    def supprimer(self, numero, confirme: bool = False) -> str:
        if erreur := self._erreur_si_indisponible():
            return erreur
        ev, message = self._confirmation("supprimer", numero, bool(confirme))
        if ev is None:
            return message
        with self._lock:
            self._service.events().delete(calendarId=ev.agenda_id, eventId=ev.id).execute()
        log.info("Agenda : supprimé %s", ev.description())
        return f"Supprimé : {ev.description()}."

    def modifier(self, numero, titre: str = "", debut: str = "", fin: str = "", lieu: str = "",
                 confirme: bool = False) -> str:
        if erreur := self._erreur_si_indisponible():
            return erreur
        details = {k: v for k, v in (("titre", titre), ("debut", debut), ("fin", fin), ("lieu", lieu)) if v}
        if not details and not confirme:
            return "Erreur : rien à modifier."
        ev, info = self._confirmation("modifier", numero, bool(confirme), details)
        if ev is None:
            return info
        details = info or {}
        patch: dict = {}
        if details.get("titre"):
            patch["summary"] = details["titre"]
        if details.get("lieu"):
            patch["location"] = details["lieu"]
        if details.get("debut") or details.get("fin"):
            nouveau_debut = lire_date(details["debut"])[0] if details.get("debut") else ev.debut
            if details.get("fin"):
                nouvelle_fin = lire_date(details["fin"])[0]
            else:
                nouvelle_fin = nouveau_debut + (ev.fin - ev.debut)  # on garde la durée
            if ev.journee:
                patch["start"] = {"date": nouveau_debut.date().isoformat()}
                patch["end"] = {"date": max(nouvelle_fin.date(), nouveau_debut.date() + timedelta(days=1)).isoformat()}
            else:
                patch["start"] = {"dateTime": nouveau_debut.isoformat()}
                patch["end"] = {"dateTime": nouvelle_fin.isoformat()}
        with self._lock:
            brut = self._service.events().patch(calendarId=ev.agenda_id, eventId=ev.id, body=patch).execute()
        nouveau = Evenement(brut, ev.agenda_id, ev.agenda_nom)
        log.info("Agenda : modifié %s", nouveau.description())
        return f"Modifié : {nouveau.description()}."

    # --- annonces avant les rendez-vous ---

    def _boucle_annonces(self) -> None:
        deja_annonces: set[tuple[str, str]] = set()
        prochains: list[Evenement] = []
        derniere_lecture = 0.0
        while True:
            maintenant = datetime.now().astimezone()
            if time.monotonic() - derniere_lecture > 300:   # relit l'agenda toutes les 5 minutes
                try:
                    # Seulement l'agenda perso (principal) : pas les cours abonnés ni les agendas partagés.
                    prochains = [e for e in self._evenements(maintenant, maintenant + timedelta(hours=2))
                                 if not e.journee and e.agenda_nom == ""]
                except Exception as e:
                    log.warning("Agenda : lecture impossible pour les annonces (%s).", e)
                derniere_lecture = time.monotonic()
            for ev in prochains:
                cle = (ev.id, ev.debut.isoformat())
                reste = (ev.debut - maintenant).total_seconds()
                if cle in deja_annonces or reste <= 0:
                    continue
                if reste <= self._minutes_annonce * 60:
                    deja_annonces.add(cle)
                    minutes = max(1, round(reste / 60))
                    lieu = f", {ev.lieu}" if ev.lieu else ""
                    try:
                        self._annoncer(f"Dans {minutes} minute{'s' if minutes > 1 else ''} : {ev.titre}{lieu}.")
                    except Exception as e:
                        log.warning("Annonce d'agenda impossible : %s", e)
            time.sleep(15)


def _nom_agenda(a: dict) -> str:
    """Nom court de l'agenda (vide pour le principal). Un agenda abonné par lien (.ics) devient « cours »."""
    if a.get("primary"):
        return ""
    nom = a.get("summaryOverride") or a.get("summary", "")
    if nom.startswith(("http://", "https://", "webcal://")) or nom.endswith(".ics"):
        return NOM_AGENDA_ABONNE
    return nom


_OUI = ("oui", "ouais", "vas-y", "vas y", "ok", "okay", "d'accord", "confirme", "confirmé", "exactement",
        "c'est ça", "c'est bien ça", "bien sûr", "carrément", "yes", "allez", "parfait", "absolument")
_NON = ("non", "pas", "attends", "annule", "laisse tomber", "stop", "surtout pas")


def _est_un_oui(texte: str) -> bool:
    bas = f" {(texte or '').lower()} "
    for c in ",.!?;:":
        bas = bas.replace(c, " ")
    return any(f" {o} " in bas for o in _OUI) and not any(f" {n} " in bas for n in _NON)


def _resume_changements(details: dict) -> str:
    noms = {"titre": "nouveau titre", "debut": "nouveau début", "fin": "nouvelle fin", "lieu": "nouveau lieu"}
    return ", ".join(f"{noms[k]} {v}" for k, v in details.items())


if __name__ == "__main__":
    # Autorisation (une seule fois) puis affichage des 7 prochains jours pour vérifier.
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dossier = Path(__file__).resolve().parent / "donnees"
    agenda = Agenda(dossier, annoncer=None, minutes_annonce=0)
    if not agenda.disponible:
        try:
            agenda._service = Agenda._construire(agenda._identifiants(interactif=True))
        except FileNotFoundError as e:
            print(f"\n{e}\nTéléchargez le fichier OAuth « application de bureau » depuis Google Cloud "
                  f"et enregistrez-le sous ce nom (voir README).")
            sys.exit(1)
    print("\nAgenda Google connecté. Vos 7 prochains jours :\n")
    aujourd_hui = date.today()
    print(agenda.evenements(aujourd_hui.isoformat(), (aujourd_hui + timedelta(days=7)).isoformat()))

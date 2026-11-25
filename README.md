# Jarvis

Assistant vocal façon Alexa, en français : mot de réveil « Jarvis », musique Spotify, minuteurs et rappels,
agenda Google, météo, briefing du jour et routine de bienvenue au double claquement de mains.

## Installation

```bash
python -m pip install -r requirements.txt
```

Créez un fichier `.env` à côté de `jarvis.py` avec au minimum :

```env
GROQ_API_KEY=votre_cle_groq
```

Pour le mot de réveil « Jarvis », placez le petit modèle français de Vosk (`vosk-model-small-fr-0.22`,
https://alphacephei.com/vosk/models) dans `modeles/vosk-fr/`. Sans lui, seul « Hey Jarvis » fonctionne.

Puis lancez :

```bash
python jarvis.py
```

Autorisez le micro si Windows le demande. Ctrl+C pour arrêter.

## Organisation du code

| Fichier | Rôle |
| ------- | ---- |
| `jarvis.py` | Réglages, outils de l'IA, conversation vocale, démarrage |
| `ecoute.py` | Micro, mot de réveil local (Vosk pour « Jarvis », openWakeWord pour « Hey Jarvis »), transcription (Groq Whisper large-v3, Google en secours) |
| `diagnostic_micro.py` | Test du micro et de la transcription avec votre voix |
| `voix.py` | Voix de Jarvis (Edge-TTS, voix Rémy par défaut) |
| `spotify.py` + `spicetify/jarvis-spotify.js` | Pilotage de Spotify via une extension Spicetify |
| `rappels.py` | Minuteurs, alarmes, rappels |
| `agenda.py` | Agenda Google |
| `meteo.py` | Météo (Open-Meteo, gratuit, sans clé) |
| `briefing.py` | Briefing du jour (météo + agenda + rappels) |
| `claquements.py` | Détection du double claquement |
| `fenetres.py` | Ouverture de pages Chrome sur un écran donné |
| `pc.py` | Contrôle du PC : applications, sites, verrouillage, veille, capture d'écran |
| `modeles/vosk-fr/` | Modèle français Vosk pour reconnaître « Jarvis » (ignoré par git) |
| `donnees/` | Rappels, accès Google (ignoré par git) |
| `ancien/` | Anciens fichiers mis de côté (supprimables) |

## Utilisation

Dites **« Jarvis »** (détecté sur le PC : rien ne part sur internet avant), attendez le bip et
« Oui, monsieur ? », puis parlez. Vous pouvez aussi tout enchaîner : « Jarvis, quelle heure est-il ? ».
« Hey Jarvis » prononcé à l'anglaise marche aussi.

- **Touche Jarvis : Ctrl+Alt+J** (marche quelle que soit la fenêtre active) : fait taire Jarvis s'il parle,
  coupe une sonnerie, sinon le réveille comme si vous disiez « Jarvis ». Réglable avec `JARVIS_TOUCHE` dans `.env`.
- Vous pouvez aussi redire « Jarvis » dans une pause de sa réponse pour le couper.
- Après une réponse, il écoute encore quelques secondes pour une question de suivi.
- « Merci » ou « c'est tout » termine la conversation. Après avoir lancé de la musique, il se rendort tout seul.
- Quand vous le réveillez, la musique Spotify est mise en pause puis relancée à la fin.

### Musique (Spotify)

« Mets Get Lucky de Daft Punk », « mets l'album Discovery », « mets du Stromae », « lance la playlist Top 50 France »,
« mets mes titres likés en aléatoire », « après ça mets Alors on danse », « c'est quoi ce morceau ? », « suivant », « pause ».

Pas besoin de Premium ni de clé : une extension Spicetify fait le lien. À installer une fois (et après chaque mise à jour
de Spotify, comme toute extension Spicetify) :

```bash
copy spicetify\jarvis-spotify.js %APPDATA%\spicetify\Extensions\
spicetify config extensions jarvis-spotify.js
spicetify apply
```

Si Spotify est fermé, Jarvis l'ouvre. L'extension parle à Jarvis sur `ws://127.0.0.1:8765` (local uniquement ;
les connexions venant de pages web sont refusées).

MP3 locaux : « joue mes MP3 », « mets ma bibliothèque rap » (sous-dossiers de `DOSSIER_MUSIQUE`).

### Contrôle du PC

« Ouvre Discord », « lance Steam », « ferme Chrome », « ouvre Netflix », « mets une vidéo de chats sur YouTube »,
« verrouille l'ordi », « mets le PC en veille », « fais une capture d'écran », « affiche le bureau », « coupe le son ».

- Les applications sont celles du menu Démarrer (logiciels classiques et applis du Store).
- Fermer = comme un clic sur la croix : un document non enregistré sera proposé à l'enregistrement.
  Jarvis refuse de fermer le programme dans lequel il tourne (terminal, VS Code) et ceux de Windows.
- Verrouillage et veille se font juste après la réponse de Jarvis. Sur les PC à veille moderne (la plupart des
  portables récents), la mise en veille éteint l'écran, comme un appui sur le bouton d'alimentation.
- Les captures vont dans *Images > Captures Jarvis*.

### Minuteurs, alarmes, rappels

« Mets un minuteur de 10 minutes », « réveille-moi demain à 7 h 30 », « rappelle-moi d'appeler Paul dans 20 minutes »,
« combien de temps reste-t-il ? », « annule l'alarme ».

Les minuteurs et alarmes sonnent jusqu'à « Jarvis » (60 s maximum). Tout est sauvegardé dans `donnees/rappels.json` :
un rappel manqué pendant que Jarvis était éteint (moins de 12 h) est annoncé au démarrage.

### Agenda Google

« Qu'est-ce que j'ai demain ? », « suis-je libre jeudi à 14 h ? », « ajoute dentiste vendredi à 10 h »,
« décale la réunion avec Paul à 16 h », « supprime le dentiste ». Modifications et suppressions demandent toujours
une confirmation (vérifiée dans le code). Jarvis annonce aussi chaque événement de votre agenda perso 10 minutes avant.

Configuration (une fois, environ 10 minutes) :

1. Sur https://console.cloud.google.com, créez un projet (ex. « Jarvis »).
2. *API et services > Bibliothèque* : activez **Google Calendar API**.
3. *Google Auth Platform* : **Commencer**, nom « Jarvis », votre e-mail, audience **Externe**.
4. *Audience* : ajoutez votre adresse Gmail comme **utilisateur test**. Pour éviter de devoir vous reconnecter
   tous les 7 jours, complétez la page *Branding* puis cliquez sur **Publier l'application**.
5. *Clients* : créez un client **Application de bureau**, téléchargez le JSON et enregistrez-le sous
   `donnees/credentials.json`.
6. Lancez `python agenda.py` : choisissez votre compte, puis *Paramètres avancés > Accéder à Jarvis* (c'est votre
   propre application) et autorisez. Vos 7 prochains jours s'affichent.

L'autorisation est gardée dans `donnees/token_google.json`. Les titres de vos événements sont envoyés à Groq pour que
l'IA formule ses réponses.

### Météo et briefing

- Météo : « quel temps fait-il ? », « je prends un parapluie demain ? », « météo à Lyon vendredi ».
- Briefing (météo + agenda du jour + rappels, en 3 à 5 phrases) : « bonjour », « c'est quoi mon programme ? »,
  « je suis rentré ». Le soir, il couvre la soirée et le lendemain.
- Après une alarme du matin arrêtée avec « Jarvis » : briefing automatique.

### Double claquement

Deux claquements de mains dans une pièce calme : musique de bienvenue sur Spotify, ouverture de deux pages Chrome
en plein écran sur vos écrans, puis « Bienvenue à la maison, monsieur. » et le briefing. Une fois par lancement.
Les claquements sont ignorés pendant que la musique joue ou que Jarvis parle (la batterie dans les enceintes
ressemble à des claquements).

## Réglages

En haut de `jarvis.py` (section RÉGLAGES) : modèle d'IA, sensibilité du mot de réveil, musique de bienvenue
(`MUSIQUE_BIENVENUE`, titre ou playlist), dossier des MP3, annonces d'agenda, briefing, double claquement, pages Chrome.
Les réglages fins de détection des claquements sont en haut de `claquements.py`. Ceux de la voix (vitesse, pauses sur la ponctuation `PAUSES_PONCTUATION`, blanc entre les phrases) sont en haut de `voix.py`.

Variables possibles dans `.env` :

| Variable | Rôle |
| -------- | ---- |
| `GROQ_API_KEY` | **Obligatoire.** Clé [Groq](https://console.groq.com) (IA et transcription) |
| `EDGE_TTS_VOICE` | Voix (défaut `fr-FR-RemyMultilingualNeural` ; autres : `fr-FR-HenriNeural`, `fr-FR-DeniseNeural`) |
| `EDGE_TTS_VITESSE` | Vitesse de la voix (défaut `+10%` ; `+0%` = normale, `+20%` = plus rapide) |
| `JARVIS_SEUIL_REVEIL` | Sensibilité de « Hey Jarvis » à l'anglaise (0.5 par défaut ; 0.3 = plus sensible, 0.7 = moins) |
| `JARVIS_TOUCHE` | Touche Jarvis (défaut `ctrl+alt+j` ; ex. `ctrl+shift+espace`, `pause`, `f9` ; vide = désactivée) |
| `JARVIS_MODELE_VOSK` | Dossier du modèle Vosk pour « Jarvis » (défaut `modeles/vosk-fr`) |
| `JARVIS_INPUT_DEVICE` | Micro à utiliser : numéro ou morceau du nom (liste : `python -c "import sounddevice as sd; print(sd.query_devices())"`) |
| `JARVIS_DOSSIER_MUSIQUE` | Dossier des MP3 locaux (défaut `E:\Musiques`) |
| `METEO_VILLE` | Ville de la météo (défaut Valence, France) ; ou `METEO_LATITUDE` / `METEO_LONGITUDE` |
| `JARVIS_PAGE_1`, `JARVIS_PAGE_2` | Pages ouvertes au double claquement (anciens noms acceptés : `CLAUDE_CODE_URL`, `BINANCE_BTC_URL`) |
| `CHROME_NEW_WINDOW_WAIT_S` | Attente de la fenêtre Chrome (25 s par défaut) |

> Offre gratuite Groq : 8 000 jetons par minute. Une demande en coûte environ 2 000 : après 3 ou 4 actions très
> rapprochées, Jarvis peut marquer une pause de quelques secondes.

## Dépannage

- **Jarvis comprend mal ce que vous dites** : lancez `python diagnostic_micro.py`, lisez les 8 phrases affichées.
  Il montre ce qui a été compris et le niveau du micro, avec des conseils (rapport dans `donnees/diagnostic/`).

- **« Jarvis » mal détecté** : vérifiez que `modeles/vosk-fr/` existe (le journal affiche « Mot de réveil « Jarvis »
  (Vosk) activé »). Dites « Jarvis » nettement ; en cas de doute, marquez une petite pause avant la demande.
- **Réveils intempestifs** : la liste des mots « leurres » est en haut de `ecoute.py` (`VOSK_MOTS`) ; ajoutez-y le mot
  qui ressemble à « Jarvis » et le déclenche.
- **Plus de voix / erreur 403 Edge-TTS** : `python -m pip install -U edge-tts` (Microsoft change régulièrement son jeton).
- **Spotify ne répond pas** : vérifiez que l'extension est installée (`spicetify apply` après une mise à jour de Spotify).
- **Agenda déconnecté** : relancez `python agenda.py` (tous les 7 jours tant que l'application Google n'est pas publiée).
- **Mauvais micro** : réglez `JARVIS_INPUT_DEVICE`.
- **Claquements ratés ou faux déclenchements** : ajustez `SPIKE_RATIO` dans `claquements.py`.
- **Erreurs audio (PortAudio)** : mettez à jour les pilotes ou essayez `SAMPLE_RATE = 48000` dans `claquements.py`.

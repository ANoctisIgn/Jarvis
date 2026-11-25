// Jarvis ↔ Spotify : extension Spicetify.
// Se connecte à Jarvis (ws://127.0.0.1:8765) et exécute ses commandes dans l'appli Spotify :
// recherche, lecture d'un titre / album / artiste / playlist, file d'attente, pause, etc.
// Installation : voir le README de Jarvis (section Spotify).

(function jarvisSpotify() {
  if (!(window.Spicetify && Spicetify.Player && Spicetify.Platform && Spicetify.GraphQL?.Definitions)) {
    setTimeout(jarvisSpotify, 300);
    return;
  }

  const ADRESSE = "ws://127.0.0.1:8765";
  const TYPES = { titre: "track", album: "album", artiste: "artist", playlist: "playlist" };

  // --- utilitaires ---------------------------------------------------------------

  const normaliser = (s) =>
    (s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9 ]/g, " ").replace(/\s+/g, " ").trim();

  function nomArtiste(o) {
    return (
      o?.artists?.items?.map((a) => a?.profile?.name).filter(Boolean).join(", ") ||
      o?.artists?.map?.((a) => a?.name).filter(Boolean).join(", ") ||
      o?.ownerV2?.data?.name ||
      o?.owner?.display_name ||
      ""
    );
  }

  // Parcourt une réponse (GraphQL ou API web) et récupère, dans l'ordre, les objets
  // dont l'URI correspond au type cherché. Résiste aux changements de format de Spotify.
  function extraire(racine, typeSpotify) {
    const prefixe = `spotify:${typeSpotify}:`;
    const vus = new Set();
    const resultats = [];
    const pile = [racine];
    while (pile.length) {
      const o = pile.shift();
      if (!o || typeof o !== "object") continue;
      if (typeof o.uri === "string" && o.uri.startsWith(prefixe) && !vus.has(o.uri)) {
        const nom = o.name || o.profile?.name;
        if (nom) {
          vus.add(o.uri);
          resultats.push({ uri: o.uri, nom, artiste: nomArtiste(o) });
        }
      }
      for (const v of Array.isArray(o) ? o : Object.values(o)) {
        if (v && typeof v === "object") pile.push(v);
      }
    }
    return resultats;
  }

  // Juste après le démarrage de Spotify, la recherche peut revenir vide : on réessaie une fois.
  async function rechercher(texte, type) {
    const res = await rechercherUneFois(texte, type);
    if (res.length) return res;
    await pause(1500);
    return rechercherUneFois(texte, type);
  }

  async function rechercherUneFois(texte, type) {
    const t = TYPES[type] || "track";
    // 1) GraphQL interne (ce que l'appli utilise elle-même). Le nom de la requête change
    //    selon les versions de Spotify : on prend la première qui existe.
    const defs = Spicetify.GraphQL.Definitions || {};
    for (const nom of ["searchModalResults", "searchDesktop"]) {
      if (!defs[nom]) continue;
      try {
        const r = await Spicetify.GraphQL.Request(defs[nom], {
          searchTerm: texte,
          offset: 0,
          limit: 10,
          numberOfTopResults: 5,
          includeAudiobooks: false,
          includeArtistHasConcertsField: false,
          includePreReleases: false,
          includeLocalConcertsField: false,
          includeAuthors: false,
        });
        const res = extraire(r?.data, t);
        if (res.length) return res;
      } catch (e) {
        console.warn("[Jarvis] recherche GraphQL", nom, e);
      }
    }
    // 2) API web avec le jeton de l'appli
    try {
      const r = await Spicetify.CosmosAsync.get(
        `https://api.spotify.com/v1/search?q=${encodeURIComponent(texte)}&type=${t}&limit=5`
      );
      return extraire(r, t);
    } catch (e) {
      console.warn("[Jarvis] recherche API", e);
      return [];
    }
  }

  // Playlists de l'utilisateur (dossiers compris), pour « mets ma playlist sport ».
  async function mesPlaylists() {
    const contenu = await Spicetify.Platform.RootlistAPI.getContents();
    const liste = [];
    const parcourir = (items) => {
      for (const it of items || []) {
        if (it.type === "playlist") liste.push({ uri: it.uri, nom: it.name, artiste: "vous" });
        if (it.items) parcourir(it.items);
      }
    };
    parcourir(contenu?.items);
    return liste;
  }

  async function trouverMaPlaylist(texte) {
    const cible = normaliser(texte);
    if (!cible) return null;
    const liste = await mesPlaylists();
    return (
      liste.find((p) => normaliser(p.nom) === cible) ||
      liste.find((p) => normaliser(p.nom).includes(cible) || cible.includes(normaliser(p.nom))) ||
      null
    );
  }

  function enCours() {
    const it = Spicetify.Player.data?.item;
    if (!it) return null;
    return {
      uri: it.uri,
      nom: it.name || it.metadata?.title || "",
      artiste: it.artists?.map((a) => a.name).join(", ") || it.metadata?.artist_name || "",
      album: it.album?.name || it.metadata?.album_title || "",
    };
  }

  const pause = (ms) => new Promise((r) => setTimeout(r, ms));

  // Après un lancement, on attend que la lecture ait vraiment démarré (sinon une pause
  // envoyée juste après serait écrasée par le démarrage du morceau).
  async function attendreLecture(max = 4000) {
    const fin = Date.now() + max;
    while (Date.now() < fin) {
      try {
        if (Spicetify.Player.isPlaying()) return;
      } catch {}
      await pause(150);
    }
  }

  // --- commandes -------------------------------------------------------------------

  async function executer(msg) {
    switch (msg.action) {
      case "rechercher":
        return { resultats: await rechercher(msg.recherche, msg.type) };


      case "jouer": {
        let choix = null;
        if (msg.type === "playlist") choix = await trouverMaPlaylist(msg.recherche);
        if (!choix) choix = (await rechercher(msg.recherche, msg.type))[0];
        if (!choix) throw new Error(`Rien trouvé sur Spotify pour « ${msg.recherche} ».`);
        await Spicetify.Player.playUri(choix.uri);
        await attendreLecture();
        return { joue: choix };
      }

      case "likes": {
        const user = await Spicetify.Platform.UserAPI.getUser();
        await Spicetify.Player.playUri(`spotify:user:${user.username}:collection`);
        await attendreLecture();
        if (msg.aleatoire) Spicetify.Player.setShuffle(true);
        return { joue: { nom: "vos titres likés", artiste: "" } };
      }

      case "file": {
        const choix = (await rechercher(msg.recherche, "titre"))[0];
        if (!choix) throw new Error(`Rien trouvé sur Spotify pour « ${msg.recherche} ».`);
        await Spicetify.addToQueue([{ uri: choix.uri }]);
        return { ajoute: choix };
      }

      case "etat": {
        const sur = (f) => { try { return f(); } catch { return null; } }; // lecteur pas encore prêt au démarrage
        return { lecture: !!sur(() => Spicetify.Player.isPlaying()), morceau: sur(enCours), volume: sur(() => Spicetify.Player.getVolume()) };
      }

      case "pause":
        Spicetify.Player.pause();
        return {};
      case "reprendre":
        Spicetify.Player.play();
        return {};
      case "suivant":
        Spicetify.Player.next();
        await pause(600);
        return { morceau: enCours() };
      case "precedent":
        Spicetify.Player.back();
        await pause(600);
        return { morceau: enCours() };
      case "aleatoire":
        Spicetify.Player.setShuffle(!!msg.actif);
        return {};
      case "volume":
        Spicetify.Player.setVolume(Math.max(0, Math.min(1, Number(msg.niveau))));
        return {};
      case "liker":
        if (!Spicetify.Player.getHeart()) Spicetify.Player.toggleHeart();
        return { morceau: enCours() };
      default:
        throw new Error(`Action inconnue : ${msg.action}`);
    }
  }

  // --- connexion à Jarvis ----------------------------------------------------------

  function connecter() {
    let ws;
    try {
      ws = new WebSocket(ADRESSE);
    } catch {
      setTimeout(connecter, 5000);
      return;
    }
    ws.onopen = () => {
      console.log("[Jarvis] connecté");
      ws.send(JSON.stringify({ type: "bonjour", version: Spicetify.Platform.version || "" }));
    };
    ws.onmessage = async (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      let reponse;
      try {
        reponse = { ok: true, ...(await executer(msg)) };
      } catch (e) {
        reponse = { ok: false, erreur: String(e?.message || e) };
      }
      ws.send(JSON.stringify({ id: msg.id, ...reponse }));
    };
    ws.onclose = () => setTimeout(connecter, 5000); // Jarvis pas lancé : on réessaie
    ws.onerror = () => {};
  }

  // On laisse Spotify finir de démarrer (lecteur, recherche) avant d'accepter des commandes.
  setTimeout(connecter, 3000);
})();

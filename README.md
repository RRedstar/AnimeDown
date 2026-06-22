# Interface graphique – Anime Downloader

## Installation

Place ces 3 fichiers à la **racine du projet**, au même niveau que les
dossiers `core/`, `extractors/` et le fichier `utils.py` :

- `gui_app.py`
- `async_bridge.py`
- `gui_progress.py`

Installe la dépendance supplémentaire (en plus de ce que ton CLI utilise déjà) :

```bash
pip install customtkinter
```

`tkinter` lui-même est inclus nativement avec Python sur Windows et macOS.
Sur Linux, si jamais il manque :

```bash
# Debian/Ubuntu
sudo apt install python3-tk
```

## Lancement

```bash
python gui_app.py
```

## Fonctionnement

- **`async_bridge.py`** : fait tourner une boucle `asyncio` dans un thread
  séparé du thread Tkinter (l'UI ne doit jamais être bloquée par un `await`).
  L'UI soumet des coroutines via `bridge.submit(...)`, et reçoit les résultats
  de façon asynchrone via une `queue.Queue` thread-safe, consommée toutes les
  80ms par `_poll_events()`.

- **`gui_progress.py`** : un faux objet `Progress`, compatible avec l'API de
  `rich.progress.Progress` utilisée telle quelle dans `orchestrator.py` et
  `core/downloader.py` (`add_task`, `update`, `console.print`, `console.status`).
  Au lieu de dessiner une barre dans un terminal, il pousse des événements
  dans la queue du bridge. **Une instance est créée par épisode** (et non
  partagée), ce qui garantit que chaque tâche de progression est associée au
  bon épisode même avec plusieurs téléchargements simultanés.

- **`gui_app.py`** : l'interface elle-même (CustomTkinter). Réutilise
  directement `Orchestrator`, `VoirAnimeEpisode`, `SupportedPlayers` et
  `sanitize_filename` — aucune logique métier n'a été dupliquée ou réécrite.

## Ce que fait l'UI

1. Tu colles une URL (série ou épisode unique) → "Rechercher" appelle
   `Orchestrator.get_series_episodes()` (avec fallback automatique sur un
   épisode unique si la page n'est pas une série, comme dans le CLI).
2. La liste des épisodes apparaît avec une case à cocher (cochée par défaut).
3. Tu peux régler : dossier de sortie, lecteur, nombre de téléchargements
   simultanés, épisode de départ.
4. "Télécharger" lance les téléchargements sélectionnés en parallèle (limité
   par le semaphore de l'`Orchestrator`), avec une barre de progression et un
   indicateur de statut (gris = en attente, bleu = en cours, vert = terminé,
   rouge = erreur) par épisode.
5. Le panneau de logs en bas affiche les mêmes messages que la console rich
   du CLI (statuts, erreurs, avertissements).

## Limites connues / pistes d'amélioration

- **Pas d'annulation en cours de route.** Fermer la fenêtre arrête la boucle
  asyncio, mais un téléchargement déjà lancé via `httpx` peut continuer
  jusqu'à son timeout côté OS. Ajouter un vrai bouton "Annuler" demanderait
  de propager un `asyncio.Event` jusque dans `SmartDownloader`.
- **Pas de persistance des préférences** (dossier de sortie, lecteur,
  concurrence) entre deux lancements de l'app. Facile à ajouter avec un
  petit fichier `config.json` ou le module `json` + `~/.anime_dl/settings.json`.
- **`core/base.py` et `extractors/players/streamtape.py`** n'ont pas été
  fournis : l'UI n'en a pas besoin directement (elle passe par
  `Orchestrator`), mais s'ils exposent des informations utiles (ex : qualité
  vidéo disponible), l'UI pourrait être étendue pour les afficher.

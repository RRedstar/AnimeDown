"""
Adaptateur "faux Progress" compatible avec l'API utilisée par
Orchestrator.download_episode() et SmartDownloader (rich.progress.Progress) :

    task = progress.add_task(description, total=..., completed=...)
    progress.update(task, advance=...)
    progress.console.print(...)

Au lieu de dessiner une barre dans un terminal, chaque appel pousse un
UIEvent dans la queue de l'AsyncBridge, que l'UI Tkinter consomme pour
mettre à jour ses propres barres de progression (CustomTkinter).

Ceci permet de réutiliser Orchestrator et SmartDownloader strictement
tels quels, sans toucher à une seule ligne de logique métier.

IMPORTANT : Orchestrator.download_episode() ne transmet pas l'épisode
en cours à `progress.add_task`, et SmartDownloader.download() ne le fait
pas non plus (il ne connaît que `ep_num`, qui EST passé à add_task via
le paramètre `total`/description mais pas explicitement à notre API).
Pour associer fiablement chaque tâche de progression à son épisode même
avec plusieurs téléchargements concurrents, on instancie UN GuiProgress
PAR épisode (voir gui_app.py), avec un `episode_number` fixé à la
construction. C'est plus robuste qu'une heuristique de devinette.
"""

import itertools
from typing import Optional

from async_bridge import AsyncBridge


class _FakeConsole:
    """Imite console.print() et console.status() utilisés dans orchestrator.py."""

    def __init__(self, bridge: AsyncBridge, episode_number: Optional[int]):
        self._bridge = bridge
        self._episode_number = episode_number

    def print(self, message):
        self._bridge.push_event(
            "log",
            {"episode_number": self._episode_number, "message": str(message)},
        )

    def status(self, message):
        # orchestrator.py appelle current_console.status(...).start()/.stop()/.update(...)
        return _FakeStatus(self, message)


class _FakeStatus:
    def __init__(self, console: "_FakeConsole", message: str):
        self._console = console
        self._message = message

    def start(self):
        self._console.print(self._message)

    def update(self, message):
        self._message = message
        self._console.print(message)

    def stop(self):
        pass


class GuiProgress:
    """
    Une instance PAR ÉPISODE (et non partagée entre tous les épisodes en
    cours), créée juste avant d'appeler orchestrator.download_episode(ep, ...).
    Cela garantit que chaque task_id créé via add_task() est associé sans
    ambiguïté à `episode_number`, même si plusieurs épisodes téléchargent
    en parallèle (max_concurrent > 1).
    """

    def __init__(self, bridge: AsyncBridge, episode_number: int):
        self._bridge = bridge
        self.episode_number = episode_number
        self._task_ids = itertools.count(1)
        self._tasks = {}
        self.console = _FakeConsole(bridge, episode_number=episode_number)

    def add_task(self, description: str, total: Optional[float] = None, completed: float = 0):
        task_id = next(self._task_ids)
        self._tasks[task_id] = {"total": total or 0, "completed": completed}
        self._bridge.push_event(
            "task_created",
            {
                "task_id": task_id,
                "episode_number": self.episode_number,
                "description": description,
                "total": total or 0,
                "completed": completed,
            },
        )
        return task_id

    def update(self, task_id, advance: float = 0, **kwargs):
        task = self._tasks.get(task_id)
        if task is None:
            return
        task["completed"] += advance
        self._bridge.push_event(
            "task_update",
            {
                "task_id": task_id,
                "episode_number": self.episode_number,
                "completed": task["completed"],
                "total": task["total"],
            },
        )

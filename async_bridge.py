"""
Pont entre la boucle asyncio (qui fait tourner l'Orchestrator) et l'UI Tkinter
(qui tourne sur le thread principal et ne doit jamais être bloquée).

Principe :
- Une boucle asyncio dédiée tourne dans un thread daemon séparé.
- L'UI soumet des coroutines via `submit()`, qui retourne immédiatement.
- Les résultats / erreurs / progressions remontent vers l'UI via une queue
  thread-safe, que l'UI consomme périodiquement avec `root.after(...)`.
"""

import asyncio
import threading
import queue
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class UIEvent:
    """Événement générique envoyé du thread asyncio vers le thread UI."""
    kind: str  # "episodes_fetched" | "episode_progress" | "episode_done" | "episode_error" | "log" | "all_done" | "fatal_error"
    payload: Any = None


class AsyncBridge:
    def __init__(self):
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.events: "queue.Queue[UIEvent]" = queue.Queue()
        self._started = threading.Event()

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self._started.wait()  # attend que la loop soit prête

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._started.set()
        self.loop.run_forever()

    def submit(self, coro_factory: Callable[[], "asyncio.coroutines.Coroutine"]):
        """
        Soumet une coroutine à exécuter sur la boucle asyncio dédiée.
        `coro_factory` est une fonction sans argument qui RETOURNE une coroutine
        (pour éviter de créer la coroutine sur le mauvais thread).
        """
        if self.loop is None:
            raise RuntimeError("AsyncBridge not started")

        # call_soon_threadsafe garantit que ensure_future() est exécuté
        # sur le thread de la boucle asyncio, même si submit() est appelé
        # depuis le thread Tkinter.
        self.loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(coro_factory(), loop=self.loop)
        )

    def push_event(self, kind: str, payload: Any = None):
        """Appelé depuis le thread asyncio pour notifier l'UI."""
        self.events.put(UIEvent(kind=kind, payload=payload))

    def stop(self):
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)

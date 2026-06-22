"""
Interface graphique moderne (CustomTkinter) pour le téléchargeur d'anime.

Réutilise directement Orchestrator, VoirAnimeEpisode, SupportedPlayers et
sanitize_filename — aucune logique métier n'est dupliquée ici.

Lancement :
    python gui_app.py

Dépendances supplémentaires par rapport au CLI :
    pip install customtkinter
"""

import os
import sys
import threading
import queue
import asyncio

import customtkinter as ctk

# --- Imports du projet existant ---------------------------------------
# On suppose que ce fichier est placé à la racine du projet, au même
# niveau que les dossiers core/ et extractors/ et le fichier utils.py.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import sanitize_filename
from core.orchestrator import Orchestrator
from core.config import SupportedPlayers
from extractors.platforms.voiranime import VoirAnimeEpisode

from async_bridge import AsyncBridge
from gui_progress import GuiProgress


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


# ---------------------------------------------------------------------
# Widget : une ligne de progression pour un épisode
# ---------------------------------------------------------------------
class EpisodeRow(ctk.CTkFrame):
    def __init__(self, master, episode_label: str, **kwargs):
        super().__init__(master, corner_radius=10, **kwargs)
        self.grid_columnconfigure(2, weight=1)

        self.selected_var = ctk.BooleanVar(value=True)
        self.checkbox = ctk.CTkCheckBox(self, text="", variable=self.selected_var, width=20)
        self.checkbox.grid(row=0, column=0, rowspan=2, padx=(10, 0), pady=10)

        self.status_dot = ctk.CTkLabel(self, text="●", text_color="#888888", width=20)
        self.status_dot.grid(row=0, column=1, padx=(6, 0), pady=10, sticky="w")

        self.label = ctk.CTkLabel(self, text=episode_label, anchor="w")
        self.label.grid(row=0, column=2, padx=10, pady=(10, 0), sticky="ew")

        self.progress_bar = ctk.CTkProgressBar(self)
        self.progress_bar.set(0)
        self.progress_bar.grid(row=1, column=2, padx=10, pady=(0, 10), sticky="ew")

        self.percent_label = ctk.CTkLabel(self, text="0%", width=50)
        self.percent_label.grid(row=1, column=3, padx=(0, 10), pady=(0, 10))

    def set_progress(self, completed: float, total: float):
        if total > 0:
            fraction = max(0.0, min(1.0, completed / total))
        else:
            fraction = 0.0
        self.progress_bar.set(fraction)
        self.percent_label.configure(text=f"{int(fraction * 100)}%")

    def set_status(self, status: str):
        colors = {
            "pending": "#888888",
            "running": "#3b8ed0",
            "done": "#2fa84f",
            "error": "#d04343",
            "skipped": "#c2a83e",
        }
        self.status_dot.configure(text_color=colors.get(status, "#888888"))


# ---------------------------------------------------------------------
# Application principale
# ---------------------------------------------------------------------
class AnimeDownloaderApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Anime Downloader")
        self.geometry("880x640")
        self.minsize(700, 500)

        self.bridge = AsyncBridge()
        self.bridge.start()

        self.episode_rows: dict[int, EpisodeRow] = {}  # episode.number -> EpisodeRow
        self.task_to_episode: dict[int, int] = {}  # task_id -> episode.number
        self.orchestrator: Orchestrator | None = None
        self.episodes_cache = []

        self._build_layout()
        self._poll_events()

    # -- Construction de l'UI ------------------------------------------------
    def _build_layout(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # --- Barre du haut : URL + bouton de recherche ---
        top_frame = ctk.CTkFrame(self, corner_radius=10)
        top_frame.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")
        top_frame.grid_columnconfigure(0, weight=1)

        self.url_entry = ctk.CTkEntry(
            top_frame, placeholder_text="URL de la série ou de l'épisode VoirAnime"
        )
        self.url_entry.grid(row=0, column=0, padx=(12, 8), pady=12, sticky="ew")

        self.fetch_button = ctk.CTkButton(
            top_frame, text="Rechercher", width=120, command=self._on_fetch_clicked
        )
        self.fetch_button.grid(row=0, column=1, padx=(0, 12), pady=12)

        # --- Options : dossier de sortie, lecteur, concurrence, ep de départ ---
        options_frame = ctk.CTkFrame(self, corner_radius=10)
        options_frame.grid(row=1, column=0, padx=16, pady=8, sticky="ew")
        for i in range(5):
            options_frame.grid_columnconfigure(i, weight=1)

        ctk.CTkLabel(options_frame, text="Dossier de sortie").grid(
            row=0, column=0, padx=12, pady=(10, 0), sticky="w"
        )
        self.output_entry = ctk.CTkEntry(options_frame, placeholder_text="(auto, basé sur le nom de la série)")
        self.output_entry.grid(row=1, column=0, padx=12, pady=(0, 10), sticky="ew")

        browse_btn = ctk.CTkButton(options_frame, text="Parcourir...", width=100, command=self._on_browse_clicked)
        browse_btn.grid(row=1, column=1, padx=(0, 12), pady=(0, 10))

        ctk.CTkLabel(options_frame, text="Lecteur").grid(
            row=0, column=2, padx=12, pady=(10, 0), sticky="w"
        )
        self.player_menu = ctk.CTkOptionMenu(
            options_frame, values=[p.value for p in SupportedPlayers]
        )
        self.player_menu.set(SupportedPlayers.STREAMTAPE.value)
        self.player_menu.grid(row=1, column=2, padx=12, pady=(0, 10), sticky="ew")

        ctk.CTkLabel(options_frame, text="Téléchargements simultanés").grid(
            row=0, column=3, padx=12, pady=(10, 0), sticky="w"
        )
        self.concurrency_entry = ctk.CTkEntry(options_frame, width=60)
        self.concurrency_entry.insert(0, "3")
        self.concurrency_entry.grid(row=1, column=3, padx=12, pady=(0, 10), sticky="w")

        ctk.CTkLabel(options_frame, text="Épisode de départ").grid(
            row=0, column=4, padx=12, pady=(10, 0), sticky="w"
        )
        self.start_ep_entry = ctk.CTkEntry(options_frame, width=80, placeholder_text="1er dispo")
        self.start_ep_entry.grid(row=1, column=4, padx=12, pady=(0, 10), sticky="w")

        # --- Liste scrollable des épisodes ---
        self.episodes_frame = ctk.CTkScrollableFrame(self, label_text="Épisodes")
        self.episodes_frame.grid(row=2, column=0, padx=16, pady=8, sticky="nsew")
        self.episodes_frame.grid_columnconfigure(0, weight=1)

        # --- Bas : bouton télécharger + logs ---
        bottom_frame = ctk.CTkFrame(self, corner_radius=10)
        bottom_frame.grid(row=3, column=0, padx=16, pady=(8, 16), sticky="ew")
        bottom_frame.grid_columnconfigure(0, weight=1)

        self.download_button = ctk.CTkButton(
            bottom_frame,
            text="Télécharger les épisodes sélectionnés",
            command=self._on_download_clicked,
            state="disabled",
            height=40,
        )
        self.download_button.grid(row=0, column=0, padx=12, pady=12, sticky="ew")

        self.log_box = ctk.CTkTextbox(bottom_frame, height=120, state="disabled")
        self.log_box.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="ew")

    # -- Helpers UI ------------------------------------------------------
    def _log(self, message: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_episode_rows(self):
        for row in self.episode_rows.values():
            row.destroy()
        self.episode_rows.clear()

    # -- Actions utilisateur ----------------------------------------------
    def _on_browse_clicked(self):
        from tkinter import filedialog
        path = filedialog.askdirectory()
        if path:
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, path)

    def _on_fetch_clicked(self):
        url = self.url_entry.get().strip()
        if not url:
            self._log("⚠ Merci de renseigner une URL.")
            return

        self.fetch_button.configure(state="disabled", text="Recherche...")
        self._clear_episode_rows()
        self.download_button.configure(state="disabled")

        player_code = self.player_menu.get()

        def make_coro():
            return self._fetch_episodes_coro(url, player_code)

        self.bridge.submit(make_coro)

    async def _fetch_episodes_coro(self, url: str, player_code: str):
        orchestrator = Orchestrator(output_dir=".", max_concurrent=3, player_code=player_code)
        try:
            episodes = await orchestrator.get_series_episodes(url)
        except Exception as e:
            self.bridge.push_event("fatal_error", {"message": f"Erreur lors de la recherche : {e}"})
            return

        if not episodes:
            # Peut-être une page d'épisode unique plutôt qu'une série.
            ep_num = 0
            try:
                parts = url.rstrip("/").split("-")
                for p in reversed(parts):
                    if p.isdigit():
                        ep_num = int(p)
                        break
            except ValueError:
                pass

            full_url = url
            if player_code == SupportedPlayers.STREAMTAPE:
                separator = "&" if "?" in url else "?"
                full_url = f"{url}{separator}host=LECTEUR%20Stape"

            episode = VoirAnimeEpisode(
                number=ep_num, name=f"Episode {ep_num}", url=full_url, player_code=player_code
            )
            episodes = [episode]

        self.orchestrator = orchestrator
        self.bridge.push_event("episodes_fetched", {"episodes": episodes, "url": url})

    def _on_download_clicked(self):
        if not self.episodes_cache:
            return

        selected = [ep for ep in self.episodes_cache if self.episode_rows[ep.number].selected_var.get()]

        start_ep_text = self.start_ep_entry.get().strip()
        if start_ep_text:
            try:
                start_ep = int(start_ep_text)
                selected = [ep for ep in selected if ep.number >= start_ep]
            except ValueError:
                self._log("⚠ Épisode de départ invalide, ignoré.")

        if not selected:
            self._log("⚠ Aucun épisode à télécharger.")
            return

        try:
            concurrency = max(1, int(self.concurrency_entry.get().strip() or "3"))
        except ValueError:
            concurrency = 3

        output_dir = self.output_entry.get().strip()
        if not output_dir:
            series_name = self.url_entry.get().rstrip("/").split("/")[-1] or "Anime"
            output_dir = sanitize_filename(series_name)
            self.output_entry.insert(0, output_dir)

        player_code = self.player_menu.get()

        self.download_button.configure(state="disabled", text="Téléchargement en cours...")
        self._log(f"▶ Lancement du téléchargement de {len(selected)} épisode(s) vers '{output_dir}'.")

        for ep in selected:
            self.episode_rows[ep.number].set_status("pending")

        def make_coro():
            return self._download_all_coro(selected, output_dir, concurrency, player_code)

        self.bridge.submit(make_coro)

    async def _download_all_coro(self, episodes, output_dir, concurrency, player_code):
        orchestrator = Orchestrator(
            output_dir=output_dir, max_concurrent=concurrency, player_code=player_code
        )

        async def run_one(ep):
            self.bridge.push_event("episode_status", {"episode_number": ep.number, "status": "running"})
            # Un GuiProgress dédié par épisode : chaque task_id créé par
            # add_task() est ainsi associé sans ambiguïté à ep.number, même
            # si plusieurs épisodes téléchargent en parallèle.
            progress = GuiProgress(self.bridge, episode_number=ep.number)
            try:
                ok = await orchestrator.download_episode(ep, progress)
                status = "done" if ok else "error"
            except Exception as e:
                self.bridge.push_event(
                    "log", {"episode_number": ep.number, "message": f"Erreur sur {ep.name} : {e}"}
                )
                status = "error"
            self.bridge.push_event("episode_status", {"episode_number": ep.number, "status": status})

        await asyncio.gather(*(run_one(ep) for ep in episodes))
        self.bridge.push_event("all_done", None)

    # -- Boucle de polling des événements venant du thread asyncio -----------
    def _poll_events(self):
        try:
            while True:
                event = self.bridge.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        finally:
            self.after(80, self._poll_events)

    def _handle_event(self, event):
        kind = event.kind
        payload = event.payload

        if kind == "episodes_fetched":
            self.fetch_button.configure(state="normal", text="Rechercher")
            episodes = payload["episodes"]
            self.episodes_cache = episodes
            self._populate_episode_rows(episodes)
            self.download_button.configure(state="normal")
            self._log(f"✔ {len(episodes)} épisode(s) trouvé(s).")

        elif kind == "fatal_error":
            self.fetch_button.configure(state="normal", text="Rechercher")
            self._log(f"✖ {payload['message']}")

        elif kind == "log":
            self._log(payload["message"])

        elif kind == "task_created":
            ep_number = payload["episode_number"]
            self.task_to_episode[payload["task_id"]] = ep_number
            row = self.episode_rows.get(ep_number)
            if row:
                row.set_status("running")

        elif kind == "task_update":
            ep_number = payload["episode_number"]
            row = self.episode_rows.get(ep_number)
            if row:
                row.set_progress(payload["completed"], payload["total"])

        elif kind == "episode_status":
            ep_number = payload["episode_number"]
            row = self.episode_rows.get(ep_number)
            if row:
                row.set_status(payload["status"])
                if payload["status"] == "done":
                    row.set_progress(1, 1)

        elif kind == "all_done":
            self.download_button.configure(state="normal", text="Télécharger les épisodes sélectionnés")
            self._log("✔ Tous les téléchargements sont terminés.")

    def _populate_episode_rows(self, episodes):
        self._clear_episode_rows()
        for ep in episodes:
            row = EpisodeRow(self.episodes_frame, episode_label=ep.name)
            row.grid(sticky="ew", pady=4, padx=2)
            self.episodes_frame.grid_columnconfigure(0, weight=1)
            self.episode_rows[ep.number] = row

    def on_close(self):
        self.bridge.stop()
        self.destroy()


def main():
    app = AnimeDownloaderApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()

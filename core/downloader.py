import os
import time
import httpx
import re
import asyncio
from urllib.parse import urlparse
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    DownloadColumn,
    TransferSpeedColumn,
    TimeRemainingColumn,
)
from rich.console import Console

_console = Console()


class SmartDownloader:
    def __init__(self, output_dir, series_name, max_retries=3, num_segments=8, chunk_size=1024 * 256):
        self.output_dir = output_dir
        self.series_name = series_name
        self.max_retries = max_retries
        self.num_segments = num_segments  # nombre de connexions parallèles par fichier
        self.chunk_size = chunk_size  # 256 Ko par défaut, au lieu de 8 Ko
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124"
        }
        # Limites de connexions HTTP : on autorise assez de connexions
        # simultanées pour couvrir les segments + un peu de marge.
        self._limits = httpx.Limits(
            max_connections=num_segments + 4,
            max_keepalive_connections=num_segments + 4,
        )

    def _get_filename(self, response, url, override_name=None):
        if override_name:
            return override_name

        cd = response.headers.get("Content-Disposition")
        if cd:
            fname = re.findall("filename=(.+)", cd)
            if fname:
                return fname[0].strip().strip('"')

        path = urlparse(url).path
        name = os.path.basename(path)
        if not name or "." not in name:
            name = f"video_{int(time.time())}.mp4"
        return name

    def _check_existing(self, path, remote_size):
        if not os.path.exists(path):
            return 0, "wb"

        local_size = os.path.getsize(path)
        if local_size == remote_size:
            return -1, None

        if local_size > remote_size:
            return 0, "wb"

        return local_size, "ab"

    def _make_segment_ranges(self, total_size: int):
        """Découpe [0, total_size) en self.num_segments tranches contiguës."""
        n = max(1, self.num_segments)
        base = total_size // n
        ranges = []
        start = 0
        for i in range(n):
            end = total_size - 1 if i == n - 1 else start + base - 1
            if start > end:
                break
            ranges.append((start, end))
            start = end + 1
        return ranges

    async def _download_segment(
        self, client: httpx.AsyncClient, url: str, start: int, end: int,
        path: str, progress, task,
    ):
        """Télécharge l'intervalle [start, end] (inclusif) et l'écrit à la bonne position."""
        headers = self.headers.copy()
        headers["Range"] = f"bytes={start}-{end}"

        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                async with client.stream("GET", url, headers=headers) as r:
                    r.raise_for_status()
                    written = 0
                    expected = end - start + 1
                    with open(path, "r+b") as f:
                        f.seek(start)
                        async for chunk in r.aiter_bytes(chunk_size=self.chunk_size):
                            f.write(chunk)
                            written += len(chunk)
                            if progress and task is not None:
                                progress.update(task, advance=len(chunk))
                    return
            except Exception as e:
                last_err = e
                if attempt < self.max_retries:
                    # On retire la progression déjà comptée pour ce segment
                    # avant de retenter, pour ne pas fausser la barre globale.
                    if progress and task is not None and 'written' in locals() and written:
                        progress.update(task, advance=-written)
                    await asyncio.sleep(2 * (attempt + 1))
                else:
                    raise last_err

    async def _supports_range(self, client: httpx.AsyncClient, url: str, total_size: int) -> bool:
        """Vérifie si le serveur honore réellement les requêtes Range."""
        if total_size <= 0:
            return False
        try:
            probe_end = min(1023, total_size - 1)
            r = await client.get(url, headers={**self.headers, "Range": f"bytes=0-{probe_end}"})
            return r.status_code == 206
        except Exception:
            return False

    async def _perform_sequential_download(
        self, client: httpx.AsyncClient, url, path, resume_byte, total_size,
        ep_num: int, progress=None, mode="wb",
    ):
        """Fallback : un seul flux, comme avant (avec resume si possible)."""
        headers = self.headers.copy()
        if resume_byte > 0:
            headers["Range"] = f"bytes={resume_byte}-"

        async with client.stream("GET", url, headers=headers) as r:
            r.raise_for_status()
            with open(path, mode) as f:
                if progress:
                    task = progress.add_task(
                        f"[green]Downloading ep{ep_num:02d}",
                        total=total_size,
                        completed=resume_byte,
                    )
                    async for chunk in r.aiter_bytes(chunk_size=self.chunk_size):
                        f.write(chunk)
                        progress.update(task, advance=len(chunk))
                else:
                    _console.print(f"[blue]Downloading ep{ep_num:02d}")
                    with Progress(
                        SpinnerColumn(),
                        TextColumn("{task.description}"),
                        BarColumn(),
                        DownloadColumn(),
                        TransferSpeedColumn(),
                        TimeRemainingColumn(),
                        console=_console,
                    ) as inner_progress:
                        task = inner_progress.add_task(
                            "[green]Downloading ",
                            total=total_size,
                            completed=resume_byte,
                        )
                        async for chunk in r.aiter_bytes(chunk_size=self.chunk_size):
                            f.write(chunk)
                            inner_progress.update(task, advance=len(chunk))

    async def _perform_parallel_download(
        self, client: httpx.AsyncClient, url, path, total_size, ep_num: int, progress=None,
    ):
        """Télécharge le fichier en plusieurs segments en parallèle."""
        # Pré-allocation du fichier à la taille finale pour pouvoir
        # écrire à des offsets arbitraires depuis plusieurs coroutines.
        with open(path, "wb") as f:
            if total_size > 0:
                f.truncate(total_size)

        ranges = self._make_segment_ranges(total_size)

        owns_progress = progress is None
        if owns_progress:
            progress = Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=_console,
            )
            progress.start()

        try:
            task = progress.add_task(
                f"[green]Downloading ep{ep_num:02d} ({len(ranges)} segments)",
                total=total_size,
            )
            jobs = [
                self._download_segment(client, url, start, end, path, progress, task)
                for start, end in ranges
            ]
            await asyncio.gather(*jobs)
        finally:
            if owns_progress:
                progress.stop()

    async def download(self, url: str, ep_num: int, progress=None):
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

        filename = f"{self.series_name} ep{ep_num:02d}.mp4"
        filename = os.path.join(self.output_dir, filename)

        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    headers=self.headers,
                    follow_redirects=True,
                    timeout=30,
                    limits=self._limits,
                ) as client:
                    r = await client.head(url)
                    remote_size = int(r.headers.get("Content-Length", 0))
                    final_name = self._get_filename(r, url, filename)
                    output_path = os.path.join(self.output_dir, final_name)

                    resume_byte, mode = self._check_existing(output_path, remote_size)
                    if resume_byte == -1:
                        return output_path, True

                    # On n'utilise le mode parallèle que si :
                    # - on connaît la taille totale
                    # - on repart de zéro (le multi-segment ne gère pas
                    #   le resume partiel proprement, donc fallback sequential
                    #   si un fichier partiel existe déjà)
                    # - le serveur honore réellement les Range requests
                    can_parallel = (
                        remote_size > 0
                        and resume_byte == 0
                        and await self._supports_range(client, url, remote_size)
                    )

                    if can_parallel:
                        await self._perform_parallel_download(
                            client, url, output_path, remote_size, ep_num, progress
                        )
                    else:
                        await self._perform_sequential_download(
                            client, url, output_path, resume_byte, remote_size,
                            ep_num, progress, mode,
                        )

                return output_path, False
            except Exception as e:
                if attempt < self.max_retries:
                    error_console = progress.console if progress else _console
                    error_console.print(f"[yellow]Error: {e}. Retrying in 5s...[/]")
                    await asyncio.sleep(5)
                else:
                    error_console = progress.console if progress else _console
                    error_console.print(
                        f"[red]Failed after {self.max_retries} attempts.[/]"
                    )
                    raise e
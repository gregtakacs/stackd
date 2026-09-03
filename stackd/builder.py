"""`stackctl build` — produce a model's local image via the Docker Engine /build
API (through the same scoped socket-proxy `DockerApiRunner` uses). stackd runs in
its own container with no `docker` CLI, so it tars the build context itself and
streams it to the daemon.

Only the CLI imports this; `serve.py` and the smoke suite never touch it. Stdlib
only (`tarfile`, `io`, `json`, `urllib`).
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import urllib.error
import urllib.parse
import urllib.request

# The ROCm ComfyUI build is ~30 min; give the streamed response plenty of room.
_BUILD_READ_TIMEOUT_S = 3 * 60 * 60


def _tar_context(context_dir: str) -> bytes:
    """gzip tar of `context_dir` (its contents at the archive root), honoring a
    `.dockerignore` only for the obvious noise. Kept deliberately simple."""
    if not os.path.isdir(context_dir):
        raise FileNotFoundError(f"build context not found: {context_dir}")
    ignore = {".git", "__pycache__", ".DS_Store"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for root, dirs, files in os.walk(context_dir):
            dirs[:] = [d for d in dirs if d not in ignore]
            for name in files:
                if name in ignore:
                    continue
                full = os.path.join(root, name)
                arc = os.path.relpath(full, context_dir)
                tf.add(full, arcname=arc, recursive=False)
    return buf.getvalue()


def build_image(
    base_url: str,
    tag: str,
    context_dir: str,
    *,
    dockerfile: str = "Dockerfile",
    buildargs: dict[str, str] | None = None,
    on_line=print,
) -> None:
    """POST the tarred context to {base_url}/build and stream the result. Raises
    RuntimeError on a build error line or a non-2xx status."""
    q = urllib.parse.urlencode({
        "t": tag,
        "dockerfile": dockerfile,
        "buildargs": json.dumps(buildargs or {}),
        "rm": "1",
        "pull": "0",
    })
    body = _tar_context(context_dir)
    on_line(f"[build] {tag}  context={context_dir} ({len(body) // 1024} KiB)  dockerfile={dockerfile}")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/build?{q}", data=body, method="POST",
        headers={"content-type": "application/x-tar"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=_BUILD_READ_TIMEOUT_S)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"/build returned HTTP {e.code}: {e.read()[:500]!r}") from None

    with resp:
        for raw in resp:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                on_line(raw.decode("utf-8", "replace"))
                continue
            if "stream" in msg:
                line = msg["stream"].rstrip("\n")
                if line:
                    on_line(line)
            elif "error" in msg:
                raise RuntimeError(msg.get("error") or msg.get("errorDetail", {}).get("message", "build failed"))
            elif "status" in msg:
                on_line(f"  {msg['status']}{(' ' + msg['progress']) if msg.get('progress') else ''}")
    on_line(f"[build] {tag}  done")

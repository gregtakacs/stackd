# stackd

A single daemon that is three things for a small multi-GPU host:

- **an OpenAI-compatible proxy** — `POST /v1/chat/completions`, `/v1/completions`,
  `/v1/embeddings`, `GET /v1/models` (plus `GET /v1/model/info`, a LiteLLM-shaped
  metadata view so clients like Cline auto-pick the context window + capabilities),
  with bearer auth, per-model presets, streaming reverse-proxy, and a usage/cost ledger
- **a two-device model orchestrator** — you declare the models you want and group them
  into *profiles*; a fit-solver places each on the GPU it fits (dedicated VRAM **and**
  shared/unified RAM are both balanced), and stackd creates / starts / stops the engine
  containers (llama.cpp, vLLM, ComfyUI) on demand, rebuilding only the delta on a
  profile switch
- **an image-generation MCP** — `generate_image` / `edit_image` / `stylize_image` over
  MCP streamable-HTTP at `/mcp`, backed by an *elastic image tier*: stackd runs one
  ComfyUI in whatever VRAM the active profile leaves free, picking the model from a
  preference list and swapping it on demand — never at an LLM's expense

- **a web dashboard** — `GET /` serves a single-page UI (token-gated, no build step): a
  live map of what is loaded on which device, the profile list with activate / pin / evict,
  the elastic image tier with model / capability swap, a scheduler-event feed, per-engine
  telemetry, and the usage / energy / savings history

Only runtime dependency for the core is **PyYAML** (the image MCP adds `mcp`, `httpx`,
`pillow`, `uvicorn` via the `imagegen` extra; the dashboard vendors uPlot, no install).
Config is stdlib `dataclasses` built through a small typed constructor.

## Run it

See **[`deploy/`](deploy/)** — a `docker-compose.yml` (+ `.env.example` + README) that
brings up stackd on localhost, optionally with Open WebUI:

```bash
cd deploy
cp .env.example .env && $EDITOR .env      # data dir, GPU sizes, an API key
docker compose up -d                      # OpenAI proxy on :11444, image MCP on :8000
docker compose --profile full up -d       # + Open WebUI on :3000
```

Then point any OpenAI client at `http://localhost:11444/v1`, or open
`http://localhost:11444/` for the dashboard (unlock with the same API token).

## Concepts

**Model** (`config/models/<name>.yaml`) — a servable model + how to run it: an engine
`template` (`llamacpp-cuda` | `llamacpp-vulkan` | `vllm-cuda` | `sglang-pennyroyal` |
`comfyui`), a `budget:` (VRAM/RAM footprint — a starting estimate you refine with
`stackctl bench`), a `placement:` device-preference list, and the API `serves:` names
clients ask for. The *device* is chosen by the solver, not pinned.
(`sglang-pennyroyal` is the RTX PRO 6000 / sm120 SGLang fork
[`jpezzulli/sglang-rtxpro6000`](https://github.com/jpezzulli/sglang-rtxpro6000) —
`stackctl build`s its own image; **not** vanilla SGLang. The adapter is generic
cmd_extra passthrough, so a `sglang-cuda` on a vendor image is a one-line add.)

**Profile** (`config/profiles/<name>.yaml`) — a priority-ordered list of model names.
The solver places each on the first device it fits; the highest-priority profile that
serves a requested model wins, and a request for a model only a higher-priority profile
serves triggers an automatic switch. One profile is `default: true` (the floor);
another can `idle_evict:` back down to it.

**Pools** (`config/pools.yaml`) / **devices** (`config/devices.yaml`) — the memory
budgets. A *dedicated* pool is a discrete GPU's VRAM; a *shared* pool is unified/system
RAM, charged for an integrated GPU's GTT, a host reserve, model RAM spill, and load
slack. `stackctl validate` proves a profile balances both before anything spawns.

**Media tier** (`config/media/image.yaml`; `video`/`audio` later) — an *elastic*
image-generation layer that is **not** a profile member. After a profile's LLM models
are placed, stackd computes the free VRAM per device and runs one ComfyUI there,
loading the first entry from an ordered `prefer:` list that fits. It stays put (sticky)
until the profile changes or a request needs a capability the resident model lacks —
then it swaps, auto-downgrading to a smaller capable model if the best doesn't fit and
saying so. It is torn down before LLMs spawn on a switch and can never displace one.
Drive it with `stackctl image` or `POST /image/{model,capability}`.

**Config is environment-driven.** Every box-specific value in `config/*.yaml` is a
`${VAR}` resolved at load from the environment + a bind-mounted `.env` (see
`deploy/.env.example`). Edit `.env` or a config file, then `stackctl reload` — no
restart; an engine whose spec changed is recreated, a pool/size change just re-solves.

**Web assets are live; Python is not.** `web/dashboard.html` and `/static/*` are
re-read the moment the file's mtime/size changes, so a UI-only fix lands without
restarting the daemon — copy it into the running container or rebuild, and a tab
that is already open notices the new build id and offers to reload (see `WEB_REV`
in `dashboard.html`). Python is baked into the image with no source bind-mount, so
a `.py` change always needs `docker compose build stackd && docker compose up -d`.

**Config overlay.** `STACKD_CONFIG_OVERLAY` (or `-O`) is a second config dir that wins
**per file** — mount your private `pools.yaml` / `models/*.yaml` / `catalog/*.json`
there and keep the shipped `config/` a pristine generic example. A new file adds; an
empty overlay file deletes the base entry.

## `stackctl`

```
stackctl show | validate | status          # inspect config / what fits / what's running
stackctl reload                            # re-read config + .env, re-converge (talks to the daemon)
stackctl image show | use <m> | capability <v>   # inspect / swap the elastic image tier
stackctl bench <model> [--verify <prof>]   # measure a model's real VRAM/RAM footprint
stackctl build <target>                    # build a local image — a model, or "image:vulkan"
stackctl serve                             # the daemon: OpenAI front + MCP + supervisor
stackctl down                              # tear down every engine the daemon spawned, then stop it
stackctl --fake use|tick|route|status      # dry-run the control loop with no GPUs/docker
```

Switch profiles on a running daemon via the control plane, not `stackctl use`:

```
curl -XPOST localhost:11444/profiles/<name>/activate -H "Authorization: Bearer $STACKD_API_KEY"
```

## Layout

```
stackd/
  config/            models.py (dataclass schema) + _build.py + loader.py (${VAR} interpolation)
  engines/           EngineAdapter ABC + one module per template (llamacpp / vllm / comfyui)
  runner.py          LaunchSpec + Runner: DockerApiRunner (scoped socket-proxy), LocalRunner, FakeRunner
  solver.py          placement fit-solver + model_identity (delta-reconcile key) + headroom()
  reconciler.py      converge() + tick() + the image tier — teardown/spawn ordering, cuda drain barrier, crash-restart
  manager.py         priority gating, stand-in routing, idle-evict, capabilities(), reload_config(), set_image()
  serve.py           OpenAI HTTP front + control plane + /capabilities + /image + /comfyui + /register + /savings + /reload
                     + the dashboard: / (shell) /static/* /events /gpu /host /history /profiles /engine|/slots/<stack>
  events.py telemetry.py   ring buffer of scheduler actions + nvidia-smi / amdgpu-sysfs / /proc + per-engine metric parsing
  web/               dashboard.html (single page, no build) + vendored uplot
  cleaner.py         the ComfyUI scratch janitor (POST /cleaner/on|off)
  builder.py         `stackctl build` — image builds via the Docker /build API
  store.py           SQLite: per-user OWU keys + usage/cost ledger + prices + energy
  catalog.py bench.py probe.py pricing.py validator.py planner.py
  imagegen/          the image MCP: tools.py + workflow graphs + the ComfyUI custom node + Dockerfiles
  llamacpp_image/    Dockerfile.cuda — the CUDA llama.cpp build (`chat` model's container.build)
config/              a worked example config — replace models/ + profiles/ + media/ with your own
deploy/              docker-compose + .env.example + setup guide
tests/               smoke*.py (stdlib, no deps) + test_validator.py (pytest)
stackd-images.audit.yml   compose-shaped manifest (NOT a real compose file) listing every image/build
                     stackd creates directly via the Docker API — none of it appears in any real
                     compose file, so it's otherwise invisible to Docker-Tools/compose-version-audit
```

## Tests

```bash
for t in tests/smoke*.py; do python3 "$t"; done   # ~280 checks, stdlib only
pytest                                             # test_validator.py, if available
```

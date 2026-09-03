# stackd

A single daemon that is three things for a small multi-GPU host:

- **an OpenAI-compatible proxy** — `POST /v1/chat/completions`, `/v1/completions`,
  `/v1/embeddings`, `GET /v1/models`, with bearer auth, per-model presets, streaming
  reverse-proxy, and a usage/cost ledger
- **a two-device model orchestrator** — you declare the models you want and group them
  into *profiles*; a fit-solver places each on the GPU it fits (dedicated VRAM **and**
  shared/unified RAM are both balanced), and stackd creates / starts / stops the engine
  containers (llama.cpp, vLLM, ComfyUI) on demand, rebuilding only the delta on a
  profile switch
- **an image-generation MCP** — `generate_image` / `edit_image` / `stylize_image` over
  MCP streamable-HTTP at `/mcp`, backed by a ComfyUI stackd manages

Only runtime dependency for the core is **PyYAML** (the image MCP adds `mcp`, `httpx`,
`pillow`, `uvicorn` via the `imagegen` extra). Config is stdlib `dataclasses` built
through a small typed constructor.

## Run it

See **[`deploy/`](deploy/)** — a `docker-compose.yml` (+ `.env.example` + README) that
brings up stackd on localhost, optionally with Open WebUI:

```bash
cd deploy
cp .env.example .env && $EDITOR .env      # data dir, GPU sizes, an API key
docker compose up -d                      # OpenAI proxy on :11444, image MCP on :8000
docker compose --profile full up -d       # + Open WebUI on :3000
```

Then point any OpenAI client at `http://localhost:11444/v1`.

## Concepts

**Model** (`config/models/<name>.yaml`) — a servable model + how to run it: an engine
`template` (`llamacpp-cuda` | `llamacpp-vulkan` | `vllm-cuda` | `comfyui`), a `budget:`
(VRAM/RAM footprint — a starting estimate you refine with `stackctl bench`), a
`placement:` device-preference list, and the API `serves:` names clients ask for. The
*device* is chosen by the solver, not pinned.

**Profile** (`config/profiles/<name>.yaml`) — a priority-ordered list of model names.
The solver places each on the first device it fits; the highest-priority profile that
serves a requested model wins, and a request for a model only a higher-priority profile
serves triggers an automatic switch. One profile is `default: true` (the floor);
another can `idle_evict:` back down to it.

**Pools** (`config/pools.yaml`) / **devices** (`config/devices.yaml`) — the memory
budgets. A *dedicated* pool is a discrete GPU's VRAM; a *shared* pool is unified/system
RAM, charged for an integrated GPU's GTT, a host reserve, model RAM spill, and load
slack. `stackctl validate` proves a profile balances both before anything spawns.

**Config is environment-driven.** Every box-specific value in `config/*.yaml` is a
`${VAR}` resolved at load from the environment + a bind-mounted `.env` (see
`deploy/.env.example`). Edit `.env` or a config file, then `stackctl reload` — no
restart; an engine whose spec changed is recreated, a pool/size change just re-solves.

**Config overlay.** `STACKD_CONFIG_OVERLAY` (or `-O`) is a second config dir that wins
**per file** — mount your private `pools.yaml` / `models/*.yaml` / `catalog/*.json`
there and keep the shipped `config/` a pristine generic example. A new file adds; an
empty overlay file deletes the base entry.

## `stackctl`

```
stackctl show | validate | status         # inspect config / what fits / what's running
stackctl reload                           # re-read config + .env, re-converge (talks to the daemon)
stackctl bench <model> [--verify <prof>]  # measure a model's real VRAM/RAM footprint
stackctl build <model>                    # build a model's local image (container.build)
stackctl serve                            # the daemon: OpenAI front + MCP + supervisor
stackctl --fake use|tick|route|status     # dry-run the control loop with no GPUs/docker
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
  solver.py          placement fit-solver + model_identity (delta-reconcile key)
  reconciler.py      converge() + tick() — teardown/spawn ordering, cuda drain barrier, crash-restart
  manager.py         priority gating, stand-in routing, idle-evict, capabilities(), reload_config()
  serve.py           OpenAI HTTP front + control plane + /capabilities + /comfyui + /register + /savings + /reload
  cleaner.py         the ComfyUI scratch janitor (POST /cleaner/on|off)
  builder.py         `stackctl build` — image builds via the Docker /build API
  store.py           SQLite: per-user OWU keys + usage/cost ledger + prices + energy
  catalog.py bench.py probe.py pricing.py validator.py planner.py
  imagegen/          the image MCP: tools.py + workflow graphs + the ComfyUI custom node + Dockerfiles
config/              a worked example config (two profiles) — replace models/ + profiles/ with your own
deploy/              docker-compose + .env.example + setup guide
tests/               smoke*.py (stdlib, no deps) + test_validator.py (pytest)
```

## Tests

```bash
for t in tests/smoke*.py; do python3 "$t"; done   # ~190 checks, stdlib only
pytest                                             # test_validator.py, if available
```

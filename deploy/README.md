# stackd — standalone deployment

`stackd` is three things in one daemon:

- an **OpenAI-compatible proxy** — `POST /v1/chat/completions`, `/v1/completions`,
  `/v1/embeddings`, `GET /v1/models`
- a **two-device model orchestrator** — you declare the models you want and group them
  into *profiles*; a fit-solver places each on the GPU it fits, and stackd creates /
  starts / stops the engine containers (llama.cpp, vLLM, ComfyUI) on demand
- an **image-generation MCP** — `generate_image` / `edit_image` / `stylize_image` over
  MCP's streamable-HTTP transport at `/mcp`, backed by a ComfyUI it manages

This directory runs it on **localhost, no reverse proxy**.

```
http://localhost:11444/v1     OpenAI endpoint   (Authorization: Bearer $STACKD_API_KEY)
http://localhost:11444/…      control plane     (/status /capabilities /profiles/<p>/activate /reload …)
http://localhost:8000/mcp     image MCP         (Authorization: Bearer $MCP_API_KEY)
http://localhost:3000         Open WebUI        (--profile full)
```

---

## Requirements

- Docker + Compose v2
- **NVIDIA**: `nvidia-container-toolkit`, and `runtime: nvidia` registered in
  `/etc/docker/daemon.json`. stackd itself only runs `nvidia-smi`; the engine
  containers it creates get the GPU. No NVIDIA GPU? see *Different hardware* below.
- A directory of model weights on the host (`$DOCKERDIR/appdata/…`, see `.env`).
- Optional (`--profile full`): nothing extra — Open WebUI + SearXNG images are pulled.

## Quick start — LLM proxy

```bash
cp .env.example .env
$EDITOR .env            # set DOCKERDIR, STACKD_API_KEY, and your GPU sizes
$EDITOR ../config/…     # point the model files at your weights — see below

docker compose up -d
docker compose logs -f stackd
```

Then:

```bash
curl http://localhost:11444/v1/models -H "Authorization: Bearer $STACKD_API_KEY"

curl http://localhost:11444/v1/chat/completions \
  -H "Authorization: Bearer $STACKD_API_KEY" -H 'content-type: application/json' \
  -d '{"model":"<one of your serves: names>","messages":[{"role":"user","content":"hi"}]}'
```

`stackd` boots into the **default profile** and converges it (spawns its engine
containers) before accepting traffic. A request for a model a *higher-priority* profile
serves will switch profiles automatically.

---

## Pointing it at your models

`../config/` ships a **worked example** tuned for a reference box (1× large CUDA GPU + an AMD
iGPU; Qwen3 27B, a vLLM coder, Flux.2). It's bind-mounted into the container
and **live-editable** — edit a file, then `docker compose exec stackd stackctl reload`.

```
config/
  pools.yaml        memory budgets (cuda_vram, host_unified)  ← mostly from .env
  devices.yaml      cuda0 (cuda) / igpu0 (vulkan)             ← mostly from .env
  runtime.yaml      engine base images, per-backend knobs, the ComfyUI cleaner
  models/*.yaml     one file per engine
  profiles/*.yaml   priority-ordered lists of model names
```

**Replace the `models/` and `profiles/` files with your own.** A minimal single-model
setup:

`config/models/my-chat.yaml`
```yaml
model: my-chat
engine:
  template: llamacpp-cuda          # llamacpp-cuda | llamacpp-vulkan | vllm-cuda | comfyui
  model: Qwen2.5-7B-Instruct-Q4_K_M   # basename of the .gguf under $DOCKERDIR/appdata/llm-models/
  params:
    ctx: 32768
    parallel: 4
  container:
    entrypoint: ["llama-server"]   # the ggml-org image needs this
budget: { vram_gib: 6, ram_gib: 1 }   # a starting estimate — refine with `stackctl bench`
placement: { devices: [cuda0] }
serves:
  - { api_name: my-chat, preset: {} }   # the model id OpenAI clients ask for
```

`config/profiles/main.yaml`
```yaml
profile: main
priority: 100
default: true
models: [my-chat]
```

Delete the example `models/*.yaml` / `profiles/*.yaml` you don't want. `stackctl
validate` checks it fits your pools; `stackctl reload` applies it.

- **vLLM**: `template: vllm-cuda`, put the full server args in `container.cmd_extra`,
  bind-mount the weights in `container.mounts` (see `models/coding-flash.yaml`).
- **Image gen**: `template: comfyui` — one model per ComfyUI instance; stackd creates
  the container and the MCP advertises its capabilities. The CUDA image is stock; the
  ROCm image is a local build: `docker compose exec stackd stackctl build <model>`.

## `stackctl`

```bash
docker compose exec stackd stackctl status          # what's running, pool usage
docker compose exec stackd stackctl validate        # does every profile fit?
docker compose exec stackd stackctl reload          # re-read config + .env, re-converge (no restart)
docker compose exec stackd stackctl use <profile>   # NOTE: only when the daemon is NOT running;
                                                    #   with it running use the HTTP control plane:
curl -X POST http://localhost:11444/profiles/<profile>/activate -H "Authorization: Bearer $STACKD_API_KEY"
docker compose exec stackd stackctl bench <model>   # measure a model's real VRAM/RAM footprint
docker compose exec stackd stackctl build <model>   # build a model's local image (container.build)
```

---

## Full stack — chat UI + image round-trip

```bash
docker compose --profile full up -d
```

Open WebUI comes up at **http://localhost:3000**, already pointed at stackd as its model
backend. First run:

1. Create the admin account.
2. **Register your OWU key with stackd** so image generations save to *your* account:
   Open WebUI → *Settings → Account → API Keys*, copy it, then open
   `http://localhost:11444/register` and paste it. (This backs `edit_image` /
   `stylize_image`, which act on the last image in the chat.)
3. **Add the image MCP** as a tool server: Open WebUI → *Admin → Settings → Tools →
   Add* → URL `http://stackd:8000/mcp`, Auth *Bearer*, key = your `MCP_API_KEY`.

Now `generate_image` / `edit_image` / `stylize_image` are available in chat, and the
model that runs is whichever ComfyUI the active profile has resident.

---

## The edit → reload loop

`.env` and `../config/` are both bind-mounted. Change a value:

```bash
$EDITOR .env                 # or ../config/models/foo.yaml
docker compose exec stackd stackctl reload
```

No container recreate. An engine whose spec changed (image tag, mounts, env) is
recreated by that reload; a pool/size change just re-runs the solver.

**Still needs `docker compose up -d stackd`:** `DOCKERDIR`, `STACKD_NETWORK`, and the
published ports — those shape the compose file itself, not just stackd's config.

---

## Different hardware

- **No iGPU** — set `IGPU_VRAM_BUDGET_GIB=1` and don't put any model on `igpu0`
  (`placement: { devices: [cuda0] }` everywhere). Remove `models/*` that target it.
- **AMD dGPU / no NVIDIA** — remove `runtime: nvidia` from `stackd` in
  `docker-compose.yml`; in `config/devices.yaml` change `cuda0`'s backend to `vulkan`
  (or `rocm`) and drop the `cuda` `device_profile`; the drain barrier + `nvidia-smi`
  probes simply no-op.
- **Multiple GPUs** — add more `devices:` entries and give each engine a
  `placement.devices` preference list.
- **More/less RAM/VRAM** — `CUDA_VRAM_GIB`, `HOST_RAM_GIB`, `HOST_RESERVE_GIB` in `.env`.
  Measure `HOST_RESERVE_GIB` as the RAM used with stackd's engines stopped.

## Notes

- `docker-api` (tecnativa/docker-socket-proxy) is **privileged** and holds the docker
  socket — stackd creates containers through it. It's not published; keep it that way.
- No auth: leave `STACKD_API_KEY` / `MCP_API_KEY` blank in `.env`. Only do this if
  11444/8000 aren't reachable off the host.
- State (ledger, per-user keys, active profile) lives in the `stackd_data` volume.
- Engine containers stackd creates are named `stackd-<model>` (or the `container.name`
  you set) and join the `${STACKD_NETWORK}` bridge.

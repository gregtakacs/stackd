# stackd — standalone deployment

`stackd` is three things in one daemon:

- an **OpenAI-compatible proxy** — `POST /v1/chat/completions`, `/v1/completions`,
  `/v1/embeddings`, `GET /v1/models`, `GET /v1/model/info` (LiteLLM-shaped metadata)
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
  models/*.yaml     one file per LLM engine
  profiles/*.yaml   priority-ordered lists of model names
  media/image.yaml  the elastic image tier (ComfyUI): per-backend container + `prefer:` catalog
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

### Keep your config private with an overlay

To leave `config/` a pristine generic example and version-control your real config
separately, use a **per-file overlay**:

```bash
mkdir -p config.local/models config.local/catalog
cp ../config/models/my-chat.yaml config.local/models/     # then edit your copy
```

In `.env` set `STACKD_CONFIG_OVERLAY=/app/config.local` and uncomment the
`./config.local:/app/config.local:ro` volume in `docker-compose.yml`. Now a file in
`config.local/` **wins over** the same-named file in `config/`; a new name adds; an
**empty** overlay file deletes that base entry. `pools.yaml` / `devices.yaml` /
`runtime.yaml` / `catalog/*.json` / `media/*.yaml` overlay the same way. `stackctl reload` re-reads it.

Keep `config.local/` in its own private git repo (`docker-compose.yml` + `.env` +
`config.local/` = your whole deployment); pull this repo for code only.

- **vLLM**: `template: vllm-cuda`, put the full server args in `container.cmd_extra`,
  bind-mount the weights in `container.mounts` (see `models/coding.yaml`).
- **Image gen** is not a model file — it's `config/media/image.yaml`: a `containers:`
  block per GPU backend (`cuda` stock, `vulkan` a local build via
  `docker compose exec stackd stackctl build image:vulkan`) and an ordered `prefer:`
  list of loadable pipelines with a `footprint_gib` each. stackd runs one ComfyUI in
  whatever VRAM a profile leaves free and loads the first entry that fits; swap it with
  `stackctl image use <model>` or let a `generate_image` / `edit_image` call escalate
  automatically. It's torn down before LLMs spawn on a switch and never displaces one.

## `stackctl`

```bash
docker compose exec stackd stackctl status          # what's running, pool usage
docker compose exec stackd stackctl validate        # does every profile fit?
docker compose exec stackd stackctl reload          # re-read config + .env, re-converge (no restart)
docker compose exec stackd stackctl image show      # resident image model + free VRAM + prefer list
docker compose exec stackd stackctl image use flux2-klein     # swap the image pipeline
docker compose exec stackd stackctl use <profile>   # NOTE: only when the daemon is NOT running;
                                                    #   with it running use the HTTP control plane:
curl -X POST http://localhost:11444/profiles/<profile>/activate -H "Authorization: Bearer $STACKD_API_KEY"
docker compose exec stackd stackctl bench <model>   # measure a model's real VRAM/RAM footprint
docker compose exec stackd stackctl build image:vulkan   # build a local image (a model name, or "<kind>:<backend>")
docker compose exec stackd stackctl down            # tear down every engine, then stop the daemon
```

### Bringing it down

The LLM / image engines are containers **stackd** creates through the socket
proxy — they are not in this compose file, so a plain `docker compose down`
stops `stackd` but leaves them orphaned and holding VRAM. Either:

```bash
docker compose exec stackd stackctl down   # stops every engine, then the daemon exits
docker compose down                         # now removes stackd + the socket proxy
```

or set `STACKD_TEARDOWN_ON_SIGTERM=1` in `.env` so a plain `docker compose down`
does the teardown itself. Leave it unset if you use `docker compose restart
stackd` or rebuilds — those SIGTERM too, and there you *want* the engines kept
(stackd re-adopts the healthy ones on the next boot).

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

Now `generate_image` / `edit_image` / `stylize_image` are available in chat. The
pipeline that runs is whatever the elastic image tier has resident for the active
profile; a call for a capability the resident model lacks makes the tier swap to one
that has it (auto-downgrading to a smaller model if the best doesn't fit the free VRAM,
and telling you so in the reply).

---

## The edit → reload loop

`.env` and `../config/` are both bind-mounted. Change a value:

```bash
$EDITOR .env                 # or ../config/models/foo.yaml
docker compose exec stackd stackctl reload
```

No container recreate. An engine whose spec changed (image tag, mounts, env) is
recreated by that reload; a pool/size change just re-runs the solver. Editing
`config/media/image.yaml` works the same way — a changed `containers:` block rebuilds
the ComfyUI, a reordered `prefer:` / retuned `footprint_gib` takes effect on the next
converge.

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
  Measure `HOST_RESERVE_GIB` as the RAM used with stackd's engines stopped, and set
  `HOST_RAM_GIB` to `MemTotal` from `/proc/meminfo` rather than the size printed on
  the kit (a 128 GB box reports ~124.4 GiB). Rounding the total up is a loan against
  the reserve, and the solver can only spend it once.
- **iGPU budget above half of RAM** — the amdgpu GTT window is `ttm.pages_limit`
  reported in bytes (`mem_info_gtt_total`), and the kernel auto-sets it to **half of
  MemTotal**. So `IGPU_VRAM_BUDGET_GIB` above that is not a hardware impossibility,
  it is an un-booted knob: stackd clamps to the window and says so (`stackctl
  validate`, `stackctl status`, and the dashboard lane). To raise it, add to
  `GRUB_CMDLINE_LINUX_DEFAULT` (`32505856` pages = 124 GiB on a 128 GB box):

  ```
  ttm.pages_limit=32505856 ttm.page_pool_size=32505856
  ```

  then `sudo update-grub` and reboot; `dmesg | grep 'GTT memory ready'` should show
  the new size and the clamp flag disappears. **The reboot is not optional** —
  `/sys/module/ttm/parameters/pages_limit` is mode `0644 root` and accepts a write
  at runtime (it reads back exactly what you wrote), but `mem_info_gtt_total` does
  not move: the GTT zone is sized once, at `amdgpu_ttm_init`, from the value present
  at module load. Verified on this box — wrote 32505856, read it back, GTT stayed at
  66812620800 (62.2 GiB), so the daemon kept clamping. Recovery-mode entries are
  built from `GRUB_CMDLINE_LINUX` (not `..._DEFAULT`), so if you want the widened
  window in recovery too, put the args in both. `sh deploy/check-gtt-window.sh`
  re-checks all four layers (cmdline -> module param -> `mem_info_gtt_total` ->
  what stackd books) after a reboot. On kernels where TTM is split out
  (6.17+), add the `amdttm.*` equivalents — plain `ttm.*` is inert there, and vice
  versa: this box's `7.0.0` kernel has no `amdttm` module at all. **Widen
  `HOST_RESERVE_GIB` in the same change** — with the aperture open the reserve is
  the only guard left between a booked plan and an OOM.

## Notes

- `docker-api` (tecnativa/docker-socket-proxy) is **privileged** and holds the docker
  socket — stackd creates containers through it. It's not published; keep it that way.
- No auth: leave `STACKD_API_KEY` / `MCP_API_KEY` blank in `.env`. Only do this if
  11444/8000 aren't reachable off the host.
- State (ledger, per-user keys, active profile) lives in the `stackd_data` volume.
- Engine containers stackd creates are named `stackd-<model>` (or the `container.name`
  you set) and join the `${STACKD_NETWORK}` bridge.

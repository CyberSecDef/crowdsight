# Provisioning

Everything CrowdSight needs from the internet, gathered once, so the stack can run with
no network at all afterwards.

Read this **before** disconnecting the machine. Several of these assets are fetched
lazily at runtime by a dependency rather than installed by a package manager, and the
failure modes are quiet — one of them degrades every simulation without producing an
error.

---

## What needs the network, and when

| Asset | When | Sealed afterwards |
|---|---|---|
| Docker base images | `docker compose build` | Yes |
| Python packages | `docker compose build` | Yes |
| npm packages (frontend) | `docker compose build frontend` | Yes |
| Model weights (~9 GB) | one-time pull, below | Yes |
| tiktoken BPE encodings | baked in at build | Yes |
| `Twitter/twhin-bert-base` | baked in at build | Yes |

Build time needs the network; run time does not. Rebuilding an image needs it again.

---

## The one-time pull

```bash
git clone https://github.com/CyberSecDef/crowdsight && cd crowdsight
cp .env.example .env          # defaults are already local-only; set NEO4J_PASSWORD

docker compose build          # base images, pip, npm

# Temporarily attach Ollama to a routable network and pull the models
docker compose -f docker-compose.yml -f docker-compose.provision.yml up -d ollama
docker compose exec ollama ollama pull qwen2.5:14b
docker compose exec ollama ollama pull nomic-embed-text
docker compose -f docker-compose.yml -f docker-compose.provision.yml down
```

`docker-compose.provision.yml` is a small overlay that puts Ollama on a routable network
for the duration of the pull. **It must never be used at runtime.** It exists as a
separate file precisely so that using it is a deliberate act rather than a flag someone
leaves set.

---

## Re-sealing, and proving it took

Bring the stack up normally — no overlay — and check:

```bash
docker compose up -d
docker compose exec backend python -m app.egress_check
```

That attempts real connections to real external hosts and exits non-zero if any succeed.
For the full gate, and for how to verify all of this independently, see
[`PRIVACY.md`](PRIVACY.md).

The models survive in the named volume `crowdsight_ollama_models`; the seal does not
remove them.

---

## Two assets that are easy to miss

Both are fetched at *runtime* by a dependency rather than installed by pip, and both are
baked into the image at build time so that never happens on a sealed machine.

### tiktoken BPE encodings

`TIKTOKEN_CACHE_DIR=/opt/tiktoken`

camel resolves an encoding the moment a `ChatAgent` is constructed. Sealed and missing,
that is a DNS failure and **agent construction dies before any model is contacted** — so
the error names tiktoken rather than anything you were doing.

### `Twitter/twhin-bert-base`

`HF_HOME=/opt/huggingface`

OASIS's Twitter platform hardcodes `recsys_type="twhin-bert"` and pulls this model the
first time it builds a feed. This is the dangerous one: sealed and missing, **the
recommender fails and every agent gets a degraded feed** — a silently worse simulation
rather than an error. Reddit uses no recommender model and is unaffected.

`HF_HUB_OFFLINE=1` is set so a cache miss fails immediately rather than spending ~90
seconds retrying against a DNS that cannot resolve.

**If you rebuild the image without a network, copy both directories forward** —
`/opt/tiktoken` and `/opt/huggingface` — or the rebuild will produce an image that starts
cleanly and simulates badly.

---

## Verifying provisioning worked

```bash
curl -s http://localhost:8080/api/health | jq
```

The `models` block lists what Ollama actually holds and names anything missing. This is
the check worth running before a long run: **reachable is not the same as usable**, and a
sealed stack whose model was never pulled looks perfectly healthy until the first
inference call fails — and it cannot fix itself, because pulling needs the internet the
seal removes.

```bash
docker compose exec ollama ollama list                     # the models themselves
docker compose exec backend ls /opt/tiktoken /opt/huggingface
```

---

## The GPU

Ollama takes the GPU by device reservation, so the NVIDIA Container Toolkit must be
installed or the `ollama` service will not start. That failure is deliberate: CPU-only
inference is impractical for multi-hundred-agent runs, and silently falling back to it
turns an overnight job into a multi-day one.

The toolkit is not in Ubuntu's own repositories; add NVIDIA's first.

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# verify
docker info --format '{{.Runtimes}}' | grep -q nvidia && echo "runtime registered"
docker run --rm --gpus all ubuntu:24.04 nvidia-smi --query-gpu=name --format=csv,noheader
```

**Kernel module and userspace driver must match.** A mismatch surfaces as a confusing
Docker mount error mentioning `/run/nvidia-persistenced/socket` rather than anything about
versions:

```bash
cat /proc/driver/nvidia/version                                # kernel module
nvidia-smi --query-gpu=driver_version --format=csv,noheader    # userspace
```

If they differ, reboot after the driver update.

See [the timings](../README.md#how-long-a-run-takes) for what to expect once it works.

## Updating a sealed machine

There is no in-place update path that does not need the network. To update:

1. Back up first — `./scripts/backup.sh`
2. Reconnect, `git pull`, `docker compose build`
3. Pull any new models through the provisioning overlay, as above
4. Disconnect and re-verify with `python -m app.egress_check`

Step 1 is not optional advice. A rebuild replaces the images, and the assets baked into
them go with the old ones.

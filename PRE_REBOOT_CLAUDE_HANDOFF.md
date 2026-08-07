# Handoff — written before a host reboot

**Written:** 2026-08-07 · **At commit:** `81253db` · **Position:** 40 of 53 steps

This exists so work can continue from here with nothing lost, whether that is the
same conversation resumed, a fresh agent, or a person. It records the things that
are *not* obvious from reading the code: why decisions were made the way they
were, which dependency behaviours had to be discovered by reading source, and
which mistakes cost time and would cost it again.

`REQ_SPEC.md` is the authoritative record of what is done — every completed step
carries a ✅ and an account of what was found while doing it. This file covers
what that one does not: operational state and working practice.

---

## 1. Why the reboot

The host has an **NVIDIA driver/library version mismatch**, so the `ollama`
container cannot start and no inference is possible.

```
$ nvidia-smi
Failed to initialize NVML: Driver/library version mismatch
NVML library version: 595.84

$ cat /proc/driver/nvidia/version
NVRM version: ... 595.71.05 ...
```

A driver package was updated (595.71.05 → 595.84) while the old kernel module
stayed loaded. The module cannot be reloaded because `gnome-shell`, `openrgb`,
`ptyxis` and `btop` all hold `/dev/nvidia0`, so **a reboot is the fix**.

The failure surfaces as a confusing Docker error, not as a GPU error:

```
failed to create shim task: OCI runtime create failed: ...
open /run/nvidia-persistenced/socket: no such file or directory
```

If that appears again, check `nvidia-smi` first — the socket is a symptom.

**After rebooting, confirm before doing anything else:**

```bash
nvidia-smi                                    # must report the RTX 5070 Ti
docker compose up -d
docker compose exec ollama nvidia-smi         # the container must see it too
```

---

## 2. Bringing the system back

```bash
cd /home/rweber/crowdsight
docker compose up -d          # containers were removed with `down`, so `restart:
                              # unless-stopped` has nothing to restart
```

Everything else persists across the reboot and needs no action:

| What | Where | Note |
|---|---|---|
| All code and history | git, pushed | clean tree, 0 unpushed commits |
| Model weights | `crowdsight_ollama_models` volume | no re-download of qwen2.5:14b |
| Knowledge graphs | `crowdsight_neo4j_data` volume | |
| Runs, profiles, reports | `data/` bind mount | includes cached sentiment scores |
| Backend image | `crowdsight-backend:dev` | twhin-bert + tiktoken baked in |

To resume this conversation with full context:

```bash
claude --resume f7fd4d3b-747b-446c-ae58-0df5ff79fd18
```

### Sanity check after restart

```bash
docker compose exec backend python -m pytest -q        # expect 1351 passed, 3 skipped
docker compose exec backend python -m pytest -m integration -q   # expect 62 passed (~3 min)
```

The three skips are `test_network_isolation.py` topology assertions, which inspect
the Docker daemon and only run from the host. They are *supposed* to run — see §6.

---

## 3. Exactly where work stopped

**Phase 8 Step 3 is complete and committed.** Next is **Phase 8 Step 4** (report
test units).

| Phase | | |
|---|---|---|
| 1–7 | complete | foundation → monitoring, data access, interviews |
| 8 | 3 of 4 | report agent, grounding, API done; **test units next** |
| 9 | 0 of 7 | Vue frontend — nothing built, the largest remaining phase |
| 10 | 0 of 5 | integration testing, egress verification, ops docs |

### One verification is outstanding

Step 3 was verified without inference because the GPU was already unavailable.
Everything not needing the model was checked against real data (11/11), but the
**live end-to-end generation path was not run**:

```bash
# after the reboot, with the stack up
curl -sX POST localhost:8080/api/report/generate \
     -H 'Content-Type: application/json' \
     -d '{"sim_id":"<a completed sim>"}'
# then poll /api/report/status/<task_id> to succeeded,
# and GET /api/report/<report_id>/export?format=html
```

Phase 8 Step 4 needs inference anyway, so this gets exercised there. Completed
runs already exist under `data/simulations/` — pick one whose `state` is
`complete` from `GET /api/simulation/list`.

### What Step 4 asks for

`tests/test_report_agent.py`, `test_report_grounding.py` and `test_report_api.py`
**already exist and pass** (written during Steps 1–3, 143 tests between them).
The genuinely missing file is `tests/test_report_sanitizer.py` — tool results
sanitised before entering the prompt, oversized results truncated rather than
blowing the context window. Some of that is already covered inside
`test_report_agent.py`; the pattern used in earlier phases was to move such tests
into the spec-named file rather than duplicate them, and to audit the existing
files against the spec's exact wording, which has twice found real gaps.

---

## 4. How this project has been worked

Established over 40 steps and worth continuing.

**The cadence.** One step at a time: read the spec's step, ask genuinely material
questions before building, implement, verify against real services, append ✅ and
an account of findings to that step in `REQ_SPEC.md`, update `README.md` if the
user-facing story changed, commit, push, tear the stack down.

**Commit messages** are prose explaining *why*, not a list of files. Every one
ends with:

```
Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FErSunUX57c16BxCbQRwYR
```

Use `git commit -F <file>` — **never `-m` with backticks**. An early commit had
backticks in the message, bash command-substituted them, and the message shipped
with the text executed and stripped. That cost a force-push to fix.

Git author is the noreply alias `17597068+CyberSecDef@users.noreply.github.com`,
set repo-locally.

**Read the dependency's source before writing against it.** This has repeatedly
been the difference between working code and plausible code — see §5, where
almost every entry was found this way rather than by testing.

**Verify against real services, not only mocks.** Mocked tests assert against
output *I* wrote, so they prove the code does what I assumed the model does. Every
step has ended with a scratchpad script driving real Ollama/Neo4j, and those runs
have found bugs no unit test could: the event-loop deadlock, the 1-of-11 sentiment
scoring, engine actions polluting the feed, agents signed up as NULL.

**Fix the code, not the test.** When a test fails, the first question is whether it
found something real. Several times it had.

**Never mark a step done on partial work.** If something could not be verified, say
so plainly in the commit and the spec, as was done for Step 3's live path above.

---

## 5. Hard-won knowledge about the dependencies

These cost real time to find. None are documented upstream.

### OASIS (camel-oasis 0.2.5)

- **`env.step()` gathers agent turns with no `return_exceptions`.** One agent
  raising aborts the whole round and kills a run that may be hours old.
  `harden_agent()` in `simulation_runner.py` wraps each turn so it cannot raise.
- **`perform_action_by_llm` catches exceptions *inside* its try, but
  `env.to_text_prompt()` on line 127 is outside it.** That is the failure the
  hardening actually catches.
- **`get_db_path()` falls back to a database inside the installed package** when
  `OASIS_DB_PATH` is unset, and `agent_environment.py:71` calls it on *every agent
  turn* to build that agent's feed. Without the env var, agents read a shared
  package-internal file rather than the run they are in — regardless of the
  `database_path` passed to `oasis.make()`.
- **`OasisEnv` defaults `semaphore=128`** — its own concurrency limiter. On one
  12 GB GPU that is the exhaustion the whole concurrency design exists to prevent.
- **`generate_twitter_agent_graph` never sets `user_name`**, so every population
  agent signs up as NULL and the run database cannot say who posted what. We
  backfill from `profiles.json` before `reset()`, which is where signup happens.
- **Both agent-graph generators are coroutines** despite reading as plain
  factories. `inspect.signature` hides it.
- **Twitter is CSV, Reddit is JSON.** The spec said otherwise; `pd.read_csv` on a
  JSON file raises inside agent generation, hours in.
- **`recsys_type` does not restrict actions.** It selects the recommender and the
  system-message wording only. Our per-platform action split is a realism choice
  we impose, not one OASIS enforces.
- **29 of 32 `ActionType` members are agent-invokable.** `EXIT`, `SIGNUP` and
  `UPDATE_REC_TABLE` have no tool. An unrecognised action is *warned about and
  silently dropped* — configure only typos and the agent gets zero tools and sits
  inert all run.
- **The trace table records `sign_up`** alongside real decisions, so a 300-agent
  run opens with 300 rows of registration noise. Filtered out of the action feed
  by default.
- **`perform_interview` deliberately bypasses `astep`**, reading memory and calling
  the model directly, so interviewing an agent does **not** change its later
  behaviour. Upstream's own comment says so. This is what makes interviews an
  instrument rather than a nudge.
- **Interviews are persisted by OASIS** as `interview` trace rows carrying prompt
  and response — interview history needed no new storage.
- **Reposts are rows with empty content** pointing at the original; quotes carry
  their own text. Sentiment scoring has to handle this or reposts vanish.
- **`trace` is keyed on `(user_id, created_at, action, info)`** — two identical
  rows collide. Test fixtures must vary them.
- **OASIS indexes nothing but its primary keys.** We add nine indexes on first read.

### camel / CAMEL-AI

- **`OllamaModel` starts its own server** when no `url` is given, shelling out to
  an `ollama` binary the image does not contain.
- **`astep` writes both sides of every turn to memory and OASIS never resets it**,
  so agent context grows for the whole run. Bounded by
  `SIMULATION_MEMORY_ROUNDS`; trimming must happen at user-message boundaries
  because an orphaned tool result is rejected by the completions API.
- **Constructing any `ChatAgent` resolves a tiktoken encoding**, downloaded on
  first use. Sealed, that is a DNS failure before any model is contacted.

### Sealed-network assets fetched at *runtime*

Two dependencies fetch things lazily that pip never installs. Both are baked into
the image at build time; **if you rebuild without a network, copy these forward**:

- `TIKTOKEN_CACHE_DIR=/opt/tiktoken` — four BPE encodings
- `HF_HOME=/opt/huggingface` — `Twitter/twhin-bert-base`, which OASIS's Twitter
  recommender pulls the first time it builds a feed. Sealed, that fails and every
  agent gets a **degraded feed rather than an error** — a silently worse
  simulation. Reddit uses no recommender model.

`HF_HUB_OFFLINE=1` is set so a cache miss fails immediately instead of spending
~90 seconds retrying against a DNS that cannot resolve.

### Python / pytest

- **`docker compose cp` overlays are lost** whenever a container is recreated.
  This silently ran pre-fix code for a whole step once. **Rebuild the image before
  any final verification**, do not rely on `cp`.
- **`spawn` re-imports `__main__`**, so a verification script that spawns must
  guard everything behind `if __name__ == "__main__":` or the whole script re-runs
  in each child.
- **A JSON body carries a real `0`**, so `int(payload.get("limit") or 50)` silently
  turns `limit: 0` into 50. Query strings are unaffected because `"0"` is truthy.
- **Zombie processes read as alive** in `/proc` until reaped. Check the state field.
- **pytest's `tmp_path` can exceed the 107-character Unix socket limit** on its
  own. Tests needing a control socket use a short `/tmp` directory.

---

## 6. Invariants that must not be broken

These are load-bearing. Several have their own tests specifically to stop a future
change quietly removing them.

1. **No cloud model, ever.** `build_model()` has no platform parameter, so "no code
   path can construct a cloud model" is a property of the signature. It also
   re-checks the URL through `classify_host`.
2. **The network seal.** `test_network_isolation.py` **never skips** — with the
   stack down the suite goes red rather than green. A test that passes by skipping
   itself when it cannot verify the seal is worse than no test.
3. **No invented statement attributed to a real person.** A quote survives only if
   it is found verbatim in the source document; otherwise it is demoted to the
   synthetic broadcaster and the reason recorded. Operator edits go through the
   *same* check — review must not be the way around it.
4. **Simulated content is never confused with document content.** Graph memory
   writes carry their own `Sim*` labels and never merge into `:Entity`.
5. **Every report claim cites the run, and citations are verified before the report
   is returned.** A claim citing something that does not exist is dropped and
   recorded; silently dropping it would be its own dishonesty.
6. **Everything rendered to HTML is escaped.** Reports carry agent-written text.
7. **The GPU budget is divided, not handed out whole.**
   `(LLM_CONCURRENCY - API_LLM_RESERVE) // MAX_CONCURRENT_SIMULATIONS`.
8. **A run's config freezes when it starts.** Editing a started run forks it.

---

## 7. Operational notes

**Stress testing** (opt-in, deliberately punishing — it overrides every guard
above):

```bash
scripts/stress.sh                    # 12 minutes
MINUTES=3 scripts/stress.sh          # short burst
```

Last full run pegged the GPU at 100% peak / 92.8% mean, 9.8 GB of 12.2 GB VRAM,
81 °C, and produced 9.49M chunks and 83k graph writes in three minutes. Gated
twice: a `stress` marker the default `addopts` deselects, **and**
`CROWDSIGHT_STRESS=1`.

**Test suite shape:**

- `pytest` → 1351 unit, no services, ~46 s
- `pytest -m integration` → 62, needs Neo4j + Ollama, ~3 min
- `pytest -m stress` → the load generator, additionally env-gated
- `tests/test_simulation_smoke.py` is the release gate

**Useful entry points:**

- `GET /api/simulation/list` — every run and its state
- `GET /api/simulation/budget` — where the inference budget went, when a run crawls
- `GET /api/simulation/<id>/run-status` — safe to poll, works on finished runs
- `POST /api/simulation/env-status` — alive / unresponsive / closed, ~2 s timeout

---

## 8. If starting completely fresh

Read in this order:

1. `REQ_SPEC.md` — the requirements *and* an account of every step completed,
   including the defects found and the corrections made to the spec itself. Where
   the spec was wrong, the correction and the evidence are recorded in place.
2. `README.md` — the user-facing story, configuration table, and the sealed-network
   design.
3. This file — operational state and the dependency knowledge above.
4. Module docstrings. Every service module opens with why it is shaped as it is,
   usually naming the failure that shaped it. Those are worth reading before
   changing anything in them.

The spec's step accounts are deliberately detailed because they are the only
record of *why* — the code shows what was built, not which three alternatives were
rejected and on what evidence.

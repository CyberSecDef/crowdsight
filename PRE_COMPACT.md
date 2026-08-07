# Pre-compaction notes — read with PRE_REBOOT_CLAUDE_HANDOFF.md

**Written:** 2026-08-07 · **At commit:** `a591e89` · **Position:** 40 of 53 steps

Written immediately before this session is compacted. `PRE_REBOOT_CLAUDE_HANDOFF.md`
covers operational state and hard-won dependency knowledge; **this file covers what
a compacted conversation loses that the code and the spec do not record** — chiefly
the decisions the operator made, so they are not silently re-litigated.

**Read both.** Neither duplicates the other.

---

## 1. The working relationship

The operator drives one step at a time with a consistent prompt:

> Execute phase N. Step M.
> Ask any questions you have.
> When complete, add a check.
> Then commit and push

That means, in order:

1. Read that step in `REQ_SPEC.md`.
2. **Investigate before asking** — read the dependency's source, check what already
   exists. Several questions turned out to be answerable that way, and the ones
   that remained were better for it.
3. Ask genuinely material questions via `AskUserQuestion` — the ones where different
   answers produce materially different work. Put the recommended option first and
   label it `(Recommended)`. Give real trade-offs, not strawmen. Two or three
   questions is typical; one is fine; none is fine if nothing is genuinely open.
4. Build it, following the answers.
5. **Verify against real services**, not only mocks (see §4).
6. Append ` ✅` to that step's `**Step N: …**` heading in `REQ_SPEC.md` — *on the
   heading line*, not buried in the body — and write an account beneath it of what
   was found, including defects and any corrections to the spec itself.
7. Update `README.md` if the user-facing story or the test counts changed.
8. Commit with `git commit -F <file>` and push. Tear the stack down when finished.

### Style that has worked

- The operator values **honesty about what was not verified**. Step 3 of Phase 8
  shipped saying plainly that its live path could not be run. That was the right
  call, not a failure.
- Findings are reported as findings: what broke, why, what it means. Not "all
  tests pass!"
- Commit messages are prose explaining *why*, not lists of files.
- The operator sometimes overrides the spec (Phase 6 Step 4 was built despite the
  spec advising against it at that point). When that happens, note the spec's
  caution in the write-up and proceed.
- **They may be on a phone.** Keep replies tight; lead with the outcome.

---

## 2. Decisions the operator made — do not re-litigate

These came from ~25 `AskUserQuestion` exchanges. They are load-bearing and mostly
**not recoverable from the code**. If a future step touches one, honour it or ask
again explicitly.

### Configuration and population

| Decision | Choice |
|---|---|
| Endpoint perimeter | Loopback preferred; private LAN allowed with a warning; public refused |
| Occupation coverage | Must span "all walks of life" incl. trades (mechanic, carpenter); `person` as fallback |
| NVIDIA toolkit | "Install the nvidia toolkit and anything else you need" |

### Phase 5 — simulation configuration

| Decision | Choice |
|---|---|
| Inactivity | **Both**: roll `activity_level` to skip agents pre-round (free), *and* keep `DO_NOTHING` |
| Off-platform action | Reject at validation |
| Platform action lists | Keep the spec's lists exactly; no additions |
| Operator edits | Re-verify against the source, same code path as generation |
| `sim_id` | Timestamped + short random (`sim-YYYYmmdd-HHMMSS-xxxxxx`) |
| Editing a started run | Config locked; edits fork into a new simulation |
| Over-length seed post | Warn and record, do not reject |
| Real-LLM config test | Yes, `integration`-marked |

### Phase 6 — execution engine

| Decision | Choice |
|---|---|
| Agent failure | Wrap each agent so its turn cannot raise; failure = did nothing |
| Broadcaster | Added to the graph separately and flagged non-population |
| IPC | Unix socket per run (survives an API restart) |
| Concurrency split | **Static**, by `MAX_CONCURRENT_SIMULATIONS`; never rebalanced live |
| Orphans on restart | Adopt if it answers; else kill (PID-verified) and mark failed |
| Agent memory | Bounded window (`SIMULATION_MEMORY_ROUNDS`, default 3) |
| Interrupted round | Roll back to the last checkpoint and re-run it |
| Graph memory feedback | **Close the loop** — write outcomes *and* feed them into prompts |
| Simulated vs document data | Separate `Sim*` labels, linked to real entities by `:ABOUT` |
| "Significant" outcome | Engagement-ranked, computed — no extra inference |
| Route naming | Spec's singular routes canonical; Phase 5's plural ones kept |
| Restarting a failed run | `POST /start` resumes from checkpoint and says `resumed: true` |
| Re-preparing | Reuse and fill gaps; `force=true` to rebuild |

### Phase 7 — monitoring and interviews

| Decision | Choice |
|---|---|
| `run-status` source | Disk first; live worker fields as enrichment, marked stale if absent |
| `agent-stats` | On demand, with indexes we add to the run database |
| `platform` filter | Validate it (400 on mismatch); add an `action` type filter instead |
| Paging | Offset-based with `order=newest\|oldest`, hard cap in the reader |
| Interview timing | Immediately, concurrent with the round in progress |
| Interview on a finished run | Refuse (409); serve history from the database |
| Bulk interviews | Async task, poll for results |
| `close-env` | Stop, then *verify* teardown; 207 if anything survived |
| `env-status` on a wedged worker | Short probe (~2 s), report `unresponsive` distinctly |
| Control-plane concurrency | Bound it now (8 in flight, 503 beyond) — do not defer to Phase 10 |

### Phase 8 — reports

| Decision | Choice |
|---|---|
| Sentiment | Score once with the model, cache per post in the run database |
| Agent evidence | Deterministic bundle first, then spend the tool budget on follow-up |
| Unresolvable citation | **Drop the claim**, record what was dropped and why |
| Prose | Scan free text for references and verify them too |
| Citations in export | Inline evidence line under each claim |
| Verification section | Always rendered, including on a clean report |

### Stress test

| Decision | Choice |
|---|---|
| Intensity | "Hard but survivable" — oversubscribe, stop short of deliberate OOM |

---

## 3. Exactly what is next

**Phase 8 Step 4 — report test units.** The spec names four files:

| File | Status |
|---|---|
| `tests/test_report_agent.py` | exists, 40 tests |
| `tests/test_report_grounding.py` | exists, 41 tests |
| `tests/test_report_api.py` | exists, 62 tests |
| `tests/test_report_sanitizer.py` | **missing** — the actual work |

The sanitizer requirement: *"tool results are sanitised before entering the prompt;
oversized results are truncated rather than blowing the context window."* Some of
this is already covered inside `test_report_agent.py` (`_sanitise`,
`MAX_TOOL_RESULT_CHARS`, the fence-defanging test).

**The established pattern for a test-units step**, which has twice found real bugs:

1. Move existing tests into the spec-named file rather than duplicating them
   (done for the smoke test in Phase 6 Step 6 and the IPC tests in Phase 7 Step 5).
2. **Audit the other named files against the spec's exact wording.** This found
   the missing `comments` round-attribution in Phase 6 Step 6, and the missing
   "documented shape" assertions in Phase 7 Step 5. Read the spec's sentence
   literally and check each clause is actually tested.

Then **Phase 9 — the frontend (7 steps)**. Nothing exists yet; it is the largest
remaining phase and the only one with no code. Vue 3, five-stage workflow. The
backend API it consumes is complete and its response shapes are pinned by
`test_monitoring_api.py`'s shape tests.

Then **Phase 10 (5 steps)** — full-pipeline integration test, egress verification
suite, ops docs.

---

## 4. How verification has been done

Every step ends with a script driving **real services**, because mocked tests only
prove the code does what *I* assumed the model does. These runs found the
event-loop deadlock, sentiment scoring 1 of 11 posts, engine actions polluting the
action feed, and agents signed up as NULL — none of which any unit test saw.

The pattern:

```bash
cat > <scratchpad>/verify_x.py <<'PYEOF'
...  # a series of check("label", condition, detail) calls, ending with
     # print(f"{sum(ok)}/{len(ok)} checks passed"); sys.exit(0 if all(ok) else 1)
PYEOF
docker compose cp <scratchpad>/verify_x.py backend:/app/verify_x.py
docker compose exec -T backend python /app/verify_x.py
# then: docker compose exec -T backend rm -f /app/verify_x.py
```

Write checks as **claims about behaviour**, not about implementation, and put the
interesting detail in the third argument so the output is readable evidence.

### Traps that have cost real time

- **`docker compose cp` overlays are lost when a container is recreated.**
  This silently ran pre-fix code for an entire step. **Rebuild the image
  (`docker compose build backend`) before any final verification.**
- **The scratchpad under `/tmp` does not survive a reboot** — and its path changes
  per session. A patch script staged there failed silently one commit ago. If a
  script must survive, put it in the repo or inline it.
- **Long verifications exceed the 600 s tool timeout.** Run them backgrounded
  writing to a file, then poll the file.
- **`spawn` re-imports `__main__`** — guard verification scripts with
  `if __name__ == "__main__":` or the whole script re-runs in every child.
- **Multiple `asyncio.run()` calls with one Neo4j driver** fail: the async driver
  binds to the loop that created it. Do all graph work in one loop.
- Suites: `pytest` (1351 unit, ~48 s) · `pytest -m integration` (62, ~2.5 min,
  needs Neo4j + Ollama) · `pytest -m stress` (additionally env-gated).

---

## 5. Current state

- **Commit `a591e89`**, clean tree, nothing unpushed, 50 commits.
- **1351 unit tests**, **62 integration** — all passing post-reboot.
- GPU healthy (595.84 throughout); both models in the volume; seal verified.
- 6 simulations on disk, 5 complete — reusable for report and monitoring work
  without generating a new run.
- 25 service modules, 39 test files, 32 config settings.

### The invariants

Listed in full in the handoff, §6. In short: no cloud model ever; the network
seal test never skips; no invented statement attributed to a real person;
simulated content never merges with document content; every report claim cites
the run and is verified before the report is returned; all HTML is escaped; the
GPU budget is divided not handed out whole; a started run's config freezes.

Each has tests specifically to stop a future change quietly removing it. If one
seems inconvenient, that is the moment to be most careful.

---

## 6. If something has clearly gone wrong after compaction

Recovery order:

1. `git log --oneline -15` — the commit messages are the decision record.
2. `REQ_SPEC.md` — every ✅ step carries an account of what was found, including
   defects and spec corrections. It is the authoritative state.
3. `PRE_REBOOT_CLAUDE_HANDOFF.md` §5 — the dependency behaviours, which are the
   single most expensive thing to rediscover.
4. This file §2 — the operator's decisions.
5. Module docstrings — every service opens by explaining the failure that shaped
   it.

Ask the operator rather than guessing on anything touching §2 or the invariants.
Everything else is recoverable from the repository.

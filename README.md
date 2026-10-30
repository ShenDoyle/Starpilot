# Starpilot 星舵

> Once a requirement is established, a web-based AI plans the full solution; a local agent retrieves the instructions, executes them step by step, reports every step truthfully, and closes out with a deliverable.

**Division of labor (hard boundary)**

| Role | Responsible for | Not responsible for |
|---|---|---|
| Web AI | Plan, steps, criteria, acceptance standards | Never touches local files, never sees real execution results |
| Local agent | Execution, evidence collection, truthful reporting | Never invents plan changes, never guesses outcomes, never embellishes results |
| Human | Requirement confirmation, authorization, acceptance | — |

> **Language / 语言**: this is the default (English) README. 中文版见 [README.zh-CN.md](README.zh-CN.md)。
> The deeper working documents (`SKILL.md`, `references/`, `docs/`) are maintained in Chinese —
> the 13-field lesson format in `references/lessons.md` is machine-parsed by
> `scripts/lessons_audit.py` and is deliberately not translated.

---

## 1. What is this

A **dual-engine** autonomous development system, **business-agnostic**:

```
① Requirement  → requirement card (three columns: clarified / unclear / conflicting)
② Web AI plans → project charter + contract + stage roadmap
③ Step breakdown → stages (each with exit criteria + acceptance method)
④ Execute instructions → code/artifacts + evidence (runtime/runs/<run_id>/)
⑤ Truthful report → six-part feedback (evidence / data / deviations / pending rulings)
⑥ Stage close-out → stage report + blocker list + ruling
                    ↑________ back to ①
```

**The real value is not the degree of automation — it is the division-of-labor discipline**: the web AI acts as architect (it cannot see your machine, so it must produce verifiable criteria), and the local agent acts as executor (it can actually run things, so it must return evidence-backed reports). Neither can replace the other: the AI must never guess execution results, and the agent must never change the plan on its own.

---

## 2. Repository layout

```
Starpilot/
├── SKILL.md                      The skill itself: six-ring model + six hard rules + INIT/BIND/LOOP/CLOSE
├── README.md                     This file (English, default)
├── README.zh-CN.md               Chinese README
│
├── docs/
│   ├── orchestrator-contract.md    ★ Orchestrator contract v1.5 (O0–O5 exit criteria + §2.4 metrics / §2.5 three-valued semantics / §2.6 conversation-ownership gate / §2.7 human-gate & exec mode / §2.8 platform occupancy lock / §2.9 grant scope & expire_time / §2.10 risk tiers / §2.11 mid-loop mode switch / §2.12 step receipts / §2.13 thinking-block stripping)
│   └── requirements/               Requirement cards (must pass the negative-form gate before planning)
│
├── references/                   Knowledge layer (read-only, consult as needed; in Chinese)
│   ├── protocol.md                   Loop protocol: state shape, feedback template, context-compression rules
│   ├── web-ai-platforms.md           ★ Platform tiers + degradation mechanisms + 6-step onboarding (page recon & signal calibration)
│   ├── dom-probes.md                 DOM decision snippets (copy-paste ready; no screenshots)
│   ├── process-charter.md            Human process charter: six stages + process red lines
│   └── lessons.md                    Field-tested pitfall log (cross-project; appendix is the auto-reflow zone)
│
├── scripts/
│   ├── platforms.py                 ★ Platform adapter layer: the single source of selectors & binding shapes
│   ├── wa.py                        Loop bridge CLI: bind/probe/send/wait/read/state/unlock/plock/stop (bind/send/clear take a cross-task platform occupancy lock)
│   ├── write_gate.py                 ★ Write gate: dry-run by default → approval → grant scope (single/count/stage/task + expire_time) → real write → verify
│   ├── risk.py                       Out-of-scope request risk tiers: low = allow & log, medium = switch to alternative, high = stop and ask
│   ├── orchestrator.py               ★ Orchestrator: 14-state machine + guards + budget + recovery + close-out reflow
│   ├── confirm.py                    ★ The only implementation of send/wait verdicts (anchor-based, shared by both layers)
│   ├── statelock.py                  Single-writer state lock (O_EXCL + dead-process takeover, stdlib only)
│   ├── requirement_gate.py           Negative-form requirement-card validation (rejects only, never generates)
│   ├── lessons_audit.py              Knowledge-base health check (format / duplicates / stale open / blocking)
│   └── upgrade.py                    ★ Upgrader: pull latest from GitHub, auto-fallback to public accelerators, back up, self-check
│
├── templates/                    Artifact templates (fill them in; do not improvise)
│   ├── requirement-card.md           ① Requirement card (incl. exec mode / grant scope / unattended policy)
│   ├── confirm-gate.md               ★ One-shot human confirmation sheet (plain language, ≤10 lines + 6 permission rows)
│   ├── feedback.md                   ⑤ Feedback + failure handoff + write-operation report
│   ├── phase-report.md               ⑥ Stage report
│   ├── done-check.md                 Pre-close four-way consistency check
│   └── web-ai-eval.md                ★ Web-AI memory self-test (used when choosing a platform)
│
├── tests/                            ★ 229 acceptance tests (stdlib unittest, zero third-party deps)
│   ├── test_orchestrator.py          State machine / budget / recovery / close-out
│   ├── test_orchestrator_gaps.py     Migration matrix actor gating / three-valued semantics / anchor wait / preconditions
│   ├── test_confirm.py               Send-verdict criteria (incl. two incident replays)
│   ├── test_fill_guard.py            Write-acceptance verdicts (blank class / send key / length) + streaming witness
│   ├── test_bind_guard.py            Conversation-ownership gate (conv_id / msg_count / conv_origin)
│   ├── test_statelock.py             State lock (incl. dead-process takeover, foreign locks preserved)
│   ├── test_platformlock.py          Cross-task platform occupancy lock + exec-mode & round-done pauses
│   ├── test_grant_risk.py            Grant scope / expire_time / out-of-scope risk tiers
│   ├── test_mode_switch.py           Mid-loop step<->auto switching
│   ├── test_read_noise.py            Thinking-block / suggested-question stripping on read
│   ├── test_platforms_replay.py      Live DOM snapshot replay (a broken selector turns red offline)
│   ├── test_lessons_audit.py         Knowledge-base health check (incl. blocking / staleness)
│   ├── test_upgrade.py               Upgrader: version parsing / mirror fallback / install / prune
│   ├── domsel.py                     Mini selector engine (for replay, pure html.parser)
│   └── fixtures/{deepseek,kimi,gemini}/  Live-recorded DOM snapshots
│
├── runs/<run_id>/                Orchestrator run artifacts: state.json / journal.jsonl / plan.md
│                                 + writes/ (dry·approval·result) + evidence/ + replies/   [gitignored]
├── state/                        current (run pointer), current_goal.md, contract.md            [gitignored]
├── stage/<id>/handoff.md         Stage handoffs (re-read at every step; never assume the platform remembers) [gitignored]
│
└── archive/
    └── loop-scaffolding/         Early loop scaffolding (reference implementation, not the main path)
```

---

## 3. How to use

### Get it

```bash
git clone https://github.com/ShenDoyle/Starpilot.git
cd Starpilot
python -m unittest discover -s tests -p "test_*.py" -t tests   # 229 tests, stdlib only
```

Repo: **<https://github.com/ShenDoyle/Starpilot>**

### Upgrade (just say "update Starpilot to the latest")

```bash
python scripts/upgrade.py --check        # local vX → remote vY (exit 4 when newer)
python scripts/upgrade.py --to-latest    # pick source → download → back up → install → run tests
```

- **Automatic fallback behind a bad network**: `raw.githubusercontent.com` and `codeload.github.com` often time
  out from mainland China. The script probes direct access and a set of public accelerators in parallel
  (`ghfast.top`, `gh-proxy.com`, `ghproxy.net`, `hub.gitmirror.com`, `ghproxy.cc`), prefers direct when it works,
  otherwise takes the fastest live mirror. Pin one with `--mirror <prefix>`, or force direct with `--mirror direct`.
- **Never touches your data**: `runtime/` `runs/` `state/` `stage/` and `.wa-state.json` are excluded; the old
  tree is copied to `<dir>.bak-<timestamp>` first, and a failed self-check prints the rollback command.
- **Self-check built in**: the full suite (229 tests) runs after install, so a broken upgrade goes red immediately.
- In a `.git` checkout the script refuses to overwrite (use `git pull --ff-only`); `--force` overrides.

> **Prerequisite**: install the **`bsk`** browser-driving CLI first — the loop is currently built and live-tested against it (see §6; a `BSK_BIN` override exists for equivalent CLIs).

### Use it two ways

**A. As an agent skill** — Starpilot follows the de-facto `SKILL.md` convention (a folder whose front-matter declares `name` / `description`). If your AI agent harness loads skills this way, copy or symlink this folder into that harness's skills directory. The agent activates on the `description`'s trigger phrases — English: **starpilot, web-ai-loop, web AI loop, AI loop development, outer-brain development, follow web AI instructions, plan with web AI**; Chinese triggers (网页版AI、AI循环开发、星舵…) are kept for habit compatibility.

**B. Standalone CLIs** — everything under `scripts/` is plain stdlib Python 3.9+. Skip the skill layer entirely and drive the loop yourself with the commands below; `SKILL.md` then serves as the operating manual.

> Starpilot is **host-agnostic**: it does not depend on any particular agent product, OS, or machine. The only external dependency is a local browser-driving CLI (see §6).

### Run one loop manually

```bash
cd <project workspace>

# 0. Fill the requirement card → get user confirmation → proceed to planning only after explicit approval
#    templates/requirement-card.md

# 1. Bind a conversation (bind once; URL shape varies by platform)
python scripts/wa.py bind "https://chatgpt.com/c/<conversation-id>" --platform chatgpt

# 1'. Or start a brand-new conversation (first round only; never re-open mid-loop)
python scripts/wa.py bind --new --platform kimi

# 2. Read the web AI's reply, save verbatim
python scripts/wa.py read --out runtime/wa/turn-1.md

# 3. Do what it said (nothing more), leave evidence

# 4. Fill the feedback template → send it
#    templates/feedback.md
python scripts/wa.py send --platform chatgpt --file runtime/wa/feedback-1.md

# 5. Wait for the reply to finish (triple DOM verdict, no screenshots)
python scripts/wa.py wait --platform chatgpt --timeout 900

# 6. Close out: stage report + done-check + reflow pitfalls into lessons
```

### Run one loop via the orchestrator (recommended: gated writes, crash-recoverable)

```bash
python scripts/orchestrator.py init --goal "<goal for this run>" --stage O1 --card docs/requirements/x.md
                                                       # with --card: the requirement gate runs first; no run on failure
python scripts/orchestrator.py bind --reuse            # reuse the bound conversation; no new URL
python scripts/orchestrator.py plan --file runtime/wa/turn-1.md   # planning artifact lands on disk
python scripts/orchestrator.py windows                 # hard rule 6 assertion: exactly one session per browser instance

# Real-write, four steps: dry-run → approve → single real send → post-write verify (dry_run by default; no write without approval)
python scripts/orchestrator.py loop --once --file runtime/wa/feedback-1.md
python scripts/orchestrator.py approve --approval <id> --ttl 900
python scripts/orchestrator.py loop --once             # APPROVED → SENDING → AWAITING_REPLY
#   Pass criterion: effective_delta == 1 (anchor-based); verify runs automatically after the reply lands

# Crash recovery: DOM re-verification only, never re-sends; insufficient evidence → PAUSED_HUMAN
python scripts/orchestrator.py loop --resume
python scripts/orchestrator.py unblock --to AWAITING_REPLY --reason "<why it is safe to continue>"
#   Note: this repair edge requires last_send.send_id to exist and verdict ∈ INCONCLUSIVE*

# Audit & close-out: four zeros + causal chain + mandatory lesson reflow
python scripts/orchestrator.py audit --run <run_id> --trace
python scripts/orchestrator.py close --stage O1 --require-lessons
python scripts/lessons_audit.py --strict      # knowledge-base health check (blocking open items block close-out)
```

**Metric definition**: the pass line is **`effective_delta == 1`** (anchor precedence: message ID > body fingerprint > count); `reply_count_delta` is audit-only. Definitions are frozen in `docs/orchestrator-contract.md` §2.4 / §2.4.1, and the three-valued `fail_streak` semantics live in §2.5 — **any code change must update those sections in the same commit**.

### The six hard rules (violating any = failure)

1. **One conversation** — the project uses exactly one web-AI conversation URL for its entire life; never open another.
2. **No guessing intent** — unclear requirements produce questions, not solutions; **raw requirements pass through unmodified**: the user's original wording reaches the web AI verbatim, anything the agent adds must be factual and explicitly labeled, and clarifying questions come from the **web AI** (the agent only relays).
3. **No screenshots for judgment** — state is always determined from DOM / element attributes.
4. **Stop after two failures** — two consecutive failures on the same goal means package-and-hand-off; no third workaround attempt.
5. **Report truthfully** — failure is reported as failure; every number must have a source.
6. **Single-window discipline** — exactly one session / agent window per browser instance.

---

## 4. Platform support

`scripts/platforms.py` is the **single source** of platform differences; the main flow `wa.py` contains no platform-specific selectors.

> **Recommended, in order: Kimi → ChatGPT → DeepSeek.**
> Kimi leads on single-conversation long-memory coherence — the one hard selection criterion in this system (one conversation for the whole project life). ChatGPT has the most precise selectors and the richest completion signals. DeepSeek is the strongest domestic option (its virtual list demands ID-based deltas, which the tooling already handles). Gemini and Doubao are live-tested and usable, but sit outside the recommended set; Claude is untested.

| key | Platform | Status | Notes |
|---|---|---|---|
| `kimi` | Kimi | **Live-tested** · recommended | Top tier for long-context coherence; write path live (2026-10-29; Lexical editor, send control is a `div`, not a `button`) |
| `chatgpt` | ChatGPT | **Live-tested** · recommended | Precise selectors, richest signals; the safest all-rounder |
| `deepseek` | DeepSeek | **Live-tested** · recommended | Full chain 2026-10-29; editor is a `textarea` (read `value`, not `innerText`), send button uses BEM `--disabled`, messages live in a **virtual list** (deltas must use message IDs) |
| `gemini` | Gemini | **Live-tested** | Full chain 2026-10-29; editor is **Quill** (`ql-editor`) — **exec writes silently fail** (text enters DOM, not model), must use `native` real keystrokes; send button is conditionally rendered (absent on empty input); `div.conversation-container`'s hex `id` is the anchor |
| `doubao` | Doubao (豆包) | **Live-tested** | Full chain 2026-10-29; editor is **Tiptap ProseMirror** — `bsk fill` keystroke simulation fails, `exec` works; **the send button must not be clicked** (no reaction and it steals focus); the only working path is exec write + keep focus + Enter (`send_method=enter`); "suggested questions" render inside the reply node and are stripped on read (`read_ignore`); completion signal = `message_action_regenerate` appearing (icon-only button, needs `regen_selector`) |
| `claude` | Claude | Not tested | Pairs well with Claude Projects (persistent knowledge against context compression) |

**Discipline reminder**: hard rule 1 ("one conversation for the whole life") means **a platform cannot be probed without binding it** — in the matrix, `skipped_not_bound` means never probed (not a failure) and `deferred_rule1` means shelved by discipline (not a technical failure). Live-testing another platform requires a **human rebinding decision** — that is an architect-level call.

**Candidate selectors come from generic-pattern inference; first use must probe live and backfill**:

```bash
python scripts/wa.py platforms                          # list & status
python scripts/wa.py probe --platform claude            # probe selectors live
python scripts/wa.py bind "<conversation-url>" --platform claude
```

Live-tested: **ChatGPT** (richest signals), **Kimi** (2026-10-29; Lexical editor, `div` send control, `div.chat-content-item-*` message items),
**DeepSeek** (2026-10-29; `textarea` editor, `div.ds-button--primary.ds-button--circle` send key, `div.ds-virtual-list` virtual list),
**Gemini** (2026-10-29; Quill `ql-editor`, conditionally rendered `button[aria-label="Send"]`, `div.conversation-container` turn containers), and
**Doubao** (2026-10-29; Tiptap ProseMirror editor, exec write + Enter send — button clicks don't work, canonical `data-testid` message system, suggested-question noise stripped on read).

Platform-specific pitfalls (Kimi rate-limit modal / DeepSeek virtual-list count distortion / Gemini silent exec failure / Doubao dead send button) are logged in `references/lessons.md`.

The 6-step onboarding flow (recon → definition → probe → signal calibration → live run), selection criteria, and the three "fragmentation" mechanisms → `references/web-ai-platforms.md` (in Chinese).
Before switching platforms, run `templates/web-ai-eval.md` first (20 minutes; self-testing beats benchmarks).

---

## 5. Current status (2026-10-29, O5 closed)

> Completion is **self-assessed as of 2026-10-29** (self-assessment is not live testing: the percentages for ⑤⑥ are estimated from "completed evidence vs contract exit criteria"; the basis for each row is listed and re-checkable).

| Ring | Completion | Basis |
|---|---|---|
| ① Requirement | 75% | Methodology + template + **tool-enforced gate** (`requirement_gate.py`: un-clarified items, un-adjudicated conflicts, or missing approval → rejected; `init --card` enforces it) |
| ② Web AI planning | 90% | 29 planning rounds archived; charter + contract + stage reports produced; O0–O4 all adjudicated |
| ③ Step breakdown | 90% | Stage roadmap + per-stage exit criteria and rulings (O4 close-out criteria frozen into the contract) |
| ④ Execute instructions | 88% | `wa.py` + `platforms.py` + `write_gate.py` + `orchestrator.py` in place; 6 platforms routable, **ChatGPT + Kimi + DeepSeek + Gemini + Doubao live-tested** |
| ⑤ Step-by-step execution | 95% | Write gate live-tested (dry-run→approve→single real send→verify, `effective_delta=1` with `delta_source=anchor`); wait side moved to anchor criteria; crash recovery live-tested with zero re-sends; **229 acceptance tests green** |
| ⑥ Finish the project | 60% | Knowledge reflow enforced **with health checks** (`lessons_audit.py`: blocking open items block close-out; open items older than 14 days warn); remaining gaps: "multi-platform live tests (needs human rebinding)" and "long-running soak" |

### Three gaps that have been closed (2026-10-29)

| Former gap | How it works now | Evidence |
|---|---|---|
| **No orchestrator** | `scripts/orchestrator.py`: 14-state machine + guarded transitions + round budget + crash recovery (DOM re-verification only, never re-sends) | `runs/20261029T042822-recovery2/evidence/recovery-003.json` (local run artifact) |
| **Write side not implemented** | `scripts/write_gate.py`: dry_run by default + mandatory approval + single real write + post-write verify (`reply_count_delta == 1`) | `runs/20261029T043211-turn4/writes/*.result.json` (local run artifact) |
| **Knowledge reflow was voluntary** | `close --stage <id> --require-lessons` forcibly merges lesson candidates; acceptance checks `candidates=merged=appendix_added` and `lessons_pending=0`; `lessons_audit.py` adds anti-rot health checks | `references/lessons.md` appendix (21 entries; lesson_ids grep-able; health check `verdict: PASS`) |

### Remaining gaps

| Gap | Symptom | Fix |
|---|---|---|
| **Multi-platform live tests** | Claude read-only probing **never done** — matrix records `skipped_not_bound`; hard rule 1 forbids probing without binding | A **human rebinding** (architect-level decision), then onboard via `references/web-ai-platforms.md` six steps |
| **ChatGPT deferred by discipline** | Live-tested but not bound in the current run; matrix records `deferred_rule1` (discipline-deferred, not a technical failure), kept as a `status=open` lesson | Unlocked only by an explicit human rebinding |
| **No long soak** | The orchestrator has only run single/double rounds, never a 20-round budget-level automatic loop | Candidate topic for a future round |
| ~~**No requirement-stage tooling**~~ | **Closed (2026-10-29)**: `requirement_gate.py` negative-form validation + `init --card` enforcement | — |
| **Offline replay doesn't cover render timing** | `tests/fixtures/` verifies selector hits, not real-page render timing; offline green can still diverge live | Next round: live vs replay deviation comparison |

The full gap list lives in `SKILL.md` §8 (in Chinese).

---

## 6. External dependencies

| Item | Notes |
|---|---|
| Browser-driving CLI | **The loop is currently built and live-tested against `bsk` — install it first.** `wa.py` looks it up on `PATH` and `~/.local/bin`, or honors a `BSK_BIN` override. Any local CLI exposing the same commands (`session` / `navigate` / `evaluate` / `click` / `press` / `fill`) can be wired in via `BSK_BIN`; all DOM decisions go through `evaluate` (JS returns facts), never screenshots |
| Python 3.9+ | Stdlib only, zero third-party dependencies |
| Web AI | One logged-in conversation URL + one browser instance |

---

## 7. Maintenance conventions

- Before adding to `references/lessons.md`, ask: is this a generic pitfall or a project-specific one? Only generic ones go in.
- Changing a hard rule in `SKILL.md` requires a revision record (who changed it and why).
- Changing a selector in `scripts/platforms.py` must update the `verified` flag and the `notes` live-test date together.
- **Changing metric definitions** (`reply_count` / `msg_count` / `resend_attempts` / pass line) must be synchronized with `docs/orchestrator-contract.md` §2.4 — metric definitions must never live only in code.
- Credentials never enter the repo, the conversation, or the logs. Starpilot's own discipline applies to itself.

---

## License

[MIT](LICENSE) © 2026 TOPcbm

<h1 align="center">Starpilot 星舵</h1>

<p align="center"><b>Let the web AI do the thinking, let the local agent do the work.</b></p>

<p align="center">
<sub>Code, documents, spreadsheets, ops runbooks, research — anything that needs both a plan and a pair of hands</sub>
</p>

<p align="center">
  <a href="https://github.com/Tencent/BrowserSkill"><img alt="Core dependency: bsk" src="https://img.shields.io/badge/Core%20dependency-bsk-2f75f0"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.9%2B-2f75f0">
  <img alt="Version" src="https://img.shields.io/badge/version-0.1.1%20preview-6b7280">
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/github/license/ShenDoyle/Starpilot?color=2f75f0"></a>
  <a href="https://github.com/ShenDoyle/Starpilot/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/ShenDoyle/Starpilot?style=social"></a>
</p>

<p align="center">
  <a href="#what-it-does"><b>What it does</b></a> ·
  <a href="#use-the-agent-you-already-have">Agent</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#try-one-task">Try one task</a> ·
  <a href="#which-web-ai-to-use">Web AI</a> ·
  <a href="#commands">Commands</a> ·
  <a href="#docs">Docs</a>
</p>

<p align="center"><a href="README.zh-CN.md">简体中文</a> · <b>English</b></p>

## What it does

| What you want | How Starpilot helps |
| --- | --- |
| **Land a large change** | The web AI cuts the goal into stages with acceptance criteria; the agent does one step at a time and reports back before asking for the next |
| **Restructure long documents, merge many sources** | Structure first, then rewrite section by section, each with its own criteria; any drift stops the round and triggers a re-plan |
| **Clean data, reconcile sheets** | Every number carries a source; mismatches are reported as differences |
| **Rule-based batch checks and reports** | The web AI writes the rules, the agent executes and keeps evidence |
| **Compare options, pick a stack** | The web AI sets the criteria, the agent gathers evidence, every conclusion traces back to a source |

Compared with the usual approaches, the difference is the division of labour:

|  | Chat window only | Local agent only | Starpilot |
| --- | --- | --- | --- |
| **Who plans** | You | The agent guesses | Web AI: plan, steps, acceptance criteria |
| **Who executes** | You | The agent | Local agent: execute, collect evidence, report truthfully |
| **How drift surfaces** | You notice it | Often missed | Every round reports deviations; failures are never dressed up |
| **How state survives** | Scroll back through chat | Nothing recorded | journal + stage handoff, resumable at any point |

## Use the agent you already have

<p align="center">
<table><tr>
<td align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/agents/codex-dark.svg"><img alt="Codex" src="assets/agents/codex.svg" width="36"></picture><br><b>Codex</b></td>
<td align="center"><img alt="Claude Code" src="assets/agents/claude.svg" width="36"><br><b>Claude Code</b></td>
<td align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/agents/cursor-dark.svg"><img alt="Cursor" src="assets/agents/cursor.svg" width="36"></picture><br><b>Cursor</b></td>
<td align="center"><img alt="OpenClaw" src="assets/agents/openclaw-avatar.png" width="36"><br><b>OpenClaw</b></td>
</tr><tr>
<td align="center"><img alt="CodeBuddy" src="assets/agents/codebuddy.svg" width="36"><br><b>CodeBuddy</b></td>
<td align="center"><img alt="WorkBuddy" src="assets/agents/workbuddy.svg" width="36"><br><b>WorkBuddy</b></td>
<td align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/agents/pi-dark.svg"><img alt="Pi" src="assets/agents/pi.svg" width="36"></picture><br><b>Pi</b></td>
<td align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/agents/shell-dark.svg"><img alt="Any agent that can run commands" src="assets/agents/shell.svg" width="36"></picture><br><b>Any agent</b></td>
</tr></table>
</p>

These, and any other agent that can run shell commands, install it directly. The agent and the model are your call.

## Quick start

One agent from the list above; it installs the rest itself. The one manual step left is flipping the browser extension on.

1. Put this whole folder into your AI client's skills directory (for example `~/.workbuddy/skills/starpilot`), or `git clone` it there.
2. Say one sentence (see [Try one task](#try-one-task)).

Or hand the whole install to the agent — copy this verbatim:

```text
Install Starpilot from https://github.com/ShenDoyle/Starpilot. Install whatever is missing yourself,
then run python scripts/env.py to confirm the environment and tell me what is still needed.
```

<details>
<summary><b>🔧 Manual install</b></summary>

**bsk** (the core dependency — the browser CLI from [Tencent/BrowserSkill](https://github.com/Tencent/BrowserSkill), installed to `~/.local/bin` by default):

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/Tencent/BrowserSkill/main/install.sh | sh
```

On Windows, run this in PowerShell: `irm https://raw.githubusercontent.com/Tencent/BrowserSkill/main/install.ps1 | iex`

Then enable the browser extension ([Chrome](https://chromewebstore.google.com/detail/hhcmgoofomhgciiibhipgmgkgnoenaoi) / [Edge](https://microsoftedge.microsoft.com/addons/detail/browserskill/emacgiaaaiojkkpkddmmdfhmokgmnikg)); `bsk doctor` must show Connected. If it landed somewhere non-default, point `BSK_BIN` at it.

Python 3.9+, no third-party packages; run `python scripts/env.py` afterwards to confirm.

</details>

## Try one task

Once installed, no commands to memorise:

> 💬 Use Starpilot to do X ／ Have the web AI plan X ／ Follow DeepSeek on this project

- Requirements get pinned down before work starts: what is clear, what is open, what conflicts. Nothing runs until you agree.
- Every round reports three lines: what was done, where the evidence is, what fell short.

## Watching, or not watching

| The pace you want | What to say before starting |
| --- | --- |
| 👀 **Step by step** | "Show me every step first" — it stops after each round until you nod |
| 🚀 **Straight through** | "Don't ask me at every step" — it runs on, and only comes back for things only you can do, like logging in or scanning a code |

Writes need authorisation first: "you may write for the whole task", "only this stage", or say nothing — then every write is asked one by one. Change your mind mid-way and it takes effect next round.

## What it will not do

- **No guessing at requirements** — with you watching it asks first; unattended it follows the pre-agreed policy and records the rationale
- **No dressing up results** — a failure is reported as a failure, never as "should be fine now"
- **No widening its own authority** — harmless requests pass with a log line, risky ones switch to an alternative, anything touching credentials, deletion, or cross-site stops
- **No touching files behind your back** — writes dry-run by default; either approve them one by one, or authorise a stage up front
- **No fighting over one conversation** — one conversation and one window per project, for its whole life; parallel tasks queue up
- **No judging pages from screenshots** — state comes from the DOM, so "it looked like it worked" cannot happen

These six are fixed in `SKILL.md` §1, with no room to interpret them. Two failures do not stop and wait for you — they go back with the failure in hand for a new plan.

## Which web AI to use

Any other chat UI can be wired in too — one `probe` run before its first write.

| key | Platform | Status | Best for |
| --- | --- | --- | --- |
| `deepseek` | DeepSeek | Live-tested · **first choice in China** | fast direct connection domestically, steady planning |
| `chatgpt` | ChatGPT | Live-tested · **first choice elsewhere** | best plans and acceptance criteria |
| `kimi` | Kimi | Live-tested | strongest long-conversation coherence |
| `gemini` | Gemini | Live-tested | big window, long documents |
| `doubao` | Doubao | Live-tested | direct in China, cheapest to set up |
| `claude` | Claude | Not calibrated | one calibration run and it works |

Calibration and platform details: `references/web-ai-platforms.md`.

## Commands

Day to day the loop is driven by `scripts/orchestrator.py`: a 14-state machine with a write gate (dry-run → approval → real send → post-write check); after a crash it only re-verifies, never re-sends. You can also use it without the skill — `scripts/` is plain standard-library Python, and `SKILL.md` doubles as the manual.

**Environment** (the agent runs this before starting)

```bash
python scripts/env.py                 # Python version, bsk path, extension status
python scripts/env.py --install       # install bsk when missing
```

**Bridging the web AI** (`wa.py`)

```bash
python scripts/wa.py platforms                          # list platforms and calibration status
python scripts/wa.py bind "https://chatgpt.com/c/<id>" --platform chatgpt
python scripts/wa.py bind --new --platform kimi         # start a new conversation (first round only)
python scripts/wa.py read --out runtime/wa/turn-1.md    # read the reply, save it verbatim
python scripts/wa.py send --platform kimi --file runtime/wa/feedback-1.md
python scripts/wa.py wait --platform kimi --timeout 900 # DOM-based verdict, never screenshots
python scripts/wa.py probe --platform claude            # calibrate before binding a new platform
```

**Driving a round** (`orchestrator.py`, recommended)

```bash
python scripts/orchestrator.py init --goal "<goal>" --stage O1 --card docs/requirements/x.md
python scripts/orchestrator.py bind --reuse              # reuse the already-bound conversation
python scripts/orchestrator.py plan --file runtime/wa/turn-1.md
python scripts/orchestrator.py loop --once --file runtime/wa/feedback-1.md   # dry-run by default
python scripts/orchestrator.py approve --approval <id> --ttl 900
python scripts/orchestrator.py loop --once               # real send only after approval
python scripts/orchestrator.py loop --resume             # crash recovery: re-verify, never re-send
python scripts/orchestrator.py replan --reason "stuck on the same button twice"   # two failures: get a new plan
python scripts/orchestrator.py rebind --reason drift                              # conversation taken over: open a fresh one
python scripts/orchestrator.py decide --question "..." --choice "..." --why "web AI ruled: ..."  # record an autonomous call
python scripts/orchestrator.py audit --run <run_id> --trace
python scripts/orchestrator.py close --stage O1 --require-lessons
```

- Pass line: `effective_delta == 1` (anchor order: message ID > body fingerprint > count), frozen in `docs/orchestrator-contract.md` §2.4.
- Trouble does not come back to you: two failures → `replan`; conversation hijacked or deleted → `rebind`; new ambiguity → the web AI decides against the project's own requirements, then `decide` records it (§2.14).
- Too many stops: `orchestrator.py mode --set auto --reason "<your words>"`, effective next round.

**Helpers**

| Command | Purpose |
| --- | --- |
| `write_gate.py` | Write gate: grant scope (single / count / stage / task + expiry) |
| `risk.py` | Out-of-scope grading: low passes with a log, medium switches approach, high stops and asks |
| `requirement_gate.py` | Negative-form requirement-card check (rejects only, never generates) |
| `lessons_audit.py --strict` | Knowledge-base health check: format, duplicates, stale unresolved items |
| `statelock.py` | Single-writer lock on the state file |
| `upgrade.py` | Pull the latest from GitHub: picks a source, backs up, self-tests |

## Dependencies, self-test, upgrade

| Item | Notes |
| --- | --- |
| 🌐 [bsk](https://github.com/Tencent/BrowserSkill) | Core dependency (MIT): CLI + Chrome/Edge extension, driving the browser you are already logged into. `wa.py` looks it up via `BSK_BIN`, then `PATH`, then `~/.local/bin` |
| 🐍 Python 3.9+ | Standard library only, no third-party dependencies |
| 🤖 Web AI | One logged-in conversation URL + one browser instance |

```bash
python -m unittest discover -s tests -p "test_*.py" -t tests   # 294 tests, stdlib only
python scripts/upgrade.py --check        # is there a newer version
python scripts/upgrade.py --to-latest    # upgrade: pick a source, back up, run the tests
```

Or just say **"update Starpilot to the latest"**. Direct GitHub access often times out from mainland China, so the upgrader probes direct access and a set of public accelerators in parallel and takes whichever is fastest; `runtime/ runs/ state/ stage/` are left alone, the tree is backed up before being replaced, and the test suite runs afterwards.

No skill, CLI only? Take the `starpilot-cli` package: `scripts/` + `references/` + `templates/` + contract + tests, no `SKILL.md` — stdlib only, ready for cron or another agent to drive.

<details>
<summary><b>bsk connects then drops? (hosts that reap child processes)</b></summary>

Hosts such as WorkBuddy and CodeBuddy reap command subprocesses along with the command, so `bsk doctor` may pass and the connection still dies mid-run. Keep the daemon alive as an independent background task: `bsk daemon start --foreground`, then verify the connection from a separate tool call. For shared `BSK_HOME` and `BSK_AUTO_START=0`, see the [sandboxed-agent guide](https://github.com/Tencent/BrowserSkill/blob/main/docs/sandboxed-agents.md).

</details>

<details>
<summary><b>📂 What it leaves behind</b></summary>

```
<workspace>/.wa-state.json   bound URL, platform, round, send hash, failure count
<workspace>/runtime/wa/      turn-<n>.md (AI replies verbatim), feedback-<n>.md (our side)
<workspace>/runs/<run_id>/   state.json + journal.jsonl (the truth) + writes/ + evidence/ + replies/
<workspace>/state/           current pointer, current_goal.md, contract.md
<workspace>/stage/<id>/      handoff.md (stage handoff, re-read every step)
```

When something goes wrong, look here first: `journal.jsonl` is the single source of truth, and `writes/` records the dry-run, approval and result of every write.

</details>

## Docs

| What you want to change | Read |
| --- | --- |
| Flow, hard rules, agent behaviour | `SKILL.md` |
| State machine, metric definitions, confirmation gate and grants | `docs/orchestrator-contract.md` |
| Onboard a new platform, or a platform starts misbehaving | `references/web-ai-platforms.md` |
| Keep the agent away from pitfalls others already hit | `references/lessons.md` |
| Requirement card, confirmation sheet, feedback, stage report | `templates/` |
| What shipped in this version | `CHANGELOG.md` |

> The working documents (`SKILL.md`, `references/`, `docs/`) are in Chinese.

## License

[MIT](LICENSE) © 2026 TOPcbm

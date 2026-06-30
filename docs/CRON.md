# Cron Schedule — premium_extractor

This document is the **canonical reference** for the host crontab entries that
manage the `premium_extractor` lifecycle. The crontab itself lives on the host
(`crontab -e`) and is **not** checked into this repo. When this doc changes,
update the host crontab to match.

Last updated: 2026-06-29 (TASK-2026-289 — single-instruction design)

---

## Lifecycle pattern (single-instruction)

The extractor lifecycle is managed by **one** cron instruction: the watchdog.
It is responsible for both **cold-start** (extractor down at market open) and
**post-crash recovery** (extractor dies mid-session). No separate START line.

`premium_extractor` and `gex_extractor` have always used a single
`*/5 * * * *` watchdog because there is no need for two instructions when one
is sufficient. The watchdog itself is **shared between both extractors**
(`/Users/ubexbot/.openclaw/scripts/extractor-watchdog.sh`); it walks the
extractor root directories, sees which one is `MARKET_STATUS=OPEN && !is_alive`,
and starts them independently.

`ibkr_trader_engine` was migrated to the same single-instruction design on
2026-06-29 (see PR #20). `tradingView_signal_generator` was migrated the same
day (see PR #65). This PR applies the pattern here for completeness, so the
three sibling repos (`ibkr_trader_engine`, `tradingView_signal_generator`,
`premium_extractor`) all document the same design.

Helper scripts (`run-extractor.sh`, `stop-extractor.sh`, `extractor-watchdog.sh`)
live in `/Users/ubexbot/.openclaw/scripts/` (host-managed, out of repo scope).
The wrapper signature is `run-extractor.sh <extractor-root> <python-bin> <log-name>`.
Both extractor wrappers are **idempotent**: if a previous instance is still
running (`run.pid` points to a live PID), they log and exit 0 without forking
a duplicate.

---

## Current crontab (host)

```cron
# -- premium_extractor / gex_extractor --------------------------------------
# SPX premium + GEX data extractors -- single-instruction lifecycle
# (watchdog-only). The shared extractor-watchdog.sh fires every 5 minutes
# during the trading day and is responsible for BOTH cold-start
# (extractor down at market open) AND post-crash recovery. No separate
# START line. See docs/CRON.md for the design rationale.
# .env (SUPABASE_URL, SUPABASE_SECRET_KEY) loaded by run-extractor.sh
# STOP at 4:00 PM ET (Mon-Fri, market-aware) -- stops BOTH extractors.
0 16 * * 1-5 [ "$(/opt/homebrew/bin/python3 /Users/ubexbot/.openclaw/vault/vault/SharedResources/Scripts/is_market_open.py)" = "OPEN" ] && /Users/ubexbot/.openclaw/scripts/stop-extractor.sh /Users/ubexbot/.openclaw/workspace-venkat/premium_extractor && /Users/ubexbot/.openclaw/scripts/stop-extractor.sh /Users/ubexbot/.openclaw/workspace-venkat/gex_extractor >> /Users/ubexbot/.openclaw/logs/extractor-watchdog.log 2>&1
# WATCHDOG -- every 5 min, restarts a crashed extractor during market hours.
# This is the ONLY instruction that starts the extractor. Shared with
# gex_extractor via extractor-watchdog.sh.
*/5 * * * 1-5 /Users/ubexbot/.openclaw/scripts/extractor-watchdog.sh >> /Users/ubexbot/.openclaw/logs/extractor-watchdog.log 2>&1
```

---

## Cold-start timing

The watchdog fires every 5 minutes (`*/5 * * * 1-5`) during the trading day.
Its behaviour at the start of a session:

| Time (ET)         | What happens                                                              |
|-------------------|---------------------------------------------------------------------------|
| 09:30 open        | First watchdog tick at `:30` (or `:35` if `:30` already passed).          |
| ≤ 09:35           | Extractor is DOWN; watchdog sees `MARKET_STATUS=OPEN && !is_alive` → starts. |
| ~09:30–09:35      | Extractor warm-up: launch `.venv/bin/python run.py`, Supabase client init |
|                   | (~5–10s), first options-chain scrape + write at ~15–30s. **30+ seconds of |
|                   | warm-up is normal** because the Supabase writer pool has to establish    |
|                   | fresh HTTPS connections and the options-chain scraper resolves SPX/NDX   |
|                   | OCC symbol tables.                                                        |
| 09:35+            | Extractor is UP; watchdog sees `is_alive` → exits cleanly without action. |

**Worst-case cold-start latency: 5 minutes** (the gap between two watchdog
ticks). This is acceptable because:

1. The extractor's own launch-and-warm-up is 30+ seconds (venv activation +
   Supabase writer pool init + first OCC symbol-table fetch).
2. At 09:30 sharp, the pre-market auction is still settling — the first minute
   of trading is often wide spreads and unreliable prints, so a 5-min delay
   to first-tick loses ~3 minutes of low-quality data.
3. `run-extractor.sh` is idempotent — if a previous instance is still running
   (`run.pid` points to a live PID), it logs and exits 0 without forking a
   duplicate. Two simultaneous watchdog ticks (e.g. on a host wake-from-sleep
   race) cannot fork duplicate extractor instances.

If the extractor crashes mid-session (e.g. Supabase DNS blip → writer exits),
the watchdog's next tick (≤ 5 minutes later) restarts it.

---

## Why single-instruction, not two

On **2026-06-29 at 09:30:00 ET**, two cron jobs fired in the same second on
the sibling `ibkr_trader_engine` host:

- `30 9 * * 1-5 ...run-ibkr-engine.sh` (START)
- `*/5 * * * * ...ibkr-engine-watchdog.sh` (WATCHDOG — `:00`, `:05`, ...,
  `:30`, `:35`, ...)

Both saw no pidfile. Both forked `run.py`. Both tried to claim IBKR `clientId
31`. Result: **two engine instances fought for the same clientId for ~3h 40m**
before the duplicate was detected. See `ibkr_trader_engine` PR #20 for the
post-mortem.

The same structural risk existed on `premium_extractor` before this PR
(2026-06-29): the START line at `30 9 * * 1-5` and the watchdog line at
`*/5 * * * *` both fire at `:30`, `:35`, ... — both could fork
`run-extractor.sh` simultaneously and both could see no pidfile. The Supabase
writer pool does not currently fail-fast on a duplicate writer (unlike ibkr's
clientId collision), so a duplicate extractor would silently double-write
options-chain rows to Supabase until the next watchdog tick killed one. (Note:
the watchdog itself is **shared with `gex_extractor`** — a watchdog race on
premium_extractor would also affect gex_extractor because both are managed by
the same shared script on the same `*/5` cadence.)

The lessons (from ibkr's incident):

1. **One instruction is structurally safer than two.** With one instruction
   there is no possible collision by construction — only one instruction can
   start the extractor. Two instructions require non-trivial coordination
   (mutex, stagger, failfast) to avoid the same bug recurring.
2. **Defence-in-depth still matters.** Even with one instruction,
   `run-extractor.sh`'s `kill -0 $EXISTING_PID` check (idempotent start) keeps
   two simultaneous ticks (e.g. on a host wake-from-sleep race) from forking
   duplicates.
3. **The watchdog already does cold-start.** `extractor-watchdog.sh` branches
   on `MARKET_STATUS=OPEN && !is_alive` and calls `run-extractor.sh`. No
   separate START line is needed.

PR #20 (TASK-2026-268 follow-up) removed the redundant START line on
`ibkr_trader_engine`. PR #65 (TASK-2026-288) did the same on
`tradingView_signal_generator`. This PR (TASK-2026-289) applies the same
pattern here.

---

## Applying the change

The crontab is host-managed. After this PR is merged, run this **one-time**
command to remove the redundant START line from the host crontab:

```bash
# Backup first
crontab -l > /tmp/crontab.backup-$(date +%Y-%m-%d-%H%M)

# Edit and remove the line starting with "30 9 * * 1-5" that references
# run-extractor.sh AND has "premium_extractor" in the path. Keep the
# */5 watchdog and the 0 16 STOP lines (which already cover both extractors).
crontab -e
```

The line to **remove**:

```
30 9 * * 1-5 [ "$(/opt/homebrew/bin/python3 /Users/ubexbot/.openclaw/vault/vault/SharedResources/Scripts/is_market_open.py)" = "OPEN" ] && /Users/ubexbot/.openclaw/scripts/run-extractor.sh /Users/ubexbot/.openclaw/workspace-venkat/premium_extractor /Users/ubexbot/.openclaw/workspace-venkat/premium_extractor/.venv/bin/python cron.log >> /Users/ubexbot/.openclaw/workspace-venkat/premium_extractor/logs/cron.log 2>&1
```

The lines to **keep**:

```
0 16 * * 1-5 [ ... ] && /Users/ubexbot/.openclaw/scripts/stop-extractor.sh /Users/ubexbot/.openclaw/workspace-venkat/premium_extractor && /Users/ubexbot/.openclaw/scripts/stop-extractor.sh /Users/ubexbot/.openclaw/workspace-venkat/gex_extractor ...
*/5 * * * * /Users/ubexbot/.openclaw/scripts/extractor-watchdog.sh >> /Users/ubexbot/.openclaw/logs/extractor-watchdog.log 2>&1
```

Verify after the edit:

```bash
crontab -l | grep -E "run-extractor\.sh|stop-extractor\.sh|extractor-watchdog"
# Expected: 2 lines (STOP + WATCHDOG). The START line is gone.
```

---

## Disabling for a day

To skip `premium_extractor` (and `gex_extractor`) for a single trading day
(e.g. a known-bad data day), comment out the watchdog line in the crontab
for that day, then restore it before the next session. Both extractors stay
down until the watchdog line is re-enabled.

To pause for an extended period (vacation, Supabase maintenance window),
comment out both the STOP and the WATCHDOG lines. Both extractors stay
down until you re-enable them. (Note: `extractor-watchdog.sh` is shared
between premium_extractor and gex_extractor — commenting out the watchdog
stops both. If you only want to stop premium_extractor temporarily, edit
`extractor-watchdog.sh`'s iterator to skip the premium_extractor root, or
manually stop premium_extractor via `stop-extractor.sh` and leave the
watchdog live so gex_extractor keeps running.)

For an emergency **mid-session** kill of just `premium_extractor`, run:

```bash
/Users/ubexbot/.openclaw/scripts/stop-extractor.sh /Users/ubexbot/.openclaw/workspace-venkat/premium_extractor
```

This stops the extractor cleanly without leaving a stale pidfile. The
watchdog will not restart it during market hours because the cron line is
still uncommented — call this only when you genuinely want premium_extractor
down for the rest of the session.

---

## Inspecting the state

```bash
# Is premium_extractor running?
pgrep -fl "premium_extractor.*run\.py"

# When was the last data write?
tail -n 5 /Users/ubexbot/.openclaw/workspace-venkat/premium_extractor/logs/cron.log

# What has the watchdog done today?
tail -n 30 /Users/ubexbot/.openclaw/logs/extractor-watchdog.log

# What did the STOP script do at 16:00?
tail -n 20 /Users/ubexbot/.openclaw/logs/extractor-watchdog.log

# What does the crontab look like right now?
crontab -l | grep -E "extractor|premium"
```

---

## Related

- **TASK-2026-289 (this doc)** — drop the redundant START line on
  `premium_extractor`; watchdog-only design.
- **TASK-2026-288 (PR #65, tradingView_signal_generator)** — same change on
  the sibling `tradingView_signal_generator` repo on 2026-06-29.
- **TASK-2026-268 follow-up (PR #20, ibkr_trader_engine)** — same change on
  the sibling `ibkr_trader_engine` repo on 2026-06-29.
- **TASK-2026-290 (sibling PR #10, gex_extractor)** — same change on the
  sibling `gex_extractor` repo, shipping in parallel. shares the
  `extractor-watchdog.sh` with this PR; coordination discipline means this
  PR does NOT touch the shared watchdog script.
- **TASK-2026-269 (PR #16, ibkr_trader_engine)** — `flock` / `lockdir` mutex
  on the engine wrapper. `premium_extractor`'s `run-extractor.sh` does not
  currently use `flock`, but its `kill -0 $EXISTING_PID` check is sufficient
  idempotency for the watchdog-only design.
- **TASK-2026-287** — parent "cron single-instruction refactor" directive
  from Sarthak (2026-06-29 22:13 EDT) covering `ibkr_trader_engine` and
  `tradingView_signal_generator`, extended here for `premium_extractor`.

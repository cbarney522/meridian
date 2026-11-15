# Antenna banks — module spec

**Status:** spec, not built. **Target:** CAP 0.1.4.x (after tool list / badge access, or sooner if the
8-drawer cabinet needs it). **Code home:** `app/core/rfid.py` (new class `BankSwitch`, small changes to
`parse_event`, `TagTracker`, firing patterns) + `app/core/banks_io.py` (select-line drivers).

## 1. Purpose

Let one R700 drive more antennas than it has ports by putting an external RF switch on each port
and selecting a **bank** with an output line. The reader only ever reports physical ports 1–4;
Meridian knows which bank it selected, so it turns every read into a **logical antenna** number
and everything downstream (zones, Tags in field, events, MQTT, rules) uses logical antennas only.

Reference hardware: 4 × SPDT (e.g. pSemi PE42820) on ports 1–4, one select line for all four →
2 banks × 4 ports = logical antennas 1–8. The spec allows N banks (2–4) for SP3T/SP4T switches
or several select lines, and ports that are not switched.

**Not needed with the Impinj R700 Antenna Hub (IPJ-A6010):** the reader numbers hub ports itself
(port 1 → 1–8, port 2 → 9–16, …). Banks must stay **disabled** when the hub feature is enabled;
Meridian refuses to enable both (reader detail shows hub status).

## 2. Terms

| Term | Meaning |
|---|---|
| physical port | antenna port reported by the reader (1–4) |
| bank | switch position, numbered **1…N** in UI, config and events |
| logical antenna | port as Meridian reports it (1–32) |
| epoch | one continuous period with one bank selected and RF allowed: `{id, bank, start, end}` |
| guard window | time after an epoch starts (and before it ends) in which reads are considered ambiguous |

## 3. Numbering

Default formula, for ports that go through the switch:

```
logical = port + ports_per_bank × (bank − 1)        # bank 1: 1–4, bank 2: 5–8
```

`ports_per_bank` defaults to 4. A port listed in `unswitched_ports` is wired straight to an antenna and
keeps the same logical number in every bank (default: its own port number, so with port 4
unswitched, banks give 1–3, 5–7, and 4). An explicit `map` overrides the formula:

```json
"map": {"1": {"1": 1, "2": 2, "3": 3, "4": 4}, "2": {"1": 5, "2": 6, "3": 7, "4": 8}}
```
(`map[bank][port] = logical`; validation: 1–32, no logical number used by two (bank, port) pairs
unless the port is unswitched.)

Zones (`rfid.zones`) are keyed by **logical** antenna. They already accept 1–32; no change.

## 4. Settings (`config.json` → `rfid.banks`)

```json
"banks": {
  "enabled": false,
  "count": 2,
  "ports_per_bank": 4,
  "unswitched_ports": [],
  "map": null,
  "select": [ {"out": "r700:gpo1", "active": "high"} ],
  "readback": [ {"in": "r700:gpi4"} ],
  "settle_ms": 5,
  "drain_ms": 250,
  "stop_timeout_ms": 2000,
  "guard_ms": 50,
  "check_tags": {"1": "", "2": ""},
  "check_tag_missing_after": 3,
  "idle_bank": 1
}
```

- **select**: one entry per select line. For `count` = 2, one line (low = bank 1, high = bank 2).
  For 3–4 banks, two lines, bank = binary value + 1. `out` is an output reference:
  - `r700:gpo1…4`: the reader's own GPO through the IoT Device Interface (`PUT /api/v1/device/gpos`,
    **verify on FW 10.4**). Meridian generally avoids the R700 GPIO port, but one bank-select line is
    a reasonable exception: it is local, fast (one localhost call) and needs no I/O board.
  - `io:<name>`: an output on the future Modbus I/O board (Hardware › I/O map). Allowed, but the
    poll/write cycle adds latency; `settle_ms` should be raised to the board's write-confirm time.
- **readback** (optional, strongly recommended): an input wired to each select line *at the switch
  board*, so a broken cable or dead driver shows up as a fault.
- **check_tags** (optional): EPC of a fixed tag per bank, placed so that only that bank's antenna
  reads it. Catches a switch stuck in one position, which readback cannot see.
- **idle_bank**: bank selected while no inventory runs and after a fault is cleared.

Validation (in `validate()`): `count` 2–4; enough select lines for `count`; output/input
references exist; the readback input is not also used by rules; check tags are distinct hex EPCs;
banks disabled if the reader's antenna hub is enabled.

## 5. Bank switch sequence (`BankSwitch.select(bank)`)

Runs under one lock; never overlaps with a preset start/stop from elsewhere.

1. **Stop** inventory if running (`control.stop()`); poll status until idle or `stop_timeout_ms`.
   Timeout → fault `stop_timeout`, abort the switch.
2. **Close the epoch**: `epoch.end = now`.
3. **Drain**: wait `drain_ms` so reads already captured by the reader reach the stream and are
   attributed to the closing epoch (they carry their own timestamps, §6).
4. **Drive** the select line(s). Output error → fault `output_error`, abort.
5. **Read back** (if configured); mismatch → retry once after 20 ms, then fault `readback_mismatch`.
6. **Settle** `settle_ms` (switch ICs settle in microseconds; this mainly covers relays/I/O boards).
7. **Open a new epoch** `{id+1, bank, start = now, end = None}`; emit `antenna_bank` (debug event).
8. **Start** the preset for this bank.

RF is never on while the switch moves (no hot switching). On fault: inventory stays stopped,
reads are not reported, status shows the fault, an `antenna_bank_fault` event goes out (MQTT too),
and the engine retries the switch at the next scheduled step; three faults in a row stop the pattern.

## 6. Attributing reads to a bank

Every read is attributed by **its own timestamp**, never by when Meridian received it.

- `parse_event` also returns a timestamp: the tag's `lastSeenTime` if the preset includes it,
  else the event's top-level `timestamp`. Presets used with banks **should enable `lastSeenTime`**;
  Meridian shows a warning on the bank panel when a selected preset doesn't, and when a preset uses
  a reporting interval (the event time is then the report time, not the read time).
- Clocks: Meridian runs on the reader, so epoch times and read times share one clock (UTC).
  Epoch times are taken with the wall clock, not `time.monotonic()`, for that reason.
- Keep the last 64 epochs. A read with timestamp `ts` on physical port `p` belongs to the epoch with
  `start + guard ≤ ts ≤ end` (or `end` is None and `ts ≥ start + guard`).
  - Found → `bank = epoch.bank`, `logical = map(bank, p)`.
  - `p` is unswitched → logical number regardless of epoch (still subject to "no epoch at all").
  - In a guard window or no epoch → **discard**, count in `ambiguous_reads`; debug-log the first 20.
- No timestamp in the event at all (unexpected): accept only while an epoch is open and no switch
  is in progress, using arrival time; otherwise discard.

```python
def attribute(self, port, ts):
    if port in self.unswitched:
        return self.unswitched[port], None
    g = self.guard_s
    for e in reversed(self.epochs):
        if ts >= e.start + g and (e.end is None or ts <= e.end):
            return self.map[e.bank].get(port), e.bank
        if ts >= e.start:          # inside this epoch's guard window
            break
    self.ambiguous += 1
    return None, None
```

The tracker is then called with the logical number: `tracker.read(epc, logical, rssi, now)`.

## 7. Tracker changes (bank-aware "gone after")

A tag on a bank that isn't selected is not read, and must not be reported gone for that reason.

- Firing patterns that alternate banks already stretch "gone after" to ≥ one cycle + 2 s; keep that.
- For on-demand switching (rules/MQTT select a bank and stay there), the sweeper ages a tag only
  by the time **its bank** has been selected since the tag's last read ("bank-active time"), not by
  wall time. Implementation: per bank, accumulate active seconds; store on each tag the bank's
  counter at last read; sweep compares `active_now − active_at_last_read` with the exit timeout.
  Unswitched ports age by wall time as today.
- Arrival rules (`enter_reads`, "Reads to confirm arrival") are **unchanged and never overridden**.
- A tag seen on a bank-1 antenna and then a bank-2 antenna is a normal zone move (move-quiet rules apply).

## 8. Firing patterns

Each step gets an optional `bank` (1…N; default = current bank):

```json
{"preset": "drawers", "bank": 1, "seconds": 2, "pause_s": 0},
{"preset": "drawers", "bank": 2, "seconds": 2, "pause_s": 0}
```

The pattern runner already stops the preset between steps; when the next step's bank differs, it
calls `BankSwitch.select()` (which does stop → … → start) instead of a plain start.
`pattern_cycle_s` adds the switch overhead (`drain_ms + settle_ms` + stop time, measured) so
"gone after" stretching stays correct. A pattern with no bank fields behaves exactly as today.

When banks are enabled and a plain preset (not a pattern) is started, Meridian offers
"alternate banks every N s" as a convenience: it builds an unnamed in-memory pattern with one step
per bank.

## 9. On-demand bank selection

- **Rules (when built):** action `select_bank {bank}`, e.g. drawer 6 closes (input) → select bank 2 →
  scan 3 s → back to `idle_bank`. Requests are queued; a newer request replaces a waiting one.
- **MQTT:** the `inventory` command accepts `"bank": n` with `start`, and a new `bank` command
  `{"bank": n}` that switches without changing what's running. Acks report the new epoch id or the fault.
- **UI:** bank selector on Hardware › RFID › Inventory & zones (Technician+, `rfid.edit`).

## 10. Events and API

Event payloads (`tag_enter`, `tag_exit`, Tags in field rows) keep `antenna` = **logical** and add
`port` (physical) and `bank` (null for unswitched ports or when banks are disabled).
`app/defs/events.json` gains:

| Event | Payload | Published to MQTT |
|---|---|---|
| `antenna_bank` | `{bank, epoch, previous, reason: "pattern"\|"rule"\|"mqtt"\|"ui"\|"idle"}` | no (debug/live view only; option per site) |
| `antenna_bank_fault` | `{reason: "stop_timeout"\|"output_error"\|"readback_mismatch"\|"check_tag_wrong_bank"\|"check_tag_missing", bank, details}` | yes |

`GET /api/rfid/status` adds:
```json
"banks": {"enabled": true, "count": 2, "active": 2, "epoch": 1841, "since": "…Z",
          "switches": 1840, "ambiguous_reads": 12, "fault": null, "last_switch_ms": 312}
```
`POST /api/rfid/bank {"bank": 2}` (needs `rfid.edit`). Settings travel with the existing
`/api/rfid/settings` (new `banks` object). Document both in `api.md` when built.

## 11. Self-checks

- **Readback** (§5) on every switch.
- **Check tags:** the bank-N check tag read while bank M ≠ N is selected → fault
  `check_tag_wrong_bank` (switch stuck or cross-wired). Bank N's check tag not read in
  `check_tag_missing_after` consecutive bank-N epochs → `check_tag_missing` warning (antenna,
  cable or tag problem; not a hard fault). Check tags never appear in Tags in field or tag events.
- **Ambiguous-read rate:** if more than 5 % of reads in a minute are discarded, warn on the
  dashboard (guard too long, drain too short, or the preset lacks `lastSeenTime`).

## 12. UI (Hardware › RFID › Inventory & zones)

- New panel **Antenna banks**: enable, bank count, ports per bank, unswitched ports, select line(s)
  and active level, readback input, check tags per bank, timing (advanced, collapsed), live state
  (active bank, epoch, switches, ambiguous reads, last fault) and a **Test switch** button that
  cycles every bank once and reports readback and check-tag results.
- Zone table lists logical antennas 1…(count × ports) with a hint "Bank 2 · port 3".
- Pattern step editor gets a **Bank** column (hidden when banks are disabled).
- Tags in field: Antenna column shows the logical number; tooltip shows bank/port.
- Hidden, not greyed, for roles without `rfid.edit`.

## 13. Mock and tests

`dev/fake_impinj.py`: simulated switch — each simulated tag sits on a logical antenna; the fake
reports it on its physical port only while its bank is selected (fake GPO endpoint), adds
`lastSeenTime`, and can inject late reads (timestamped before the switch, delivered after) and a
stuck switch.

Unit tests (`tests/test_rfid_banks.py`):
1. Numbering: formula, unswitched ports, explicit map, validation errors.
2. Attribution: read in epoch → right logical; late read delivered after switch → previous epoch;
   read in guard window → discarded and counted; read with no epoch → discarded.
3. Sequence order: stop → epoch end → drain → output → readback → settle → epoch start → start
   (fake control records call order); readback mismatch → fault, no start.
4. Bank-aware aging: tag on inactive bank never exits; exits after bank-active time ≥ timeout.
5. Patterns: step bank change triggers a switch; same bank does not; cycle time includes overhead.
6. Check tags: wrong-bank fault; missing warning after N epochs; check tags filtered from events.
7. Events catalogue test picks up `antenna_bank` and `antenna_bank_fault`.
8. Banks refused while the antenna hub is enabled.

## 14. Hardware notes (for the switch board)

- Switch per port: high-power SPDT, ≥ 36 dBm P0.1dB, IL ≤ 0.5 dB, isolation ≥ 30 dB at 902–928 MHz
  (PE42820: 0.3 dB / 35 dB / 45.5 dBm, 15 µs). Off-port reflective is fine.
- Select line from an R700 GPO (a switched output, not logic level) → opto or transistor pulling the
  board's 3.3 V select net; pull-down so the board defaults to bank 1 with the cable unplugged.
  Same net back out to a terminal for the readback input.
- 50 Ω coplanar waveguide on FR-4 is fine at 915 MHz with short runs; ESD protection on every
  antenna-side connector; metal enclosure; RP-TNC or SMA bulkheads.
- Budget 0.5–1 dB extra loss each way per path (switch + one more cable and connector pair).

## 15. Open questions

- R700 IoT DI GPO endpoint and latency on FW 10.4; whether GPO state survives a preset start.
- Does `control.stop()` return before the reader has delivered its last reads? (Sets `drain_ms`.)
- `lastSeenTime` resolution in the stream (µs vs ms) — sets the practical minimum `guard_ms`.
- Fixed bank per drawer vs. alternating patterns for the 8-drawer cabinet (rules decide).
- Whether to support the Impinj hub and external banks together on different ports (currently refused).

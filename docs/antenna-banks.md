# Antenna banks — module spec

**Status:** spec, not built. **Target:** CAP 0.1.4.x. **Code home:** `app/core/rfid.py` (new class
`BankTracker`, small changes to `parse_event` and `TagTracker`).
**Decision (2026-11-15, Cliff):** banks are switched by a **free-running timer on the switch board**;
the board reports each bank change to an **R700 GPI**. Meridian does not drive the switch
(Option A). Meridian-driven switching through a GPO is kept as a secondary mode (§11).

## 1. Purpose

Let one R700 drive more antennas than it has ports by putting an external RF switch on each port.
The reader only ever reports physical ports 1–4. The switch board flips banks on its own timer and
echoes the active bank to a GPI; Meridian reads the GPI events from the reader's event stream, works
out which bank each read came from, and reports a **logical antenna** number. Everything downstream
(zones, Tags in field, events, MQTT, rules) uses logical antennas only.

Reference hardware: 4 × SPDT on ports 1–4, one select line for all four, 2 banks × 4 ports =
logical antennas 1–8. The reader **keeps inventorying continuously**; a few tag replies are lost at
each flip, which is acceptable because tags stay in the field and are read on the next pass.

**Not needed with the Impinj R700 Antenna Hub (IPJ-A6010):** the reader numbers hub ports itself.
Banks must stay disabled when the hub feature is enabled; Meridian refuses to enable both.

## 2. Terms

| Term | Meaning |
|---|---|
| physical port | antenna port reported by the reader (1–4) |
| bank | switch position, numbered **1…N** in UI, config and events |
| logical antenna | port number as Meridian reports it (1–32) |
| epoch | one period with one bank active: `{id, bank, start, end}`, bounded by GPI transitions |
| guard window | ± time around a bank transition in which reads are ambiguous and discarded |

## 3. Numbering

Default formula for switched ports:

```
logical = port + ports_per_bank × (bank − 1)        # bank 1: 1–4, bank 2: 5–8
```

`ports_per_bank` defaults to 4. A port in `unswitched_ports` is wired straight to an antenna and keeps
the same logical number in every bank (default: its own port number). An explicit `map`
(`map[bank][port] = logical`) overrides the formula. Validation: 1–32; no logical number used by two
(bank, port) pairs unless the port is unswitched. Zones (`rfid.zones`) are keyed by logical antenna.

## 4. Settings (`config.json` → `rfid.banks`)

```json
"banks": {
  "enabled": false,
  "control": "timer",
  "count": 2,
  "ports_per_bank": 4,
  "unswitched_ports": [],
  "map": null,
  "bank_inputs": [ {"gpi": 1, "bank2_when": "high"} ],
  "guard_ms": 30,
  "reorder_ms": 300,
  "stall_factor": 3,
  "check_tags": {"1": "", "2": ""},
  "check_tag_missing_after": 3
}
```

- **control**: `"timer"` (this design) or `"gpo"` (§11).
- **bank_inputs**: the GPI(s) carrying the bank signal. For `count` = 2, one input; `bank2_when` sets
  the polarity. For 3–4 banks, two inputs, bank = binary value + 1 (the timer firmware must change
  both lines together; see §10).
- **guard_ms**: reads within ± this of a transition are discarded (switch settles in µs; the guard
  covers timestamp resolution and the tag reply that was in flight).
- **reorder_ms**: how long a read is held before attribution, so a GPI event that arrives on the
  stream slightly after reads stamped later than it is still applied first (§6).
- **stall_factor**: no transition for this many × the measured period → `timer_stalled` fault.
- **check_tags**: optional fixed reference tag per bank (§9).

There is no period setting: the period is set by the DIP switch on the board. Meridian **measures**
it from the GPI transitions and shows it (and its jitter) in the UI.

Validation: `count` 2–4; enough inputs for `count`; the GPIs are not also used by rules; check tags
are distinct hex EPCs; banks disabled if the reader's antenna hub is enabled.

## 5. Bank state from the GPI

- The stream already carries `gpiTransitionEvent` (`parse_event` returns `("gpi", n, level)`).
  `parse_event` must also return the event **timestamp** for GPI and tag events (§6).
- On a transition of a bank input at time `t`: close the open epoch (`end = t`), open a new one
  (`bank` from the input levels, `start = t`), increment the switch counter, update the measured
  period, emit `antenna_bank` (debug event).
- **Start-up:** read the current GPI levels once from the IoT Device Interface (endpoint to verify
  on FW 10.4) and open epoch 0 with that bank and `start = now`. If the levels can't be read, the bank
  is unknown and reads are discarded until the first transition (at most one period).
- **Reconnect** of the event stream: same as start-up (re-read levels; transitions during the gap
  are lost, so discard reads stamped inside the gap).
- Meridian never stops or restarts inventory for a bank change in timer mode.

## 6. Attributing reads to a bank

Every read is attributed by **its own timestamp**, never by when Meridian received it.

- Timestamp: the tag's `lastSeenTime` if the preset includes it, else the event's top-level
  `timestamp`. Presets used with banks **should enable `lastSeenTime`** and use **no reporting
  interval** (an aggregated report can span a flip; its event time is the report time). The bank
  panel warns about either.
- GPI and tag timestamps come from the same reader clock, so they compare directly.
- **Reorder buffer:** each read waits `reorder_ms` (default 300 ms) in a small queue before it is
  attributed, so any GPI transition stamped before it has arrived. Cost: tag events are delayed by
  that much.
- A read at `ts` on physical port `p`:
  - `p` unswitched → its fixed logical number (still needs a known bank state, i.e. not in a gap).
  - Within ± `guard_ms` of any transition → **discard**, count in `ambiguous_reads`.
  - Inside an epoch → `bank = epoch.bank`, `logical = map(bank, p)`.
  - Bank unknown (before the first transition, inside a stream gap) → discard and count.
- Keep the last 64 epochs.

```python
def attribute(self, port, ts):
    if self.in_gap(ts):
        return self._ambiguous()
    if port in self.unswitched:
        return self.unswitched[port], None
    for t in self.transitions_near(ts):
        if abs(ts - t) <= self.guard_s:
            return self._ambiguous()
    e = self.epoch_at(ts)
    if e is None or e.bank is None:
        return self._ambiguous()
    return self.map[e.bank].get(port), e.bank
```

The tracker is then called with the logical number: `tracker.read(epc, logical, rssi, now)`.

## 7. Tracker changes (bank-aware "gone after")

A tag on the bank that isn't active is not read and must not be reported gone for that reason.

- The sweeper ages a tag by the time **its bank** has been active since the tag's last read, not by
  wall time. Per bank, accumulate active seconds from the epochs; each tag stores its bank's counter
  at last read; it exits when `active_now − active_at_last_read ≥ exit_timeout`.
- Unswitched ports age by wall time as today.
- If the bank state is unknown (stall, stream gap), aging pauses for switched-port tags.
- Arrival rules ("Reads to confirm arrival") are **unchanged and never overridden**.
- A tag seen on a bank-1 antenna and then a bank-2 antenna is a normal zone move.
- UI hint: with a short period (1 s) aging is smooth; with long periods (10 s) a tag that leaves
  while its bank is idle is reported gone only after its bank comes back and stays silent.

## 8. Events and API

`tag_enter`, `tag_exit` and Tags in field rows keep `antenna` = **logical** and add `port`
(physical) and `bank` (null for unswitched ports or when banks are disabled).
`app/defs/events.json` gains:

| Event | Payload | MQTT |
|---|---|---|
| `antenna_bank` | `{bank, epoch, previous, period_s}` | no (debug/live view; per-site option) |
| `antenna_bank_fault` | `{reason: "timer_stalled"\|"bank_unknown"\|"check_tag_wrong_bank"\|"check_tag_missing"\|"bad_bank_code", bank, details}` | yes |

`GET /api/rfid/status` adds:
```json
"banks": {"enabled": true, "control": "timer", "count": 2, "active": 2, "epoch": 1841,
          "since": "…Z", "period_s": 2.0, "period_jitter_ms": 3, "switches": 1840,
          "ambiguous_reads": 12, "fault": null}
```
Settings travel with `/api/rfid/settings` (new `banks` object). Document both in `api.md` when built.

## 9. Self-checks

- **Timer stalled:** no transition for `stall_factor` × measured period (or 30 s before a period is
  known) → `timer_stalled` fault; bank state becomes unknown; switched-port reads are discarded and
  aging pauses until transitions resume. Catches a dead MCU, broken readback wire, unpowered board.
- **Bad code** (3–4 banks): an input combination that maps to no bank → `bad_bank_code`.
- **Check tags:** the bank-N check tag read while bank M ≠ N is active → `check_tag_wrong_bank`
  (RF switch stuck or cross-wired while the timer and GPI still toggle — the GPI alone can't see
  this). Bank N's check tag not read in `check_tag_missing_after` consecutive bank-N epochs →
  `check_tag_missing` warning. Check tags never appear in Tags in field or tag events.
- **Ambiguous-read rate:** more than 5 % of reads discarded in a minute → dashboard warning (guard
  too long, preset lacks `lastSeenTime`, or a reporting interval is set).

## 10. Switch board (timer) — requirements

- MCU (ATtiny-class or RP2040) at 3.3 V; DIP switch selects the period: 1, 2, 5, 10 s (4 positions;
  3 bits allow 8, e.g. add 0.5, 3, 20, 30 s). Optional: uneven split (e.g. 3 s bank 1 / 1 s bank 2)
  via a DIP bit or a second table.
- Drives SEL (through an RC to the switch CTRL pins), the bank LEDs, and an optocoupler whose
  isolated output drives the R700 GPI (input common to R700 GPI COM). Size the opto LED resistor and
  check the GPI input thresholds/pull-up requirements in the R700 GPIO spec.
- For 3–4 banks: change both select lines and both GPI lines in one port write (no intermediate code).
- Watchdog in firmware; on reset, start in bank 1.
- **Hot switching:** the switch now changes with RF on (up to 33 dBm at the reader). Confirm the
  chip's hot-switching power rating, which is usually lower than its maximum power rating; otherwise
  lower reader power or choose a part rated for it. Verify on the bench that the R700 logs no antenna
  faults at flips.
- RF: high-power SPDT per port (≥ 36 dBm P0.1dB, IL ≤ 0.5 dB, isolation ≥ 30 dB at 902–928 MHz),
  50 Ω coplanar waveguide, ESD protection at antenna connectors, metal enclosure. Budget 0.5–1 dB
  extra loss each way.

## 11. Secondary mode: Meridian-driven switching (`control: "gpo"`)

For sites that need Meridian to choose the bank (e.g. read only the drawer that just closed):
`select` output(s) (`r700:gpo1…4` via the IoT Device Interface, or `io:<name>` on the Modbus board)
replace the timer. Sequence: stop inventory → wait idle → drain (~250 ms) → set output → optional
readback on a GPI → settle → open epoch → start inventory. Firing-pattern steps gain a `bank` field;
rules and MQTT gain `select_bank`. Attribution (§6), aging (§7) and self-checks (§9) are the same.
The same board can support both modes with a jumper: MCU timer, or SEL taken from an opto input.

## 12. UI (Hardware › RFID › Inventory & zones)

- Panel **Antenna banks**: enable, control mode, bank count, ports per bank, unswitched ports, bank
  input GPI(s) and polarity, check tags, timing (guard, reorder; collapsed), live state (active bank,
  measured period and jitter, switches, ambiguous reads, last fault), and a **Watch 10 s** button
  that shows each transition and check-tag read as it happens.
- Zone table lists logical antennas 1…(count × ports) with a hint "Bank 2 · port 3".
- Tags in field: Antenna column shows the logical number; tooltip shows bank/port.
- Warnings shown on the panel: preset lacks `lastSeenTime`; preset has a reporting interval;
  antenna hub enabled.
- Hidden, not greyed, for roles without `rfid.edit`.

## 13. Mock and tests

`dev/fake_impinj.py`: simulated timer board — flips a bank every N s and emits `gpiTransitionEvent`
with timestamps; each simulated tag sits on a logical antenna and is reported on its physical port
only while its bank is active; adds `lastSeenTime`; can deliver a GPI event late (after reads stamped
later than it), stop flipping (stall), and simulate a stuck RF switch (GPI toggles, RF doesn't).

Unit tests (`tests/test_rfid_banks.py`):
1. Numbering: formula, unswitched ports, explicit map, validation errors.
2. Attribution: read in epoch → right logical; read within guard → discarded and counted; GPI event
   delivered late but within `reorder_ms` → reads still attributed correctly; read before the first
   known bank → discarded.
3. Start-up with GPI level read vs. unknown; stream reconnect gap discards reads inside the gap.
4. Bank-aware aging: tag on inactive bank never exits; exits after bank-active time ≥ timeout;
   aging pauses while stalled.
5. Stall fault after `stall_factor` × period; clears on the next transition.
6. Check tags: wrong-bank fault; missing warning after N epochs; filtered from events.
7. 3–4 banks: two-input decoding; bad code fault.
8. Events catalogue test picks up `antenna_bank` and `antenna_bank_fault`.
9. Banks refused while the antenna hub is enabled.

## 14. Open questions (verify on the bench R700, FW 10.4)

- GPI transition events on the `/api/v1/data/stream` tag stream, with timestamps (listed as
  "still to verify" in the design notes). Fallback: poll GPI state over REST — coarser timing, so a
  larger `guard_ms` and only practical with periods ≥ 2 s.
- REST endpoint for current GPI levels (start-up/reconnect).
- `lastSeenTime` resolution and its offset from the GPI timestamp (sets the minimum `guard_ms`).
- R700 GPI input electrical requirements (threshold, pull-up, max voltage) for the opto output.
- Hot-switching behavior of the chosen switch at full power; any reader antenna-fault logs at flips.
- Best default period for the 8-drawer cabinet (shorter = faster gone-after, more lost replies).

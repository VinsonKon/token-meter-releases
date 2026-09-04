# Codex Meter BLE JSON protocol v1

The device exposes one custom service and accepts compact UTF-8 JSON:

- Device name: `Codex Meter-XXXX`, where `XXXX` is the final four hexadecimal
  digits of the Bluetooth MAC address
- Service: `7f520001-1b15-4f9f-8bd8-29dbe9e681a1`
- Writable characteristic: `7f520002-1b15-4f9f-8bd8-29dbe9e681a1`
- ACK characteristic: `7f520003-1b15-4f9f-8bd8-29dbe9e681a1`
- Device-info characteristic (read): `7f520004-1b15-4f9f-8bd8-29dbe9e681a1`
- OTA-control characteristic (write): `7f520005-1b15-4f9f-8bd8-29dbe9e681a1`
- OTA-data characteristic (write): `7f520006-1b15-4f9f-8bd8-29dbe9e681a1`
- OTA-status characteristic (read/notify): `7f520007-1b15-4f9f-8bd8-29dbe9e681a1`
- Maximum message size: 512 bytes
- Suggested ATT MTU: 247; GATT long writes are supported

Example:

```json
{"v":1,"n":1783759000,"ts":1783759000,"q":[[42,300,1783776735,0],[11,10080,1784363535,0]],"t":[0,0,0,0,2111540267],"rc":3,"rcv":true,"c":182934,"p":"prolite","pr":"openai","m":"gpt-5.6-sol","a":"Work","s":"thinking","ap":false,"r":false,"af":true,"lt":true}
```

## Fields

| Key | Meaning |
|---|---|
| `v` | Protocol version; currently `1` |
| `n` | Sender sequence number |
| `ts` | Generation time in Unix seconds |
| `q` | `[short_window, weekly_window]`; either entry may be `null` |
| `t` | `[input, cached_input, output, reasoning_output, display_total]` token counts |
| `rc` | Banked Codex rate-limit resets currently available on the signed-in account |
| `rcv` | Whether `rc` is currently available; otherwise the UI displays `--` |
| `c` | Estimated API-equivalent daily cost in CNY cents, or a manual override; `-1` means unavailable |
| `p` | Plan name |
| `pr` | Optional provider id (`openai` for Codex, `anthropic` for Claude Code); absent values default to `openai` for backward compatibility |
| `m` | Model name |
| `a` | Optional account label |
| `s` | AI task status (`idle`, `thinking`, or `need approval`) |
| `ap` | Explicit pending-approval signal; when true, the device displays approval ahead of every other task status |
| `r` | Whether the selected quota is below the alert threshold or explicitly limited |
| `af` | Device-visible quota freshness; `false` keeps the device in `DATA STALE` even when BLE heartbeats continue. A transient refresh failure continues using a recent cached quota for up to 120 seconds, and local daily token totals do not affect this flag. |
| `lt` | `true` when `t[4]` is an account lifetime total; otherwise it is a local daily total |
| `x` | Optional display-test state (`1`–`16`); omitted for normal live data |

A quota window is `[used_percent, window_minutes, resets_at, tokens_used]`.
The UI displays remaining quota as `100 - used_percent`. A window with less
than 10% remaining uses the `LIMIT REACHED` card treatment; exactly 10%
remaining stays in the normal treatment. Only the affected window turns red.
`resets_at` is Unix time. The receiver calculates its countdown from
`resets_at - ts`, so it does not need network time or RTC synchronization.

For Codex, the desktop reads the official `account/rateLimits/read` response,
selects only `rateLimitsByLimitId["codex"]`, and stores every other keyed bucket
separately without transmitting it as the main quota. App Server `primary` and
`secondary` are not fixed UI slots: the desktop classifies windows by
`windowDurationMins` before encoding `q`. A 300-minute window occupies the
short/5H slot, a 10080-minute window occupies the WEEK slot, and a missing slot
is encoded as `null`. A live packet with `[null, weekly_window]` automatically
uses the Figma single-card composition. Dual-window packets keep both fixed
cards; short-only or empty packets retain the fixed layout for compatibility.

For Codex, `t[4]` is today's total across every eligible user session and every
model. The desktop derives it from differences between cumulative
`total_token_usage` records, treats a decreasing counter as a new baseline,
and removes semantically identical event replays across files. `t[0..3]` are
zero because only the all-model display total is transmitted. Session token
events are never interpreted as account quota or remaining balance. The
official `account/usage/read.summary.lifetimeTokens` value remains diagnostic
account data and is not sent as the dashboard total. Claude Code likewise uses
its locally aggregated daily total.

For the optional RMB estimate, the desktop retains input/cache/output deltas
internally per model and prices each known model separately. An unknown model
only omits that model's subtotal; it does not invalidate known-model amounts or
the all-model token total. The pricing breakdown is not exposed on the wire.

The receiver rejects malformed JSON, unsupported versions, and values over 512
bytes. Valid data is converted into a typed internal render model and persisted
to NVS. New optional keys may be added without changing protocol version; a
breaking change increments `v`.

`s` represents the AI task state only. On Codex, `task_started` enters
`thinking` and the state remains active until the session reaches
`task_complete`; concurrent sessions are combined, so one completed task does
not hide another running task. An unresolved elevated tool call changes `s` to
`need approval` and takes display priority over `thinking`; it returns to
`thinking` when the matching tool output arrives. A crashed or abandoned
session is cleared by the same 30-minute no-write timeout used for task state,
so it cannot leave the device stuck indefinitely. The quota flag `r` remains
independent, so an approval banner and a low-quota card can be visible at the
same time. Battery percentage, low-battery color, and charging are read
independently from the board PMU and may be displayed at the same time as any
task status. While charging, the battery fill repeatedly sweeps from left to
right.

Codex session inclusion is determined from structured `session_meta` fields.
`thread_source=user` remains eligible for CLI/Desktop aggregation;
`thread_source=subagent` or a structured `source.subagent` value (including
guardian and spawned worker sessions) is excluded before the realtime scan
limit, task/approval reduction, model selection, token aggregation, and cost
estimation. Arbitrary log text and `originator` are not used for filtering.

`CodexMeterSync-Test` sends `x:1` when test mode starts. While `x` is non-zero,
each new touch on the device advances the local test state through:

1. Default
2. Thinking
3. Need Approval (`x:16`, appended for compatibility)
4. Limit Reached
5. Data Stale
6. Waiting for Sync
7. Charging
8. Low Battery
9. Update Available
10. Downloading
11. Installing
12. Verifying
13. Restarting
14. Update Failed
15. Rolled Back
16. Single Quota

The sequence returns to Default after Single Quota. Test packets are rendered
in RAM but are not persisted to NVS. A normal packet with `x` omitted exits
test mode and restores live data.

## Delivery acknowledgement

The ACK characteristic supports `READ` and `NOTIFY`. After a packet has been
parsed, persisted, and rendered, the device notifies five bytes:

```text
[protocol_version, sequence_le_u32]
```

The desktop keeps the GATT connection open and considers an update complete
only after receiving the matching sequence number.

## Firmware version and BLE OTA

The device-info characteristic returns compact JSON:

```json
{"version":"1.0.6","protocol":1,"ota":true,"board":"waveshare-amoled-2.16","slot":"ota_0"}
```

OTA requires firmware installed with the dual-slot partition table. A legacy
single-slot device reports `ota:false` and must be bootstrapped once over USB
with `codex-token-monitor.factory.bin`.

The client writes JSON commands to OTA control:

```json
{"op":"begin","size":982800,"sha256":"...64 hex characters...","version":"1.0.6","board":"waveshare-amoled-2.16"}
{"op":"finish"}
{"op":"abort"}
{"op":"display","state":"UPDATE AVAILABLE","progress":0}
```

`display` is a lightweight UI-only command used by the formal Windows client
before or between OTA transfers. Supported states are `IDLE`,
`UPDATE AVAILABLE`, `DOWNLOADING`, `INSTALLING`, `VERIFYING`, `RESTARTING`,
`UPDATE FAILED`, and `ROLLED BACK`. `progress` is clamped to `0`–`100` and is
used by the downloading/installing layouts. `IDLE` clears the transient update
UI and returns to the live dashboard.

After `begin`, the device validates board id, semantic version, image size,
battery level, and availability of an inactive OTA partition, then notifies
`{"state":"ready","offset":0}`. Each OTA-data write contains a four-byte
little-endian absolute offset followed by image bytes. Offsets must be
contiguous. OTA-status sends progress/error JSON and finally `complete` or
`restarting`.

During a normal update, the computer download is shown as `DOWNLOADING`; writes
to the inactive ESP32 partition are shown as `INSTALLING`; digest and image
validation are shown as `VERIFYING`; and the successful handoff is shown as
`RESTARTING`. The last three states animate their ellipsis continuously.

Both the desktop and device calculate SHA-256 over the downloaded/transferred
image. The device switches the boot partition only after size and digest match,
then restarts. The current release format provides integrity checking but no
separate publisher signature.

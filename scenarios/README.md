# Scenarios

A scenario is a JSON file that sends commands to the running cFS and states the
telemetry that must come back. `./start --no-ui scenario` runs scenario files without
the browser and exits 0 when every scenario passes, so a build server can use them as
tests of the flight software.

```sh
./start --no-ui build
./start --no-ui run --no-follow
./start --no-ui scenario scenarios/*.json --report report.json --junit report.xml
./start --no-ui stop
```

The runner connects the [Ground Station](../ground_station/README.md) to the cFS that
Flight Studio started on the selected target, so cFS must be running first. It uses the
same connection as the browser's Ground station view. Disconnect that view before
running scenarios: TO_LAB sends telemetry to one address, and running both at once is
not supported.

## A scenario file

```json
{
  "name": "SAMPLE_APP accepts a NOOP command",
  "description": "Checks the whole command path.",
  "requirements": ["REQ-CMD-001"],
  "steps": [
    {"expect_telemetry": {"packet": "SAMPLE_APP_HkTlm_t"}, "timeout": 15},
    {"send": "SAMPLE_APP_NOOP_CC"},
    {"expect_event": {"app": "SAMPLE_APP", "contains": "NOOP command"}, "timeout": 5},
    {"expect_telemetry": {"packet": "SAMPLE_APP_HkTlm_t", "field": "CommandCounter",
                          "increases_by": 1}, "timeout": 15}
  ]
}
```

`name` and `steps` are required. `requirements` lists the identifiers of the
requirements the scenario verifies; they are copied to the reports and can be those of
your own mission. Steps run in order.

| Step | Meaning |
| --- | --- |
| `{"send": ID, "values": {...}}` | Sends one command. `ID` is a command identifier of the build, as shown in the Ground station view, and `values` gives its fields by name. |
| `{"expect_event": {...}, "timeout": S}` | Waits for an event message. `app` is the name of the app that sent it (letter case is ignored), `event_id` its number, `contains` a text the message includes and `matches` a regular expression found in the message. Every key given must match. |
| `{"expect_telemetry": {"packet": NAME}, "timeout": S}` | Waits for one packet of that name. |
| `{"expect_telemetry": {"packet": NAME, "field": F, COMPARISON: V}, "timeout": S}` | Waits for a packet whose field satisfies the comparison. |
| `{"wait": S}` | Waits `S` seconds. |

`timeout` is in seconds, defaults to 10 and may be up to 600. Packet and field names
are the ones the Telemetry tab shows, such as `PacketID.EventID` for a nested field.

| Comparison | Passes when |
| --- | --- |
| `"equals": V` | the field equals `V` |
| `"at_least": V`, `"at_most": V` | the field is within the limit; both may be given for a range |
| `"increases_by": N` | the field is `N` more than in the last packet received before the command |

## What an expectation accepts

An expectation only accepts telemetry received after the last command, or after the
start of the scenario when no command has been sent. An event or a counter left over
from an earlier step therefore cannot satisfy a later one. Commands sent one right
after another count as one group, so the replies to all of them stay visible:

```json
{"send": "A_CC"}, {"send": "B_CC"},
{"expect_event": {"contains": "reply to A"}}, {"expect_event": {"contains": "reply to B"}}
```

An expectation waits until a matching packet arrives or its timeout ends. A housekeeping
packet that was already on its way when the command was sent still shows the old
counter; the step keeps waiting for the next one.

`increases_by` needs a value from before the command. Start the scenario with an
`expect_telemetry` step for that packet, as in the example above, so that one has
arrived.

## Results

Each step is reported as PASSED, FAILED or NOT RUN. A scenario stops at its first
failed step and the steps after it are not run; the next scenario file still runs.

```
Scenario: SAMPLE_APP accepts a NOOP command (scenarios/sample_app_noop.json)
  PASSED   step 1 expect_telemetry {"packet": "SAMPLE_APP_HkTlm_t"}: SAMPLE_APP_HkTlm_t received
  PASSED   step 2 send "SAMPLE_APP_NOOP_CC": sent 8 bytes to MID 0x1882, command code 0
  FAILED   step 3 expect_event {"app": "SAMPLE_APP", "contains": "NOOP"}: no matching event in 5 s; no events were received
  NOT RUN  step 4 expect_telemetry {...}: an earlier step failed
  FAILED in 5.01 s
```

| Exit code | Meaning |
| --- | --- |
| 0 | Every scenario passed. |
| 1 | A scenario failed, a file is invalid or cFS could not be reached. The reason is printed. |
| 2 | The command line is invalid. |

Before sending anything, the runner checks every file against the running build. A
command, packet or field that the build does not have is reported with its step number
and nothing is sent.

`--report PATH` writes the full result as JSON and `--junit PATH` as JUnit XML, with
one test suite per scenario and one test case per step. Both record the target, the
fingerprint of the build, the commit of the `cFS` submodule and the requirement
identifiers, so a result can be traced to the software it was obtained on.

## Limits

- A PASSED step shows what the telemetry reported. It does not show timing margins: QEMU
  and a native process do not run at the speed of flight hardware.
- `increases_by` does not follow a counter that wraps around to zero.
- Scenarios cannot set emulated sensor values yet. Use
  [synthetic sensor data](../sensors/README.md) alongside a scenario.

#!/usr/bin/env python3
"""
Dell PowerEdge XE9640 Fleet CLI Simulator
==========================================

An educational, offline sandbox that mimics the look and feel of managing a
fleet of Dell PowerEdge XE9640 (8x GPU) servers laid out across a real data
center hierarchy:

    Data Center (4) -> Data Hall (up to 6 each) -> Pod (32) -> Rack (16)
    -> Server (8)  =  up to 98,304 addressable servers.

You start at a "fleet console" (like a jump host / DCIM tool) where you can
browse the hierarchy and `connect` to exactly one server at a time. Once
connected, you get the original single-server shell: a subset of `racadm`
(iDRAC command-line) syntax plus common Linux/OS-level diagnostic commands,
backed by a small in-memory hardware/firmware model, with the same
fault-injection engine as before (failed DIMM, GPU fallen off the bus, PSU
failure, crashed NIC driver, stuck iDRAC job, outdated firmware, failing
NVMe drive, failed fan).

Each server's state is created lazily the first time it's touched (via
`connect` or `scenario fleet`), so the simulator stays lightweight even
though the addressable fleet is large.

This is NOT real Dell software, is not affiliated with or endorsed by Dell,
and does not talk to any real hardware. Command output is illustrative, not
a byte-for-byte reproduction of real racadm/OS output.

Run:
    python xe9640_sim.py

Then try:
    help
    topology
    list dc
    connect 1 1 1 1 1
    scenario start
    racadm getsel
    faults
    exit
    scenario fleet 3
    alerts
"""

from __future__ import annotations

import hashlib
import random
import re
import shlex
import sys
from collections import namedtuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

CHASSIS = "PowerEdge XE9640"
SERVICE_TAG = "9XE9640"  # fallback tag, only used if a state has no location

# ---------------------------------------------------------------------------
# Fleet topology
#
#   4 data centers, each with up to 6 data halls, each hall with 32 pods,
#   each pod with 16 racks, each rack with 8 servers.
#
#   HALLS_PER_DC is a dict so a data center can have fewer than the max 6
#   halls if that's a better match for your real layout -- just edit the
#   numbers below.
# ---------------------------------------------------------------------------

NUM_DATA_CENTERS = 4
HALLS_PER_DC = {1: 6, 2: 6, 3: 6, 4: 6}  # dc -> number of data halls
PODS_PER_HALL = 32
RACKS_PER_POD = 16
SERVERS_PER_RACK = 8

Location = namedtuple("Location", "dc dh pod rack srv")


def total_fleet_size() -> int:
    halls = sum(HALLS_PER_DC.get(dc, 0) for dc in range(1, NUM_DATA_CENTERS + 1))
    return halls * PODS_PER_HALL * RACKS_PER_POD * SERVERS_PER_RACK


def location_id(loc: Location) -> str:
    return f"DC{loc.dc}-DH{loc.dh:02d}-POD{loc.pod:02d}-RACK{loc.rack:02d}-SRV{loc.srv}"


def synth_service_tag(loc_id: str) -> str:
    """Deterministic 7-char pseudo Dell service tag derived from location."""
    h = hashlib.sha1(loc_id.encode()).hexdigest().upper()
    return h[:7]


def _to_int(tok: str) -> Optional[int]:
    """Parse '7', 'dc7', 'DH03', 'rack05', 'srv7' etc. into an int."""
    t = tok.strip().lower()
    for pre in ("dc", "dh", "pod", "rack", "srv"):
        if t.startswith(pre):
            t = t[len(pre):]
            break
    t = t.lstrip("0") or "0"
    try:
        return int(t)
    except ValueError:
        return None


def parse_location(args: list) -> Optional[Location]:
    """Accept 'connect 1 3 12 5 7', 'connect dc1 dh3 pod12 rack5 srv7',
    or 'connect DC1-DH03-POD12-RACK05-SRV07' (also '/' or '.' separated)."""
    if len(args) == 1 and re.search(r"[-/.]", args[0]):
        parts = re.split(r"[-/.]+", args[0].strip())
    else:
        parts = args
    if len(parts) != 5:
        return None
    nums = [_to_int(p) for p in parts]
    if any(n is None for n in nums):
        return None
    return Location(*nums)


def validate_location(loc: Location) -> Optional[str]:
    if not (1 <= loc.dc <= NUM_DATA_CENTERS):
        return f"Invalid data center {loc.dc}. Valid range: 1-{NUM_DATA_CENTERS}."
    max_dh = HALLS_PER_DC.get(loc.dc, 0)
    if not (1 <= loc.dh <= max_dh):
        return f"Invalid data hall {loc.dh} for DC{loc.dc}. Valid range: 1-{max_dh}."
    if not (1 <= loc.pod <= PODS_PER_HALL):
        return f"Invalid pod {loc.pod}. Valid range: 1-{PODS_PER_HALL}."
    if not (1 <= loc.rack <= RACKS_PER_POD):
        return f"Invalid rack {loc.rack}. Valid range: 1-{RACKS_PER_POD}."
    if not (1 <= loc.srv <= SERVERS_PER_RACK):
        return f"Invalid server {loc.srv}. Valid range: 1-{SERVERS_PER_RACK}."
    return None


# Sparse: only servers that have been connected to or fault-seeded exist here.
FLEET: dict = {}


# ---------------------------------------------------------------------------
# Hardware / firmware state model (one per server)
# ---------------------------------------------------------------------------


def _dimm_layout():
    dimms = []
    for bank in ("A", "B"):
        for i in range(1, 9):
            dimms.append({"id": f"{bank}{i}", "size_gb": 64, "status": "Ok"})
    return dimms


def fresh_state() -> dict:
    now = datetime.now()
    return {
        "location": None,
        "service_tag": SERVICE_TAG,
        "power": "On",
        "bios_version": "2.4.3",
        "bios_version_latest": "2.6.1",
        "idrac_version": "7.10.30.00",
        "idrac_version_latest": "7.10.30.00",
        "boot_time": now,
        "cpus": [
            {"id": "CPU.Socket.1", "model": "Intel Xeon Platinum 8462Y+", "cores": 32, "status": "Ok"},
            {"id": "CPU.Socket.2", "model": "Intel Xeon Platinum 8462Y+", "cores": 32, "status": "Ok"},
        ],
        "dimms": _dimm_layout(),
        "gpus": [
            {"id": f"GPU.{i}", "model": "NVIDIA H100 SXM5 80GB", "status": "Ok", "temp_c": 42, "xid": None}
            for i in range(1, 9)
        ],
        "psus": [
            {"id": "PSU.Slot.1", "watt": 2800, "status": "Ok"},
            {"id": "PSU.Slot.2", "watt": 2800, "status": "Ok"},
        ],
        "fans": [{"id": f"Fan.{i}", "rpm": 9800, "status": "Ok"} for i in range(1, 9)],
        "nvme": [
            {"id": f"Disk.Bay.{i}", "size": "3.84TB", "status": "Ok"} for i in range(0, 8)
        ],
        "nics": [
            {"id": "NIC.Embedded.1-1", "link": "Up", "status": "Ok"},
            {"id": "NIC.Embedded.2-1", "link": "Up", "status": "Ok"},
        ],
        "networking_service": "active",
        "sel": [],
        "sel_seq": 0,
        "jobqueue": [],
        "active_faults": {},  # key -> {"step": int, "def": fault_def}
        "started": now,
    }


def get_or_create_state(loc: Location) -> dict:
    lid = location_id(loc)
    if lid not in FLEET:
        st = fresh_state()
        st["location"] = lid
        st["service_tag"] = synth_service_tag(lid)
        FLEET[lid] = st
    return FLEET[lid]


def add_sel(state, severity, message):
    state["sel_seq"] += 1
    ts = datetime.now() - timedelta(seconds=random.randint(0, 120))
    state["sel"].append(
        {
            "seq": state["sel_seq"],
            "time": ts,
            "severity": severity,
            "message": message,
        }
    )


def find(items, item_id):
    for it in items:
        if it["id"].lower() == item_id.lower():
            return it
    return None


# ---------------------------------------------------------------------------
# Fault definitions
#
# Each fault has:
#   key          unique id used with `inject <key>`
#   title        short description
#   severity     Critical / Warning
#   inject(state)   mutates state to introduce the problem
#   hint         nudges the user toward the right diagnostic command
#   steps        ordered list of (regex, human description) the user must
#                run (in order) to resolve the fault
#   solution     the exact command sequence, for `solution <key>`
#   resolve(state) mutates state back to healthy
# ---------------------------------------------------------------------------


@dataclass
class Fault:
    key: str
    title: str
    severity: str
    inject: Callable[[dict], None]
    hint: str
    steps: list
    solution: str
    resolve: Callable[[dict], None]


def _inject_dimm_ecc(state):
    d = find(state["dimms"], "A5")
    d["status"] = "Critical"
    add_sel(state, "Critical", "Memory: Uncorrectable ECC error detected on DIMM A5 (CPU1)")


def _resolve_dimm_ecc(state):
    d = find(state["dimms"], "A5")
    d["status"] = "Ok"
    add_sel(state, "Info", "Memory: DIMM A5 replaced and passed POST memory test")


def _inject_gpu_fallen_off_bus(state):
    g = find(state["gpus"], "GPU.3")
    g["status"] = "Unknown"
    g["xid"] = 79
    add_sel(state, "Critical", "NVRM: Xid (PCI:0000:8d:00): 79, GPU.3 has fallen off the bus")


def _resolve_gpu_fallen_off_bus(state):
    g = find(state["gpus"], "GPU.3")
    g["status"] = "Ok"
    g["xid"] = None
    add_sel(state, "Info", "GPU.3 reseated and re-enumerated successfully after power cycle")


def _inject_psu_failure(state):
    p = find(state["psus"], "PSU.Slot.2")
    p["status"] = "Critical"
    add_sel(state, "Critical", "PSU: Power supply PSU.Slot.2 failure (AC lost / unit fault)")


def _resolve_psu_failure(state):
    p = find(state["psus"], "PSU.Slot.2")
    p["status"] = "Ok"
    add_sel(state, "Info", "PSU: PSU.Slot.2 replaced, redundancy restored")


def _inject_nic_driver_crash(state):
    n = find(state["nics"], "NIC.Embedded.1-1")
    n["link"] = "Down"
    n["status"] = "Degraded"
    state["networking_service"] = "failed"
    add_sel(state, "Warning", "NIC.Embedded.1-1: link down, driver watchdog timeout")


def _resolve_nic_driver_crash(state):
    n = find(state["nics"], "NIC.Embedded.1-1")
    n["link"] = "Up"
    n["status"] = "Ok"
    state["networking_service"] = "active"
    add_sel(state, "Info", "NIC.Embedded.1-1: link restored after networking service restart")


def _inject_bios_outdated(state):
    state["bios_version"] = "2.4.3"
    add_sel(state, "Warning", f"BIOS: installed 2.4.3 is older than recommended {state['bios_version_latest']}")


def _resolve_bios_outdated(state):
    state["bios_version"] = state["bios_version_latest"]
    add_sel(state, "Info", "BIOS: firmware update completed successfully")


def _inject_fan_failure(state):
    f = find(state["fans"], "Fan.4")
    f["status"] = "Critical"
    f["rpm"] = 0
    for g in state["gpus"]:
        g["temp_c"] += 9
    add_sel(state, "Critical", "Cooling: Fan.4 failure detected, RPM = 0")


def _resolve_fan_failure(state):
    f = find(state["fans"], "Fan.4")
    f["status"] = "Ok"
    f["rpm"] = 9800
    for g in state["gpus"]:
        g["temp_c"] = max(42, g["temp_c"] - 9)
    add_sel(state, "Info", "Cooling: Fan.4 replaced, RPM nominal")


def _inject_nvme_predictive(state):
    d = find(state["nvme"], "Disk.Bay.2")
    d["status"] = "Predictive Failure"
    add_sel(state, "Warning", "Storage: Disk.Bay.2 SMART predictive failure (wear level critical)")


def _resolve_nvme_predictive(state):
    d = find(state["nvme"], "Disk.Bay.2")
    d["status"] = "Ok"
    add_sel(state, "Info", "Storage: Disk.Bay.2 replaced and rebuilt")


def _inject_jobqueue_stuck(state):
    state["jobqueue"].append(
        {
            "id": "JID_804512030123",
            "name": "Firmware Update: BIOS",
            "status": "Running",
            "percent": 42,
            "started": datetime.now() - timedelta(hours=6),
        }
    )
    add_sel(state, "Warning", "Lifecycle Controller: job JID_804512030123 has not progressed in 5+ hours")


def _resolve_jobqueue_stuck(state):
    state["jobqueue"] = [j for j in state["jobqueue"] if j["id"] != "JID_804512030123"]
    add_sel(state, "Info", "Lifecycle Controller: stuck job JID_804512030123 deleted from queue")


FAULTS: dict = {
    f.key: f
    for f in [
        Fault(
            key="dimm_ecc",
            title="Uncorrectable ECC memory error on DIMM A5",
            severity="Critical",
            inject=_inject_dimm_ecc,
            hint="Check `racadm getsel` or `dmidecode -t memory` for the failing DIMM slot.",
            steps=[(r"^service replace dimm a5$", "Replace failed DIMM A5")],
            solution="service replace dimm A5",
            resolve=_resolve_dimm_ecc,
        ),
        Fault(
            key="gpu_bus",
            title="GPU.3 fallen off the PCIe/NVLink bus (Xid 79)",
            severity="Critical",
            inject=_inject_gpu_fallen_off_bus,
            hint="Run `nvidia-smi` and `dmesg` - look for a Xid 79 error on GPU.3.",
            steps=[
                (r"^service reseat gpu 3$", "Reseat GPU.3"),
                (r"^racadm serveraction powercycle$", "Power cycle the server"),
            ],
            solution="service reseat gpu 3  (then)  racadm serveraction powercycle",
            resolve=_resolve_gpu_fallen_off_bus,
        ),
        Fault(
            key="psu_fail",
            title="PSU.Slot.2 failure, power redundancy lost",
            severity="Critical",
            inject=_inject_psu_failure,
            hint="Run `racadm getsensorinfo` or `racadm hwinventory` to see which PSU is critical.",
            steps=[(r"^service replace psu 2$", "Replace PSU.Slot.2")],
            solution="service replace psu 2",
            resolve=_resolve_psu_failure,
        ),
        Fault(
            key="nic_crash",
            title="Embedded NIC1 driver hung, link down (software only)",
            severity="Warning",
            inject=_inject_nic_driver_crash,
            hint="Run `ip a` and `systemctl status networking` to confirm the service failed.",
            steps=[(r"^systemctl restart networking$", "Restart the networking service")],
            solution="systemctl restart networking",
            resolve=_resolve_nic_driver_crash,
        ),
        Fault(
            key="bios_old",
            title="BIOS firmware out of date",
            severity="Warning",
            inject=_inject_bios_outdated,
            hint="Run `racadm swinventory` and compare BIOS version to the recommended version.",
            steps=[(r"^racadm update -f bios\.exe -t tftp$", "Push the BIOS firmware update")],
            solution="racadm update -f BIOS.exe -t TFTP",
            resolve=_resolve_bios_outdated,
        ),
        Fault(
            key="fan_fail",
            title="Fan.4 failure, elevated GPU temperatures",
            severity="Critical",
            inject=_inject_fan_failure,
            hint="Run `racadm getsensorinfo` to see which fan reports 0 RPM.",
            steps=[(r"^service replace fan 4$", "Replace Fan.4")],
            solution="service replace fan 4",
            resolve=_resolve_fan_failure,
        ),
        Fault(
            key="nvme_smart",
            title="Disk.Bay.2 SMART predictive failure",
            severity="Warning",
            inject=_inject_nvme_predictive,
            hint="Run `racadm hwinventory` and look for a drive reporting Predictive Failure.",
            steps=[(r"^service replace nvme 2$", "Replace Disk.Bay.2")],
            solution="service replace nvme 2",
            resolve=_resolve_nvme_predictive,
        ),
        Fault(
            key="job_stuck",
            title="Stuck Lifecycle Controller firmware job",
            severity="Warning",
            inject=_inject_jobqueue_stuck,
            hint="Run `racadm jobqueue view` to find the job that isn't progressing.",
            steps=[(r"^racadm jobqueue delete -i jid_804512030123$", "Delete the stuck job")],
            solution="racadm jobqueue delete -i JID_804512030123",
            resolve=_resolve_jobqueue_stuck,
        ),
    ]
}


# ---------------------------------------------------------------------------
# Output formatting helpers
# ---------------------------------------------------------------------------


def table(headers, rows):
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(str(c)))
    lines = []
    sep = "  "
    lines.append(sep.join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    lines.append(sep.join("-" * widths[i] for i in range(len(headers))))
    for r in rows:
        lines.append(sep.join(str(c).ljust(widths[i]) for i, c in enumerate(r)))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-server command implementations
# ---------------------------------------------------------------------------


def cmd_help(args, state):
    if not args:
        return """\
Available commands (type `help <topic>` for details):

  System            status, whoami, faults, hint <id>, solution <id>
  Scenario engine    scenario start [n], scenario list, inject <id>, reset
  racadm             getsysinfo, getsel [-c], hwinventory, swinventory,
                      getsensorinfo, get <FQDD>, serveraction <action>,
                      jobqueue view|delete -i <id>, update -f <file> -t <target>
  OS shell           nvidia-smi [-r], lspci, dmesg, dmidecode -t memory,
                      free -h, lscpu, df -h, ip a, ipmitool sel list,
                      systemctl status|restart <service>, uptime
  Maintenance        service reseat <component> <id>
                      service replace <component> <id>
  Session            exit / disconnect (back to fleet console)
                      quit (exit the simulator entirely)
  Misc               help, clear

Topics: help racadm | help os | help service | help scenario"""
    topic = args[0].lower()
    topics = {
        "racadm": """\
racadm getsysinfo                 Show system/chassis summary
racadm getsel [-c]                Show System Event Log (-c clears it)
racadm hwinventory                Show hardware component status
racadm swinventory                Show firmware/BIOS/iDRAC versions
racadm getsensorinfo              Show temperature/fan/voltage sensors
racadm get <FQDD>                 Show a config group, e.g. System.ServerPwr
racadm serveraction <action>      powerstatus|powerup|powerdown|powercycle|hardreset
racadm jobqueue view              List Lifecycle Controller jobs
racadm jobqueue delete -i <id>    Delete a job by ID
racadm update -f <file> -t TFTP   Apply a firmware update (fixes stale firmware)
racadm racreset                   Reset the iDRAC controller""",
        "os": """\
nvidia-smi [-r]        GPU status table (-r resets GPUs)
lspci                  List PCI devices
dmesg                  Kernel ring buffer (recent hardware/driver events)
dmidecode -t memory    DIMM inventory and ECC status
free -h                Memory usage summary
lscpu                  CPU summary
df -h                  Filesystem usage
ip a                   Network interface status
ipmitool sel list      Alternate view of the System Event Log
systemctl status|restart <service>   e.g. `systemctl restart networking`
uptime                 Server uptime""",
        "service": """\
service reseat <component> <id>    Simulate physically reseating a part
service replace <component> <id>   Simulate physically replacing a part
Components: dimm, gpu, psu, fan, nvme
Example: service replace psu 2""",
        "scenario": """\
scenario start [n]     Reset to healthy, then inject n random faults (default 2)
scenario list          List every fault the simulator knows about
inject <id>            Inject one specific fault by key (see `scenario list`)
reset                  Clear all faults, return to a fully healthy system
faults                 List currently active (unresolved) faults
hint <id>              Get a diagnostic nudge for an active fault
solution <id>          Reveal the exact fix command(s) for a fault""",
    }
    return topics.get(topic, f"No help topic '{topic}'. Try: racadm, os, service, scenario")


def cmd_status(args, state):
    issues = len(state["active_faults"])
    health = "HEALTHY" if issues == 0 else f"{issues} ACTIVE ISSUE(S)"
    lines = [
        f"{CHASSIS}  (Service Tag: {state.get('service_tag', SERVICE_TAG)})",
        f"Location        : {state.get('location') or 'n/a (standalone)'}",
        f"Power           : {state['power']}",
        f"Overall Health  : {health}",
        f"BIOS Version    : {state['bios_version']}",
        f"iDRAC Version   : {state['idrac_version']}",
    ]
    if issues:
        lines.append("")
        lines.append("Run `faults` to see details, or `help scenario` for the fix workflow.")
    return "\n".join(lines)


def cmd_whoami(args, state):
    return f"{state.get('location') or '(standalone, not part of the fleet)'}  Service Tag: {state.get('service_tag', SERVICE_TAG)}"


def cmd_faults(args, state):
    if not state["active_faults"]:
        return "No active faults. System is healthy."
    rows = []
    for key, entry in state["active_faults"].items():
        fdef = entry["def"]
        rows.append((key, fdef.severity, fdef.title))
    return table(["ID", "SEVERITY", "DESCRIPTION"], rows)


def cmd_scenario(args, state):
    if not args or args[0] == "list":
        rows = [(f.key, f.severity, f.title) for f in FAULTS.values()]
        return table(["ID", "SEVERITY", "DESCRIPTION"], rows)
    if args[0] == "start":
        n = 2
        if len(args) > 1:
            try:
                n = max(1, min(int(args[1]), len(FAULTS)))
            except ValueError:
                pass
        loc, tag = state.get("location"), state.get("service_tag", SERVICE_TAG)
        state.update(fresh_state())
        state["location"], state["service_tag"] = loc, tag
        keys = random.sample(list(FAULTS.keys()), n)
        for k in keys:
            _inject_fault(state, k)
        return (
            f"Scenario started with {n} injected fault(s).\n"
            "Use `faults`, `racadm getsel`, `nvidia-smi`, `dmesg`, etc. to diagnose,\n"
            "then use `service reseat/replace ...` or the relevant racadm/OS command to fix.\n"
            "(`hint <id>` for a nudge, `solution <id>` if you're stuck.)"
        )
    return "Usage: scenario start [n] | scenario list"


def _inject_fault(state, key):
    fdef = FAULTS[key]
    fdef.inject(state)
    state["active_faults"][key] = {"step": 0, "def": fdef}


def cmd_inject(args, state):
    if not args or args[0] not in FAULTS:
        valid = ", ".join(FAULTS.keys())
        return f"Usage: inject <id>\nValid ids: {valid}"
    key = args[0]
    if key in state["active_faults"]:
        return f"Fault '{key}' is already active."
    _inject_fault(state, key)
    return f"Injected fault: {FAULTS[key].title}"


def cmd_reset(args, state):
    loc, tag = state.get("location"), state.get("service_tag", SERVICE_TAG)
    state.update(fresh_state())
    state["location"], state["service_tag"] = loc, tag
    return "System reset. All components healthy, SEL cleared."


def cmd_hint(args, state):
    if not args:
        return "Usage: hint <id>"
    key = args[0]
    if key not in state["active_faults"]:
        return f"No active fault with id '{key}'. Run `faults` to see active issues."
    return FAULTS[key].hint


def cmd_solution(args, state):
    if not args:
        return "Usage: solution <id>"
    key = args[0]
    if key not in FAULTS:
        return f"Unknown fault id '{key}'."
    return f"Fix: {FAULTS[key].solution}"


# --- racadm ---------------------------------------------------------------


def racadm_getsysinfo(state):
    lines = [
        "System Information:",
        f"Chassis Model             = {CHASSIS}",
        f"Service Tag               = {state.get('service_tag', SERVICE_TAG)}",
        f"Location                  = {state.get('location') or 'n/a (standalone)'}",
        f"BIOS Version               = {state['bios_version']}",
        f"CPU Count                  = {len(state['cpus'])}",
        f"GPU Count                  = {len(state['gpus'])}",
        f"System Power State         = {state['power']}",
        "",
        "iDRAC Information:",
        f"iDRAC Firmware Version     = {state['idrac_version']}",
    ]
    return "\n".join(lines)


def racadm_getsel(state, clear=False):
    if not state["sel"]:
        out = "System Event Log is empty."
    else:
        rows = [
            (e["seq"], e["time"].strftime("%Y-%m-%d %H:%M:%S"), e["severity"], e["message"])
            for e in state["sel"]
        ]
        out = table(["SEQ", "TIME", "SEVERITY", "MESSAGE"], rows)
    if clear:
        state["sel"] = []
        state["sel_seq"] = 0
        out += "\n\nSEL cleared."
    return out


def racadm_hwinventory(state):
    rows = []
    for c in state["cpus"]:
        rows.append((c["id"], "CPU", c["model"], c["status"]))
    for d in state["dimms"]:
        rows.append((f"DIMM.{d['id']}", "Memory", f"{d['size_gb']}GB", d["status"]))
    for g in state["gpus"]:
        rows.append((g["id"], "GPU", g["model"], g["status"]))
    for p in state["psus"]:
        rows.append((p["id"], "PSU", f"{p['watt']}W", p["status"]))
    for f in state["fans"]:
        rows.append((f["id"], "Fan", f"{f['rpm']} RPM", f["status"]))
    for n in state["nvme"]:
        rows.append((n["id"], "NVMe", n["size"], n["status"]))
    return table(["FQDD", "TYPE", "INFO", "STATUS"], rows)


def racadm_swinventory(state):
    rows = [
        ("BIOS", state["bios_version"], state["bios_version_latest"]),
        ("iDRAC with Lifecycle Controller", state["idrac_version"], state["idrac_version_latest"]),
    ]
    return table(["COMPONENT", "INSTALLED", "RECOMMENDED"], rows)


def racadm_getsensorinfo(state):
    rows = []
    for g in state["gpus"]:
        rows.append((g["id"], "Temperature", f"{g['temp_c']} C", "Ok" if g["temp_c"] < 85 else "Warning"))
    for f in state["fans"]:
        rows.append((f["id"], "Fan Speed", f"{f['rpm']} RPM", f["status"]))
    for p in state["psus"]:
        rows.append((p["id"], "Power Supply", f"{p['watt']}W", p["status"]))
    return table(["SENSOR", "TYPE", "READING", "STATUS"], rows)


def racadm_get(state, fqdd):
    fqdd = fqdd or ""
    known = {
        "system.serverpwr": f"System.ServerPwr.PowerState={state['power']}",
        "idrac.info": f"iDRAC.Info.Version={state['idrac_version']}",
        "bios.biosinformation": f"BIOS.BiosInformation.Version={state['bios_version']}",
    }
    val = known.get(fqdd.lower())
    if val:
        return val
    return f"ERROR: unknown or unsupported group '{fqdd}'. Try: System.ServerPwr, iDRAC.Info, BIOS.BiosInformation"


def racadm_serveraction(state, action):
    action = (action or "").lower()
    if action == "powerstatus":
        return f"Server power status: {state['power']}"
    if action == "powerup":
        state["power"] = "On"
        return "Server power operation: powerup ... Server is powered ON."
    if action == "powerdown":
        state["power"] = "Off"
        return "Server power operation: powerdown ... Server is powered OFF."
    if action in ("powercycle", "hardreset"):
        state["power"] = "On"
        return f"Server power operation: {action} ... Server has been reset and is powered ON."
    return "Usage: racadm serveraction <powerstatus|powerup|powerdown|powercycle|hardreset>"


def racadm_jobqueue(state, args):
    if not args or args[0] == "view":
        if not state["jobqueue"]:
            return "No jobs in the queue."
        rows = [(j["id"], j["name"], j["status"], f"{j['percent']}%") for j in state["jobqueue"]]
        return table(["JOB ID", "NAME", "STATUS", "PROGRESS"], rows)
    if args[0] == "delete" and "-i" in args:
        jid = args[args.index("-i") + 1]
        before = len(state["jobqueue"])
        state["jobqueue"] = [j for j in state["jobqueue"] if j["id"].lower() != jid.lower()]
        if len(state["jobqueue"]) < before:
            return f"Job {jid} deleted from the queue."
        return f"Job {jid} not found."
    return "Usage: racadm jobqueue view | racadm jobqueue delete -i <id>"


def racadm_update(state, args):
    if "bios" in " ".join(args).lower():
        return "Firmware update staged. It will complete on next `racadm jobqueue view` check or reboot."
    return "Firmware update staged for the specified target."


def racadm_racreset(state):
    return "iDRAC reset initiated. This may take up to 60 seconds on real hardware. (No effect on managed state.)"


# --- OS-level commands ------------------------------------------------------


def os_nvidia_smi(state, args):
    if "-r" in args or "--gpu-reset" in args:
        return "All GPUs queried for reset. (Use `service reseat gpu <n>` first if a GPU has fallen off the bus.)"
    header = "GPU  NAME                       TEMP   STATUS"
    lines = [header]
    for g in state["gpus"]:
        status = g["status"] if g["status"] != "Unknown" else "ERR! (Xid %s)" % g["xid"]
        lines.append(f"{g['id']:<6}{g['model']:<28}{g['temp_c']:>3} C   {status}")
    return "\n".join(lines)


def os_lspci(state, args):
    lines = []
    for g in state["gpus"]:
        lines.append(f"8d:00.{g['id'].split('.')[-1]} 3D controller: NVIDIA Corporation GH100 [H100 SXM5 80GB]")
    if any("nvidia" in a.lower() for a in args) or "grep" in " ".join(args).lower():
        return "\n".join(lines)
    lines.append("00:00.0 Host bridge: Intel Corporation Device 0x1234")
    lines.append("18:00.0 Ethernet controller: Broadcom Inc. BCM57508 NetXtreme-E")
    return "\n".join(lines)


def os_dmesg(state, args):
    lines = []
    for g in state["gpus"]:
        if g["xid"]:
            lines.append(f"[12345.678901] NVRM: Xid (PCI:0000:8d:00): {g['xid']}, {g['id']} has fallen off the bus")
    for d in state["dimms"]:
        if d["status"] != "Ok":
            lines.append(f"[12300.111222] mce: [Hardware Error]: CPU src: DIMM {d['id']} uncorrectable ECC error")
    if state["networking_service"] != "active":
        lines.append("[12200.000111] bnxt_en: NIC.Embedded.1-1: watchdog timeout, resetting adapter")
    if not lines:
        lines.append("[    0.000000] Linux version 6.5.0 (XE9640 simulated kernel)")
        lines.append("[    2.113456] All hardware initialized normally.")
    return "\n".join(lines)


def os_dmidecode_memory(state):
    rows = [(f"DIMM_{d['id']}", f"{d['size_gb']} GB", d["status"]) for d in state["dimms"]]
    return table(["LOCATOR", "SIZE", "STATUS"], rows)


def os_free(state):
    total = sum(d["size_gb"] for d in state["dimms"] if d["status"] == "Ok")
    used = round(total * 0.31)
    return (
        "              total        used        free\n"
        f"Mem:      {total:>6}Gi     {used:>6}Gi     {total-used:>6}Gi"
    )


def os_lscpu(state):
    lines = []
    for c in state["cpus"]:
        lines.append(f"{c['id']}: {c['model']}  ({c['cores']} cores)  status={c['status']}")
    return "\n".join(lines)


def os_df(state):
    rows = [(f"/dev/nvme{i}n1", n["size"], n["status"]) for i, n in enumerate(state["nvme"])]
    return table(["FILESYSTEM", "SIZE", "STATUS"], rows)


def os_ip_a(state):
    lines = []
    for i, n in enumerate(state["nics"]):
        lines.append(f"{i+2}: {n['id']}: state {n['link'].upper()}  status {n['status']}")
    return "\n".join(lines)


def os_ipmitool_sel(state):
    return racadm_getsel(state, clear=False)


def os_systemctl(state, args):
    if not args:
        return "Usage: systemctl status|restart <service>"
    action = args[0]
    service = args[1] if len(args) > 1 else "networking"
    if service != "networking":
        return f"Unit {service}.service could not be found."
    if action == "status":
        return f"networking.service - {state['networking_service']}"
    if action == "restart":
        return "networking.service: restart requested"
    return "Usage: systemctl status|restart <service>"


def os_uptime(state):
    delta = datetime.now() - state["started"]
    return f"up {int(delta.total_seconds())} seconds, simulated session"


# ---------------------------------------------------------------------------
# Fault progress tracking
# ---------------------------------------------------------------------------


def advance_faults(state, normalized_cmd):
    resolved_msgs = []
    progressed_msgs = []
    for key in list(state["active_faults"].keys()):
        entry = state["active_faults"][key]
        fdef = entry["def"]
        step_idx = entry["step"]
        if step_idx >= len(fdef.steps):
            continue
        pattern, desc = fdef.steps[step_idx]
        if re.match(pattern, normalized_cmd):
            entry["step"] += 1
            if entry["step"] >= len(fdef.steps):
                fdef.resolve(state)
                del state["active_faults"][key]
                resolved_msgs.append(f"[FIXED] {fdef.title}")
            else:
                next_desc = fdef.steps[entry["step"]][1]
                progressed_msgs.append(f"[OK] {desc} - next: {next_desc}")
    return resolved_msgs, progressed_msgs


# ---------------------------------------------------------------------------
# Per-server dispatcher
# ---------------------------------------------------------------------------


class Disconnect(Exception):
    """Raised by `exit`/`disconnect` inside a server session to pop back to
    the fleet console (as opposed to `quit`, which ends the whole program)."""


def dispatch(line, state):
    raw = line.strip()
    if not raw:
        return ""
    normalized = re.sub(r"\s+", " ", raw.lower())
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    if not tokens:
        return ""
    head = tokens[0].lower()
    args = tokens[1:]

    output = None

    if head in ("exit", "disconnect", "logout"):
        raise Disconnect
    elif head == "quit":
        raise SystemExit
    elif head == "help":
        output = cmd_help(args, state)
    elif head == "clear":
        output = "\033c"
    elif head == "status":
        output = cmd_status(args, state)
    elif head in ("whoami", "where"):
        output = cmd_whoami(args, state)
    elif head == "faults":
        output = cmd_faults(args, state)
    elif head == "scenario":
        output = cmd_scenario(args, state)
    elif head == "inject":
        output = cmd_inject(args, state)
    elif head == "reset":
        output = cmd_reset(args, state)
    elif head == "hint":
        output = cmd_hint(args, state)
    elif head == "solution":
        output = cmd_solution(args, state)
    elif head == "service":
        # produce a human-readable confirmation; fault resolution handled below
        if len(args) >= 3 and args[0] in ("reseat", "replace"):
            output = f"{args[0].capitalize()}d {args[1]} {args[2]}."
        else:
            output = "Usage: service <reseat|replace> <component> <id>"
    elif head == "racadm":
        output = dispatch_racadm(args, state)
    elif head == "nvidia-smi":
        output = os_nvidia_smi(state, args)
    elif head == "lspci":
        output = os_lspci(state, args)
    elif head == "dmesg":
        output = os_dmesg(state, args)
    elif head == "dmidecode":
        if "memory" in args:
            output = os_dmidecode_memory(state)
        else:
            output = "Usage: dmidecode -t memory"
    elif head == "free":
        output = os_free(state)
    elif head == "lscpu":
        output = os_lscpu(state)
    elif head == "df":
        output = os_df(state)
    elif head == "ip" and args and args[0] == "a":
        output = os_ip_a(state)
    elif head == "ipmitool" and args[:2] == ["sel", "list"]:
        output = os_ipmitool_sel(state)
    elif head == "systemctl":
        output = os_systemctl(state, args)
    elif head == "uptime":
        output = os_uptime(state)
    else:
        output = f"bash: {head}: command not found. Type `help` for the command list."

    resolved_msgs, progressed_msgs = advance_faults(state, normalized)
    extra = "\n".join(resolved_msgs + progressed_msgs)
    if extra:
        output = (output + "\n\n" + extra) if output else extra
    return output


def dispatch_racadm(args, state):
    if not args:
        return "Usage: racadm <getsysinfo|getsel|hwinventory|swinventory|getsensorinfo|get|serveraction|jobqueue|update|racreset>"
    sub = args[0].lower()
    rest = args[1:]
    if sub == "getsysinfo":
        return racadm_getsysinfo(state)
    if sub == "getsel":
        return racadm_getsel(state, clear="-c" in rest)
    if sub == "hwinventory":
        return racadm_hwinventory(state)
    if sub == "swinventory":
        return racadm_swinventory(state)
    if sub == "getsensorinfo":
        return racadm_getsensorinfo(state)
    if sub == "get":
        return racadm_get(state, rest[0] if rest else "")
    if sub == "serveraction":
        return racadm_serveraction(state, rest[0] if rest else "")
    if sub == "jobqueue":
        return racadm_jobqueue(state, rest)
    if sub == "update":
        return racadm_update(state, rest)
    if sub == "racreset":
        return racadm_racreset(state)
    return f"ERROR: unknown racadm subcommand '{sub}'. Type `help racadm`."


# ---------------------------------------------------------------------------
# Fleet console -- browse the DC/hall/pod/rack/server hierarchy and connect
# to exactly one server at a time.
# ---------------------------------------------------------------------------


def _check_dc(dc):
    if dc is None or not (1 <= dc <= NUM_DATA_CENTERS):
        return f"Invalid data center. Valid range: 1-{NUM_DATA_CENTERS}."
    return None


def _check_dh(dc, dh):
    max_dh = HALLS_PER_DC.get(dc, 0)
    if dh is None or not (1 <= dh <= max_dh):
        return f"Invalid data hall for DC{dc}. Valid range: 1-{max_dh}."
    return None


def _check_pod(pod):
    if pod is None or not (1 <= pod <= PODS_PER_HALL):
        return f"Invalid pod. Valid range: 1-{PODS_PER_HALL}."
    return None


def _check_rack(rack):
    if rack is None or not (1 <= rack <= RACKS_PER_POD):
        return f"Invalid rack. Valid range: 1-{RACKS_PER_POD}."
    return None


def cmd_fleet_help(args):
    return f"""\
Fleet console -- browse the hierarchy, then connect to one server at a time.

  topology                                   Show the DC/hall/pod/rack/server layout
  list dc                                    List data centers
  list dh <dc>                               List data halls in a data center
  list pod <dc> <dh>                         List pods in a data hall
  list rack <dc> <dh> <pod>                  List racks in a pod
  list server <dc> <dh> <pod> <rack>         List servers in a rack + their status
  connect <dc> <dh> <pod> <rack> <srv>       Open a session on one server
  alerts                                     Fleet-wide list of active faults
  scenario fleet [n]                         Seed n random faults across the fleet (default 3)
  quit / exit                                Exit the simulator

`connect` also accepts a single token, e.g. `connect DC1-DH03-POD12-RACK05-SRV07`.
Fleet size: {NUM_DATA_CENTERS} data centers x up to {max(HALLS_PER_DC.values())} halls x \
{PODS_PER_HALL} pods x {RACKS_PER_POD} racks x {SERVERS_PER_RACK} servers \
= {total_fleet_size()} addressable {CHASSIS} servers."""


def cmd_topology(args):
    rows = []
    for dc in range(1, NUM_DATA_CENTERS + 1):
        halls = HALLS_PER_DC.get(dc, 0)
        srv_count = halls * PODS_PER_HALL * RACKS_PER_POD * SERVERS_PER_RACK
        rows.append((f"DC{dc}", halls, PODS_PER_HALL, RACKS_PER_POD, SERVERS_PER_RACK, srv_count))
    out = table(["DATA CENTER", "HALLS", "PODS/HALL", "RACKS/POD", "SRV/RACK", "TOTAL SERVERS"], rows)
    out += f"\n\nFleet total: {total_fleet_size()} servers across {NUM_DATA_CENTERS} data centers."
    out += f"\n{len(FLEET)} server(s) touched/initialized this session ({len(FLEET)} in memory)."
    return out


def cmd_list(args):
    if not args:
        return "Usage: list dc | list dh <dc> | list pod <dc> <dh> | list rack <dc> <dh> <pod> | list server <dc> <dh> <pod> <rack>"
    scope = args[0].lower()
    rest = [_to_int(a) for a in args[1:]]

    if scope == "dc":
        rows = [(f"DC{dc}", HALLS_PER_DC.get(dc, 0)) for dc in range(1, NUM_DATA_CENTERS + 1)]
        return table(["DATA CENTER", "DATA HALLS"], rows)

    if scope == "dh":
        if len(rest) < 1:
            return "Usage: list dh <dc>"
        dc = rest[0]
        err = _check_dc(dc)
        if err:
            return err
        n = HALLS_PER_DC.get(dc, 0)
        rows = [(f"DH{dh:02d}", PODS_PER_HALL) for dh in range(1, n + 1)]
        return table(["DATA HALL", "PODS"], rows)

    if scope == "pod":
        if len(rest) < 2:
            return "Usage: list pod <dc> <dh>"
        dc, dh = rest[0], rest[1]
        err = _check_dc(dc) or _check_dh(dc, dh)
        if err:
            return err
        rows = [(f"POD{p:02d}", RACKS_PER_POD) for p in range(1, PODS_PER_HALL + 1)]
        return table(["POD", "RACKS"], rows)

    if scope == "rack":
        if len(rest) < 3:
            return "Usage: list rack <dc> <dh> <pod>"
        dc, dh, pod = rest[0], rest[1], rest[2]
        err = _check_dc(dc) or _check_dh(dc, dh) or _check_pod(pod)
        if err:
            return err
        rows = [(f"RACK{r:02d}", SERVERS_PER_RACK) for r in range(1, RACKS_PER_POD + 1)]
        return table(["RACK", "SERVERS"], rows)

    if scope == "server":
        if len(rest) < 4:
            return "Usage: list server <dc> <dh> <pod> <rack>"
        dc, dh, pod, rack = rest[0], rest[1], rest[2], rest[3]
        err = _check_dc(dc) or _check_dh(dc, dh) or _check_pod(pod) or _check_rack(rack)
        if err:
            return err
        rows = []
        for srv in range(1, SERVERS_PER_RACK + 1):
            loc = Location(dc, dh, pod, rack, srv)
            lid = location_id(loc)
            if lid in FLEET:
                issues = len(FLEET[lid]["active_faults"])
                status = "OK" if issues == 0 else f"{issues} ISSUE(S)"
            else:
                status = "OK (uninitialized)"
            rows.append((lid, status))
        return table(["SERVER", "STATUS"], rows)

    return f"Unknown list scope '{scope}'. Try: dc, dh, pod, rack, server."


def cmd_alerts(args):
    rows = []
    for lid, st in sorted(FLEET.items()):
        for key, entry in st["active_faults"].items():
            fdef = entry["def"]
            rows.append((lid, key, fdef.severity, fdef.title))
    if not rows:
        return (
            "No active alerts across the fleet.\n"
            "Use `scenario fleet [n]` to seed training incidents, or `connect <dc> <dh> <pod> <rack> <srv>` "
            "to inspect a specific server directly."
        )
    out = table(["SERVER", "FAULT ID", "SEVERITY", "DESCRIPTION"], rows)
    servers_hit = len(set(r[0] for r in rows))
    out += f"\n\n{len(rows)} active alert(s) across {servers_hit} server(s). `connect <server>` to troubleshoot one."
    return out


def cmd_scenario_fleet(args):
    n = 3
    if args:
        parsed = _to_int(args[0])
        if parsed is not None:
            n = max(1, parsed)
    max_possible = total_fleet_size()
    n = min(n, max_possible)
    FLEET.clear()
    seeded = []
    attempts = 0
    while len(seeded) < n and attempts < n * 25 + 25:
        attempts += 1
        dc = random.randint(1, NUM_DATA_CENTERS)
        max_dh = HALLS_PER_DC.get(dc, 0)
        if max_dh < 1:
            continue
        dh = random.randint(1, max_dh)
        pod = random.randint(1, PODS_PER_HALL)
        rack = random.randint(1, RACKS_PER_POD)
        srv = random.randint(1, SERVERS_PER_RACK)
        loc = Location(dc, dh, pod, rack, srv)
        lid = location_id(loc)
        if lid in FLEET:
            continue
        st = get_or_create_state(loc)
        fkey = random.choice(list(FAULTS.keys()))
        _inject_fault(st, fkey)
        seeded.append(lid)
    halls_total = sum(HALLS_PER_DC.values())
    return (
        f"Seeded {len(seeded)} fault(s) across the fleet ({NUM_DATA_CENTERS} data centers, "
        f"{halls_total} halls total).\n"
        "Run `alerts` to locate them, then `connect <dc> <dh> <pod> <rack> <srv>` to start troubleshooting."
    )


def run_server_session(args):
    """Connect to exactly one server and run its shell until `exit`/`disconnect`
    (returns to the fleet console) or `quit` (raised as SystemExit, propagated
    up to end the whole program)."""
    loc = parse_location(args)
    if loc is None:
        print(
            "Usage: connect <dc> <dh> <pod> <rack> <srv>\n"
            "  e.g. connect 1 3 12 5 7\n"
            "       connect dc1 dh03 pod12 rack05 srv7\n"
            "       connect DC1-DH03-POD12-RACK05-SRV07"
        )
        return
    err = validate_location(loc)
    if err:
        print(err)
        return
    state = get_or_create_state(loc)
    lid = location_id(loc)
    prompt = f"root@{lid}:~# "
    print(f"Connected to {lid}  (Service Tag: {state['service_tag']})")
    print("Type `exit` to return to the fleet console, or `quit` to exit the simulator entirely.\n")
    while True:
        try:
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            break
        try:
            out = dispatch(line, state)
        except Disconnect:
            print(f"Disconnected from {lid}.")
            break
        if out:
            print(out)


# ---------------------------------------------------------------------------
# Main loop (fleet console)
# ---------------------------------------------------------------------------

FLEET_PROMPT = "fleet# "

FLEET_BANNER = f"""\
================================================================
 XE9640 Fleet CLI Simulator (unofficial, offline, for training)
 {NUM_DATA_CENTERS} data centers x up to {max(HALLS_PER_DC.values())} halls x {PODS_PER_HALL} pods x \
{RACKS_PER_POD} racks x {SERVERS_PER_RACK} servers = {total_fleet_size()} servers
================================================================
Type `help` for commands, `topology` for the layout, or:
    connect <dc> <dh> <pod> <rack> <srv>
to reach one server for configuration and debugging.
"""


def main():
    print(FLEET_BANNER)
    while True:
        try:
            line = input(FLEET_PROMPT)
        except (EOFError, KeyboardInterrupt):
            print()
            break
        raw = line.strip()
        if not raw:
            continue
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split()
        if not tokens:
            continue
        head = tokens[0].lower()
        args = tokens[1:]

        if head in ("exit", "quit", "logout"):
            break
        if head == "connect":
            try:
                run_server_session(args)
            except SystemExit:
                break
            continue
        if head == "help":
            print(cmd_fleet_help(args))
        elif head == "clear":
            print("\033c", end="")
        elif head == "topology":
            print(cmd_topology(args))
        elif head == "list":
            print(cmd_list(args))
        elif head == "alerts":
            print(cmd_alerts(args))
        elif head == "scenario" and args[:1] == ["fleet"]:
            print(cmd_scenario_fleet(args[1:]))
        elif head == "scenario":
            print("Usage: scenario fleet [n]  (or `connect <server>` first, then `scenario start` on that server)")
        else:
            print(f"Unknown command '{head}'. Type `help` for the fleet console command list.")


if __name__ == "__main__":
    main()

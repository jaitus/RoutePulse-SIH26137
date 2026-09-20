"""Energy accounting for a re-plan.

WHY THIS EXISTS
---------------
The 2025 systematic review of quantum and quantum-inspired optimisation in
transport (Liu, Parkinson & Best, *Smart Cities* 8(6):206) names energy
reporting as a gap: papers quote solution quality and sometimes wall-clock, and
essentially never say what the computation cost to run. A re-optimisation that
fires every time a road closes is not a one-off batch job -- it runs hundreds of
times a day per depot -- so its energy per invocation is an operational number,
not a curiosity.

WHAT IS MEASURED AND WHAT IS MODELLED
-------------------------------------
Two quantities, and the distinction is kept visible everywhere this module is
used, because conflating them would be exactly the kind of unearned claim the
rest of this project exists to avoid.

  MEASURED   process CPU time (user + system) for the work inside the block,
             from `time.process_time()`, and wall-clock elapsed.

  MEASURED   *if the machine is running on battery*: whole-system power draw in
             milliwatts, from the ACPI battery discharge rate (Windows WMI
             `root\\wmi BatteryStatus.DischargeRate`, Linux
             `/sys/class/power_supply/BAT*/power_now`). This is a real sensor
             reading. It measures the WHOLE LAPTOP -- screen, radios and all --
             so it is an upper bound on what the solver costs, and it is
             reported as such.

  MODELLED   everything else: on mains power no sensor is exposed, so energy is
             derived as `cpu_seconds x watts_per_core`, with the coefficient
             printed alongside every figure it produced. `watts_per_core`
             defaults to 15.0 W (a mobile x86 package under a sustained
             single-core load) and is overridable with ROUTEPULSE_CPU_WATTS.

A reader can therefore always tell which number came from a sensor and which
came from an assumption, and can re-derive the modelled one under their own
coefficient because the CPU-seconds are reported raw.
"""
from __future__ import annotations

import os
import platform
import subprocess
import time
from dataclasses import dataclass, asdict

# Package power attributed to one busy core when no sensor is available.
# Deliberately a module-level constant rather than a magic number inline: it is
# an assumption, and assumptions get names.
DEFAULT_WATTS_PER_CORE = float(os.environ.get("ROUTEPULSE_CPU_WATTS", "15.0"))

JOULES_PER_WH = 3600.0


@dataclass
class EnergyReading:
    """One accounted block of work."""
    label: str
    wall_s: float
    cpu_s: float
    source: str                 # "battery-sensor" | "cpu-time-model"
    watts: float                # measured system draw, or the modelled coefficient
    joules: float
    wh: float
    mwh: float
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------- power sensor

def system_power_watts() -> tuple[float | None, str]:
    """Whole-system power draw from the battery controller, if it is exposed.

    Returns (watts, note). watts is None when no sensor is readable -- which is
    the normal case on mains power, because a charged laptop on AC simply does
    not report a discharge rate. We report that plainly instead of inventing a
    number.
    """
    sysname = platform.system()

    if sysname == "Linux":
        base = "/sys/class/power_supply"
        try:
            for name in sorted(os.listdir(base)):
                if not name.startswith("BAT"):
                    continue
                p = os.path.join(base, name, "power_now")     # microwatts
                if os.path.exists(p):
                    with open(p, encoding="utf-8") as f:
                        uw = float(f.read().strip())
                    if uw > 0:
                        return uw / 1e6, f"{name} power_now"
        except OSError:
            pass
        return None, "no readable battery power sensor"

    if sysname == "Windows":
        # ACPI exposes the discharge rate in mW. On AC power the field is either
        # zero or the int32 sentinel -2147483648; both mean "not discharging",
        # not "zero watts".
        ps = ("$b = Get-WmiObject -Namespace root\\wmi -Class BatteryStatus "
              "-ErrorAction SilentlyContinue | Select-Object -First 1; "
              "if ($b) { \"$($b.DischargeRate)|$($b.PowerOnline)\" } else { 'none' }")
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=8).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None, "battery query failed"
        if "|" not in out:
            return None, "no battery present"
        raw, online = out.split("|", 1)
        try:
            mw = int(raw)
        except ValueError:
            return None, "battery rate unreadable"
        if mw <= 0:
            return None, ("on mains power, so the battery reports no discharge "
                          "rate" if online.strip().lower().startswith("t")
                          else "battery reports no discharge rate")
        return mw / 1000.0, "ACPI BatteryStatus.DischargeRate (whole system)"

    return None, f"no power sensor path for {sysname}"


# ------------------------------------------------------------------- accounting

class EnergyMeter:
    """Context manager accounting one block of work.

    Usage:
        with EnergyMeter("replan") as m:
            ...
        m.reading.mwh

    Sampling the sensor costs a subprocess launch on Windows (~80 ms), which is
    far too expensive to sit inside a 500 ms re-plan budget. So the sensor is
    probed ONCE per process and cached: `probe()` is called explicitly by
    long-running scripts, and the live server uses the cached verdict. If no
    sensor is available the meter falls back to the CPU-time model, which costs
    nothing to read.
    """

    _probed = False
    _sensor_watts: float | None = None
    _sensor_note: str = "not probed"

    def __init__(self, label: str = "block",
                 watts_per_core: float = DEFAULT_WATTS_PER_CORE) -> None:
        self.label = label
        self.watts_per_core = watts_per_core
        self.reading: EnergyReading | None = None

    @classmethod
    def probe(cls, force: bool = False) -> tuple[float | None, str]:
        """Read the power sensor once and cache the verdict."""
        if cls._probed and not force:
            return cls._sensor_watts, cls._sensor_note
        cls._sensor_watts, cls._sensor_note = system_power_watts()
        cls._probed = True
        return cls._sensor_watts, cls._sensor_note

    def __enter__(self) -> "EnergyMeter":
        self._t_wall = time.perf_counter()
        self._t_cpu = time.process_time()
        return self

    def __exit__(self, *exc) -> None:
        wall = time.perf_counter() - self._t_wall
        cpu = time.process_time() - self._t_cpu
        self.reading = self.account(self.label, wall, cpu, self.watts_per_core)

    @staticmethod
    def account(label: str, wall_s: float, cpu_s: float,
                watts_per_core: float = DEFAULT_WATTS_PER_CORE) -> EnergyReading:
        watts, note = EnergyMeter.probe()
        if watts is not None:
            # Sensor path: the draw is real but it is the WHOLE machine, and it
            # is integrated over WALL time because that is what the battery
            # actually discharged for.
            joules = watts * wall_s
            source = "battery-sensor"
            note = (f"whole-system draw from {note}; an upper bound on the "
                    f"solver's own consumption")
        else:
            # Model path: attribute one core's worth of package power to the CPU
            # time this block actually consumed.
            watts = watts_per_core
            joules = watts * cpu_s
            source = "cpu-time-model"
            note = (f"no power sensor ({note}); modelled as cpu_seconds x "
                    f"{watts_per_core:.1f} W per busy core")
        return EnergyReading(
            label=label, wall_s=round(wall_s, 6), cpu_s=round(cpu_s, 6),
            source=source, watts=round(watts, 3), joules=round(joules, 6),
            wh=round(joules / JOULES_PER_WH, 9),
            mwh=round(joules / JOULES_PER_WH * 1000.0, 6), note=note,
        )


def per_day(mwh_per_replan: float, replans_per_day: int) -> dict:
    """Scale one re-plan to a depot's daily duty cycle.

    The point of an energy figure is the operational total, not the per-call
    number. 400 re-plans/day is one every ~2 minutes over a 14-hour delivery
    window -- a busy depot during monsoon, not a worst case.
    """
    wh = mwh_per_replan * replans_per_day / 1000.0
    return {
        "replans_per_day": replans_per_day,
        "wh_per_day": round(wh, 4),
        "kwh_per_year": round(wh * 365 / 1000.0, 4),
        # CEA National Power Portal, all-India grid emission factor for 2023-24.
        "kg_co2e_per_year": round(wh * 365 / 1000.0 * 0.716, 4),
        "co2e_factor": "0.716 kg CO2e/kWh (CEA all-India grid average, 2023-24)",
    }

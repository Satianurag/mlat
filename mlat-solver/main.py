#!/usr/bin/env python3
"""Layer 4: MLAT Solver

Reads Layer 3 JSONL correlation groups from stdin, solves each group
to compute aircraft position using TDOA/TOA multilateration, and
outputs position fixes as JSONL to stdout.

Solver pipeline (from MLAT_Verified_Combined_Reference.md Part 20):

1. Route by sensor count:
   - 5+ sensors → Inamdar exact algebraic (no iteration)
   - 4 sensors + altitude → Inamdar with altitude disambiguation
   - 3 sensors + altitude → Constrained TDOA
   - 2 sensors → Skip (prediction-aided requires Layer 5 tracker)
   - 0-1 sensors → Cannot solve

2. Iterative refinement:
   - Frisch TOA formulation with scipy.optimize.least_squares
   - loss='huber' for outlier robustness
   - Atmospheric refraction velocity (Markochev model)

3. Validation:
   - Position within MAX_RANGE of all sensors
   - GDOP computation → reject if > threshold
   - Residual check

Usage:
    Full pipeline (Layer 1 → 2 → 3 → 4):
    ./data-pipe/mlat-pipe | python3 modes-decoder/main.py | \\
        python3 correlation-engine/main.py | python3 mlat-solver/main.py

    Or replay from saved Layer 3 output:
    cat correlated_data.jsonl | python3 mlat-solver/main.py

Input (JSONL from Layer 3):
    {"icao":"4CA7E8","df_type":4,"altitude_ft":36000,"squawk":null,
     "raw_msg":"2000171806A983","num_sensors":3,
     "receptions":[
       {"sensor_id":1001,"lat":50.1,"lon":-5.7,"alt":100.0,
        "timestamp_s":43200,"timestamp_ns":500000000},
       {"sensor_id":1002,"lat":50.2,"lon":-5.6,"alt":105.0,
        "timestamp_s":43200,"timestamp_ns":500000050},
       {"sensor_id":1003,"lat":50.3,"lon":-5.5,"alt":110.0,
        "timestamp_s":43200,"timestamp_ns":500000100}
     ]}

Output (JSONL):
    {"icao":"4CA7E8","lat":50.15,"lon":-5.65,"alt_ft":36000,
     "residual_m":145.23,"gdop":3.45,"num_sensors":3,
     "solve_method":"constrained_3sensor",
     "timestamp_s":43200,"timestamp_ns":500000000,
     "df_type":4,"squawk":null,"raw_msg":"2000171806A983",
     "t0_s":43200.000499833}

All logs go to stderr to keep stdout as a clean data stream.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from geo import lla_to_ecef
C_VACUUM = 299792458.0

from solver import solve_group

STATS_INTERVAL = 30  # seconds


# Stats counters
class Stats:
    """Solver statistics counters."""

    def __init__(self) -> None:
        self.groups_received = 0
        self.groups_solved = 0
        self.groups_failed = 0
        self.groups_skipped_sensors = 0
        self.parse_errors = 0
        # Per-method counters
        self.method_counts: dict[str, int] = {}
        # Residual tracking
        self.residuals: list[float] = []
        # Clock sync validation (rolling 100 samples per sensor)
        self.sensor_residuals_m: dict[int, list[float]] = {}

    def record_solve(self, method: str, residual_m: float) -> None:
        self.groups_solved += 1
        self.method_counts[method] = self.method_counts.get(method, 0) + 1
        self.residuals.append(residual_m)

    def record_sensor_residual(self, sensor_id: int, residual_m: float) -> None:
        if sensor_id not in self.sensor_residuals_m:
            self.sensor_residuals_m[sensor_id] = []
        self.sensor_residuals_m[sensor_id].append(residual_m)
        if len(self.sensor_residuals_m[sensor_id]) > 100:
            self.sensor_residuals_m[sensor_id].pop(0)

    def to_dict(self) -> dict:
        result: dict = {
            "groups_received": self.groups_received,
            "groups_solved": self.groups_solved,
            "groups_failed": self.groups_failed,
            "groups_skipped_sensors": self.groups_skipped_sensors,
            "parse_errors": self.parse_errors,
            "solve_rate": (
                f"{self.groups_solved / self.groups_received * 100:.1f}%"
                if self.groups_received > 0
                else "0.0%"
            ),
            "methods": self.method_counts,
        }
        if self.residuals:
            sorted_r = sorted(self.residuals)
            n = len(sorted_r)
            result["median_residual_m"] = round(sorted_r[n // 2], 2)
            result["p95_residual_m"] = round(
                sorted_r[int(n * 0.95)] if n > 20 else sorted_r[-1], 2
            )
        if self.sensor_residuals_m:
            # Report the mean residual bias per sensor as a clock sync indicator
            result["clock_sync_bias_m"] = {
                str(sid): round(sum(res) / len(res), 2)
                for sid, res in self.sensor_residuals_m.items()
                if len(res) >= 10
            }
        return result


def log(msg: str) -> None:
    """Log to stderr (keeps stdout clean for data)."""
    print(msg, file=sys.stderr, flush=True)


def main() -> None:
    log("=== MLAT Solver (Layer 4) ===")
    log("Reading JSONL correlation groups from stdin")
    log("Solver: Frisch TOA formulation with Inamdar algebraic initialization")
    log("Loss: Huber (outlier-robust)")
    log("Atmospheric model: Markochev refraction correction")
    log(f"Stats logged every {STATS_INTERVAL}s to stderr")

    stats = Stats()
    last_stats_time = time.monotonic()

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            # Parse input JSON
            try:
                group = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                stats.parse_errors += 1
                continue

            stats.groups_received += 1

            # Check minimum sensor count before attempting solve
            receptions = group.get("receptions", [])
            n_sensors = len(receptions)
            if n_sensors < 2:
                stats.groups_skipped_sensors += 1
                continue

            # Pass 2-sensor groups downstream for track-builder prediction-aided solve
            if n_sensors == 2 and group.get("altitude_ft") is not None:
                # We need altitude to constrain the 2-sensor system
                output = {"unsolved_group": group}
                print(json.dumps(output, separators=(",", ":")), flush=True)
                stats.groups_skipped_sensors += 1
                continue

            # Solve the correlation group (>= 3 sensors)
            result = solve_group(group)

            if result is not None:
                # Output solved position fix
                output = result.to_dict()
                print(json.dumps(output, separators=(",", ":")), flush=True)
                stats.record_solve(result.solve_method, result.residual_m)

                # Back-compute individual sensor residuals for Clock Sync validation
                aircraft_ecef = lla_to_ecef(result.lat, result.lon, result.alt_ft * 0.3048)
                for rec in group.get("receptions", []):
                    s_id = rec.get("sensor_id")
                    if s_id is not None:
                        s_pos = lla_to_ecef(rec["lat"], rec["lon"], rec["alt"])
                        dt_s = rec["timestamp_s"] - result.timestamp_s
                        dt_ns = rec["timestamp_ns"] - result.timestamp_ns
                        arr_time = dt_s + dt_ns * 1e-9
                        
                        dist = ((aircraft_ecef[0] - s_pos[0])**2 + 
                                (aircraft_ecef[1] - s_pos[1])**2 + 
                                (aircraft_ecef[2] - s_pos[2])**2)**0.5
                                
                        expected_arr = dist / C_VACUUM
                        actual_arr = arr_time - result.t0_s
                        residual_m = (actual_arr - expected_arr) * C_VACUUM
                        stats.record_sensor_residual(s_id, residual_m)
            else:
                stats.groups_failed += 1

            # Periodic stats
            now = time.monotonic()
            if now - last_stats_time >= STATS_INTERVAL:
                log(f"[stats] {json.dumps(stats.to_dict())}")
                last_stats_time = now

    except KeyboardInterrupt:
        log("Interrupted by user")
    except BrokenPipeError:
        pass
    finally:
        log("Shutting down. Final stats:")
        log(f"[stats] {json.dumps(stats.to_dict(), indent=2)}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run a live MVP3 long scenario while sampling process and disk resources."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def descendants(root: int, rows: dict[int, dict[str, str]]) -> list[int]:
    children: dict[int, list[int]] = {}
    for pid, row in rows.items():
        try:
            parent = int(row["ppid"])
        except (KeyError, ValueError):
            continue
        children.setdefault(parent, []).append(pid)
    result: list[int] = []
    stack = [root]
    while stack:
        pid = stack.pop()
        if pid in result:
            continue
        result.append(pid)
        stack.extend(children.get(pid, ()))
    return result


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: profile_live_long_run.py OUTPUT_DIRECTORY")
    output = Path(sys.argv[1]).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise SystemExit(f"Output directory already exists; choose a fresh path: {output}")
    profile = Path("results/mvp3/profiling")
    stem = output.name
    sample_path = profile / f"{stem}.resources.jsonl"
    log_path = profile / f"{stem}.log"
    summary_path = profile / f"{stem}.summary.json"

    env = os.environ.copy()
    env["RIOSE_ZEPHYR_ELF"] = "/tmp/riose-mvp3-renode-build/zephyr/zephyr.elf"
    if not Path(env["RIOSE_ZEPHYR_ELF"]).is_file():
        raise SystemExit(f"Zephyr ELF does not exist: {env['RIOSE_ZEPHYR_ELF']}")
    command = [
        "uv", "run", "--project", ".", "riose", "mvp3", "run", "long-simulation",
        "--headless", "--live-lockstep", "--output", str(output),
    ]
    start = time.monotonic()
    wall_started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    peak_rss_kib = 0
    peak_cpu_percent = 0.0
    min_free_disk_bytes = shutil.disk_usage(output.parent).free
    with log_path.open("w", encoding="utf-8") as log, sample_path.open("w", encoding="utf-8") as samples:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)
        next_log = 0.0
        while process.poll() is None:
            now = time.monotonic()
            ps = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,rss=,pcpu=,comm=,args="],
                text=True, capture_output=True, check=True,
            )
            rows: dict[int, dict[str, str]] = {}
            for line in ps.stdout.splitlines():
                fields = line.strip().split(None, 5)
                if len(fields) != 6:
                    continue
                try:
                    pid = int(fields[0])
                except ValueError:
                    continue
                rows[pid] = {
                    "ppid": fields[1], "rss_kib": fields[2],
                    "cpu_percent": fields[3], "command": fields[4],
                    "args": fields[5],
                }
            pids = descendants(process.pid, rows)
            members = [rows[pid] | {"pid": pid} for pid in pids if pid in rows]
            tree_rss = sum(int(row["rss_kib"]) for row in members)
            tree_cpu = sum(float(row["cpu_percent"]) for row in members)
            peak_rss_kib = max(peak_rss_kib, tree_rss)
            peak_cpu_percent = max(peak_cpu_percent, tree_cpu)
            free_disk_bytes = shutil.disk_usage(output.parent).free
            min_free_disk_bytes = min(min_free_disk_bytes, free_disk_bytes)
            elapsed = now - start
            samples.write(json.dumps({
                "wall_elapsed_s": round(elapsed, 3),
                "tracked_processes": members,
                "tree_rss_kib": tree_rss,
                "tree_cpu_percent": round(tree_cpu, 1),
                "filesystem_free_bytes": free_disk_bytes,
            }, sort_keys=True) + "\n")
            samples.flush()
            if elapsed >= next_log:
                print(
                    f"wall={elapsed:.0f}s tree_rss={tree_rss / 1024:.1f}MiB "
                    f"tree_cpu={tree_cpu:.1f}% free_disk={free_disk_bytes / (1024**3):.1f}GiB",
                    flush=True,
                )
                next_log = elapsed + 60.0
            time.sleep(max(0.0, 10.0 - (time.monotonic() - now)))
        returncode = process.wait()
    runtime = time.monotonic() - start
    summary = {
        "command": command,
        "returncode": returncode,
        "wall_started_utc": wall_started,
        "wall_runtime_s": runtime,
        "peak_sampled_process_tree_rss_kib": peak_rss_kib,
        "maximum_sampled_process_tree_cpu_percent": peak_cpu_percent,
        "minimum_filesystem_free_bytes": min_free_disk_bytes,
        "log": str(log_path.resolve()),
        "samples": str(sample_path.resolve()),
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())

import json
import csv
import pathlib
import re
import statistics
import subprocess
import sys


def scan(path: str, threshold: float) -> list[float]:
    filt = f"select='gt(scene,{threshold})',showinfo"
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vf", filt,
         "-an", "-f", "null", "NUL"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return [float(x) for x in re.findall(r"pts_time:([0-9.]+)", proc.stderr)]


def summarize(times: list[float], duration: float) -> dict:
    boundaries = [0.0, *times, duration]
    intervals = [round(b - a, 3) for a, b in zip(boundaries, boundaries[1:])]
    return {
        "detected_changes": len(times),
        "visual_segments": len(times) + 1,
        "timestamps_seconds": times,
        "intervals_seconds": intervals,
        "mean_interval": round(statistics.mean(intervals), 3),
        "median_interval": round(statistics.median(intervals), 3),
        "min_interval": min(intervals),
        "max_interval": max(intervals),
    }


if __name__ == "__main__":
    path = sys.argv[1]
    duration = float(sys.argv[2])
    result = {str(t): summarize(scan(path, t), duration) for t in (0.12, 0.20, 0.30)}
    if len(sys.argv) >= 4:
        stem = pathlib.Path(sys.argv[3])
        stem.parent.mkdir(parents=True, exist_ok=True)
        with stem.with_suffix(".json").open("w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        primary = result["0.12"]
        boundaries = [0.0, *primary["timestamps_seconds"], duration]
        with stem.with_suffix(".csv").open("w", encoding="utf-8-sig", newline="") as f:
            out = csv.writer(f)
            out.writerow(["segment", "starts_at_seconds", "starts_at_timecode", "duration_seconds", "starts_at_frame"])
            for number, (start, end) in enumerate(zip(boundaries, boundaries[1:]), 1):
                hours, rem = divmod(start, 3600)
                minutes, seconds = divmod(rem, 60)
                timecode = f"{int(hours):02d}:{int(minutes):02d}:{seconds:05.2f}"
                out.writerow([number, f"{start:.2f}", timecode, f"{end-start:.2f}", round(start * 25)])
        print(json.dumps({"csv": str(stem.with_suffix('.csv')), "json": str(stem.with_suffix('.json')), "primary": primary}, ensure_ascii=False))
    else:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))

#!/usr/bin/env python3
"""Free disk space held by finished DeLM runs without losing recoverable work.

Build and dependency directories (target/, node_modules/, dist/, ...) that the
workers produced are removed from the worker copies and from the recovery
bundle. Source changes stay recoverable: the bundle's complete.json is rewritten
without the pruned paths, and only blobs no remaining entry references are
deleted. A run whose runtime is still alive is never touched.

Usage:
  prune_runs.py                 prune build directories in every finished run
  prune_runs.py --dry-run       report what would be freed
  prune_runs.py --remove RUN_ID delete a whole run after its work was recovered
  prune_runs.py --quiet         one summary line, for git hooks
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RUNS = Path.home() / "Library" / "Application Support" / "DeLM" / "runs"
PRUNED_SEGMENTS = {"target", "node_modules", "dist", ".vitest", ".vite-plus", ".build", ".cache"}
BLOB_NAME_LENGTH = 64


def pruned(path: str) -> bool:
    return any(segment in PRUNED_SEGMENTS for segment in path.split("/"))


def live_run_ids() -> set:
    output = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True).stdout.splitlines()
    runtime_lines = [line for line in output
                     if line.split() and line.split()[0].rsplit("/", 1)[-1] == "delm" and " view " not in line]
    live = set()
    for run in RUNS.iterdir():
        if not run.is_dir():
            continue
        state = run / "claude.json"
        if state.is_file():
            try:
                finished = json.loads(state.read_text()).get("finished") is True
            except (OSError, ValueError):
                finished = False
            if not finished:
                live.add(run.name)
        elif not (run / "workspace" / "recovery" / "complete.json").is_file():
            live.add(run.name)
        if any(run.name in line for line in runtime_lines):
            live.add(run.name)
    return live


def tree_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def prune_worker_copies(workspace: Path, dry_run: bool) -> int:
    freed = 0
    for worker in workspace.glob("worker-*"):
        for root, dirs, _ in os.walk(worker):
            for name in list(dirs):
                if name in PRUNED_SEGMENTS:
                    target = Path(root) / name
                    freed += tree_size(target)
                    if not dry_run:
                        shutil.rmtree(target, ignore_errors=True)
                    dirs.remove(name)
    return freed


def entry_digests(change) -> set:
    digests = set()
    for version in change:
        if isinstance(version, dict) and version.get("kind") == "file" and version.get("sha256"):
            digests.add(version["sha256"])
    return digests


def prune_recovery(recovery: Path, dry_run: bool) -> tuple:
    manifest_path = recovery / "complete.json"
    if not manifest_path.is_file():
        return 0, 0
    manifest = json.loads(manifest_path.read_text())
    kept_digests, dropped = set(), 0
    for worker in manifest.get("workers", []):
        changes = worker.get("changes", {})
        kept = {path: change for path, change in changes.items() if not pruned(path)}
        dropped += len(changes) - len(kept)
        worker["changes"] = kept
        for change in kept.values():
            kept_digests |= entry_digests(change)
    freed = 0
    for blob in recovery.iterdir():
        if len(blob.name) == BLOB_NAME_LENGTH and blob.name not in kept_digests:
            freed += blob.stat().st_size
            if not dry_run:
                blob.unlink()
    if dropped and not dry_run:
        with tempfile.NamedTemporaryFile("w", dir=recovery, delete=False) as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(handle.name, manifest_path)
    return freed, dropped


def human(size: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} GiB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--remove", metavar="RUN_ID")
    args = parser.parse_args()
    if not RUNS.is_dir():
        if not args.quiet:
            print("No DeLM runs on this computer.")
        return 0
    live = live_run_ids()
    if args.remove:
        run = RUNS / args.remove
        if not run.is_dir():
            print(f"No run {args.remove}", file=sys.stderr)
            return 1
        if args.remove in live:
            print(f"Run {args.remove} still has a live runtime; stop it first", file=sys.stderr)
            return 1
        size = tree_size(run)
        if not args.dry_run:
            shutil.rmtree(run)
        print(f"{'Would remove' if args.dry_run else 'Removed'} run {args.remove}: {human(size)}")
        return 0
    total_freed, pruned_runs, skipped = 0, 0, []
    for run in sorted(RUNS.iterdir()):
        if not run.is_dir():
            continue
        if run.name in live:
            skipped.append(run.name)
            continue
        workspace = run / "workspace"
        freed = prune_worker_copies(workspace, args.dry_run) if workspace.is_dir() else 0
        recovered, dropped = prune_recovery(workspace / "recovery", args.dry_run) if workspace.is_dir() else (0, 0)
        if freed or recovered:
            pruned_runs += 1
            if not args.quiet:
                verb = "would free" if args.dry_run else "freed"
                print(f"{run.name}: {verb} {human(freed + recovered)} ({dropped} build entries dropped from recovery)")
        total_freed += freed + recovered
    verb = "would free" if args.dry_run else "freed"
    note = f", skipped live: {', '.join(skipped)}" if skipped else ""
    print(f"DeLM prune: {verb} {human(total_freed)} in {pruned_runs} run(s){note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

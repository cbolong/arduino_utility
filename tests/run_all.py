"""Run every test module in tests/ as a separate process; report a summary.

Separate processes keep Qt state isolated (one QApplication per module) and
mean a crash in one module can't poison the others.

Usage, from the repo root:
    python tests/run_all.py            # run everything
    python tests/run_all.py smoke      # run only modules whose name contains 'smoke'
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    pattern = sys.argv[1] if len(sys.argv) > 1 else ""
    mods = sorted(
        f for f in os.listdir(HERE)
        if f.startswith("test_") and f.endswith(".py") and pattern in f
    )
    if not mods:
        print(f"no test modules match {pattern!r}")
        return 1

    results = []
    for m in mods:
        t0 = time.time()
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, m)],
            capture_output=True, text=True, timeout=300,
        )
        dt = time.time() - t0
        ok = proc.returncode == 0
        results.append((m, ok, dt))
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {m}  ({dt:.1f}s)")
        if not ok:
            print("---- stdout ----")
            print(proc.stdout[-3000:])
            print("---- stderr ----")
            print(proc.stderr[-3000:])

    failed = [m for m, ok, _ in results if not ok]
    print()
    print(f"{len(results) - len(failed)}/{len(results)} modules passed")
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

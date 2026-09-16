"""One command to verify the whole toolbox surface: logic suite + live served bytes.

Run as: python3 tests/verify_all.py
Exits non-zero if anything fails, so it is usable as a pre-flight before a browser session.
"""
import subprocess
import sys

HERE = __import__("pathlib").Path(__file__).resolve().parent
rc = 0
for label, argv in (("offline logic suite", [sys.executable, str(HERE / "smoke_toolbox.py")]),
                    ("live served bytes", [sys.executable, str(HERE / "check_serve_live.py")]),
                    ("controller inputs", ["python3", "/tmp/verify_controller.py"]),
                    ("new assets live", ["python3", "/tmp/check_assets_live.py"])):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        print(f"  SKIP  {label}: {e}")
        continue
    tail = (r.stdout or r.stderr).strip().splitlines()
    print(f"=== {label}: rc={r.returncode}")
    for line in tail[-14:]:
        print("   ", line)
    rc |= r.returncode
print("\n" + ("ALL GREEN" if rc == 0 else "FAILURES PRESENT"))
raise SystemExit(rc)

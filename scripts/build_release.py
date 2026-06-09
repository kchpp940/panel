from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

GREEN = "\033[0;32m"
RED = "\033[0;31m"
RESET = "\033[0m"


def clean_dist() -> None:
    dist_dir = ROOT / "dist"
    if dist_dir.exists():
        print(f"{GREEN}[PANEL]{RESET} Cleaning dist/", flush=True)
        shutil.rmtree(dist_dir)
    else:
        print(f"{GREEN}[PANEL]{RESET} dist/ not present, nothing to clean", flush=True)


def build() -> None:
    print(f"{GREEN}[PANEL]{RESET} Building wheel and sdist", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "build", "."],
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        print(f"{RED}[PANEL]{RESET} Build FAILED", flush=True)
        sys.exit(1)
    print(f"{GREEN}[PANEL]{RESET} Build finished", flush=True)


def verify_release() -> None:
    print(f"{GREEN}[PANEL]{RESET} Running release artifact verification", flush=True)
    verify_script = ROOT / "scripts" / "verify_frontend_artifacts.py"
    result = subprocess.run(
        [sys.executable, str(verify_script), "--release"],
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        print(f"{RED}[PANEL]{RESET} Release artifact verification FAILED", flush=True)
        sys.exit(1)
    print(f"{GREEN}[PANEL]{RESET} Release artifact verification passed", flush=True)


def main() -> int:
    print("=" * 70)
    print("Panel release build pipeline: clean → build → verify")
    print("=" * 70)
    clean_dist()
    build()
    verify_release()
    print("\n" + "=" * 70)
    print(f"{GREEN}Release build and verification completed successfully{RESET}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())

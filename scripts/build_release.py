from __future__ import annotations

import json
import os
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


def locate_artifacts() -> dict[str, str]:
    dist_dir = ROOT / "dist"

    wheels = sorted(dist_dir.glob("*.whl"))
    if len(wheels) != 1:
        names = ", ".join(w.name for w in wheels) if wheels else "(none)"
        print(
            f"{RED}[PANEL]{RESET} Expected exactly 1 wheel in dist/, "
            f"found {len(wheels)}: {names}",
            flush=True,
        )
        sys.exit(1)

    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(sdists) != 1:
        names = ", ".join(s.name for s in sdists) if sdists else "(none)"
        print(
            f"{RED}[PANEL]{RESET} Expected exactly 1 sdist in dist/, "
            f"found {len(sdists)}: {names}",
            flush=True,
        )
        sys.exit(1)

    return {
        "wheel": str(wheels[0].relative_to(ROOT)),
        "wheel_name": wheels[0].name,
        "sdist": str(sdists[0].relative_to(ROOT)),
        "sdist_name": sdists[0].name,
    }


def write_manifest(artifacts: dict[str, str]) -> Path:
    manifest_path = ROOT / "dist" / "build-manifest.json"
    manifest_path.write_text(json.dumps(artifacts, indent=2) + "\n")
    print(f"{GREEN}[PANEL]{RESET} Wrote manifest: {manifest_path.relative_to(ROOT)}", flush=True)
    return manifest_path


def emit_github_output(artifacts: dict[str, str]) -> None:
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if not gh_output:
        return
    with open(gh_output, "a") as f:
        for key, value in artifacts.items():
            f.write(f"{key}={value}\n")
    print(f"{GREEN}[PANEL]{RESET} Emitted GITHUB_OUTPUT for wheel/sdist paths", flush=True)


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
    print("Panel release build pipeline: clean → build → locate → manifest → verify")
    print("=" * 70)
    clean_dist()
    build()
    artifacts = locate_artifacts()
    write_manifest(artifacts)
    emit_github_output(artifacts)
    verify_release()
    print("\n" + "=" * 70)
    print(f"{GREEN}Release build and verification completed successfully{RESET}")
    print(f"  wheel : {artifacts['wheel']}")
    print(f"  sdist : {artifacts['sdist']}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())

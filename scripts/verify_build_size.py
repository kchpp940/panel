import argparse
import sys

from pathlib import Path

EXPECTED_SIZES_MB = {
    "conda": 25,
    "npm": 25,
    "sdist": 31,
    "whl": 31,
}

GLOB_PATH = {
    "conda": "dist/*.conda",
    "npm": "panel/*.tgz",
    "sdist": "dist/*.tar.gz",
    "whl": "dist/*.whl",
}

PATH = Path(__file__).parents[1]


def main(build_type: str, explicit_path: str | None = None) -> None:
    if explicit_path:
        file = Path(explicit_path)
        if not file.is_absolute():
            file = PATH / file
        if not file.exists():
            raise AssertionError(f"File not found at explicit path: {file}")
        files = [file]
    else:
        files = list(PATH.rglob(GLOB_PATH[build_type]))
        assert len(files) == 1, f"Expected one {build_type} file, got {len(files)}"

    size = files[0].stat().st_size / 1024**2
    assert size < EXPECTED_SIZES_MB[build_type], f"{build_type} file is too large: {size:.2f} MB"
    print(f"{build_type} file size: {size:.2f} MB")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify build artifact sizes.")
    parser.add_argument(
        "build_type",
        choices=list(EXPECTED_SIZES_MB.keys()),
        help="Type of build artifact to check",
    )
    parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="Explicit path to the artifact file (bypasses glob search)",
    )
    args = parser.parse_args()
    main(args.build_type, args.path)

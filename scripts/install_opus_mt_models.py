"""Convert the official OPUS-MT en->zh archive for local inference."""

from __future__ import annotations

import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = PROJECT_ROOT / "backend" / "translation_models"
DOWNLOAD_DIR = MODELS_DIR / ".downloads"
CONVERTER = PROJECT_ROOT / ".venv" / "Scripts" / "ct2-opus-mt-converter.exe"
MODEL_ARCHIVES = {
    "opus-mt-en-zh": "https://object.pouta.csc.fi/Tatoeba-MT-models/eng-zho/opus-2020-07-17.zip",
}


def is_converted(model_dir: Path) -> bool:
    return all(
        (model_dir / filename).exists()
        for filename in ("model.bin", "source.spm", "target.spm")
    )


def find_extracted_model(root: Path) -> Path:
    if (root / "decoder.yml").exists():
        return root
    matches = list(root.rglob("decoder.yml"))
    if not matches:
        raise RuntimeError(f"decoder.yml was not found after extracting {root}")
    return matches[0].parent


def extract_archive(archive: Path, destination: Path) -> Path:
    if not list(destination.rglob("decoder.yml")):
        destination.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as package:
            package.extractall(destination)
    return find_extracted_model(destination)


def main() -> None:
    if not CONVERTER.exists():
        raise RuntimeError(f"CTranslate2 converter not found: {CONVERTER}")

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    for model_name, url in MODEL_ARCHIVES.items():
        output_dir = MODELS_DIR / model_name
        if is_converted(output_dir):
            print(f"Already converted: {model_name}")
            continue

        archive = DOWNLOAD_DIR / f"{model_name}.zip"
        if not archive.exists() and model_name == "opus-mt-en-zh":
            legacy_archive = DOWNLOAD_DIR / "opus-2020-07-17.zip"
            if legacy_archive.exists():
                archive = legacy_archive
        if not archive.exists():
            print(f"Downloading {model_name}...")
            urllib.request.urlretrieve(url, archive)
        else:
            print(f"Using downloaded archive: {archive}")

        raw_dir = DOWNLOAD_DIR / model_name
        model_dir = extract_archive(archive, raw_dir)
        print(f"Converting {model_name} to CTranslate2 int8...")
        subprocess.run(
            [
                str(CONVERTER),
                "--model_dir",
                str(model_dir),
                "--output_dir",
                str(output_dir),
                "--quantization",
                "int8",
                "--force",
            ],
            check=True,
        )

        for filename in ("source.spm", "target.spm"):
            shutil.copy2(model_dir / filename, output_dir / filename)
        print(f"Installed local OPUS-MT model: {model_name}")

    print("Local OPUS-MT translation is ready. Restart the meeting assistant.")


if __name__ == "__main__":
    main()

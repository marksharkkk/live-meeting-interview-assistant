"""Optional offline subtitle translation through local OPUS-MT models."""

from __future__ import annotations

import logging
import os
from threading import Lock
from pathlib import Path


logger = logging.getLogger(__name__)
PACKAGE_MODELS_DIR = Path(__file__).resolve().parent / "translation_models"
USER_MODELS_DIR = Path(os.environ.get("MEETING_ASSISTANT_DATA_DIR") or Path(__file__).resolve().parent) / "translation_models"

try:
    import ctranslate2
except ImportError:  # Optional dependency; the normal LLM path must still work.
    ctranslate2 = None

try:
    import sentencepiece as spm
except ImportError:  # Optional dependency; the normal LLM path must still work.
    spm = None


def detect_translation_direction(text: str) -> tuple[str, str]:
    """Return the OPUS-MT source and target codes for a subtitle string."""

    cjk_count = sum("\u4e00" <= character <= "\u9fff" for character in text)
    latin_count = sum(character.isascii() and character.isalpha() for character in text)
    if cjk_count and cjk_count >= latin_count:
        return "zh", "en"
    return "en", "zh"


class LocalTranslationService:
    """Best-effort local OPUS-MT translation service.

    The downloaded models are converted to CTranslate2 format once during
    setup. Missing packages, models, or runtime errors return ``None`` so the
    caller can use the configured online provider instead of breaking live
    subtitles.
    """

    _MODEL_NAMES = {
        ("zh", "en"): "opus-mt-zh-en",
        ("en", "zh"): "opus-mt-en-zh",
    }
    _TARGET_PREFIXES = {("en", "zh"): ">>cmn_Hans<<"}

    def __init__(self):
        self._models: dict[tuple[str, str], tuple[object, object, object]] = {}
        self._failed_models: set[tuple[str, str]] = set()
        self._load_lock = Lock()

    @property
    def installed(self) -> bool:
        return ctranslate2 is not None and spm is not None and all(
            self._model_dir(source, target).joinpath("model.bin").exists()
            for source, target in self._MODEL_NAMES
        )

    @classmethod
    def _model_dir(cls, source: str, target: str) -> Path:
        name = cls._MODEL_NAMES[(source, target)]
        user_model = USER_MODELS_DIR / name
        if all((user_model / filename).is_file() for filename in ("model.bin", "source.spm", "target.spm")):
            return user_model
        return PACKAGE_MODELS_DIR / name

    def _load_model(self, source: str, target: str):
        key = (source, target)
        if key in self._models:
            return self._models[key]
        if key in self._failed_models or ctranslate2 is None or spm is None:
            return None

        model_dir = self._model_dir(source, target)
        source_spm = model_dir / "source.spm"
        target_spm = model_dir / "target.spm"
        if not (model_dir / "model.bin").exists() or not source_spm.exists() or not target_spm.exists():
            return None

        with self._load_lock:
            if key in self._models:
                return self._models[key]
            try:
                try:
                    translator = ctranslate2.Translator(
                        str(model_dir), device="cpu", compute_type="int8"
                    )
                except Exception:
                    translator = ctranslate2.Translator(str(model_dir), device="cpu")
                source_processor = spm.SentencePieceProcessor(model_proto=source_spm.read_bytes())
                target_processor = spm.SentencePieceProcessor(model_proto=target_spm.read_bytes())
            except Exception:
                self._failed_models.add(key)
                logger.exception("Could not load local OPUS-MT model: %s", model_dir)
                return None

            self._models[key] = (translator, source_processor, target_processor)
            logger.info("Loaded local OPUS-MT model: %s", model_dir.name)
            return self._models[key]

    def translate(self, text: str) -> str | None:
        if not text.strip():
            return None

        source, target = detect_translation_direction(text)
        model = self._load_model(source, target)
        if model is None:
            return None

        translator, source_processor, target_processor = model
        try:
            source_tokens = source_processor.encode(text, out_type=str)
            target_prefix = self._TARGET_PREFIXES.get((source, target))
            if target_prefix:
                source_tokens.insert(0, target_prefix)
            result = translator.translate_batch(
                [source_tokens],
                beam_size=1,
                max_decoding_length=256,
            )[0]
            target_tokens = [
                token
                for token in result.hypotheses[0]
                if token not in {"</s>", "<pad>", target_prefix}
            ]
            translated = target_processor.decode(target_tokens)
        except Exception:
            logger.debug(
                "Local OPUS-MT translation unavailable for %s->%s; falling back",
                source,
                target,
                exc_info=True,
            )
            return None
        return translated.strip() or None

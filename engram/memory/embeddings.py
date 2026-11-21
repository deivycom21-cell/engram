"""Embedding providers (interchangeable).

Which components are *neural*?
* ``OnnxEmbeddingProvider`` — yes: a transformer (multilingual-e5-small, int8-quantised)
  run locally with ONNX Runtime on CPU. Default when the model has been downloaded.
* ``OllamaEmbeddingProvider`` — yes: any embedding model served by a *local* Ollama.
* ``HashEmbeddingProvider`` — **no**: deterministic feature hashing of words and
  character trigrams. A lexical fallback so the system works with zero downloads;
  similarities it produces are labelled "lexical", never "semantic".

Every provider exposes calibrated thresholds because cosine scales differ wildly
between models (e5 scores cluster in 0.7–1.0, hashing scores are much lower).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

log = logging.getLogger("engram.embeddings")


@dataclass(frozen=True)
class Thresholds:
    association: float   # cosine above which two engrams are proposed as related
    duplicate: float     # cosine above which two engrams are proposed as near-duplicates
    topic: float         # cosine above which two engrams are considered "same topic" (contradiction/evolution checks)


class EmbeddingProvider(Protocol):
    name: str
    dim: int
    neural: bool
    thresholds: Thresholds

    def embed(self, texts: list[str], mode: str = "passage") -> np.ndarray: ...


def _normalize(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (m / norms).astype(np.float32)


# --------------------------------------------------------------------------- hash
_WORD = re.compile(r"\w+", re.UNICODE)


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


class HashEmbeddingProvider:
    """Non-neural lexical vectors via signed feature hashing (deterministic, zero downloads)."""

    neural = False

    def __init__(self, dim: int = 384):
        self.dim = dim
        self.name = f"hash-v1-{dim}"
        self.thresholds = Thresholds(association=0.45, duplicate=0.92, topic=0.35)

    def _features(self, text: str):
        text = _strip_accents(text.lower())
        words = [w for w in _WORD.findall(text) if len(w) > 2]
        for w in words:
            yield "w:" + w[:7], 1.0               # crude stemming by prefix
            padded = f"#{w}#"
            for i in range(len(padded) - 2):
                yield "c:" + padded[i : i + 3], 0.35
        for a, b in zip(words, words[1:]):
            yield f"b:{a[:6]}_{b[:6]}", 0.6

    def embed(self, texts: list[str], mode: str = "passage") -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for feat, weight in self._features(text):
                h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "little")
                idx = h % self.dim
                sign = 1.0 if (h >> 63) & 1 else -1.0
                out[row, idx] += sign * weight
        return _normalize(out)


# --------------------------------------------------------------------------- centering
# Transformer sentence embeddings are anisotropic: every vector shares a large common
# component, so even unrelated texts score cos≈0.8 with e5. Subtracting the mean of a
# fixed, topic-diverse *background* set (independent of the user's corpus) and
# re-normalising spreads scores out (related ≈0.5–0.7, unrelated ≈0–0.3).
# See docs/DECISIONS.md D-07 for the measurements.
BACKGROUND_SENTENCES = [
    "El tiempo mañana será soleado con temperaturas suaves.", "La receta lleva harina, huevos y azúcar.",
    "The football match ended in a draw after extra time.", "Compré un billete de tren para visitar a mi familia.",
    "The stock market fell sharply on Monday.", "Mi gato duerme casi todo el día en el sofá.",
    "Photosynthesis converts light into chemical energy.", "La reunión de vecinos se celebra el jueves por la tarde.",
    "I need to renew my passport before the trip.", "El museo abre de martes a domingo.",
    "The new phone has a larger battery and a better camera.", "Hay que regar las plantas dos veces por semana.",
    "Quantum computers use qubits instead of bits.", "La novela cuenta la historia de una familia en el siglo XIX.",
    "We should refactor the login module and add tests.", "El presupuesto del trimestre supera lo previsto.",
    "The patient was prescribed antibiotics for ten days.", "Aprendí a tocar la guitarra el verano pasado.",
    "The database migration failed because of a missing index.", "Los precios del alquiler subieron un diez por ciento.",
    "She painted the kitchen walls light blue.", "El servidor web responde con un error 500.",
    "Remember to buy milk and bread on the way home.", "La conferencia trata sobre cambio climático y energía.",
    "The team decided to postpone the product launch.", "Mi abuela cocina el mejor arroz con pollo.",
    "Running every morning improved my sleep.", "El contrato vence a final de año.",
    "The API returns JSON with pagination.", "Voy a estudiar alemán para mudarme a Berlín.",
]


class _Centered:
    """Mixin: subtract per-mode background means before normalising."""

    _means: dict[str, np.ndarray]

    def _init_centering(self) -> None:
        self._means = {}
        for mode in ("passage", "query"):
            raw = self._raw_embed(BACKGROUND_SENTENCES, mode)
            self._means[mode] = raw.mean(axis=0)

    def embed(self, texts: list[str], mode: str = "passage") -> np.ndarray:
        raw = self._raw_embed(texts, mode)
        if len(raw) == 0:
            return raw
        return _normalize(raw - self._means["query" if mode == "query" else "passage"])


# Thresholds for *centred* neural embeddings (calibrated with multilingual-e5-small).
CENTERED_THRESHOLDS = Thresholds(association=0.55, duplicate=0.93, topic=0.45)

# --------------------------------------------------------------------------- onnx
ONNX_MODELS = {
    "multilingual-e5-small": {
        "repo": "Xenova/multilingual-e5-small",
        "files": {
            "model_quantized.onnx": "onnx/model_quantized.onnx",
            "tokenizer.json": "tokenizer.json",
        },
        "dim": 384,
        "query_prefix": "query: ",
        "passage_prefix": "passage: ",
        "max_tokens": 512,
        "thresholds": CENTERED_THRESHOLDS,
    },
}


def onnx_model_dir(models_dir: Path, model: str) -> Path:
    return models_dir / model


def onnx_model_available(models_dir: Path, model: str) -> bool:
    spec = ONNX_MODELS.get(model)
    if not spec:
        return False
    d = onnx_model_dir(models_dir, model)
    return all((d / f).is_file() and (d / f).stat().st_size > 0 for f in spec["files"])


def download_onnx_model(models_dir: Path, model: str, progress=print) -> Path:
    """Download a public model from Hugging Face. Only *downloads*; no user data is sent."""
    spec = ONNX_MODELS[model]
    target = onnx_model_dir(models_dir, model)
    target.mkdir(parents=True, exist_ok=True)
    for local_name, remote in spec["files"].items():
        dest = target / local_name
        if dest.is_file() and dest.stat().st_size > 0:
            progress(f"  ✓ {local_name} already present")
            continue
        url = f"https://huggingface.co/{spec['repo']}/resolve/main/{remote}"
        progress(f"  ↓ {url}")
        tmp = dest.with_suffix(dest.suffix + ".part")
        req = urllib.request.Request(url, headers={"User-Agent": "engram-model-downloader"})
        with urllib.request.urlopen(req, timeout=60) as resp, tmp.open("wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if total:
                    progress(f"    {done / total:6.1%}  ({done // (1 << 20)} / {total // (1 << 20)} MB)")
        tmp.replace(dest)
    (target / "engram-model.json").write_text(json.dumps({"model": model, "repo": spec["repo"]}), "utf-8")
    return target


class OnnxEmbeddingProvider(_Centered):
    neural = True

    def __init__(self, models_dir: Path, model: str = "multilingual-e5-small", threads: int = 2, batch_size: int = 16):
        import onnxruntime as ort  # local import: optional dependency
        from tokenizers import Tokenizer

        spec = ONNX_MODELS[model]
        d = onnx_model_dir(models_dir, model)
        self.name = f"onnx:{model}-q8-c1"  # c1 = background-centred, version 1
        self.dim = spec["dim"]
        self.thresholds = spec["thresholds"]
        self._qp, self._pp = spec["query_prefix"], spec["passage_prefix"]
        self.batch_size = batch_size
        self.tokenizer = Tokenizer.from_file(str(d / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=spec["max_tokens"])
        self.tokenizer.enable_padding(pad_id=self.tokenizer.token_to_id("<pad>") or 1, pad_token="<pad>")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, threads)
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # No arena: memory stays flat (~515 MB total with model) instead of growing per batch.
        opts.enable_cpu_mem_arena = False
        self.session = ort.InferenceSession(str(d / "model_quantized.onnx"), opts, providers=["CPUExecutionProvider"])
        self._input_names = {i.name for i in self.session.get_inputs()}
        self._init_centering()

    def _raw_embed(self, texts: list[str], mode: str = "passage") -> np.ndarray:
        prefix = self._qp if mode == "query" else self._pp
        out = []
        for start in range(0, len(texts), self.batch_size):
            batch = [prefix + t for t in texts[start : start + self.batch_size]]
            enc = self.tokenizer.encode_batch(batch)
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._input_names:
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self.session.run(None, feeds)[0]  # (batch, seq, dim)
            m = mask[..., None].astype(np.float32)
            pooled = (hidden * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
            out.append(pooled)
        if not out:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _normalize(np.vstack(out))  # unit vectors; centring happens in embed()


# --------------------------------------------------------------------------- ollama
class OllamaEmbeddingProvider(_Centered):
    """Embeddings from a *local* Ollama server (http://127.0.0.1:11434 by default).

    Thresholds are the centred defaults calibrated on e5; for other models they are an
    approximation — override ``association_threshold`` in engram.toml if needed.
    """

    neural = True

    def __init__(self, url: str, model: str):
        self.url = url.rstrip("/")
        self.model = model
        self.name = f"ollama:{model}-c1"
        self.thresholds = CENTERED_THRESHOLDS
        probe = self._call(["probe"])
        self.dim = probe.shape[1]
        self._init_centering()

    def _call(self, texts: list[str]) -> np.ndarray:
        body = json.dumps({"model": self.model, "input": texts}).encode()
        req = urllib.request.Request(f"{self.url}/api/embed", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return np.array(data["embeddings"], dtype=np.float32)

    def _raw_embed(self, texts: list[str], mode: str = "passage") -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _normalize(self._call(texts))


# --------------------------------------------------------------------------- factory
def create_provider(settings) -> tuple[EmbeddingProvider | None, str]:
    """Return (provider, human readable status)."""
    choice = settings.embedding_provider
    if choice == "none":
        return None, "disabled by configuration"
    if choice in ("auto", "onnx"):
        if onnx_model_available(settings.models_dir, settings.onnx_model):
            try:
                p = OnnxEmbeddingProvider(settings.models_dir, settings.onnx_model, settings.onnx_threads, settings.embed_batch_size)
                return p, "neural (local ONNX)"
            except ImportError as exc:
                msg = f"onnxruntime/tokenizers not installed ({exc})"
            except Exception as exc:  # corrupted model etc.
                msg = f"ONNX model failed to load: {exc}"
        else:
            msg = "ONNX model not downloaded (run: engram models download)"
        if choice == "onnx":
            raise RuntimeError(msg)
        log.warning("%s — falling back to lexical hash vectors", msg)
        return HashEmbeddingProvider(), f"lexical fallback (non-neural): {msg}"
    if choice == "ollama":
        return OllamaEmbeddingProvider(settings.ollama_url, settings.ollama_embed_model), "neural (local Ollama)"
    if choice == "hash":
        return HashEmbeddingProvider(), "lexical (non-neural, by configuration)"
    raise ValueError(f"unknown embedding provider: {choice}")


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / ((np.linalg.norm(a) * np.linalg.norm(b)) or 1.0))


def to_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def from_blob(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


__all__ = [
    "EmbeddingProvider", "HashEmbeddingProvider", "OnnxEmbeddingProvider", "OllamaEmbeddingProvider",
    "Thresholds", "create_provider", "download_onnx_model", "onnx_model_available", "cosine",
    "to_blob", "from_blob",
]

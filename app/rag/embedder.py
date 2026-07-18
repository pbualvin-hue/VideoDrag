"""fastembed (ONNX) embedding wrapper — no PyTorch (CLAUDE.md rule 4).

Chunk embeddings are computed over "title + newline + chunk text";
the stored chunk text stays pure content. The winning model from the
Phase 1 A/B eval is locked into the meta table and guarded at startup.
"""

from __future__ import annotations

import threading

import numpy as np
from fastembed import TextEmbedding


class Embedder:
    def __init__(self, model_name: str):
        self.model_name = model_name
        self._model = TextEmbedding(model_name=model_name)
        # One Embedder is shared across FastAPI request threads; fastembed's
        # thread-safety isn't contractually guaranteed, so serialize inference.
        self._lock = threading.Lock()
        # Probe once to learn the dimension instead of trusting a lookup table.
        self.dim = len(next(iter(self._model.embed(["dim probe"]))))

    def embed_texts(self, texts: list[str]) -> list[np.ndarray]:
        with self._lock:
            return [np.asarray(v, dtype=np.float32)
                    for v in self._model.embed(texts)]

    def embed_chunks(self, title: str, chunk_texts: list[str]) -> list[np.ndarray]:
        """Embedding input must be title-prefixed (CLAUDE.md rule 4)."""
        return self.embed_texts([f"{title}\n{text}" for text in chunk_texts])

    def embed_query(self, query: str) -> np.ndarray:
        # fastembed's query_embed applies model-specific query instructions
        # (e.g. BGE's retrieval prefix) when the model defines one.
        with self._lock:
            vec = next(iter(self._model.query_embed(query)))
        return np.asarray(vec, dtype=np.float32)


def vector_to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()

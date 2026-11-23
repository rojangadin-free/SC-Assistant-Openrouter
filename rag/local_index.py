"""In-process dot-product hybrid search with atomic, cross-worker snapshots.

The root manifest is the commit point. Generation files are immutable, so a
reader holding an old snapshot never observes a half-written rebuild.
"""
from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any
import uuid

import numpy as np
from scipy import sparse as sp
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.retrievers import BaseRetriever
from pinecone_text.sparse import BM25Encoder
from pydantic import Field

from rag.store import _file_transaction

MODEL_NAME = 'sentence-transformers/all-MiniLM-L6-v2'


def _atomic_write(path, writer):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.tmp', delete=False) as handle:
            temporary = handle.name
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _encoder_bytes(encoder):
    # Use the encoder's public serialization, not its private fitted fields.
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'bm25.json'
        encoder.dump(str(path))
        return path.read_bytes()


def publish_bm25(path, encoder):
    """Keep the legacy BM25 artifact available for rollback and vocabulary tools."""
    with _file_transaction(str(path) + '.refresh'):
        payload = _encoder_bytes(encoder)
        _atomic_write(path, lambda handle: handle.write(payload))


def _model_name(embeddings, expected):
    model = getattr(embeddings, 'model', embeddings)
    actual = getattr(model, 'model_name', expected)
    if actual != expected:
        raise ValueError(f'Local index embedding model mismatch: running {actual!r}, expected {expected!r}.')
    return actual


def _mask(documents, metadata_filter):
    result = np.ones(len(documents), dtype=bool)
    if metadata_filter is None:
        return result
    if not isinstance(metadata_filter, dict):
        raise ValueError('Local index filter must be a metadata dictionary or None.')
    for key, condition in metadata_filter.items():
        if not isinstance(condition, dict) or len(condition) != 1:
            raise ValueError('Local index filters support only $eq and $in.')
        operator, value = next(iter(condition.items()))
        if operator == '$eq':
            matches = [key in d.metadata and d.metadata[key] == value for d in documents]
        elif operator == '$in' and isinstance(value, (list, tuple)):
            matches = [key in d.metadata and d.metadata[key] in value for d in documents]
        else:
            raise ValueError('Local index filters support only $eq and $in with a list.')
        result &= matches
    return result


@dataclass(frozen=True)
class _Snapshot:
    documents: tuple
    dense: np.ndarray
    sparse: Any
    encoder: Any
    vocabulary: dict
    manifest: dict


class LocalIndex:
    def __init__(self, path, embeddings, embedding_model=MODEL_NAME, reload_interval=1.0):
        self.path = Path(path).resolve()
        self.embeddings = embeddings
        self.embedding_model = _model_name(embeddings, embedding_model)
        self.dimension = len(embeddings.embed_query('dimension probe'))
        self.reload_interval = reload_interval
        self._lock = threading.RLock()
        self._checked_at = 0.0
        self._signature = None
        self._snapshot = None
        self.snapshot()

    def _load(self, manifest):
        if manifest.get('embedding_model') != self.embedding_model:
            raise ValueError('Local index embedding model mismatch. Rebuild with the running embedding model.')
        if manifest.get('dimension') != self.dimension:
            raise ValueError('Local index embedding dimension mismatch. Rebuild with the running embeddings.')
        if manifest.get('format_version') != 1:
            raise ValueError('Unsupported local index manifest version. Rebuild the index.')
        generation = manifest['generation']
        if not isinstance(generation, str) or not re_safe_generation(generation):
            raise ValueError('Invalid local index generation.')
        directory = self.path / 'generations' / generation
        for name in ('vectors.npy', 'chunks.jsonl', 'sparse.npz', 'bm25.json'):
            digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
            if digest != manifest['files'][name]:
                raise ValueError(f'Local index artifact hash mismatch: {name}. Rebuild the index.')
        dense = np.load(directory / 'vectors.npy', allow_pickle=False)
        sparse = sp.load_npz(directory / 'sparse.npz').tocsr()
        with (directory / 'chunks.jsonl').open(encoding='utf-8') as handle:
            documents = tuple(Document(**json.loads(line)) for line in handle)
        vocabulary = {int(token): column for column, token in enumerate(manifest['sparse_tokens'])}
        count = manifest['chunk_count']
        if (len(documents) != count or dense.shape != (count, self.dimension)
                or dense.dtype != np.float32 or sparse.shape != (count, len(vocabulary))
                or not np.isfinite(dense).all() or not np.isfinite(sparse.data).all()):
            raise ValueError('Local index artifact shape/type mismatch. Rebuild the index.')
        dense.flags.writeable = False
        sparse.data.flags.writeable = False
        sparse.indices.flags.writeable = False
        sparse.indptr.flags.writeable = False
        encoder = BM25Encoder().load(str(directory / 'bm25.json'))
        return _Snapshot(documents, dense, sparse, encoder, vocabulary, manifest)

    def snapshot(self):
        with self._lock:
            now = time.monotonic()
            if self._snapshot is not None and now - self._checked_at < self.reload_interval:
                return self._snapshot
            manifest_path = self.path / 'manifest.json'
            # Windows cannot replace an open manifest reliably. Readers join the
            # publication lock briefly; immutable generation loading stays outside it.
            with _file_transaction(str(manifest_path) + '.publish'):
                try:
                    stat = manifest_path.stat()
                except FileNotFoundError:
                    raise RuntimeError(f'Local index manifest missing at {manifest_path}. '
                                       'Run VECTOR_BACKEND=local python store_index.py before starting the app.') from None
                signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
                if signature != self._signature:
                    with manifest_path.open(encoding='utf-8') as handle:
                        manifest = json.load(handle)
            if signature != self._signature:
                candidate = self._load(manifest)
                # No state changes until every artifact and the encoder validate.
                self._snapshot = candidate
                self._signature = signature
            self._checked_at = now
            return self._snapshot

    @classmethod
    def build(cls, path, documents, embeddings, sparse_encoder, embedding_model=MODEL_NAME, bm25_path=None):
        _model_name(embeddings, embedding_model)
        dimension = len(embeddings.embed_query('dimension probe'))
        documents = list(documents)
        texts = [d.page_content for d in documents]
        dense = np.asarray(embeddings.embed_documents(texts), dtype=np.float32) if texts else np.empty((0, dimension), np.float32)
        with _file_transaction(str(Path(path) / 'manifest.json.refresh')):
            cls._persist(path, documents, dense, sparse_encoder, embedding_model, dimension)
            if bm25_path is not None:
                try:
                    publish_bm25(bm25_path, sparse_encoder)
                except OSError as exc:
                    raise RuntimeError('Local index generation committed, but legacy BM25 publication '
                                       f'failed at {bm25_path}. Local retrieval remains coherent; '
                                       'refresh that file before Pinecone rollback.') from exc
        return cls(path, embeddings, embedding_model)

    @staticmethod
    def _persist(path, documents, dense, encoder, model, dimension, sparse_vectors=None):
        dense = np.asarray(dense, dtype=np.float32)
        if dense.shape != (len(documents), dimension) or not np.isfinite(dense).all():
            raise ValueError('Local index document embeddings have incorrect dimension or non-finite values.')
        texts = [d.page_content for d in documents]
        encoded = sparse_vectors if sparse_vectors is not None else (encoder.encode_documents(texts) if texts else [])
        if len(encoded) != len(documents):
            raise ValueError('Local index sparse document count mismatch.')
        tokens = sorted({int(token) for vector in encoded for token in vector['indices']})
        columns = {token: column for column, token in enumerate(tokens)}
        rows, indices, values = [], [], []
        for row, vector in enumerate(encoded):
            for token, value in zip(vector['indices'], vector['values']):
                rows.append(row)
                indices.append(columns[int(token)])
                values.append(float(value))
        matrix = sp.csr_matrix((np.asarray(values, dtype=np.float32), (rows, indices)),
                               shape=(len(documents), len(tokens)), dtype=np.float32)
        if not np.isfinite(matrix.data).all():
            raise ValueError('Local index sparse embeddings contain non-finite values.')
        chunks = b''.join(_json_bytes({'page_content': d.page_content, 'metadata': d.metadata}) + b'\n'
                          for d in documents)
        generation = uuid.uuid4().hex
        directory = Path(path) / 'generations' / generation
        directory.mkdir(parents=True)
        _atomic_write(directory / 'vectors.npy', lambda handle: np.save(handle, dense, allow_pickle=False))
        _atomic_write(directory / 'chunks.jsonl', lambda handle: handle.write(chunks))
        _atomic_write(directory / 'sparse.npz', lambda handle: sp.save_npz(handle, matrix))
        bm25 = _encoder_bytes(encoder)
        _atomic_write(directory / 'bm25.json', lambda handle: handle.write(bm25))
        files = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                 for name in ('vectors.npy', 'chunks.jsonl', 'sparse.npz', 'bm25.json')}
        manifest = {'format_version': 1, 'generation': generation, 'embedding_model': model,
                    'dimension': dimension, 'chunk_count': len(documents),
                    'built_at': datetime.now(timezone.utc).isoformat(),
                    'content_hash': hashlib.sha256(chunks).hexdigest(),
                    'files': files, 'sparse_tokens': tokens}
        # The transaction lock protects read/modify/write operations; this separate
        # short publication lock lets readers continue during expensive staging.
        with _file_transaction(str(Path(path) / 'manifest.json.publish')):
            _atomic_write(Path(path) / 'manifest.json', lambda handle: handle.write(_json_bytes(manifest)))

    def search(self, query, top_k=30, alpha=0.5, filter=None):
        if not 0.0 <= alpha <= 1.0 or top_k < 0:
            raise ValueError('Local index alpha must be in [0, 1] and top_k non-negative.')
        snapshot = self.snapshot()
        eligible = np.flatnonzero(_mask(snapshot.documents, filter))
        if not len(eligible) or top_k == 0:
            return []
        scores = np.zeros(len(eligible), dtype=np.float32)
        if alpha:
            vector = np.asarray(self.embeddings.embed_query(query), dtype=np.float32)
            if vector.shape != (self.dimension,) or not np.isfinite(vector).all():
                raise ValueError('Local index query embedding dimension mismatch or non-finite values.')
            # The model already normalizes. Pinecone dotproduct does no extra normalization.
            scores += alpha * (snapshot.dense[eligible] @ vector)
        if alpha < 1:
            encoded = snapshot.encoder.encode_queries(query)
            entries = [(snapshot.vocabulary[int(token)], float(value))
                       for token, value in zip(encoded['indices'], encoded['values'])
                       if int(token) in snapshot.vocabulary]
            if entries:
                columns, values = zip(*entries)
                vector = sp.csr_matrix((np.asarray(values, np.float32),
                                        (np.zeros(len(columns), dtype=int), columns)),
                                       shape=(1, len(snapshot.vocabulary)))
                scores += (1 - alpha) * (snapshot.sparse[eligible] @ vector.T).toarray().ravel()
        k = min(top_k, len(eligible))
        # Include boundary ties deterministically; Pinecone's zero-score tie order is unspecified.
        if k < len(eligible):
            selected = np.argpartition(-scores, k - 1)[:k]
            candidates = np.flatnonzero(scores >= scores[selected].min())
        else:
            candidates = np.arange(len(eligible))
        order = candidates[np.lexsort((eligible[candidates], -scores[candidates]))][:k]
        result = []
        for position in order:
            document = snapshot.documents[eligible[position]]
            metadata = deepcopy(document.metadata)
            context = metadata.pop('context', document.page_content)
            metadata.setdefault('score', float(scores[position]))
            result.append(Document(page_content=context, metadata=metadata))
        return result

    def _latest_for_write(self):
        with (self.path / 'manifest.json').open(encoding='utf-8') as handle:
            return self._load(json.load(handle))

    @staticmethod
    def _sparse_vectors(snapshot):
        tokens = snapshot.manifest['sparse_tokens']
        vectors = []
        for row in range(len(snapshot.documents)):
            start, end = snapshot.sparse.indptr[row:row + 2]
            vectors.append({'indices': [tokens[column] for column in snapshot.sparse.indices[start:end]],
                            'values': snapshot.sparse.data[start:end].tolist()})
        return vectors

    def upsert(self, documents, sparse_encoder=None):
        documents = list(documents)
        if not documents:
            return
        dense = np.asarray(self.embeddings.embed_documents([d.page_content for d in documents]), dtype=np.float32)
        if dense.shape != (len(documents), self.dimension) or not np.isfinite(dense).all():
            raise ValueError('Local index document embeddings have incorrect dimension or non-finite values.')
        with _file_transaction(str(self.path / 'manifest.json.refresh')):
            current = self._latest_for_write()
            combined = list(current.documents)
            vectors = list(current.dense)
            sparse_vectors = self._sparse_vectors(current)
            encoder = sparse_encoder or current.encoder
            if sparse_encoder is not None and encoder.get_params() != current.encoder.get_params():
                raise ValueError('Changing BM25 statistics requires a full local index build, not an upsert.')
            encoded = encoder.encode_documents([d.page_content for d in documents])
            positions = {d.metadata['chunk_id']: i for i, d in enumerate(combined)}
            for document, vector, sparse_vector in zip(documents, dense, encoded):
                key = document.metadata['chunk_id']
                if key in positions:
                    position = positions[key]
                    combined[position], vectors[position] = document, vector
                    sparse_vectors[position] = sparse_vector
                else:
                    positions[key] = len(combined)
                    combined.append(document)
                    vectors.append(vector)
                    sparse_vectors.append(sparse_vector)
            self._persist(self.path, combined, np.asarray(vectors, np.float32),
                          encoder, self.embedding_model, self.dimension,
                          sparse_vectors=sparse_vectors)
        with self._lock:
            self._checked_at = 0
        self.snapshot()

    def delete(self, filter=None):
        with _file_transaction(str(self.path / 'manifest.json.refresh')):
            current = self._latest_for_write()
            keep = ~_mask(current.documents, filter)
            if keep.all():
                return
            documents = [d for d, retained in zip(current.documents, keep) if retained]
            sparse_vectors = [v for v, retained in zip(self._sparse_vectors(current), keep) if retained]
            self._persist(self.path, documents, current.dense[keep], current.encoder,
                          self.embedding_model, self.dimension, sparse_vectors=sparse_vectors)
        with self._lock:
            self._checked_at = 0
        self.snapshot()

    def delete_by_source(self, source):
        self.delete(filter={'source': {'$eq': source}})


def re_safe_generation(value):
    return len(value) == 32 and all(character in '0123456789abcdef' for character in value)


class LocalHybridSearchRetriever(BaseRetriever):
    embeddings: Embeddings
    sparse_encoder: Any = None
    index: Any
    top_k: int = Field(default=30, ge=0)
    alpha: float = Field(default=0.5, ge=0.0, le=1.0)

    def _get_relevant_documents(self, query, *, run_manager, **kwargs):
        return self.index.search(query, top_k=self.top_k, alpha=self.alpha,
                                 filter=kwargs.get('filter'))

"""Read-only Pinecone export preserving the exact deployed chunks and vectors."""
import _bootstrap  # noqa: F401
import argparse
from pathlib import Path

import numpy as np
from langchain_core.documents import Document
from pinecone import Pinecone
from pinecone_text.sparse import BM25Encoder

from config import PINECONE_API_KEY, INDEX_NAME, LOCAL_INDEX_DIR
from rag.local_index import LocalIndex, MODEL_NAME
from rag.store import _file_transaction


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=LOCAL_INDEX_DIR)
    parser.add_argument('--bm25', default='bm25_values.json')
    args = parser.parse_args()
    if not PINECONE_API_KEY:
        parser.error('PINECONE_API_KEY is required for this read-only migration.')
    pc = Pinecone(api_key=PINECONE_API_KEY)
    remote = pc.Index(INDEX_NAME)
    description = pc.describe_index(INDEX_NAME)
    if description.dimension != 384 or description.metric != 'dotproduct':
        raise ValueError('Migration expects MiniLM 384-dimensional dotproduct vectors.')
    records = {}
    for page in remote.list(namespace=''):
        items = getattr(page, 'vectors', page)
        identifiers = [item if isinstance(item, str) else item.id for item in items]
        records.update(remote.fetch(ids=identifiers, namespace='').vectors)
    documents, dense, sparse = [], [], []
    for identifier in sorted(records):
        record = records[identifier]
        metadata = dict(record.metadata)
        text = metadata.pop('context')
        documents.append(Document(page_content=text, metadata=metadata))
        dense.append(record.values)
        vector = record.sparse_values
        sparse.append({'indices': list(vector.indices), 'values': list(vector.values)}
                      if vector is not None else {'indices': [], 'values': []})
    if len(records) != remote.describe_index_stats().total_vector_count:
        raise ValueError('Remote index changed during export or has other namespaces; retry while ingestion is paused.')
    encoder = BM25Encoder().load(args.bm25)
    with _file_transaction(str(Path(args.output) / 'manifest.json.refresh')):
        LocalIndex._persist(args.output, documents, np.asarray(dense, np.float32).reshape((-1, 384)),
                            encoder, MODEL_NAME, 384, sparse_vectors=sparse)
    print(f'Exported {len(documents)} exact Pinecone records to {args.output}. No remote writes.')
    print('The supplied BM25 must be the encoder used by the deployed retriever. Run the rank comparison next.')


if __name__ == '__main__':
    main()

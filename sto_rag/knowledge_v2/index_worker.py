"""Background generation builder; no automatic release publication."""
import argparse
import logging
from pathlib import Path
import time

from .embedding import HttpEncoder, MODELS
from .search import GenerationBuilder, QdrantIndex
from .store import KnowledgeStore


def run(data_dir,model,model_cache,embedding_url,qdrant_url,poll_seconds=5):
    store=KnowledgeStore(data_dir)
    encoder=HttpEncoder(model,model_cache,embedding_url)
    builder=GenerationBuilder(store,encoder,QdrantIndex(qdrant_url))
    logging.info('Indexer ready: embedding space %s',encoder.space.id)
    while True:
        try:
            if not builder.work_once():time.sleep(poll_seconds)
        except Exception:
            logging.exception('Generation build failed; task state recorded')
            time.sleep(max(5,poll_seconds))


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--data-dir',required=True,type=Path)
    p.add_argument('--model',required=True,choices=MODELS)
    p.add_argument('--model-cache',required=True,type=Path)
    p.add_argument('--embedding-url',default='http://127.0.0.1:8109')
    p.add_argument('--qdrant-url',default='http://127.0.0.1:6333')
    p.add_argument('--poll-seconds',type=int,default=5)
    a=p.parse_args();logging.basicConfig(level=logging.INFO)
    run(a.data_dir,a.model,a.model_cache,a.embedding_url,a.qdrant_url,a.poll_seconds)

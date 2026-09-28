"""CPU-only embedding boundary for the new knowledge store.

No model is downloaded or loaded while importing this module. A model revision is
explicit and the space identifier includes hashes of the local model files.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import urllib.request

from .store import checksum


MODELS = {
    'intfloat/multilingual-e5-small': {
        'revision': 'ada7b62be30f82b0bc5da131b0477721c8fc14e9',
        'dimension': 384, 'max_tokens': 512, 'query_prefix': 'query: ',
        'passage_prefix': 'passage: ',
    },
    'Qwen/Qwen3-Embedding-0.6B': {
        'revision': 'c935f2d3fce3e2337b4a66ac3130faa6dada3218',
        'dimension': 1024, 'max_tokens': 32768,
        'query_prefix': 'Instruct: Найди применимые нормативные требования и точные пункты.\nQuery: ',
        'passage_prefix': '',
    },
}


@dataclass(frozen=True)
class EmbeddingSpace:
    model: str
    revision: str
    model_sha256: str
    tokenizer_sha256: str
    dimension: int
    max_tokens: int
    query_prefix: str
    passage_prefix: str
    chunker: str = 'exact-char-v1'
    normalization: str = 'l2'

    @property
    def id(self):
        return 'emb-v1-' + checksum(self.__dict__)[:24]

    @classmethod
    def from_model_files(cls, model, directory):
        if model not in MODELS:
            raise ValueError('Embedding model is not allowlisted')
        root = Path(directory).resolve()
        weights = next((root / name for name in ('model.safetensors', 'pytorch_model.bin')
                        if (root / name).is_file()), None)
        tokenizer = root / 'tokenizer.json'
        if not weights or not tokenizer.is_file():
            raise FileNotFoundError('Model weights and tokenizer.json are required locally')
        def digest(path):
            h = hashlib.sha256()
            with path.open('rb') as source:
                for block in iter(lambda: source.read(4 * 1024 * 1024), b''):
                    h.update(block)
            return h.hexdigest()
        return cls(model=model, revision=MODELS[model]['revision'],
                   model_sha256=digest(weights), tokenizer_sha256=digest(tokenizer),
                   **{k: v for k, v in MODELS[model].items() if k != 'revision'})


class CpuEncoder:
    """One long-lived SentenceTransformer, limited to a small CPU thread budget."""

    def __init__(self, model, cache_dir, *, threads=2, batch_size=8):
        if model not in MODELS or not 1 <= threads <= 8 or not 1 <= batch_size <= 32:
            raise ValueError('Unsupported model or CPU budget')
        os.environ['OMP_NUM_THREADS'] = str(threads)
        os.environ['MKL_NUM_THREADS'] = str(threads)
        import torch
        from huggingface_hub import snapshot_download
        from sentence_transformers import SentenceTransformer
        torch.set_num_threads(threads)
        root = snapshot_download(model, revision=MODELS[model]['revision'],
                                 cache_dir=str(cache_dir), local_files_only=True,
                                 allow_patterns=['*.json','*.safetensors','*.txt','*.model'])
        self.space = EmbeddingSpace.from_model_files(model, root)
        self.model = SentenceTransformer(root, device='cpu')
        self.tokenizer = self.model.tokenizer
        self.batch_size = batch_size

    def token_count(self, text):
        return len(self.tokenizer.encode(text, add_special_tokens=True, truncation=False))

    def _check(self, texts, prefix):
        if any(not isinstance(t, str) or not t.strip() for t in texts):
            raise ValueError('Empty embedding input')
        if any(self.token_count(prefix + t) > self.space.max_tokens for t in texts):
            raise ValueError('Embedding input exceeds model limit; split explicitly')

    def encode(self, texts, *, query=False):
        prefix = self.space.query_prefix if query else self.space.passage_prefix
        self._check(texts, prefix)
        vectors = self.model.encode([prefix + t for t in texts], batch_size=self.batch_size,
                                    normalize_embeddings=True, convert_to_numpy=True,
                                    show_progress_bar=False)
        result = [list(map(float, row)) for row in vectors]
        if any(len(row) != self.space.dimension for row in result):
            raise ValueError('Embedding dimension mismatch')
        return result


class HttpEncoder:
    """Builder/search client of the separate, local CPU embedding process."""
    def __init__(self,model,cache_dir,url='http://127.0.0.1:8109'):
        import re
        if not re.fullmatch(r'http://(?:127\.0\.0\.1|localhost|embeddings)(?::\d+)?',url.rstrip('/')):
            raise ValueError('Embedding service must be local or on the private Compose network')
        from huggingface_hub import snapshot_download
        from transformers import AutoTokenizer
        root=snapshot_download(model,revision=MODELS[model]['revision'],
                               cache_dir=str(cache_dir),local_files_only=True,
                               allow_patterns=['*.json','*.safetensors','*.txt','*.model'])
        self.space=EmbeddingSpace.from_model_files(model,root)
        self.tokenizer=AutoTokenizer.from_pretrained(root,local_files_only=True)
        self.url=url.rstrip('/')
        with urllib.request.urlopen(self.url+'/health',timeout=5) as response:
            info=json.load(response)
        if info['embedding_space_id']!=self.space.id:
            raise ValueError('Embedding service uses a different space')
        self.batch_size=info.get('max_batch_size',8)
        if type(self.batch_size) is not int or not 1<=self.batch_size<=32:
            raise ValueError('Invalid embedding service batch size')

    def token_count(self,text):
        return len(self.tokenizer.encode(text,add_special_tokens=True,truncation=False))

    def encode(self,texts,*,query=False):
        prefix=self.space.query_prefix if query else self.space.passage_prefix
        if not texts or any(self.token_count(prefix+t)>self.space.max_tokens for t in texts):
            raise ValueError('Explicitly split input before embedding')
        vectors=[]
        for start in range(0,len(texts),self.batch_size):
            batch=texts[start:start+self.batch_size]
            body=json.dumps({'texts':batch,'query':query},ensure_ascii=False).encode('utf-8')
            req=urllib.request.Request(self.url+'/embed',data=body,method='POST',
                                       headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=120) as response:
                result=json.load(response)
            if result['embedding_space_id']!=self.space.id:
                raise ValueError('Embedding response space changed')
            rows=result.get('vectors')
            if (not isinstance(rows,list) or len(rows)!=len(batch) or
                any(not isinstance(row,list) or len(row)!=self.space.dimension or
                    any(type(x) not in (int,float) or not math.isfinite(x) for x in row) for row in rows)):
                raise ValueError('Invalid embedding response shape or values')
            vectors.extend(rows)
        return vectors


def exact_chunks(text, token_count, limit, prefix='', *, minimum_fraction=0.7):
    """Return lossless, character-offset chunks; never silently truncate input."""
    if not isinstance(text, str) or not text or limit < 16:
        raise ValueError('Invalid chunk input')
    if token_count(prefix + 'a') > limit:
        raise ValueError('Prefix consumes embedding budget')
    # Most normative paragraphs already fit. Avoid a remote tokenizer round trip
    # for every binary-search step when the complete exact text can be retained.
    if token_count(prefix + text) <= limit:
        return [(0, len(text), text)]
    chunks = []
    start = 0
    while start < len(text):
        lo, hi, best = start + 1, len(text), start
        while lo <= hi:
            mid = (lo + hi) // 2
            if token_count(prefix + text[start:mid]) <= limit:
                best = mid; lo = mid + 1
            else:
                hi = mid - 1
        if best == start:
            raise ValueError('One character exceeds embedding budget')
        end = best
        if end < len(text):
            floor = start + int((best - start) * minimum_fraction)
            candidates = [text.rfind(mark, floor, best) for mark in ('\n', '. ', '; ', ' ')]
            boundary = max(candidates)
            if boundary > start:
                end = boundary + (1 if text[boundary] == '\n' else 0)
        chunks.append((start, end, text[start:end]))
        start = end
    assert ''.join(row[2] for row in chunks) == text
    return chunks

class RemoteEncoder(HttpEncoder):
    """Lightweight trusted-network client; tokenizer and weights stay in CPU service."""
    def __init__(self,url='http://127.0.0.1:8109'):
        import re
        if not re.fullmatch(r'http://(?:127\.0\.0\.1|localhost|embeddings)(?::\d+)?',url.rstrip('/')):
            raise ValueError('Embedding service must be local/private')
        self.url=url.rstrip('/')
        with urllib.request.urlopen(self.url+'/health',timeout=5) as response:info=json.load(response)
        self.space=EmbeddingSpace(**info['space'])
        if self.space.id!=info['embedding_space_id'] or self.space.model not in MODELS:raise ValueError('Embedding identity')
        self.batch_size=info['max_batch_size']
        if type(self.batch_size) is not int or not 1<=self.batch_size<=32:raise ValueError('Embedding batch size')
    def token_count(self,text):
        request=urllib.request.Request(self.url+'/token-count',data=json.dumps({'text':text}).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=30) as response:data=json.load(response)
        if data['embedding_space_id']!=self.space.id:raise ValueError('Embedding identity changed')
        return data['count']

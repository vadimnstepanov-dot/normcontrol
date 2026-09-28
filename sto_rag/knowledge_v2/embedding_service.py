"""Long-lived loopback CPU embedding service (no document data leaves the host)."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

from .embedding import CpuEncoder, MODELS


def serve(model,cache_dir,port=8109,threads=2,batch_size=8,host='127.0.0.1'):
    encoder=CpuEncoder(model,cache_dir,threads=threads,batch_size=batch_size)
    gate=threading.BoundedSemaphore(1)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path!='/health':self.send_error(404);return
            self.reply(200,{'model':model,'embedding_space_id':encoder.space.id,
                            'dimension':encoder.space.dimension,'threads':threads,
                            'max_batch_size':batch_size,'space':encoder.space.__dict__})

        def do_POST(self):
            if self.path not in ('/embed','/token-count'):self.send_error(404);return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 1<=size<=2*1024*1024:raise ValueError('Request size exceeds limit')
                body=json.loads(self.rfile.read(size))
                if self.path=='/token-count':
                    if not isinstance(body.get('text'),str):raise ValueError('Text required')
                    with gate:count=encoder.token_count(body['text'])
                    self.reply(200,{'count':count,'embedding_space_id':encoder.space.id});return
                texts=body['texts'];query=body.get('query',False)
                if not isinstance(texts,list) or not 1<=len(texts)<=batch_size or type(query) is not bool:
                    raise ValueError('Invalid embedding batch')
                with gate:vectors=encoder.encode(texts,query=query)
                self.reply(200,{'embedding_space_id':encoder.space.id,'vectors':vectors})
            except (ValueError,KeyError,TypeError) as exc:
                self.reply(400,{'error':str(exc)})

        def reply(self,status,data):
            raw=json.dumps(data,ensure_ascii=False).encode('utf-8')
            self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)

    ThreadingHTTPServer((host,port),Handler).serve_forever()


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--model',required=True,choices=MODELS)
    p.add_argument('--cache-dir',required=True,type=Path)
    p.add_argument('--port',type=int,default=8109)
    p.add_argument('--host',default='127.0.0.1',choices=('127.0.0.1','0.0.0.0'))
    p.add_argument('--threads',type=int,default=2)
    p.add_argument('--batch-size',type=int,default=8)
    a=p.parse_args();serve(a.model,a.cache_dir,a.port,a.threads,a.batch_size,a.host)

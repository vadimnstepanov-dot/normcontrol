"""Outbound-only v2 command bridge; it never claims legacy review jobs."""
import argparse
import json
import os
import signal
import threading
import urllib.request
import urllib.error
import urllib.parse
import uuid
import time
import ssl
from .ingest import ingest, PARSER_VERSION
from .store import KnowledgeStore, checksum, Conflict, NotReady
from .norm_runtime import analyze_source
from .norms import EXTRACTOR_VERSION


class Bridge:
    def __init__(self, store, portal, token, transport=None, downloader=None, experience_index=None, experience_advisor=None, normative_index=None, check_downloader=None, check_client=None, analysis_client=None):
        parsed=urllib.parse.urlsplit(portal)
        if parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in ('127.0.0.1','localhost','knowledge-portal')):
            raise ValueError('Portal requires HTTPS outside loopback')
        if len(token)<32:raise ValueError('Dedicated v2 worker token required')
        self.experience_advisor=experience_advisor
        self.experience_index=experience_index
        self.normative_index=normative_index
        self.check_downloader=check_downloader or self.http_check_document
        self.check_client=check_client
        self.analysis_client=analysis_client
        self.stop_event=threading.Event()
        self.store=store;self.portal=portal.rstrip('/');self.token=token
        self.transport=transport or self.http
        self.downloader=downloader or self.http_source

    def http_source(self,command_id,source_id,lease):
        path=f'/worker/commands/{command_id}/sources/{source_id}/'
        request=urllib.request.Request(self.portal+path,headers={'Authorization':'Bearer '+self.token,
                                                                  'X-Knowledge-Lease':lease})
        return urllib.request.urlopen(request,timeout=60)

    def http_check_document(self,command_id,job_id,document_id,lease):
        path=f'/worker/checks/{command_id}/{job_id}/documents/{document_id}/'
        request=urllib.request.Request(self.portal+path,headers={'Authorization':'Bearer '+self.token,
                                                                  'X-Knowledge-Lease':lease})
        return urllib.request.urlopen(request,timeout=60)

    def http(self, path, payload):
        request=urllib.request.Request(self.portal+path,data=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8'),
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        # Claim is not idempotent. Retrying it could reserve another command.
        safe=path in ('/worker/renew/','/worker/analysis/','/worker/coverage/','/worker/events/','/worker/authorize/')
        for attempt in range(5 if safe else 1):
            try:
                with urllib.request.urlopen(request,timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if not safe or attempt==4 or error.code not in (408,429,500,502,503,504):raise
            except urllib.error.URLError as error:
                if not safe or attempt==4 or isinstance(error.reason,ssl.SSLCertVerificationError):raise
            except (TimeoutError,ConnectionError):
                if not safe or attempt==4:raise
            time.sleep(min(2**attempt,8))

    def authorization_for(self,user_id):
        """Fresh portal ACL for search and each returned exact citation."""
        def authorize(set_id):
            reply=self.transport('/worker/authorize/',{'user_id':user_id,'set_id':set_id,'action':'read'})
            if type(reply.get('allowed')) is not bool:raise ValueError('Invalid authorization reply')
            return reply['allowed']
        return authorize

    def deliver(self, envelope):
        try:
            response=self.transport('/worker/events/',envelope)
        except urllib.error.HTTPError as error:
            if error.code in (403,409):
                self.store.finish_delivery(envelope['command_id'],acknowledged=False)
                return False
            raise
        if response.get('accepted') is not True:raise ValueError('Unacknowledged command')
        self.store.finish_delivery(envelope['command_id'])
        return True

    def fail(self,claim,reason,permanent):
        try:self.transport('/worker/fail/',{'command_id':claim['command_id'],'lease':claim['lease'],
                                            'reason':reason,'permanent':permanent})
        except Exception:pass  # The lease will expire and the bounded retry still applies.

    def analysis_delivery(self, claim, analysis=None):
        """Persist ready analysis before the first remote chunk, including partial results."""
        from .structure import atomic_json
        path=self.store.directory/'analysis-deliveries'/(str(uuid.UUID(claim['command_id']))+'.json')
        identity=checksum(dict(kind=claim['kind'],payload=claim['payload']))
        if analysis is not None:
            atomic_json(path,dict(command_digest=identity,analysis=analysis,digest=checksum(analysis)))
            return analysis
        if not path.exists():return None
        saved=json.loads(path.read_text(encoding='utf8'))
        if saved.get('command_digest')!=identity or saved.get('digest')!=checksum(saved.get('analysis')):
            raise Conflict('Prepared analysis delivery mismatch')
        return saved['analysis']

    def once(self):
        pending=self.store.pending_delivery()
        if pending and self.deliver(pending):return True
        capabilities=['trace.suggest','set.register','source.ingest','source.analyze','release.prepare','release.publish','release.revoke','review.execute','review.submit','review.approve','review.reject','review.revoke','experience.publish','review.repair','review.suggest']
        if os.getenv('KNOWLEDGE_EXPERT_ONLY')=='1':capabilities=['expert.apply','area.import']
        elif os.getenv('KNOWLEDGE_REVIEW_ONLY')=='1':capabilities=['review.execute','trace.suggest','review.suggest']
        else:capabilities.extend(['area.import','source.identify'])
        if os.getenv('KNOWLEDGE_SKIP_REVIEW')=='1' and os.getenv('KNOWLEDGE_REVIEW_ONLY')!='1':capabilities=[x for x in capabilities if x!='review.execute']
        capabilities.append('normative.search')
        from .model_profile import enabled as profile_control
        # A v5 worker must never resume a v4 model/transport snapshot.
        features=['context-budget-v5','check-log-v1','pipeline-v1']
        if profile_control():features.append('visual-tail-v1')
        claim=self.transport('/worker/claim/',{'protocol_version':2,'capabilities':capabilities,'features':features})['command']
        if claim is None:return False
        # After a lost reply the portal lease expires and the same command is redelivered.
        # The store's durable inbox returns exactly the original result.
        if claim['kind']=='normative.search':
            from .embedding import RemoteEncoder
            from .search import HybridSearch,QdrantIndex
            encoder,vector=self.normative_index or (RemoteEncoder(os.getenv('KNOWLEDGE_EMBEDDING_URL','http://127.0.0.1:8109')),QdrantIndex(os.getenv('KNOWLEDGE_QDRANT_URL','http://127.0.0.1:6333')))
            payload=claim['payload']
            def authorize(sid):return self.transport('/worker/authorize/',dict(user_id=payload['actor_id'],set_id=sid,action='read')).get('allowed') is True
            found=HybridSearch(self.store,encoder,vector,authorize).reference(payload['release_id'],payload['query'],kinds=('requirement','term_definition'),profiles=payload.get('profiles'),limit=30)
            with self.store.connection() as db:
                for row in found:
                    record=db.execute('SELECT payload FROM records WHERE id=? AND version=?',(row['record_id'],row['version'])).fetchone()
                    value=json.loads(record['payload']);row['card_id']=value.get('lineage')
            result=dict(kind='normative.search.done',set_id=payload['set_id'],release_id=payload['release_id'],entries=found)
            result=self.store.remember_result(claim['command_id'],claim['kind'],payload,result)
        elif claim['kind']=='source.identify':
            from .source_identity import identify_source
            from .structural_model import StructuralClient
            p=claim['payload'];result=self.store.command_result(claim['command_id'],claim['kind'],p)
            stopped=threading.Event()
            def renew_identity():
                while not stopped.wait(30):
                    try:self.transport('/worker/renew/',dict(command_id=claim['command_id'],lease=claim['lease']))
                    except Exception:return
            heart=threading.Thread(target=renew_identity,daemon=True);heart.start()
            try:
                if result is None:
                    model=self.analysis_client or StructuralClient(os.environ['NORMCONTROL_LLM_ENDPOINT'],os.getenv('KNOWLEDGE_LLM_MODEL','local-qwen'),self.store,api_key=os.getenv('NORMCONTROL_LLM_API_KEY',''),revision=os.environ['KNOWLEDGE_LLM_REVISION'],max_tokens=2500,measure_context=True)
                    identity=identify_source(self.store,p['set_id'],p['source_id'],model,cancel=self.stop_event.is_set)
                    result=self.store.remember_result(claim['command_id'],claim['kind'],p,dict(kind='source.identified',set_id=p['set_id'],source_identity=identity))
            except Exception:
                self.fail(claim,'source_identification_failed',False);raise
            finally:stopped.set();heart.join(timeout=1)
        elif claim['kind']=='area.import':
            from .portable_area import apply
            from .norm_runtime import projection_chunks
            stopped=threading.Event()
            def renew_import():
                while not stopped.wait(30):
                    try:self.transport('/worker/renew/',dict(command_id=claim['command_id'],lease=claim['lease']))
                    except Exception:return
            heart=threading.Thread(target=renew_import,daemon=True);heart.start()
            try:
                result=apply(self.store,claim['command_id'],claim['payload'],lambda actor,sid,action:self.transport('/worker/authorize/',dict(user_id=actor,set_id=sid,action=action)).get('allowed') is True)
                rows=json.loads((self.store.directory/'area-imports'/(claim['command_id']+'.json')).read_text(encoding='utf8'))
                for n,chunk in enumerate(projection_chunks(rows)):
                    ack=self.transport('/worker/area-import/',dict(command_id=claim['command_id'],lease=claim['lease'],sequence=n,entries=chunk,digest=checksum(chunk)))
                    if ack.get('accepted') is not True:raise ValueError('Area import not acknowledged')
            except (ValueError,PermissionError,Conflict):
                self.fail(claim,'area_import_validation_failed',True);raise
            except Exception:
                self.fail(claim,'area_import_delivery_failed',False);raise
            finally:stopped.set();heart.join(timeout=1)
        elif claim['kind']=='expert.apply':
            from .expert import apply
            def grant_expert(actor,set_id,action):
                return self.transport('/worker/authorize/',dict(user_id=actor,set_id=set_id,action=action)).get('allowed') is True
            try:result=apply(self.store,claim['command_id'],claim['payload'],grant_expert)
            except (ValueError,PermissionError):
                self.fail(claim,'expert_validation_failed',True);raise
        elif claim['kind']=='trace.suggest':
            from .trace_suggest import suggest
            from .review_client import LlamaClient
            model=self.check_client or LlamaClient(os.environ['NORMCONTROL_LLM_ENDPOINT'],context=16384,output_tokens=2048,timeout=300,store=self.store)
            stopped=threading.Event()
            def renew_trace():
                while not stopped.wait(30):
                    try:self.transport('/worker/renew/',dict(command_id=claim['command_id'],lease=claim['lease']))
                    except Exception:return
            heart=threading.Thread(target=renew_trace,daemon=True);heart.start()
            try:
                result=suggest(self.store,claim['command_id'],claim['payload'],model,
                    lambda actor,sid,action:self.transport('/worker/authorize/',dict(user_id=actor,set_id=sid,action=action)).get('allowed') is True)
            except Exception:
                self.fail(claim,'trace_suggestion_failed',False);raise
            finally:stopped.set();heart.join(timeout=1)
        elif claim['kind']=='review.execute':
            from .check_runtime import execute
            stopped=threading.Event()
            def renew_check():
                while not stopped.wait(30):
                    try:self.transport('/worker/renew/',{'command_id':claim['command_id'],'lease':claim['lease']})
                    except Exception:return
            heartbeat=threading.Thread(target=renew_check,daemon=True);heartbeat.start()
            try:result=execute(self,claim,self.check_downloader,self.check_client,self.experience_index)
            except (ValueError,PermissionError):
                self.fail(claim,'review_validation_failed',True);raise
            except Exception:
                self.fail(claim,'review_execution_failed',False);raise
            finally:stopped.set();heartbeat.join(timeout=1)
        elif claim['kind']=='release.prepare':
            from .prepare import prepare
            from .embedding import RemoteEncoder
            from .search import QdrantIndex
            encoder,vector=self.normative_index or (RemoteEncoder(os.getenv('KNOWLEDGE_EMBEDDING_URL','http://127.0.0.1:8109')),
                QdrantIndex(os.getenv('KNOWLEDGE_QDRANT_URL','http://127.0.0.1:6333')))
            stopped=threading.Event()
            def renew_preparation():
                while not stopped.wait(30):
                    try:self.transport('/worker/renew/',{'command_id':claim['command_id'],'lease':claim['lease']})
                    except Exception:return
            def grant_preparation(actor,set_id,action):
                return self.transport('/worker/authorize/',dict(user_id=actor,set_id=set_id,action=action)).get('allowed') is True
            heartbeat=threading.Thread(target=renew_preparation,daemon=True);heartbeat.start()
            try:
                result=prepare(self.store,claim['command_id'],claim['payload'],encoder,vector,grant_preparation)
                if 'material_digest' in result:
                    from .norm_runtime import projection_chunks
                    rows=json.loads((self.store.directory/'publication-material'/f"{claim['payload']['release_id']}.json").read_text(encoding='utf8'))
                    for i,chunk in enumerate(projection_chunks(rows)):
                        ack=self.transport('/worker/release-material/',dict(command_id=claim['command_id'],lease=claim['lease'],
                            sequence=i,entries=chunk,digest=checksum(chunk)))
                        if ack.get('accepted') is not True:raise ValueError('Publication material not acknowledged')
            except (ValueError,PermissionError,Conflict,NotReady):
                self.fail(claim,'preparation_validation_failed',True);raise
            except Exception:
                self.fail(claim,'preparation_failed',False);raise
            finally:stopped.set();heartbeat.join(timeout=1)
        elif claim['kind']=='experience.publish':
            from .experience import publish
            from .embedding import RemoteEncoder
            from .search import QdrantIndex
            pair=self.experience_index or (RemoteEncoder(os.getenv('KNOWLEDGE_EMBEDDING_URL','http://127.0.0.1:8109')),QdrantIndex(os.getenv('KNOWLEDGE_QDRANT_URL','http://127.0.0.1:6333')))
            stopped=threading.Event()
            def renew_experience():
                while not stopped.wait(30):
                    try:self.transport('/worker/renew/',{'command_id':claim['command_id'],'lease':claim['lease']})
                    except Exception:return
            def grant(actor,set_id,action):
                return self.transport('/worker/authorize/',dict(user_id=actor,set_id=set_id,action=action)).get('allowed') is True
            heartbeat=threading.Thread(target=renew_experience,daemon=True);heartbeat.start()
            try:result=publish(self.store,claim['command_id'],claim['payload'],*pair,grant)
            except Exception:
                self.fail(claim,'experience_publication_failed',False);raise
            finally:stopped.set();heartbeat.join(timeout=1)
        elif claim['kind']=='review.suggest':
            from .experience import suggest
            from .review_client import LlamaClient
            client=self.experience_advisor or LlamaClient(os.environ['NORMCONTROL_LLM_ENDPOINT'],context=8192,output_tokens=1024,timeout=90,store=self.store)
            def grant_suggestion(actor,set_id,action):
                return self.transport('/worker/authorize/',dict(user_id=actor,set_id=set_id,action=action)).get('allowed') is True
            stopped=threading.Event()
            def renew_suggestion():
                while not stopped.wait(30):
                    try:self.transport('/worker/renew/',{'command_id':claim['command_id'],'lease':claim['lease']})
                    except Exception:return
            heartbeat=threading.Thread(target=renew_suggestion,daemon=True);heartbeat.start()
            try:result=suggest(self.store,claim['command_id'],claim['payload'],client,grant_suggestion)
            except Exception:
                self.fail(claim,'review_suggestion_failed',False);raise
            finally:stopped.set();heartbeat.join(timeout=1)
        elif claim['kind'].startswith('review.'):
            from .experience import apply
            def authorize(actor,set_id,action):
                return self.transport('/worker/authorize/',dict(user_id=actor,set_id=set_id,action=action)).get('allowed') is True
            try:result=apply(self.store,claim['command_id'],claim['kind'],claim['payload'],authorize)
            except (ValueError,PermissionError):
                self.fail(claim,'review_validation_failed',True);raise
        elif claim['kind']=='source.ingest':
            payload=claim['payload'];stopped=threading.Event()
            if payload.get('parser_version')!=PARSER_VERSION:
                self.fail(claim,'dependency_unavailable',True)
                raise RuntimeError('Parser version unavailable')
            def renew():
                while not stopped.wait(90):
                    try:self.transport('/worker/renew/',{'command_id':claim['command_id'],'lease':claim['lease']})
                    except Exception:return
            result=self.store.command_result(claim['command_id'],claim['kind'],payload)
            if result is None:
                heartbeat=threading.Thread(target=renew,daemon=True);heartbeat.start()
                try:
                    try:
                        with self.downloader(claim['command_id'],payload['source_id'],claim['lease']) as source:
                            result=ingest(self.store,payload['set_id'],payload['source_id'],payload['filename'],
                                          payload['sha256'],source,payload.get('supersedes'))
                    except ValueError:
                        self.fail(claim,'invalid_source',True);raise
                    except RuntimeError as exc:
                        self.fail(claim,'dependency_unavailable' if 'required' in str(exc) or 'unavailable' in str(exc)
                                  else 'conversion_failed',True);raise
                    except Exception:
                        self.fail(claim,'transfer_failed',False);raise
                    journal=json.loads((self.store.directory/'coverage'/(payload['source_id']+'.json')).read_text(encoding='utf-8'))
                    for i in range(0,len(journal['coverage']),500):
                        try:
                            chunk=journal['coverage'][i:i+500]
                            accepted=self.transport('/worker/coverage/',{'command_id':claim['command_id'],'lease':claim['lease'],
                                'source_id':payload['source_id'],'sequence':i//500,'entries':chunk,'digest':checksum(chunk)})
                            if accepted.get('accepted') is not True:raise ValueError('Coverage chunk not acknowledged')
                        except Exception:
                            self.fail(claim,'transfer_failed',False);raise
                    result=self.store.remember_result(claim['command_id'],claim['kind'],payload,result)
                finally:stopped.set();heartbeat.join(timeout=1)
        elif claim['kind']=='source.analyze':
            payload=claim['payload'];stopped=threading.Event()
            from .semantic import VERSION as SEMANTIC_VERSION
            if payload.get('extractor_version') not in (EXTRACTOR_VERSION,SEMANTIC_VERSION):
                self.fail(claim,'dependency_unavailable',True);raise RuntimeError('Extractor version unavailable')
            lease_lost=threading.Event()
            def cancelled():return lease_lost.is_set() or self.stop_event.is_set()
            def renew_analysis():
                from .lease_heartbeat import renew_analysis_lease
                renew_analysis_lease(self.transport,claim,stopped,lease_lost)
            heartbeat=threading.Thread(target=renew_analysis,daemon=True);heartbeat.start()
            try:
                result=self.store.command_result(claim['command_id'],claim['kind'],payload)
                if result is None:
                    client=None;parse_ref=None
                    if payload['extractor_version']==SEMANTIC_VERSION:
                        from .structural_model import StructuralClient
                        client=self.analysis_client
                        if client is None:
                            endpoint=os.environ.get('NORMCONTROL_LLM_ENDPOINT','')
                            revision=os.environ.get('KNOWLEDGE_LLM_REVISION','')
                            if not endpoint or not revision:raise RuntimeError('Configured model endpoint and immutable revision required')
                            client=StructuralClient(endpoint,os.getenv('KNOWLEDGE_LLM_MODEL','local-qwen'),self.store,
                                api_key=os.getenv('NORMCONTROL_LLM_API_KEY',''),revision=revision,max_tokens=6000,measure_context=True)
                        parse_ref=payload.get('parse_ref')
                        if parse_ref is None and self.analysis_client is None:
                            from .structure_runtime import reparse_source
                            vision=StructuralClient(os.getenv('KNOWLEDGE_VISION_ENDPOINT') or endpoint,
                                os.getenv('KNOWLEDGE_VISION_MODEL') or client.model,self.store,
                                api_key=os.getenv('KNOWLEDGE_VISION_API_KEY') or os.getenv('NORMCONTROL_LLM_API_KEY',''),
                                revision=os.getenv('KNOWLEDGE_VISION_REVISION') or revision,max_tokens=3000)
                            parsed=reparse_source(self.store,payload['set_id'],payload['source_id'],cancel=cancelled,
                                structural_client=vision,interpret_graphics=True)
                            parse_ref=parsed['parse_ref']
                    analysis=self.analysis_delivery(claim)
                    if analysis is None:
                        analysis=analyze_source(self.store,payload['set_id'],payload['source_id'],client=client,
                            cancel=cancelled,parse_ref=parse_ref)
                        self.analysis_delivery(claim,analysis)
                    if client is not None and (payload.get('automatic_screening') or os.getenv('KNOWLEDGE_QUALITY_SCREENING','0')=='1'):
                        from .quality_audit import audit
                        if cancelled():raise InterruptedError('Stopped before quality screening')
                        audit_client=(self.analysis_client or StructuralClient(endpoint,client.model,self.store,
                            api_key=os.getenv('NORMCONTROL_LLM_API_KEY',''),revision=revision,max_tokens=1800,measure_context=True))
                        audit(self.store,analysis,audit_client,cancel=cancelled)
                    if analysis['source_sha256']!=payload['sha256']:raise ValueError('Analysis source mismatch')
                    self.transport('/worker/renew/',{'command_id':claim['command_id'],'lease':claim['lease']})
                    from .norm_runtime import projection_chunks
                    for i,chunk in enumerate(projection_chunks(analysis['projection'])):
                        ack=self.transport('/worker/analysis/',dict(command_id=claim['command_id'],lease=claim['lease'],
                            sequence=i,entries=chunk,digest=checksum(chunk)))
                        if ack.get('accepted') is not True:raise ValueError('Analysis chunk not acknowledged')
                    result=self.store.remember_result(claim['command_id'],claim['kind'],payload,analysis['summary'])
            except Exception:
                self.fail(claim,'processing_failed',False);raise
            finally:stopped.set();heartbeat.join(timeout=1)
        else:result=self.store.apply_command(claim['command_id'],claim['kind'],claim['payload'])
        event_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'knowledge-v2:'+claim['command_id']))
        envelope=dict(event_id=event_id,command_id=claim['command_id'],lease=claim['lease'],payload=result,payload_hash=checksum(result))
        self.store.save_delivery(claim['command_id'],envelope)
        return self.deliver(envelope)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');args=parser.parse_args()
    store=KnowledgeStore()
    bridge=Bridge(store,os.environ['NORMCONTROL_KNOWLEDGE_PORTAL'],os.environ['NORMCONTROL_KNOWLEDGE_WORKER_TOKEN'])
    stopped=threading.Event()
    bridge.stop_event=stopped
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stopped.set())
    while not stopped.is_set():
        try:bridge.once()
        except Exception as e:
            # Only a type is logged: neither auth headers nor private payloads.
            print('Knowledge command delivery failed: '+type(e).__name__,flush=True)
            if args.once:raise SystemExit(1)
        if args.once:return
        stopped.wait(5)


if __name__=='__main__':main()

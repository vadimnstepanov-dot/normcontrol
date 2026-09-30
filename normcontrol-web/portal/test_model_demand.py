import os
import uuid
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from .models import Batch, LLMRuntime, WorkerRun
from .model_demand import queue_start, pending_demand
from knowledge.models import Scope, NormativeSet, Snapshot, KnowledgeCheck, Command


class ModelDemandTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user('queue-owner')
        self.batch=Batch.objects.create(owner=self.user,name='Synthetic',status='waiting',checks=['logic'])
        self.runtime=LLMRuntime.objects.create(pk=1)
        self.now=timezone.now()

    def test_waiting_work_creates_one_authenticated_start_command(self):
        token='synthetic-token-'+'x'*40
        with patch.dict(os.environ,{'NORMCONTROL_WORKER_TOKEN':token}):
            url='/normcontol/worker/llm/command/'
            self.assertEqual(self.client.get(url).status_code,403)
            a=self.client.get(url,HTTP_AUTHORIZATION='Bearer '+token).json()['command']
            b=self.client.get(url,HTTP_AUTHORIZATION='Bearer '+token).json()['command']
        self.assertEqual(a,b)
        self.assertEqual(a['action'],'start')
        self.assertTrue(a['automatic'])
        self.assertFalse(WorkerRun.objects.exists())

    def test_paused_cancelled_and_archived_batches_do_not_wake(self):
        for state in ('paused','cancelled','completed','prepared'):
            self.batch.status=state;self.batch.save()
            self.assertIsNone(pending_demand())
        self.batch.status='waiting';self.batch.archived=True;self.batch.save()
        self.assertIsNone(pending_demand())

    def test_fresh_online_model_and_pending_manual_command_are_preserved(self):
        self.runtime.sample={'online':True};self.runtime.history=[{'at':self.now.isoformat()}]
        self.assertEqual(queue_start(self.runtime,self.now),{})
        self.runtime.sample={};self.runtime.command={'id':'manual','action':'stop','state':'pending'}
        self.assertEqual(queue_start(self.runtime,self.now),self.runtime.command)

    def test_manual_stop_blocks_old_work_but_new_task_wakes(self):
        self.runtime.command={'action':'stop','state':'done','created':(self.now+timedelta(seconds=1)).isoformat()}
        self.assertEqual(queue_start(self.runtime,self.now),self.runtime.command)
        Batch.objects.filter(pk=self.batch.pk).update(created=self.now+timedelta(seconds=2))
        self.assertEqual(queue_start(self.runtime,self.now)['action'],'start')

    def test_failed_start_has_retry_backoff(self):
        self.runtime.command={'action':'start','state':'failed','finished':self.now.isoformat()}
        self.assertEqual(queue_start(self.runtime,self.now),self.runtime.command)
        self.assertTrue(queue_start(self.runtime,self.now+timedelta(minutes=6))['automatic'])

    def test_stale_online_sample_cannot_block_queue(self):
        self.runtime.sample={'online':True}
        self.runtime.history=[{'at':(self.now-timedelta(minutes=2)).isoformat()}]
        self.assertTrue(queue_start(self.runtime,self.now)['automatic'])

    def test_normative_only_queue_and_native_dependency(self):
        self.batch.status='running';self.batch.save()
        scope=Scope.objects.create(name='Synthetic',kind='project',owner=self.user)
        dataset=NormativeSet.objects.create(name='Synthetic',scope=scope,created_by=self.user)
        snapshot=Snapshot.objects.create(job_id=uuid.uuid4(),owner=self.user,data={},digest='synthetic')
        job=KnowledgeCheck.objects.create(batch=self.batch,owner=self.user,snapshot=snapshot)
        command=Command.objects.create(normative_set=dataset,actor=self.user,kind='review.execute',
            idempotency_key='synthetic',payload={'job_id':str(job.pk)},digest='synthetic')
        self.assertEqual(pending_demand(),'check:'+str(job.pk))
        command.state='delivering';command.save()
        self.assertEqual(pending_demand(),'check:'+str(job.pk))
        job.pause_requested=True;job.save()
        self.assertIsNone(pending_demand())
        job.pause_requested=False;job.save()
        command.payload['after_non_normative']=True;command.save()
        self.assertIsNone(pending_demand())
        WorkerRun.objects.create(batch=self.batch,worker='synthetic',state='completed')
        self.assertEqual(pending_demand(),'check:'+str(job.pk))

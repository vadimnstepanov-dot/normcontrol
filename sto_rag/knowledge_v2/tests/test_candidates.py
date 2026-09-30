import copy,json,unittest
from unittest.mock import patch
from knowledge_v2.candidate_policy import enable,VERSION
from knowledge_v2.review import ledger,release_records,aggregate
from knowledge_v2.trace import links_for
from . import test_review


class CandidateTests(unittest.TestCase):
    add=test_review.ReviewTests.add
    setUp=test_review.ReviewTests.setUp

    def fixture(self):
        manifest,records=release_records(self.store,'release',lambda _:True)
        records['r',1]['payload']['lineage']='r'
        card=records['r',1]['payload']['card']
        card.update(state='candidate',quality=dict(status='candidate',reasons=['semantic_ambiguity','critical_context_incomplete']),
            publication_trust=dict(preliminary_only=True,approval_current=False,blocking_reasons=['semantic_ambiguity','critical_context_incomplete']))
        card['validation']['completeness']['semantic']='needs_review'
        records['policy',1]=dict(kind='publication_policy',payload=dict(effective_refs=[['r',1]]))
        return manifest,records

    def test_opt_in_preserves_limitations_and_default_skips_candidates(self):
        with patch('knowledge_v2.review.release_records',return_value=self.fixture()):
            args=(self.store,'release',['profile'],self.facts[self.did],lambda _:True,lambda *_:True)
            default=ledger(*args);enabled=ledger(*args,include_candidates=True)
        self.assertTrue(default[0]['execution_issues'])
        self.assertFalse(enabled[0]['execution_issues'])
        self.assertTrue(enabled[0]['issues']);self.assertTrue(enabled[0]['preliminary_only'])
        self.assertEqual(enabled[0]['candidate_analysis']['version'],VERSION)
        self.assertFalse(enabled[0]['publication_trust']['approval_current'])

    def test_hard_blocks_and_draft_are_never_waived(self):
        for block in ('condition_unresolved','dependency_unresolved','provenance_unverified','profile_unresolved'):
            fixture=self.fixture();fixture[1]['r',1]['payload']['card']['publication_trust']['blocking_reasons'].append(block)
            with patch('knowledge_v2.review.release_records',return_value=fixture):
                result=ledger(self.store,'release',['profile'],self.facts[self.did],lambda _:True,lambda *_:True,include_candidates=True)
            self.assertIn(block,result[0]['execution_issues'])
        with patch('knowledge_v2.review.release_records',return_value=self.fixture()):
            result=ledger(self.store,'release',['profile'],self.facts[self.did],lambda _:True,lambda *_:True,include_candidates=True,draft_preview=True)
        self.assertNotIn('candidate_analysis',result[0]);self.assertTrue(result[0]['execution_issues'])

    def test_failed_audit_example_and_unverified_quote_are_not_enabled(self):
        for reason in ('context_audit','example_scope','source_gap'):
            fixture=self.fixture();fixture[1]['r',1]['payload']['card']['quality']['reasons'].append(reason)
            with patch('knowledge_v2.review.release_records',return_value=fixture):
                result=ledger(self.store,'release',['profile'],self.facts[self.did],lambda _:True,lambda *_:True,include_candidates=True)
            self.assertNotIn('candidate_analysis',result[0])
        fixture=self.fixture();fixture[1]['r',1]['payload']['card']['validation']['provenance']['status']='failed'
        with patch('knowledge_v2.review.release_records',return_value=fixture):
            result=ledger(self.store,'release',['profile'],self.facts[self.did],lambda _:True,lambda *_:True,include_candidates=True)
        self.assertNotIn('candidate_analysis',result[0])

    def test_verified_contradiction_remains_preliminary_and_policy_is_pinned(self):
        with patch('knowledge_v2.review.release_records',return_value=self.fixture()):
            task=self.runner.create([self.path],{self.did:{'release':['profile']}},self.facts,lambda *_:True,include_candidates=True)
        self.runner.run_once(task);report=self.runner.report(task)
        self.assertEqual(report['snapshot']['versions']['candidate_analysis'],VERSION)
        self.assertEqual(report['candidate_analysis']['applicable'],2)
        self.assertEqual(report['violation_count'],0)
        self.assertTrue(all(d['state']=='unknown' and d['preliminary_violation'] for d in report['decisions']))
        self.assertEqual(len(self.model.calls),2)
        self.assertIn('candidate_analysis',self.model.calls[0]['obligations'][0])

    def test_candidate_link_is_opt_in_and_not_trusted(self):
        manifest,records=self.fixture()
        records['policy',1]['payload']['links']=[dict(id='edge',quality_executable=False,trusted=True,mandatory_target=True,
            endpoint_refs=dict(source=['r',1],target=['r',1],basis=['r',1]))]
        with patch('knowledge_v2.trace.release_records',return_value=(manifest,records)):
            self.assertFalse(links_for(self.store,['release'],lambda _:True))
            links=links_for(self.store,['release'],lambda _:True,include_candidates=True)
        self.assertEqual(len(links),1);self.assertFalse(links[0]['trusted']);self.assertFalse(links[0]['mandatory_target'])

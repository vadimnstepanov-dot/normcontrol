import unittest
from knowledge_v2.glossary import identity,frozen_active


class GlossaryTests(unittest.TestCase):
    def test_source_variants_retained_but_only_selected_definition_effective(self):
        rows=[dict(card=dict(entity_type='definition',term='RPO',glossary_kind='abbreviation'),item=dict(id=x,status='unreviewed')) for x in ('first','second')]
        choices=dict(glossary=[dict(key='abbreviation:rpo',active='second',revision=2)])
        self.assertEqual(frozen_active(choices,rows),{'second'})
        self.assertIsNone(frozen_active({},rows))
        for invalid in ([dict(key='abbreviation:rpo',active='outside',revision=2)],[],choices['glossary']*2):
            with self.assertRaises(ValueError):frozen_active(dict(glossary=invalid),rows)
    def test_identity_does_not_merge_different_types_or_guess_names(self):
        self.assertEqual(identity(dict(term='  RPO ',glossary_kind='symbol'))[0],'symbol:rpo')
        with self.assertRaises(ValueError):identity(dict(term=''))

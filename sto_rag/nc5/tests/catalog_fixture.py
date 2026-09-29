"""Public synthetic catalogue; never used or activated by application code."""
from nc5.common import write


def install(directory):
    version = 'synthetic-test-catalog-v1'
    rule = dict(requirement_id='synthetic-storage', source_id='synthetic',
                source_sha256='synthetic-not-a-published-standard',
                document_name='Синтетический норматив для тестов', clause='1', appendix='А',
                source_locator='p1', source_quote='Описывается порядок хранения сведений.',
                parent_context_refs=[], normative_kind='обязательное требование',
                applicability={'profile': 'test-chtz'}, document_types=['test-chtz'],
                document_scope='template', check_stage='sto', check_method='semantic',
                expected_evidence='Сведения', validation_status='source_exact_semantics_pending')
    profile = dict(id='test-chtz', name='Частное техническое задание', code='ЧТЗ',
                   source_id='synthetic', appendix='А', requirements=[rule['requirement_id']],
                   general=[], expected_sections=[])
    catalog = dict(version=version, profiles=[profile], cards=[rule], sources=[], limitations=[])
    write(directory/'catalogs'/version/'catalog.json', catalog)
    write(directory/'catalog-current.json', {'version': version})
    return catalog

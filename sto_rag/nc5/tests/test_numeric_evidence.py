import unittest
from nc5.facts import validate_value,normalize,contradictions

def fact(value,quote,operator='='):
    return dict(entity='Система',parameter='количество',value=str(value),operator=operator,
                unit='шт',scope='Функция',environment='Общий контур',conditions='',time_basis='',
                evidence=[dict(document='d',locator='p1',quote=quote)])

class NumericEvidence(unittest.TestCase):
    def test_standalone_inflected_cardinals(self):
        for value,quote,operator in [(4,'Система разделяется на четыре основные группы:','='),
            (2,'не менее чем на двух серверных операционных системах','не менее'),
            (3,'одно из трёх состояний: увеличение, уменьшение или отсутствие изменений','∈'),
            (2,'в двух экземплярах: один в docx, второй в pdf','='),
            (2,'Предусмотрено два варианта отображения','='),
            (2,'Показатель может содержать до двух отклонений','≤')]:
            with self.subTest(quote=quote):validate_value(fact(value,quote,operator))

    def test_aliases_preserve_direction_and_still_reject_inversion(self):
        for operator in ('<=','≤','не более'):
            validate_value(fact(7,'не более 7 элементов',operator))
            self.assertEqual(normalize(fact(7,'не более 7 элементов',operator))['operator'],'<=')
        for operator in ('>=','≥','не менее'):
            with self.assertRaises(ValueError):validate_value(fact(7,'не более 7 элементов',operator))
        with self.assertRaises(ValueError):validate_value(fact(2,'не менее чем на двух ОС','≤'))

    def test_no_count_invention_from_evidence_list(self):
        f=fact(2,'«Успех»');f['evidence'].append(dict(document='d',locator='p2',quote='«Ошибка»'))
        with self.assertRaises(ValueError):validate_value(f)

    def test_compounds_and_fractions_are_not_truncated(self):
        for quote in ('двадцать два элемента','двадцати двух элементов','двух сотен элементов',
                      'два миллиона элементов','два с половиной элемента','две целых пять десятых',
                      'две тысячи элементов','два тыс. элементов'):
            with self.subTest(quote=quote),self.assertRaises(ValueError):validate_value(fact(2,quote))

    def test_word_and_digit_wrong_values_still_fail(self):
        for quote in ('четыре группы','14 групп'):
            with self.subTest(quote=quote),self.assertRaises(ValueError):validate_value(fact(3,quote))

    def test_advisory_bound_does_not_become_mandatory_conflict(self):
        advisory=normalize(fact(7,'рекомендуется отображать не более 7 элементов','≤'))
        lower=normalize(fact(8,'не менее 8 элементов','≥'))
        self.assertEqual(contradictions([advisory,lower]),[])
        hard=normalize(fact(7,'не более 7 элементов','≤'))
        self.assertEqual(len(contradictions([hard,lower])),1)

    def test_conditions_remain_separate(self):
        upper=normalize(fact(7,'не более 7 элементов','≤'))
        lower=normalize({**fact(8,'не менее 8 элементов','≥'),'conditions':'другой режим'})
        self.assertEqual(contradictions([upper,lower]),[])

if __name__=='__main__':unittest.main()

"""Independent approved catalogue contract, not derived from runtime constants."""
import unittest
import equipment_catalog as catalog

EXPECTED = {
    'excavators': ('Экскаваторы', ('мини-экскаватор', 'гусеничный экскаватор', 'колёсный экскаватор', 'экскаватор-погрузчик', 'экскаватор')),
    'loaders': ('Погрузчики', ('мини-погрузчик', 'фронтальный погрузчик', 'телескопический погрузчик', 'вилочный погрузчик')),
    'cranes': ('Краны и подъёмники', ('автокран', 'манипулятор', 'автовышка', 'мини-кран «Паук»')),
    'trucks': ('Грузовая техника', ('самосвал', 'трал', 'лесовоз', 'ломовоз', 'контейнеровоз', 'автовоз', 'эвакуатор')),
    'road': ('Дорожная техника', ('дорожный каток', 'асфальтоукладчик', 'дорожная фреза', 'автогрейдер', 'автогудронатор')),
    'municipal': ('Коммунальная техника', ('поливомоечная машина', 'подметальная машина', 'илосос', 'ассенизатор', 'снегоуборочная машина', 'КДМ')),
    'concrete': ('Бетонная техника', ('автобетононасос', 'автобетоносмеситель', 'стационарный бетононасос')),
    'drilling': ('Буровая техника', ('буровая установка', 'ямобур', 'установка ГНБ')),
    'transport': ('Перевозки и вывоз', ('вывоз грунта', 'вывоз строительного мусора', 'перевозка спецтехники', 'доставка материалов', 'вывоз снега')),
    'other': ('Другая техника', ('другая техника',)),
}

class CatalogContractTests(unittest.TestCase):
    def test_phone_truncated_groups_and_long_types_get_their_own_row(self):
        labels=[name for name,_ in catalog.GROUPS]
        rows=catalog.button_rows(labels,lambda index,text:(index,text))
        for group in ('Краны и подъёмники','Коммунальная техника'):
            index=labels.index(group)
            row=next(row for row in rows if any(button[0]==index for button in row))
            self.assertEqual(row,[(index,catalog.DISPLAY_LABELS[group])])
        self.assertEqual([i for row in rows for i,_ in row],list(range(10)))
        self.assertTrue(any(len(row)==2 for row in rows))
        for _,kinds in catalog.GROUPS:
            rows=catalog.button_rows(kinds,lambda index,text:(index,text))
            for row in rows:
                if any(len(kinds[index])>16 for index,_ in row):
                    self.assertEqual(len(row),1)

    def test_exact_group_order_count_and_unique_positions(self):
        self.assertEqual(tuple(name for name, _ in catalog.GROUPS), tuple(name for name, _ in EXPECTED.values()))
        positions = [kind for _, kinds in catalog.GROUPS for kind in kinds]
        self.assertEqual(len(positions), 43)
        self.assertEqual(len(set(positions)), 43)
        self.assertEqual(catalog.LEGACY_ALIASES['каток'], 'дорожный каток')


def group_test(expected):
    def test(self):
        name, kinds = expected
        self.assertEqual(dict(catalog.GROUPS)[name], kinds)
        for kind in kinds:
            self.assertIn(kind, catalog.TYPES)
            self.assertTrue(catalog.DISPLAY_LABELS[kind].endswith(kind[0].upper() + kind[1:]))
    return test

for key, expected in EXPECTED.items():
    setattr(CatalogContractTests, 'test_approved_' + key, group_test(expected))

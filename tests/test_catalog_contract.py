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

"""Shared ordered catalogue. Stored type names also serve as matching keys."""

OTHER = "другая техника"
GROUPS = (
    ("Экскаваторы", ("мини-экскаватор", "гусеничный экскаватор", "колёсный экскаватор",
                    "экскаватор-погрузчик", "экскаватор")),
    ("Погрузчики", ("мини-погрузчик", "фронтальный погрузчик", "телескопический погрузчик",
                   "вилочный погрузчик")),
    ("Краны и подъёмники", ("автокран", "манипулятор", "автовышка", "мини-кран «Паук»")),
    ("Грузовая техника", ("самосвал", "трал", "лесовоз", "ломовоз", "контейнеровоз", "автовоз", "эвакуатор")),
    ("Дорожная техника", ("дорожный каток", "асфальтоукладчик", "дорожная фреза",
                         "автогрейдер", "автогудронатор")),
    ("Коммунальная техника", ("поливомоечная машина", "подметальная машина", "илосос",
                             "ассенизатор", "снегоуборочная машина", "КДМ")),
    ("Бетонная техника", ("автобетононасос", "автобетоносмеситель", "стационарный бетононасос")),
    ("Буровая техника", ("буровая установка", "ямобур", "установка ГНБ")),
    ("Перевозки и вывоз", ("вывоз грунта", "вывоз строительного мусора", "перевозка спецтехники",
                         "доставка материалов", "вывоз снега")),
    ("Другая техника", (OTHER,)),
)
GROUP_ICONS = ("🚜", "🚜", "🏗", "🚛", "🛣", "🧹", "🧱", "🛠", "🚚", "⚙️")
DISPLAY_LABELS = {}
for (group, kinds), icon in zip(GROUPS, GROUP_ICONS, strict=True):
    DISPLAY_LABELS[group] = f"{icon} {group}"
    for kind in kinds:
        DISPLAY_LABELS[kind] = f"{icon} {kind[0].upper() + kind[1:]}"

LEGACY_ALIASES = {"каток": "дорожный каток"}
TYPES = frozenset(kind for _, kinds in GROUPS for kind in kinds) | LEGACY_ALIASES.keys()


def button_rows(labels, button):
    rows = []
    pair = []
    for index, label in enumerate(labels):
        if len(label) > 21 and pair:
            rows.append(pair)
            pair = []
        pair.append(button(index, DISPLAY_LABELS.get(label, label[0].upper() + label[1:])))
        if len(pair) == 2 or len(label) > 21:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    return rows

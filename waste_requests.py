"""Vocabulary and validation for the waste-collection request form."""
import re
from decimal import Decimal

CATEGORY = "Вывоз грунта, отходов и снега"
DIRECTIONS = {
    "soil": "Вывоз грунта",
    "construction": "Вывоз строительных отходов",
    "snow": "Вывоз снега",
}
MATERIALS = {
    "soil": ("Грунт", "Песок", "Глина", "Грунт с примесями", "Состав уточняется"),
    "construction": (
        "Бетон и кирпич", "Асфальт", "Древесина", "Смешанные стройотходы",
        "Другой состав", "Состав уточняется",
    ),
    "snow": ("Снег", "Снег с примесями", "Состав уточняется"),
}
SERVICES = {
    "transport": "Только перевозка",
    "container": "Контейнер",
    "full": "Комплексный вывоз с погрузкой",
}
LOADING = ("Погрузка нужна", "Погрузка есть", "Погрузку уточним")
HAZARD_CLASSES = ("I", "II", "III", "IV", "V")
UNKNOWN = "Не знаю"
NO_DESTINATION = "Место приёма уточним"
NO_DETAILS = "Без дополнительных условий"


def to_thousandths(value: str | None) -> int | None:
    """Parse m³ or tonnes to integer litres or kilograms."""
    if value is None or not re.fullmatch(r"[0-9]{1,7}(?:[.,][0-9]{1,3})?", value.strip()):
        return None
    amount = int(Decimal(value.strip().replace(",", ".")) * 1000)
    return amount or None


def format_amount(value: int) -> str:
    units, fraction = divmod(value, 1000)
    return f"{units},{fraction:03d}".rstrip("0") if fraction else str(units)


async def write(db, lead_id, data):
    await db.execute(
        """INSERT INTO waste_lead_details
           (lead_id, kind, material, hazard_class, service, volume_liters,
            mass_kg, loading, destination)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            lead_id, data["waste_kind"], data["waste_material"], data["waste_hazard"],
            data["waste_service"], data["waste_volume"], data["waste_mass"],
            data["waste_loading"], data["waste_destination"],
        ),
    )

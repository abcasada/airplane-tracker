"""Aircraft size class from the ADS-B emitter category, used for visibility distances."""

SIZE_CLASSES = ["large", "medium", "light"]
SIZE_BY_CATEGORY = {
    "A3": "large", "A4": "large", "A5": "large", "A2": "medium", "A6": "medium",
    "A1": "light", "A7": "light", "B1": "light", "B2": "medium", "B4": "light", "B6": "light",
}  # fmt: skip


def size_for(category: str | None) -> str:
    return SIZE_BY_CATEGORY.get(category or "", "medium")

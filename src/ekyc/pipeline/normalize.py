import unicodedata

from unidecode import unidecode


def fold(text: str) -> str:
    return " ".join(unidecode(text).lower().replace("'", "").replace("`", "").split())


def fold_aligned(text: str) -> str:
    return "".join(
        (unidecode(char) or " ")[0].lower() for char in unicodedata.normalize("NFC", text)
    )

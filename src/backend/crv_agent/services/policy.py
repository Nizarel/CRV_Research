import re
import unicodedata

_HAZARD = re.compile(r"\b(cyanur\w*|cyanid\w*|nacn|h2s)\b")
_INSTRUCTION = re.compile(
    r"\b(comment|how|procedure|protocol\w*|prepar\w*|manipul\w*|ppe|epi|dos\w*)\b",
)


def hazardous_instructions(question: str) -> bool:
    text = "".join(
        char for char in unicodedata.normalize("NFKD", question.casefold())
        if not unicodedata.combining(char)
    )
    return bool(_HAZARD.search(text) and _INSTRUCTION.search(text))

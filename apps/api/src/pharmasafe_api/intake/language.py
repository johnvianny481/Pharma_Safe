"""Language detection for report text, without external models.

Two signals, both deterministic:

1. **Script.** Text mostly in Devanagari, Cyrillic, Greek, Arabic, Hangul or Japanese
   kana is identified by its script. Han characters without kana are read as Chinese.
2. **Function words.** For Latin-script text, each language scores one point per token
   found in its list of very common words ("the", "und", "le", "el", ...). These words
   carry no personal data and are the same in clinical and consumer writing.

Short or mixed text returns ``und`` (undetermined, as in BCP 47) instead of a guess.
Production can put a hosted detector (e.g. Azure AI Language) behind the same function.
"""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

UNDETERMINED = "und"
MIN_WORD_HITS = 3
MIN_MARGIN = 0.2

_STOPWORDS: dict[str, frozenset[str]] = {
    lang: frozenset(words.split())
    for lang, words in {
        "en": "the and of to in is was with for on that it he she after his her patient "
              "had has have not this were be at by from took taking",
        "de": "der die das und ist nicht ein eine mit von zu den dem des auf für sich nach "
              "wurde hat bei patient patientin nahm",
        "fr": "le la les et est une un des du de pas avec pour dans sur après il elle a "
              "été patient patiente au aux",
        "es": "el la los las y es una un del de con para en por que se no tras paciente "
              "fue tomó presentó",
        "it": "il lo la gli le e è una un del della di con per che non dopo paziente ha "
              "stato stata nel",
        "pt": "o a os as e é uma um do da de com para em que não após paciente foi tomou "
              "apresentou no na",
        "nl": "de het een en is van met voor op niet na patiënt heeft werd zijn bij ook",
        "sv": "och är en ett av med för på inte efter patienten har var att det som",
        "pl": "i w z na się nie jest do po że pacjent pacjentka był była ma od",
    }.items()
}  # fmt: skip

_SCRIPTS = (
    ("DEVANAGARI", "hi"),
    ("CYRILLIC", "ru"),
    ("GREEK", "el"),
    ("ARABIC", "ar"),
    ("HANGUL", "ko"),
    ("HIRAGANA", "ja"),
    ("KATAKANA", "ja"),
    ("CJK", "zh"),
)

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


@dataclass(frozen=True)
class Detection:
    language: str
    confidence: float


def detect_language(text: str) -> Detection:
    by_script = _detect_script(text)
    if by_script is not None:
        return by_script
    words = [w.lower() for w in _WORD.findall(text)]
    scores = Counter[str]()
    for word in words:
        for lang, stopwords in _STOPWORDS.items():
            if word in stopwords:
                scores[lang] += 1
    ranked = scores.most_common(2)
    if not ranked or ranked[0][1] < MIN_WORD_HITS:
        return Detection(UNDETERMINED, 0.0)
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    margin = (best_score - runner_up) / best_score
    if margin < MIN_MARGIN:
        return Detection(UNDETERMINED, round(margin, 2))
    return Detection(best, round(min(1.0, margin), 2))


def _detect_script(text: str) -> Detection | None:
    counts = Counter[str]()
    letters = 0
    for char in text:
        if not char.isalpha():
            continue
        letters += 1
        name = unicodedata.name(char, "")
        for prefix, lang in _SCRIPTS:
            if name.startswith(prefix):
                counts[lang] += 1
                break
    if not letters or not counts:
        return None
    # Japanese mixes kana with Han characters; any kana means Japanese.
    if counts["ja"]:
        counts["ja"] += counts.pop("zh", 0)
    lang, count = counts.most_common(1)[0]
    share = count / letters
    if share < 0.5:
        return None
    return Detection(lang, round(share, 2))

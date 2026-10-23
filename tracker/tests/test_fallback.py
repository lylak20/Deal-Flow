import pytest

from collector import Article
from extractor import Extraction, FallbackExtractor, QuotaExhausted

ARTICLE = Article("https://example.com/a", "TechCrunch", "t", "", None)
OK = Extraction(is_funding_announcement=False, events=[])


class Fake:
    def __init__(self, fail: bool):
        self.fail, self.calls = fail, 0

    def extract(self, article, text):
        self.calls += 1
        if self.fail:
            raise QuotaExhausted("limit")
        return OK


def test_switches_to_fallback_and_stays_there():
    claude, gemini = Fake(fail=True), Fake(fail=False)
    x = FallbackExtractor([("claude", claude), ("gemini", gemini)])
    assert x.extract(ARTICLE, "") is OK
    assert x.extract(ARTICLE, "") is OK
    # Claude is not retried for every article once its quota is gone.
    assert (claude.calls, gemini.calls) == (1, 2)


def test_raises_when_every_provider_is_exhausted():
    x = FallbackExtractor([("claude", Fake(fail=True)), ("gemini", Fake(fail=True))])
    with pytest.raises(QuotaExhausted):
        x.extract(ARTICLE, "")


def test_other_errors_do_not_trigger_fallback():
    class Broken:
        def extract(self, article, text):
            raise RuntimeError("bad article")

    gemini = Fake(fail=False)
    x = FallbackExtractor([("claude", Broken()), ("gemini", gemini)])
    with pytest.raises(RuntimeError):
        x.extract(ARTICLE, "")
    assert gemini.calls == 0

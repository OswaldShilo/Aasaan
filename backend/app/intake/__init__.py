"""Layer 1, Intake: what the learner gave -> one clean, validated `source_pack`. Nothing else (no outline, no resource search, no teaching)."""
from .extract import BasicExtractor, IntakeInput, RawPage, RawSource  # noqa: F401
from .pack import build_source_pack  # noqa: F401
from .schema import SourcePack  # noqa: F401

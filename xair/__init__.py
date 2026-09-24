"""XAIR reference runtime — execution-time validation of AIS action intents."""

__version__ = "1.0.1"

from xair.core.models import ActionIntent, DecisionOutcome, IntentState  # noqa: E402
from xair.core.runtime import XAIRRuntime  # noqa: E402

__all__ = ["ActionIntent", "DecisionOutcome", "IntentState", "XAIRRuntime", "__version__"]

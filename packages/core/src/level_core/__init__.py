"""Level core library.

Contains everything that isn't a deployable surface: agents, memory bank,
guardrails, model clients, observability, bias taxonomy, identity, and the
Agent Gateway. Both the API (`level_api`) and the Cloud Run Jobs
(`level_jobs`) depend on this package.
"""

from level_core.config import Settings, get_settings

__all__ = ["Settings", "get_settings"]
__version__ = "0.1.0"

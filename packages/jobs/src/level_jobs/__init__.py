"""Level's Cloud Run Jobs — long-running async workers.

Each module in this package is a distinct Cloud Run Job. They share a
common bootstrap pattern (configure observability, register agents, run,
exit) via :mod:`level_jobs.base`.
"""

__version__ = "0.1.0"

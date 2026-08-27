"""Demo ICS fixtures packaged with ``level_core`` so ``importlib.resources``
can find them regardless of how the wheel is installed.

The canonical human-readable copies live at ``<repo>/example-data/`` and are
kept in sync at build time. The runtime always loads from HERE - see
``level_core.demo.scenarios.ScenarioConfig.ics_path``.
"""

"""Keep tests away from the user's configuration, cache and session bus."""
from __future__ import annotations

import os

from blueferry_plugin_kit.testing import isolate_environment

# No test may reach a real bus: the plugin is exercised in-process.
isolate_environment("blueferry-calendar-tests-", bus_name="blueferry-calendar-tests")

# The card and message assertions are in English; two tests switch to German.
for _variable in ("LC_ALL", "LC_MESSAGES", "LANGUAGE"):
    os.environ.pop(_variable, None)
os.environ["LANG"] = "C.UTF-8"

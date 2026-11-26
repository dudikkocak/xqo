"""xqo-agency: `xqo agency …` imports and updates agents from the agency-agents library into department files.
CLI only: it adds nothing to a running HQ."""
from __future__ import annotations

from xqo.api import Plugin

__version__ = "3.2.0"


class AgencyPlugin(Plugin):
    name = "agency"
    description = "`xqo agency`: import agents from the agency-agents library"
    config_files = ("agency.json", "agency.lock.json", "AGENCY_AGENTS_LICENSE")

    @classmethod
    def add_cli(cls, sub, url) -> list[str]:
        from . import importer
        g = sub.add_parser("agency", help="import and update agents from the agency-agents library")
        importer.add_arguments(g)
        return ["agency"]

    @classmethod
    def run_cli(cls, a) -> None:
        from . import importer
        importer.main(a)

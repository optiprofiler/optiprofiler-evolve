from __future__ import annotations

import unittest

from test_config import minimal_config

from optiprofiler_evolve.config import load_config
from optiprofiler_evolve.observability.provenance import build_run_provenance


class ProvenanceTests(unittest.TestCase):
    def test_capture_uses_supplied_identities_without_component_resolution(self):
        components = {"phases": [{"name": "owner_stage", "callable": "owner:Stage"}]}
        templates = {"solver_worker": "owner-prompt/3"}
        result = build_run_provenance(
            load_config(minimal_config()), run_id="test-run",
            components=components, prompt_templates=templates,
        )
        self.assertEqual(result["components"], components)
        self.assertEqual(result["prompt_templates"], templates)
        self.assertEqual(result["run_id"], "test-run")
        self.assertRegex(result["config_hash"], r"^[0-9a-f]{64}$")

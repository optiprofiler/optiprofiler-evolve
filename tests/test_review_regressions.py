import json
import tempfile
import unittest
from pathlib import Path

import jsonschema
from test_config import minimal_config
from test_engine import FakeEvaluator, fake_agent_runner, fake_evaluator_factory
from worker_helpers import RunnerAdapter

from optiprofiler_evolve._presets import dfo_default
from optiprofiler_evolve.agents.docker_runtime import _read_gateway_usage
from optiprofiler_evolve.application.run import EvolutionRun
from optiprofiler_evolve.config import AssessmentConfig, CriterionConfig, load_config
from optiprofiler_evolve.observability.usage import UsageRecord, aggregate_usage
from optiprofiler_evolve.observability.views.owner import _attempt_review_section
from optiprofiler_evolve.solver import InterfaceSpec


class ReviewRegressionTests(unittest.TestCase):
    def test_explicit_null_gateway_is_valid_in_schema_and_loader(self):
        raw = minimal_config()
        raw['workers']['pool'][0]['provider_gateway'] = None
        schema = json.loads((Path(__file__).parents[1] / 'config.schema.json').read_text())
        jsonschema.Draft202012Validator(schema).validate(raw)
        self.assertIsNone(load_config(raw).workers.pool[0].provider_gateway)

    def test_numeric_token_options_survive_redaction(self):
        raw = minimal_config()
        raw['assessment']['criteria']['custom'] = {'enabled': False, 'options': {'max_tokens': 1234}}
        options = load_config(raw).redacted_dict()['assessment']['criteria']['custom']['options']
        self.assertEqual(options['max_tokens'], 1234)

    def test_preset_and_mapping_defaults_preserve_explicit_overrides(self):
        self.assertEqual(dfo_default().assessment.criteria['integrity'].weight, 0)
        config = AssessmentConfig(criteria={'complexity': {'enabled': True}, 'integrity': {}})
        self.assertEqual(config.criteria['complexity'].mode, 'warn')
        self.assertEqual(config.criteria['integrity'].weight, 0)
        explicit = CriterionConfig(weight=1, mode='enforce')
        self.assertIs(AssessmentConfig(criteria={'integrity': explicit}).criteria['integrity'], explicit)

    def test_execution_and_data_are_validated_without_performance(self):
        for section, key, value in (
            ('execution', 'backend', 'dockr'), ('execution', 'feedback_mode', 'nonsense'),
            ('execution', 'cpus', 0), ('execution', 'pids_limit', 1),
            ('data', 'library', 'not a lib!'),
        ):
            with self.subTest(key=key):
                raw = minimal_config()
                raw['assessment']['criteria']['performance']['enabled'] = False
                raw['assessment']['criteria']['complexity']['enabled'] = True
                raw[section][key] = value
                with self.assertRaises(ValueError):
                    load_config(raw)

    def test_empty_optional_hidden_preserves_validation_score(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'solver.py'
            source.write_text('def solver(fun, x0):\n    return x0\n')
            raw = minimal_config()
            raw['data']['split']['hidden_fraction'] = 0
            FakeEvaluator.calls = []
            result = EvolutionRun(
                initial=source, interface=InterfaceSpec.parse('solver.py:solver'),
                runtime='python', editable=('solver.py',), config=load_config(raw),
                run_dir=root / 'run', worker_adapter=RunnerAdapter(fake_agent_runner),
                evaluator_factory=fake_evaluator_factory,
            ).run()
            self.assertEqual(result.final_score, 0.68)
            self.assertNotIn('hidden', FakeEvaluator.calls)

    def test_gateway_bad_line_preserves_known_usage_as_a_lower_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audit.jsonl'
            good = {'path': '/v1/responses', 'usage': {'input_tokens': 100, 'output_tokens': 20}}
            path.write_text(json.dumps(good) + '\n{truncated\n')
            usage = _read_gateway_usage(path, request_count=2)
            self.assertEqual(usage.calls, 2)
            self.assertIsNone(usage.input_tokens)
            self.assertEqual(usage.lower_bounds['input_tokens'], 100)
            total = aggregate_usage([usage, UsageRecord(source='other', input_tokens=7)])
            self.assertIsNone(total.input_tokens)
            self.assertEqual(total.lower_bounds['input_tokens'], 107)

    def test_owner_gate_uses_the_matching_profile_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = {}
            for profile, verdict in [('validation', 'approve'), ('public', 'quarantine')]:
                path = root / f'{profile}.json'
                path.write_text(json.dumps({'verdict': verdict, 'summary': f'{profile}-decision'}))
                reports[profile] = str(path)
            html = _attempt_review_section(
                {'gate': 'quarantined', 'gate_profile': 'public', 'review_reports': reports},
                root, root, 'candidate',
            )
            self.assertIn('public-decision', html)
            self.assertNotIn('validation-decision', html)

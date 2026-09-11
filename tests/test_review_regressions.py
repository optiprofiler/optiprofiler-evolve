import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

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
    def test_profile_cache_is_derived_and_never_survives_a_config_change(self):
        raw = minimal_config()
        raw['assessment']['criteria']['performance']['options']['benchmark'] = {'max_eval_factor': 100}
        config = load_config(raw)
        before = config.resolved_dict()
        profiles = config.resolved_profiles
        self.assertIs(profiles, config.resolved_profiles)
        self.assertIs(profiles['public'].criteria, config.assessment.resolve('public'))
        self.assertEqual(config.resolved_dict(), before)
        with self.assertRaises(TypeError):
            profiles['public'].criteria['performance'].options['new'] = True
        derived = config.with_criterion('performance', options={'benchmark': {'max_eval_factor': 200}})
        self.assertIsNot(derived.resolved_profiles, profiles)
        self.assertEqual(config.reference_max_eval_factor, 100)
        self.assertEqual(derived.reference_max_eval_factor, 200)
        self.assertEqual(derived.resolved_profiles['public'].performance.benchmark['max_eval_factor'], 200)
        disabled = replace(config, assessment=AssessmentConfig(criteria={
            'performance': {'enabled': False, 'options': {'unknown': 'unused'}},
            'complexity': {'enabled': True},
        }))
        disabled.validate()
        self.assertTrue(all(profile.performance is None for profile in disabled.resolved_profiles.values()))

    def test_smoke_overrides_do_not_change_the_materialized_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'solver.py'
            source.write_text('def solver(fun, x0):\n    return x0\n')
            hashes = []
            for index, smoke in enumerate(({'benchmark': {'max_eval_factor': 5}},
                                            {'benchmark': {'max_eval_factor': 11}}, None)):
                raw = minimal_config()
                raw['workflow'] = {'phases': [{'name': 'prepare'}]}
                raw['assessment']['criteria']['performance']['options'] = {
                    'reference': 'scipy_powell', 'benchmark': {'max_eval_factor': 100},
                }
                raw['assessment']['profiles'] = {'smoke': {'performance': {
                    'enabled': smoke is not None, 'options': smoke or {},
                }}}
                config = load_config(raw)
                run_dir = root / f'run-{index}'
                EvolutionRun(initial=source, interface=InterfaceSpec.parse('solver.py:solver'),
                             runtime='python', editable=('solver.py',), config=config,
                             run_dir=run_dir, worker_adapter=RunnerAdapter(fake_agent_runner),
                             evaluator_factory=fake_evaluator_factory).run()
                manifest = json.loads((run_dir / 'controller/reference_manifest.json').read_text())
                self.assertEqual(manifest['max_eval_factor'], 100)
                hashes.append(manifest['tree_hash'])
                namespace = {}
                with patch('scipy.optimize.minimize') as minimize:
                    exec((run_dir / 'controller/reference/solver.py').read_text(), namespace)
                    minimize.return_value.x = [0.0, 0.0]
                    namespace['solver'](lambda x: 0.0, [1.0, 1.0])
                self.assertEqual(minimize.call_args.kwargs['options']['maxfev'], 200)
                self.assertEqual(config.resolved_profiles['validation'].performance.benchmark['max_eval_factor'], 100)
            self.assertEqual(len(set(hashes)), 1)

    def test_provenance_records_all_enabled_review_profiles_and_workers(self):
        raw = minimal_config()
        raw['workflow'] = {'phases': [{'name': 'prepare'}]}
        raw['assessment']['profiles'] = {
            profile: {'integrity': {'enabled': True, 'options': {
                'component': {'name': 'agent_integrity'}, 'allow_unsafe_stub': False,
                'worker': {'harness': 'claude', 'model': f'{profile}-reviewer'},
            }}}
            for profile in ('public', 'hidden')
        }
        raw['assessment']['profiles']['validation'] = {'integrity': {'enabled': False}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'solver.py'
            source.write_text('def solver(fun, x0):\n    return x0\n')
            adapter = RunnerAdapter(fake_agent_runner)
            with patch.object(adapter, 'provenance', wraps=adapter.provenance) as provenance:
                EvolutionRun(initial=source, interface=InterfaceSpec.parse('solver.py:solver'),
                             runtime='python', editable=('solver.py',), config=load_config(raw),
                             run_dir=root / 'run', worker_adapter=adapter,
                             evaluator_factory=fake_evaluator_factory).run()
            workers = provenance.call_args.args[0]
            self.assertEqual({worker.model for worker in workers},
                             {'test-model', 'public-reviewer', 'hidden-reviewer'})
            components = json.loads((root / 'run/provenance.json').read_text())['components']
            self.assertNotIn('integrity_reviewer', components)
            reviewers = components['integrity_reviewers']
            self.assertEqual(set(reviewers), {'public', 'hidden'})
            for profile, reviewer in reviewers.items():
                self.assertEqual(reviewer['name'], 'agent_integrity')
                self.assertEqual(reviewer['worker'], {'harness': 'claude', 'model': f'{profile}-reviewer'})

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

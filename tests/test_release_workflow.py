"""Искусственные примеры только для проверки кода; без живых аккаунтов и запусков LLM."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bamboo import analytics as a, core, publishing as p
from bamboo.advice import workspace_readiness
from bamboo.cli import main, parser
from bamboo.core import BambooError, config, file_digest, init, read_json, snapshot, write_json
from bamboo.install import SYSTEMS, install
from test_bamboo import Fixture, ROOT


class ReliabilityTests(Fixture):
    def row(self, day='2026-09-21', **kw):
        return {'date': day, 'source': 'google', 'grain': 'page', 'page': 'https://example.org/a',
                'query': '', 'impressions': 100, 'clicks': 5, 'position': 8, **kw}

    def test_query_hint_update_replaces_observation(self):
        a.ingest(self.root, [self.row(source='yandex', grain='query', query='тяван')])
        a.ingest(self.root, [self.row(source='yandex', grain='query', query='тяван', page='https://example.org/b', clicks=8)])
        result = a.report(self.root, '2026-09-21', 1)
        self.assertEqual(result['queries'][0]['current']['clicks'], 8)
        self.assertEqual(result['queries'][0]['current']['impressions'], 100)
        self.assertEqual(result['queries'][0]['page_hint'], 'https://example.org/b')

    def test_duplicate_query_with_different_hints_in_one_batch_rejected(self):
        with self.assertRaises(BambooError):
            a.ingest(self.root, [self.row(grain='query', query='тяван'), self.row(grain='query', query='тяван', page='https://example.org/b')])
        self.assertFalse((self.root/'analytics/metrics.sqlite3').exists())

    def test_legacy_hint_duplicates_are_not_guessed_or_summed(self):
        a.ingest(self.root, [self.row(source='yandex', grain='query', query='тяван')])
        db = a.connect(self.root)
        with db:
            db.execute("INSERT INTO metrics SELECT date,source,grain,'https://example.org/b',query,impressions,clicks,position,product_clicks,leads,orders,revenue,cost FROM metrics")
        db.close()
        result = a.report(self.root, '2026-09-21', 1)
        self.assertEqual(result['excluded_ambiguous_queries'], 1)
        self.assertEqual(result['queries'], [])
        self.assertTrue(result['warnings'])

    def test_null_clicks_suppress_ctr_and_growth(self):
        a.ingest(self.root, [self.row('2026-09-18'), self.row('2026-09-19'), self.row('2026-09-20'), self.row(clicks=None)])
        current = a.report(self.root, '2026-09-21', 2)['pages'][0]
        self.assertIsNone(current['current']['ctr'])
        self.assertIsNone(current['click_change_fraction'])
        self.assertFalse(current['current']['metric_coverage']['clicks']['complete'])
        self.assertEqual(current['current']['clicks'], 5)

    def test_cluster_does_not_mix_grains_or_zero_fill(self):
        a.ingest(self.root, [self.row(grain='page_query', query='тяван', clicks=None, impressions=None),
                             self.row(grain='query', query='тяван', clicks=None, impressions=None)])
        clusters = a.report(self.root, '2026-09-21', 1)['query_clusters']
        self.assertEqual(len(clusters), 2)
        self.assertTrue(all(x['clicks'] is None and x['impressions'] is None and x['ctr'] is None for x in clusters))

    def test_funnel_requires_same_measured_dates(self):
        a.ingest(self.root, [self.row(), self.row('2026-09-20', grain='conversion', clicks=None, impressions=None, orders=2)])
        self.assertEqual(a.report(self.root, '2026-09-21', 2)['funnels'], [])

    def test_report_default_end_and_no_database_mutation(self):
        a.ingest(self.root, [self.row()])
        db_path = self.root/'analytics/metrics.sqlite3'
        before = file_digest(db_path)
        result = a.report(self.root)
        self.assertEqual(result['current'][1], '2026-09-21')
        self.assertEqual(result['latest_dates']['google'], '2026-09-21')
        self.assertEqual(before, file_digest(db_path))

    def test_empty_report_no_database_and_no_made_up_topics(self):
        result = a.report(self.root)
        self.assertEqual(result['opportunities']['actions'], [])
        self.assertEqual(result['opportunities']['status'], 'no_data')
        self.assertFalse((self.root/'analytics/metrics.sqlite3').exists())

    def test_bad_numeric_and_date_types_rejected(self):
        for row in (self.row(clicks=True), self.row(page=['invalid']), self.row(day='20260921')):
            with self.subTest(row=row), self.assertRaises((BambooError, ValueError)):
                a.ingest(self.root, [row])

    def test_yandex_atomic_rollback(self):
        path = self.root/'yandex.csv'
        path.write_text('Date,Host,URL,Query,Region,Clicks,Impressions,Position\n2026-09-21,example.org,https://example.org/a,тяван,Moscow,2,100,5\n', encoding='utf-8')
        a.import_yandex_enhanced_csv(self.root, path)
        path.write_text(path.read_text().replace(',2,100,5', ',9,150,6'), encoding='utf-8')
        with patch('bamboo.analytics._upsert', side_effect=BambooError('test rollback')):
            with self.assertRaises(BambooError):
                a.import_yandex_enhanced_csv(self.root, path)
        db = a.connect(self.root)
        self.assertEqual(db.execute('SELECT clicks FROM yandex_enhanced').fetchone()[0], 2)
        self.assertEqual(db.execute('SELECT clicks FROM metrics').fetchone()[0], 2)
        db.close()

    def test_duplicate_csv_headers_and_truncated_rows_rejected(self):
        for content in ('Date,Date,Host,URL,Query,Region,Clicks,Impressions,Position\n',
                        'Date,Host,URL,Query,Region,Clicks,Impressions,Position\n2026-09-21,example.org\n'):
            path = self.root/'bad.csv'; path.write_text(content, encoding='utf-8')
            with self.assertRaises(BambooError):
                a.import_yandex_enhanced_csv(self.root, path)
        self.assertFalse((self.root/'analytics/metrics.sqlite3').exists())

    def test_partial_evidence_does_not_trigger_opportunity(self):
        self.good()
        self.change('brief.json', lambda d: d['seo'].update(page_type='article', target_url='https://example.org/a'))
        a.ingest(self.root, [self.row(grain='page_query', query='купить тяван', impressions=300)])
        result = a.report(self.root, '2026-09-21', 7)
        self.assertEqual(result['opportunities']['status'], 'insufficient_data')
        self.assertEqual(result['opportunities']['actions'], [])

    def test_opportunity_has_real_evidence_and_does_not_mutate_pack(self):
        self.good()
        self.change('brief.json', lambda d: d['seo'].update(page_type='article', target_url='https://example.org/a'))
        a.ingest(self.root, [self.row(grain='page_query', query='купить тяван', impressions=300)])
        before = file_digest(self.job/'pack.json')
        result = a.report(self.root, '2026-09-21', 1)
        action = result['opportunities']['actions'][0]
        self.assertEqual(action['target_url'], 'https://example.org/a')
        self.assertEqual(action['evidence']['source'], 'google')
        self.assertEqual(action['evidence']['query'], 'купить тяван')
        self.assertTrue(action['human_decision_required'])
        self.assertEqual(before, file_digest(self.job/'pack.json'))

    def test_opportunity_product_link_requires_confirmed_passport(self):
        self.good('article', photo=True)
        self.change('brief.json', lambda d: d['seo'].update(page_type='article', target_url='https://example.org/a'))
        product = read_json(self.root/'content/products/bowl-01.json')
        product['product_url'] = 'https://example.org/product/bowl-01'
        write_json(self.root/'content/products/bowl-01.json', product)
        a.ingest(self.root, [self.row(grain='page_query', query='как выбрать тяван', impressions=300)])
        result = a.report(self.root, '2026-09-21', 1)
        action = result['opportunities']['actions'][0]
        self.assertEqual(action['kind'], 'review_product_link')
        self.assertEqual(action['product_ids'], ['bowl-01'])
        product['confirmed'] = False
        write_json(self.root/'content/products/bowl-01.json', product)
        self.assertEqual(a.report(self.root, '2026-09-21', 1)['opportunities']['actions'], [])

    def test_doctor_is_read_only_and_readme_not_voice(self):
        before = sorted(str(x.relative_to(self.root)) for x in self.root.rglob('*'))
        with patch.dict(os.environ, {}, clear=True), patch('socket.socket', side_effect=AssertionError('network')):
            result = workspace_readiness(self.root)
        self.assertEqual(result['voice_samples'], 0)
        self.assertFalse(result['providers']['google']['configured'])
        self.assertTrue(result['next_steps'])
        self.assertEqual(before, sorted(str(x.relative_to(self.root)) for x in self.root.rglob('*')))

    def test_uninitialized_doctor_no_writes(self):
        path = self.root/'not-created'
        self.assertFalse(workspace_readiness(path)['initialized'])
        self.assertFalse(path.exists())

    def test_default_new_is_one_vk_post(self):
        self.assertEqual(parser().parse_args(['new', 'test', '--topic', 'Тема']).formats, 'vk')

    def test_preview_all_channels_and_obsolete_export_cleanup(self):
        self.good('article')
        self.change('brief.json', lambda d: d.update(formats=['article', 'vk']))
        self.change('pack.json', lambda d: d['formats'].update(vk={'text': 'ТЕКСТ ВК [[fact]]', 'cta': 'Вопрос мастеру', 'claims': ['fact']}))
        dest = Path(p.export(self.root, 'example')['path'])
        preview = (dest/'preview.html').read_text()
        self.assertIn('data-format="article"', preview)
        self.assertIn('data-format="vk"', preview)
        self.assertIn('ТЕКСТ ВК', preview)
        self.change('brief.json', lambda d: d.update(formats=['article']))
        self.change('pack.json', lambda d: d['formats'].pop('vk'))
        p.export(self.root, 'example')
        self.assertFalse((dest/'vk.txt').exists())
        self.assertFalse((dest/'vk.json').exists())

    def test_dictionary_json_invalidates_approval_snapshot(self):
        self.good()
        engine = self.root/'test-engine'
        path = engine/'docs/domain.json'; path.parent.mkdir(parents=True)
        write_json(path, {'version': 1})
        with patch.object(core, '__file__', str(engine/'bamboo/core.py')):
            first = snapshot(self.root, 'example')
            write_json(path, {'version': 2})
            self.assertNotEqual(first, snapshot(self.root, 'example'))


class InstalledWorkflowTests(unittest.TestCase):
    def test_installed_cli_end_to_end_without_network_or_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()/'workspace'
            install(ROOT, root, SYSTEMS)
            def command(*args, expected=0):
                # CLI launched in a fresh process, exercising the actual installed entrypoint.
                out = subprocess.run([sys.executable, str(root/'bamboo.py'), '--workspace', str(root), *args],
                                     cwd=root, text=True, capture_output=True, timeout=30)
                self.assertEqual(out.returncode, expected, out.stderr + out.stdout)
                return json.loads(out.stdout) if out.stdout else {}
            command('init')
            doctor = command('doctor')
            self.assertTrue(doctor['readiness']['initialized'])
            self.assertEqual(doctor['readiness']['voice_samples'], 0)
            command('new', 'example', '--topic', 'Искусственный тестовый пост')
            self.assertFalse(command('validate', 'example', expected=1)['ok'])
            job = root/'content/jobs/example'
            brief = read_json(job/'brief.json'); brief.update(intent='Тест', master_notes='Искусственная фактура', master_notes_public=True)
            write_json(job/'brief.json', brief)
            (job/'maker.md').write_text('Искусственный тестовый факт', encoding='utf-8')
            write_json(job/'sources.json', [{'id':'maker','kind':'master','title':'Тест','path':'content/jobs/example/maker.md','visibility':'public','verified_at':'2026-09-21'}])
            write_json(job/'claims.json', [{'id':'fact','text':'Искусственный тестовый факт','source_ids':['maker'],'status':'verified','checked_by':'TEST ONLY'}])
            pack = read_json(job/'pack.json'); pack['formats']['vk'].update(text='Искусственный факт [[fact]]', cta='Тестовый вопрос', claims=['fact'])
            write_json(job/'pack.json', pack)
            self.assertTrue(command('validate', 'example')['ok'])
            exported = command('export', 'example')
            self.assertTrue(Path(exported['preview']).is_file())
            self.assertFalse(exported['published'])
            self.assertFalse((job/'approval.json').exists())
            plan = command('publish', 'example', '--channel', 'wordpress')
            self.assertFalse(plan['network_called'])
            # No approval: must fail BEFORE any real service request.
            command('publish', 'example', '--channel', 'wordpress', '--execute', expected=2)
            csv_path = root/'fixture.csv'
            csv_path.write_text('date,source,grain,page,query,impressions,clicks,position\n2026-09-21,google,page,https://example.org/a,,100,5,8\n', encoding='utf-8')
            command('analytics-import', '--file', str(csv_path))
            report = command('analytics-report')
            self.assertEqual(report['current'][1], '2026-09-21')
            self.assertEqual(report['opportunities']['actions'], [])
            self.assertTrue((root/'analytics/report.md').exists())


if __name__ == '__main__':
    unittest.main()

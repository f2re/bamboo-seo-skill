"""Данные здесь искусственные; это регрессионные тесты, не статистика магазина."""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from bamboo import analytics as a, commerce as cm, publishing as p, quality as q
from bamboo.cli import main
from bamboo.core import (BambooError, CHECKS, approval_token, config, digest, file_digest, init,
                         job_path, lock, new_job, read_json, safe, snapshot, write_json, write_text)
from bamboo.install import BEGIN, SYSTEMS, adapters, block, install, uninstall
from bamboo import net

ROOT = Path(__file__).resolve().parent.parent
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        init(self.root)
        self.job = self.root / 'content/jobs/example'

    def tearDown(self):
        self.tmp.cleanup()

    def good(self, fmt='card', photo=False):
        if photo:
            write_json(self.root/'content/products/bowl-01.json', {'id':'bowl-01','confirmed':True,'volume_ml':200})
        new_job(self.root, 'example', 'Тестовая тема', [fmt], ['bowl-01'] if photo else [])
        brief = read_json(self.job/'brief.json')
        brief.update(intent='Тестовый вопрос покупателя', master_notes='Публичный тестовый факт', master_notes_public=True)
        write_json(self.job/'brief.json', brief)
        write_text(self.job/'maker.md', 'Искусственная фактура для тестов. Объём 200 мл.')
        write_json(self.job/'sources.json', [{'id':'maker','kind':'master','title':'Тестовый источник',
            'path':'content/jobs/example/maker.md','visibility':'public','verified_at':'2026-09-21'}])
        write_json(self.job/'claims.json', [{'id':'fact','text':'Тестовый факт. Объём 200 мл.',
            'source_ids':['maker'],'status':'verified','checked_by':'test-fixture'}])
        pack = read_json(self.job/'pack.json')
        pack['description']='Описание тестовой статьи'
        pack['formats'][fmt]={'text':'Тестовый факт — описание формы. [[fact]]','cta':'Задайте вопрос мастеру.','claims':['fact']}
        if photo:
            (self.root/'content/media/test.png').write_bytes(PNG)
            pack['photos']=[{'path':'content/media/test.png','sha256':digest(PNG),'origin':'camera',
                            'rights_confirmed':True,'product_ids':['bowl-01'],'alt':'Тестовое фото','caption':''}]
        write_json(self.job/'pack.json', pack)
        return pack

    def approved(self, fmt='card', photo=False):
        pack = self.good(fmt, photo)
        q.review_template(self.root,'example')
        self.sign()
        return pack

    def sign(self):
        review = read_json(self.job/'review.json')
        review.update(reviewer='TEST ONLY',notes='Искусственное согласие исключительно в тесте',
                      checks={k:True for k in CHECKS},warnings_acknowledged=True,content_hash=snapshot(self.root,'example'))
        write_json(self.job/'review.json',review)
        q.approve(self.root,'example',approval_token(self.root,'example'))

    def change(self, file, mutate):
        data = read_json(self.job/file)
        mutate(data)
        write_json(self.job/file,data)



class CoreTests(Fixture):
    def test_init_preserves_config(self):
        path=self.root/'bamboo.json';obj=read_json(path);obj['brand']='CUSTOM';write_json(path,obj)
        collections=self.root/'content/collections'
        if collections.exists(): collections.rmdir()
        self.assertEqual(init(self.root)['status'],'exists')
        self.assertEqual(config(self.root)['brand'],'CUSTOM')
        self.assertTrue(collections.is_dir())

    def test_paths(self):
        for value in ('../escape','/tmp/escape','a/../../escape','a\\b'):
            with self.subTest(value=value), self.assertRaises(BambooError): safe(self.root,value)

    def test_symlink(self):
        target=self.root/'link'
        try: target.symlink_to(self.root/'bamboo.json')
        except (OSError,NotImplementedError): self.skipTest('Символические ссылки недоступны')
        for call in (lambda:safe(self.root,'link'),lambda:read_json(target),lambda:write_text(target,'x')):
            with self.assertRaises(BambooError): call()

    def test_duplicate_json(self):
        f=self.root/'duplicate.json';f.write_text('{"x":1,"x":2}')
        with self.assertRaises(BambooError): read_json(f)

    def test_nan_json(self):
        f=self.root/'bad.json';f.write_text('{"x":NaN}')
        with self.assertRaises(BambooError): read_json(f)

    def test_lock_conflict(self):
        with lock(self.root):
            with self.assertRaises(BambooError):
                with lock(self.root): pass
        with lock(self.root): pass

    def test_duplicate_job_preserved(self):
        self.good()
        before=file_digest(self.job/'pack.json')
        with self.assertRaises(BambooError): new_job(self.root,'example','Other',['card'],[])
        self.assertEqual(before,file_digest(self.job/'pack.json'))

    def test_incomplete_fails(self):
        new_job(self.root,'example','Topic',['card'],[])
        self.assertFalse(q.validate(self.root,'example')['ok'])

    def test_good_and_dash_not_ai_marker(self):
        self.good()
        result=q.validate(self.root,'example')
        self.assertTrue(result['ok'],result)
        self.assertFalse(q.lint('У чаши — заметная грань.'))

    def test_private_source_fails(self):
        self.good();self.change('sources.json',lambda d:d[0].update(visibility='internal'))
        self.assertFalse(q.validate(self.root,'example')['ok'])

    def test_unknown_claim_fails(self):
        self.good();self.change('pack.json',lambda d:d['formats']['card'].update(claims=['absent']))
        self.assertFalse(q.validate(self.root,'example')['ok'])

    def test_numeric_claim_fails(self):
        self.good();self.change('pack.json',lambda d:d['formats']['card'].update(text='Объём 250 мл.'))
        self.assertFalse(q.validate(self.root,'example')['ok'])

    def test_numeric_claim_allowed(self):
        self.good();self.change('pack.json',lambda d:d['formats']['card'].update(text='Объём 200 мл.'))
        self.assertTrue(q.validate(self.root,'example')['ok'])

    def test_strict_length(self):
        self.good();cfg=config(self.root);cfg['quality']['strict_lengths']=True;write_json(self.root/'bamboo.json',cfg)
        self.assertFalse(q.validate(self.root,'example')['ok'])

    def test_cta_required(self):
        self.good();self.change('pack.json',lambda d:d['formats']['card'].update(cta=''))
        self.assertFalse(q.validate(self.root,'example')['ok'])

    def test_evidence_sensitive_domain_term_requires_matching_claim(self):
        self.good()
        self.change('pack.json',lambda d:d['formats']['card'].update(
            text='Эта исинская глина вылеплена вручную. [[fact]]'))
        self.assertFalse(q.validate(self.root,'example')['ok'])
        self.change('claims.json',lambda d:d[0].update(text='Подтверждено: исинская глина. Объём 200 мл.'))
        self.assertTrue(q.validate(self.root,'example')['ok'])

    def test_metadata_requires_matching_evidence_too(self):
        self.good()
        self.change('pack.json',lambda d:d.update(title='Чаша из исинской глины'))
        self.assertFalse(q.validate(self.root,'example')['ok'])
        self.change('claims.json',lambda d:d[0].update(text='Подтверждено: исинская глина. Объём 200 мл.'))
        self.assertTrue(q.validate(self.root,'example')['ok'])


    def test_affirmative_health_claim_is_error_but_refutation_is_warning(self):
        affirmative=q.lint('Эта глина очищает воду.')
        refutation=q.lint('Нет доказательств, что эта глина очищает воду.')
        self.assertTrue(any(x['code']=='health_claim' and x['level']=='error' for x in affirmative))
        self.assertTrue(any(x['code']=='health_claim' and x['level']=='warning' for x in refutation))


    def test_optional_seo_context_validation(self):
        self.good()
        self.change('brief.json',lambda d:d['seo'].update(page_type='bad-type'))
        self.assertFalse(q.validate(self.root,'example')['ok'])
        self.change('brief.json',lambda d:d['seo'].update(page_type='article',target_url='javascript:bad'))
        self.assertFalse(q.validate(self.root,'example')['ok'])
        self.change('brief.json',lambda d:d['seo'].update(target_url='https://example.org/journal/example/'))
        self.assertTrue(q.validate(self.root,'example')['ok'])


    def test_photo_required(self):
        self.good(photo=True);self.change('pack.json',lambda d:d.update(photos=[]))
        self.assertFalse(q.validate(self.root,'example')['ok'])

    def test_generated_photo_fails(self):
        self.good(photo=True);self.change('pack.json',lambda d:d['photos'][0].update(origin='generated'))
        self.assertFalse(q.validate(self.root,'example')['ok'])

    def test_photo_modified_fails(self):
        self.good(photo=True);(self.root/'content/media/test.png').write_bytes(b'changed')
        self.assertFalse(q.validate(self.root,'example')['ok'])

    def test_product_commerce_url_is_validated(self):
        self.good(photo=True)
        write_json(self.root/'content/products/bowl-01.json',
                   {'id':'bowl-01','confirmed':True,'volume_ml':200,'product_url':'javascript:alert(1)'})
        self.assertFalse(q.validate(self.root,'example')['ok'])


    def test_review_template_not_approval(self):
        self.good();q.review_template(self.root,'example')
        with self.assertRaises(BambooError): q.approve(self.root,'example',approval_token(self.root,'example'))

    def test_approval_current(self):
        self.approved();self.assertTrue(q.require_approval(self.root,'example'))

    def test_approval_changes(self):
        self.approved()
        self.change('pack.json',lambda d:d['formats']['card'].update(text='Иная версия.'))
        with self.assertRaises(BambooError): q.require_approval(self.root,'example')

    def test_source_changes_stale(self):
        self.approved();write_text(self.job/'maker.md','Факт изменён')
        with self.assertRaises(BambooError): q.require_approval(self.root,'example')

    def test_review_changes_stale(self):
        self.approved();self.change('review.json',lambda d:d.update(notes='other'))
        with self.assertRaises(BambooError): q.require_approval(self.root,'example')

    def test_config_changes_stale(self):
        self.approved();cfg=config(self.root);cfg['shop_url']='https://example.org';write_json(self.root/'bamboo.json',cfg)
        with self.assertRaises(BambooError): q.require_approval(self.root,'example')

    def test_product_changes_stale(self):
        self.approved(photo=True);write_json(self.root/'content/products/bowl-01.json',{'id':'bowl-01','confirmed':True,'volume_ml':201})
        with self.assertRaises(BambooError): q.require_approval(self.root,'example')

    def test_export_no_publication(self):
        self.good(photo=True)
        result=p.export(self.root,'example')
        text=Path(result['preview']).read_text()
        self.assertIn('noindex,nofollow',text)
        self.assertNotIn('[[fact]]',text)
        self.assertFalse(result['published'])
        self.assertEqual((Path(result['path'])/'media'/f'{digest(PNG)}.png').read_bytes(),PNG)
        commerce=read_json(Path(result['path'])/'commerce.json')
        self.assertEqual(commerce['product_ids'],['bowl-01'])
        self.assertEqual(commerce['products'][0]['id'],'bowl-01')

    def test_vk_export_contains_tracking_without_overwriting_existing_utm(self):
        self.good('vk',photo=True)
        product=read_json(self.root/'content/products/bowl-01.json')
        product.update(product_url='https://example.org/p/1?ref=catalog',vk_product_id='vk-17')
        write_json(self.root/'content/products/bowl-01.json',product)
        result=p.export(self.root,'example')
        vk=read_json(Path(result['path'])/'vk.json')
        self.assertEqual(vk['catalog_items'],['vk-17'])
        self.assertEqual(vk['recommended_cta_type'],'product')
        suggested=vk['tracking']['urls'][0]['suggested_url']
        self.assertIn('utm_source=vk',suggested)
        self.assertIn('utm_campaign=example',suggested)
        self.assertEqual(p.tracking_url('https://example.org/p?utm_source=custom','example'),
                         'https://example.org/p?utm_source=custom')

    def test_escaping(self):
        text=p.page('<img onerror=x>','" onload="x',p.markdown('<b>unsafe</b>'))
        self.assertIn('&lt;img',text);self.assertNotIn('<b>unsafe',text)
        self.assertIn('&quot;',text)

    def test_static_requires_approval(self):
        self.good('article')
        with self.assertRaises(BambooError): p.publish_static(self.root,'example')

    def test_static_site(self):
        cfg=config(self.root);cfg['site_url']='https://example.org';write_json(self.root/'bamboo.json',cfg)
        self.approved('article',photo=True)
        result=p.publish_static(self.root,'example')
        self.assertEqual(result['status'],'built_locally')
        text=(self.root/'site/journal/example/index.html').read_text()
        self.assertIn('rel="canonical"',text);self.assertNotIn('noindex',text)
        self.assertIn('property="og:type" content="article"',text)
        self.assertIn('"@type":"Article"',text)
        self.assertIn('"@type":"BreadcrumbList"',text)
        self.assertTrue((self.root/'site/sitemap.xml').exists())

    def test_cli_validate_and_dry_run(self):
        new_job(self.root,'example','Incomplete',['card'],[])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--workspace',str(self.root),'validate','example']),1)
        with patch('bamboo.publishing.request') as req, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--workspace',str(self.root),'publish','example','--channel','wordpress']),0)
        req.assert_not_called()

    def test_cli_error_no_traceback(self):
        stream=io.StringIO()
        with contextlib.redirect_stderr(stream):
            rc=main(['--workspace',str(self.root),'approve','missing','--confirm','bad'])
        self.assertEqual(rc,2);self.assertNotIn('Traceback',stream.getvalue())


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.target=Path(self.tmp.name).resolve()/'work'
    def tearDown(self): self.tmp.cleanup()

    def test_dry_run_no_writes(self):
        result=install(ROOT,self.target,SYSTEMS,True)
        self.assertTrue(result['changed']);self.assertFalse(self.target.exists())

    def test_install_preserves_and_idempotent(self):
        self.target.mkdir();write_text(self.target/'AGENTS.md','CUSTOM RULE\n')
        write_text(self.target/'CLAUDE.md','CUSTOM CLAUDE\n')
        install(ROOT,self.target,SYSTEMS)
        self.assertIn('CUSTOM RULE',(self.target/'AGENTS.md').read_text())
        self.assertIn('@AGENTS.md',(self.target/'CLAUDE.md').read_text())
        self.assertEqual(install(ROOT,self.target,SYSTEMS)['changed'],[])
        self.assertEqual((self.target/'AGENTS.md').read_text().count(BEGIN),1)

    def test_conflict_preflight(self):
        self.target.mkdir();write_text(self.target/'bamboo.py','CUSTOM')
        with self.assertRaises(BambooError): install(ROOT,self.target,SYSTEMS)
        self.assertEqual((self.target/'bamboo.py').read_text(),'CUSTOM')
        self.assertFalse((self.target/'.agents/skills').exists())

    def test_modified_managed_refused(self):
        install(ROOT,self.target,SYSTEMS)
        write_text(self.target/'.claude/agents/bamboo-editor.md','CUSTOM')
        with self.assertRaises(BambooError): install(ROOT,self.target,SYSTEMS)

    def test_external_project_executable(self):
        install(ROOT,self.target,SYSTEMS)
        result=subprocess.run([sys.executable,str(self.target/'bamboo.py'),'--workspace',str(self.target),'init'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((self.target/'bamboo.json').exists())

    def test_uninstall_preserves_data_and_custom(self):
        install(ROOT,self.target,SYSTEMS);init(self.target)
        write_text(self.target/'.claude/agents/bamboo-editor.md','CUSTOM')
        result=uninstall(self.target)
        self.assertIn('.claude/agents/bamboo-editor.md',result['kept_modified'])
        self.assertTrue((self.target/'bamboo.json').exists())
        self.assertNotIn(BEGIN,(self.target/'AGENTS.md').read_text())

    def test_native_adapters_contract(self):
        files,blocks=adapters(ROOT,SYSTEMS,'.')
        self.assertEqual(len([x for x in files if x.startswith('.agents/skills/')]),6)
        self.assertIn('disable-model-invocation: true',files['.claude/skills/bamboo-publish/SKILL.md'])
        self.assertIn('sandbox_mode = "read-only"',files['.codex/agents/bamboo-editor.toml'])
        self.assertIn('commandExecutionPolicy: off',files['.agents/agents/bamboo-editor.md'])
        self.assertIn('model: inherit',files['.claude/agents/bamboo-editor.md'])

    def test_generated_files_in_sync(self):
        files,blocks=adapters(ROOT,SYSTEMS,'.')
        for rel,text in files.items(): self.assertEqual((ROOT/rel).read_text(encoding='utf-8'),text,rel)
        for rel,text in blocks.items(): self.assertEqual(block((ROOT/rel).read_text(encoding='utf-8')),text)


class AnalyticsTests(Fixture):
    def row(self,day='2026-09-21',**kwargs):
        return {'date':day,'source':'fixture','grain':'page','page':'https://example.org/a','query':'','impressions':100,'clicks':5,'position':8,**kwargs}

    def test_upsert_not_double_counted(self):
        a.ingest(self.root,[self.row()]);a.ingest(self.root,[self.row(clicks=8)])
        out=a.report(self.root,'2026-09-21',1)
        self.assertEqual(out['pages'][0]['current']['clicks'],8)

    def test_reject_duplicates(self):
        with self.assertRaises(BambooError): a.ingest(self.root,[self.row(),self.row()])

    def test_invalid_batch_no_partial(self):
        with self.assertRaises(BambooError): a.ingest(self.root,[self.row(),self.row('2026-09-20',clicks=float('nan'))])
        self.assertFalse((self.root/'analytics/metrics.sqlite3').exists())

    def test_grains_not_summed(self):
        a.ingest(self.root,[self.row(),self.row(grain='page_query',query='phrase',clicks=3)])
        self.assertEqual(a.report(self.root,'2026-09-21',1)['pages'][0]['current']['clicks'],5)

    def test_no_query_on_page_total(self):
        with self.assertRaises(BambooError): a.normalize(self.row(query='incorrect'))

    def test_zero_baseline_no_infinity(self):
        a.ingest(self.root,[self.row('2026-09-20',clicks=0),self.row()])
        self.assertIsNone(a.report(self.root,'2026-09-21',1)['pages'][0]['click_change_fraction'])

    def test_weighted_position(self):
        rows=[a.normalize(self.row('2026-09-20',impressions=100,position=10)),a.normalize(self.row(impressions=300,position=2))]
        result=a.summarize(rows)
        self.assertEqual(result['position'],4)
        self.assertEqual(result['ctr'],10/400)

    def test_missing_values_remain_unknown(self):
        a.ingest(self.root,[self.row(clicks=None,impressions=None)])
        out=a.report(self.root,'2026-09-21',1)['pages'][0]['current']
        self.assertIsNone(out['clicks']);self.assertIsNone(out['ctr'])

    def test_incomplete_days_not_growth(self):
        a.ingest(self.root,[self.row('2026-09-19'),self.row()])
        self.assertIsNone(a.report(self.root,'2026-09-21',2)['pages'][0]['click_change_fraction'])

    def test_gsc_dimensions(self):
        with patch('bamboo.analytics.request',return_value={'rows':[{'keys':['2026-09-21','https://example.org/','query'],'clicks':2,'impressions':100,'position':7}]}) as req:
            rows=a.gsc_rows('sc-domain:example.org','secret','2026-09-01','2026-09-21','page_query')
        self.assertEqual(rows[0]['query'],'query')
        self.assertEqual(req.call_args.kwargs['payload']['dimensions'],['date','page','query'])
        self.assertTrue(req.call_args.kwargs['readonly'])
        self.assertIn('sc-domain%3Aexample.org',req.call_args.args[0])

    def test_gsc_pagination(self):
        item={'keys':['2026-09-21','https://example.org/'],'clicks':0,'impressions':1}
        with patch('bamboo.analytics.request',side_effect=[{'rows':[item]*25000},{'rows':[]}]) as req:
            rows=a.gsc_rows('sc-domain:example.org','secret','2026-09-01','2026-09-21','page')
        self.assertEqual(len(rows),25000)
        self.assertEqual(req.call_args.kwargs['payload']['startRow'],25000)

    def test_yandex_popular_not_joint_dimension(self):
        item={'text_indicator':{'type':'URL','value':'https://example.org/'},
              'popular_complementary_indicator':{'type':'QUERY','value':'do-not-attribute'},
              'statistics':[{'date':'2026-09-21','field':'CLICKS','value':10},{'date':'2026-09-01','field':'CLICKS','value':1000}]}
        with patch('bamboo.analytics.request',return_value={'count':1,'text_indicator_to_statistics':[item]}):
            rows=a.yandex_rows('u','h','secret','2026-09-20','2026-09-21')
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['query'],'')
        self.assertEqual(rows[0]['clicks'],10);self.assertIsNone(rows[0]['impressions'])

    def test_yandex_query_is_separate_grain(self):
        item={'text_indicator':{'type':'QUERY','value':'купить тяван'},
              'popular_complementary_indicator':{'type':'URL','value':'https://example.org/chawan/'},
              'statistics':[{'date':'2026-09-21','field':'CLICKS','value':3},
                            {'date':'2026-09-21','field':'IMPRESSIONS','value':90},
                            {'date':'2026-09-21','field':'POSITION','value':6}]}
        with patch('bamboo.analytics.request',return_value={'count':1,'text_indicator_to_statistics':[item]}) as req:
            rows=a.yandex_rows('u','h','secret','2026-09-20','2026-09-21','QUERY')
        self.assertEqual(rows[0]['grain'],'query')
        self.assertEqual(rows[0]['query'],'купить тяван')
        self.assertEqual(rows[0]['page'],'https://example.org/chawan/')
        self.assertEqual(req.call_args.kwargs['payload']['text_indicator'],'QUERY')
        self.assertEqual(a.normalize(rows[0])['grain'],'query')

    def test_report_uses_queries_and_flags_multi_url_candidate(self):
        rows=[
            self.row(impressions=300,clicks=15),
            self.row(source='google',grain='page_query',query='как выбрать тяван',
                     page='https://example.org/a',impressions=120,clicks=2,position=8),
            self.row(source='google',grain='page_query',query='как выбрать тяван',
                     page='https://example.org/b',impressions=110,clicks=1,position=9),
            self.row(source='yandex',grain='query',query='купить тяван',
                     page='https://example.org/catalog/chawan',impressions=140,clicks=5,position=7),
        ]
        a.ingest(self.root,rows)
        out=a.report(self.root,'2026-09-21',1)
        self.assertEqual(out['query_count_total'],3)
        self.assertEqual(out['queries'][0]['intent_hint'] in
                         ('informational','transactional','commercial_research','unknown','branded'),True)
        yandex=[x for x in out['queries'] if x['source']=='yandex'][0]
        self.assertIsNone(yandex['page'])
        self.assertEqual(yandex['page_hint'],'https://example.org/catalog/chawan')
        self.assertEqual(len(out['cannibalization_candidates']),1)
        self.assertEqual(len(out['cannibalization_candidates'][0]['pages']),2)

    def test_conversion_rows_are_reported_separately(self):
        a.ingest(self.root,[self.row(source='manual',grain='conversion',query='',
            impressions=None,clicks=None,product_clicks=20,leads=5,orders=2,revenue=10000,cost=1000)])
        out=a.report(self.root,'2026-09-21',1)
        self.assertEqual(out['pages'],[])
        self.assertEqual(out['conversions'][0]['current']['orders'],2)
        self.assertEqual(out['conversions'][0]['current']['lead_to_order_rate'],0.4)

    def test_query_intent_hint(self):
        self.assertEqual(a.query_intent_hint('купить тяван ручной работы'),'transactional')
        self.assertEqual(a.query_intent_hint('как выбрать тяван'),'commercial_research')
        self.assertEqual(a.query_intent_hint('Bamboo Pottery'),'branded')

    def test_yandex_enhanced_export_is_explicit_and_quota_aware(self):
        cfg=config(self.root);cfg['analytics'].update(yandex_user_id='7',yandex_host_id='host-id');write_json(self.root/'bamboo.json',cfg)
        task='2f1c5d3b-7d9b-4c3e-8a14-9d8b924a12ef'
        response={'task_id':task,'free_quota_used':2,'pro_quota_used':0,'total_quota_used':2,
                  'free_quota_remaining':98,'pro_quota_remaining':0}
        with patch.dict(os.environ,{'BAMBOO_YANDEX_TOKEN':'SECRET'},clear=True):
            with patch('bamboo.analytics.request',return_value=response) as req:
                result=a.yandex_export_start(self.root,['2026-09-20'],['/journal/a','/catalog'],[],False)
        self.assertEqual(result['task_id'],task)
        self.assertEqual(req.call_args.kwargs['payload']['use_pro_tariff'],'false')
        self.assertEqual(req.call_args.kwargs['payload']['paths'],['/journal/a','/catalog'])
        self.assertNotIn('readonly',req.call_args.kwargs)
        self.assertTrue((self.root/f'analytics/yandex-export-{task}.json').exists())

    def test_yandex_enhanced_export_status_does_not_poll(self):
        cfg=config(self.root);cfg['analytics'].update(yandex_user_id='7',yandex_host_id='host-id');write_json(self.root/'bamboo.json',cfg)
        task='2f1c5d3b-7d9b-4c3e-8a14-9d8b924a12ef'
        with patch.dict(os.environ,{'BAMBOO_YANDEX_TOKEN':'SECRET'},clear=True):
            with patch('bamboo.analytics.request',return_value={'download_status':'SUCCESS','url':'https://storage.example/report.csv'}) as req:
                result=a.yandex_export_status(self.root,task)
        self.assertEqual(result['download_status'],'SUCCESS')
        self.assertTrue(req.call_args.kwargs['readonly'])
        self.assertEqual(req.call_count,1)

    def test_yandex_export_rejects_absolute_url_path(self):
        cfg=config(self.root);cfg['analytics'].update(yandex_user_id='7',yandex_host_id='host-id');write_json(self.root/'bamboo.json',cfg)
        with self.assertRaises(BambooError):
            a.yandex_export_start(self.root,['2026-09-20'],['https://example.org/a'])

    def test_yandex_enhanced_csv_imports_exact_page_query_and_raw_regions(self):
        path=self.root/'yandex.csv'
        path.write_text(
            'Дата;Хост;URL;Запрос;Регион;Клики;Показы;Позиция\n'
            '2026-09-21;example.org;https://example.org/a;тяван;Москва;2;100;5\n'
            '2026-09-21;example.org;https://example.org/a;тяван;СПб;1;50;7\n',
            encoding='utf-8')
        result=a.import_yandex_enhanced_csv(self.root,path)
        self.assertEqual(result['raw_rows'],2)
        self.assertEqual(result['page_query_rows'],1)
        out=a.report(self.root,'2026-09-21',1)
        row=[x for x in out['queries'] if x['source']=='yandex_enhanced'][0]
        self.assertEqual(row['current']['impressions'],150)
        self.assertEqual(row['current']['clicks'],3)
        self.assertAlmostEqual(row['current']['position'],(5*100+7*50)/150)

    def test_yandex_enhanced_partial_region_import_keeps_previous_regions(self):
        first=self.root/'y1.csv';second=self.root/'y2.csv'
        header='Date,Host,URL,Query,Region,Clicks,Impressions,Ranking\n'
        first.write_text(header+'2026-09-21,example.org,https://example.org/a,chawan,Moscow,2,100,5\n',encoding='utf-8')
        second.write_text(header+'2026-09-21,example.org,https://example.org/a,chawan,SPb,1,50,7\n',encoding='utf-8')
        a.import_yandex_enhanced_csv(self.root,first)
        a.import_yandex_enhanced_csv(self.root,second)
        out=a.report(self.root,'2026-09-21',1)
        row=[x for x in out['queries'] if x['source']=='yandex_enhanced'][0]
        self.assertEqual(row['current']['impressions'],150)
        self.assertEqual(row['current']['clicks'],3)


    def test_query_clusters_intent_mismatch_and_funnel(self):
        self.good()
        self.change('brief.json',lambda d:d['seo'].update(
            page_type='article',cluster='chawan',target_url='https://example.org/a'))
        a.ingest(self.root,[
            self.row(source='google',grain='page',page='https://example.org/a',
                     impressions=300,clicks=30,position=5),
            self.row(source='google',grain='page_query',page='https://example.org/a',
                     query='купить тяван',impressions=150,clicks=10,position=4),
            self.row(source='google',grain='conversion',page='https://example.org/a',query='',
                     impressions=None,clicks=None,position=None,product_clicks=12,leads=4,orders=2)
        ])
        out=a.report(self.root,'2026-09-21',1)
        self.assertEqual(out['query_clusters'][0]['cluster_hint'],'chawan')
        self.assertEqual(out['intent_mismatch_candidates'][0]['page_type'],'article')
        self.assertEqual(out['funnels'][0]['product_click_rate'],12/30)
        self.assertEqual(out['funnels'][0]['order_rate'],2/30)


    def test_oauth_form_and_secrets_not_in_url(self):
        with patch.dict(os.environ,{'BAMBOO_GSC_CLIENT_ID':'client','BAMBOO_GSC_CLIENT_SECRET':'SECRET','BAMBOO_GSC_REFRESH_TOKEN':'REFRESH'},clear=True):
            with patch('bamboo.analytics.request',return_value={'access_token':'result'}) as req:
                self.assertEqual(a.google_token(),'result')
        self.assertIsInstance(req.call_args.kwargs['payload'],bytes)
        self.assertEqual(req.call_args.kwargs['headers']['Content-Type'],'application/x-www-form-urlencoded')
        self.assertNotIn('SECRET',req.call_args.args[0])


class CommerceTests(Fixture):
    def test_graph_links_content_products_and_collections(self):
        self.good(photo=True)
        cfg=config(self.root);cfg['site_url']='https://example.org';write_json(self.root/'bamboo.json',cfg)
        product=read_json(self.root/'content/products/bowl-01.json')
        product.update(collection='chawan',name='Тестовый тяван',
                       product_url='https://example.org/products/bowl-01',
                       price=5000,currency='RUB',availability='InStock',
                       photo_set=['https://example.org/media/bowl-01.jpg'])
        write_json(self.root/'content/products/bowl-01.json',product)
        write_json(self.root/'content/collections/chawan.json',{
            'schema_version':1,'id':'chawan','confirmed':True,'name':'Тяваны','type':'category',
            'cluster':'chawan','url':'https://example.org/catalog/chawan','product_ids':['bowl-01']})
        brief=read_json(self.job/'brief.json')
        brief['seo'].update(page_type='article',cluster='chawan',target_url='https://example.org/journal/choose-chawan')
        write_json(self.job/'brief.json',brief)
        graph=cm.build_graph(self.root)
        self.assertIn('bowl-01',graph['nodes']['products'])
        self.assertIn('chawan',graph['nodes']['collections'])
        self.assertTrue(any(x['relation']=='contains' for x in graph['edges']))
        self.assertEqual(graph['internal_link_suggestions'][0]['to_url'],'https://example.org/products/bowl-01')
        candidate=graph['structured_data_candidates'][0]
        self.assertTrue(candidate['same_site_purchase_page'])
        self.assertTrue(candidate['merchant_listing_candidate'])
        self.assertEqual(candidate['missing_required_fields'],[])
        self.assertTrue((self.root/'content/commerce-graph.json').exists())


class WordPressTests(Fixture):
    def setup_wp(self,photo=False):
        cfg=config(self.root);cfg['wordpress_url']='https://example.org';write_json(self.root/'bamboo.json',cfg)
        self.approved('article',photo=photo)
        self.env=patch.dict(os.environ,{'BAMBOO_WP_USER':'fixture','BAMBOO_WP_APP_PASSWORD':'secret'})
        self.env.start();self.addCleanup(self.env.stop)

    def test_draft_and_idempotence(self):
        self.setup_wp()
        with patch('bamboo.publishing.request',side_effect=[[],{'id':7,'link':'https://example.org/p','status':'draft','modified_gmt':'now'}]) as req:
            result=p.publish_wordpress(self.root,'example')
            again=p.publish_wordpress(self.root,'example')
        self.assertEqual(result['status'],'draft');self.assertTrue(again['unchanged'])
        self.assertEqual(req.call_count,2)
        self.assertEqual(req.call_args.kwargs['payload']['status'],'draft')

    def test_existing_slug_refused(self):
        self.setup_wp()
        with patch('bamboo.publishing.request',return_value=[{'id':22}]):
            with self.assertRaises(BambooError): p.publish_wordpress(self.root,'example')

    def test_pending_stops_repeat_and_reconciles(self):
        self.setup_wp()
        with patch('bamboo.publishing.request',side_effect=[[],BambooError('timeout')]) as req:
            with self.assertRaises(BambooError): p.publish_wordpress(self.root,'example')
            with self.assertRaises(BambooError): p.publish_wordpress(self.root,'example')
        self.assertEqual(req.call_count,2)
        ledger=read_json(self.job/'wordpress.json')
        item={'id':7,'link':'https://example.org/p','status':'draft',
              'content':{'raw':f'<!-- bamboo:example:{ledger["pending_hash"]} -->'}}
        with patch('bamboo.publishing.request',return_value=[item]): result=p.wp_reconcile(self.root,'example')
        self.assertFalse(result['pending']);self.assertEqual(result['id'],7)

    def test_pending_media_recovery(self):
        self.setup_wp(photo=True)
        with patch('bamboo.publishing.request',side_effect=[[],BambooError('timeout')]):
            with self.assertRaises(BambooError): p.publish_wordpress(self.root,'example')
        ledger=read_json(self.job/'wordpress.json');self.assertEqual(ledger['pending_kind'],'media')
        with patch('bamboo.publishing.request',return_value=[{'id':5,'slug':digest(PNG),'source_url':'https://example.org/test.png'}]):
            result=p.wp_reconcile(self.root,'example')
        self.assertFalse(result['pending']);self.assertEqual(result['media'][digest(PNG)]['id'],5)

    def test_photo_upload_payload(self):
        self.setup_wp(photo=True)
        with patch('bamboo.publishing.request',side_effect=[[],{'id':5,'source_url':'https://example.org/test.png'}, {},
                {'id':7,'link':'https://example.org/p','status':'draft'}]) as req:
            p.publish_wordpress(self.root,'example')
        self.assertEqual(req.call_args_list[1].kwargs['payload'],PNG)
        self.assertIn('alt_text',req.call_args_list[2].kwargs['payload'])
        self.assertIn('<img',req.call_args_list[3].kwargs['payload']['content'])

    def test_live_requires_explicit_token_cli(self):
        self.setup_wp()
        with patch('bamboo.publishing.request') as req, contextlib.redirect_stderr(io.StringIO()):
            rc=main(['--workspace',str(self.root),'publish','example','--channel','wordpress','--execute','--live'])
        self.assertEqual(rc,2);req.assert_not_called()


class NetTests(unittest.TestCase):
    def test_reject_http(self):
        with self.assertRaises(BambooError): net.request('http://example.org')
    def test_reject_credentials_url(self):
        with self.assertRaises(BambooError): q.http_url('https://user:secret@example.org')
    def test_no_redirect(self):
        self.assertIsNone(net.NoRedirect().redirect_request(None,None,302,'',{},'https://other.org'))
    def test_mutation_no_retry_secret_safe(self):
        error=urllib.error.HTTPError('https://example.org',503,'secret-token',{},None)
        opener=MagicMock();opener.open.side_effect=error
        with patch('urllib.request.build_opener',return_value=opener):
            with self.assertRaises(BambooError) as caught: net.request('https://example.org',payload={'x':1})
        self.assertEqual(opener.open.call_count,1);self.assertNotIn('secret-token',str(caught.exception))
    def test_read_retries(self):
        error=urllib.error.HTTPError('https://example.org',503,'unavailable',{},None)
        response=MagicMock();response.__enter__.return_value.read.return_value=b'{}'
        opener=MagicMock();opener.open.side_effect=[error,response]
        with patch('urllib.request.build_opener',return_value=opener),patch('time.sleep'):
            self.assertEqual(net.request('https://example.org',readonly=True),{})
        self.assertEqual(opener.open.call_count,2)


if __name__=='__main__': unittest.main()

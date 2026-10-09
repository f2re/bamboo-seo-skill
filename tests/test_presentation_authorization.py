"""New publication contract. All product facts, accounts and API writes here are synthetic."""
import io
import json
import time
from pathlib import Path
from unittest.mock import patch

from test_bamboo import Fixture, PNG
from test_vk_store import FakeAPI
from bamboo import publishing, quality
from bamboo.authorization import authorize, publish_vk, require_authorization
from bamboo.cli import dispatch, parser
from bamboo.core import BambooError, read_json, snapshot, write_json
from bamboo.presentation import inspect_style, markdown, vk_plain
from bamboo.vk.common import VKError, local
from bamboo.vk.store import Store, initialize
from bamboo.vk.mcp import call, serve


class PresentationTests(Fixture):
    def test_short_vk_has_no_minimum_padding_requirement(self):
        self.good('vk')
        cfg = read_json(self.root / 'bamboo.json')
        cfg['quality']['strict_lengths'] = True
        write_json(self.root / 'bamboo.json', cfg)
        report = quality.validate(self.root, 'example')
        self.assertTrue(report['ok'], report)

    def test_short_article_has_no_minimum_padding_requirement(self):
        self.good('article')
        cfg = read_json(self.root / 'bamboo.json')
        cfg['quality']['strict_lengths'] = True
        write_json(self.root / 'bamboo.json', cfg)
        self.assertTrue(quality.validate(self.root, 'example')['ok'])

    def test_profile_length_is_warning_not_error(self):
        self.good('vk')
        self.change('pack.json', lambda p: p['formats']['vk'].update(style='vk-detail'))
        report = quality.validate(self.root, 'example')
        self.assertTrue(report['ok'])
        self.assertTrue(any(w['code'] == 'profile_length' for w in report['warnings']))

    def test_incompatible_profile_rejected(self):
        self.good('vk')
        self.change('pack.json', lambda p: p['formats']['vk'].update(style='article-guide'))
        self.assertFalse(quality.validate(self.root, 'example')['ok'])

    def test_unknown_profile_rejected_without_type_crash(self):
        for value in ('bad', {}, [], True):
            self.assertEqual(inspect_style('vk', 'Текст', value)['findings'][0]['level'], 'error')

    def test_dense_text_is_warning_only(self):
        r = inspect_style('vk', 'Один длинный блок. ' * 40)
        self.assertIn('dense_block', [f['code'] for f in r['findings']])
        self.assertNotIn('error', [f['level'] for f in r['findings']])

    def test_avoids_mechanically_chopped_prose(self):
        r = inspect_style('vk', '\n\n'.join(['Короткая фраза.'] * 9))
        self.assertIn('chopped_rhythm', [f['code'] for f in r['findings']])

    def test_markdown_emphasis_quote_lists_hr(self):
        out = markdown('## Край\n\n**Важное** и *пояснение*\n\n> Памятка\n\n1. Объем\n2. Размер\n\n---\n\n- Материал\n- Покрытие')
        for tag in ('<h2>', '<strong>', '<em>', '<blockquote>', '<ol start="1">', '<hr>', '<ul>'):
            self.assertIn(tag, out)

    def test_raw_html_and_javascript_cannot_execute(self):
        out = markdown('<img src=x onerror=alert(1)> **вещь** [x](javascript:alert(1))')
        self.assertNotIn('<img', out)
        self.assertNotIn('href="javascript:', out)
        self.assertIn('&lt;img', out)

    def test_vk_plain_preserves_parameters_and_url(self):
        out = vk_plain('## Пиала\n\n**Объём:** 200 мл\n- Диаметр: 9 см\n\n[Выбрать](https://vk.ru/market-1?section=album_1)')
        self.assertNotIn('**', out)
        self.assertNotIn('##', out)
        self.assertIn('200 мл', out)
        self.assertIn('9 см', out)
        self.assertIn('section=album_1', out)
        self.assertIn('• Диаметр:', out)

    def test_photo_placed_between_article_blocks(self):
        self.good('article', photo=True)
        def update(p):
            p['formats']['article']['text'] = 'Первый блок. [[fact]]\n\n## Размер\n\nПоследний блок.'
            p['photos'][0]['after_block'] = 1
        self.change('pack.json', update)
        self.assertTrue(quality.validate(self.root, 'example')['ok'])
        out = publishing.body(self.root, 'example', ['photo.png'])
        self.assertLess(out.index('Первый блок'), out.index('<figure>'))
        self.assertLess(out.index('<figure>'), out.index('<h2>Размер'))

    def test_photo_placement_exported_for_native_editor(self):
        self.good('article', photo=True)
        self.change('pack.json', lambda p: p['photos'][0].update(after_block=0))
        result = publishing.export(self.root, 'example')
        photos = read_json(Path(result['path']) / 'photos.json')
        self.assertEqual(photos[0]['after_block'], 0)

    def test_bad_photo_positions_rejected(self):
        self.good('article', photo=True)
        for value in (True, -1, 99, '2'):
            self.change('pack.json', lambda p: p['photos'][0].update(after_block=value))
            self.assertFalse(quality.validate(self.root, 'example')['ok'])

    def test_vk_exports_plain_preview_not_fake_bold(self):
        self.good('vk')
        self.change('pack.json', lambda p: p['formats']['vk'].update(text='**Объём:** 200 мл. [[fact]]'))
        result = publishing.export(self.root, 'example')
        dest = Path(result['path'])
        self.assertNotIn('**', (dest / 'vk.txt').read_text())
        self.assertIn('200 мл', (dest / 'vk.txt').read_text())
        preview = (dest / 'preview.html').read_text()
        self.assertIn('class="vk-post"', preview)
        self.assertIn('overflow-wrap:anywhere', preview)


class DirectPublicationTests(Fixture):
    def store(self):
        initialize(self.root, 'fixture-shop', '123')
        store = Store(self.root, FakeAPI())
        store.check()
        return store

    def test_authorize_does_not_forge_human_review(self):
        self.good('vk')
        self.store()
        result = authorize(self.root, 'example', 'vk', 'Напиши и опубликуй example в ВК')
        self.assertEqual(result['mode'], 'user_instruction')
        self.assertFalse(result['human_review_claimed'])
        self.assertFalse((self.job / 'review.json').exists())
        self.assertFalse((self.job / 'approval.json').exists())
        self.assertEqual(quality.require_approval(self.root, 'example', 'vk')['target'], 'vk:77')

    def test_no_implicit_channel_or_target_expansion(self):
        self.good('vk')
        self.store()
        authorize(self.root, 'example', 'vk', 'Опубликовать example в ВК')
        with self.assertRaises(BambooError):
            require_authorization(self.root, 'example', 'wordpress')
        config_path = self.root / '.bamboo/vk/config.json'
        cfg = read_json(config_path); cfg['group_id'] = 999; write_json(config_path, cfg)
        with self.assertRaises(BambooError):
            require_authorization(self.root, 'example', 'vk')

    def test_changed_content_not_authorized(self):
        self.good('vk'); self.store()
        authorize(self.root, 'example', 'vk', 'Опубликовать example в ВК')
        self.change('pack.json', lambda p: p['formats']['vk'].update(text='Другой текст.'))
        with self.assertRaises(BambooError):
            quality.require_approval(self.root, 'example', 'vk')

    def test_authorization_cannot_hide_invalid_facts(self):
        self.good('vk'); self.store()
        self.change('pack.json', lambda p: p['formats']['vk'].update(text='Объём 999 мл.'))
        with self.assertRaises(BambooError):
            authorize(self.root, 'example', 'vk', 'Опубликовать example в ВК')

    def test_authorization_request_cannot_contain_secret(self):
        self.good('vk'); self.store()
        with self.assertRaises(BambooError):
            authorize(self.root, 'example', 'vk', 'Опубликовать Bearer ' + 'A' * 30)

    def test_direct_dry_run_has_no_network_or_authorization(self):
        self.good('vk'); store = self.store(); before = len(store.api.calls)
        result = publish_vk(store, 'example', 'Опубликовать example в ВК')
        self.assertTrue(result['dry_run'])
        self.assertEqual(len(store.api.calls), before)
        self.assertFalse((self.job / 'authorization.json').exists())
        self.assertFalse(store.api.writes)

    def test_one_step_publishes_with_no_review_json(self):
        self.good('vk', photo=True); store = self.store()
        result = publish_vk(store, 'example', 'Опубликовать example в ВК', execute=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(store.api.writes), 2)
        self.assertFalse((self.job / 'review.json').exists())

    def test_retry_reuses_plan_and_never_creates_duplicate(self):
        self.good('vk'); store = self.store()
        publish_vk(store, 'example', 'Опубликовать example в ВК', True)
        result = publish_vk(store, 'example', 'Опубликовать example в ВК', True)
        self.assertTrue(result['already_applied'])
        self.assertEqual(len(store.api.writes), 1)
        self.assertEqual(len(list(local(self.root, 'plans').glob('*.json'))), 1)

    def test_uncertain_network_cannot_be_retried_as_new_post(self):
        self.good('vk'); store = self.store(); store.api.fail_after = True
        first = publish_vk(store, 'example', 'Опубликовать example в ВК', True)
        self.assertFalse(first['ok'])
        with self.assertRaises(VKError):
            publish_vk(store, 'example', 'Опубликовать example в ВК', True)
        self.assertEqual(len(store.api.writes), 1)
        self.assertEqual(len(list(local(self.root, 'plans').glob('*.json'))), 1)

    def test_changed_published_packet_requires_update(self):
        self.good('vk'); store = self.store()
        publish_vk(store, 'example', 'Опубликовать example в ВК', True)
        self.change('pack.json', lambda p: p['formats']['vk'].update(text='Другой текст. [[fact]]'))
        with self.assertRaises(VKError):
            publish_vk(store, 'example', 'Опубликовать example в ВК', True)
        self.assertEqual(len(store.api.writes), 1)

    def test_native_article_not_silently_published_as_wall_post(self):
        self.good('article'); store = self.store()
        with self.assertRaises(VKError):
            publish_vk(store, 'example', 'Опубликовать article в ВК', True)
        self.assertFalse(store.api.writes)

    def test_status_understands_direct_authorization(self):
        self.good('vk'); self.store()
        authorize(self.root, 'example', 'vk', 'Опубликовать example в ВК')
        args = parser().parse_args(['--workspace', str(self.root), 'status', 'example'])
        result = dispatch(args)
        self.assertEqual(result['approval'], 'current')
        self.assertEqual(result['authorization_mode'], 'user_instruction')

    def test_static_direct_request_works_without_human_review(self):
        self.good('article')
        cfg = read_json(self.root / 'bamboo.json'); cfg['site_url'] = 'https://example.org'; write_json(self.root / 'bamboo.json', cfg)
        authorize(self.root, 'example', 'static', 'Собрать статью example для сайта')
        result = publishing.publish_static(self.root, 'example')
        self.assertTrue((self.root / 'site/journal/example/index.html').exists())
        self.assertFalse((self.job / 'review.json').exists())

    def test_mcp_direct_tool_available_and_dry_run_works(self):
        self.good('vk'); store = self.store(); before = len(store.api.calls)
        with patch('bamboo.vk.mcp.Store', return_value=store):
            result = call(self.root, 'vk_publish', {'slug': 'example', 'request': 'Опубликовать example в ВК'})
        self.assertTrue(result['dry_run'])
        self.assertEqual(len(store.api.calls), before)

    def test_other_channel_authorization_does_not_hide_manual_approval(self):
        self.approved('article')
        self.store()
        authorize(self.root, 'example', 'vk', 'Опубликовать example в ВК')
        approved = quality.require_approval(self.root, 'example', 'wordpress')
        self.assertEqual(approved['reviewer'], 'TEST ONLY')

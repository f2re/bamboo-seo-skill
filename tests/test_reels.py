"""Формат reel. Видео здесь — несколько байт-заглушек, не настоящая съёмка мастера."""
import json

from test_bamboo import Fixture
from bamboo import publishing, quality, reels
from bamboo.core import new_job, read_json, write_json, write_text

FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42"


class ReelFixture(Fixture):
    def reel(self, **overrides):
        if not (self.job / 'pack.json').exists():
            self.good('reel')
            (self.job / 'raw' / 'carve.mp4').write_bytes(FAKE_MP4)
            (self.job / 'raw' / 'bowl.mp4').write_bytes(FAKE_MP4)
        pack = read_json(self.job / 'pack.json')
        item = pack['formats']['reel'] = {'claims': ['fact']}
        item.update(
            text='Пиалу вырезали из цельного блока глины, внутренний объём выбран петлёй. [[fact]]',
            cta='Отправьте тому, кто пьёт чай из кружки.',
            hook='Эта чаша была куском глины',
            beats=[
                {'start': 0, 'end': 2, 'shot': 'Нож входит в блок', 'onscreen': 'Эта чаша была куском глины',
                 'clip': 'content/jobs/example/raw/carve.mp4', 'source_in': 4},
                {'start': 2, 'end': 7, 'shot': 'Петля выбирает объём', 'onscreen': 'Объём 200 мл [[fact]]',
                 'clip': 'content/jobs/example/raw/carve.mp4', 'source_in': 30},
                {'start': 7, 'end': 11, 'shot': 'Готовая пиала в руке', 'onscreen': 'Снова блок | и снова чаша',
                 'clip': 'content/jobs/example/raw/bowl.mp4'},
            ],
            footage=[
                {'path': 'content/jobs/example/raw/carve.mp4', 'origin': 'camera', 'rights_confirmed': True},
                {'path': 'content/jobs/example/raw/bowl.mp4', 'origin': 'camera', 'rights_confirmed': True},
            ],
            hashtags=['#керамика', '#пиала', '#куринуки'],
        )
        item.update(overrides)
        write_json(self.job / 'pack.json', pack)
        return pack

    def codes(self, report, level='errors'):
        return [x['code'] for x in report[level]]


class ReelJobTests(ReelFixture):
    def test_new_job_has_reel_fields_and_raw_folder(self):
        new_job(self.root, 'first-reel', 'Куринуки', ['reel'], [])
        job = self.root / 'content/jobs/first-reel'
        item = read_json(job / 'pack.json')['formats']['reel']
        self.assertEqual(item['beats'], [])
        self.assertEqual(item['hashtags'], [])
        self.assertTrue((job / 'raw' / 'README.md').is_file())

    def test_other_formats_unchanged(self):
        new_job(self.root, 'post', 'Пиала', ['vk'], [])
        item = read_json(self.root / 'content/jobs/post/pack.json')['formats']['vk']
        self.assertEqual(set(item), {'text', 'cta', 'claims'})
        self.assertFalse((self.root / 'content/jobs/post/raw').exists())


class ReelValidationTests(ReelFixture):
    def test_good_reel_passes(self):
        self.reel()
        report = quality.validate(self.root, 'example')
        self.assertTrue(report['ok'], report['errors'])

    def test_missing_hook_beats_and_footage_fail(self):
        self.reel(hook='', beats=[], footage=[])
        codes = self.codes(quality.validate(self.root, 'example'))
        for code in ('reel_hook', 'reel_beats', 'reel_footage'):
            self.assertIn(code, codes)

    def test_six_hashtags_rejected_cyrillic_counted(self):
        self.reel(hashtags=['#керамика', '#пиала', '#тяван', '#чай', '#куринуки', '#глина'])
        self.assertIn('reel_hashtags', self.codes(quality.validate(self.root, 'example')))

    def test_inline_hashtags_count_towards_limit(self):
        self.reel(text='Пиала из цельного блока. #чай #глина #ручнаяработа [[fact]]')
        self.assertIn('reel_hashtags', self.codes(quality.validate(self.root, 'example')))

    def test_bad_and_duplicate_hashtags_rejected(self):
        for tags in (['керамика'], ['#две слова'], ['#Пиала', '#пиала']):
            with self.subTest(tags=tags):
                self.reel(hashtags=tags)
                self.assertIn('reel_hashtags', self.codes(quality.validate(self.root, 'example')))

    def test_number_on_screen_needs_claim(self):
        beats = [{'start': 0, 'end': 2, 'shot': 'Кадр', 'onscreen': 'Объём 350 мл'}]
        self.reel(beats=beats)
        errors = [e['message'] for e in quality.validate(self.root, 'example')['errors']]
        self.assertTrue(any('350 мл' in e for e in errors), errors)

    def test_hook_number_needs_claim(self):
        self.reel(hook='Чаша на 500 мл')
        errors = [e['message'] for e in quality.validate(self.root, 'example')['errors']]
        self.assertTrue(any('500 мл' in e for e in errors), errors)

    def test_hashtag_number_needs_claim(self):
        self.reel(hashtags=['#350мл'])
        errors = [e['message'] for e in quality.validate(self.root, 'example')['errors']]
        self.assertTrue(any('350мл' in e for e in errors), errors)

    def test_unregistered_marker_on_screen_rejected(self):
        beats = [{'start': 0, 'end': 2, 'shot': 'Кадр', 'onscreen': 'Текст [[other]]'}]
        self.reel(beats=beats)
        errors = [e['message'] for e in quality.validate(self.root, 'example')['errors']]
        self.assertTrue(any('незарегистрированное' in e for e in errors), errors)

    def test_health_claim_on_screen_blocked(self):
        beats = [{'start': 0, 'end': 2, 'shot': 'Кадр', 'onscreen': 'Глина очищает воду'}]
        self.reel(beats=beats)
        self.assertIn('health_claim', self.codes(quality.validate(self.root, 'example')))

    def test_overlapping_and_broken_beats_rejected(self):
        for beats in ([{'start': 0, 'end': 3, 'shot': 'А'}, {'start': 2, 'end': 4, 'shot': 'Б'}],
                      [{'start': 3, 'end': 1, 'shot': 'А'}],
                      [{'start': True, 'end': 2, 'shot': 'А'}],
                      [{'start': 0, 'end': 2, 'shot': ''}],
                      ['кадр']):
            with self.subTest(beats=beats):
                self.reel(beats=beats)
                self.assertIn('reel_beats', self.codes(quality.validate(self.root, 'example')))

    def test_clip_outside_footage_rejected(self):
        beats = [{'start': 0, 'end': 2, 'shot': 'А', 'clip': 'content/jobs/example/raw/missing.mp4'}]
        self.reel(beats=beats)
        self.assertIn('reel_beats', self.codes(quality.validate(self.root, 'example')))

    def test_footage_rules(self):
        cases = [
            [{'path': 'content/media/test.png', 'origin': 'camera', 'rights_confirmed': True}],
            [{'path': 'content/jobs/example/raw/none.mp4', 'origin': 'camera', 'rights_confirmed': True}],
            [{'path': 'content/jobs/example/raw/carve.mp4', 'origin': 'stock', 'rights_confirmed': True}],
            [{'path': 'content/jobs/example/raw/carve.mp4', 'origin': 'camera'}],
            [{'path': '../escape.mp4', 'origin': 'camera', 'rights_confirmed': True}],
        ]
        for footage in cases:
            with self.subTest(footage=footage):
                self.reel(footage=footage, beats=[{'start': 0, 'end': 6, 'shot': 'Кадр'}])
                self.assertIn('reel_footage', self.codes(quality.validate(self.root, 'example')))

    def test_media_video_folder_allowed(self):
        (self.root / 'content/media/video').mkdir(parents=True)
        (self.root / 'content/media/video/kiln.mov').write_bytes(FAKE_MP4)
        footage = [{'path': 'content/media/video/kiln.mov', 'origin': 'camera', 'rights_confirmed': True}]
        beats = [{'start': 0, 'end': 2, 'shot': 'Печь', 'clip': 'content/media/video/kiln.mov'},
                 {'start': 2, 'end': 6, 'shot': 'Чаша', 'clip': 'content/media/video/kiln.mov'}]
        self.reel(footage=footage, beats=beats)
        self.assertTrue(quality.validate(self.root, 'example')['ok'])

    def test_style_signals_are_warnings(self):
        beats = [{'start': 1, 'end': 5, 'shot': 'Долгий кадр'}, {'start': 5, 'end': 13, 'shot': 'Ещё дольше'}]
        self.reel(hook='Всем привет, сегодня я покажу вам как мы делаем чашу', beats=beats)
        report = quality.validate(self.root, 'example')
        self.assertTrue(report['ok'], report['errors'])
        warnings = self.codes(report, 'warnings')
        self.assertGreaterEqual(warnings.count('reel_hook'), 2)
        self.assertGreaterEqual(warnings.count('reel_beats'), 3)

    def test_duration_outside_range_is_warning(self):
        self.reel(beats=[{'start': 0, 'end': 2, 'shot': 'А'}])
        report = quality.validate(self.root, 'example')
        self.assertTrue(report['ok'])
        self.assertIn('reel_duration', self.codes(report, 'warnings'))

    def test_caption_limits(self):
        self.reel(text='Пиала из цельного блока, ' * 10 + '[[fact]]')
        report = quality.validate(self.root, 'example')
        self.assertIn('reel_caption', self.codes(report, 'warnings'))
        self.reel(text='Слово ' * 400 + '[[fact]]')
        self.assertIn('reel_caption', self.codes(quality.validate(self.root, 'example')))

    def test_cover_inside_reel(self):
        self.reel(cover_at=5)
        self.assertTrue(quality.validate(self.root, 'example')['ok'])
        self.reel(cover_at=30)
        self.assertIn('reel_cover', self.codes(quality.validate(self.root, 'example')))


class ReelExportTests(ReelFixture):
    def test_export_writes_montage_files(self):
        self.reel(cover_at=9)
        result = publishing.export(self.root, 'example')
        dest = self.root / 'exports/example'
        caption = (dest / 'reel.txt').read_text(encoding='utf-8')
        self.assertIn('#керамика #пиала #куринуки', caption)
        self.assertNotIn('[[fact]]', caption)
        cues = (dest / 'reel-cues.txt').read_text(encoding='utf-8').splitlines()
        # Текст первого кадра совпадает с хуком: он уже на хук-карточке, в титры не дублируется.
        self.assertEqual(cues, ['00:00:02.000 --> 00:00:07.000 Объём 200 мл',
                                '00:00:07.000 --> 00:00:11.000 Снова блок | и снова чаша'])
        project = read_json(dest / 'reel-project.json')
        self.assertEqual(project['clips'][0], {'src': '../../content/jobs/example/raw/carve.mp4', 'in': 4, 'out': 6})
        self.assertEqual(project['clips'][2], {'src': '../../content/jobs/example/raw/bowl.mp4'})
        self.assertEqual(project['graphics'][0]['title'], 'Эта чаша была куском глины')
        self.assertEqual(project['graphics'][0]['end'], 2.0)
        self.assertEqual(project['check'], {'platform': 'reels'})
        manifest = read_json(dest / 'reel.json')
        self.assertEqual(manifest['duration_s'], 11.0)
        self.assertIn('reel', (dest / 'preview.html').read_text(encoding='utf-8'))
        self.assertFalse(result['published'])

    def test_preview_escapes_html(self):
        beats = [{'start': 0, 'end': 6, 'shot': '<script>x</script>', 'clip': 'content/jobs/example/raw/carve.mp4'}]
        self.reel(beats=beats)
        publishing.export(self.root, 'example')
        page = (self.root / 'exports/example/preview.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>x</script>', page)

    def test_project_keys_are_ones_render_accepts(self):
        self.reel()
        project = reels.project('example', read_json(self.job / 'pack.json')['formats']['reel'])
        allowed = {'output', 'clips', 'fit', 'captions', 'graphics', 'loudness', 'export', 'check'}
        self.assertLessEqual(set(project), allowed)
        # frame с аспектом заставил бы render.py склеить клипы в 9:16 с полями — не задаём его.
        self.assertNotIn('frame', project)
        self.assertEqual(project['fit'], {'aspect': '9:16', 'fit': 'crop'})
        for clip in project['clips']:
            self.assertLessEqual(set(clip), {'src', 'in', 'out'})

    def test_stale_reel_files_removed(self):
        self.reel()
        publishing.export(self.root, 'example')
        pack = read_json(self.job / 'pack.json')
        pack['formats'] = {'card': {'text': 'Тестовый факт — описание формы. [[fact]]',
                                    'cta': 'Задайте вопрос мастеру.', 'claims': ['fact']}}
        write_json(self.job / 'pack.json', pack)
        brief = read_json(self.job / 'brief.json')
        brief['formats'] = ['card']
        write_json(self.job / 'brief.json', brief)
        publishing.export(self.root, 'example')
        dest = self.root / 'exports/example'
        for name in ('reel.txt', 'reel-cues.txt', 'reel-project.json', 'reel.json'):
            self.assertFalse((dest / name).exists(), name)

    def test_export_does_not_touch_raw(self):
        self.reel()
        before = (self.job / 'raw/carve.mp4').read_bytes()
        publishing.export(self.root, 'example')
        self.assertEqual((self.job / 'raw/carve.mp4').read_bytes(), before)
        self.assertFalse((self.root / 'exports/example/reel.mp4').exists())

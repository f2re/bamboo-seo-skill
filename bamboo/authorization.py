"""A scoped user instruction is authorization, not a fabricated human review."""
from __future__ import annotations

from pathlib import Path

from .core import BambooError, config, file_digest, job_path, now, read_json, safe, snapshot, write_json

CHANNELS = ('vk', 'wordpress', 'static')


def target(root: Path, channel: str) -> str:
    if channel not in CHANNELS:
        raise BambooError('Канал разрешения: vk, wordpress или static')
    if channel == 'vk':
        cfg = read_json(safe(root, '.bamboo/vk/config.json'))
        if type(cfg.get('group_id')) is not int or cfg['group_id'] <= 0:
            raise BambooError('Сначала vk check: нужен числовой ID сообщества')
        return 'vk:' + str(cfg['group_id'])
    from .quality import http_url
    cfg = config(root)
    return http_url(cfg.get('wordpress_url' if channel == 'wordpress' else 'site_url'), https_only=True).rstrip('/')


def authorize(root: Path, name: str, channel: str, request: str) -> dict:
    """Call only for an actual user instruction; no NLP guess is treated as consent."""
    from .quality import lint, validate
    if not isinstance(request, str) or not 5 <= len(request.strip()) <= 4096:
        raise BambooError('Запишите фактическое поручение пользователя, а не пустое разрешение')
    if any(x['code'] in ('secret', 'placeholder') and x['level'] == 'error' for x in lint(request)):
        raise BambooError('Поручение содержит секрет или шаблон')
    report = validate(root, name)
    if not report['ok']:
        raise BambooError('Сначала устраните ошибки validate')
    data = {'schema_version': 1, 'mode': 'user_instruction', 'channel': channel,
            'target': target(root, channel), 'request': request.strip(),
            'content_hash': snapshot(root, name), 'created_at': now(),
            'human_review_claimed': False,
            'warnings': report['warnings']}
    write_json(job_path(root, name) / 'authorization.json', data)
    return data


def require_authorization(root: Path, name: str, channel: str) -> dict:
    data = read_json(job_path(root, name) / 'authorization.json')
    if (data.get('mode') != 'user_instruction' or data.get('channel') != channel
            or not isinstance(data.get('request'), str) or len(data['request'].strip()) < 5
            or data.get('content_hash') != snapshot(root, name)
            or data.get('target') != target(root, channel)):
        raise BambooError('Поручение относится к другой версии, площадке или сообществу')
    return data


def publish_vk(store, name: str, request: str, execute: bool = False, publish_date=None) -> dict:
    """One explicit operation; existing Store performs all writes and read-back checks."""
    from .quality import validate
    from .vk.common import VKError, local, read, write
    if type(execute) is not bool:
        raise VKError('execute должен быть true/false')
    report = validate(store.root, name)
    if not report['ok']:
        raise VKError('Контент не прошёл validate')
    pack = read_json(job_path(store.root, name) / 'pack.json')
    if 'vk' not in pack['formats']:
        raise VKError('Для стены нужен формат vk. Нативная статья ВК не равна wall.post.')
    if not isinstance(request, str) or len(request.strip()) < 5:
        raise VKError('Нужно конкретное поручение на публикацию')
    if not execute:
        return {'dry_run': True, 'network_called': False, 'slug': name,
                'target': store.cfg.get('group_id') or store.cfg['community'],
                'request': request, 'validation': report}
    store.check()
    # One package has one post. Reuse its plan on retry, never recreate blindly.
    ledger = local(store.root, 'publications/' + name + '.json')
    current_hash = snapshot(store.root, name)
    if ledger.exists():
        record = read(ledger)
        if (record['content_hash'] != current_hash or record['group_id'] != store.group_id
                or record.get('publish_date') != publish_date):
            raise VKError('Пакет уже связан с публикацией: прочитайте результат и редактируйте существующий пост, не создавайте дубликат')
        plan = store.show(record['plan_id'])
    else:
        authorize(store.root, name, 'vk', request)
        plan = store.plan_post(name, request, publish_date)
        write(ledger, {'content_hash': current_hash, 'group_id': store.group_id,
                       'publish_date': publish_date, 'plan_id': plan['plan_id']})
    # The direct instruction covers this single generated post and its own photos.
    # The confirmation hash is an integrity check, not a second human interaction.
    return store.apply(plan['plan_id'], plan['confirmation'], True)

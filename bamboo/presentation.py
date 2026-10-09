"""Подача материала: мягкие редакторские ориентиры, не правила алгоритма ВК."""
from __future__ import annotations

import html
import re

PROFILES = {
    'vk-detail': ('vk', 200, 600, 'chars'),
    'vk-question': ('vk', 350, 850, 'chars'),
    'vk-compare': ('vk', 450, 1000, 'chars'),
    'vk-process': ('vk', 250, 800, 'chars'),
    'vk-offer': ('vk', 300, 900, 'chars'),
    'vk-longread': ('vk', 900, 1800, 'chars'),
    'article-guide': ('article', 450, 900, 'words'),
    'article-story': ('article', 600, 1100, 'words'),
    'article-research': ('article', 900, 1500, 'words'),
}


def blocks(text: str) -> list[str]:
    return [p.strip() for p in re.split(r'\n\s*\n', text.strip()) if p.strip()]


def vk_plain(text: str) -> str:
    """Обычный текст стены: не обещает, что Markdown станет форматированием ВК."""
    text = re.sub(r'\[([^\]\n]+)\]\((https?://[^\s)]+)\)', r'\1 → \2', text)
    # Protect literal URL characters while removing authoring markup.
    parts = re.split(r'(https?://[^\s]+)', text)
    for i in range(0, len(parts), 2):
        value = re.sub(r'(?m)^#{1,6}\s+', '', parts[i])
        value = re.sub(r'\*\*([^*\n]+)\*\*', r'\1', value)
        value = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'\1', value)
        parts[i] = re.sub(r'(?m)^- ', '• ', value)
    return ''.join(parts).strip()


def inline(text: str) -> str:
    """Small escaped Markdown subset; never interpret user HTML."""
    result = []
    for part in re.split(r'(\[[^\]\n]+\]\(https?://[^\s)]+\))', text):
        match = re.fullmatch(r'\[([^\]]+)\]\(([^)]+)\)', part)
        if match:
            from .quality import http_url
            from .core import BambooError
            try:
                url = http_url(match[2])
            except BambooError:
                result.append(html.escape(match[1]))
            else:
                result.append('<a href="' + html.escape(url, quote=True) + '">' + html.escape(match[1]) + '</a>')
        else:
            value = html.escape(part)
            value = re.sub(r'\*\*([^*\n]+)\*\*', r'<strong>\1</strong>', value)
            value = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<em>\1</em>', value)
            result.append(value)
    return ''.join(result)


def render_block(block: str) -> str:
    if re.fullmatch(r'(?:---+|\*\*\*+)', block):
        return '<hr>'
    if re.match(r'^#{1,6} ', block):
        head, _, rest = block.partition('\n')
        level = min(6, max(2, len(head) - len(head.lstrip('#'))))
        return f'<h{level}>{inline(head.lstrip("#").strip())}</h{level}>' + (f'<p>{inline(rest)}</p>' if rest else '')
    lines = block.splitlines()
    if all(line.startswith('> ') for line in lines):
        return '<blockquote><p>' + inline(' '.join(line[2:] for line in lines)) + '</p></blockquote>'
    if all(re.match(r'^[-•] ', line) for line in lines):
        return '<ul>' + ''.join('<li>' + inline(line[2:]) + '</li>' for line in lines) + '</ul>'
    if all(re.match(r'^\d+[.)] ', line) for line in lines):
        start = int(re.match(r'^\d+', lines[0])[0])
        return f'<ol start="{start}">' + ''.join('<li>' + inline(re.sub(r'^\d+[.)] ', '', line)) + '</li>' for line in lines) + '</ol>'
    return '<p>' + inline(block).replace('\n', '<br>') + '</p>'


def markdown(text: str) -> str:
    return '\n'.join(render_block(p) for p in blocks(text))


def inspect_style(fmt: str, text: str, profile: str | None = None) -> dict:
    paragraphs = blocks(text)
    lengths = [len(p) for p in paragraphs]
    findings = []
    def warn(code, message):
        findings.append({'level': 'warning', 'code': code, 'message': message})
    if profile is not None and (not isinstance(profile, str) or profile not in PROFILES or PROFILES[profile][0] != fmt):
        findings.append({'level': 'error', 'code': 'style_profile', 'message': f'{fmt}: неизвестный или несовместимый профиль'})
    elif profile:
        _, low, high, unit = PROFILES[profile]
        value = len(re.findall(r'\b[\w-]+\b', text)) if unit == 'words' else len(text)
        if not low <= value <= high:
            warn('profile_length', f'{profile}: {value} {unit}; ориентир {low}–{high}. Не дописывать ради минимума; сохранить параметры.')
    if fmt in ('vk', 'article'):
        limit = 420 if fmt == 'vk' else 750
        for i, size in enumerate(lengths, 1):
            if size > limit:
                warn('dense_block', f'{fmt}: блок {i}, {size} знаков. Проверить смысловое деление, не рубить фразы механически.')
        if fmt == 'article' and len(text) > 2000 and not re.search(r'(?m)^#{1,3} ', text):
            warn('missing_sections', 'Статья без смысловых подзаголовков: добавьте точки входа по вопросам читателя.')
        if len(paragraphs) >= 8 and sum(len(p) < 70 for p in paragraphs) / len(paragraphs) > .7:
            warn('chopped_rhythm', 'Слишком много коротких обрывков: собрать связанные фразы, а не имитировать динамику.')
        if fmt == 'vk' and re.search(r'\*\*|(?m:^#{1,6} )', text):
            warn('vk_markup', 'ВК-стена: Markdown преобразуется в обычный текст; акценты создают композиция и переносы.')
    return {'profile': profile, 'paragraph_chars': lengths, 'findings': findings,
            'note': 'Эвристика редактора, не оценка авторства и не прогноз охватов.'}


def layout_findings(pack: dict) -> list[dict]:
    result = []
    article = pack.get('formats', {}).get('article')
    count = len(blocks(article.get('text', ''))) if article else 0
    for i, photo in enumerate(pack.get('photos', [])):
        if 'after_block' not in photo:
            continue
        value = photo['after_block']
        if not article or type(value) is not int or not 0 <= value <= count:
            result.append({'level': 'error', 'code': 'photo_placement',
                           'message': f'Фото {i + 1}: after_block — целое от 0 до {count} для формата article.'})
    return result

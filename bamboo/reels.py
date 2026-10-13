"""Reels для Instagram: проверка сценария и файлы для ffmpeg-skill.

Модуль не монтирует видео, не вызывает сеть и не публикует. Ориентиры ниже —
редакторские правила Bamboo и опубликованные ограничения Instagram, а не прогноз охватов.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

from .core import BambooError, safe, write_json, write_text

CAPTION_LIMIT = 2200  # предел подписи Instagram
FEED_WINDOW = 125  # примерно столько подписи видно до «ещё»
HASHTAG_LIMIT = 5  # лимит, объявленный Instagram в декабре 2025
HOOK_WORDS = 7
HOOK_SECONDS = 3.0
BEAT_SECONDS = 6.0
DURATION = (5.0, 60.0)
VIDEO = (".mp4", ".mov", ".m4v")
MARK = re.compile(r"\[\[([a-z0-9-]+)\]\]")
HASHTAG = re.compile(r"(?<![\w#])#\w+", re.UNICODE)
OPENERS = (
    (r"^(?:всем\s+)?привет\b", "приветствие вместо действия"),
    (r"^сегодня\s+(?:я\s+)?(?:расскажу|покажу|поговорим)", "анонс вместо действия"),
    (r"^в\s+этом\s+(?:видео|ролике)", "вступление про само видео"),
    (r"^(?:стоп\b|не\s+листай|досмотри)", "просьба о внимании, которое ещё не заслужено"),
)


def _issue(level: str, code: str, message: str) -> dict:
    return {"level": level, "code": code, "message": message}


def _clean(text: str) -> str:
    return MARK.sub("", text).strip()


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _beats(item: dict) -> list[dict]:
    beats = item.get("beats")
    return [b for b in beats if isinstance(b, dict)] if isinstance(beats, list) else []


def onscreen(item: dict) -> str:
    """Весь текст на экране: проверяется на факты и числа так же, как подпись."""
    parts = [item.get("hook") if isinstance(item.get("hook"), str) else ""]
    parts += [b["onscreen"] for b in _beats(item) if isinstance(b.get("onscreen"), str)]
    return "\n".join(p for p in parts if p)


def duration(item: dict) -> float:
    ends = [b["end"] for b in _beats(item) if _number(b.get("end"))]
    return float(max(ends)) if ends else 0.0


def hashtags(item: dict) -> list[str]:
    tags = item.get("hashtags", [])
    return [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []


def caption(item: dict) -> str:
    """Подпись в том виде, в каком её вставляют в Instagram."""
    parts = [_clean(item.get("text", "")), _clean(item.get("cta", "")), " ".join(hashtags(item))]
    return "\n\n".join(p for p in parts if p)


def _footage_findings(root: Path, name: str, item: dict, err) -> set[str]:
    footage = item.get("footage")
    if not isinstance(footage, list) or not footage:
        err("reel_footage", "reel: нужен footage — реальные исходники мастера в "
            f"content/jobs/{name}/raw/ или content/media/video/")
        return set()
    known = set()
    for i, clip in enumerate(footage, 1):
        if not isinstance(clip, dict) or not isinstance(clip.get("path"), str):
            err("reel_footage", f"reel: footage {i} — нужен объект с path")
            continue
        rel = clip["path"]
        if not (rel.startswith(f"content/jobs/{name}/raw/") or rel.startswith("content/media/video/")):
            err("reel_footage", f"reel: {rel} — исходники храните в content/jobs/{name}/raw/ или content/media/video/")
            continue
        try:
            path = safe(root, rel)
        except BambooError as exc:
            err("reel_footage", f"reel: {exc}")
            continue
        if path.suffix.lower() not in VIDEO:
            err("reel_footage", f"reel: {rel} — видео mp4, mov или m4v")
        elif not path.is_file():
            err("reel_footage", f"reel: нет файла {rel}")
        if clip.get("origin") != "camera" or clip.get("rights_confirmed") is not True:
            err("reel_footage", f"reel: {rel} — нужна собственная съёмка (origin=camera) и rights_confirmed=true")
        if rel in known:
            err("reel_footage", f"reel: повтор исходника {rel}")
        known.add(rel)
    return known


def findings(root: Path, name: str, item: dict) -> list[dict]:
    out: list[dict] = []
    err = lambda code, msg: out.append(_issue("error", code, msg))
    warn = lambda code, msg: out.append(_issue("warning", code, msg))

    hook = item.get("hook")
    if not isinstance(hook, str) or not hook.strip():
        err("reel_hook", "reel: нужен hook — текст на экране в первые 2–3 секунды")
    else:
        words = re.findall(r"[\w-]+", _clean(hook), re.UNICODE)
        if len(words) > HOOK_WORDS:
            warn("reel_hook", f"reel: хук {len(words)} слов; на экране читается до {HOOK_WORDS}")
        low = _clean(hook).casefold()
        for pattern, why in OPENERS:
            if re.search(pattern, low):
                warn("reel_hook", f"reel: хук начинается с «{why}»; начните с предмета или действия")

    known = _footage_findings(root, name, item, err)

    beats = item.get("beats")
    if not isinstance(beats, list) or not beats:
        err("reel_beats", "reel: нужна раскадровка beats — start, end и shot для каждого кадра")
    else:
        previous_end = 0.0
        valid = True
        for i, beat in enumerate(beats, 1):
            if not isinstance(beat, dict):
                err("reel_beats", f"reel: кадр {i} — нужен объект")
                valid = False
                continue
            start, end = beat.get("start"), beat.get("end")
            if not _number(start) or not _number(end) or start < 0 or end <= start:
                err("reel_beats", f"reel: кадр {i} — нужны числа start < end, в секундах")
                valid = False
                continue
            if start < previous_end - 1e-6:
                err("reel_beats", f"reel: кадр {i} начинается раньше конца предыдущего")
                valid = False
            if not isinstance(beat.get("shot"), str) or not beat["shot"].strip():
                err("reel_beats", f"reel: кадр {i} — опишите в shot, что видно в кадре")
            if "onscreen" in beat and not isinstance(beat["onscreen"], str):
                err("reel_beats", f"reel: кадр {i} — onscreen должен быть строкой")
            clip = beat.get("clip")
            if clip is not None and clip not in known:
                err("reel_beats", f"reel: кадр {i} ссылается на исходник вне footage: {clip}")
            source_in = beat.get("source_in")
            if source_in is not None and (not _number(source_in) or source_in < 0):
                err("reel_beats", f"reel: кадр {i} — source_in: неотрицательное число секунд")
            if source_in is not None and clip is None:
                err("reel_beats", f"reel: кадр {i} — source_in без clip")
            if end - start > BEAT_SECONDS:
                warn("reel_beats", f"reel: кадр {i} длится {end - start:g} с; статичный кадр дольше "
                     f"{BEAT_SECONDS:g} с — место, где уходят. Смените план или разделите")
            previous_end = max(previous_end, end)
        first = beats[0] if isinstance(beats[0], dict) else {}
        if valid and _number(first.get("start")) and first["start"] > 0:
            warn("reel_beats", "reel: первый кадр начинается не с 0 с; действие должно быть в первом кадре")
        if valid and _number(first.get("end")) and first["end"] > HOOK_SECONDS:
            warn("reel_beats", f"reel: первый кадр с хуком длится дольше {HOOK_SECONDS:g} с")
        total = duration(item)
        if valid and not DURATION[0] <= total <= DURATION[1]:
            warn("reel_duration", f"reel: {total:g} с; для первых роликов ориентир "
                 f"{DURATION[0]:g}–{DURATION[1]:g} с. Не растягивать ради нормы")

    tags = item.get("hashtags", [])
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        err("reel_hashtags", "reel: hashtags — массив строк вида #керамика")
        tags = []
    for tag in tags:
        if not HASHTAG.fullmatch(tag):
            err("reel_hashtags", f"reel: неверный хэштег {tag!r}: решётка и слово без пробелов")
    if len({t.casefold() for t in tags}) != len(tags):
        err("reel_hashtags", "reel: повтор хэштега")
    body = _clean(item.get("text", "")) + "\n" + _clean(item.get("cta", ""))
    inline = HASHTAG.findall(body)
    total_tags = len({t.casefold() for t in tags} | {t.casefold() for t in inline})
    if total_tags > HASHTAG_LIMIT:
        err("reel_hashtags", f"reel: {total_tags} хэштегов; Instagram принимает не больше {HASHTAG_LIMIT}")
    if inline:
        warn("reel_hashtags", "reel: хэштеги внутри подписи; держите их в поле hashtags, после текста")

    text = _clean(item.get("text", ""))
    first_line = text.splitlines()[0] if text else ""
    if len(first_line) > FEED_WINDOW:
        warn("reel_caption", f"reel: первая строка подписи {len(first_line)} знаков; до «ещё» видно около "
             f"{FEED_WINDOW}. Суть — в начало")
    full = caption(item)
    if len(full) > CAPTION_LIMIT:
        err("reel_caption", f"reel: подпись {len(full)} знаков; предел Instagram {CAPTION_LIMIT}")

    cover = item.get("cover_at")
    if cover is not None and (not _number(cover) or cover < 0 or (duration(item) and cover > duration(item))):
        err("reel_cover", "reel: cover_at — секунда внутри ролика для кадра обложки")
    return out


def _clock(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    h, rest = divmod(millis, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def cues(item: dict) -> str:
    """Титры кадров в формате `caption.py --text` из ffmpeg-skill; хук идёт отдельной карточкой."""
    hook = _clean(item.get("hook", "")).casefold() if isinstance(item.get("hook"), str) else ""
    lines = []
    for beat in _beats(item):
        text = _clean(beat.get("onscreen", "")) if isinstance(beat.get("onscreen"), str) else ""
        if hook and text.casefold() == hook:
            continue  # тот же текст уже на хук-карточке; второй раз поверх — дубль
        if text and _number(beat.get("start")) and _number(beat.get("end")):
            lines.append(f"{_clock(beat['start'])} --> {_clock(beat['end'])} " + " | ".join(text.splitlines()))
    return "\n".join(lines) + ("\n" if lines else "")


def project(name: str, item: dict) -> dict:
    """Проект render.py из ffmpeg-skill; пути — относительно exports/<slug>/."""
    clips = []
    for beat in _beats(item):
        if not isinstance(beat.get("clip"), str):
            continue
        clip = {"src": "../../" + beat["clip"]}
        if _number(beat.get("source_in")):
            clip["in"] = beat["source_in"]
            clip["out"] = round(beat["source_in"] + beat["end"] - beat["start"], 3)
        clips.append(clip)
    # Не "frame": при нескольких клипах render.py склеивает их сразу в кадр 9:16 с полями.
    # Склейка в размере первого клипа и затем обрезка до 9:16 не оставляет чёрных полос.
    data = {"output": "reel.mp4", "clips": clips, "fit": {"aspect": "9:16", "fit": "crop"},
            "loudness": {"lufs": -14, "tp": -1.0},
            "export": {"preset": "reels", "normalize": True, "fit": "crop"},
            "check": {"platform": "reels"}}
    if cues(item):
        data["captions"] = {"text": "reel-cues.txt", "animate": "pop", "position": "bottom"}
    hook = _clean(item.get("hook", "")) if isinstance(item.get("hook"), str) else ""
    if hook:
        first = _beats(item)[0] if _beats(item) else {}
        end = first.get("end") if _number(first.get("end")) else HOOK_SECONDS
        data["graphics"] = [{"template": "hook", "title": hook, "start": 0, "end": min(float(end), HOOK_SECONDS)}]
    return data


def export(dest: Path, name: str, item: dict) -> dict:
    """Пишет reel.txt, reel-cues.txt, reel-project.json и reel.json. Видео не создаётся."""
    write_text(dest / "reel.txt", caption(item) + "\n")
    cue_text = cues(item)
    cue_path = dest / "reel-cues.txt"
    if cue_text:
        write_text(cue_path, cue_text)
    else:
        cue_path.unlink(missing_ok=True)
    plan = project(name, item)
    write_json(dest / "reel-project.json", plan)
    manifest = {
        "hook": _clean(item.get("hook", "")), "duration_s": duration(item),
        "beats": [{k: b.get(k) for k in ("start", "end", "shot", "onscreen", "clip", "source_in") if k in b}
                  for b in _beats(item)],
        "footage": [c["path"] for c in item.get("footage", []) if isinstance(c, dict) and "path" in c],
        "caption": caption(item), "hashtags": hashtags(item), "cover_at": item.get("cover_at"),
        "files": {"caption": "reel.txt", "cues": "reel-cues.txt" if cue_text else None,
                  "project": "reel-project.json"},
        "montage": {
            "tool": "ffmpeg-skill",
            "render": f"python3 <ffmpeg-skill>/scripts/render.py exports/{name}/reel-project.json",
            "check": f"python3 <ffmpeg-skill>/scripts/check.py exports/{name}/reel.mp4 --platform reels",
            "look": f"python3 <ffmpeg-skill>/scripts/look.py exports/{name}/reel.mp4 --tiles 3x2",
        },
        "note": "Экспорт не монтирует видео и не публикует в Instagram. Исходники в raw/ не изменяются.",
    }
    write_json(dest / "reel.json", manifest)
    return manifest


def remove_export(dest: Path) -> None:
    for filename in ("reel.txt", "reel-cues.txt", "reel-project.json", "reel.json"):
        (dest / filename).unlink(missing_ok=True)


def preview(item: dict) -> str:
    """Безопасный HTML-фрагмент для превью: хук, раскадровка, подпись."""
    esc = html.escape
    rows = "".join(
        f"<tr><td>{b.get('start', '')}–{b.get('end', '')} с</td><td>{esc(str(b.get('shot', '')))}</td>"
        f"<td>{esc(_clean(b.get('onscreen', '')) if isinstance(b.get('onscreen'), str) else '')}</td></tr>"
        for b in _beats(item))
    return (f"<p><strong>Хук на экране:</strong> {esc(_clean(item.get('hook', '')) if isinstance(item.get('hook'), str) else '')}</p>"
            f"<table><tr><th>Время</th><th>Кадр</th><th>Текст на экране</th></tr>{rows}</table>"
            f"<div class=\"vk-post\">{esc(caption(item))}</div>")

# Changelog

## 1.1.0 — 2026-09-22

### Search intelligence

- Google Search Console: отдельные page и page_query слои участвуют в отчёте без двойного счёта.
- Яндекс Query Analytics: URL и QUERY сохраняются раздельно; popular complementary URL остаётся только подсказкой.
- Добавлен явный workflow расширенной Яндекс URL×query β-выгрузки и импорт документированного CSV.
- Региональные строки расширенной выгрузки сохраняются отдельно; page_query пересчитывается из всей накопленной детализации.
- Добавлены query clusters, intent hints, intent mismatch candidates и candidates на несколько URL.
- Кластеры не смешивают позиции разных поисковых систем.
- Добавлена воронка search click → product click → lead → order только при явной атрибуции source.

### Commerce SEO

- Добавлен commerce graph для products, collections и content jobs.
- Добавлены SEO-кластеры, связи и предложения внутренней перелинковки.
- Добавлен аудит пригодности собственной purchase page для Product/Offer structured data без ложной разметки статей.
- Экспорт ВК получил attachment order, vk_product_id и безопасные UTM suggestions.

### Domain knowledge and quality

- Добавлен runtime-слой предметного словаря с контролируемыми русскими морфологическими patterns.
- Разделены техника, материал, происхождение, назначение и стиль.
- Evidence-sensitive свойства должны подтверждаться связанным claim именно об этом свойстве.
- Проверка распространяется на текст, CTA, title, description, alt и caption.
- Утвердительные медицинские/псевдонаучные обещания блокируются; явное опровержение отмечается предупреждением.

### Engineering

- `init` идемпотентно мигрирует существующий workspace и создаёт новые каталоги без перезаписи пользовательских данных.
- Расширены behavioural evals и unit-тесты.
- Поддерживается Python 3.10–3.13 на Linux и Python 3.12 на Windows/macOS.

## 1.0.0 — 2026-09-21

Первый независимый релиз Bamboo Pottery editorial toolkit: шесть skills, пять read-only review roles,
human approval gate, WordPress/static publishing, Google/Yandex analytics, portable project installer
для Codex, Claude Code и Antigravity.

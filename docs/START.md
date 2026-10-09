# Один рабочий материал

Откройте проект в выбранном ИИ. Python 3.10+, без Docker и лишних сервисов.

```sh
python bamboo.py init
python bamboo.py new first-post --topic "Одна деталь чайной пиалы" --formats vk
```

Задание агенту: «Используй bamboo-content. По реальным сведениям о выбранной пиале напиши
короткий пост в профиле vk-detail. Не теряй объем и размеры, когда они нужны для выбора».

Агент заполняет brief/sources/claims/pack. Поле style внутри formats.vk — vk-detail.
Реальные фотографии кладутся в content/media; подпись объясняет то, что видно, а не
сочиняет свойства. Человеческая анкета на этом этапе не требуется.

```sh
python bamboo.py validate first-post
python bamboo.py style-check first-post
python bamboo.py export first-post
```

Откройте exports/first-post/preview.html. Для поста отображается обычный текст стены;
для статьи — подзаголовки, акценты и изображения в местах after_block. Сценарии подачи:
[Стили](PUBLICATION_STYLE.md).

При поручении «напиши и опубликуй» и настроенном ВК агент завершает работу:

```sh
python bamboo.py vk publish first-post --request "Напиши и опубликуй короткий пост о пиале в Bamboo Pottery" --execute
```

Настройка связи: [ВК](VK_STORE.md). Выпуск без повторных вопросов: [Политика](PUBLISHING_POLICY.md).
Если пользователь просил только текст или превью, внешней отправки нет. Ручная вычитка
через review-template/approve доступна по желанию, но не является обязательным маршрутом.
После отправки нужен реальный ID/URL и проверка результата. Локальный файл — не пост в ВК.

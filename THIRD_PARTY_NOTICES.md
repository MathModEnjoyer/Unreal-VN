# Сторонние компоненты и материалы

Этот репозиторий содержит собственный исходный код и ассеты, а не установленные
деревья библиотек или веса моделей. Зависимости перечислены в requirements-файлах
и `frontend/package-lock.json` и устанавливаются из соответствующих источников.
Их upstream-лицензии сохраняются; лицензия Story Engine их не заменяет.

## Музыка и эмбиент

Полная атрибуция 17 треков **CC BY 3.0** доступна в
[CREDITS.html](backend/music_catalogs/CREDITS.html) и
[music_catalog.json](backend/music_catalogs/music_catalog.json): название, автор,
участники/сэмплы, источник и ссылка на лицензию для каждого трека.
Файлы треков скачиваются при воспроизведении, не включены в репозиторий.
Ссылка на лицензию: <https://creativecommons.org/licenses/by/3.0/>.

Поставляемые `rain.ogg` (Ylmir) и `storm.ogg` (WuxiaScrub) — **CC0 1.0**
по исходным страницам OpenGameArt, указанным в
[backend/ambience/LICENSES.md](backend/ambience/LICENSES.md).
Источники и контрольные суммы: `backend/ambience/sources.json`.
<https://creativecommons.org/publicdomain/zero/1.0/>.

## Библиотеки и инструменты

Основные зависимости: React/React DOM, Vite и его React-плагин, FastAPI,
httpx, Uvicorn, Pydantic, python-multipart и их транзитивные зависимости.
Для разработки/сборки используются Pillow, NumPy и PyInstaller.
Ссылки на их официальные проекты:

- <https://github.com/facebook/react>
- <https://github.com/vitejs/vite>
- <https://github.com/vitejs/vite-plugin-react>
- <https://github.com/fastapi/fastapi>
- <https://github.com/encode/httpx>
- <https://github.com/encode/uvicorn>
- <https://github.com/pydantic/pydantic>
- <https://github.com/Kludex/python-multipart>
- <https://github.com/python-pillow/Pillow>
- <https://github.com/numpy/numpy>
- <https://github.com/pyinstaller/pyinstaller>

При выпуске EXE или готового frontend-бандла распространяется уже код зависимостей.
В комплект выпуска нужно включить copyright notices и тексты лицензий фактически
вошедших версий, включая транзитивные зависимости, Python и данные TLS.
Эта обзорная таблица ссылок не заменяет такой комплект и не утверждает, что любая
будущая сборка автоматически выполнила все условия.
PyInstaller имеет специальное исключение для bootloader; его применение не делает
автоматически весь исходный код приложения GPL. ComfyUI скачивается только
необязательным Colab и не включён в распространяемую игру.

## Генерация графики

Anima: <https://huggingface.co/circlestone-labs/Anima>.
ComfyUI: <https://github.com/Comfy-Org/ComfyUI>.
rembg: <https://github.com/danielgatis/rembg>.
U-2-Net: <https://github.com/xuebinqin/U-2-Net>.

Веса и код этих проектов не распространяются с исходниками Story Engine.
Условия скачивания/использования инструментов применяются отдельно; готовые
изображения Anima разрешены для коммерческого использования по пункту 2.e
лицензии модели. Подробности и ограничения области прав — [LICENSING.md](LICENSING.md).


## Шрифты

`frontend/index.html` запрашивает Inter, Press Start 2P и Silkscreen из Google Fonts.
Файлы шрифтов не включены в исходники. Это отдельные сторонние материалы;
PolyForm и CC BY-NC проекта к ним не применяются. Лицензии и правообладатели:

- <https://github.com/google/fonts/blob/main/ofl/inter/OFL.txt>
- <https://github.com/google/fonts/blob/main/ofl/pressstart2p/OFL.txt>
- <https://github.com/google/fonts/blob/main/ofl/silkscreen/OFL.txt>

При самостоятельном размещении файлов шрифтов сохраните соответствующие OFL.txt
и соблюдайте условия каждого шрифта. Системные шрифты в игре не распространяются.

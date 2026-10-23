"""Обновить автономный Colab и справочник после редактирования локального пула.

python backend/sync_character_appearance.py [--check]
"""
from pathlib import Path
import argparse
import json

from character_appearance import APPEARANCE_POOL, APPEARANCE_LABELS, DIVERSITY_RECIPE

BEGIN = '# BEGIN SHARED CHARACTER APPEARANCE\n'
END = '# END SHARED CHARACTER APPEARANCE\n'


def sync(project: Path, *, check=False):
    module = (project / 'backend/character_appearance.py').read_text(encoding='utf-8')
    path = project / 'StoryEngine_Anima_Characters_Colab.ipynb'
    notebook = json.loads(path.read_text(encoding='utf-8'))
    catalog = next(cell for cell in notebook['cells']
                   if cell['cell_type'] == 'code' and 'def diffusion_graph(' in ''.join(cell['source']))
    current = ''.join(catalog['source'])
    start = current.index(BEGIN)
    end = current.index(END, start) + len(END)
    new = current[:start] + BEGIN + module + '\n' + END + current[end:]
    catalog['source'] = new.splitlines(keepends=True)
    content = json.dumps(notebook, ensure_ascii=False, indent=1) + '\n'
    lines = [
        '# Локальный пул внешности персонажей\n',
        'Источник: `backend/character_appearance.py`. В игре DeepSeek выбирает ID из пула, '
        'а русское описание собирается локально. Дополнительных запросов нет.\n',
        '| Признак | Варианты |', '|---|---|',
    ]
    for group, options in APPEARANCE_POOL.items():
        lines.append(f'| {APPEARANCE_LABELS[group]} (`{group}`) | ' + ', '.join(item[0] for item in options.values()) + ' |')
    lines += ['\n## Веса различий\n',
              'Это стартовая эвристика, а не измеренные оценки восприятия. '
              'Вес задаёт максимальный вклад признака в расстояние.\n',
              '| Признак | Вес |', '|---|---:|']
    labels = {**APPEARANCE_LABELS, 'age': 'Возрастная группа'}
    for group, weight in sorted(DIVERSITY_RECIPE['weights'].items(), key=lambda item: -item[1]):
        lines.append(f'| {labels[group]} | {weight:g} |')
    lines += ['\nПол и одежда фиксированы внутри профиля: их разнообразие обеспечивают '
              'квоты профилей. Внутри профиля эти два веса не меняют отбор. '
              'Для возраста соседние группы дают половину веса, крайние — полный. '
              'Для роста/кожи вклад пропорционален расстоянию между градациями; '
              'близкие цвета волос дают 20%, близкие стрижки — 25% веса. '
              'Цвет волос не учитывается, если один из образов без волос.\n']
    lines += [
        '\nВозраст — отдельное число: в игре 18+, в Colab сейчас 25–60. Это не случайная замена возраста сюжета.\n',
        'Одежда выбирается целым готовым костюмом. Рабочая одежда и простое платье универсальны; '
        'дорожный костюм и длинное одеяние подходят историческим/фэнтезийным мирам, '
        'комбинезон — научной фантастике. Остальные профили рассчитаны на современность.\n',
        'Colab по умолчанию создаёт 240 образов: 2 пола × 10 костюмов × 12. '
        '`INCLUDE_EXTRA_OUTFITS=False` оставляет 144 базовых образа. '
        'Это не полный перебор 12 групп. `COPIES_PER_PROFILE=4` даёт первые '
        '80 образов со всей одеждой или 48 с базовой; расширение до 12 '
        'сохраняет прежние задания.\n',
        'На профиль пол/костюм создаются 384 воспроизводимых кандидата. '
        'Каждый следующий выбирается по максимальному расстоянию до ближайшего '
        'уже выбранного в этом профиле (maximin). Основной вклад дают возрастная '
        'группа, телосложение и причёска; мелкие детали весят меньше. '
        '12 образов содержат по четыре возраста из групп 25–34, 35–49, 50–60. '
        'Веса и бюджет кандидатов находятся в DIVERSITY_RECIPE.\n',
        'Колаб создаёт обычные взрослые образы: особая примета не обязательна; '
        'для женщин в этой выборке растительность на лице не назначается, '
        'седые/белые волосы предлагаются с 45 лет. Это правила выборки рисунков, '
        'они не переписывают заданные особенности сюжетного каста. '
        'Для bald цвет волос — none и не описывается.\n',
        'Расстояние описаний — эвристика, а не измерение сходства готовых картинок. '
        'Текущий банк содержит 235 отобранных спрайтов с разметкой лиц. '
        'Назначение учитывает крупные группы, подробные признаки задания '
        'не объявлены проверенными на каждом рисунке.\n',
        'ID сохраняются в `appearance_features`; поле `appearance` остаётся читаемой строкой. '
        'Старые свободные описания в сохранениях/JSONL не меняются. '
        'Пропущенные или неверные ID нового ответа дополняются локально без повторного запроса.\n',
        'После изменения пула выполните `python backend/sync_character_appearance.py`: '
        'обновятся этот справочник и автономная копия в Colab. Проверка: '
        '`python backend/sync_character_appearance.py --check`.\n',
    ]
    targets = {path:content, project / 'CHARACTER_APPEARANCE_POOL.md':'\n'.join(lines)}
    for target, value in targets.items():
        if not target.exists() or target.read_text(encoding='utf-8') != value:
            if check:
                raise ValueError(f'Пул внешности не синхронизирован: {target.name}')
            target.write_text(value, encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    sync(Path(__file__).resolve().parents[1], check=args.check)
    print('Пул внешности, Colab и справочник синхронизированы.')

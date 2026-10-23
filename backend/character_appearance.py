"""Общий подробный пул внешности и ограниченная разнообразная выборка Colab.

Признаки описывают образ. Эвристика выбирает 12 образов на профиль, не перебирая
декартово произведение и не обещая измеренного визуального отличия рисунков.
"""
import hashlib
import random
from functools import lru_cache

APPEARANCE_POOL_VERSION = 4
APPEARANCE_POOL = {'gender': {'woman': ('женщина', 'woman'), 'man': ('мужчина', 'man')},
 'height': {'short': ('низкий', 'short stature'),
            'average': ('средний', 'average height'),
            'tall': ('высокий', 'tall stature')},
 'build': {'slender': ('стройное', 'slender build'),
           'average': ('обычное', 'average build'),
           'stocky': ('коренастое', 'stocky build'),
           'full': ('полное', 'full-bodied build'),
           'broad': ('широкоплечее', 'broad-shouldered build')},
 'skin': {'very_light': ('очень светлая', 'very light skin'),
          'light': ('светлая', 'light skin'),
          'tan': ('смуглая', 'tan skin'),
          'brown': ('коричневая', 'brown skin'),
          'dark_brown': ('тёмно-коричневая', 'dark brown skin')},
 'face': {'oval': ('овальное', 'oval face'),
          'round': ('круглое', 'round face'),
          'angular': ('угловатое', 'angular face'),
          'long': ('вытянутое', 'long face')},
 'eyes': {'brown': ('карие', 'brown eyes'),
          'hazel': ('ореховые', 'hazel eyes'),
          'gray': ('серые', 'gray eyes'),
          'blue': ('голубые', 'blue eyes'),
          'green': ('зелёные', 'green eyes')},
 'hair_color': {'black': ('чёрные', 'black hair'),
                'dark_brown': ('тёмно-каштановые', 'dark brown hair'),
                'chestnut': ('каштановые', 'chestnut hair'),
                'light_brown': ('русые', 'light brown hair'),
                'blond': ('светлые', 'blond hair'),
                'auburn': ('рыжие', 'auburn hair'),
                'gray': ('седые', 'gray hair'),
                'white': ('белые', 'white hair'),
                'none': ('не применим — без волос', '')},
 'hair': {'cropped': ('очень короткие', 'closely cropped haircut'),
          'short': ('короткие', 'short haircut'),
          'bob': ('каре', 'bob haircut'),
          'shoulder': ('до плеч', 'shoulder-length hairstyle'),
          'long': ('длинные', 'long hairstyle'),
          'ponytail': ('хвост', 'hair tied in a ponytail'),
          'bun': ('пучок', 'hair tied in a bun'),
          'bald': ('без волос', 'bald head')},
 'facial_hair': {'none': ('нет', 'clean-shaven face'),
                 'stubble': ('щетина', 'light stubble'),
                 'mustache': ('усы', 'a simple mustache'),
                 'beard': ('короткая борода', 'a short beard')},
 'detail': {'none': ('нет', ''),
            'freckles': ('веснушки', 'subtle freckles'),
            'mole': ('родинка', 'a small facial mole'),
            'scar': ('небольшой шрам', 'a small facial scar'),
            'glasses': ('простые очки', 'simple eyeglasses')},
 'outfit': {'casual': ('повседневная одежда',
                       'a plain long-sleeved shirt, straight trousers and simple shoes'),
            'coat': ('пальто', 'a modest knee-length coat over a shirt and trousers, closed boots'),
            'work': ('рабочая одежда', 'a woven work shirt, plain trousers, a canvas apron and closed boots'),
            'traveler': ('дорожный костюм',
                         'a layered tunic, a short cloth cape, trousers and leather boots'),
            'medic': ('медицинский халат',
                      'a plain medical coat over everyday clothes and closed shoes, no logos'),
            'formal': ('строгий костюм', 'a restrained jacket, plain shirt, tailored trousers and shoes'),
            'dress': ('простое платье', 'a plain modest long-sleeved dress and closed shoes'),
            'uniform': ('служебная форма', 'a simple fully closed service uniform and boots, no insignia'),
            'robe': ('длинное одеяние',
                     'a plain long cloth robe over an opaque tunic, closed shoes, no symbols'),
            'space': ('фантастический комбинезон',
                      'a simple fully closed science-fiction utility jumpsuit and boots, no helmet')},
 'palette': {'gray': ('серый', 'muted gray'),
             'brown': ('коричневый', 'muted brown'),
             'navy': ('тёмно-синий', 'muted navy blue'),
             'olive': ('оливковый', 'muted olive'),
             'burgundy': ('бордовый', 'muted burgundy'),
             'beige': ('бежевый', 'muted beige')}}
APPEARANCE_LABELS = {'gender': 'Пол',
 'height': 'Рост',
 'build': 'Телосложение',
 'skin': 'Оттенок кожи',
 'face': 'Форма лица',
 'eyes': 'Цвет глаз',
 'hair_color': 'Цвет волос',
 'hair': 'Причёска',
 'facial_hair': 'Растительность на лице',
 'detail': 'Особая примета',
 'outfit': 'Одежда',
 'palette': 'Основной цвет одежды'}
OUTFIT_WORLDS = {'casual': 'modern', 'coat': 'modern', 'work': 'universal', 'traveler': 'historical_fantasy', 'medic': 'modern', 'formal': 'modern', 'dress': 'universal', 'uniform': 'modern', 'robe': 'historical_fantasy', 'space': 'science_fiction'}
BASE_OUTFITS = ('casual', 'coat', 'work', 'traveler', 'medic', 'formal')

LOOKS_PER_PROFILE = 12
DIVERSITY_RECIPE = {
    'version': 2, 'candidates_per_profile': 384,
    'age_bands': ((25, 34), (35, 49), (50, 60)),
    # Максимальный вклад признака в расстояние. Силуэт важнее мелких деталей.
    # gender/outfit фиксированы внутри профиля и покрываются квотами профилей.
    'weights': {'gender': 4, 'age': 3, 'outfit': 5, 'build': 4, 'hair': 4,
                'height': 1.5, 'skin': 2, 'facial_hair': 2,
                'hair_color': 1.5, 'palette': 1, 'face': 0.75, 'detail': 0.5, 'eyes': 0.25},
}


def _number(seed):
    return int.from_bytes(hashlib.sha256(str(seed).encode()).digest()[:8], 'big')


def normalize_appearance(value, *, seed='', gender=None, outfit=None):
    """Дополняет новый ответ локально; старые сохранения сюда не передаются."""
    source = dict(value) if isinstance(value, dict) else {}
    if 'hair' not in source:
        source['hair'] = source.get('hairstyle')
    aliases = {'hair': {'tied': 'ponytail'}, 'skin': {'dark': 'dark_brown'},
               'hair_color': {'dark': 'black', 'light': 'blond'}}
    for group, mapping in aliases.items():
        key = source.get(group)
        if isinstance(key, str):
            source[group] = mapping.get(key, key)
    defaults = {'facial_hair': 'none', 'detail': 'none', 'outfit': 'work'}
    result = {}
    for group, options in APPEARANCE_POOL.items():
        selected = source.get(group)
        if not isinstance(selected, str) or selected not in options:
            keys = [key for key in options if not (group == 'hair_color' and key == 'none')]
            selected = defaults.get(group, keys[_number(f'{seed}:{group}') % len(keys)])
        result[group] = selected
    for group, forced in (('gender', gender), ('outfit', outfit)):
        if isinstance(forced, str) and forced in APPEARANCE_POOL[group]:
            result[group] = forced
    if result['hair'] == 'bald':
        result['hair_color'] = 'none'
    elif result['hair_color'] == 'none':
        colors = [key for key in APPEARANCE_POOL['hair_color'] if key != 'none']
        result['hair_color'] = colors[_number(f'{seed}:hair_color') % len(colors)]
    return result


def describe_appearance(features):
    parts = []
    for group, options in APPEARANCE_POOL.items():
        key = features.get(group)
        if (not isinstance(key, str) or key not in options or key == 'none'
                or group == 'hair_color' and features.get('hair') == 'bald'):
            continue
        text = options[key][0]
        parts.append(text if group == 'gender' else f'{APPEARANCE_LABELS[group].lower()}: {text}')
    return '; '.join(parts)


def appearance_image_prompt(features, age):
    gender = APPEARANCE_POOL['gender'][features['gender']][1]
    parts = [f'{age}-year-old adult {gender}']
    for group in ('height', 'build', 'skin', 'face', 'eyes', 'hair_color', 'hair', 'facial_hair', 'detail'):
        key = features[group]
        if key == 'none' or group == 'hair_color' and features['hair'] == 'bald':
            continue
        parts.append(APPEARANCE_POOL[group][key][1])
    clothes = APPEARANCE_POOL['outfit'][features['outfit']][1]
    palette = APPEARANCE_POOL['palette'][features['palette']][1]
    return ', '.join(parts) + f', wearing {clothes}. Clothing color palette: {palette}.'


def appearance_pool_prompt():
    return '\n'.join(f'{group}: ' + ', '.join(f'{key}={value[0]}' for key, value in options.items())
                     for group, options in APPEARANCE_POOL.items())


def _age_band(age):
    return 0 if age < 35 else 1 if age < 50 else 2


def appearance_distance(first, second):
    """Взвешенная разница описаний, не оценка реального сходства изображений.

    Вход — образ {'age': int, 'appearance_features': {...}}. Соседние оттенки,
    стрижки и возрастные группы различаются слабее разных силуэтов.
    """
    a, b = first['appearance_features'], second['appearance_features']
    weights = DIVERSITY_RECIPE['weights']
    distance = weights['age'] * abs(_age_band(first['age']) - _age_band(second['age'])) / 2
    hair_groups = {'cropped': 'short', 'short': 'short', 'bob': 'medium', 'shoulder': 'medium',
                   'long': 'long', 'ponytail': 'tied', 'bun': 'tied', 'bald': 'bald'}
    color_groups = {'black': 'dark', 'dark_brown': 'dark', 'chestnut': 'dark',
                    'light_brown': 'light', 'blond': 'light', 'auburn': 'red',
                    'gray': 'gray', 'white': 'gray'}
    for group in APPEARANCE_POOL:
        x, y = a.get(group), b.get(group)
        if x == y:
            continue
        difference = 1.0
        if group == 'hair_color':
            if a.get('hair') == 'bald' or b.get('hair') == 'bald':
                continue  # Отсутствие волос уже учтено в причёске.
            if color_groups.get(x) == color_groups.get(y):
                difference = 0.2
        elif group == 'hair' and hair_groups.get(x) == hair_groups.get(y):
            difference = 0.25
        elif group in ('height', 'skin'):
            order = list(APPEARANCE_POOL[group])
            if x in order and y in order:
                difference = abs(order.index(x) - order.index(y)) / (len(order) - 1)
        distance += weights[group] * difference
    return distance


@lru_cache(maxsize=128)
def _profile_looks(gender, outfit, seed):
    """Конечный пул кандидатов + maximin с квотой по взрослым возрастам.

    На каждом шаге берём кандидата, наиболее далёкого от ближайшего выбранного.
    Независимость профилей сохраняет готовые образы при расширении партии.
    """
    if gender not in APPEARANCE_POOL['gender'] or outfit not in APPEARANCE_POOL['outfit']:
        raise ValueError('Неизвестный профиль персонажа')
    rng = random.Random(_number(f'{seed}:{gender}:{outfit}:diversity'))
    candidates, seen = [], set()
    for number in range(DIVERSITY_RECIPE['candidates_per_profile']):
        band = number % len(DIVERSITY_RECIPE['age_bands'])
        age = rng.randint(*DIVERSITY_RECIPE['age_bands'][band])
        source = {group: rng.choice(list(options)) for group, options in APPEARANCE_POOL.items()}
        # Обычные цельные взрослые образы; примета не обязательна каждому.
        source['detail'] = rng.choice(['none'] * 8 + ['freckles', 'mole', 'scar', 'glasses'])
        source['facial_hair'] = (rng.choice(['none'] * 3 + ['stubble', 'mustache', 'beard'])
                                 if gender == 'man' else 'none')
        if age < 45:
            source['hair_color'] = rng.choice(['black', 'dark_brown', 'chestnut', 'light_brown', 'blond', 'auburn'])
        features = normalize_appearance(source, seed=f'{seed}:{number}', gender=gender, outfit=outfit)
        key = (band, tuple(features.items()))
        if key in seen:
            continue
        seen.add(key)
        candidates.append({'age': age, 'appearance_features': features})
    nearest = [float('inf')] * len(candidates)
    selected = []
    available = set(range(len(candidates)))
    for number in range(LOOKS_PER_PROFILE):
        band = number % len(DIVERSITY_RECIPE['age_bands'])
        eligible = [i for i in sorted(available) if _age_band(candidates[i]['age']) == band]
        # При равном балле сохраняем воспроизводимый порядок кандидатов.
        best = max(eligible, key=lambda i: nearest[i])
        chosen = candidates[best]
        selected.append((chosen['age'], tuple(chosen['appearance_features'].items())))
        available.remove(best)
        for i in available:
            nearest[i] = min(nearest[i], appearance_distance(candidates[i], chosen))
    return tuple(selected)


def colab_look(number, *, gender, outfit, seed=''):
    """Новая копия выбранного образа; номер/партия не меняют прошлый префикс."""
    if not isinstance(number, int) or isinstance(number, bool) or not 0 <= number < LOOKS_PER_PROFILE:
        raise ValueError(f'Номер образа должен быть от 0 до {LOOKS_PER_PROFILE - 1}')
    age, features = _profile_looks(gender, outfit, str(seed))[number]
    return {'age': age, 'appearance_features': dict(features)}


def colab_profiles(include_extra=False):
    outfits = APPEARANCE_POOL['outfit'] if include_extra else BASE_OUTFITS
    return [{'id': f'{gender}_{outfit}', 'gender': gender, 'outfit_id': outfit,
             'world': OUTFIT_WORLDS[outfit], 'outfit': APPEARANCE_POOL['outfit'][outfit][1]}
            for gender in APPEARANCE_POOL['gender'] for outfit in outfits]

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from background_effects import effect_profile_for, resolve_scene_effects, scene_effect_fields, effect_scene_key, KNOWN
from visual_retrieval import VisualCatalog
from import_backgrounds import import_pack
from prompts import build_segment_prose_prompt
from PIL import Image


REGION = [[.1,.5],[.9,.5],[.9,.9],[.1,.9]]


class EffectPolicyTests(unittest.TestCase):
    def result(self, asset_id, atmosphere=None, time="night", events=None, profile=None, genre="драма"):
        return resolve_scene_effects({"atmosphere": atmosphere or {}, "screen_events": events or []},
            {"asset_id": asset_id, "effect_profile": profile or {}}, time_of_day=time, genre=genre)

    def test_catalog_is_fully_classified_and_never_auto_enables(self):
        catalog = VisualCatalog()
        self.assertEqual({item['id'] for item in catalog.backgrounds} | {'abstract_space'}, KNOWN)
        for item in catalog.backgrounds:
            self.assertNotEqual(effect_profile_for(item)['setting'], 'unknown')
            result = resolve_scene_effects({}, item, time_of_day="night", genre="хоррор")
            self.assertIsNone(result['weather'])
            self.assertIsNone(result['ambient'])
            self.assertEqual(result['events'], [])

    def test_fireflies_need_every_condition_and_real_habitat(self):
        valid = {"ambient":"fireflies", "temperature":"warm", "season":"summer", "climate":"temperate"}
        profile = {"effect_regions": {"fireflies": REGION}}
        result = self.result('forest_path', valid, profile=profile)
        self.assertEqual(result['ambient']['type'], 'fireflies')
        self.assertIsNone(self.result('forest_path', valid)['ambient'])
        for key, value in [('temperature','cold'), ('temperature','unknown'), ('season','winter'),
                           ('season','unknown'), ('climate','alpine'), ('climate','unknown'), ('weather','rain')]:
            self.assertIsNone(self.result('forest_path', {**valid,key:value}, profile=profile)['ambient'], (key,value))
        for time in ['day','morning']:
            self.assertIsNone(self.result('forest_path', valid, time=time, profile=profile)['ambient'])
        for asset_id in ['desert','mountain_pass','sandy_beach','ship_cabin','sailing_ship_deck','metal_ship_deck',
                         'small_boat','bedroom','future_city','alien_planet','space_observation_deck','abstract_space','new_place']:
            self.assertIsNone(self.result(asset_id, valid, profile=profile)['ambient'], asset_id)

    def test_masks_are_required_in_sheltered_places_even_if_lighting_is_outdoor(self):
        for asset_id in ['bedroom','hangar','carriage_interior','tent_interior','bus_interior']:
            result = self.result(asset_id, {"weather":"rain"})
            self.assertIsNone(result['weather'])
            self.assertEqual(result['blocked'][0]['reason'], 'window_masks_required')
            result = self.result(asset_id, {"weather":"rain"}, profile={"weather_masks":[REGION]})
            self.assertEqual(result['weather']['mode'], 'masked')
            self.assertEqual(result['weather']['masks'], [REGION])
        self.assertEqual(self.result('sailing_ship_deck', {"weather":"rain"})['weather']['mode'], 'full')

    def test_no_earth_weather_in_sealed_space_aircraft_or_unclassified_planet(self):
        for asset_id in ['mine','metro_car','server_room','airplane_cabin','space_observation_deck','alien_planet','new_place','abstract_space']:
            result = self.result(asset_id, {"weather":"storm"}, profile={"weather_masks":[REGION]})
            self.assertIsNone(result['weather'], asset_id)

    def test_snow_requires_compatible_artwork_cold_climate_and_season(self):
        cold = {"weather":"snow", "temperature":"cold", "climate":"alpine", "season":"winter"}
        self.assertEqual(self.result('mountain_pass', cold)['weather']['type'], 'snow')
        self.assertIsNotNone(self.result('mountain_pass', {**cold,'season':'summer'})['weather'])
        for asset_id in ['desert','forest_path','meadow','park','swamp','sandy_beach']:
            self.assertIsNone(self.result(asset_id, cold)['weather'], asset_id)
        for changes in [{'temperature':'warm'}, {'climate':'tropical'}, {'climate':'unknown'},
                        {'season':'unknown'}, {'climate':'temperate','season':'summer'}]:
            self.assertIsNone(self.result('rooftop', {**cold,**changes})['weather'], changes)

    def test_ash_requires_current_source_and_open_air(self):
        self.assertIsNone(self.result('town_square', {'ambient':'ash'})['ambient'])
        valid = {'ambient':'ash','ambient_source':'wildfire'}
        self.assertEqual(self.result('town_square', valid)['ambient']['type'], 'ash')
        for source in ['none','fire','disturbed_dust']:
            self.assertIsNone(self.result('town_square', {**valid,'ambient_source':source})['ambient'])
        for asset_id in ['bedroom','forge','space_observation_deck','alien_planet','abstract_space']:
            self.assertIsNone(self.result(asset_id, valid)['ambient'])
        for weather in ['rain','storm','snow']:
            self.assertIsNone(self.result('town_square', {**valid,'weather':weather})['ambient'])

    def test_fog_requires_humidity_and_compatible_background(self):
        self.assertIsNone(self.result('swamp', {'weather':'fog'})['weather'])
        self.assertIsNotNone(self.result('swamp', {'weather':'fog','humidity':'humid'})['weather'])
        self.assertIsNone(self.result('desert', {'weather':'fog','humidity':'humid'})['weather'])

    def test_local_particles_require_site_source_and_reviewed_region(self):
        for asset_id,effect,source in [('forge','embers','fire'), ('workshop','sparks','metalworking'),
                                       ('server_room','sparks','electrical_fault'), ('library_archive','dust_motes','disturbed_dust')]:
            atmosphere = {'ambient':effect,'ambient_source':source}
            self.assertIsNone(self.result(asset_id, atmosphere)['ambient'])
            profile = {'effect_regions':{effect:REGION}}
            self.assertEqual(self.result(asset_id, atmosphere, profile=profile)['ambient']['type'], effect)
            self.assertIsNone(self.result(asset_id, {'ambient':effect}, profile=profile)['ambient'])
            self.assertIsNone(self.result('bedroom', atmosphere, profile=profile)['ambient'])

    def test_flash_requires_explicit_source_and_lightning_follows_weather_masks(self):
        self.assertEqual(self.result('mine', events=[{'type':'flash'}])['events'], [])
        self.assertEqual(self.result('mine', events=[{'type':'flash','source':'lightning'}])['events'], [])
        event = [{'type':'flash','source':'lightning','intensity':99}]
        result = self.result('bedroom', {'weather':'storm'}, events=event, profile={'weather_masks':[REGION]})
        self.assertEqual(result['events'][0]['mode'], 'masked')
        self.assertEqual(result['events'][0]['intensity'], .55)
        self.assertEqual(result['events'][0]['masks'], [REGION])
        self.assertEqual(self.result('abstract_space', events=[{'type':'flash','source':'explosion'}])['events'], [])
        self.assertEqual(self.result('bedroom', events=[{'type':'flash','source':'magic'}])['events'], [])
        self.assertEqual(len(self.result('mage_study', events=[{'type':'flash','source':'magic'}], genre='фэнтези')['events']), 1)
        result = self.result('mine', events=[{'type':'flash','source':'explosion'}] * 4)
        self.assertEqual(len(result['events']), 1)
        self.assertEqual(result['blocked'][0]['reason'], 'one_flash_per_scene')

    def test_malformed_metadata_is_fail_closed_and_intensity_is_bounded(self):
        result = self.result('forest_path', {'weather':[], 'ambient':{}, 'temperature':{},'season':[], 'climate':[]},
                             events=[None, {'type':{}, 'source':[]}], profile={'weather_masks':['bad'], 'effect_regions':{'fireflies':[[0,0],[0,0],[0,0]]}})
        self.assertIsNone(result['weather'])
        self.assertIsNone(result['ambient'])
        self.assertEqual(result['events'], [])
        self.assertEqual(self.result('town_square', {'weather':'rain','intensity':float('nan')})['weather']['intensity'], .5)
        self.assertEqual(self.result('town_square', {'weather':'rain','intensity':9})['weather']['intensity'], 1)

    def test_art_change_invalidates_masks_and_regions(self):
        profile = {'artwork_sha256':'old', 'weather_masks':[REGION], 'effect_regions':{'fireflies':REGION}}
        same = effect_profile_for({'id':'forest_path','sha256':'old','effect_profile':profile})
        self.assertEqual(same['effect_regions']['fireflies'], REGION)
        changed = effect_profile_for({'id':'forest_path','sha256':'new','effect_profile':profile})
        self.assertEqual(changed['effect_regions'], {})
        self.assertEqual(changed['weather_masks'], [])
        self.assertEqual(changed['artwork_sha256'], 'new')

    def test_import_keeps_reviewed_regions_only_for_same_art(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'originals').mkdir()
            image = root/'originals/bedroom__day.png'
            Image.new('RGB',(1280,720),'green').save(image)
            location = {'id':'bedroom','lighting':'windowed'}
            (root/'locations.json').write_text(json.dumps([location]), encoding='utf-8')
            import_pack(root,root/'game')
            catalog_file = root/'game/visual_catalogs/background_catalog.json'
            entry = json.loads(catalog_file.read_text(encoding='utf-8'))['places']['bedroom']
            location['effect_profile'] = {'artwork_sha256':entry['sha256'], 'weather_masks':[REGION]}
            (root/'locations.json').write_text(json.dumps([location]), encoding='utf-8')
            import_pack(root,root/'game')
            self.assertEqual(json.loads(catalog_file.read_text(encoding='utf-8'))['places']['bedroom']['effect_profile']['weather_masks'], [REGION])
            Image.new('RGB',(1280,720),'red').save(image)
            import_pack(root,root/'game')
            self.assertEqual(json.loads(catalog_file.read_text(encoding='utf-8'))['places']['bedroom']['effect_profile']['weather_masks'], [])

    def test_metadata_survives_scene_shaping_and_prompts_request_it(self):
        scene = {'time_of_day':'night','atmosphere':{'ambient':'fireflies'},'screen_events':[], 'narration':'text'}
        fields = scene_effect_fields(scene)
        self.assertNotIn('narration', fields)
        self.assertEqual(fields['atmosphere'],scene['atmosphere'])
        for prompt in [build_segment_prose_prompt(['event'],None)]:
            self.assertIn('"atmosphere"', prompt)
            self.assertIn('"screen_events"', prompt)
        self.assertEqual(effect_scene_key('s','node',scene), effect_scene_key('s','node',{}))
        self.assertNotEqual(effect_scene_key('s','node',scene), effect_scene_key('s','other',scene))

    def test_mentions_and_horror_do_not_request_any_effect(self):
        result = resolve_scene_effects({'narration':'Он вспомнил снег, пепел пожара и светлячков.',
            'dialogue':'Завтра будет гроза.'}, {'asset_id':'forest_path'}, time_of_day='night',genre='хоррор')
        self.assertIsNone(result['weather'])
        self.assertIsNone(result['ambient'])
        self.assertEqual(result['events'], [])

    def test_grading_uses_approved_conditions_and_location(self):
        for asset_id in ['mine', 'metro_car', 'server_room', 'airplane_cabin', 'space_observation_deck',
                         'alien_planet', 'abstract_space', 'new_place']:
            result = self.result(asset_id, {'weather':'storm','temperature':'cold','season':'winter','climate':'cold'})
            self.assertIsNone(result['grading'], asset_id)
        self.assertIsNone(self.result('town_square')['grading'])
        self.assertIsNone(self.result('desert', {'weather':'fog','humidity':'humid'})['grading'])
        self.assertIsNone(self.result('forest_path', {'weather':'snow','temperature':'cold','season':'winter','climate':'cold'})['grading'])
        self.assertIsNone(self.result('town_square', {'ambient':'ash'})['grading'])
        self.assertEqual(self.result('town_square', {'ambient':'ash','ambient_source':'wildfire'})['grading']['ambient']['type'], 'ash')
        rain = self.result('town_square', {'weather':'rain','intensity':.8})
        self.assertEqual(rain['grading']['weather'], {'type':'rain','intensity':.8})
        self.assertEqual(rain['grading']['strength'], 1)
        window = self.result('bedroom', {'weather':'rain'})
        self.assertIsNone(window['weather'])
        self.assertEqual(window['grading']['strength'], .22)
        self.assertEqual(window['grading']['weather']['type'], 'rain')
        self.assertIsNone(self.result('bedroom', {'weather':'snow','temperature':'warm'})['grading'])
        fixed = resolve_scene_effects({'atmosphere':{'weather':'storm'}},
            {'asset_id':'bedroom','time_mode':'fixed'})
        self.assertIsNone(fixed['grading'])

    def test_winter_grade_needs_cold_compatible_art_and_climate(self):
        valid = {'season':'winter','temperature':'cold','climate':'temperate'}
        self.assertEqual(self.result('town_square', valid)['grading']['season'], 'winter')
        for changes in [{'season':'summer'}, {'temperature':'warm'}, {'climate':'tropical'}, {'climate':'unknown'}]:
            self.assertIsNone(self.result('town_square', {**valid,**changes})['grading'])
        for asset_id in ['forest_path','meadow','desert','mine']:
            self.assertIsNone(self.result(asset_id, valid)['grading'])


class EffectIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory()
        with patch('pathlib.Path.home', return_value=Path(cls.home.name)):
            import server
        cls.server = server

    @classmethod
    def tearDownClass(cls):
        cls.home.cleanup()

    def test_scene_output_filters_effects_using_selected_background(self):
        state = SimpleNamespace(genre='драма',tone='лёгкий',facts=[],recent_lines=[],tree_current_id='node')
        valid = {'ambient':'fireflies','temperature':'warm','season':'summer','climate':'temperate'}
        with patch.object(self.server,'get_visual_catalog',return_value=VisualCatalog()):
            scene = {'location':'Спальня','location_background_id':'bedroom','time_of_day':'night','atmosphere':valid}
            result = self.server._scene_with_visuals(state,scene)
            self.assertIsNone(result['visual']['effects']['ambient'])
            self.assertEqual(result['visual']['effects']['blocked'][0]['reason'], 'fireflies_habitat_required')
            self.assertNotIn('visual',scene)
            result = self.server._scene_with_visuals(state,{'location':'Лесная тропа','location_background_id':'forest_path','time_of_day':'night','atmosphere':valid})
            self.assertEqual(result['visual']['effects']['ambient']['type'], 'fireflies')
            self.assertTrue(result['visual']['effects']['scene_key'])

    def test_explicit_day_prevents_fireflies_despite_night_in_dialogue(self):
        state = SimpleNamespace(genre='драма',tone='лёгкий',facts=[],recent_lines=[])
        scene = {'location':'Лесная тропа','location_background_id':'forest_path','time_of_day':'day','dialogue':'Вчера ночью здесь было красиво.',
                 'atmosphere':{'ambient':'fireflies','temperature':'warm','season':'summer','climate':'temperate'}}
        with patch.object(self.server,'get_visual_catalog',return_value=VisualCatalog()):
            result = self.server._scene_with_visuals(state,scene)
        self.assertEqual(result['visual']['backgrounds'][0]['time_of_day'], 'day')
        self.assertIsNone(result['visual']['effects']['ambient'])
        self.assertEqual(result['visual']['effects']['blocked'][0]['reason'], 'fireflies_require_evening_or_night')


if __name__ == '__main__':
    unittest.main()

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from background_effects import scene_effect_context, scene_effect_fields, resolve_scene_effects
from deepseek_client import DeepSeekClient
from state_machine import StoryState
from prompts import build_segment_prose_prompt


def rain_scene():
    return {
        "location": "Городская площадь", "narration": "Капли застучали по мостовой.",
        "time_of_day": "evening",
        "atmosphere": {"weather": "rain", "intensity": .4, "ambient": "none",
                       "temperature": "warm", "season": "summer", "climate": "temperate",
                       "humidity": "humid", "ambient_source": "none"},
        "screen_events": [{"type": "flash", "source": "camera", "intensity": .2}],
    }


class EffectChoiceTests(unittest.TestCase):
    def state(self):
        return StoryState(genre="драма", tone="лёгкий", target_length=20)

    def test_generation_keeps_model_choice_with_one_existing_call(self):
        state = self.state()
        scene = rain_scene()
        client = DeepSeekClient(api_key="test-key")
        with patch.object(client, "_call", return_value=json.dumps({"scenes": [scene]})) as call:
            scenes, _ = client.generate_segment_prose(state, ["Дождь на площади"], None)
        call.assert_called_once()
        self.assertEqual(scenes[0]["atmosphere"], scene["atmosphere"])
        self.assertEqual(scene_effect_fields(scenes[0])["screen_events"], scene["screen_events"])
        result = resolve_scene_effects(scenes[0], {"asset_id": "town_square"}, time_of_day="evening")
        self.assertEqual(result["weather"]["type"], "rain")
        self.assertEqual(result["weather"]["intensity"], .4)


    def test_conditions_survive_snapshot_and_next_generation_without_replaying_flash(self):
        state = self.state()
        raw = rain_scene()
        DeepSeekClient.apply_scene_to_state(state, raw)
        restored = StoryState.from_dict(json.loads(json.dumps(state.to_dict())))
        self.assertEqual(restored.scene_conditions, state.scene_conditions)
        self.assertNotIn("screen_events", restored.scene_conditions)
        self.assertIn('"weather":"rain"', restored.to_prompt_block())
        raw["atmosphere"]["weather"] = "clear"
        self.assertEqual(state.scene_conditions["atmosphere"]["weather"], "rain")
        # Independent branch snapshots keep their own model choices.
        branch = copy.deepcopy(restored)
        DeepSeekClient.apply_scene_to_state(branch, {**rain_scene(), "atmosphere": {"weather": "clear", "ambient": "none"}})
        self.assertEqual(restored.scene_conditions["atmosphere"]["weather"], "rain")
        self.assertEqual(branch.scene_conditions["atmosphere"]["weather"], "clear")

    def test_old_saves_start_empty_and_missing_metadata_preserves_known_weather(self):
        state = self.state()
        data = state.to_dict()
        del data["scene_conditions"]
        restored = StoryState.from_dict(data)
        self.assertEqual(restored.scene_conditions, {})
        DeepSeekClient.apply_scene_to_state(restored, rain_scene())
        DeepSeekClient.apply_scene_to_state(restored, {"location": "Шахта", "narration": "Стало темно."})
        self.assertEqual(restored.scene_conditions['atmosphere']['weather'], 'rain')
        self.assertEqual(restored.scene_conditions['time_of_day'], 'evening')
        self.assertNotIn('screen_events', restored.scene_conditions)
        self.assertNotIn("ТЕКУЩИЕ УСЛОВИЯ", restored.to_prompt_block())
        self.assertEqual(restored.scene_conditions["atmosphere"]["weather"], "rain")
        for _ in range(4):
            result = resolve_scene_effects({}, {"asset_id": "forest_path"}, time_of_day="night")
            self.assertIsNone(result["weather"])
            self.assertIsNone(result["ambient"])
            self.assertEqual(result["events"], [])

    def test_model_choice_cannot_bypass_background_rules_or_get_a_replacement(self):
        scene = rain_scene()
        scene["screen_events"] = []
        for asset in ("mine", "bedroom", "spaceship_bridge", "abstract_space", "new_place"):
            result = resolve_scene_effects(scene, {"asset_id": asset})
            self.assertIsNone(result["weather"], asset)
            self.assertIsNone(result["ambient"], asset)
            self.assertTrue(result["blocked"], asset)
        explicit_none = {"atmosphere": {"weather": "clear", "ambient": "none"}, "screen_events": []}
        self.assertEqual(resolve_scene_effects(explicit_none, {"asset_id": "forest_path"}),
                         {"weather": None, "ambient": None, "events": [], "blocked": [], "grading": None})

    def test_context_is_bounded_and_untrusted_metadata_cannot_pollute_next_prompt(self):
        scene = {"time_of_day": [], "atmosphere": {
            "weather": ["rain"], "ambient": {"type": "ash"}, "season": "summer",
            "climate": "unexpected", "intensity": float("nan"), "ambient_intensity": 20,
            "extra": "PRIVATE_UNBOUNDED_TEXT", "ambient_source": "invented_source"},
            "screen_events": [{"type": "flash", "source": "explosion"}]}
        self.assertEqual(scene_effect_context(scene), {"atmosphere": {"season": "summer", "ambient_intensity": 1}})
        for invalid in (None, [], "bad"):
            self.assertEqual(scene_effect_context(invalid), {})

    def test_prose_assigns_free_choice_only_to_deepseek(self):
        for prompt in (
            build_segment_prose_prompt(["event"], None),
        ):
            self.assertIn("не делает собственного случайного выбора", prompt)
            self.assertIn("включая отсутствие эффектов", prompt)
            self.assertIn("из исходных условий и последовательных изменений в журнале событий", prompt)
            self.assertIn("ambient_intensity", prompt)
            self.assertIn("Отрицания", prompt)


if __name__ == "__main__":
    unittest.main()

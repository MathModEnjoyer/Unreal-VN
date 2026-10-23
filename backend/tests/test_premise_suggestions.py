import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deepseek_client import DeepSeekClient
from fastapi import HTTPException
import run
import server


class PremiseSuggestionsTests(unittest.TestCase):
    def test_empty_model_response_is_not_success(self):
        client = DeepSeekClient(api_key='offline-test-key')
        for value in ({}, {'premises': []}, {'premises': [{'text': ' '}, {'text': 12}]}):
            with self.subTest(value=value), patch.object(client, '_call', return_value=json.dumps(value)):
                with self.assertRaises(KeyError):
                    client.generate_premise_suggestions()

    def test_valid_model_response(self):
        client = DeepSeekClient(api_key='offline-test-key')
        with patch.object(client, '_call', return_value=json.dumps({'premises': [{'genre':'детектив', 'text':' Письмо '}]})):
            self.assertEqual(client.generate_premise_suggestions(), [{'genre':'детектив', 'text':'Письмо'}])

    def test_missing_network_files_have_readable_error(self):
        with patch.object(server, '_client'), patch.object(server, '_llm_call', side_effect=FileNotFoundError('missing TLS data')):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(server.premise_suggestions())
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(caught.exception.detail['reason'], 'network_runtime_error')

    def test_runtime_check_constructs_real_https_stack_without_requests(self):
        with patch.object(sys, 'argv', ['StoryEngine', '--check-runtime']), patch('httpx.Client.send', side_effect=AssertionError('No network allowed')):
            run.main()

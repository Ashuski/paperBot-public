"""Offline regression checks for the original implementation, using synthetic data."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('paperbot_under_test', ROOT / 'paper_trading_bot.py')
bot = importlib.util.module_from_spec(spec)
# Import defines functions only; __main__ is never executed. Avoid loading user .env files.
with patch('dotenv.load_dotenv'), patch.dict(os.environ, {'PORT': '8080', 'BACKUP_HOURS': '6'}):
    spec.loader.exec_module(bot)


class ParserTests(unittest.TestCase):
    def test_signal_fields(self):
        # A public program address stands in for a synthetic signal address.
        ca = str(bot.PUMP_PROGRAM_ID)
        text = f'[Example (TEST)]\n`{ca}`\nBonding Curve 25%\n`Cap:` **12.5K'
        self.assertTrue(bot.is_call_card(text))
        self.assertEqual(bot.get_ca(text), ca)
        self.assertEqual(bot.get_ticker(text), 'TEST')
        self.assertEqual(bot.get_mcap(text), 12500)
        self.assertEqual(bot.get_bonding(text), 25)

    def test_unrelated_message_is_ignored(self):
        self.assertFalse(bot.is_call_card('Hello, this is unrelated.'))
        self.assertIsNone(bot.get_ca('Hello'))

    def test_bonded_milestone(self):
        event = bot.parse_update_msg('$TEST has been bonded')
        self.assertTrue(event['bonded'])
        self.assertEqual(event['ticker'], 'TEST')


class CurveTests(unittest.TestCase):
    def test_reserves_use_sol_and_token_decimals(self):
        # Two tokens and one SOL imply 0.5 SOL/token, not a raw reserve ratio.
        raw = bytes(8) + struct.pack('<QQQQQ?', 2_000_000, 1_000_000_000, 0, 0, 2_000_000, True)
        result = bot.parse_bonding_curve_data(raw)
        self.assertEqual(result['price_sol'], 0.5)
        self.assertTrue(result['complete'])
        self.assertEqual(result['bonding_pct'], 100)

    def test_short_and_zero_reserve_accounts_are_rejected(self):
        self.assertIsNone(bot.parse_bonding_curve_data(bytes(48)))
        self.assertIsNone(bot.parse_bonding_curve_data(bytes(49)))


class StrategyTests(unittest.TestCase):
    def test_take_profit_does_not_reopen(self):
        trade = {}
        bot.init_shadows(trade)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bot.update_shadows(trade, 1.6, now)
        shadow = trade['shadows']['50/15']
        self.assertEqual(shadow['status'], 'TP_HIT')
        self.assertEqual(shadow['pnl'], 0.5)
        bot.update_shadows(trade, 0.5, now)
        self.assertEqual(shadow['status'], 'TP_HIT')

    def test_stop_loss(self):
        trade = {}
        bot.init_shadows(trade)
        bot.update_shadows(trade, 0.8, datetime.now(timezone.utc))
        self.assertEqual(trade['shadows']['50/15']['status'], 'SL_HIT')
        self.assertEqual(trade['shadows']['50/15']['pnl'], -0.15)


class PersistenceTests(unittest.TestCase):
    def test_save_and_load_in_isolated_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'synthetic.json'
            with patch.object(bot, 'LOG_FILE', str(target)):
                data = {'trades': [{'id': 1, 'ticker': 'TEST'}], 'summary': {}}
                bot.save_log(data)
                self.assertEqual(bot.load_log(), data)
                self.assertFalse(Path(str(target) + '.tmp').exists())

    def test_corrupt_log_preserves_original(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'synthetic.json'
            target.write_text('not JSON', encoding='utf-8')
            with patch.object(bot, 'LOG_FILE', str(target)):
                self.assertEqual(bot.load_log(), {'trades': [], 'summary': {}})
                self.assertEqual(Path(str(target) + '.corrupted').read_text(), 'not JSON')


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Capture the real application without binding a socket or starting a service.
        self.app = None

        def runner_factory(app):
            self.app = app
            return Mock(setup=AsyncMock())

        with patch('builtins.print'), \
                patch.object(web, 'AppRunner', side_effect=runner_factory), \
                patch.object(web, 'TCPSite', return_value=Mock(start=AsyncMock())):
            await bot.start_dashboard()
        self.middleware = self.app.middlewares[0]

    async def test_missing_password_fails_closed(self):
        with patch.dict(os.environ, {'DASHBOARD_PASSWORD': ''}):
            with self.assertRaises(web.HTTPUnauthorized):
                await self.middleware(make_mocked_request('GET', '/'), AsyncMock())

    async def test_wrong_password_is_rejected(self):
        with patch.dict(os.environ, {'DASHBOARD_PASSWORD': 'synthetic-test-value'}):
            request = make_mocked_request('GET', '/', headers={'Authorization': 'Basic invalid'})
            with self.assertRaises(web.HTTPUnauthorized):
                await self.middleware(request, AsyncMock())

    async def test_valid_viewer_credentials_reach_handler(self):
        with patch.dict(os.environ, {'DASHBOARD_PASSWORD': 'synthetic-test-value'}):
            auth = 'Basic ' + base64.b64encode(b'viewer:synthetic-test-value').decode()
            request = make_mocked_request('GET', '/', headers={'Authorization': auth})
            handler = AsyncMock(return_value=web.Response(text='ok'))
            self.assertEqual((await self.middleware(request, handler)).text, 'ok')
            handler.assert_awaited_once_with(request)

    async def test_raw_data_route_is_not_registered(self):
        paths = {route.resource.canonical for route in self.app.router.routes()}
        self.assertIn('/live', paths)
        self.assertNotIn('/json', paths)


if __name__ == '__main__':
    unittest.main()

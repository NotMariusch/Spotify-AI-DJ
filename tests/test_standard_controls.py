import contextlib
import copy
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, mock_open, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import spotify_dj as dj
import dj_server as server


class StandardControlsTests(unittest.TestCase):
    def test_dashboard_routes_standard_and_plain_wishes_only(self):
        old = copy.deepcopy(server.state)
        self.addCleanup(lambda: server.state.update(old))
        with patch.object(server, 'send_command') as send, \
                patch.object(server, 'shutdown_requested', Mock(is_set=lambda: False)):
            for retired in ('1', '2', '3', '4', 'ai-mode'):
                server.handle_command({'cmd': retired})
            send.assert_not_called()
            server.handle_command({'cmd': '5'})
            send.assert_called_once_with('5')
            self.assertEqual(server.state['mode'], 'Standard')
            server.handle_command({'cmd': 'Spiel deutschen Trap'})
            send.assert_called_with('ai:Spiel deutschen Trap')

    def test_retired_file_commands_do_not_change_active_request_or_start_music(self):
        for command in ('1', '2', '3', '4'):
            with self.subTest(command=command), contextlib.ExitStack() as stack:
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                stack.enter_context(patch.object(dj, 'auto_mode', 'ai'))
                stack.enter_context(patch.object(dj.os.path, 'exists', return_value=True))
                stack.enter_context(patch('builtins.open', mock_open(read_data=command)))
                stack.enter_context(patch.object(dj.os, 'remove'))
                # Stop before any regular loop polling; isolate command dispatch.
                stack.enter_context(patch.object(dj, 'refresh_rate_limit_status', side_effect=SystemExit))
                play = stack.enter_context(patch.object(dj, 'play_from_pool'))
                standard = stack.enter_context(patch.object(dj, 'play_global_mix'))
                reset = stack.enter_context(patch.object(dj.personal_director, 'reset'))
                with self.assertRaises(SystemExit):
                    dj.run_dj()
                self.assertEqual(dj.auto_mode, 'ai')
                play.assert_not_called()
                standard.assert_not_called()
                reset.assert_not_called()

    def test_ban_any_artist_writes_only_explicit_preference(self):
        catalog = {'Ado': {'tracks': [{'name': 'Retain me'}]}}
        legacy = {'Ado': {'id': 'id', 'trial_plays': 3}}
        original = copy.deepcopy((catalog, legacy))
        with patch.object(dj, 'now_playing', {'artist': 'Ado'}), \
                patch.object(dj, 'banned_artist_names', set()), \
                patch.object(dj, 'track_disk_cache', catalog), \
                patch.object(dj, 'discovered_artists', legacy), \
                patch.object(dj, 'ai_pool', []), patch.object(dj, 'auto_mode', 'global'), \
                patch.object(dj, 'play_global_mix') as play, \
                patch.object(dj, 'write_json_atomic') as write, contextlib.redirect_stdout(io.StringIO()):
            dj.ban_current_artist()
            write.assert_called_once_with(dj.BANNED_ARTISTS_FILE, {'artist_names': ['Ado']})
            play.assert_called_once_with(interrupted=False, banned=True)
            self.assertEqual((catalog, legacy), original)
            self.assertFalse(dj.personal_track_allowed({'id': 'song', 'artists': [{'name': 'Ado'}]}))

    def test_startup_has_no_legacy_memory_reads_or_writes(self):
        with contextlib.ExitStack() as stack:
            for name in ('acquire_lock', 'load_track_cache', 'load_banned', 'load_banned_artists',
                         'load_discovered', 'connect', 'status'):
                stack.enter_context(patch.object(dj, name))
            stack.enter_context(patch('atexit.register'))
            stack.enter_context(patch.object(dj, 'sp', None))
            stack.enter_context(patch.object(dj, 'discovered_artists', {}))
            stack.enter_context(patch.object(dj, 'run_dj', side_effect=SystemExit))
            write = stack.enter_context(patch.object(dj, 'write_json_atomic'))
            read = stack.enter_context(patch('builtins.open'))
            with self.assertRaises(SystemExit):
                dj.main()
            write.assert_not_called()
            read.assert_not_called()
        self.assertFalse(hasattr(dj, 'update_weight'))
        self.assertFalse(hasattr(dj, 'load_memory'))

    def test_hotkeys_and_dashboard_have_only_standard_mode_button(self):
        ahk = (ROOT / 'scripts/dj_hotkey.ahk').read_text(encoding='utf-8')
        html = (ROOT / 'src/dj_dashboard.html').read_text(encoding='utf-8')
        for number in range(1, 5):
            self.assertNotIn(f'id="btn-{number}"', html)
        for number in range(6, 10):
            self.assertNotIn(f'F13 & {number}::', ahk)
        for number, command in [(1, '5'), (2, '"skip"'), (3, '"ban-artist"'), (4, '"ban"'), (5, '"quit"')]:
            self.assertIn(f'F13 & {number}::SendDJ({command})', ahk)
        for number, label in [(1, 'Standard'), (2, 'Skip'), (3, 'Künstler sperren'), (4, 'Song sperren'), (5, 'DJ beenden')]:
            self.assertIn(f'F13 + {number}</kbd> = {label}', html)
        self.assertIn('>Standard</button>', html)
        self.assertNotIn('id="btn-ai"', html)
        self.assertIn('id="ai-input"', html)

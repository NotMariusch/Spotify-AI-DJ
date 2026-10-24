import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import dj_voice as voice


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.folder = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='.voice-test-', dir=ROOT))
        self.stack.enter_context(patch.object(voice.tempfile, 'tempdir', self.folder))
        self.log = self.stack.enter_context(patch.object(voice, 'log_to_server'))
        self.real_send = voice.send_command
        self.send = self.stack.enter_context(patch.object(voice, 'send_command'))
        self.device = self.stack.enter_context(patch.object(voice.sd, 'InputStream'))
        self.stack.enter_context(patch.object(voice, 'recording', False))
        self.stack.enter_context(patch.object(voice, 'processing', False))
        self.stack.enter_context(patch.object(voice, 'stream', None))
        self.stack.enter_context(patch.object(voice, 'frames', []))
        self.stack.enter_context(patch.object(voice, 'closing', threading.Event()))
        self.stack.enter_context(patch.object(voice, 'shutdown_notice', None))

    def test_microphone_start_failure_resets_state_and_can_retry(self):
        self.device.return_value.start.side_effect = RuntimeError('unavailable')
        voice.on_hotkey()
        self.assertFalse(voice.recording)
        self.assertIsNone(voice.stream)
        self.device.return_value.close.assert_called_once()
        self.assertFalse(self.log.call_args.kwargs['recording_state'])
        self.device.return_value.start.side_effect = None
        voice.on_hotkey()
        self.assertTrue(voice.recording)

    def test_shutdown_notice_stops_voice_and_prevents_new_recordings(self):
        notice = Mock()
        notice.changed.return_value = True
        voice.shutdown_notice = notice
        voice.on_hotkey()
        self.assertTrue(voice.closing.is_set())
        self.device.assert_not_called()

    def test_voice_loop_removes_hotkey_when_shutdown_is_requested(self):
        if voice.IS_LINUX:
            self.skipTest('Windows hotkey client')
        with patch.object(voice.keyboard, 'add_hotkey', return_value='fixture-hook'), \
                patch.object(voice.keyboard, 'remove_hotkey') as remove, \
                patch.object(voice, 'stopping', return_value=True):
            voice.run_voice()
        remove.assert_called_once_with('fixture-hook')

    def test_late_transcription_cannot_overwrite_quit_command(self):
        path = Path(self.folder) / 'dj_input.txt'
        path.write_text('quit', encoding='utf-8')
        voice.shutdown_notice = Mock()
        voice.shutdown_notice.changed.return_value = True
        with patch.object(voice, 'INPUT_FILE', path):
            self.real_send('ai:late result')
        self.assertEqual(path.read_text(), 'quit')

    def test_recording_to_ai_command_without_microphone_or_recognition_network(self):
        voice.on_hotkey()
        callback = self.device.call_args.kwargs['callback']
        callback(voice.np.zeros((160, 1), dtype='int16'), 160, None, None)
        self.assertEqual(len(voice.frames), 1)
        with patch.object(voice.threading, 'Thread') as worker:
            voice.on_hotkey()
            self.assertTrue(voice.processing)
            self.assertFalse(voice.recording)
            voice.on_hotkey()
            worker.assert_called_once()
        with patch.object(voice, 'transcribe', return_value='Play Ado and Eve'):
            voice.finish_recording()
        self.send.assert_called_once_with('ai:Play Ado and Eve')
        self.assertFalse(voice.processing)
        self.assertIsNone(voice.stream)
        self.assertEqual(list(Path(self.folder).glob('*.wav')), [])

    def test_unrecognized_audio_sends_no_command_and_removes_temp_audio(self):
        voice.frames = [voice.np.zeros((160, 1), dtype='int16')]
        with patch.object(voice, 'transcribe', return_value=None):
            voice.finish_recording()
        self.send.assert_not_called()
        self.assertEqual(list(Path(self.folder).glob('*.wav')), [])

    def test_recognition_failure_unblocks_next_recording_and_removes_audio(self):
        voice.processing = True
        voice.frames = [voice.np.zeros((160, 1), dtype='int16')]
        with patch.object(voice, 'transcribe', side_effect=RuntimeError('failed')):
            voice.finish_recording()
        self.assertFalse(voice.processing)
        self.send.assert_not_called()
        self.assertEqual(list(Path(self.folder).glob('*.wav')), [])

    def test_wav_can_be_reopened_on_windows(self):
        path = voice.audio_to_wav(voice.np.zeros((160, 1), dtype='int16'))
        with wave.open(path, 'rb') as audio:
            self.assertEqual(audio.getframerate(), 16000)
            self.assertEqual(audio.getnchannels(), 1)
            self.assertEqual(audio.getsampwidth(), 2)
            self.assertEqual(audio.getnframes(), 160)
        os.unlink(path)

    def test_transcription_error_is_reported_without_sending_command(self):
        path = voice.audio_to_wav(voice.np.zeros((160, 1), dtype='int16'))
        with patch.object(voice.sr, 'Recognizer') as recognizer:
            recognizer.return_value.recognize_google.side_effect = voice.sr.RequestError('offline')
            self.assertIsNone(voice.transcribe(path))
            self.assertEqual(recognizer.return_value.operation_timeout, 15)
        self.log.assert_called_once()
        self.send.assert_not_called()
        os.unlink(path)

    def test_duplicate_voice_instance_does_not_register_hotkeys(self):
        with patch.object(voice, 'InstanceLock') as lock, patch.object(voice, 'run_voice') as run:
            lock.return_value.acquire.return_value = False
            voice.main()
            run.assert_not_called()

    def test_dashboard_recording_indicator_updates(self):
        import dj_server as server
        with patch.object(server, 'state', dict(server.state)):
            client = server.app.test_client()
            self.assertEqual(client.post('/voice_log', json={'recording': True}).status_code, 204)
            self.assertTrue(server.state['recording'])
            client.post('/voice_log', json={'recording': False})
            self.assertFalse(server.state['recording'])


class VoiceLauncherTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt' and shutil.which('cscript'), 'Windows Script Host required')
    def test_vbs_dry_run_resolves_current_project_and_venv(self):
        # Execute the real launcher up to Run; replace only process creation
        # with Echo. No hotkeys, microphone capture or recognizer is started.
        source = (ROOT / 'start_voice_hidden.vbs').read_text(encoding='utf-8')
        source = source[:source.index('shell.Run Chr(34)')] + 'WScript.Echo command\n'
        with tempfile.NamedTemporaryFile(dir=ROOT, prefix='.voice-launcher-check-', suffix='.vbs', delete=False) as script:
            script.write(source.encode('utf-8'))
            path = Path(script.name)
        try:
            result = subprocess.run(['cscript', '//nologo', str(path)], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))
            self.assertIn(b'.venv\\Scripts\\python.exe', result.stdout)
            self.assertIn(b'scripts\\dj_voice.py', result.stdout)
            self.assertIn(b'data\\voice.log', result.stdout)
        finally:
            path.unlink()


if __name__ == '__main__':
    unittest.main()

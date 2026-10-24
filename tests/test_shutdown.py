import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import socket
import time
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import dj_server as server
from shutdown_signal import ShutdownNotice
import json_store


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='.shutdown-test-', dir=ROOT)))
        (self.folder / 'data').mkdir()
        self.input = self.folder / 'data' / 'dj_input.txt'
        self.notice = self.folder / 'data' / 'shutdown.signal'
        self.event = threading.Event()
        for name, value in dict(BASE_DIR=self.folder, INPUT_FILE=self.input, SHUTDOWN_FILE=self.notice,
                                shutdown_requested=self.event, dj_process=None, state=dict(server.state)).items():
            self.stack.enter_context(patch.object(server, name, value))
        self.emit = self.stack.enter_context(patch.object(server.socketio, 'emit'))

    def test_dashboard_quit_is_idempotent_and_blocks_late_commands(self):
        with patch.object(server.ShutdownNotice, 'broadcast', wraps=ShutdownNotice.broadcast) as notify:
            server.handle_command(dict(cmd='quit'))
            server.handle_command(dict(cmd='quit'))
            server.handle_command(dict(cmd='1'))
            server.handle_command(dict(cmd='ai:play other music'))
            notify.assert_called_once()
        self.assertTrue(self.event.is_set())
        self.assertEqual(self.input.read_text(), 'quit')
        self.assertTrue(server.state['shutting_down'])
        self.emit.assert_called_once()

    def test_hotkey_file_is_detected_even_while_child_is_busy(self):
        server.dj_process = Mock()
        server.dj_process.poll.return_value = None
        self.input.write_text('quit', encoding='utf-8-sig')
        server.watch_shutdown()
        self.assertTrue(self.event.is_set())
        self.assertTrue(self.notice.exists())
        server.dj_process.wait.assert_not_called()

    def test_child_exit_closes_the_server_even_if_quit_file_was_consumed(self):
        server.dj_process = Mock()
        server.dj_process.poll.return_value = 0
        server.watch_shutdown()
        self.assertTrue(self.event.is_set())

    def test_failed_browser_notification_does_not_prevent_shutdown(self):
        self.emit.side_effect = RuntimeError('disconnected')
        server.request_shutdown()
        self.assertTrue(self.event.is_set())
        self.assertEqual(self.input.read_text(), 'quit')

    def test_cooperative_child_needs_no_process_killing(self):
        process = Mock()
        process.poll.return_value = None
        with patch.object(server.subprocess, 'run') as kill:
            server.stop_dj(process)
        process.wait.assert_called_once_with(timeout=8)
        kill.assert_not_called()
        process.terminate.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows process-tree fallback')
    def test_busy_child_uses_only_owned_pid_and_whole_tree(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('fake-dj', 8), 0]
        with patch.object(server.subprocess, 'run', return_value=Mock(returncode=0)) as kill:
            server.stop_dj(process)
        self.assertEqual(kill.call_args.args[0], ['taskkill', '/PID', '12345', '/T', '/F'])
        self.assertEqual(kill.call_args.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)
        process.terminate.assert_not_called()

    def test_stale_quit_is_removed_but_other_commands_are_preserved(self):
        self.input.write_text('quit', encoding='utf-8-sig')
        server.clear_stale_quit()
        self.assertFalse(self.input.exists())
        self.input.write_text('ai:music', encoding='utf-8')
        server.clear_stale_quit()
        self.assertEqual(self.input.read_text(), 'ai:music')

    def test_restart_can_acquire_lock_and_listener_is_closed_even_on_cleanup_error(self):
        http = Mock(server_port=5001)
        http.handle_request.side_effect = lambda: server.request_shutdown()
        def launch():
            server.dj_process = Mock()
            server.dj_process.poll.return_value = None
        with patch.object(server, 'make_server', return_value=http), patch.object(server, 'launch_dj', side_effect=launch), \
                patch.object(server, 'stop_dj', side_effect=[RuntimeError('fixture cleanup error'), None]):
            with self.assertRaises(RuntimeError):
                server.main()
            http.server_close.assert_called_once()
            server.main()
        self.assertEqual(http.server_close.call_count, 2)
        self.assertFalse(self.input.exists())

    def test_old_notice_does_not_stop_a_new_voice_session(self):
        ShutdownNotice.broadcast(self.notice)
        voice_notice = ShutdownNotice(self.notice)
        self.assertFalse(voice_notice.changed())
        ShutdownNotice.broadcast(self.notice)
        self.assertTrue(voice_notice.changed())
        self.assertFalse(ShutdownNotice(self.notice).changed())

    def test_launcher_does_not_wait_at_pause_after_server_exit(self):
        lines = (ROOT / 'start_dj.bat').read_text().splitlines()
        self.assertNotIn('pause', [line.strip().casefold() for line in lines])
        self.assertIn('exit /b %errorlevel%', lines)


class InterruptedSaveTests(unittest.TestCase):
    def test_interrupted_serialization_preserves_previous_file(self):
        with tempfile.TemporaryDirectory(prefix='.save-test-', dir=ROOT) as folder:
            target = Path(folder) / 'learning.json'
            target.write_text('{"previous": true}', encoding='utf-8')
            def interrupted(value, stream, **kwargs):
                stream.write('{"incomplete":')
                stream.flush()
                self.assertEqual(json.loads(target.read_text()), {'previous': True})
                raise RuntimeError('interrupted fixture write')
            with patch.object(json_store.json, 'dump', side_effect=interrupted):
                with self.assertRaises(RuntimeError):
                    json_store.write_json_atomic(target, {'next': True})
            self.assertEqual(json.loads(target.read_text()), {'previous': True})
            self.assertEqual(list(Path(folder).iterdir()), [target])

    def test_replace_failure_preserves_previous_file_and_success_replaces_snapshot(self):
        with tempfile.TemporaryDirectory(prefix='.save-test-', dir=ROOT) as folder:
            target = Path(folder) / 'cache.json'
            target.write_text('{"previous": true}', encoding='utf-8')
            with patch.object(json_store.os, 'replace', side_effect=PermissionError('fixture lock')):
                with self.assertRaises(PermissionError):
                    json_store.write_json_atomic(target, {'next': True})
            self.assertEqual(json.loads(target.read_text()), {'previous': True})
            json_store.write_json_atomic(target, {'next': True})
            self.assertEqual(json.loads(target.read_text()), {'next': True})
            self.assertEqual(list(Path(folder).iterdir()), [target])


@unittest.skipUnless(os.name == 'nt', 'Windows venv redirector/process-tree integration')
class WindowsShutdownLifecycleTests(unittest.TestCase):
    def test_websocket_quit_hotkey_quit_and_restart_with_busy_child_tree(self):
        # Real Werkzeug/SocketIO and Windows venv redirectors; fake DJ processes
        # only. The real project command, data, API and microphone are untouched.
        import ctypes
        from ctypes import wintypes
        from simple_websocket import Client
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        def running(handle):
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
        with tempfile.TemporaryDirectory(prefix='.shutdown-live-', dir=ROOT) as folder:
            base = Path(folder)
            (base / 'data').mkdir()
            fake_dj = base / 'fake_dj.py'
            fake_dj.write_text("""import os, sys, time, subprocess
from pathlib import Path
role = sys.argv[1] if len(sys.argv) > 1 else 'dj'
Path(__file__).with_name(role + '.pid').write_text(str(os.getpid()))
if role == 'dj':
    print('DJ_STATUS:Künstler auswählen', flush=True)
    subprocess.Popen([sys.executable, __file__, 'grandchild'])
while True:
    time.sleep(1)
""", encoding='utf-8')
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            runner = ("import sys; from pathlib import Path; "
                      "sys.path.insert(0, sys.argv[1]); import dj_server as s; "
                      "s.BASE_DIR=Path(sys.argv[2]); s.INPUT_FILE=s.BASE_DIR/'data/dj_input.txt'; "
                      "s.SHUTDOWN_FILE=s.BASE_DIR/'data/shutdown.signal'; s.DJ_SCRIPT=s.BASE_DIR/'fake_dj.py'; "
                      "s.main(port=int(sys.argv[3]), shutdown_timeout=.4)")
            for mode in ('dashboard', 'hotkey'):
                handles = []
                ws = None
                for name in ('dj.pid', 'grandchild.pid'):
                    (base / name).unlink(missing_ok=True)
                with (base / 'server.log').open('w+', encoding='utf-8') as log:
                    proc = subprocess.Popen([sys.executable, '-u', '-c', runner, str(ROOT / 'src'), folder, str(port)],
                                            cwd=folder, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
                    try:
                        deadline = time.monotonic() + 10
                        def pids_ready():
                            try:
                                return all((base / name).read_text().isdigit() for name in ('dj.pid', 'grandchild.pid'))
                            except OSError:
                                return False
                        while not pids_ready() and proc.poll() is None and time.monotonic() < deadline:
                            time.sleep(.05)
                        self.assertTrue(pids_ready(), 'Fake DJ did not start')
                        for name in ('dj.pid', 'grandchild.pid'):
                            pid = int((base / name).read_text())
                            handle = kernel.OpenProcess(0x1001, False, pid)
                            self.assertTrue(handle, f'Could not track owned fixture PID {pid}')
                            handles.append(handle)
                        ws = Client(f'ws://127.0.0.1:{port}/socket.io/?EIO=4&transport=websocket')
                        self.assertTrue(ws.receive(timeout=3).startswith('0'))
                        ws.send('40')
                        connected = False
                        unicode_status = False
                        for _ in range(12):
                            packet = ws.receive(timeout=3)
                            if packet.startswith('40'):
                                connected = True
                            elif packet.startswith('42'):
                                event, data = json.loads(packet[2:])
                                if event == 'state' and data.get('status') == 'Künstler auswählen':
                                    unicode_status = True
                            if connected and unicode_status:
                                break
                        else:
                            self.fail('SocketIO connection or UTF-8 status delivery failed')
                        if mode == 'dashboard':
                            ws.send('42["command",{"cmd":"quit"}]')
                        else:
                            (base / 'data/dj_input.txt').write_text('quit', encoding='utf-8-sig')
                        self.assertEqual(proc.wait(timeout=12), 0)
                        self.assertTrue(all(not running(h) for h in handles), 'Owned DJ descendant survived quit')
                        self.assertFalse((base / 'data/dj_input.txt').exists())
                        with socket.socket() as released:
                            released.bind(('127.0.0.1', port))
                    except Exception:
                        log.seek(0)
                        print(log.read())
                        raise
                    finally:
                        if ws:
                            try:
                                ws.close()
                            except Exception:
                                pass
                        if proc.poll() is None:
                            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True,
                                           timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
                            proc.wait(timeout=5)
                        for handle in reversed(handles):
                            if running(handle):
                                kernel.TerminateProcess(handle, 1)
                            kernel.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()

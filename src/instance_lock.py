"""Process-lifetime OS lock. Stale PID contents never block a restart."""
import os


class InstanceLock:
    def __init__(self, path):
        self.path = path
        self.handle = None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        handle = open(self.path, 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if os.path.getsize(self.path) == 0:
                    handle.write(b' ')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode('ascii'))
        handle.flush()
        self.handle = handle
        return True

    def release(self):
        if self.handle:
            self.handle.close()
            self.handle = None
        # Keep the file: unlinking a locked inode races with another launcher.

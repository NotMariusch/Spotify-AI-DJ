"""Keep the previous JSON snapshot intact during interrupted writes."""
import json
import os
from pathlib import Path
import tempfile


def write_json_atomic(path, value, indent=2):
    target = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                         prefix=f'.{target.name}.', suffix='.tmp', delete=False) as output:
            temporary = output.name
            json.dump(value, output, indent=indent)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

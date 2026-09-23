import os
import sys
import json

sys.path.insert(0, os.path.dirname(__file__))

from routers import live

def main():
    merged = live.sync_once()
    out = {
        'provider': live._state.get('provider'),
        'last_error': live._state.get('last_error'),
        'count': len(merged or {}),
    }
    print(json.dumps(out, indent=2))

if __name__ == '__main__':
    main()

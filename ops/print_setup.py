# поток: print
"""Inspect print schema; install only with the explicit --install option."""
import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from print_service.queue import PrintQueue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    queue = PrintQueue(schema=os.getenv('PRINT_SCHEMA', 'print_ops'))
    if args.install:
        queue.install()
    print(json.dumps(dict(schema=queue.schema, installed=queue.installed(),
                          installation_requested=args.install)))


if __name__ == '__main__':
    main()

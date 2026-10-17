import logging
import sys

from .config import Settings
from .server import serve


def main() -> None:
    # Diagnostics to stderr; stdout carries only the audit JSON lines.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    serve(Settings.from_env())


if __name__ == "__main__":
    main()

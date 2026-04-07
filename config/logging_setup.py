import logging
import sys
from pathlib import Path


def setup_logging(name: str = 'pipeline', log_file: str | None = None) -> logging.Logger:
    fmt = '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s'
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path('logs').mkdir(exist_ok=True)
        handlers.append(logging.FileHandler(Path('logs') / log_file))
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers, force=True)
    return logging.getLogger(name)

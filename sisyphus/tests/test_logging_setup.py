"""Rotating-log setup (core/logging_setup.py)."""
import logging
from logging.handlers import RotatingFileHandler

from core import logging_setup


def test_returns_log_path_and_stays_out_of_pytest():
    # 'pytest' is in sys.modules during the run, so no file handler is attached
    # (keeps the real app log free of test noise).
    logging_setup._configured = False
    path = logging_setup.setup_logging()
    assert path.name == "sisyphus.log"
    assert not any(isinstance(h, RotatingFileHandler)
                   for h in logging.getLogger().handlers)

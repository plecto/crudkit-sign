"""Run the addon's tests: ``python runtests.py [test labels]``."""

import os
import sys

import django
from django.conf import settings
from django.test.utils import get_runner

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")
    django.setup()
    runner = get_runner(settings)(verbosity=1)
    failures = runner.run_tests(sys.argv[1:] or ["tests"])
    sys.exit(bool(failures))

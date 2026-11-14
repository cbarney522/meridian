# Keep expected log output (e.g. the deliberate corrupt-config test) out of the test run.
import logging

logging.disable(logging.CRITICAL)

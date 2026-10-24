"""The bootstrap must reject unsafe pin metadata before downloading or writing."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dev_tools', ROOT / 'scripts/dev-tools.py')
dev_tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev_tools)


class ToolPins(unittest.TestCase):
    def setUp(self):
        self.pins = json.loads((ROOT / 'scripts/dev-tools-lock.json').read_text())

    def test_reviewed_pins_are_valid(self):
        dev_tools.validate_pins(self.pins)

    def test_unknown_tool_cannot_write_outside_tool_directory(self):
        self.pins['../../unexpected'] = copy.deepcopy(self.pins['helm'])
        with self.assertRaises(ValueError):
            dev_tools.validate_pins(self.pins)

    def test_unreviewed_source_or_invalid_digest_is_rejected(self):
        for key, value in [('url', 'https://example.invalid/tool.tar.gz'), ('sha256', 'latest')]:
            with self.subTest(field=key):
                pins = copy.deepcopy(self.pins)
                pins['helm']['arm64'][key] = value
                with self.assertRaises(ValueError):
                    dev_tools.validate_pins(pins)


if __name__ == '__main__':
    unittest.main()

"""Publication guardrails use fabricated secrets, never real credentials."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('check_public_tree', Path(__file__).resolve().parents[1] / 'scripts' / 'check_public_tree.py')
audit_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit_module
SPEC.loader.exec_module(audit_module)


class PublicAuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_reports_locations_without_echoing_sensitive_values(self):
        fabricated = 'apify_' + 'api_' + 'x' * 30
        path = self.write('config.py', 'key = "' + fabricated + '"\n')
        findings = audit_module.inspect_file(self.root, path)
        self.assertEqual(findings[0].category, 'provider credential')
        self.assertEqual(findings[0].line, 1)
        self.assertNotIn(fabricated, repr(findings))

    def test_private_files_are_rejected_even_without_recognizable_content(self):
        path = self.write('data/sales.json', '{}')
        self.assertEqual(audit_module.inspect_file(self.root, path)[0].category, 'private operational file')

    def test_preserves_third_party_email_notices(self):
        address = 'maintainer' + '@' + 'example.org'
        path = self.write('public/vendor/NOTICE.txt', address)
        self.assertEqual(audit_module.inspect_file(self.root, path), [])
        path = self.write('README.md', address)
        self.assertEqual(audit_module.inspect_file(self.root, path)[0].category, 'email address')

    def test_json_fixtures_require_synthetic_marker(self):
        path = self.write('tests/fixtures/property.json', '[{}]')
        self.assertEqual(audit_module.inspect_file(self.root, path)[0].category, 'fixture is not explicitly synthetic')
        path.write_text(json.dumps([{'_synthetic_fixture': True}]))
        self.assertEqual(audit_module.inspect_file(self.root, path), [])

    def test_symlink_is_flagged_without_following_target(self):
        target = self.write('secret.txt', 'private value')
        link = self.root / 'public' / 'link.txt'
        link.parent.mkdir()
        link.symlink_to(target)
        self.assertEqual(audit_module.inspect_file(self.root, link)[0].category, 'symlink requires manual review')


if __name__ == '__main__':
    unittest.main()

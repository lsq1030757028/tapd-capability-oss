"""Public-release safety contracts that never contact TAPD."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
LIVE_ACCEPTANCE = ROOT / "tests" / "acceptance_live.py"
PUBLIC_CI = ROOT / ".github" / "workflows" / "ci.yml"


class PublicReleaseSafetyTests(unittest.TestCase):
    def test_live_acceptance_is_opt_in_and_gated_before_transport(self):
        source = LIVE_ACCEPTANCE.read_text(encoding="utf-8")
        gate = source.index("if config is None:")
        transport = source.index("TapdHttpTransport(srv._credential)")
        self.assertLess(gate, transport)
        self.assertIn("TAPD_ACCEPTANCE_CONFIG_JSON", source)
        self.assertIsNone(re.search(r"\\b\\d{8,}\\b", source))

    def test_public_ci_excludes_live_acceptance_and_has_no_live_config(self):
        workflow = PUBLIC_CI.read_text(encoding="utf-8")
        self.assertIn("--ignore=tests/acceptance_live.py", workflow)
        self.assertNotIn("TAPD_ACCEPTANCE_CONFIG_JSON", workflow)
        self.assertNotIn("${{ secrets.", workflow)
        self.assertNotIn("$TAPD_ACCESS_TOKEN", workflow)
        self.assertNotRegex(workflow, r"(?m)^\\s*TAPD_ACCESS_TOKEN\\s*:")

    def test_license_and_attribution_ship_in_source_and_runtime_image(self):
        self.assertTrue((ROOT / "LICENSE").is_file())
        self.assertTrue((ROOT / "THIRD_PARTY_NOTICES.md").is_file())
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            "COPY LICENSE THIRD_PARTY_NOTICES.md /licenses/tapd-capability/",
            dockerfile,
        )


if __name__ == "__main__":
    unittest.main()

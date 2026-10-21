"""Real Mach-O fixtures catch external library leaks without installing libraries."""

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "bundle_audit", Path(__file__).resolve().parents[1] / "verify-macos-bundle.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BundleAuditTest(unittest.TestCase):
    def test_external_homebrew_link_rejected_even_with_valid_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "Fixture.app"
            executable = app / "Contents/MacOS/Fixture"
            executable.parent.mkdir(parents=True)
            (root / "dependency.c").write_text("int dependency(void) { return 0; }\n")
            (root / "main.c").write_text(
                "int dependency(void); int main(void) { return dependency(); }\n"
            )
            subprocess.run(
                [
                    "clang",
                    "-dynamiclib",
                    str(root / "dependency.c"),
                    "-Wl,-install_name,/opt/homebrew/opt/test/lib/libtest.dylib",
                    "-o",
                    str(root / "libtest.dylib"),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "clang",
                    str(root / "main.c"),
                    "-L" + str(root),
                    "-ltest",
                    "-o",
                    str(executable),
                ],
                check=True,
            )
            report = module.audit(app)
            self.assertTrue(
                any(
                    e.get("dependency", "").startswith("/opt/homebrew/")
                    for e in report["errors"]
                )
            )

    def test_system_only_program_passes_and_missing_program_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp) / "Fixture.app"
            executable = app / "Contents/MacOS/Fixture"
            executable.parent.mkdir(parents=True)
            self.assertTrue(module.audit(app)["errors"])
            source = Path(temp) / "main.c"
            source.write_text("int main(void) { return 0; }\n")
            subprocess.run(["clang", str(source), "-o", str(executable)], check=True)
            self.assertEqual(module.audit(app)["errors"], [])


if __name__ == "__main__":
    unittest.main()

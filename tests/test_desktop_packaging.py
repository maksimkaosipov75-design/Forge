import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_ID = "io.github.forge.Desktop"


class DesktopPackagingTests(unittest.TestCase):
    def test_linux_desktop_template_matches_app_id(self):
        text = (ROOT / "packaging/linux/io.github.forge.Desktop.desktop.in").read_text(encoding="utf-8")

        self.assertIn("Name=Forge", text)
        self.assertIn("Exec=@EXEC@", text)
        self.assertIn(f"Icon={APP_ID}", text)
        self.assertIn(f"StartupWMClass={APP_ID}", text)
        self.assertIn("Development;Utility;", text)

    def test_metainfo_is_valid_and_launches_desktop_id(self):
        tree = ET.parse(ROOT / "packaging/linux/io.github.forge.Desktop.metainfo.xml")
        root = tree.getroot()

        self.assertEqual(root.attrib.get("type"), "desktop-application")
        self.assertEqual(root.findtext("id"), APP_ID)
        self.assertEqual(root.findtext("launchable"), f"{APP_ID}.desktop")

    def test_installer_uses_user_local_venv_and_desktop_metadata(self):
        script = (ROOT / "scripts/install_desktop.sh").read_text(encoding="utf-8")

        self.assertIn("python3", script)
        self.assertIn("venv --system-site-packages", script)
        self.assertIn("--no-build-isolation", script)
        self.assertIn("setuptools.backends.legacy", script)
        self.assertIn("pip-install.log", script)
        self.assertIn("PYTHONPATH=\"$ROOT_DIR", script)
        self.assertIn(".local/share/forge-ai", script)
        self.assertIn(".local/bin", script)
        self.assertIn("is not on PATH", script)
        self.assertIn("Run from any directory: forge-desktop", script)
        self.assertIn("$APP_ID.desktop.in", script)
        self.assertIn("$APP_ID.metainfo.xml", script)

    def test_repo_launcher_is_symlink_safe(self):
        launcher = (ROOT / "forge-desktop").read_text(encoding="utf-8")

        self.assertIn("readlink -f", launcher)
        self.assertIn("dirname -- \"$SOURCE\"", launcher)

    def test_icon_asset_exists(self):
        icon = ROOT / "desktop/gtk/assets/io.github.forge.Desktop.svg"

        self.assertTrue(icon.is_file())
        self.assertIn("<svg", icon.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

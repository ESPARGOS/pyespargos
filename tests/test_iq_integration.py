import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import espargos


class NativeIQIntegrationTests(unittest.TestCase):
    def test_standalone_package_provides_iq_without_addons_or_qt(self):
        # An installed library has no sibling addons or demos directories.
        with tempfile.TemporaryDirectory() as directory:
            shutil.copytree(pathlib.Path(espargos.__file__).parent, pathlib.Path(directory) / "espargos", ignore=shutil.ignore_patterns("__pycache__"))
            subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); import espargos; "
                    "assert espargos.Board._capability_factories['iq'] is espargos.IQCapability; "
                    "assert espargos.IQReceiver.__module__ == 'espargos.iq_receiver'; "
                    "assert espargos.IQBacklog and espargos.IQSignalCapture and espargos.IQAccumPacket; "
                    "assert 'espargos_iqsampling' not in sys.modules; "
                    "assert not any(name.startswith('PyQt6') for name in sys.modules)",
                    directory,
                ],
                check=True,
                cwd=directory,
            )

    def test_old_iq_entry_point_is_skipped_but_other_addons_load(self):
        legacy = SimpleNamespace(value="espargos_iqsampling", name="iqsampling", load=Mock())
        other = SimpleNamespace(value="espargos_test_native_iq_addon", name="test", load=Mock())
        with patch("pathlib.Path.is_dir", return_value=False), patch("importlib.metadata.entry_points", return_value=[legacy, other]):
            espargos._load_addons()
        legacy.load.assert_not_called()
        other.load.assert_called_once_with()
        self.assertIs(espargos.Board._capability_factories["iq"], espargos.IQCapability)

    def test_old_iq_checkout_is_skipped(self):
        # Simulate discovery even on a checkout without the obsolete addon.
        legacy = pathlib.Path("/tmp/addons/iqsampling/espargos_iqsampling/__init__.py")
        with patch("pathlib.Path.is_dir", return_value=True), patch("pathlib.Path.glob", return_value=[legacy]), patch("importlib.metadata.entry_points", return_value=[]), patch("importlib.import_module") as load:
            espargos._load_addons()
        load.assert_not_called()

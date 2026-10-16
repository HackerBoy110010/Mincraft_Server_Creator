import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minecraft_server_creator.models import ServerProfile
from minecraft_server_creator.systemd import SystemdUserService


class SystemdUnitTests(unittest.TestCase):
    def test_forge_unit_uses_generated_argument_file(self):
        with tempfile.TemporaryDirectory() as temp:
            server_dir = Path(temp) / "server"
            launch_target = "libraries/net/minecraftforge/forge/1.21.4-54.1.0/unix_args.txt"
            launch_file = server_dir / launch_target
            launch_file.parent.mkdir(parents=True)
            launch_file.write_text("-cp fake Main", encoding="utf-8")
            profile = ServerProfile(
                id="forge1",
                name="Forge test",
                slug="forge-test",
                server_type="forge",
                minecraft_version="1.21.4",
                java_major=21,
                server_dir=str(server_dir),
                loader_version="54.1.0",
                launch_mode="args",
                launch_target=launch_target,
            )
            service = SystemdUserService(Path(temp) / "units")
            with patch("minecraft_server_creator.systemd.SystemdUserService.supported", return_value=True):
                with patch.object(service, "_run") as run:
                    service.install_unit(profile, "/managed/java")
            unit = service.unit_path(profile).read_text(encoding="utf-8")
            exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
            self.assertIn('"/managed/java"', exec_start)
            self.assertIn('"@' + launch_target + '"', exec_start)
            self.assertIn('"nogui"', exec_start)
            self.assertNotIn('"-jar"', exec_start)
            run.assert_called_once_with(["systemctl", "--user", "daemon-reload"])


if __name__ == "__main__":
    unittest.main()

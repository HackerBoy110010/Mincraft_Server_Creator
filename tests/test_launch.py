import tempfile
import unittest
from pathlib import Path

from minecraft_server_creator.launch import LaunchConfigurationError, build_java_command, validate_profile_launch
from minecraft_server_creator.models import ServerProfile


def make_profile(server_dir, **overrides):
    values = {
        "id": "launch1",
        "name": "Launch test",
        "slug": "launch-test",
        "server_type": "forge",
        "minecraft_version": "1.21.4",
        "java_major": 21,
        "server_dir": str(server_dir),
        "loader_version": "54.1.0",
        "launch_mode": "args",
        "launch_target": "libraries/net/minecraftforge/forge/1.21.4-54.1.0/unix_args.txt",
    }
    values.update(overrides)
    return ServerProfile(**values)


class LaunchTests(unittest.TestCase):
    def test_builds_forge_argfile_command_without_jar_launcher(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = make_profile(temp)
            launch_file = Path(temp) / profile.launch_target
            launch_file.parent.mkdir(parents=True)
            launch_file.write_text("-cp fake Main", encoding="utf-8")
            self.assertEqual(validate_profile_launch(profile), launch_file)
            (Path(temp) / "user_jvm_args.txt").write_text("-Dexample=true", encoding="utf-8")
            self.assertEqual(
                build_java_command(profile, "/managed/java"),
                [
                    "/managed/java",
                    "@user_jvm_args.txt",
                    "-Xms1024M",
                    "-Xmx4096M",
                    f"@{profile.launch_target}",
                    "nogui",
                ],
            )

    def test_builds_vanilla_jar_command(self):
        with tempfile.TemporaryDirectory() as temp:
            jar = Path(temp) / "server.jar"
            jar.write_bytes(b"jar")
            profile = make_profile(
                temp,
                server_type="vanilla",
                loader_version=None,
                launch_mode="jar",
                launch_target="server.jar",
            )
            self.assertEqual(
                build_java_command(profile, "/managed/java"),
                ["/managed/java", "-Xms1024M", "-Xmx4096M", "-jar", str(jar), "nogui"],
            )

    def test_fails_closed_if_args_file_is_missing_or_profile_metadata_is_mismatched(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = make_profile(temp)
            with self.assertRaisesRegex(LaunchConfigurationError, "Startdatei fehlt"):
                validate_profile_launch(profile)
            launch_file = Path(temp) / profile.launch_target
            launch_file.parent.mkdir(parents=True)
            launch_file.write_text("args", encoding="utf-8")
            changed = make_profile(temp, launch_target="libraries/other/unix_args.txt")
            with self.assertRaisesRegex(LaunchConfigurationError, "stimmt nicht"):
                validate_profile_launch(changed)


if __name__ == "__main__":
    unittest.main()

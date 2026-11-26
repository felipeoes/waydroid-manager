# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for install.sh layouts, packaging scripts syntax and RPM spec checks."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
INSTALL_SH = os.path.join(ROOT, "scripts", "install.sh")
RPM_SPEC = os.path.join(ROOT, "packaging", "rpm", "waydroid-manager.spec.in")
RPM_PRESET = os.path.join(ROOT, "packaging", "rpm", "85-waydroid-manager.preset")


def read_text(path):
    with open(path) as f:
        return f.read()


class ShellSyntaxTest(unittest.TestCase):
    def test_shell_scripts_have_valid_posix_syntax(self):
        scripts = [
            "scripts/install.sh",
            "scripts/uninstall.sh",
            "packaging/build-libgbinder.sh",
            "packaging/deb/build.sh",
            "packaging/rpm/build.sh",
            "packaging/tarball/build.sh",
        ]
        for rel in scripts:
            path = os.path.join(ROOT, rel)
            res = subprocess.run(["sh", "-n", path], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, "{}: {}".format(rel, res.stderr))


class InstallLayoutTest(unittest.TestCase):
    def stage(self, *extra_args):
        dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest)
        cmd = ["sh", INSTALL_SH, "--destdir", dest, "--no-activate"] + list(extra_args)
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)
        return dest

    def test_deb_and_rpm_package_layouts(self):
        for method in ("deb", "rpm"):
            dest = self.stage("--prefix", "/usr", "--method", method)
            libdir = os.path.join(dest, "usr/lib/waydroid-manager")
            self.assertEqual(read_text(os.path.join(libdir, "install-method")).strip(), method)
            self.assertFalse(os.path.exists(os.path.join(libdir, "install-paths")))

            unit = os.path.join(dest, "usr/lib/systemd/system/waydroid-manager.service")
            self.assertIn("ExecStart=/usr/bin/waydroid-manager-daemon", read_text(unit))
            self.assertTrue(os.path.isfile(os.path.join(
                dest, "usr/share/dbus-1/system.d/io.github.waydroidmanager.Manager.conf")))
            self.assertTrue(os.path.isfile(os.path.join(
                dest, "usr/share/dbus-1/system-services/io.github.waydroidmanager.Manager.service")))
            self.assertTrue(os.path.isfile(os.path.join(
                dest, "usr/lib/udev/rules.d/70-waydroid-manager.rules")))
            self.assertTrue(os.path.isfile(os.path.join(dest, "etc/waydroid-manager/daemon.conf")))

            desktop = os.path.join(dest, "usr/share/applications/io.github.waydroidmanager.desktop")
            self.assertIn("\nExec=waydroid-manager-gui\n", read_text(desktop))

            for tool in ("waydroid-manager", "waydroid-manager-gui", "waydroid-manager-daemon"):
                wrapper = os.path.join(dest, "usr/bin", tool)
                self.assertTrue(os.access(wrapper, os.X_OK))
                self.assertIn('PYTHONPATH="/usr/lib/waydroid-manager', read_text(wrapper))

    def test_immutable_script_layout(self):
        dest = self.stage("--prefix", "/usr/local", "--immutable", "--method", "script")
        libdir = os.path.join(dest, "usr/local/lib/waydroid-manager")
        self.assertEqual(read_text(os.path.join(libdir, "install-method")).strip(), "script")

        # Immutable installs must not write into /usr outside /usr/local
        self.assertFalse(os.path.exists(os.path.join(dest, "usr/lib")))
        self.assertFalse(os.path.exists(os.path.join(dest, "usr/share")))

        unit = os.path.join(dest, "etc/systemd/system/waydroid-manager.service")
        self.assertIn("ExecStart=/usr/local/bin/waydroid-manager-daemon", read_text(unit))
        self.assertTrue(os.path.isfile(os.path.join(
            dest, "etc/dbus-1/system.d/io.github.waydroidmanager.Manager.conf")))
        self.assertTrue(os.path.isfile(os.path.join(
            dest, "etc/dbus-1/system-services/io.github.waydroidmanager.Manager.service")))
        self.assertTrue(os.path.isfile(os.path.join(
            dest, "etc/udev/rules.d/70-waydroid-manager.rules")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "etc/waydroid-manager/daemon.conf")))

        desktop = os.path.join(dest, "usr/local/share/applications/io.github.waydroidmanager.desktop")
        self.assertIn("\nExec=/usr/local/bin/waydroid-manager-gui\n", read_text(desktop))

        paths_file = os.path.join(libdir, "install-paths")
        self.assertTrue(os.path.isfile(paths_file))
        entries = dict(line.split("=", 1) for line in read_text(paths_file).splitlines())
        self.assertEqual(entries, {
            "UNITDIR": "/etc/systemd/system",
            "DBUS_CONF_DIR": "/etc/dbus-1/system.d",
            "DBUS_SVC_DIR": "/etc/dbus-1/system-services",
            "UDEV_DIR": "/etc/udev/rules.d",
            "DESKTOP_DIR": "/usr/local/share/applications",
        })

        for tool in ("waydroid-manager", "waydroid-manager-gui", "waydroid-manager-daemon"):
            wrapper = os.path.join(dest, "usr/local/bin", tool)
            self.assertTrue(os.access(wrapper, os.X_OK))
            self.assertIn('PYTHONPATH="/usr/local/lib/waydroid-manager', read_text(wrapper))

    def test_immutable_flag_defaults_prefix_to_usr_local(self):
        dest = self.stage("--immutable", "--method", "script")
        self.assertTrue(os.path.isdir(os.path.join(dest, "usr/local/lib/waydroid-manager")))
        self.assertFalse(os.path.exists(os.path.join(dest, "usr/lib")))


class RpmSpecTest(unittest.TestCase):
    def test_spec_guards_and_metadata(self):
        spec = read_text(RPM_SPEC)
        self.assertIn("%global __provides_exclude_from ^/usr/lib/waydroid-manager/lib/", spec)
        self.assertIn("%global __requires_exclude_from ^/usr/lib/waydroid-manager/data/(openrc|runit)/", spec)
        self.assertIn("%config(noreplace) /etc/waydroid-manager/daemon.conf", spec)
        self.assertIn("/usr/lib/systemd/system-preset/85-waydroid-manager.preset", spec)
        self.assertEqual(read_text(RPM_PRESET).strip(), "enable waydroid-manager.service")

        for macro in ("%systemd_post waydroid-manager.service",
                      "%systemd_preun waydroid-manager.service",
                      "%systemd_postun_with_restart waydroid-manager.service"):
            self.assertIn(macro, spec)

        # Every scriptlet that touches live systemd/runtime state must guard with /run/systemd/system
        sections = {}
        sec_end = r"(?=^%(?:description|prep|build|install|check|clean|pre|post|preun|postun|files|changelog)\b|\Z)"
        for name in ("pre", "post", "preun", "postun"):
            m = re.search(r"(?m)^%{}\n(.*?){}".format(name, sec_end), spec, re.S)
            self.assertIsNotNone(m, "missing %{} section".format(name))
            sections[name] = m.group(1)
            self.assertIn("[ -d /run/systemd/system ]", sections[name],
                          "%{} missing /run/systemd/system guard for rpm-ostree".format(name))


if __name__ == "__main__":
    unittest.main()

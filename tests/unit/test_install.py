# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for install.sh/uninstall.sh layouts, packaging scripts and RPM spec checks."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from waydroid_manager import paths
from waydroid_manager.session import main as session_main

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
INSTALL_SH = os.path.join(ROOT, "scripts", "install.sh")
UNINSTALL_SH = os.path.join(ROOT, "scripts", "uninstall.sh")
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
            "scripts/migrate-from-waydroid-multi.sh",
            "packaging/build-libgbinder.sh",
            "packaging/deb/build.sh",
            "packaging/deb/preinst",
            "packaging/deb/postinst",
            "packaging/deb/prerm",
            "packaging/deb/postrm",
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
        # dbus-daemon never searches /etc/dbus-1/system-services
        self.assertFalse(os.path.exists(os.path.join(dest, "etc/dbus-1/system-services")))

        unit = os.path.join(dest, "etc/systemd/system/waydroid-manager.service")
        self.assertIn("ExecStart=/usr/local/bin/waydroid-manager-daemon", read_text(unit))
        self.assertTrue(os.path.isfile(os.path.join(
            dest, "etc/dbus-1/system.d/io.github.waydroidmanager.Manager.conf")))
        self.assertTrue(os.path.isfile(os.path.join(
            dest, "usr/local/share/dbus-1/system-services/io.github.waydroidmanager.Manager.service")))
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
            "DBUS_SVC_DIR": "/usr/local/share/dbus-1/system-services",
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

    def test_prefix_trailing_slashes_are_normalized(self):
        dest = self.stage("--prefix", "/usr/local///", "--immutable", "--method", "script")
        unit = os.path.join(dest, "etc/systemd/system/waydroid-manager.service")
        self.assertIn("ExecStart=/usr/local/bin/waydroid-manager-daemon", read_text(unit))
        desktop = os.path.join(dest, "usr/local/share/applications/io.github.waydroidmanager.desktop")
        self.assertIn("\nExec=/usr/local/bin/waydroid-manager-gui\n", read_text(desktop))


class UninstallTest(unittest.TestCase):
    def _extract_remove_files(self):
        text = read_text(UNINSTALL_SH)
        m = re.search(r"(?m)^remove_files\(\) \{\n.*?\n\}", text, re.S)
        self.assertIsNotNone(m)
        return m.group(0)

    def _run_remove_files(self, install_paths_content=None):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        prefix = os.path.join(tmp, "prefix")
        libdir = os.path.join(prefix, "lib", "waydroid-manager")
        os.makedirs(libdir)
        if install_paths_content is not None:
            with open(os.path.join(libdir, "install-paths"), "w") as f:
                f.write(install_paths_content)
        removed_log = os.path.join(tmp, "removed.txt")
        fakebin = os.path.join(tmp, "bin")
        os.makedirs(fakebin)
        for cmd in ("sed", "systemctl", "dbus-send", "udevadm"):
            stub = os.path.join(fakebin, cmd)
            with open(stub, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(stub, 0o755)
        fn = self._extract_remove_files()
        harness = (
            "set -u\n"
            'PATH="{fakebin}:$PATH"\n'
            'PREFIX="{prefix}"\n'
            'LIBDIR="{libdir}"\n'
            'SV_DIR="{sv}"\n'
            'RUNDIR="{run}"\n'
            'rm() {{ for a in "$@"; do case "$a" in -*) ;; *) printf "%s\\n" "$a" >> "{log}" ;; esac; done; }}\n'
            "{fn}\n"
            "remove_files\n"
        ).format(fakebin=fakebin, prefix=prefix, libdir=libdir, sv=os.path.join(tmp, "sv"),
                 run=os.path.join(tmp, "run"), log=removed_log, fn=fn)
        res = subprocess.run(["sh", "-c", harness], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        return read_text(removed_log).splitlines(), tmp

    def test_remove_files_uses_install_paths_including_no_trailing_newline(self):
        content = (
            "UNITDIR=/etc/systemd/system\n"
            "DBUS_CONF_DIR=/etc/dbus-1/system.d\n"
            "DBUS_SVC_DIR=/usr/local/share/dbus-1/system-services\n"
            "UDEV_DIR=/etc/udev/rules.d\n"
            "DESKTOP_DIR=/usr/local/share/applications"  # intentionally no trailing newline
        )
        removed, _tmp = self._run_remove_files(content)
        self.assertIn("/etc/systemd/system/waydroid-manager.service", removed)
        self.assertIn("/etc/dbus-1/system.d/io.github.waydroidmanager.Manager.conf", removed)
        self.assertIn("/usr/local/share/dbus-1/system-services/io.github.waydroidmanager.Manager.service", removed)
        self.assertIn("/etc/udev/rules.d/70-waydroid-manager.rules", removed)
        self.assertIn("/usr/local/share/applications/io.github.waydroidmanager.desktop", removed)

    def test_remove_files_ignores_relative_and_unsafe_install_paths(self):
        marker = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, marker)
        pwn = os.path.join(marker, "pwned")
        content = (
            "UNITDIR=relative/systemd\n"
            "DBUS_CONF_DIR=../etc/dbus\n"
            "DBUS_SVC_DIR=\n"
            "UDEV_DIR=udev/rules.d\n"
            "DESKTOP_DIR=/tmp/$(touch {})\n"
            "OTHER=/etc/passwd\n"
        ).format(pwn)
        removed, tmp = self._run_remove_files(content)
        self.assertFalse(os.path.exists(pwn))
        self.assertNotIn("relative/systemd/waydroid-manager.service", removed)
        self.assertNotIn("../etc/dbus/io.github.waydroidmanager.Manager.conf", removed)
        self.assertNotIn("/etc/passwd", removed)
        # Rejected entries fall back to defaults
        prefix = os.path.join(tmp, "prefix")
        self.assertIn(prefix + "/lib/systemd/system/waydroid-manager.service", removed)
        self.assertIn("/usr/share/dbus-1/system.d/io.github.waydroidmanager.Manager.conf", removed)
        self.assertIn("/usr/share/dbus-1/system-services/io.github.waydroidmanager.Manager.service", removed)
        self.assertIn("/usr/lib/udev/rules.d/70-waydroid-manager.rules", removed)

    def test_default_prefix_falls_back_to_usr_local_when_only_local_exists(self):
        text = read_text(UNINSTALL_SH)
        m = re.search(r'(?m)^case "\$SELF_DIR" in\n.*?\nesac', text, re.S)
        self.assertIsNotNone(m)
        block = m.group(0)

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        # Rewrite the absolute checks inside the extracted block to point under tmp
        sandboxed = block.replace("/usr/lib/waydroid-manager", tmp + "/usr/lib/waydroid-manager")
        sandboxed = sandboxed.replace("/usr/local/lib/waydroid-manager", tmp + "/usr/local/lib/waydroid-manager")

        def eval_prefix(self_dir):
            script = 'set -u\nSELF_DIR="{}"\n{}\nprintf "%s" "$DEFAULT_PREFIX"\n'.format(self_dir, sandboxed)
            res = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            return res.stdout

        # Neither exists -> /usr
        self.assertEqual(eval_prefix("/tmp/checkout/scripts"), "/usr")
        # Only /usr/local/lib/waydroid-manager exists -> /usr/local
        os.makedirs(os.path.join(tmp, "usr/local/lib/waydroid-manager"))
        self.assertEqual(eval_prefix("/tmp/checkout/scripts"), "/usr/local")
        # Both exist -> /usr
        os.makedirs(os.path.join(tmp, "usr/lib/waydroid-manager"))
        self.assertEqual(eval_prefix("/tmp/checkout/scripts"), "/usr")
        # Invoked directly from an installed libdir -> that prefix
        self.assertEqual(eval_prefix("/opt/wdm/lib/waydroid-manager"), "/opt/wdm")


class RpmSpecTest(unittest.TestCase):
    def test_spec_guards_and_metadata(self):
        spec = read_text(RPM_SPEC)
        self.assertIn("%global __provides_exclude_from ^/usr/lib/waydroid-manager/lib/", spec)
        self.assertIn("%global __requires_exclude_from ^/usr/lib/waydroid-manager/data/(openrc|runit)/", spec)
        self.assertIn("%{?systemd_ordering}", spec)
        self.assertRegex(spec, r"(?m)^Requires:\s+iptables-nft$")
        self.assertNotIn("(iptables-nft or nftables)", spec)
        self.assertIn("%config(noreplace) /etc/waydroid-manager/daemon.conf", spec)
        self.assertIn("%dir /usr/share/licenses/waydroid-manager", spec)
        self.assertIn("%dir /usr/share/doc/waydroid-manager", spec)
        self.assertIn("/usr/lib/systemd/system-preset/85-waydroid-manager.preset", spec)
        self.assertEqual(read_text(RPM_PRESET).strip(), "enable waydroid-manager.service")

        for macro in ("%systemd_post waydroid-manager.service",
                      "%systemd_preun waydroid-manager.service",
                      "%systemd_postun_with_restart waydroid-manager.service"):
            self.assertIn(macro, spec)

        # Every scriptlet that touches live systemd/runtime state must guard with /run/systemd/system
        sections = {}
        sec_end = (
            r"(?=^%(?:description|prep|build|install|check|clean|"
            r"pre|post|preun|postun|files|changelog)\b|\Z)"
        )
        for name in ("pre", "post", "preun", "postun"):
            m = re.search(r"(?m)^%{}\n(.*?){}".format(name, sec_end), spec, re.S)
            self.assertIsNotNone(m, "missing %{} section".format(name))
            sections[name] = m.group(1)
            self.assertIn("[ -d /run/systemd/system ]", sections[name],
                          "%{} missing /run/systemd/system guard for rpm-ostree".format(name))

        # %pre must not query rpmdb (rpm -qf), must clean stale install-paths and check /usr/local
        self.assertNotIn("rpm -qf", sections["pre"])
        self.assertIn("rm -f /usr/lib/waydroid-manager/install-paths", sections["pre"])
        self.assertIn("/usr/local/lib/waydroid-manager", sections["pre"])

        # %post only restarts on fresh install ($1 -eq 1); upgrades use %systemd_postun_with_restart
        self.assertIn('[ "$1" -eq 1 ]', sections["post"])
        self.assertNotIn("try-restart", sections["post"])


class PkexecCliPathTest(unittest.TestCase):
    def test_cli_path_falls_back_to_prefix_bin(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        pkg_dir = os.path.join(tmp, "lib", "waydroid-manager", "waydroid_manager")
        bin_dir = os.path.join(tmp, "bin")
        os.makedirs(pkg_dir)
        os.makedirs(bin_dir)
        cli_bin = os.path.join(bin_dir, "waydroid-manager")
        with open(cli_bin, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(cli_bin, 0o755)

        with mock.patch.object(session_main.shutil, "which", return_value=None), \
                mock.patch.object(paths, "PKG_DIR", pkg_dir):
            self.assertEqual(session_main._cli_path(), cli_bin)

        with mock.patch.object(session_main.shutil, "which", return_value="/usr/bin/waydroid-manager"):
            self.assertEqual(session_main._cli_path(), "/usr/bin/waydroid-manager")


class UninstallRebootNoticeTest(unittest.TestCase):
    def test_reboot_notice_only_for_ostree_rpm(self):
        try:
            from waydroid_manager.gui import window
        except (ImportError, ValueError):
            self.skipTest("GTK4/Adw unavailable")

        with mock.patch.object(window.os.path, "exists", return_value=False):
            self.assertFalse(window._uninstall_reboot_needed("Reboot to complete", "rpm"))

        with mock.patch.object(window.os.path, "exists", return_value=True):
            self.assertTrue(window._uninstall_reboot_needed("", "rpm"))
            self.assertTrue(window._uninstall_reboot_needed("Reboot to complete removing.", "script"))
            self.assertFalse(window._uninstall_reboot_needed("Removed cleanly.", "script"))


if __name__ == "__main__":
    unittest.main()

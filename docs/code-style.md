# Code style and guidelines

How code in waydroid-multi is written, and what to do and not do when you add to it. The
overall rule: **new code should look like the code around it.** If something here disagrees
with a file you're editing, follow this guide for new code, and don't reformat old code in the
same change.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the architecture and the PR workflow.

## Python

- **Python 3, standard library plus distro packages only.** The allowed packages are
  python3-dbus, python3-gi (GTK 4, libadwaita, Pango, Rsvg), python3-cairo, python3-gbinder and
  stock Waydroid's `tools`. **No pip dependencies**: the program is installed by `install.sh`
  and the .deb, and must run with what the distro provides. A new runtime dependency has to be
  added to the .deb's `Depends` (`packaging/deb/control.in`), the README requirements and the
  `install.sh` checks.
- **Every file starts with** `# SPDX-License-Identifier: GPL-3.0-or-later` and a one-line module
  docstring saying what the module is for.
- **Strings:** use `"...".format(...)`, which is what the code uses (≈200 times vs. a handful of
  f-strings). Use double quotes.
- **Lines** up to about 110 characters, with a hard limit of 120. Indent with 4 spaces.
- **No type annotations.** The code doesn't use them, and a half-annotated codebase is worse
  than none.
- **Names:** `snake_case` functions and variables, `CamelCase` classes, `UPPER_CASE` module
  constants. Use the short names the code already uses: `inst` for an `Instance`, `iid` for an
  instance id, `uid`, `fd`, `cfg`.
- **Functions** should be short and do one thing. Prefer a plain function over a class unless
  there is state to keep.
- **Comments say why, not what.** Write them for the surprising parts: a kernel or compositor
  quirk, a race, a security reason. Don't narrate obvious code. Docstrings are one or two lines.
- **Imports:** standard library first, then third-party (`dbus`, `gi`), then relative project
  imports. `gi.require_version(...)` goes before `from gi.repository import ...`, and those
  imports get `# noqa: E402`.
- **Errors:**
  - in the daemon, raise `Error(message, "Name")` from `daemon/main.py` for anything the caller
    should see. Messages are short sentences a user understands ("stop instance #2 before
    cloning it");
  - in the CLI, use `die(message)`;
  - don't catch broad exceptions unless you log them (`log.exception`) or turn them into an
    `Error`. A bare `except Exception` needs `# noqa: BLE001` and a reason.
- **Logging:** use the module's `log` (`logging.getLogger("waydroid-multi…")`), never `print` in
  the daemon or the session. Prefix messages with the instance id: `log.info("%s: …", inst.id)`.
  Log user-supplied strings with `%r`, so they can't fake log lines.
- **User-facing text** (CLI output, GUI, notifications) uses plain words and refers to instances
  as `#N` or by name. Never show internal terms (lxc, binder, overlay) unless the user is
  debugging.

## Root daemon (`waydroid_multi/daemon/`)

The daemon runs as root and takes requests from every local user. Treat each change as
security-relevant.

**Do**
- **Validate every D-Bus argument at the boundary**, using the validators in `instance.py`.
  Check the owner (`check_owner`) before doing anything to an instance.
- **Treat every path below a user's home, and everything inside a container, as hostile:**
  - walk paths with `O_NOFOLLOW`, as `_clone_source` does;
  - open files inside a running container with `util.open_in_container`;
  - work through file descriptors (`/proc/<pid>/fd/N`) instead of re-resolving a checked path;
  - `chown` with `follow_symlinks=False`.
- **Check passed fds** with `_regular_fd`: a regular file with the right access mode.
- **Run long work** (`Start`, `Create`, copies) in a worker thread via `run_async`, holding the
  instance lock. Emit signals from the GLib loop with `GLib.idle_add`.
- **Read and write config with `configparser.ConfigParser(interpolation=None)`.**
- **Write generated files atomically**: write to a temporary file, then `os.replace`.

**Don't**
- Don't use `shell=True`, or `sh -c` with anything user-controlled. Pass argument lists to `run`.
- Don't hand a caller's file or fd to `lxc-attach`. It chowns and chmods its stdio, so always go
  through pipes (see `storage.install_apk`, `storage.screenshot`).
- Don't block the GLib main loop: no `time.sleep`, waiting on a subprocess or long I/O in a
  D-Bus handler.
- Don't `rm -rf` a tree while anything may be mounted below it. `--one-file-system` does not
  stop at bind mounts from the same filesystem. Unmount first (`umount_tree`) and verify.
- Don't let a non-root caller set protected Android properties (`PROTECTED_PROP_RE`), or add a
  setting that ends up as raw text in the LXC config without a strict validator.
- Don't poll `waydroid status` or use stock Waydroid's stateful helpers (its run helpers,
  D-Bus services). Stock 1.6.x leaks fds per call. Read stock state through `stockctl.py`.
- Don't modify anything that belongs to stock Waydroid (`/var/lib/waydroid`,
  `~/.local/share/waydroid`, its LXC container or config).

## Wayland proxy (`waydroid_multi/session/wlproxy.py`)

The HWC is a client we can't change, and it aborts on anything unexpected.

**Do**
- Add new message handling to the `Translator`/`Session` handlers, and **add a unit test** in
  `tests/unit/test_wlproxy.py` that feeds a synthetic HWC sequence.
- Keep proxy-created objects in the proxy's id range, and swallow every event about them.
- Inject messages that carry fds only at message boundaries, with an empty inbound buffer.

**Don't**
- Don't send the HWC an event that references an object it didn't create.
- Don't forward a real `xdg_toplevel.configure` size (it hotplugs Android's display), and don't
  forward `xdg_toplevel.close` (it wipes Android's recent tasks).
- Don't block. The proxy is a single-threaded event loop; anything slow belongs in the session.

## GUI (`waydroid_multi/gui/`)

- Use **libadwaita widgets** (`Adw.PreferencesGroup`, `Adw.SwitchRow`, `Adw.ComboRow`,
  `Adw.AlertDialog`, toasts) and follow the GNOME HIG.
- **Call the daemon asynchronously** (`Backend.call` with `ok`/`fail`), never synchronously from
  a signal handler.
- Report results with a **toast**. Confirm destructive actions (delete, stop on close,
  uninstall) with an `Adw.AlertDialog` using `DESTRUCTIVE` appearance.
- Settings that need a restart say so ("restart the instance to apply").

## Shell scripts (`scripts/`, `data/`, `packaging/`)

- POSIX `sh`, not bash. Start with `set -eu` (or `set -u` where failing steps are expected and
  handled).
- Quote every expansion, and use `"$@"`.
- Root scripts must not follow user-controlled symlinks. Do per-user work as that user
  (`runuser -u "$user" -- …`).
- Check syntax with `sh -n` before committing.
- `scripts/install.sh` is the single source of the installed file layout. When you add an
  installed file, add it there, and the .deb picks it up.

## Tests

- **Unit tests** (`tests/unit/`, standard `unittest`, no root): required for anything that
  generates config (`lxcconfig`, `netconfig`, props), parses or rewrites Wayland messages,
  validates input, or allocates ids. Run them with
  `python3 -m unittest discover -s tests/unit -t .`.
- **End-to-end** (`tests/integration/smoke.sh`): run it before opening a PR that touches the
  daemon, containers, networking or cloning, and say in the PR that it passed.
- **GUI and window changes:** test by hand, and describe what you checked in the PR.

## Commits and pull requests

- **One topic per commit.** The subject is in the imperative ("Add …", "Fix …"), about 70
  characters at most, with a body explaining why when it isn't obvious.
- **Keep PRs focused.** Refactors go in their own PR, separate from behaviour changes.
- **Update docs in the same PR:** README for user-visible changes, CONTRIBUTING or this file for
  developer-facing ones, and add an entry under `## [Unreleased]` in `CHANGELOG.md`.
- **Don't commit** generated files (`dist/`, `__pycache__`), local notes or anything with
  credentials.

# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-instance IHardware binder service, run as a small helper process.

python3-gbinder never releases the GIL and its async add_service() is broken,
so binder services are registered with add_service_sync() in a process of
their own: a stalled binder call can only block this helper, never the daemon.

Android's requests are answered immediately and reported to the daemon as
lines on stdout: "wdm:suspend", "wdm:reboot", "wdm:upgrade" (libgbinder may
print its own log lines there too; those lack the prefix).

  python3 -m waydroid_multi.daemon.hwhelper /var/lib/waydroid-multi/instances/<id>/instance.cfg
"""
import configparser
import signal
import sys

INTERFACE = "lineageos.waydroid.IHardware"
SERVICE_NAME = "waydroidhardware"

TRANSACTION_enableNFC = 1
TRANSACTION_enableBluetooth = 2
TRANSACTION_suspend = 3
TRANSACTION_reboot = 4
TRANSACTION_upgrade = 5
TRANSACTION_upgrade2 = 6


def emit(word):
    try:
        sys.stdout.write("wdm:" + word + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        sys.exit(0)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cfg = configparser.ConfigParser()
    if not argv or not cfg.read(argv[0]):
        print("usage: hwhelper INSTANCE_CFG", file=sys.stderr)
        return 2
    w = cfg["waydroid"]

    import gbinder
    from gi.repository import GLib

    sm = gbinder.ServiceManager("/dev/" + w["binder"], w["service_manager_protocol"], w["binder_protocol"])

    def handler(req, code, flags):
        reply = obj.new_reply()
        if code in (TRANSACTION_enableNFC, TRANSACTION_enableBluetooth):
            reply.append_int32(0)
            reply.append_int32(0)
        elif code == TRANSACTION_suspend:
            emit("suspend")
            reply.append_int32(0)
        elif code == TRANSACTION_reboot:
            emit("reboot")
            reply.append_int32(0)
        elif code in (TRANSACTION_upgrade, TRANSACTION_upgrade2):
            emit("upgrade")
            reply.append_int32(0)
        else:
            return reply, -99999  # unknown: make Android raise a RemoteException
        return reply, 0

    def presence():
        if sm.is_present():
            status = sm.add_service_sync(SERVICE_NAME, obj)
            emit("registered" if status == 0 else "error {}".format(status))

    obj = sm.new_local_object(INTERFACE, handler)
    loop = GLib.MainLoop()
    sm.add_presence_handler(presence)
    presence()
    from waydroid_multi.glibcompat import signal_add
    signal_add(signal.SIGTERM, loop.quit)
    signal_add(signal.SIGINT, loop.quit)
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

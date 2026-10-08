/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Addresses in the pinned full13 HWC; code offsets differ from file offsets by 0x4000. */
wl_display_create_queue = 0x640b0;
wl_event_queue_destroy = 0x63f50;
wl_proxy_create_wrapper = 0x65340;
wl_proxy_wrapper_destroy = 0x65460;
wl_proxy_set_queue = 0x653e0;
wl_proxy_marshal_flags = 0x64830;
wl_proxy_add_listener = 0x64470;
wl_proxy_destroy = 0x64270;
wl_display_dispatch_queue = 0x654d0;
wl_display_dispatch_queue_pending = 0x65fd0;
wl_callback_interface = 0x9e388;
done_listener = 0x9f2f8;
epilogue = 0x6530c;
SECTIONS { .text 0x65131 : { *(.text) } }

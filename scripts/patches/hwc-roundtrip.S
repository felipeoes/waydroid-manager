# SPDX-License-Identifier: GPL-3.0-or-later
# Keep sync callbacks off the HWC's concurrently dispatched default queue.
# Replaces the body of wl_display_roundtrip_queue in the SHA-pinned full13 HWC;
# its original prologue, epilogue, done listener and unwind metadata stay intact.
# VMA 0x65131 is file offset 0x61131. Reproduce HWC_NVIDIA_ROUNDTRIP with:
#   as --64 hwc-roundtrip.S -o /tmp/roundtrip.o
#   ld -e roundtrip -T hwc-roundtrip.ld /tmp/roundtrip.o -o /tmp/roundtrip.elf
#   objcopy -O binary --only-section=.text /tmp/roundtrip.elf /tmp/roundtrip.bin

.text
.globl roundtrip
roundtrip:
    mov %rsi, %rbx
    mov %rdi, %r14
    movl $0, -0x2c(%rbp)
    call wl_display_create_queue
    mov %rax, %r12
    test %rax, %rax
    jz failed
    mov %r14, %rdi
    call wl_proxy_create_wrapper
    mov %rax, %r13
    test %rax, %rax
    jz failed_queue
    mov %rax, %rdi
    mov %r12, %rsi
    call wl_proxy_set_queue
    mov %r13, %rdi
    xor %esi, %esi
    lea wl_callback_interface(%rip), %rdx
    mov $1, %ecx
    xor %r8d, %r8d
    xor %r9d, %r9d
    xor %eax, %eax
    call wl_proxy_marshal_flags
    mov %rax, -0x40(%rbp)
    mov %r13, %rdi
    call wl_proxy_wrapper_destroy
    mov -0x40(%rbp), %r13
    mov $-1, %r15d
    test %r13, %r13
    jz cleanup
    mov %r13, %rdi
    lea done_listener(%rip), %rsi
    lea -0x2c(%rbp), %rdx
    call wl_proxy_add_listener
    test %eax, %eax
    js destroy_callback
dispatch:
    mov %r14, %rdi
    mov %r12, %rsi
    call wl_display_dispatch_queue
    mov %eax, %r15d
    cmpl $0, -0x2c(%rbp)
    jne cleanup
    test %eax, %eax
    jns dispatch
destroy_callback:
    mov %r13, %rdi
    call wl_proxy_destroy
cleanup:
    mov %r12, %rdi
    call wl_event_queue_destroy
    test %r15d, %r15d
    js epilogue
    mov %r14, %rdi
    mov %rbx, %rsi
    call wl_display_dispatch_queue_pending
    mov %eax, %r15d
    jmp epilogue
failed_queue:
    mov %r12, %rdi
    call wl_event_queue_destroy
failed:
    mov $-1, %r15d
    jmp epilogue

# SPDX-License-Identifier: GPL-3.0-or-later
# The pinned function's unchanged entry and exit around the replacement body.
.text
.globl patched_roundtrip
patched_roundtrip:
    push %rbp
    mov %rsp, %rbp
    push %r15
    push %r14
    push %r13
    push %r12
    push %rbx
    sub $0x18, %rsp
.include "scripts/patches/hwc-roundtrip.S"
epilogue:
    mov %r15d, %eax
    add $0x18, %rsp
    pop %rbx
    pop %r12
    pop %r13
    pop %r14
    pop %r15
    pop %rbp
    ret
.section .note.GNU-stack,"",@progbits

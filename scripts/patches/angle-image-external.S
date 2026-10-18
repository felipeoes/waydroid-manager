# SPDX-License-Identifier: GPL-3.0-or-later
# ANGLE's shader translator: "#extension GL_OES_EGL_image_external" also sets the behavior of
# GL_OES_EGL_image_external_essl3, so ESSL 3.x shaders that name the ESSL 1 extension (Unreal
# Engine on NVIDIA does) get the essl3 texture() builtins, as NVIDIA's own drivers allow.
# Entered by a jmp that replaces TDirectiveHandler::handleExtension's store of the behavior
# into its std::map node and the instruction after it (NEXT); rax, rcx, rdx and rsi are dead
# there. Placed past the end of the executable segment's file bytes and the TLS segment's
# (file-less) .tbss that follows them; the patch extends the executable segment over it.
# Per build (container.ANGLE_PATCHES), as -D flags:
#   16: EXT=0x29 ESSL3=0x2a NODE=r13 BEH=r14b THIS=r12 'NEXT=mov rbx,[rbp-0x30]' CAVE=0x580990 BACK=0x4c8a66
#   15: EXT=0x27 ESSL3=0x28 NODE=r13 BEH=r12b THIS=r14 'NEXT=mov rdi,[rbp-0x38]' CAVE=0x59d180 BACK=0x4e70e4
#   17: EXT=0x28 ESSL3=0x29 NODE=rbx BEH=r13b THIS=r14 'NEXT=mov rbx,[rbp-0x38]' CAVE=0x590630 BACK=0x4e94d7
# Reproduce the bytes with:
#   cpp -P $FLAGS angle-image-external.S > /tmp/cave.s && as --64 /tmp/cave.s -o /tmp/cave.o
#   ld -Ttext=$CAVE --defsym back=$BACK /tmp/cave.o -o /tmp/cave.elf
#   objcopy -O binary --only-section=.text /tmp/cave.elf /tmp/cave.bin

.intel_syntax noprefix
.text
.globl _start
_start:
    mov byte ptr [NODE + 0x1a], BEH
    cmp byte ptr [NODE + 0x19], EXT
    jne done
    mov rcx, [THIS + 0x10]          # the extension behavior std::map
    mov rsi, [rcx + 8]              # its root
    add rcx, 8                      # its end node
    mov rdx, rcx
walk:
    test rsi, rsi
    je found
    cmp byte ptr [rsi + 0x19], ESSL3
    jb right
    mov rdx, rsi
    mov rsi, [rsi]
    jmp walk
right:
    mov rsi, [rsi + 8]
    jmp walk
found:
    cmp rdx, rcx
    je done
    cmp byte ptr [rdx + 0x19], ESSL3
    jne done
    mov byte ptr [rdx + 0x1a], BEH
done:
    NEXT
    jmp back

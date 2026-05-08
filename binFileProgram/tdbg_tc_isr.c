/*
 * tdbg_tc_isr.c — TC2 channel 0 compare-match ISR for the TDBG playback
 * engine.
 *
 * Why a standalone .c file (not inside the .ino):
 *
 * Arduino IDE 2.x compiles .ino files as C++ AND runs a ctags-based
 * "auto-prototype" generator that injects forward declarations for
 * every function it finds at the TOP of the .ino. For a vector-table
 * override like TC6_Handler, that generated prototype is plain
 *
 *     void TC6_Handler(void);
 *
 * with no `extern "C"`, which pins the symbol's first-declared
 * linkage to C++. Subsequent `extern "C" void TC6_Handler(void) {...}`
 * in the .ino disagrees with that linkage; GCC sometimes accepts the
 * build but resolves the symbol with C++ name mangling
 * (`_Z11TC6_Handlerv`), which doesn't match the C-linkage weak alias
 * the SAM core's `startup_sam3xa.c` uses for the vector slot. The
 * vector stays bound to `Dummy_Handler` (a `while(1);` infinite loop)
 * and the first CPCS interrupt freezes the MCU. Symptom: PLAY_STARTED
 * comes out OK, then no further pin transitions, no PLAY_DONE,
 * subsequent LOAD commands silently ignored because the CPU is stuck
 * in IRQ context inside Dummy_Handler.
 *
 * .c files in the Arduino sketch folder are compiled by `gcc` (not
 * `g++`), so symbols use C linkage natively and the auto-prototype
 * pass doesn't touch them. Defining TC6_Handler here guarantees the
 * weak alias is overridden as the SAM core expects.
 *
 * State variables this ISR consumes are declared in the .ino as
 * non-static globals in an `extern "C"` block, which gives them C
 * linkage so the `extern` references below resolve at link time.
 */

#include <sam.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

/* Mirror of the constants the ISR uses. The .ino has the same defines;
 * these stay in sync because both are tiny and rarely change. */
#define TDBG_EVENT_BYTES   5U
#define TDBG_TC_CHUNK_CPU  65536U

/* State shared with binFileProgram.ino — see the `extern "C"` block
 * around the state-var definitions in the .ino. */
extern Pio*                    tdbgPort;
extern uint32_t                tdbgMask;
extern volatile const uint8_t* tdbgPlayPtr;
extern volatile uint16_t       tdbgPlayLeft;
extern volatile uint16_t       tdbgTcDeadline;
extern volatile uint32_t       tdbgRemainCpu;
extern volatile bool           tdbgPlayDone;

/* Inline helper: deadline_inc = step >> 1, floored at 1.
 *
 * Floor matters because SAM3X TC compare match is edge-triggered (CV
 * transitioning into equality with RC). If we computed an increment of
 * 0 — possible if a delta is 0 or 1 cycle — RC stays at the previous
 * value, CV is already past it, and the next match has to wait a full
 * 16-bit counter wrap (~1.56 ms penalty). Forcing >= 1 makes RC
 * advance at least one tick on every write so the next CV increment
 * produces a clean edge. */
static inline uint16_t deadline_inc_floor(uint32_t step) {
    uint16_t inc = (uint16_t)(step >> 1);
    if (inc == 0) inc = 1;
    return inc;
}

void TC6_Handler(void) {
    /* Acknowledge the compare flag (read of SR clears CPCS). */
    uint32_t sr = TC2->TC_CHANNEL[0].TC_SR;
    (void)sr;

    /* Sanity probe — D13 (LED_BUILTIN, PB27) is pre-configured as an
     * output by tdbgPlayOnceTc setup; here we just SODR it on every
     * ISR entry. SODR is idempotent so this is a one-cycle no-op on
     * subsequent entries. The disarm paths below CODR it off so each
     * play attempt has its own visible "ISR ran" signal. */
    PIOB->PIO_SODR = (1u << 27);

    /* Still mid-wait inside a long-gap chunk: schedule the next chunk
     * but do not write the pin or advance the event pointer. */
    if (tdbgRemainCpu > 0) {
        uint32_t step = (tdbgRemainCpu > TDBG_TC_CHUNK_CPU)
                        ? TDBG_TC_CHUNK_CPU : tdbgRemainCpu;
        tdbgRemainCpu -= step;
        tdbgTcDeadline = (uint16_t)(tdbgTcDeadline + deadline_inc_floor(step));
        TC2->TC_CHANNEL[0].TC_RC = tdbgTcDeadline;
        return;
    }

    /* remain == 0 → this compare lands at the event-fire time. */
    if (tdbgPlayLeft == 0) {
        /* Trailing tick after the last fire; nothing left. Disarm.
         * Order: mask source IRQ in TC, stop clock, drain stale SR
         * flag, then mask in NVIC and clear pending. Without the SR
         * drain + pending clear, a CPCS that asserted between IDR and
         * CLKDIS would leave a stale pending bit, and the NEXT play's
         * NVIC_EnableIRQ would re-fire this disarm branch immediately
         * before any real work began. */
        TC2->TC_CHANNEL[0].TC_IDR = TC_IDR_CPCS;
        TC2->TC_CHANNEL[0].TC_CCR = TC_CCR_CLKDIS;
        (void)TC2->TC_CHANNEL[0].TC_SR;
        NVIC_DisableIRQ(TC6_IRQn);
        NVIC_ClearPendingIRQ(TC6_IRQn);
        PIOB->PIO_CODR = (1u << 27);   /* LED probe off — playback complete */
        tdbgPlayDone = true;
        return;
    }

    /* Drive the pin for the current event. */
    uint8_t state = tdbgPlayPtr[4];
    if (state) tdbgPort->PIO_SODR = tdbgMask;
    else       tdbgPort->PIO_CODR = tdbgMask;

    /* Advance to next event. */
    tdbgPlayPtr  += TDBG_EVENT_BYTES;
    tdbgPlayLeft--;
    if (tdbgPlayLeft == 0) {
        /* No more events — schedule one trailing tick so the next ISR
         * takes the disarm branch above. 64 TC ticks ≈ 1.5 µs of dead
         * time, comfortably above the ISR round-trip cost so the next
         * compare actually lands ahead of CV. */
        tdbgTcDeadline = (uint16_t)(tdbgTcDeadline + 64);
        TC2->TC_CHANNEL[0].TC_RC = tdbgTcDeadline;
        return;
    }

    /* Load next event's delta into the chunked accumulator. Schedule
     * the first chunk (or the whole thing if it fits in 16 bits). */
    uint32_t delta_cpu;
    memcpy(&delta_cpu, (const void*)tdbgPlayPtr, sizeof(delta_cpu));
    tdbgRemainCpu = delta_cpu;
    uint32_t step = (tdbgRemainCpu > TDBG_TC_CHUNK_CPU)
                    ? TDBG_TC_CHUNK_CPU : tdbgRemainCpu;
    tdbgRemainCpu -= step;
    tdbgTcDeadline = (uint16_t)(tdbgTcDeadline + deadline_inc_floor(step));
    TC2->TC_CHANNEL[0].TC_RC = tdbgTcDeadline;
}

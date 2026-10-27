"""Change the number of measures of one section of a user pattern, over MIDI.

    python set_measures.py PATTERN SECTION MEASURES [--fill] [--send] [--in-port X --out-port Y]

Pulls the pattern with a dump request, edits the measures byte for SECTION in the
first header block, and (with --send) writes the whole pattern back in the order
the unit dumped it, framed by bulk mode ON / OFF. Without --send it only writes
the files and prints what it would send.

SECTION is intro, maina, mainb, fillab, fillba or ending (or 0 to 5).
--fill also repeats the notes of every track in that section so they last the
whole new length; without it the tracks keep their old length and only the
header changes.

Files written next to this script, in dumps/: <name>-backup.syx (what the unit
sent) and <name>-edited.syx (what would be sent). To undo, send the backup with
send_blob framed by bulk mode, or use the unit's own reload.
"""
import argparse
import os
import sys
import time

import mido

from qy100syx import patternfmt as F
from qy100syx import protocol as P
from qy100syx import transfer as T

KEYS = {"intro": 0, "maina": 1, "mainb": 2, "fillab": 3, "fillba": 4, "ending": 5}
BAR = 4 * F.CLOCKS_PER_QUARTER      # 4/4 only; the header's signature is checked


def rebuild(m, data):
    """Same message with new unpacked data (128 bytes), checksum recomputed."""
    return P.build_dump(m.addr, F.pack(bytes(data)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pattern", type=int, help="user pattern 1-64")
    ap.add_argument("section")
    ap.add_argument("measures", type=int)
    ap.add_argument("--fill", action="store_true")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--in-port", default="U2MIDI Pro 0")
    ap.add_argument("--out-port", default="U2MIDI Pro 1")
    a = ap.parse_args()

    sec = KEYS[a.section.lower()] if a.section.lower() in KEYS else int(a.section)
    if not 1 <= a.measures <= 128:
        raise SystemExit("measures must be 1-128 (the byte holds measures minus one)")
    n = a.pattern - 1

    inp = mido.open_input(a.in_port)
    outp = mido.open_output(a.out_port)
    T.silenciar_reloj(inp, print)
    try:
        outp.send(mido.Message("sysex", data=P.bulk_mode(True)[1:-1]))
        time.sleep(0.3)
        blob, _ = T.request(outp, inp, P.Addr.PATTERN_ALL, 2.5, 120, print)
    finally:
        outp.send(mido.Message("sysex", data=P.bulk_mode(False)[1:-1]))
        time.sleep(0.3)

    raws = P.split_sysex(blob)
    msgs = [P.Message(r) for r in raws]
    mine = [m for m in msgs if m.sub == P.SUB_DUMP and m.addr[0] == 0x12 and m.addr[1] == n]
    if not mine:
        raise SystemExit("pattern %d came back empty" % a.pattern)
    for m in mine:
        if not m.verify():
            raise SystemExit("checksum mismatch in the dump, not touching anything")

    # the message builder must reproduce the unit's own bytes exactly
    for m in mine[:3]:
        if rebuild(m, F.unpack(m.data)) != m.raw:
            raise SystemExit("rebuilt message differs from the unit's: refusing")

    os.makedirs("dumps", exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join("dumps", "pattern%02d-%s" % (a.pattern, stamp))
    open(base + "-backup.syx", "wb").write(b"".join(m.raw for m in mine))

    hdr = [m for m in mine if m.addr[2] == F.HEADER_TR]
    name, meas = F.decode_header(hdr[0].data)
    print("pattern %d '%s' measures %s" % (a.pattern, name, meas))
    d = bytearray(F.unpack(hdr[0].data))
    ts = d[F.TIME_SIG_OFF]
    out = []
    new_hdr0 = None
    for m in mine:
        if m is hdr[0]:
            d[F.MEASURES_OFF + sec] = a.measures - 1
            new_hdr0 = rebuild(m, d)
            out.append(new_hdr0)
        else:
            out.append(m.raw)
    print("section %s: %d -> %d measures" % (F.SECTIONS[sec], meas[sec], a.measures))

    if a.fill:
        tr_lo, tr_hi = sec * F.TRACKS_PER_SECTION, (sec + 1) * F.TRACKS_PER_SECTION
        out = []
        done = set()
        for m in mine:
            if m is hdr[0]:
                out.append(new_hdr0)
            elif tr_lo <= m.addr[2] < tr_hi:
                if m.addr[2] in done:
                    continue                    # the chain is re-emitted with the first block
                done.add(m.addr[2])
                chain = [x for x in mine if x.addr[2] == m.addr[2]]
                pl = [x.data for x in chain]
                notes, clocks = F.decode_blocks(pl)
                prefix = F.unpack(pl[0])[:F.EVENT_STREAM_START]
                if F.encode_blocks(notes, clocks, prefix) != pl:
                    raise SystemExit("track %d has events beyond notes (CC etc.): "
                                     "--fill would drop them, refusing" % m.addr[2])
                reps = -(-a.measures * BAR // clocks) if clocks else 1
                tiled = [F.Note(x.pitch, x.velocity, x.gate, x.time + r * clocks)
                         for r in range(reps) for x in notes]
                tiled = [x for x in tiled if x.time < a.measures * BAR]
                for p in F.encode_blocks(tiled, a.measures * BAR, prefix):
                    out.append(P.build_dump(m.addr, p))
                print("track %d: %d notes x %d -> %d blocks" % (m.addr[2], len(notes),
                                                                reps, len(chain)))
            else:
                out.append(m.raw)
    open(base + "-edited.syx", "wb").write(b"".join(out))
    print("%d messages (was %d). backup/edited saved as %s-*.syx" % (len(out), len(mine), base))

    if not a.send:
        print("dry run: nothing sent")
        return
    print("sending ...")
    T.send_pattern(outp, out, log=print)
    print("done")
    inp.close()
    outp.close()


if __name__ == "__main__":
    main()
